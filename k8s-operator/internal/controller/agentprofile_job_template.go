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
	"fmt"
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
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/profilepod"
)

// Job mode for the unattended tier (#3655 PR 6). The dispatcher runs each
// unattended task as a Job in the profile namespace, from a pod template the
// operator renders per profile. The same template goes two places: into the
// dispatch config (a2aDispatchProfile.JobTemplate), which the dispatcher
// stamps a Job from, and into a PodTemplate in the profile namespace, which
// the admission policy (config/admission/a2a-dispatcher-job-policy.yaml)
// holds every dispatcher Job to. An index PodTemplate lists the
// ServiceAccounts those templates run as, so a Job running as any other is
// denied. Only the operator writes PodTemplates there.
const (
	// dispatchJobTemplatePrefix names a profile's PodTemplate:
	// kube-agents-dispatch-job-profile-<profile>.
	dispatchJobTemplatePrefix = "kube-agents-dispatch-job-profile-"
	// dispatchJobIndexName is the index PodTemplate, which the policy tells
	// apart by this name. The templates' prefix keeps a profile named
	// "index" from taking it.
	dispatchJobIndexName = "kube-agents-dispatch-job-index"
	// dispatchJobAccountsAnnotation lists, on the index, the ServiceAccounts
	// the templates run as, comma-separated and sorted.
	dispatchJobAccountsAnnotation = "kube-agents.dev/dispatch-job-service-accounts"
	// dispatchJobTemplateLabel and the managed-by label (commonLabels'
	// labelManagedBy: fieldOwner) are what the policy's binding selects
	// params by.
	dispatchJobTemplateLabel = "kube-agents.dev/dispatch-job-template"
	// dispatchJobLabel marks a Job the dispatcher created (lib.DispatchJobLabel),
	// so a teardown can find them.
	dispatchJobLabel = "kubeagents.x-k8s.io/dispatch-job"
	// dispatchJobIndexImage is the index's container image. The index is a
	// PodTemplate only so the policy can read it as a param; nothing ever runs
	// from it, and the API server requires a container in it.
	dispatchJobIndexImage = "registry.k8s.io/pause:3.10"
	// The worker's scratch workspace and its tmpfs for the proxy task token
	// (contract addendum 8).
	workerScratchVolume    = "scratch"
	workerScratchSizeLimit = "2Gi"
	workerTokenVolume      = "task-token"
	workerTokenSizeLimit   = "1Mi"
	workerContainerName    = "worker"
)

// profileWorkerPodSpecForJob is the worker pod a profile's Job runs. It is a
// stand-in until the warm worker (#3655 PR 5) lands buildProfileWorkerPodSpec,
// which then replaces it here, in the shape PR 5's pod contract on #3655
// gives; the Job wraps whatever it returns.
var profileWorkerPodSpecForJob = stubProfileWorkerPodSpec

// stubProfileWorkerPodSpec is the worker pod in PR 5's posted shape: the
// harness container under the profile's tier ServiceAccount, the persona and
// tools mounted (image volumes, or staged into emptyDirs), a scratch
// emptyDir, a tmpfs for the proxy task token, the bus and broker tokens, and
// the bus CA. Pod Security restricted, and nothing scheduling-specific.
func stubProfileWorkerPodSpec(agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile, tier string, imageVolumes bool) corev1.PodSpec {
	persona := profilepod.RenderPersonaAndTools(p, imageVolumes)
	volumes := []corev1.Volume{
		{Name: workerScratchVolume, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse(workerScratchSizeLimit))}}},
		{Name: workerTokenVolume, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse(workerTokenSizeLimit))}}},
		a2aBusTokenVolumeSource(),
		{Name: "broker-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{Sources: []corev1.VolumeProjection{{
			ServiceAccountToken: &corev1.ServiceAccountTokenProjection{Audience: credentialProxyProfileAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token"},
		}}}}},
		a2aNATSCABundleVolume(agent),
	}
	volumes = append(volumes, persona.Volumes...)
	mounts := []corev1.VolumeMount{
		{Name: workerScratchVolume, MountPath: "/workspace"},
		{Name: workerTokenVolume, MountPath: "/run/task-token"},
		a2aNATSCABundleMount(),
	}
	mounts = append(mounts, persona.Mounts...)
	return corev1.PodSpec{
		ServiceAccountName:           agentProfileServiceAccountName(p, tier),
		AutomountServiceAccountToken: ptr.To(false),
		RestartPolicy:                corev1.RestartPolicyNever,
		SecurityContext: &corev1.PodSecurityContext{
			RunAsNonRoot:   ptr.To(true),
			SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
		},
		InitContainers: persona.InitContainers,
		Containers: []corev1.Container{{
			Name:  workerContainerName,
			Image: resolveAgentImage(agent.Spec.Deployment, defaultPlatformAgentImage()),
			Env: []corev1.EnvVar{
				{Name: "PROFILE_WORKER_ONE_SHOT", Value: "1"},
				{Name: "POD_NAME", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"}}},
				{Name: "POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}},
			},
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
			},
			VolumeMounts: mounts,
		}},
		Volumes: volumes,
	}
}

