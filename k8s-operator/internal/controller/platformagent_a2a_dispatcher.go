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
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"slices"
	"strings"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The AgentProfile dispatcher (#3655; contract on #3655 and its addenda): the
// only reader of the profiles' shared durables and the only writer of
// PROFILE_LEASES. This file renders what it runs on. The binary is PR 3's.
const (
	// a2aDispatcherImageEnvVar names the dispatcher's image. Unset, the
	// operator renders no dispatcher at all, not its identity and not its
	// Deployment: the image isn't in the release set yet, and a Deployment
	// with no image to pull would leave every next install with a pod stuck
	// in ImagePullBackOff. The dispatcher's PR adds the image to the release
	// set and makes it the default.
	a2aDispatcherImageEnvVar = "A2A_DISPATCHER_IMAGE"

	// a2aDispatcherUser is the dispatcher's NATS user, and so its inbox
	// prefix and its verifier caller name. The binary spells the same string.
	a2aDispatcherUser = "dispatcher"

	a2aDispatcherContainerName = "dispatcher"
	a2aDispatcherStatusPort    = 8080

	// a2aDispatchConfigKey is lib.DispatchConfigKey; this module can't
	// import lib, and no test holds the two equal.
	a2aDispatchConfigKey = "dispatch.json"

	a2aDispatcherCPURequest    = "50m"
	a2aDispatcherMemoryRequest = "64Mi"
	a2aDispatcherCPULimit      = "500m"
	a2aDispatcherMemoryLimit   = "256Mi"

	// The worker pod labels the dispatcher selects pools by
	// (lib.ProfilePoolSelector), and the component value that distinguishes
	// a worker pod from the profile's ServiceAccounts.
	agentProfileWorkerComponent = "agentprofile-worker"
)

func a2aDispatcherImage() string { return strings.TrimSpace(os.Getenv(a2aDispatcherImageEnvVar)) }

// a2aDispatcherEnabled reports whether this operator renders the dispatcher.
func a2aDispatcherEnabled() bool { return a2aDispatcherImage() != "" }

func a2aDispatcherName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-dispatcher"
}

func a2aDispatchConfigName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-a2a-dispatch"
}

func a2aDispatcherPodSelector(agent *agentv1alpha1.PlatformAgent) map[string]string {
	return map[string]string{"app": a2aDispatcherName(agent)}
}

// dispatcherIdentity is the dispatcher's bus principal. Every grant, and why:
//
//   - its durables on PROFILE_SUBMITS (profile-<profile>-<tier>-<kind>,
//     which only the operator creates): INFO to bind, MSG.NEXT to fetch, and
//     $JS.ACK on them to ack, nak and send the in-progress heartbeats a held
//     message lives on. No CONSUMER.CREATE: the durables are the operator's.
//     Each durable is named rather than PROFILE_SUBMITS.*, so a dispatcher
//     reads only the durables its config lists. The grants are fixed for a
//     connection, so the dispatcher reconnects when its config adds a
//     durable (contract on #3655).
//   - PROFILE_LEASES, which it alone writes: put and delete on $KV, INFO and
//     direct get, and CONSUMER.CREATE/DELETE on the bucket's stream for the
//     ordered consumer that lists a (profile, tier)'s keys after a restart.
//   - the hand-off: publish offers and abandons on a2a.dispatch.*.*.<pod>, and
//     read accepts and lease renewals on .accept and .lease beneath them. The
//     pod token in those subjects is the only identity it trusts.
//   - terminal failures on a task's .supervisor subject: a refusal at
//     admission, an expired lease, a deadline. That subject is where a
//     task's supervisor writes the terminal it synthesizes
//     (lib.TaskSupervisorSubject), so .events stays the executor's alone.
//     Named per profile it dispatches for, a2a.tasks.<profile>.*.supervisor,
//     so it can't write a session's or the bridge's. TASKS refuses rollup
//     headers, so this publisher can't purge the stream.
//   - the verifier, for CheckProfileRun, on its own caller subject, with
//     answers in its own reply namespace.
//
// No subscribe on a2a.tasks.> or a2a.submit.>: it reads submissions only
// through its durables, and never a task's .in or .events.
func dispatcherIdentity(agent *agentv1alpha1.PlatformAgent, ns string, profiles, durables []string) a2aIdentity {
	const leases = "KV_PROFILE_LEASES"
	var perProfile []string
	for _, d := range durables {
		perProfile = append(perProfile,
			"$JS.API.CONSUMER.INFO."+a2aProfileSubmitStream+"."+d,
			"$JS.API.CONSUMER.MSG.NEXT."+a2aProfileSubmitStream+"."+d,
			"$JS.ACK."+a2aProfileSubmitStream+"."+d+".>",
		)
	}
	for _, p := range profiles {
		perProfile = append(perProfile, "a2a.tasks."+p+".*.supervisor")
	}
	return a2aIdentity{
		user:    a2aDispatcherUser,
		account: a2aAccountApp,
		comment: "the AgentProfile dispatcher: reads the profiles' durables, writes PROFILE_LEASES, " +
			"hands tasks to workers, and fails a task it can't run (#3655)",
		auth:           a2aAuthCallout,
		serviceAccount: a2aServiceAccountName(ns, a2aDispatcherName(agent)),
		publish: append(perProfile,
			"$KV.PROFILE_LEASES.>",
			"$JS.API.STREAM.INFO."+leases,
			"$JS.API.DIRECT.GET."+leases+".>",
			"$JS.API.CONSUMER.CREATE."+leases+".>",
			"$JS.API.CONSUMER.DELETE."+leases+".*",
			// The key listing and watch run an ordered push consumer with
			// flow control, which the client answers on $JS.FC. Listings
			// as large as a test can make didn't reach flow control (the
			// listing is headers-only), so this is held for the larger
			// ones that do, as the gateway's and the bridge's are.
			"$JS.FC."+leases+".>",
			"a2a.dispatch.*.*.*",
			"a2a.cap.verify."+a2aDispatcherUser,
			"_INBOX."+a2aDispatcherUser+".>",
		),
		subscribe: []string{
			"a2a.dispatch.*.*.*.accept",
			"a2a.dispatch.*.*.*.lease",
			"a2a.cap.reply." + a2aDispatcherUser + ".>",
			"_INBOX." + a2aDispatcherUser + ".>",
		},
	}
}

