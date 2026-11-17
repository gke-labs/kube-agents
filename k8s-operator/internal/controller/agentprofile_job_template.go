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
	goerrors "errors"
	"fmt"
	"maps"
	"os"
	"slices"
	"sort"
	"strings"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Job mode for the unattended tier (#3655 PR 6). The dispatcher runs each
// unattended task as a Job in the profile namespace, from a pod template the
// operator renders per profile. The same template goes two places: into the
// dispatch config (a2aDispatchProfile.JobTemplate), which the dispatcher
// stamps a Job from, and, defaulted by a server-side dry run, into a
// DispatchJobTemplate in the profile namespace, which the admission policy
// (config/admission/a2a-dispatcher-job-policy.yaml) holds every dispatcher
// Job to. An index DispatchJobTemplate lists the ServiceAccounts those
// templates run as, so a Job running as any other is denied. Only the
// operator writes DispatchJobTemplates.
const (
	// dispatchJobTemplatePrefix names a profile's DispatchJobTemplate for its
	// unattended-mode tier: kube-agents-dispatch-job-profile-<profile>.
	dispatchJobTemplatePrefix = "kube-agents-dispatch-job-profile-"
	// dispatchJobTierTemplatePrefix names a profile's DispatchJobTemplate for
	// any other tier, where permit-carrying tasks run in their own one-pod
	// Job (#5646): kube-agents-dispatch-job-tier-<profile>.<tier>. Its own
	// prefix, so no tier's param is another profile's unattended-mode one,
	// and a dot between the two, which neither can hold (a profile name is a
	// dot-free label by the CRD's rule and resolveOneAgentProfile's check; a
	// tier is a subject token), so no two pairs share a name.
	// lib.DispatchJobTierTemplateName must stay the same.
	dispatchJobTierTemplatePrefix = "kube-agents-dispatch-job-tier-"
	// dispatchJobParamProfileAnnotation and dispatchJobParamTierAnnotation
	// say which (profile, tier) a param holds the template of, so the
	// operator never parses it out of the name. Annotations, not labels: the
	// policy requires every param label on every Job.
	dispatchJobParamProfileAnnotation = "kubeagents.x-k8s.io/dispatch-profile"
	dispatchJobParamTierAnnotation    = "kubeagents.x-k8s.io/dispatch-tier"
	// dispatchJobParamDefaultedByAnnotation is the API server version whose
	// dry run defaulted the param's template: a param whose rendering, labels
	// and defaulting server are all current is left as it is, without a
	// fresh dry run or apply.
	dispatchJobParamDefaultedByAnnotation = "kubeagents.x-k8s.io/dispatch-defaulted-by"
	// dispatchJobParamDigestAnnotation is a digest of the template the param
	// was written with, so a param whose template was edited since is written
	// again rather than left as current.
	dispatchJobParamDigestAnnotation = "kubeagents.x-k8s.io/dispatch-param-digest"
	// dispatchJobIndexName is the index DispatchJobTemplate, which the policy tells
	// apart by this name. The templates' prefix keeps a profile named
	// "index" from taking it.
	dispatchJobIndexName = "kube-agents-dispatch-job-index"
	// dispatchJobTemplateLabel and the managed-by label (commonLabels'
	// labelManagedBy: fieldOwner) are what the policy's binding selects
	// params by.
	dispatchJobTemplateLabel = "kube-agents.dev/dispatch-job-template"
	// dispatchJobLabel marks a Job the dispatcher created (lib.DispatchJobLabel),
	// so a teardown can find them.
	dispatchJobLabel = "kubeagents.x-k8s.io/dispatch-job"
)

// profileWorkerPodSpecForJob is the worker pod a profile's Job runs: the warm
// pool's pod (buildProfileWorkerPodSpec) in one-shot mode, which serves one
// task and exits (PROFILE_WORKER_ONE_SHOT, restartPolicy Never), under the
// (profile, tier)'s ServiceAccount.
func profileWorkerPodSpecForJob(agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile, tier string, imageVolumes bool) corev1.PodSpec {
	return buildProfileWorkerPodSpec(agent, p, tier, agentProfileServiceAccountName(p, tier), profileWorkerOneShot, imageVolumes)
}

// buildProfileJobTemplate is the pod template a profile's unattended Jobs
// run: buildProfileJobTemplateAt its one unattended-mode tier
// (unattendedTierOf): unattended itself, or a Tier resource whose mode is
// unattended (#3659).
func buildProfileJobTemplate(agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile, imageVolumes bool) *corev1.PodTemplateSpec {
	tier := unattendedTierOf(p)
	if tier == "" {
		tier = agentv1alpha1.AgentProfileTierUnattended
	}
	return buildProfileJobTemplateAt(agent, p, tier, imageVolumes)
}

// buildProfileJobTemplateAt is the pod template a profile's Jobs at tier
// run: the one-shot worker pod under the (profile, tier)'s ServiceAccount,
// whose restartPolicy is Never (a Job's pod must not restart, and a
// PodTemplate defaults it to Always, which would break the policy's exact
// comparison), with the worker labels for that tier. At a tier that isn't
// unattended only permit-carrying tasks run on it (#5646).
func buildProfileJobTemplateAt(agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile, tier string, imageVolumes bool) *corev1.PodTemplateSpec {
	spec := profileWorkerPodSpecForJob(agent, p, tier, imageVolumes)
	settleForPlatformMutation(&spec, p)
	return &corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{
			a2aComponentLabel:     agentProfileWorkerComponent,
			agentProfileNameLabel: p.Name,
			agentProfileTierLabel: tier,
		}},
		Spec: spec,
	}
}

