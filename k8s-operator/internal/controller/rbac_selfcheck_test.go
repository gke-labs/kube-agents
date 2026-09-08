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
	"os"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	authorizationv1client "k8s.io/client-go/kubernetes/typed/authorization/v1"
	k8stesting "k8s.io/client-go/testing"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// generatedRolePath is controller-gen's output for the markers above
// Reconcile, relative to this package directory (where `go test` runs).
const generatedRolePath = "../../config/rbac/role.yaml"

// fakeAuthorizer answers SelfSubjectAccessReviews from a fake clientset. deny
// decides per review; err, when set, fails every review instead. Both are
// read under a lock because the ticker test changes them while the checker's
// goroutine is probing.
type fakeAuthorizer struct {
	mu   sync.Mutex
	deny func(*authorizationv1.ResourceAttributes) bool
	err  error
}

func (f *fakeAuthorizer) set(deny func(*authorizationv1.ResourceAttributes) bool, err error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.deny, f.err = deny, err
}

func (f *fakeAuthorizer) reviews() authorizationv1client.SelfSubjectAccessReviewInterface {
	clientset := k8sfake.NewClientset()
	clientset.PrependReactor("create", "selfsubjectaccessreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		f.mu.Lock()
		deny, err := f.deny, f.err
		f.mu.Unlock()
		if err != nil {
			return true, nil, err
		}
		review := action.(k8stesting.CreateAction).GetObject().(*authorizationv1.SelfSubjectAccessReview)
		review.Status.Allowed = deny == nil || !deny(review.Spec.ResourceAttributes)
		return true, review, nil
	})
	return clientset.AuthorizationV1().SelfSubjectAccessReviews()
}

func denyPDBPatch(attrs *authorizationv1.ResourceAttributes) bool {
	return attrs.Group == "policy" && attrs.Resource == "poddisruptionbudgets" && attrs.Verb == "patch"
}

func permissionSet(rules []rbacv1.PolicyRule) map[string]struct{} {
	set := map[string]struct{}{}
	for _, rule := range rules {
		for _, group := range rule.APIGroups {
			for _, resource := range rule.Resources {
				for _, verb := range rule.Verbs {
					set[describePermission(verb, group, resource)] = struct{}{}
				}
			}
		}
	}
	return set
}

func sortedDifference(a, b map[string]struct{}) []string {
	var out []string
	for key := range a {
		if _, ok := b[key]; !ok {
			out = append(out, key)
		}
	}
	sort.Strings(out)
	return out
}

// TestRequiredPermissionsMatchTheGeneratedRole is what keeps the probe's copy
// of the RBAC markers honest: the markers are comments and role.yaml is not in
// the binary, so requiredPermissions is a second copy by necessity, and a
// marker change that forgets it would leave the self-check silently checking
// yesterday's role.
func TestRequiredPermissionsMatchTheGeneratedRole(t *testing.T) {
	raw, err := os.ReadFile(generatedRolePath)
	if err != nil {
		t.Fatalf("read %s: %v", generatedRolePath, err)
	}
	var role rbacv1.ClusterRole
	if err := yaml.Unmarshal(raw, &role); err != nil {
		t.Fatalf("parse %s: %v", generatedRolePath, err)
	}
	generated := permissionSet(role.Rules)

	var declared []rbacv1.PolicyRule
	for _, permission := range requiredPermissions {
		declared = append(declared, rbacv1.PolicyRule{
			APIGroups: []string{permission.Group},
			Resources: permission.Resources,
			Verbs:     permission.Verbs,
		})
	}
	probed := permissionSet(declared)

	if missing := sortedDifference(generated, probed); len(missing) > 0 {
		t.Errorf("role.yaml grants permissions the self-check does not probe; add them to requiredPermissions in rbac_selfcheck.go:\n  %s",
			strings.Join(missing, "\n  "))
	}
	if extra := sortedDifference(probed, generated); len(extra) > 0 {
		t.Errorf("the self-check probes permissions role.yaml does not grant; every install would report them denied. Remove them from requiredPermissions or add the marker and run make manifests:\n  %s",
			strings.Join(extra, "\n  "))
	}
}

func TestProbeRBACNamesEachDeniedPermission(t *testing.T) {
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		return denyPDBPatch(attrs) ||
			(attrs.Resource == "pods" && attrs.Subresource == "log" && attrs.Verb == "get")
	}}
	denied, err := probeRBAC(context.Background(), authorizer.reviews())
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	want := []string{"get pods/log", "patch poddisruptionbudgets.policy"}
	got := append([]string(nil), denied...)
	sort.Strings(got)
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("denied = %v, want %v", denied, want)
	}
}