func buildA2ADispatcherServiceAccount(agent *agentv1alpha1.PlatformAgent) *corev1.ServiceAccount {
	return &corev1.ServiceAccount{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ServiceAccount"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aDispatcherName(agent),
			Namespace: agent.Namespace,
			Labels:    a2aLabels(agent, "dispatcher"),
		},
	}
}

// buildA2ADispatcherDeployment runs one dispatcher, replaced with Recreate:
// two at once would each hold their own share of every durable's in-flight
// count and enforce the ceiling against half the picture. The gap Recreate
// leaves is what restartGraceSeconds covers.
func buildA2ADispatcherDeployment(agent *agentv1alpha1.PlatformAgent) *appsv1.Deployment {
	name := a2aDispatcherName(agent)
	selector := a2aDispatcherPodSelector(agent)
	podLabels := a2aLabels(agent, "dispatcher")
	for k, v := range selector {
		podLabels[k] = v
	}
	return &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Labels: a2aLabels(agent, "dispatcher")},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(1)),
			Selector: &metav1.LabelSelector{MatchLabels: selector},
			Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					ServiceAccountName: name,
					// The API token is for its two watches: the dispatch
					// ConfigMap and the worker pods (its Roles). The bus
					// token is the projected one, as on every callout
					// principal.
					AutomountServiceAccountToken: ptr.To(true),
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot:   ptr.To(true),
						RunAsUser:      ptr.To(int64(1000)),
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Containers: []corev1.Container{{
						Name:       a2aDispatcherContainerName,
						Image:      a2aDispatcherImage(),
						WorkingDir: "/",
						Env: []corev1.EnvVar{
							{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
							a2aNATSCAFileEnvVar(),
							{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{
								FieldPath: "metadata.namespace",
							}}},
							{Name: "DISPATCH_CONFIGMAP", Value: a2aDispatchConfigName(agent)},
						},
						VolumeMounts: []corev1.VolumeMount{a2aBusTokenVolumeMount(), a2aNATSCABundleMount()},
						Ports:        []corev1.ContainerPort{{Name: "status", ContainerPort: a2aDispatcherStatusPort}},
						ReadinessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/readyz", Port: intstr.FromInt32(a2aDispatcherStatusPort),
							}},
							PeriodSeconds:    5,
							FailureThreshold: 3,
						},
						LivenessProbe: &corev1.Probe{
							ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
								Path: "/healthz", Port: intstr.FromInt32(a2aDispatcherStatusPort),
							}},
							PeriodSeconds:    10,
							FailureThreshold: 6,
						},
						Resources: a2aResources(a2aDispatcherCPURequest, a2aDispatcherMemoryRequest,
							a2aDispatcherCPULimit, a2aDispatcherMemoryLimit),
						SecurityContext: hardenedSecurityContext(),
					}},
					Volumes: []corev1.Volume{a2aBusTokenVolumeSource(), a2aNATSCABundleVolume(agent)},
				},
			},
		},
	}
}