// Autopilot's defaults for a container's unset resources, which the Job
// template sets itself to the same values.
var jobDefaultResources = corev1.ResourceList{
	corev1.ResourceCPU:              resource.MustParse("500m"),
	corev1.ResourceMemory:           resource.MustParse("2Gi"),
	corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
}

// jobArchToleration is the toleration GKE Autopilot adds to a pod that names
// no architecture (the images are linux/amd64). Harmless elsewhere: it
// tolerates only the architecture taint.
var jobArchToleration = corev1.Toleration{Key: "kubernetes.io/arch", Operator: corev1.TolerationOpEqual, Value: "amd64", Effect: corev1.TaintEffectNoSchedule}

// settleForPlatformMutation sets in the Job template what a managed
// platform's mutating admission would otherwise fill in on the Job, after the
// operator rendered the template and before the Job admission policy compares
// the two, so the comparison holds. Found live on GKE Autopilot, whose
// webhook defaults a container's unset cpu, memory and ephemeral-storage and
// adds jobArchToleration: every container gets a request and a limit for all
// three, and the toleration is added. The worker builder sets the worker's
// three itself (cpu and memory from the profile, ephemeral-storage from its
// emptyDirs, buildProfileWorkerPodSpec), and those stand; what a container
// leaves unset is filled (the worker's from the profile as a fallback,
// anything else from jobDefaultResources, a limit from its request). Autopilot also raises a request that breaks its cpu-to-memory ratio;
// that it can't be told apart from a forged Job, so such a profile's Jobs are
// refused there (fail closed).
func settleForPlatformMutation(spec *corev1.PodSpec, p *agentv1alpha1.AgentProfile) {
	settle := func(c *corev1.Container, requests, limits corev1.ResourceList) {
		if c.Resources.Requests == nil {
			c.Resources.Requests = corev1.ResourceList{}
		}
		if c.Resources.Limits == nil {
			c.Resources.Limits = corev1.ResourceList{}
		}
		for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory, corev1.ResourceEphemeralStorage} {
			if _, ok := c.Resources.Requests[name]; !ok {
				if q, ok := requests[name]; ok {
					c.Resources.Requests[name] = q
				} else {
					c.Resources.Requests[name] = jobDefaultResources[name]
				}
			}
			if _, ok := c.Resources.Limits[name]; !ok {
				if q, ok := limits[name]; ok {
					c.Resources.Limits[name] = q
				} else {
					c.Resources.Limits[name] = c.Resources.Requests[name]
				}
			}
		}
	}
	profile := p.Spec.Resources
	// The fallback for a worker with no ephemeral-storage: the pod's ceiling
	// is its containers' limits, and its disk-backed emptyDirs count against
	// it, so the worker's share covers their size limits, on top of the
	// default for its own writes.
	ephemeral := jobDefaultResources[corev1.ResourceEphemeralStorage].DeepCopy()
	for _, v := range spec.Volumes {
		if v.EmptyDir != nil && v.EmptyDir.Medium != corev1.StorageMediumMemory && v.EmptyDir.SizeLimit != nil {
			ephemeral.Add(*v.EmptyDir.SizeLimit)
		}
	}
	for i := range spec.Containers {
		var requests, limits corev1.ResourceList
		if i == 0 { // the worker
			requests = corev1.ResourceList{corev1.ResourceCPU: profile.Requests.CPU, corev1.ResourceMemory: profile.Requests.Memory, corev1.ResourceEphemeralStorage: ephemeral}
			limits = corev1.ResourceList{corev1.ResourceCPU: profile.Limits.CPU, corev1.ResourceMemory: profile.Limits.Memory, corev1.ResourceEphemeralStorage: ephemeral}
		}
		settle(&spec.Containers[i], requests, limits)
	}
	for i := range spec.InitContainers {
		settle(&spec.InitContainers[i], nil, nil)
	}
	if !slices.ContainsFunc(spec.Tolerations, func(t corev1.Toleration) bool { return t.Key == jobArchToleration.Key }) {
		spec.Tolerations = append(spec.Tolerations, jobArchToleration)
	}
}

