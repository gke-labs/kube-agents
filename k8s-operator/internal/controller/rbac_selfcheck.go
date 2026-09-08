/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"time"

	authorizationv1 "k8s.io/api/authorization/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	authorizationv1client "k8s.io/client-go/kubernetes/typed/authorization/v1"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The operator's image and its ClusterRole ship together only when whoever
// deploys it re-applies both. A `make deploy` from a floating tag, or a
// `kubectl set image`, moves the image on the next pod reschedule and leaves
// the role where it was, and the first verb the newer controller needs that
// the older role lacks becomes an unbounded `Reconciler error` loop that
// reads like an RBAC misconfiguration (#1009). Nothing in that loop says the
// role is simply older than the binary.
//
// So the controller asks the API server, once at startup and again every
// rbacReprobeInterval, whether it holds every permission its own RBAC markers
// declare, and reports the ones it does not: one log line at boot, and a
// Degraded condition on every PlatformAgent it reconciles. It does not stop
// reconciling — a verb used only on an optional path (tokenreviews for the
// split broker, fqdnnetworkpolicies) must not take a working install down,
// and a bail-out withholds guardrails (#964). Steps that hit a forbidden error
// still fail exactly as before; the difference is that the status now names
// the cause.
//
// The probe never runs on the reconcile worker. It is one
// SelfSubjectAccessReview per (verb, resource) — 179 of them — and the
// PlatformAgent controller has a single worker, so a reconcile that paid for
// the round trips would stall every agent for their duration. The checker is
// a manager Runnable that re-probes on its own ticker under a deadline;
// Reconcile only reads the last answer.
//
// SelfSubjectAccessReview needs no grant of its own: every authenticated
// subject may create one through the bootstrap system:basic-user ClusterRole,
// so role.yaml gains nothing for this probe.
const (
	// reasonRBACIncomplete is the Degraded condition reason while any required
	// permission is denied.
	reasonRBACIncomplete = "RBACIncomplete"
	// degradedConditionType is the condition updateStatusReady already owns;
	// the RBAC self-check shares it rather than inventing a fourth type.
	degradedConditionType = "Degraded"
	// rbacReprobeInterval is the checker's own tick, and the requeue a reconcile
	// asks for while a denial stands, so a re-applied ClusterRole clears the
	// condition without an operator restart or an unrelated event.
	rbacReprobeInterval = 5 * time.Minute
	// RBACProbeTimeout bounds one probe. The boot probe runs before the manager
	// and its health endpoints start, so an API server that accepts the
	// connection and never answers must not hold the pod there past its
	// liveness probe; the ticker probe must not pile up behind a slow one.
	RBACProbeTimeout = 30 * time.Second
	// rbacProbeConcurrency caps the reviews in flight at once. The calls are
	// independent and evaluated locally by the API server's authorizer, so
	// they parallelise well; the cap keeps a probe from opening every one of
	// them at once against a struggling API server.
	rbacProbeConcurrency = 16
	// RBACIncompleteCause is the sentence the boot log line and the condition
	// both carry: what happened and the two ways to fix it. Exported for main.go.
	RBACIncompleteCause = "the ClusterRole bound to the operator is older than this image; " +
		"re-apply k8s-operator/config (make deploy) or run helm upgrade so RBAC and image ship together"
	// rbacIncompleteMessagePrefix opens the condition message; the denied
	// permissions follow it.
	rbacIncompleteMessagePrefix = "The operator is denied permissions its RBAC markers declare: "
	// rbacProbeLogName is the logger name for lines the checker writes outside
	// a reconcile.
	rbacProbeLogName = "rbac-selfcheck"
	// rbacSubresourceSeparator splits "pods/log" into resource and subresource.
	rbacSubresourceSeparator = "/"
)

// requiredPermission is one rule the controller needs, in the shape the
// +kubebuilder:rbac markers above Reconcile declare it.
type requiredPermission struct {
	Group     string
	Resources []string
	Verbs     []string
}

