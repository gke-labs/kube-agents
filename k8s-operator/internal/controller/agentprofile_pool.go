package controller

// The AgentProfile workers (#3655, PR 5b): one builder for the worker pod,
// which the warm pools here and PR 6's Jobs both render, and the warm pool
// itself, a Deployment per (profile, interactive tier) in the profile
// namespace. The pod's shape is contract addendum 10 on #3655, and the worker
// it runs is the adapter's profile mode (a2a/worker-adapter/profile.go).

import (
	"context"
	"fmt"
	"strconv"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/profilepod"
)

// profileWorkerMode is how a worker pod runs: a warm pool's pods serve task
// after task, a Job's pod serves one (PROFILE_WORKER_ONE_SHOT).
type profileWorkerMode int

const (
	profileWorkerPool profileWorkerMode = iota
	profileWorkerOneShot
)

const (
	profileWorkerContainer = "worker"
	// profileWorkerUID is the worker image's user (a2a-worker, node:22's
	// node user), and its FSGroup.
	profileWorkerUID = 1000
	// profileWorkerStatusPort is lib.ProfileWorkerStatusPort: /readyz and
	// /healthz, pod-local.
	profileWorkerStatusPort = 8080

	profileWorkerScratchVolume    = "scratch"
	profileWorkerScratchPath      = "/scratch"
	profileWorkerScratchSizeLimit = "4Gi"

	// The proxy token the confirm carries (addendum 8), written by the
	// worker for the task's lifetime. Memory-backed, so it never lands on a
	// node disk.
	// The harness's per-task config directories (its CLAUDE_CONFIG_DIR):
	// an emptyDir of their own, apart from the working directory, which the
	// worker renders the persona into read-only (lib.ProfileConfigRoot).
	profileWorkerConfigVolume = "harness-config"
	profileWorkerConfigPath   = "/var/run/kube-agents/harness-config"

	profileWorkerTaskTokenVolume = "task-token"
	profileWorkerTaskTokenPath   = "/var/run/kube-agents/task-token"

	// The worker's broker token: a projected ServiceAccount token for the
	// broker's profile audience (#4257).
	profileWorkerBrokerTokenVolume = "credential-proxy-token"
	profileWorkerBrokerTokenPath   = "/var/run/secrets/credential-proxy"
	profileWorkerBrokerTokenFile   = "token"
	profileWorkerBrokerTokenExpiry = 3600

	// profileWorkerImagePath is the worker image's PATH, which the tools'
	// bin directories go in front of.
	profileWorkerImagePath = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

	// profileWorkerModelKey only satisfies the harness's "an API key exists"
	// check: the install's inference gateway runs keyless (the session
	// worker's default, a2a/cmd/worker-adapter).
	profileWorkerModelKey = "a2a-playground" // #nosec G101 -- placeholder, not a credential
)

// profileWorkerLabels are a worker pod's labels: the agent's, the worker
// component, and the profile and tier the dispatcher's pool selects on
// (lib.ProfilePoolSelector).
func profileWorkerLabels(agent *agentv1alpha1.PlatformAgent, profile, tier string) map[string]string {
	labels := a2aLabels(agent, agentProfileWorkerComponent)
	labels[agentProfileNameLabel] = profile
	labels[agentProfileTierLabel] = tier
	return labels
}