// GKE Autopilot's bounds on a general-purpose container's memory per vCPU
// requested, in GiB, outside which it raises the smaller request.
const (
	autopilotMinGiBPerCPU = 1.0
	autopilotMaxGiBPerCPU = 6.5
)

const (
	reasonAgentProfileJobResourcesFit       = "Fits"
	reasonAgentProfileOutsideAutopilotRatio = "OutsideAutopilotRatio"
	// reasonProfileJobResources is the Warning Event on the PlatformAgent
	// when a profile it renders a Job template for is outside the ratio.
	reasonProfileJobResources = "ProfileJobResourcesOutsideAutopilotRatio"
)

// autopilotRatioProblem says why a profile would have its Jobs refused on
// GKE Autopilot, or "": its worker's requests break Autopilot's
// memory-per-vCPU bounds, so Autopilot raises one of them on the Job and the
// Job no longer equals its template (found live). Every profile has Jobs
// under Job mode: its unattended tasks, and at any tier its permit-carrying
// ones (#5646). Checked whatever the cluster: the operator can't tell an
// Autopilot cluster apart, and the condition says where it matters.
func autopilotRatioProblem(p *agentv1alpha1.AgentProfile) string {
	cpu := p.Spec.Resources.Requests.CPU.AsApproximateFloat64()
	gib := p.Spec.Resources.Requests.Memory.AsApproximateFloat64() / (1 << 30)
	if cpu <= 0 {
		return ""
	}
	if ratio := gib / cpu; ratio < autopilotMinGiBPerCPU || ratio > autopilotMaxGiBPerCPU {
		return fmt.Sprintf("resources.requests (cpu %s, memory %s) are %.2f GiB per vCPU, outside GKE Autopilot's %.1f to %.1f: on Autopilot the profile's Jobs are refused (Autopilot raises a request, and the Job no longer equals its template)",
			p.Spec.Resources.Requests.CPU.String(), p.Spec.Resources.Requests.Memory.String(), ratio, autopilotMinGiBPerCPU, autopilotMaxGiBPerCPU)
	}
	return ""
}

// a2aDispatcherJobPolicyEnvVar is the chart's signal that it rendered the
// dispatcher Job admission policy (config/admission/a2a-dispatcher-job-policy.yaml),
// set from the same helper as the policy. Job mode runs only where it is set:
// without the policy the dispatcher's Job create would be as wide as its Role,
// so the operator renders no Job templates and no Jobs Role, and unattended
// tasks have no worker (the warm pools serve the read tier only) and are
// refused no-worker at admission. It says the policy was rendered, not that it still
// holds.
const a2aDispatcherJobPolicyEnvVar = "A2A_DISPATCHER_JOB_POLICY"

func dispatcherJobPolicyRendered() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(a2aDispatcherJobPolicyEnvVar)), "true")
}

// profileJobTemplates are one profile's Job templates, by the name of the
// param (DispatchJobTemplate) each goes in: one per live tier with an
// identity (#5646). The tier is the template's agentProfileTierLabel.
type profileJobTemplates map[string]*corev1.PodTemplateSpec

// dispatchJobParamName is the param that holds p's template at tier: the
// profile's own name for its unattended-mode tier, which a dispatcher from
// before per-tier templates reads, and the tier's for any other.
func dispatchJobParamName(p *agentv1alpha1.AgentProfile, tier string) string {
	if tier == unattendedTierOf(p) {
		return dispatchJobTemplatePrefix + p.Name
	}
	return dispatchJobTierTemplatePrefix + p.Name + "." + tier
}