// buildA2ADispatcherConfigRole lets the dispatcher read its one ConfigMap,
// by name, as the callout reads the identity map.
func buildA2ADispatcherConfigRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "dispatcher")},
		Rules: []rbacv1.PolicyRule{{
			APIGroups:     []string{""},
			Resources:     []string{"configmaps"},
			ResourceNames: []string{a2aDispatchConfigName(agent)},
			Verbs:         []string{"get", "list", "watch"},
		}},
	}
}

func buildA2ADispatcherConfigRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.RoleBinding {
	return &rbacv1.RoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "dispatcher")},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: a2aDispatcherName(agent)},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: a2aDispatcherName(agent), Namespace: agent.Namespace}},
	}
}

// buildA2ADispatcherPodsRole lets the dispatcher see which worker pods are
// ready, in the profile namespace. RBAC can't limit a list to a label
// selector, so it covers every pod there; the namespace holds only profile
// workers, and it is read-only, with no exec, logs or secrets. It lives in
// the profile namespace and so carries no owner reference (they can't cross
// namespaces); the agent's labels mark it as this agent's, as on the
// profile fence.
func buildA2ADispatcherPodsRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherName(agent), Namespace: profileNamespaceName(agent), Labels: a2aLabels(agent, "dispatcher")},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{""},
			Resources: []string{"pods"},
			Verbs:     []string{"get", "list", "watch"},
		}},
	}
}

func buildA2ADispatcherPodsRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.RoleBinding {
	return &rbacv1.RoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherName(agent), Namespace: profileNamespaceName(agent), Labels: a2aLabels(agent, "dispatcher")},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: a2aDispatcherName(agent)},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: a2aDispatcherName(agent), Namespace: agent.Namespace}},
	}
}

// dispatchReserves are the child and interactive reserves the dispatcher
// enforces. They are disjoint shares of the ceiling, and together leave at
// least one slot reserved for neither, so top-level unattended work can always
// run. The child reserve comes first, because without it waiting parents can
// deadlock the ceiling; the interactive reserve, max(1, ceil(ceiling/3)), gets
// what is left. A ceiling of 1 reserves nothing: every task runs alone, and a
// parent there can never start a child.
func dispatchReserves(agent *agentv1alpha1.PlatformAgent, ceiling int) (child, interactive int) {
	if ceiling < 2 {
		return 0, 0
	}
	// From the ceiling given, not the agent's: the shortfall advice asks
	// what other ceilings would do.
	wantChild := max(1, (ceiling+2)/3)
	if t := profileTuning(agent); t != nil && t.ChildReserve != nil {
		wantChild = *t.ChildReserve
	}
	child = min(wantChild, ceiling-1)
	interactive = min(max(1, (ceiling+2)/3), ceiling-1-child)
	return child, interactive
}

// reasonProfileDeadlineCapped is the Warning Event on the PlatformAgent when a
// profile's activeDeadlineSeconds is past the install's task deadline cap.
const reasonProfileDeadlineCapped = "ProfileDeadlineCapped"

// cappedTaskDeadline is a profile's task deadline in seconds, clamped to the
// install's cap (resolveProfileMaxTaskDeadline), and whether it was clamped.
// The cap isn't a CRD maximum on activeDeadlineSeconds: on an existing field
// that would wedge every profile already above it.
func cappedTaskDeadline(agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile) (int, bool) {
	limit := int(resolveProfileMaxTaskDeadline(agent) / time.Second)
	if d := int(p.Spec.Lifecycle.ActiveDeadlineSeconds); d <= limit {
		return d, false
	}
	return limit, true
}

// reasonProfileReservesShort is the Warning Event on the PlatformAgent when
// its profile ceiling is too small to honour every reserve #3660 promises.
const reasonProfileReservesShort = "ProfileReservesShort"