// buildProfileWorkerPodSpec is the worker pod for one (profile, tier), as a
// warm pool or a Job renders it. serviceAccount is the (profile, tier)'s,
// from the identity resolution. imageVolumes says whether the persona and
// tools mount as image volumes or are staged by init containers
// (profilepod.RenderPersonaAndTools).
func buildProfileWorkerPodSpec(agent *agentv1alpha1.PlatformAgent, profile *agentv1alpha1.AgentProfile, tier, serviceAccount string, mode profileWorkerMode, imageVolumes bool) corev1.PodSpec {
	rendered := profilepod.RenderPersonaAndTools(profile, imageVolumes)
	env := []corev1.EnvVar{
		{Name: "PROFILE", Value: profile.Name},
		{Name: "NATS_URL", Value: a2aNATSClientURL(agent)},
		a2aNATSCAFileEnvVar(),
		{Name: "A2A_POD_NAME", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"}}},
		{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"}}},
		{Name: "A2A_PROFILE_EXECUTOR", Value: "true"},
		{Name: "A2A_PROFILE_WORKER", Value: "true"},
		{Name: "A2A_PROFILE_TIER", Value: tier},
		{Name: "A2A_WORKDIR", Value: profileWorkerScratchPath},
		{Name: "A2A_PROFILE_CONFIG_ROOT", Value: profileWorkerConfigPath},
		// Children of a profile task go through the gateway, which a later
		// change wires; until then the worker has no delegate tool.
		{Name: "A2A_DELEGATE_TOOL", Value: "off"},
		// The install's inference gateway, by its full name: the worker runs
		// in the profile namespace, where the session worker's short default
		// doesn't resolve.
		{Name: "ANTHROPIC_BASE_URL", Value: fmt.Sprintf("http://%s.%s.svc.cluster.local", inferenceGatewayServiceName, agent.Namespace)},
		{Name: "ANTHROPIC_API_KEY", Value: profileWorkerModelKey},
		{Name: "CREDENTIAL_PROXY_URL", Value: credentialProxyURL(agent)},
		{Name: "CREDENTIAL_PROXY_TOKEN_FILE", Value: profileWorkerBrokerTokenPath + "/" + profileWorkerBrokerTokenFile},
		{Name: "CREDENTIAL_PROXY_TASK_TOKEN_FILE", Value: profileWorkerTaskTokenPath + "/token"},
	}
	if m := profile.Spec.Harness.Model; m != "" {
		env = append(env, corev1.EnvVar{Name: "A2A_MODEL", Value: m})
	}
	if n := profile.Spec.Harness.MaxTurns; n > 0 {
		env = append(env, corev1.EnvVar{Name: "A2A_MAX_TURNS", Value: strconv.Itoa(int(n))})
	}
	if rendered.PathPrefix != "" {
		env = append(env, corev1.EnvVar{Name: "PATH", Value: rendered.PathPrefix + ":" + profileWorkerImagePath})
	}
	restart := corev1.RestartPolicyAlways
	if mode == profileWorkerOneShot {
		env = append(env, corev1.EnvVar{Name: "PROFILE_WORKER_ONE_SHOT", Value: "1"})
		restart = corev1.RestartPolicyNever
	}

	mounts := []corev1.VolumeMount{
		{Name: profileWorkerScratchVolume, MountPath: profileWorkerScratchPath},
		{Name: profileWorkerConfigVolume, MountPath: profileWorkerConfigPath},
		{Name: profileWorkerTaskTokenVolume, MountPath: profileWorkerTaskTokenPath},
		a2aBusTokenVolumeMount(),
		{Name: profileWorkerBrokerTokenVolume, MountPath: profileWorkerBrokerTokenPath, ReadOnly: true},
		a2aNATSCABundleMount(),
	}
	mounts = append(mounts, rendered.Mounts...)
	volumes := []corev1.Volume{
		{Name: profileWorkerScratchVolume, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{
			SizeLimit: ptr.To(resource.MustParse(profileWorkerScratchSizeLimit)),
		}}},
		{Name: profileWorkerConfigVolume, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{
			SizeLimit: ptr.To(resource.MustParse("256Mi")),
		}}},
		{Name: profileWorkerTaskTokenVolume, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{
			Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("1Mi")),
		}}},
		a2aBusTokenVolumeSource(),
		{Name: profileWorkerBrokerTokenVolume, VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience:          credentialProxyProfileAudience,
				ExpirationSeconds: ptr.To(int64(profileWorkerBrokerTokenExpiry)),
				Path:              profileWorkerBrokerTokenFile,
			}}},
		}}},
		a2aNATSCABundleVolume(agent),
	}
	volumes = append(volumes, rendered.Volumes...)

	res := profile.Spec.Resources
	return corev1.PodSpec{
		ServiceAccountName:           serviceAccount,
		AutomountServiceAccountToken: ptr.To(false),
		RestartPolicy:                restart,
		RuntimeClassName:             shellSandboxRuntimeClassName(agent),
		SecurityContext: &corev1.PodSecurityContext{
			RunAsNonRoot:   ptr.To(true),
			RunAsUser:      ptr.To(int64(profileWorkerUID)),
			FSGroup:        ptr.To(int64(profileWorkerUID)),
			SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
		},
		Volumes:        volumes,
		InitContainers: rendered.InitContainers,
		Containers: []corev1.Container{{
			Name:         profileWorkerContainer,
			Image:        profile.Spec.Harness.Image,
			Env:          env,
			VolumeMounts: mounts,
			Ports:        []corev1.ContainerPort{{Name: "status", ContainerPort: profileWorkerStatusPort}},
			// Ready only once the worker is listening on its dispatch
			// subject, so the dispatcher never offers into a pod that would
			// drop the offer.
			ReadinessProbe: &corev1.Probe{
				ProbeHandler:  corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: "/readyz", Port: intstr.FromInt32(profileWorkerStatusPort)}},
				PeriodSeconds: 2,
			},
			LivenessProbe: &corev1.Probe{
				ProbeHandler:  corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: "/healthz", Port: intstr.FromInt32(profileWorkerStatusPort)}},
				PeriodSeconds: 20, FailureThreshold: 3,
			},
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{corev1.ResourceCPU: res.Requests.CPU, corev1.ResourceMemory: res.Requests.Memory},
				Limits:   corev1.ResourceList{corev1.ResourceCPU: res.Limits.CPU, corev1.ResourceMemory: res.Limits.Memory},
			},
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
				SeccompProfile:           &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
		}},
	}
}