// dispatchJobTemplates is each profile's Job templates, by profile name: one
// for every live tier the profile has an identity at, for every profile the
// dispatch config admits. The unattended-mode tier's runs its unattended
// tasks; every tier's, that one's included, runs the permit-carrying tasks
// admitted at it, each in its own one-pod Job (#5646). A draining tier has
// none, so no new Job starts there (#5638). A profile whose worker would
// never schedule (profileWorkerToolsProblem) gets none, as it gets no pool;
// dispatchJobToolsProblems says why.
func dispatchJobTemplates(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, imageVolumes bool) map[string]profileJobTemplates {
	if !dispatcherJobPolicyRendered() {
		return nil
	}
	out := map[string]profileJobTemplates{}
	resolved := resolveAgentProfileIdentities(agent, profiles)
	for i := range profiles {
		p := &profiles[i]
		res := resolved[p.Name]
		if res.refused != nil || profileWorkerToolsProblem(p, imageVolumes) != "" {
			continue
		}
		set := profileJobTemplates{}
		for _, tier := range dispatchJobTiersOf(p, res) {
			set[dispatchJobParamName(p, tier)] = buildProfileJobTemplateAt(agent, p, tier, imageVolumes)
		}
		if len(set) > 0 {
			out[p.Name] = set
		}
	}
	return out
}

// dispatchJobTiersOf is the tiers p gets a Job template at: its live tiers
// that have an identity, in allowedTiers order.
func dispatchJobTiersOf(p *agentv1alpha1.AgentProfile, res agentProfileResolution) []string {
	has := map[string]bool{}
	for _, id := range res.identities {
		has[id.tier] = true
	}
	var out []string
	for _, tier := range liveTiersOf(p) {
		if has[tier] {
			out = append(out, tier)
		}
	}
	return out
}

// dispatchJobToolsProblems names each profile dispatchJobTemplates leaves
// without a Job template because its worker would never schedule, as the
// pool path names one it leaves without a pool.
func dispatchJobToolsProblems(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, imageVolumes bool) error {
	if !dispatcherJobPolicyRendered() {
		return nil
	}
	var errs []error
	resolved := resolveAgentProfileIdentities(agent, profiles)
	for i := range profiles {
		p := &profiles[i]
		if resolved[p.Name].refused != nil {
			continue
		}
		if problem := profileWorkerToolsProblem(p, imageVolumes); problem != "" {
			errs = append(errs, fmt.Errorf("the Job template of profile %s: %s", p.Name, problem))
		}
	}
	return goerrors.Join(errs...)
}

// dispatchJobLabels are the labels on every Job the dispatcher creates.
func dispatchJobLabels(agent *agentv1alpha1.PlatformAgent) map[string]string {
	labels := a2aLabels(agent, "dispatcher")
	labels[dispatchJobLabel] = "true"
	return labels
}

// dispatchJobParamLabels are a policy param's labels: the agent's, which
// mark it as this agent's, and the two the binding selects by.
func dispatchJobParamLabels(agent *agentv1alpha1.PlatformAgent) map[string]string {
	labels := a2aLabels(agent, "dispatcher")
	labels[dispatchJobTemplateLabel] = "true"
	return labels
}

// buildDispatchJobTemplate is the param name: one (profile, tier)'s Job
// template, defaulted as the API server defaults a pod template
// (defaultedJobTemplate), stamped with hash, its profile's
// dispatchJobTemplatesHash.
func buildDispatchJobTemplate(agent *agentv1alpha1.PlatformAgent, name, hash string, defaulted *corev1.PodTemplateSpec) (*agentv1alpha1.DispatchJobTemplate, error) {
	return buildDispatchJobTemplateBy(agent, name, hash, "", defaulted)
}

// buildDispatchJobTemplateBy is buildDispatchJobTemplate stamped with the
// version of the API server whose dry run defaulted it ("" for unknown).
func buildDispatchJobTemplateBy(agent *agentv1alpha1.PlatformAgent, name, hash, server string, defaulted *corev1.PodTemplateSpec) (*agentv1alpha1.DispatchJobTemplate, error) {
	raw, err := json.Marshal(defaulted)
	if err != nil {
		return nil, err
	}
	annotations := map[string]string{
		dispatchJobTemplateHashAnnotation: hash,
		dispatchJobParamProfileAnnotation: defaulted.Labels[agentProfileNameLabel],
		dispatchJobParamTierAnnotation:    defaulted.Labels[agentProfileTierLabel],
	}
	if server != "" {
		annotations[dispatchJobParamDefaultedByAnnotation] = server
		annotations[dispatchJobParamDigestAnnotation] = dispatchJobParamDigest(raw)
	}
	return &agentv1alpha1.DispatchJobTemplate{
		TypeMeta: metav1.TypeMeta{APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "DispatchJobTemplate"},
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: profileNamespaceName(agent), Labels: dispatchJobParamLabels(agent),
			Annotations: annotations,
		},
		Spec: agentv1alpha1.DispatchJobTemplateSpec{Template: &runtime.RawExtension{Raw: raw}},
	}, nil
}