// buildProfileJobTemplate is the pod template a profile's unattended Jobs
// run: the worker pod, restartPolicy Never (a Job's pod must not restart, and
// a PodTemplate defaults it to Always, which would break the policy's exact
// comparison), with the worker labels for the profile's unattended tier.
func buildProfileJobTemplate(agent *agentv1alpha1.PlatformAgent, p *agentv1alpha1.AgentProfile, imageVolumes bool) *corev1.PodTemplateSpec {
	tier := agentv1alpha1.AgentProfileTierUnattended
	spec := profileWorkerPodSpecForJob(agent, p, tier, imageVolumes)
	spec.RestartPolicy = corev1.RestartPolicyNever
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
// three (the worker's cpu and memory from the profile, anything else unset
// from jobDefaultResources, a limit from its request), and the toleration is
// added. Autopilot also raises a request that breaks its cpu-to-memory ratio;
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
	for i := range spec.Containers {
		var requests, limits corev1.ResourceList
		if i == 0 { // the worker
			requests = corev1.ResourceList{corev1.ResourceCPU: profile.Requests.CPU, corev1.ResourceMemory: profile.Requests.Memory}
			limits = corev1.ResourceList{corev1.ResourceCPU: profile.Limits.CPU, corev1.ResourceMemory: profile.Limits.Memory}
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

// a2aDispatcherJobPolicyEnvVar is the chart's signal that it rendered the
// dispatcher Job admission policy (config/admission/a2a-dispatcher-job-policy.yaml),
// set from the same helper as the policy. Job mode runs only where it is set:
// without the policy the dispatcher's Job create would be as wide as its Role,
// so the operator renders no Job templates and no Jobs Role, and unattended
// tasks run on the pool. It says the policy was rendered, not that it still
// holds.
const a2aDispatcherJobPolicyEnvVar = "A2A_DISPATCHER_JOB_POLICY"

func dispatcherJobPolicyRendered() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(a2aDispatcherJobPolicyEnvVar)), "true")
}

// dispatchJobTemplates is the Job template of each profile the dispatch
// config admits that allows the unattended tier, by profile name.
func dispatchJobTemplates(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, imageVolumes bool) map[string]*corev1.PodTemplateSpec {
	if !dispatcherJobPolicyRendered() {
		return nil
	}
	out := map[string]*corev1.PodTemplateSpec{}
	resolved := resolveAgentProfileIdentities(agent, profiles)
	for i := range profiles {
		p := &profiles[i]
		if resolved[p.Name].refused != nil || !slices.Contains(p.AllowedTierList(), agentv1alpha1.AgentProfileTierUnattended) {
			continue
		}
		out[p.Name] = buildProfileJobTemplate(agent, p, imageVolumes)
	}
	return out
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

// buildDispatchJobTemplate is profile's PodTemplate param: its Job template,
// as the dispatcher stamps it.
func buildDispatchJobTemplate(agent *agentv1alpha1.PlatformAgent, profile string, tmpl *corev1.PodTemplateSpec) *corev1.PodTemplate {
	return &corev1.PodTemplate{
		TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "PodTemplate"},
		ObjectMeta: metav1.ObjectMeta{Name: dispatchJobTemplatePrefix + profile, Namespace: profileNamespaceName(agent), Labels: dispatchJobParamLabels(agent)},
		Template:   *tmpl.DeepCopy(),
	}
}

// buildDispatchJobIndex is the index param: the ServiceAccounts the
// templates run as. A PodTemplate only so the policy can read it, with an
// inert container the API server requires and nothing ever runs.
func buildDispatchJobIndex(agent *agentv1alpha1.PlatformAgent, templates map[string]*corev1.PodTemplateSpec) *corev1.PodTemplate {
	var accounts []string
	for _, t := range templates {
		accounts = append(accounts, t.Spec.ServiceAccountName)
	}
	return buildDispatchJobIndexFor(agent, accounts)
}

// buildDispatchJobIndexFor is the index listing accounts.
func buildDispatchJobIndexFor(agent *agentv1alpha1.PlatformAgent, accounts []string) *corev1.PodTemplate {
	accounts = slices.Clone(accounts)
	sort.Strings(accounts)
	accounts = slices.Compact(accounts)
	return &corev1.PodTemplate{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "PodTemplate"},
		ObjectMeta: metav1.ObjectMeta{Name: dispatchJobIndexName, Namespace: profileNamespaceName(agent), Labels: dispatchJobParamLabels(agent),
			Annotations: map[string]string{dispatchJobAccountsAnnotation: strings.Join(accounts, ",")}},
		Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{
			AutomountServiceAccountToken: ptr.To(false),
			Containers:                   []corev1.Container{{Name: "index", Image: dispatchJobIndexImage}},
		}},
	}
}