// dispatchReservesShortfall says which reserve the ceiling can't honour in
// full, or "". Each reserve's promise is max(1, ceil(ceiling/3)) unless the
// CR sets childReserve; dispatchReserves trims them to leave a slot for
// top-level unattended work, child first.
func dispatchReservesShortfall(agent *agentv1alpha1.PlatformAgent, ceiling, child, interactive int) string {
	promised := max(1, (ceiling+2)/3)
	wantChild := promised
	if t := profileTuning(agent); t != nil && t.ChildReserve != nil {
		wantChild = *t.ChildReserve
	}
	var short []string
	if child < wantChild {
		short = append(short, fmt.Sprintf("the child reserve is %d of %d", child, wantChild))
	}
	if interactive < promised {
		short = append(short, fmt.Sprintf("the interactive reserve is %d of %d", interactive, promised))
	}
	if len(short) == 0 {
		return ""
	}
	advice := "No ceiling up to 1000 honours them; lower spec.harness.tuning.profiles.childReserve."
	if c := smallestHonouringCeiling(agent, ceiling); c > 0 {
		advice = fmt.Sprintf("Raise the ceiling (or maxInProgress, which it defaults to) to at least %d.", c)
	}
	return fmt.Sprintf("spec.harness.tuning.profiles.ceiling %d is too small to honour every reserve and leave a slot for top-level unattended work: %s. %s",
		ceiling, strings.Join(short, ", "), advice)
}

// smallestHonouringCeiling is the smallest ceiling above this one at which
// dispatchReserves honours both reserves in full, or 0 if none up to the
// CRD's maximum does. Not monotonic in general (the default reserves are
// honoured at 3 but not at 4), so it searches.
func smallestHonouringCeiling(agent *agentv1alpha1.PlatformAgent, ceiling int) int {
	for c := ceiling + 1; c <= 1000; c++ {
		child, interactive := dispatchReserves(agent, c)
		if dispatchReservesShortfall0(agent, c, child, interactive) {
			return c
		}
	}
	return 0
}

// dispatchReservesShortfall0 reports whether both reserves are honoured in
// full at ceiling, without building the message.
func dispatchReservesShortfall0(agent *agentv1alpha1.PlatformAgent, ceiling, child, interactive int) bool {
	promised := max(1, (ceiling+2)/3)
	wantChild := promised
	if t := profileTuning(agent); t != nil && t.ChildReserve != nil {
		wantChild = *t.ChildReserve
	}
	return child >= wantChild && interactive >= promised
}

// dispatcherGrantNames are the profiles the dispatch config lists and every
// durable under them, in its order: the names the dispatcher's grants
// enumerate.
func dispatcherGrantNames(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) (names, durables []string) {
	cfg, err := desiredDispatchConfig(agent, profiles)
	if err != nil {
		return nil, nil
	}
	for _, p := range cfg.Profiles {
		names = append(names, p.Name)
		for _, d := range p.Durables {
			durables = append(durables, d.Name)
		}
	}
	return names, durables
}

// a2aDispatchConfig is lib.DispatchConfig as this module renders it; the
// dispatch-config test decodes the render into lib's type.
type a2aDispatchConfig struct {
	Version              string               `json:"version"`
	Namespace            string               `json:"namespace"`
	ProfileNamespace     string               `json:"profileNamespace"`
	Ceiling              int                  `json:"ceiling"`
	InteractiveReserve   int                  `json:"interactiveReserve"`
	ChildReserve         int                  `json:"childReserve"`
	LeaseSeconds         int                  `json:"leaseSeconds"`
	RenewSeconds         int                  `json:"renewSeconds"`
	RestartGraceSeconds  int                  `json:"restartGraceSeconds"`
	AcceptTimeoutSeconds int                  `json:"acceptTimeoutSeconds"`
	Profiles             []a2aDispatchProfile `json:"profiles"`
}

type a2aDispatchProfile struct {
	Name                  string               `json:"name"`
	Tiers                 []string             `json:"tiers"`
	Concurrency           int                  `json:"concurrency"`
	QueueTimeoutSeconds   int                  `json:"queueTimeoutSeconds"`
	ActiveDeadlineSeconds int                  `json:"activeDeadlineSeconds"`
	Parameters            []a2aCardParameter   `json:"parameters,omitempty"`
	Durables              []a2aDispatchDurable `json:"durables"`
}

type a2aDispatchDurable struct {
	Tier string `json:"tier"`
	Kind string `json:"kind"`
	Name string `json:"name"`
}

// The submission kinds, lib.SubmitKindTop and lib.SubmitKindChild.
var a2aSubmitKinds = []string{"top", "child"}

// profileDurableName is lib.ProfileDurableName.
func profileDurableName(profile, tier, kind string) string {
	return fmt.Sprintf("profile-%s-%s-%s", profile, tier, kind)
}