// dispatchJobParamCurrent is whether pt, the live param, already holds
// want's rendering as the operator writes it: this agent's labels and only
// them, the profile's hash, its (profile, tier), and a template defaulted by
// server, the API server serving now. Then its dry run and apply would
// change nothing, and are skipped. An unknown server never matches, so a
// param is re-defaulted after an API server upgrade.
func dispatchJobParamCurrent(pt *agentv1alpha1.DispatchJobTemplate, agent *agentv1alpha1.PlatformAgent, want *corev1.PodTemplateSpec, hash, server string) bool {
	a := pt.Annotations
	if pt.Spec.Template == nil || a[dispatchJobParamDigestAnnotation] != dispatchJobParamDigest(pt.Spec.Template.Raw) {
		return false
	}
	// An unknown server ("") never matches: a param written with one carries
	// its version, and one written without carries no digest.
	return agentLabelsMark(pt, agent) && maps.Equal(pt.Labels, dispatchJobParamLabels(agent)) &&
		a[dispatchJobTemplateHashAnnotation] == hash && a[dispatchJobParamDefaultedByAnnotation] == server &&
		a[dispatchJobParamProfileAnnotation] == want.Labels[agentProfileNameLabel] &&
		a[dispatchJobParamTierAnnotation] == want.Labels[agentProfileTierLabel]
}

// dispatchJobTemplateResource is the DispatchJobTemplate CRD's plural, as an
// RBAC rule names it.
const dispatchJobTemplateResource = "dispatchjobtemplates"

// dispatchJobTemplateHashAnnotation is lib.DispatchJobTemplateHashAnnotation:
// which rendering of a profile's template its param holds, which the dispatch
// config's reference names (the operator can't import lib).
const dispatchJobTemplateHashAnnotation = "kubeagents.x-k8s.io/dispatch-template-hash"

// dispatchJobTemplateHashLength is how many hex digits of the rendering's
// sha256 the hash keeps: enough that two renderings don't meet, and short
// enough for an annotation a person reads.
const dispatchJobTemplateHashLength = 16

// dispatchJobTemplatesHash is one hash over all of a profile's renderings
// (not defaulted), each by its param's name, stamped on every one of its
// params and named by its config entry. One per profile, however many tiers
// (#5646): a change to any tier's template moves them all, and the config
// entry stays as wide as with one template.
func dispatchJobTemplatesHash(templates profileJobTemplates) string {
	names := make([]string, 0, len(templates))
	for name := range templates {
		names = append(names, name)
	}
	sort.Strings(names)
	type rendering struct {
		Param    string                  `json:"param"`
		Template *corev1.PodTemplateSpec `json:"template"`
	}
	all := make([]rendering, 0, len(names))
	for _, name := range names {
		all = append(all, rendering{name, templates[name]})
	}
	raw, err := json.Marshal(all)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])[:dispatchJobTemplateHashLength]
}

// dispatchJobTemplateRef is the dispatch config's reference to a profile's
// params, or nil when it has none: their one hash, per tier.
func dispatchJobTemplateRef(profile string, templates profileJobTemplates) *a2aDispatchJobTemplateRef {
	if len(templates) == 0 {
		return nil
	}
	return &a2aDispatchJobTemplateRef{Name: dispatchJobTemplatePrefix + profile, Hash: dispatchJobTemplatesHash(templates), PerTier: true}
}

// buildDispatchJobIndex is the index param: the ServiceAccounts the
// templates run as, every tier's.
func buildDispatchJobIndex(agent *agentv1alpha1.PlatformAgent, templates map[string]profileJobTemplates) *agentv1alpha1.DispatchJobTemplate {
	var accounts []string
	for _, set := range templates {
		for _, t := range set {
			accounts = append(accounts, t.Spec.ServiceAccountName)
		}
	}
	return buildDispatchJobIndexFor(agent, accounts)
}

// isDispatchJobTemplateParam is whether name is a template param's (not the
// index): a profile's own, or a tier's (#5646).
func isDispatchJobTemplateParam(name string) bool {
	return name != dispatchJobIndexName &&
		(strings.HasPrefix(name, dispatchJobTemplatePrefix) || strings.HasPrefix(name, dispatchJobTierTemplatePrefix))
}