// profilePoolName is the warm pool Deployment for one (profile, tier).
func profilePoolName(profile, tier string) string {
	return "agentprofile-" + profile + "-" + tier
}

// buildProfilePool is one (profile, interactive tier)'s warm pool: warmReplicas
// pods of the worker, in the profile namespace. It has no owner reference,
// since an owner can't be in another namespace, so the operator removes it by
// its labels (deleteProfilePools).
func buildProfilePool(agent *agentv1alpha1.PlatformAgent, profile *agentv1alpha1.AgentProfile, tier, serviceAccount string, imageVolumes bool) *appsv1.Deployment {
	labels := profileWorkerLabels(agent, profile.Name, tier)
	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{APIVersion: "apps/v1", Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      profilePoolName(profile.Name, tier),
			Namespace: profileNamespaceName(agent),
			Labels:    labels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: ptr.To(int32(resolveProfileWarmReplicas(agent))),
			Selector: &metav1.LabelSelector{MatchLabels: map[string]string{
				a2aComponentLabel:     agentProfileWorkerComponent,
				agentProfileNameLabel: profile.Name,
				agentProfileTierLabel: tier,
			}},
			// One surge pod per pool, which the profile quota budgets for
			// (profileQuotaHard), and no pod taken down before its
			// replacement is Ready, so a rollout never leaves the pool empty.
			Strategy: appsv1.DeploymentStrategy{
				Type: appsv1.RollingUpdateDeploymentStrategyType,
				RollingUpdate: &appsv1.RollingUpdateDeployment{
					MaxSurge:       ptr.To(intstr.FromInt32(1)),
					MaxUnavailable: ptr.To(intstr.FromInt32(0)),
				},
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec:       buildProfileWorkerPodSpec(agent, profile, tier, serviceAccount, profileWorkerPool, imageVolumes),
			},
		},
	}
}

// interactiveTier reports whether a tier runs on a warm pool. The unattended
// tier runs as a Job per task (PR 6).
func interactiveTier(tier string) bool {
	return tier == agentv1alpha1.AgentProfileTierRead
}

// profileCABundleComponent labels the NATS CA bundle's copy in the profile
// namespace.
const profileCABundleComponent = "profile-nats-ca-bundle"