// desiredDispatchConfig is what the dispatcher reads for this agent: every
// value resolved, and the profiles that have a rendered identity, each with
// the durables the operator creates for it, one per (tier, kind).
func desiredDispatchConfig(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) (a2aDispatchConfig, error) {
	ceiling := resolveProfileCeiling(agent)
	child, interactive := dispatchReserves(agent, ceiling)
	cfg := a2aDispatchConfig{
		Namespace:            agent.Namespace,
		ProfileNamespace:     profileNamespaceName(agent),
		Ceiling:              ceiling,
		InteractiveReserve:   interactive,
		ChildReserve:         child,
		LeaseSeconds:         resolveProfileLeaseSeconds(agent),
		RenewSeconds:         resolveProfileRenewSeconds(agent),
		RestartGraceSeconds:  resolveProfileRestartGraceSeconds(agent),
		AcceptTimeoutSeconds: resolveProfileAcceptTimeoutSeconds(agent),
		Profiles:             []a2aDispatchProfile{},
	}
	resolved := resolveAgentProfileIdentities(agent, profiles)
	for i := range profiles {
		p := &profiles[i]
		if resolved[p.Name].refused != nil {
			continue
		}
		dp := a2aDispatchProfile{
			Name:                  p.Name,
			Tiers:                 p.AllowedTierList(),
			Concurrency:           int(p.Spec.Concurrency),
			QueueTimeoutSeconds:   int(p.QueueTimeout()),
			ActiveDeadlineSeconds: func() int { d, _ := cappedTaskDeadline(agent, p); return d }(),
			Durables:              []a2aDispatchDurable{},
		}
		for _, param := range p.Spec.Parameters {
			dp.Parameters = append(dp.Parameters, a2aCardParameter{Name: param.Name, Required: param.Required})
		}
		for _, tier := range dp.Tiers {
			for _, kind := range a2aSubmitKinds {
				dp.Durables = append(dp.Durables, a2aDispatchDurable{Tier: tier, Kind: kind, Name: profileDurableName(p.Name, tier, kind)})
			}
		}
		cfg.Profiles = append(cfg.Profiles, dp)
	}
	slices.SortFunc(cfg.Profiles, func(a, b a2aDispatchProfile) int { return strings.Compare(a.Name, b.Name) })
	body, err := json.Marshal(cfg)
	if err != nil {
		return cfg, err
	}
	sum := sha256.Sum256(body)
	cfg.Version = hex.EncodeToString(sum[:8])
	return cfg, nil
}

func buildA2ADispatchConfigMap(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) (*corev1.ConfigMap, error) {
	cfg, err := desiredDispatchConfig(agent, profiles)
	if err != nil {
		return nil, err
	}
	body, err := json.MarshalIndent(cfg, "", "  ")
	if err != nil {
		return nil, err
	}
	return &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatchConfigName(agent), Namespace: agent.Namespace, Labels: a2aLabels(agent, "dispatcher")},
		Data:       map[string]string{a2aDispatchConfigKey: string(body)},
	}, nil
}