func TestProbeRBACWithEverythingGrantedReportsNothing(t *testing.T) {
	denied, err := probeRBAC(context.Background(), (&fakeAuthorizer{}).reviews())
	if err != nil {
		t.Fatalf("probe failed: %v", err)
	}
	if len(denied) != 0 {
		t.Fatalf("expected no denials, got %v", denied)
	}
}

func TestProbeRBACErrorIsNotADenial(t *testing.T) {
	authorizer := &fakeAuthorizer{err: errors.New("the API server is away")}
	denied, err := probeRBAC(context.Background(), authorizer.reviews())
	if err == nil {
		t.Fatal("expected the transport error to surface")
	}
	if denied != nil {
		t.Fatalf("a probe that could not run must not report denials, got %v", denied)
	}
}

func TestTheCheckerReprobesOnItsOwnTickerAndKeepsTheLastAnswerOnError(t *testing.T) {
	authorizer := &fakeAuthorizer{deny: denyPDBPatch}
	checker := NewRBACChecker(authorizer.reviews())
	checker.interval = 10 * time.Millisecond
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	if denied, err := checker.Probe(ctx); err != nil || len(denied) != 1 {
		t.Fatalf("boot probe = %v, %v; want the one denial", denied, err)
	}
	if got := checker.Denied(); len(got) != 1 {
		t.Fatalf("Denied should read the boot answer, got %v", got)
	}

	done := make(chan struct{})
	go func() { _ = checker.Start(ctx); close(done) }()

	// The role is fixed; the ticker notices without anyone calling Denied.
	authorizer.set(nil, nil)
	waitFor(t, func() bool { return len(checker.Denied()) == 0 }, "the ticker should clear the denial")

	// A probe that cannot run keeps the previous (clean) answer rather than
	// raising a false alarm; a later denial is picked up once probes work.
	authorizer.set(nil, errors.New("timeout"))
	time.Sleep(5 * checker.interval)
	if got := checker.Denied(); len(got) != 0 {
		t.Fatalf("a failed probe must keep the previous answer; got %v", got)
	}
	authorizer.set(denyPDBPatch, nil)
	waitFor(t, func() bool { return len(checker.Denied()) == 1 }, "a new denial should be picked up")

	cancel()
	<-done
}

func waitFor(t *testing.T, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal(what)
}

func TestProbeRBACReportsDenialsInDeclarationOrder(t *testing.T) {
	// The reviews run concurrently; the report must not depend on which
	// finished first, because the condition message compares by text.
	authorizer := &fakeAuthorizer{deny: func(attrs *authorizationv1.ResourceAttributes) bool {
		return attrs.Verb == "delete"
	}}
	var want []string
	for _, tuple := range flattenRequiredPermissions() {
		if tuple.verb == "delete" {
			want = append(want, tuple.label)
		}
	}
	for i := 0; i < 5; i++ {
		denied, err := probeRBAC(context.Background(), authorizer.reviews())
		if err != nil {
			t.Fatalf("probe failed: %v", err)
		}
		if strings.Join(denied, ",") != strings.Join(want, ",") {
			t.Fatalf("run %d: denied = %v, want declaration order %v", i, denied, want)
		}
	}
}

func TestProbeRBACHonoursItsTimeout(t *testing.T) {
	clientset := k8sfake.NewClientset()
	clientset.PrependReactor("create", "selfsubjectaccessreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		time.Sleep(50 * time.Millisecond)
		review := action.(k8stesting.CreateAction).GetObject().(*authorizationv1.SelfSubjectAccessReview)
		review.Status.Allowed = true
		return true, review, nil
	})
	checker := NewRBACChecker(clientset.AuthorizationV1().SelfSubjectAccessReviews())
	checker.timeout = 20 * time.Millisecond
	started := time.Now()
	denied, err := checker.Probe(context.Background())
	if err == nil {
		t.Fatal("a probe past its deadline should report an error, not an answer")
	}
	if denied != nil || len(checker.Denied()) != 0 {
		t.Fatalf("a timed-out probe must not record denials, got %v / %v", denied, checker.Denied())
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("the deadline did not stop the probe: %v", elapsed)
	}
}

func TestANilCheckerNeverProbes(t *testing.T) {
	var checker *RBACChecker
	if got := checker.Denied(); got != nil {
		t.Fatalf("nil checker reported %v", got)
	}
	if err := checker.Start(context.Background()); err != nil {
		t.Fatalf("nil checker Start = %v", err)
	}
	if denied, err := checker.Probe(context.Background()); denied != nil || err != nil {
		t.Fatalf("nil checker Probe = %v, %v", denied, err)
	}
}