// buildDispatchJobIndexFor is the index listing accounts.
func buildDispatchJobIndexFor(agent *agentv1alpha1.PlatformAgent, accounts []string) *agentv1alpha1.DispatchJobTemplate {
	// Never nil: an index with no accounts is sent as an empty list, not as
	// an empty spec (see DispatchJobTemplateSpec.ServiceAccounts).
	accounts = append([]string{}, accounts...)
	sort.Strings(accounts)
	accounts = slices.Compact(accounts)
	return &agentv1alpha1.DispatchJobTemplate{
		TypeMeta:   metav1.TypeMeta{APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "DispatchJobTemplate"},
		ObjectMeta: metav1.ObjectMeta{Name: dispatchJobIndexName, Namespace: profileNamespaceName(agent), Labels: dispatchJobParamLabels(agent)},
		Spec:       agentv1alpha1.DispatchJobTemplateSpec{ServiceAccounts: &accounts},
	}
}

// dispatchJobParamTemplate is the pod template a param holds, or false (the
// index, or a template that won't decode).
func dispatchJobParamTemplate(param *agentv1alpha1.DispatchJobTemplate) (corev1.PodTemplateSpec, bool) {
	var tmpl corev1.PodTemplateSpec
	if param.Spec.Template == nil || len(param.Spec.Template.Raw) == 0 || json.Unmarshal(param.Spec.Template.Raw, &tmpl) != nil {
		return tmpl, false
	}
	return tmpl, true
}

// defaultedJobTemplate is tmpl as the API server defaults it, from a
// server-side dry-run create of a PodTemplate in ns (nothing is stored). The
// policy compares a Job's defaulted pod template with its param field for
// field, and the API server doesn't default a DispatchJobTemplate's
// contents, so the param holds the defaulted form. Run every reconcile, so
// it follows the API server's defaults across upgrades. The operator's only
// podtemplate verb is create, for this.
func (r *PlatformAgentReconciler) defaultedJobTemplate(ctx context.Context, ns, param string, tmpl *corev1.PodTemplateSpec) (*corev1.PodTemplateSpec, error) {
	// A generated name: nothing is stored, and no PodTemplate of a fixed
	// name, the operator's or anyone's, can make the create collide.
	dry := &corev1.PodTemplate{ObjectMeta: metav1.ObjectMeta{GenerateName: param + "-", Namespace: ns}, Template: *tmpl.DeepCopy()}
	if err := r.Create(ctx, dry, client.DryRunAll); err != nil {
		return nil, fmt.Errorf("default the Job template %s (a dry-run PodTemplate): %w", param, err)
	}
	return &dry.Template, nil
}

// buildA2ADispatcherJobsRole lets the dispatcher run unattended tasks as
// Jobs in the profile namespace: create, read and delete jobs, and get the
// DispatchJobTemplates the dispatch config names a profile's template by
// (its pod-read Role covers pods). No update or patch on jobs, so the
// admission policy on CREATE is the whole of what it can shape, and no write
// verb on the policy's params, so it reads them and can't change them. Get
// only, by name: it reads the one its config names when it launches a Job.
// Marked by the agent's labels, as the pod-read Role is.
func buildA2ADispatcherJobsRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherJobsRoleName(agent), Namespace: profileNamespaceName(agent), Labels: a2aLabels(agent, "dispatcher")},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{"batch"},
			Resources: []string{"jobs"},
			Verbs:     []string{"create", "get", "list", "watch", "delete"},
		}, {
			APIGroups: []string{agentv1alpha1.GroupVersion.Group},
			Resources: []string{dispatchJobTemplateResource},
			Verbs:     []string{"get"},
		}},
	}
}

func buildA2ADispatcherJobsRoleBinding(agent *agentv1alpha1.PlatformAgent) *rbacv1.RoleBinding {
	return &rbacv1.RoleBinding{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "RoleBinding"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherJobsRoleName(agent), Namespace: profileNamespaceName(agent), Labels: a2aLabels(agent, "dispatcher")},
		RoleRef:    rbacv1.RoleRef{APIGroup: "rbac.authorization.k8s.io", Kind: "Role", Name: a2aDispatcherJobsRoleName(agent)},
		Subjects:   []rbacv1.Subject{{Kind: "ServiceAccount", Name: a2aDispatcherName(agent), Namespace: agent.Namespace}},
	}
}

func a2aDispatcherJobsRoleName(agent *agentv1alpha1.PlatformAgent) string {
	return a2aDispatcherName(agent) + "-jobs"
}

// errForeignDispatchJobParam is a DispatchJobTemplate under a param's name that
// isn't this agent's: never adopted or overwritten, since the admission
// policy trusts what it holds.
type errForeignDispatchJobParam struct{ name string }