// reconcileA2ADispatcher renders the dispatcher when this operator has an
// image for it, and removes what it rendered when it doesn't. The objects in
// the agent's namespace are owned by the agent; the pod-read Role and its
// binding in the profile namespace are marked by the agent's labels and
// follow the profile-namespace rule: a failure there is returned for the
// caller to log, and the rest still lands.
func (r *PlatformAgentReconciler) reconcileA2ADispatcher(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, profileNamespaceReady bool) (profileErr error, err error) {
	if !a2aDispatcherEnabled() {
		return r.removeA2ADispatcher(ctx, agent)
	}
	cm, err := buildA2ADispatchConfigMap(agent, profiles)
	if err != nil {
		return nil, fmt.Errorf("failed to render the dispatch config: %w", err)
	}
	// A ceiling too small for every reserve is said out loud, so nobody
	// finds out by interactive users waiting.
	ceiling := resolveProfileCeiling(agent)
	child, interactive := dispatchReserves(agent, ceiling)
	if msg := dispatchReservesShortfall(agent, ceiling, child, interactive); msg != "" {
		r.recordEvent(agent, corev1.EventTypeWarning, reasonProfileReservesShort, msg)
	}
	resolved := resolveAgentProfileIdentities(agent, profiles)
	for i := range profiles {
		// Only profiles the dispatch config admits: a refused one has no
		// tasks to cap, and its condition says why it's refused.
		if resolved[profiles[i].Name].refused != nil {
			continue
		}
		if d, capped := cappedTaskDeadline(agent, &profiles[i]); capped {
			r.recordEvent(agent, corev1.EventTypeWarning, reasonProfileDeadlineCapped, fmt.Sprintf(
				"AgentProfile %s: spec.lifecycle.activeDeadlineSeconds %d is clamped to %ds by the install's task deadline cap (spec.harness.tuning.profiles.maxTaskDeadlineSeconds), so its tasks end at %ds",
				profiles[i].Name, profiles[i].Spec.Lifecycle.ActiveDeadlineSeconds, d, d))
		}
	}
	// The identity before the Deployment that mounts a token for it, and the
	// config and its Role before the pod that watches them.
	owned := []client.Object{
		buildA2ADispatcherServiceAccount(agent),
		cm,
		buildA2ADispatcherConfigRole(agent),
		buildA2ADispatcherConfigRoleBinding(agent),
		buildA2ADispatcherDeployment(agent),
	}
	for _, obj := range owned {
		if err := ctrl.SetControllerReference(agent, obj, r.Scheme); err != nil {
			return nil, err
		}
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return nil, fmt.Errorf("failed to apply the A2A dispatcher's %T: %w", obj, err)
		}
	}
	if !profileNamespaceReady {
		return nil, nil
	}
	for _, obj := range []client.Object{buildA2ADispatcherPodsRole(agent), buildA2ADispatcherPodsRoleBinding(agent)} {
		if err := r.applyManaged(ctx, agent, obj); err != nil {
			return fmt.Errorf("the dispatcher's pod-read %T: %w", obj, err), nil
		}
	}
	return nil, nil
}

// removeA2ADispatcher deletes what reconcileA2ADispatcher rendered: the
// owned objects only if this agent controls them, and the profile namespace's
// Role and binding only if they carry this agent's labels.
func (r *PlatformAgentReconciler) removeA2ADispatcher(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (profileErr error, err error) {
	name := a2aDispatcherName(agent)
	for _, obj := range []client.Object{
		&appsv1.Deployment{}, &rbacv1.RoleBinding{}, &rbacv1.Role{}, &corev1.ServiceAccount{},
	} {
		if err := r.deleteIfControlled(ctx, agent, obj, types.NamespacedName{Namespace: agent.Namespace, Name: name}); err != nil {
			return nil, err
		}
	}
	if err := r.deleteIfControlled(ctx, agent, &corev1.ConfigMap{}, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}); err != nil {
		return nil, err
	}
	return r.deleteDispatcherPodsRBAC(ctx, agent), nil
}

// deleteDispatcherPodsRBAC removes the dispatcher's pod-read Role and binding
// from the profile namespace, if they carry this agent's labels. Read
// uncached: Role is a kind this controller doesn't watch.
func (r *PlatformAgentReconciler) deleteDispatcherPodsRBAC(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	key := types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aDispatcherName(agent)}
	for _, obj := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}} {
		if err := r.a2aReader().Get(ctx, key, obj); err != nil {
			if apierrors.IsNotFound(err) {
				continue
			}
			return fmt.Errorf("the dispatcher's pod-read %T: %w", obj, err)
		}
		if !agentLabelsMark(obj, agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, obj)); err != nil {
			return fmt.Errorf("deleting the dispatcher's pod-read %T: %w", obj, err)
		}
	}
	return nil
}

// deleteIfControlled deletes the named object if this agent controls it.
func (r *PlatformAgentReconciler) deleteIfControlled(ctx context.Context, agent *agentv1alpha1.PlatformAgent, obj client.Object, key types.NamespacedName) error {
	// Uncached for the kinds this controller doesn't watch (Role,
	// RoleBinding), so the read doesn't start a cluster-wide informer.
	reader := client.Reader(r.Client)
	switch obj.(type) {
	case *rbacv1.Role, *rbacv1.RoleBinding:
		reader = r.a2aReader()
	}
	if err := reader.Get(ctx, key, obj); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !metav1.IsControlledBy(obj, agent) {
		return nil
	}
	return client.IgnoreNotFound(r.Delete(ctx, obj))
}

// agentLabelsMark reports whether obj carries this agent's a2a labels, the
// ownership test for objects in the profile namespace.
func agentLabelsMark(obj client.Object, agent *agentv1alpha1.PlatformAgent) bool {
	want := a2aLabels(agent, "dispatcher")
	got := obj.GetLabels()
	for k, v := range want {
		if got[k] != v {
			return false
		}
	}
	return true
}