var (
	rbacReadVerbs  = []string{"get", "list", "watch"}
	rbacWriteVerbs = []string{"get", "list", "watch", "create", "update", "patch", "delete"}
)

// requiredPermissions mirrors the +kubebuilder:rbac markers above Reconcile,
// which controller-gen turns into config/rbac/role.yaml. The markers are
// comments and role.yaml is not in the binary, so this is a second copy by
// necessity; TestRequiredPermissionsMatchTheGeneratedRole parses role.yaml and
// fails on any difference in either direction, so a marker change that forgets
// this list does not build green.
var requiredPermissions = []requiredPermission{
	{Group: "kubeagents.x-k8s.io", Resources: []string{"platformagents"}, Verbs: rbacWriteVerbs},
	{Group: "kubeagents.x-k8s.io", Resources: []string{"platformagents/status"}, Verbs: []string{"get", "list", "watch", "update", "patch"}},
	{Group: "kubeagents.x-k8s.io", Resources: []string{"platformagents/finalizers"}, Verbs: []string{"update"}},
	{Group: "kubeagents.x-k8s.io", Resources: []string{"agentplugins"}, Verbs: rbacReadVerbs},
	{Group: "kubeagents.x-k8s.io", Resources: []string{"agentplugins/status"}, Verbs: []string{"get", "update", "patch"}},
	{Group: "apps", Resources: []string{"deployments", "statefulsets"}, Verbs: rbacWriteVerbs},
	{Group: "apps", Resources: []string{"daemonsets", "replicasets"}, Verbs: rbacReadVerbs},
	{Group: "", Resources: []string{"serviceaccounts", "persistentvolumeclaims", "configmaps", "services", "pods"}, Verbs: rbacWriteVerbs},
	{Group: "", Resources: []string{"namespaces", "nodes", "events", "persistentvolumes", "limitranges", "endpoints", "pods/log"}, Verbs: rbacReadVerbs},
	// resourcequotas: the mode-next session-pod bound is rendered and removed
	// by the operator, so this one is write rather than read.
	{Group: "", Resources: []string{"resourcequotas"}, Verbs: rbacWriteVerbs},
	// secrets: `get` for checkShellSandboxKeys; the rest for the mode-next A2A
	// credential and config Secrets the operator generates. No list/watch --
	// see tests/test_operator_secrets_grant.py.
	{Group: "", Resources: []string{"secrets"}, Verbs: []string{"get", "create", "update", "patch", "delete"}},
	{Group: "metrics.k8s.io", Resources: []string{"nodes", "pods"}, Verbs: rbacReadVerbs},
	{Group: "autoscaling", Resources: []string{"horizontalpodautoscalers"}, Verbs: rbacReadVerbs},
	{Group: "batch", Resources: []string{"cronjobs"}, Verbs: rbacReadVerbs},
	// jobs: the mode-next provisioning run is created and swept by the operator.
	{Group: "batch", Resources: []string{"jobs"}, Verbs: rbacWriteVerbs},
	{Group: "coordination.k8s.io", Resources: []string{"leases"}, Verbs: rbacWriteVerbs},
	{Group: "node.k8s.io", Resources: []string{"runtimeclasses"}, Verbs: rbacReadVerbs},
	{Group: "networking.k8s.io", Resources: []string{"networkpolicies"}, Verbs: rbacWriteVerbs},
	{Group: "networking.k8s.io", Resources: []string{"ingresses"}, Verbs: rbacReadVerbs},
	{Group: "networking.gke.io", Resources: []string{"fqdnnetworkpolicies"}, Verbs: rbacWriteVerbs},
	{Group: "policy", Resources: []string{"poddisruptionbudgets"}, Verbs: rbacWriteVerbs},
	{Group: "rbac.authorization.k8s.io", Resources: []string{"clusterroles", "clusterrolebindings", "roles", "rolebindings"}, Verbs: rbacWriteVerbs},
	{Group: "authentication.k8s.io", Resources: []string{"tokenreviews"}, Verbs: []string{"create"}},
	{Group: "apiextensions.k8s.io", Resources: []string{"customresourcedefinitions"}, Verbs: rbacReadVerbs},
}