func (e errForeignDispatchJobParam) Error() string {
	return fmt.Sprintf("DispatchJobTemplate %s in the profile namespace is not this agent's (it lacks the agent's labels), so it is left alone and Job mode is off for every profile (unattended tasks have no worker, and are refused no-worker at admission) until it is removed", e.name)
}

// reconcileDispatchJobParams brings the policy's params in line with
// templates. The policy admits a Job running as an account the index lists
// and holds it to that account's template only if the template is there, so
// at no step may the index list an account without its template, nor name
// one whose template now runs as another. So:
//  1. the index narrows to the accounts the same param's template runs as
//     both now and after this pass;
//  2. the templates are written;
//  3. the index lists the accounts they now run as;
//  4. the templates no longer wanted are deleted.
//
// A param is keyed by its name, which follows from its (profile, tier)
// (dispatchJobParamName). A DispatchJobTemplate under a param's name without
// this agent's labels stops the pass with an error.
func (r *PlatformAgentReconciler) reconcileDispatchJobParams(ctx context.Context, agent *agentv1alpha1.PlatformAgent, templates map[string]profileJobTemplates) error {
	ns := profileNamespaceName(agent)
	var existing agentv1alpha1.DispatchJobTemplateList
	if err := r.a2aReader().List(ctx, &existing, client.InNamespace(ns), client.MatchingLabels(dispatchJobParamLabels(agent))); err != nil {
		return fmt.Errorf("list the dispatcher's Job templates: %w", err)
	}
	want := map[string]*corev1.PodTemplateSpec{}
	hashes := map[string]string{}
	for _, set := range templates {
		hash := dispatchJobTemplatesHash(set)
		for name, t := range set {
			want[name], hashes[name] = t, hash
		}
	}
	var both []string
	for i := range existing.Items {
		pt := &existing.Items[i]
		if !isDispatchJobTemplateParam(pt.Name) || !agentLabelsMark(pt, agent) {
			continue
		}
		// Kept only where the same param keeps the account: one moving to
		// another (profile, tier) would otherwise be listed while the
		// template that ran as it is rewritten before its new owner's is
		// written.
		held, ok := dispatchJobParamTemplate(pt)
		if sa := held.Spec.ServiceAccountName; ok && want[pt.Name] != nil && want[pt.Name].Spec.ServiceAccountName == sa {
			both = append(both, sa)
		}
	}
	if err := r.applyDispatchJobParam(ctx, agent, buildDispatchJobIndexFor(agent, both)); err != nil {
		return err
	}
	names := make([]string, 0, len(want))
	for name := range want {
		names = append(names, name)
	}
	sort.Strings(names)
	live := map[string]*agentv1alpha1.DispatchJobTemplate{}
	for i := range existing.Items {
		live[existing.Items[i].Name] = &existing.Items[i]
	}
	server := r.apiServerVersion()
	for _, name := range names {
		// A get, a dry run and an apply per param per pass, every tier's: a
		// param already current costs none of them.
		if pt := live[name]; pt != nil && dispatchJobParamCurrent(pt, agent, want[name], hashes[name], server) {
			continue
		}
		defaulted, err := r.defaultedJobTemplate(ctx, ns, name, want[name])
		if err != nil {
			return err
		}
		param, err := buildDispatchJobTemplateBy(agent, name, hashes[name], server, defaulted)
		if err != nil {
			return err
		}
		if err := r.applyDispatchJobParam(ctx, agent, param); err != nil {
			return err
		}
	}
	if err := r.applyDispatchJobParam(ctx, agent, buildDispatchJobIndex(agent, templates)); err != nil {
		return err
	}
	for i := range existing.Items {
		pt := &existing.Items[i]
		if !isDispatchJobTemplateParam(pt.Name) || want[pt.Name] != nil || !agentLabelsMark(pt, agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, pt)); err != nil {
			return fmt.Errorf("delete the stale Job template %s: %w", pt.Name, err)
		}
	}
	return nil
}

// dispatchJobParamDigest is a param template's digest, over its JSON as
// stored.
func dispatchJobParamDigest(raw []byte) string {
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		return ""
	}
	canonical, err := json.Marshal(v)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:])[:dispatchJobTemplateHashLength]
}

// apiServerVersion is the API server's git version, or "" when it can't be
// read (no discovery client, or an error), which makes every param be
// defaulted and applied again.
func (r *PlatformAgentReconciler) apiServerVersion() string {
	if r.DiscoveryClient == nil {
		return ""
	}
	ver, err := r.DiscoveryClient.ServerVersion()
	if err != nil || ver == nil {
		return ""
	}
	return ver.GitVersion
}