// TestReconcileReportsAnOutOfDateClusterRoleAndKeepsGoing is the incident in
// #1009 as a test: the ClusterRole lacks `patch poddisruptionbudgets`, and
// the operator has to say so on the CR rather than only in an error loop —
// while still reconciling everything else, because a missing verb on one
// step is not a reason to withhold the workload.
func TestReconcileReportsAnOutOfDateClusterRoleAndKeepsGoing(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
		Spec: agentv1alpha1.PlatformAgentSpec{},
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	authorizer := &fakeAuthorizer{deny: denyPDBPatch}
	checker := NewRBACChecker(authorizer.reviews())
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	result, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile failed: %v", err)
	}
	// Re-applying the manifests triggers no reconcile, so while the condition
	// stands the controller has to come back on its own to notice the fix.
	if result.RequeueAfter != rbacReprobeInterval {
		t.Errorf("RequeueAfter = %v while RBAC is incomplete, want %v", result.RequeueAfter, rbacReprobeInterval)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatalf("re-read agent: %v", err)
	}
	degraded := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType)
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != reasonRBACIncomplete {
		t.Fatalf("expected Degraded=True/%s on the CR, got %+v", reasonRBACIncomplete, degraded)
	}
	for _, want := range []string{"patch poddisruptionbudgets.policy", RBACIncompleteCause} {
		if !strings.Contains(degraded.Message, want) {
			t.Errorf("condition message %q does not name %q", degraded.Message, want)
		}
	}
	// Ready is still whatever the reconcile decided; the skew is reported
	// beside it, not instead of it.
	if ready := meta.FindStatusCondition(stored.Status.Conditions, "Ready"); ready == nil {
		t.Error("the Ready condition went missing; the self-check must not replace updateStatusReady")
	}

	// The rest of the reconcile ran: the workload exists.
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, &appsv1.Deployment{}); err != nil {
		t.Fatalf("the self-check withheld the workload: %v", err)
	}

	// Re-apply the role; the ticker's next probe sees it and the requeued
	// reconcile clears the condition.
	authorizer.set(nil, nil)
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("re-probe: %v", err)
	}
	result, err = r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("second Reconcile failed: %v", err)
	}
	if err := cl.Get(ctx, req.NamespacedName, stored); err != nil {
		t.Fatalf("re-read agent: %v", err)
	}
	if degraded := meta.FindStatusCondition(stored.Status.Conditions, degradedConditionType); degraded != nil {
		t.Fatalf("the condition should clear once the role is current, still have %+v", degraded)
	}
	if result.RequeueAfter == rbacReprobeInterval {
		t.Error("the RBAC poll should stop once nothing is denied")
	}
}

// TestAStandingRBACDenialDoesNotWriteStatusEveryPass guards against the loop
// the report is meant to replace: the PlatformAgent watch has no generation
// filter, so a status write on every reconcile re-enqueues the agent at once.
// While a denial stands, a pass that changes nothing must write nothing.
func TestAStandingRBACDenialDoesNotWriteStatusEveryPass(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "test-agent",
			Namespace:  "test-ns",
			Finalizers: []string{platformAgentFinalizer},
		},
	}
	statusWrites := 0
	funcs := fakeServerSideApplyInterceptors()
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		statusWrites++
		return cl.SubResource(subResourceName).Update(ctx, obj, opts...)
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(funcs).
		Build()
	checker := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews())
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("first Reconcile failed: %v", err)
	}
	if statusWrites == 0 {
		t.Fatal("the first pass should have written the condition")
	}
	for pass := 2; pass <= 4; pass++ {
		before := statusWrites
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", pass, err)
		}
		if statusWrites != before {
			t.Fatalf("pass %d wrote status %d time(s) with nothing changed; that re-enqueues the agent forever", pass, statusWrites-before)
		}
	}
}

// TestTheRBACSkewYieldsToAnotherDegradedReason pins the ownership rule: while
// updateStatusReady's InvalidGitRepoURL holds the Degraded condition, the RBAC
// report neither overwrites it nor flaps it, and still asks for the poll.
func TestTheRBACSkewYieldsToAnotherDegradedReason(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
	}
	foreign := metav1.Condition{
		Type: degradedConditionType, Status: metav1.ConditionTrue,
		Reason: "InvalidGitRepoURL", Message: "not a URL", LastTransitionTime: metav1.Now(),
	}
	meta.SetStatusCondition(&agent.Status.Conditions, foreign)
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		Build()
	checker := NewRBACChecker((&fakeAuthorizer{deny: denyPDBPatch}).reviews())
	if _, err := checker.Probe(context.Background()); err != nil {
		t.Fatalf("boot probe: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, RBAC: checker}

	degraded, err := r.reportRBACSkew(context.Background(), agent)
	if err != nil {
		t.Fatalf("reportRBACSkew: %v", err)
	}
	if !degraded {
		t.Error("a denial must still be reported as degraded so the caller keeps polling")
	}
	got := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	if got == nil || got.Reason != foreign.Reason || got.Message != foreign.Message {
		t.Fatalf("the foreign Degraded reason was overwritten: %+v", got)
	}
}