// describePermission renders one (verb, resource, group) the way kubectl
// auth can-i reports it: "patch poddisruptionbudgets.policy", "list pods".
func describePermission(verb, group, resource string) string {
	if group == "" {
		return verb + " " + resource
	}
	return verb + " " + resource + "." + group
}

// permissionTuple is one review to send: the flattened (verb, resource) form
// of requiredPermissions, in declaration order.
type permissionTuple struct {
	group, resource, subresource, verb string
	label                              string
}

func flattenRequiredPermissions() []permissionTuple {
	var tuples []permissionTuple
	for _, permission := range requiredPermissions {
		for _, fullResource := range permission.Resources {
			resource, subresource, _ := strings.Cut(fullResource, rbacSubresourceSeparator)
			for _, verb := range permission.Verbs {
				tuples = append(tuples, permissionTuple{
					group: permission.Group, resource: resource, subresource: subresource, verb: verb,
					label: describePermission(verb, permission.Group, fullResource),
				})
			}
		}
	}
	return tuples
}

// probeRBAC asks the API server, one SelfSubjectAccessReview per (verb,
// resource), whether the caller holds every permission in
// requiredPermissions, up to rbacProbeConcurrency at a time. It returns the
// denied ones in declaration order. An error means the probe could not run
// and says nothing about permissions; callers must not read it as a denial.
//
// The reviews carry no namespace: both install paths bind the role through
// a ClusterRoleBinding, so cluster-wide is the grant being checked.
func probeRBAC(ctx context.Context, reviews authorizationv1client.SelfSubjectAccessReviewInterface) ([]string, error) {
	tuples := flattenRequiredPermissions()
	denied := make([]bool, len(tuples))
	errs := make([]error, len(tuples))
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()

	var wg sync.WaitGroup
	slots := make(chan struct{}, rbacProbeConcurrency)
	for i, tuple := range tuples {
		wg.Add(1)
		go func(i int, tuple permissionTuple) {
			defer wg.Done()
			select {
			case slots <- struct{}{}:
				defer func() { <-slots }()
			case <-ctx.Done():
				errs[i] = ctx.Err()
				return
			}
			review := &authorizationv1.SelfSubjectAccessReview{
				Spec: authorizationv1.SelfSubjectAccessReviewSpec{
					ResourceAttributes: &authorizationv1.ResourceAttributes{
						Group:       tuple.group,
						Resource:    tuple.resource,
						Subresource: tuple.subresource,
						Verb:        tuple.verb,
					},
				},
			}
			result, err := reviews.Create(ctx, review, metav1.CreateOptions{})
			if err != nil {
				errs[i] = fmt.Errorf("SelfSubjectAccessReview for %s: %w", tuple.label, err)
				cancel() // one failure fails the probe; stop paying for the rest
				return
			}
			denied[i] = !result.Status.Allowed
		}(i, tuple)
	}
	wg.Wait()

	// Report the failure that started the cancellation rather than the
	// cancellations it caused; if only cancellations remain, the caller's
	// own context expired.
	var cancelled error
	for _, err := range errs {
		if err == nil {
			continue
		}
		if errors.Is(err, context.Canceled) {
			cancelled = err
			continue
		}
		return nil, err
	}
	if cancelled != nil {
		return nil, cancelled
	}
	var out []string
	for i, tuple := range tuples {
		if denied[i] {
			out = append(out, tuple.label)
		}
	}
	return out, nil
}