// applyDispatchJobParam writes want, unless a DispatchJobTemplate of its name
// is there without this agent's labels.
func (r *PlatformAgentReconciler) applyDispatchJobParam(ctx context.Context, agent *agentv1alpha1.PlatformAgent, want *agentv1alpha1.DispatchJobTemplate) error {
	var got agentv1alpha1.DispatchJobTemplate
	err := r.a2aReader().Get(ctx, types.NamespacedName{Namespace: want.Namespace, Name: want.Name}, &got)
	switch {
	case apierrors.IsNotFound(err):
	case err != nil:
		return fmt.Errorf("read the Job template %s: %w", want.Name, err)
	case !agentLabelsMark(&got, agent):
		return errForeignDispatchJobParam{want.Name}
	}
	if err := r.applyManaged(ctx, agent, want); err != nil {
		return fmt.Errorf("apply the Job template %s: %w", want.Name, err)
	}
	return nil
}

// revokeDispatcherJobsRole deletes the dispatcher's Jobs RoleBinding and
// Role in the profile namespace, if they carry this agent's labels, so it
// can create no more Jobs; its running Jobs, and the policy's params, stay.
func (r *PlatformAgentReconciler) revokeDispatcherJobsRole(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	key := types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aDispatcherJobsRoleName(agent)}
	for _, obj := range []client.Object{&rbacv1.RoleBinding{}, &rbacv1.Role{}} {
		if err := r.a2aReader().Get(ctx, key, obj); err != nil {
			if apierrors.IsNotFound(err) {
				continue
			}
			return fmt.Errorf("the dispatcher's Jobs %T: %w", obj, err)
		}
		if !agentLabelsMark(obj, agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, obj)); err != nil {
			return fmt.Errorf("deleting the dispatcher's Jobs %T: %w", obj, err)
		}
	}
	return nil
}

// deleteDispatcherJobResources removes what Job mode put in the profile
// namespace, each only if it carries this agent's labels, read uncached. The
// Jobs Role and binding go first: once the dispatcher can't create a Job, the
// order of the rest can't open the policy (a template left without its index
// admits any account no template runs as). Then the dispatcher's Jobs (no
// dispatcher holds their leases once it is gone; propagated, so their pods go
// too), then the templates, and the index last.
func (r *PlatformAgentReconciler) deleteDispatcherJobResources(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if err := r.revokeDispatcherJobsRole(ctx, agent); err != nil {
		return err
	}
	return r.deleteDispatcherJobsAndTemplates(ctx, agent)
}

// deleteDispatcherJobsAndTemplates is deleteDispatcherJobResources after
// the revoke, for a caller that has just revoked the Jobs Role itself.
func (r *PlatformAgentReconciler) deleteDispatcherJobsAndTemplates(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	ns := profileNamespaceName(agent)
	// One by one: the operator holds delete on Jobs, not deletecollection.
	// Only those with this agent's labels (dispatchJobLabels, which the
	// policy requires of every dispatcher Job).
	var jobs batchv1.JobList
	if err := r.a2aReader().List(ctx, &jobs, client.InNamespace(ns), client.HasLabels{dispatchJobLabel}); err != nil {
		return fmt.Errorf("list the dispatcher's Jobs: %w", err)
	}
	for i := range jobs.Items {
		if !agentLabelsMark(&jobs.Items[i], agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, &jobs.Items[i],
			client.PropagationPolicy(metav1.DeletePropagationBackground))); err != nil {
			return fmt.Errorf("delete the dispatcher's Job %s: %w", jobs.Items[i].Name, err)
		}
	}
	var params agentv1alpha1.DispatchJobTemplateList
	err := r.a2aReader().List(ctx, &params, client.InNamespace(ns), client.MatchingLabels(dispatchJobParamLabels(agent)))
	switch {
	case isCRDNotInstalledError(err):
		// The kind isn't served (a helm upgrade doesn't apply crds/), so
		// there are no templates to delete.
		return nil
	case err != nil:
		return fmt.Errorf("list the dispatcher's Job templates: %w", err)
	}
	// The index last.
	sort.SliceStable(params.Items, func(i, j int) bool {
		return params.Items[j].Name == dispatchJobIndexName && params.Items[i].Name != dispatchJobIndexName
	})
	for i := range params.Items {
		if !agentLabelsMark(&params.Items[i], agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, &params.Items[i])); err != nil {
			return fmt.Errorf("delete the Job template %s: %w", params.Items[i].Name, err)
		}
	}
	return nil
}