// buildProfileCABundle is the agent namespace's NATS CA bundle, copied into
// the profile namespace: a pod mounts a ConfigMap from its own namespace
// only.
func buildProfileCABundle(agent *agentv1alpha1.PlatformAgent, data map[string]string) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      a2aNATSCABundleName(agent),
			Namespace: profileNamespaceName(agent),
			Labels:    a2aLabels(agent, profileCABundleComponent),
		},
		Data: data,
	}
}

// reconcileProfilePools renders a warm pool for every admitted profile's
// interactive tiers, with the CA bundle they mount, and removes this agent's
// pools that no longer have a profile or tier. Only with the dispatcher, which
// is the pools' only source of work.
func (r *PlatformAgentReconciler) reconcileProfilePools(ctx context.Context, agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, resolved map[string]agentProfileResolution) error {
	imageVolumes := r.imageVolumeSupported(agent)
	var pools []*appsv1.Deployment
	for i := range profiles {
		res := resolved[profiles[i].Name]
		if res.refused != nil {
			continue
		}
		for _, id := range res.identities {
			if interactiveTier(id.tier) {
				pools = append(pools, buildProfilePool(agent, &profiles[i], id.tier, id.serviceAccount, imageVolumes))
			}
		}
	}
	want := map[string]bool{}
	if len(pools) > 0 {
		// The CA bundle the pools mount, copied before the pools that need
		// it.
		src := &corev1.ConfigMap{}
		if err := r.a2aReader().Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSCABundleName(agent)}, src); err != nil {
			return fmt.Errorf("reading the NATS CA bundle to copy into the profile namespace: %w", err)
		}
		if err := r.applyManaged(ctx, agent, buildProfileCABundle(agent, src.Data)); err != nil {
			return fmt.Errorf("the profile namespace's NATS CA bundle: %w", err)
		}
		for _, pool := range pools {
			if err := r.applyManaged(ctx, agent, pool); err != nil {
				return fmt.Errorf("the warm pool %s: %w", pool.Name, err)
			}
			want[pool.Name] = true
		}
	}
	return r.deleteProfilePoolsExcept(ctx, agent, want)
}

// deleteProfilePoolsExcept deletes this agent's warm pools in the profile
// namespace whose names aren't in keep. A pool without this agent's labels is
// someone else's and stays. Read uncached: the profile namespace's
// Deployments aren't in the manager's cache.
func (r *PlatformAgentReconciler) deleteProfilePoolsExcept(ctx context.Context, agent *agentv1alpha1.PlatformAgent, keep map[string]bool) error {
	pools := &appsv1.DeploymentList{}
	if err := r.a2aReader().List(ctx, pools, client.InNamespace(profileNamespaceName(agent)),
		client.MatchingLabels{a2aComponentLabel: agentProfileWorkerComponent}); err != nil {
		return fmt.Errorf("listing the warm pools: %w", err)
	}
	for i := range pools.Items {
		pool := &pools.Items[i]
		if keep[pool.Name] || !agentLabelsMarkComponent(pool, agent, agentProfileWorkerComponent) {
			continue
		}
		if err := client.IgnoreNotFound(r.Delete(ctx, pool, client.PropagationPolicy(metav1.DeletePropagationBackground))); err != nil {
			return fmt.Errorf("deleting the warm pool %s: %w", pool.Name, err)
		}
	}
	return nil
}

// deleteProfilePools removes every warm pool of this agent's, and the CA
// bundle's copy, from the profile namespace: the gate turned off, the flip to
// today, and PlatformAgent deletion (through deleteDispatcherPodsRBAC).
func (r *PlatformAgentReconciler) deleteProfilePools(ctx context.Context, agent *agentv1alpha1.PlatformAgent) error {
	if err := r.deleteProfilePoolsExcept(ctx, agent, nil); err != nil {
		return err
	}
	bundle := &corev1.ConfigMap{}
	key := types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aNATSCABundleName(agent)}
	if err := r.a2aReader().Get(ctx, key, bundle); err != nil {
		return client.IgnoreNotFound(err)
	}
	if !agentLabelsMarkComponent(bundle, agent, profileCABundleComponent) {
		return nil
	}
	return client.IgnoreNotFound(r.Delete(ctx, bundle))
}