// RBACChecker holds the most recent probe result and, once started as a
// manager Runnable, refreshes it every interval off the reconcile path. A nil
// *RBACChecker, which is what tests and the golden harness supply, never
// probes and reports nothing denied.
type RBACChecker struct {
	reviews authorizationv1client.SelfSubjectAccessReviewInterface
	// interval and timeout are fields so tests can shorten them.
	interval time.Duration
	timeout  time.Duration

	mu     sync.Mutex
	denied []string
	probed bool
}

// NewRBACChecker returns a checker that probes through reviews.
func NewRBACChecker(reviews authorizationv1client.SelfSubjectAccessReviewInterface) *RBACChecker {
	return &RBACChecker{reviews: reviews, interval: rbacReprobeInterval, timeout: RBACProbeTimeout}
}

// Probe runs the self-check now under the checker's timeout, remembers the
// answer, and returns it. main.go calls it once before the manager starts so
// the boot log carries the result before any reconcile; the ticker calls it
// after. A probe that cannot run keeps the previous answer: a transient API
// error must neither raise a false alarm nor clear a real one.
func (c *RBACChecker) Probe(ctx context.Context) ([]string, error) {
	if c == nil || c.reviews == nil {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	denied, err := probeRBAC(ctx, c.reviews)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.probed && !slices.Equal(c.denied, denied) {
		logf.Log.WithName(rbacProbeLogName).Info("RBAC self-check result changed", "denied", denied)
	}
	c.denied = denied
	c.probed = true
	return append([]string(nil), denied...), nil
}

// Denied returns the last probe's denials. It never talks to the API server:
// the reconcile worker reads the answer, the ticker produces it.
func (c *RBACChecker) Denied() []string {
	if c == nil {
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string(nil), c.denied...)
}

// Start re-probes every interval until ctx is cancelled. It satisfies
// manager.Runnable; main.go adds the checker to the manager after the boot
// probe.
func (c *RBACChecker) Start(ctx context.Context) error {
	if c == nil || c.reviews == nil {
		return nil
	}
	ticker := time.NewTicker(c.interval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if _, err := c.Probe(ctx); err != nil && ctx.Err() == nil {
				logf.Log.WithName(rbacProbeLogName).Error(err, "RBAC self-check could not run; keeping the previous result")
			}
		}
	}
}

// NeedLeaderElection reports false: the probe is about this pod's own
// identity, so every replica runs it, leader or not.
func (c *RBACChecker) NeedLeaderElection() bool { return false }

// rbacIncompleteMessage is the condition message for a set of denials.
func rbacIncompleteMessage(denied []string) string {
	return rbacIncompleteMessagePrefix + strings.Join(denied, ", ") + "; " + RBACIncompleteCause + "."
}

// reportRBACSkew writes or clears the Degraded/RBACIncomplete condition on
// agent to match the checker's last answer, and reports whether any
// permission is denied right now — the caller polls while that is true, since
// the fix (someone re-applying the manifests) triggers no reconcile of its
// own. It leaves a Degraded condition with any other reason alone:
// updateStatusReady owns InvalidGitRepoURL, and two problems at once should
// not make the condition flap between them on every pass — the boot log line
// still names the denied permissions in that case.
func (r *PlatformAgentReconciler) reportRBACSkew(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (bool, error) {
	denied := r.RBAC.Denied()
	degraded := len(denied) > 0
	existing := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	ownsCondition := existing == nil || existing.Reason == reasonRBACIncomplete

	if !degraded {
		if existing == nil || existing.Reason != reasonRBACIncomplete {
			return false, nil
		}
		meta.RemoveStatusCondition(&agent.Status.Conditions, degradedConditionType)
		return false, r.Status().Update(ctx, agent)
	}
	if !ownsCondition {
		return true, nil
	}
	message := rbacIncompleteMessage(denied)
	if existing != nil && existing.Status == metav1.ConditionTrue && existing.Message == message {
		return true, nil
	}
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:               degradedConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             reasonRBACIncomplete,
		Message:            message,
		LastTransitionTime: metav1.Now(),
	})
	return true, r.Status().Update(ctx, agent)
}