// buildA2ADispatcherJobsRole lets the dispatcher run unattended tasks as
// Jobs in the profile namespace: create, read and delete jobs, nothing else
// (its pod-read Role covers pods). No update or patch, so the admission
// policy on CREATE is the whole of what it can shape, and no podtemplate
// verb, so it can't touch the policy's params. Marked by the agent's labels,
// as the pod-read Role is.
func buildA2ADispatcherJobsRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta:   metav1.TypeMeta{APIVersion: "rbac.authorization.k8s.io/v1", Kind: "Role"},
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatcherJobsRoleName(agent), Namespace: profileNamespaceName(agent), Labels: a2aLabels(agent, "dispatcher")},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{"batch"},
			Resources: []string{"jobs"},
			Verbs:     []string{"create", "get", "list", "watch", "delete"},
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

// errForeignDispatchJobParam is a PodTemplate under a param's name that
// isn't this agent's: never adopted or overwritten, since the admission
// policy trusts what it holds.
type errForeignDispatchJobParam struct{ name string }

func (e errForeignDispatchJobParam) Error() string {
	return fmt.Sprintf("PodTemplate %s in the profile namespace is not this agent's (it lacks the agent's labels), so it is left alone and the profile's Jobs are not admitted until it is removed", e.name)
}

// reconcileDispatchJobParams brings the policy's params in line with
// templates. The policy admits a Job running as an account the index lists
// and holds it to that account's template only if the template is there, so
// at no step may the index list an account without its template, nor name
// one whose template now runs as another. So:
//  1. the index narrows to the accounts the same profile's template runs as
//     both now and after this pass;
//  2. the templates are written;
//  3. the index lists the accounts they now run as;
//  4. the templates no longer wanted are deleted.
//
// A PodTemplate under a param's name without this agent's labels stops the
// pass with an error.
func (r *PlatformAgentReconciler) reconcileDispatchJobParams(ctx context.Context, agent *agentv1alpha1.PlatformAgent, templates map[string]*corev1.PodTemplateSpec) error {
	ns := profileNamespaceName(agent)
	var existing corev1.PodTemplateList
	if err := r.a2aReader().List(ctx, &existing, client.InNamespace(ns), client.MatchingLabels(dispatchJobParamLabels(agent))); err != nil {
		return fmt.Errorf("list the dispatcher's Job templates: %w", err)
	}
	want := map[string]bool{}
	for _, t := range templates {
		want[t.Spec.ServiceAccountName] = true
	}
	var both []string
	for i := range existing.Items {
		pt := &existing.Items[i]
		profile, ok := strings.CutPrefix(pt.Name, dispatchJobTemplatePrefix)
		if !ok || pt.Name == dispatchJobIndexName || !agentLabelsMark(pt, agent) {
			continue
		}
		// Kept only where the same profile keeps the account: one moving to
		// another profile would otherwise be listed while the template that
		// ran as it is rewritten before its new owner's is written.
		if sa := pt.Template.Spec.ServiceAccountName; templates[profile] != nil && templates[profile].Spec.ServiceAccountName == sa {
			both = append(both, sa)
		}
	}
	if err := r.applyDispatchJobParam(ctx, agent, buildDispatchJobIndexFor(agent, both)); err != nil {
		return err
	}
	names := make([]string, 0, len(templates))
	for name := range templates {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if err := r.applyDispatchJobParam(ctx, agent, buildDispatchJobTemplate(agent, name, templates[name])); err != nil {
			return err
		}
	}
	if err := r.applyDispatchJobParam(ctx, agent, buildDispatchJobIndex(agent, templates)); err != nil {
		return err
	}
	for i := range existing.Items {
		pt := &existing.Items[i]
		profile, ok := strings.CutPrefix(pt.Name, dispatchJobTemplatePrefix)
		if !ok || pt.Name == dispatchJobIndexName || templates[profile] != nil || !agentLabelsMark(pt, agent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, pt)); err != nil {
			return fmt.Errorf("delete the stale Job template %s: %w", pt.Name, err)
		}
	}
	return nil
}

// applyDispatchJobParam writes want, unless a PodTemplate of its name is
// there without this agent's labels.
func (r *PlatformAgentReconciler) applyDispatchJobParam(ctx context.Context, agent *agentv1alpha1.PlatformAgent, want *corev1.PodTemplate) error {
	var got corev1.PodTemplate
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

// deleteDispatcherJobResources removes what Job mode put in the profile
// namespace, each only if it carries this agent's labels, read uncached. The
// Jobs Role and binding go first: once the dispatcher can't create a Job, the
// order of the rest can't open the policy (a template left without its index
// admits any account no template runs as). Then the dispatcher's Jobs (no
// dispatcher holds their leases once it is gone; propagated, so their pods go
// too), then the templates, and the index last.
func (r *PlatformAgentReconciler) deleteDispatcherJobResources(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	ns := profileNamespaceName(agent)
	key := types.NamespacedName{Namespace: ns, Name: a2aDispatcherJobsRoleName(agent)}
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
	var params corev1.PodTemplateList
	if err := r.a2aReader().List(ctx, &params, client.InNamespace(ns), client.MatchingLabels(dispatchJobParamLabels(agent))); err != nil {
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
