package controller

import (
	"context"
	"slices"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/profilepod"
)

func envOf(c corev1.Container) map[string]string {
	env := map[string]string{}
	for _, e := range c.Env {
		env[e.Name] = e.Value
		if e.ValueFrom != nil && e.ValueFrom.FieldRef != nil {
			env[e.Name] = "fieldRef:" + e.ValueFrom.FieldRef.FieldPath
		}
	}
	return env
}

// The worker pod is contract addendum 10's: one harness container running
// the adapter's profile mode, the volumes it lists and no others, Ready on
// the worker's own /readyz, and nothing that schedules it apart.
func TestTheWorkerPodIsTheContractsShape(t *testing.T) {
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor")
	profile.Spec.Harness.Model = "model-x"
	profile.Spec.Harness.MaxTurns = 7
	spec := buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", profileWorkerPool, false)

	if spec.ServiceAccountName != "agentprofile-auditor-read" || spec.AutomountServiceAccountToken == nil || *spec.AutomountServiceAccountToken {
		t.Errorf("serviceAccount %q automount %v", spec.ServiceAccountName, spec.AutomountServiceAccountToken)
	}
	if spec.RestartPolicy != corev1.RestartPolicyAlways {
		t.Errorf("a pool pod's restartPolicy is %s", spec.RestartPolicy)
	}
	if len(spec.Containers) != 1 || spec.Containers[0].Image != profile.Spec.Harness.Image {
		t.Fatalf("containers %+v, want one running the harness image", spec.Containers)
	}
	c := spec.Containers[0]
	env := envOf(c)
	for k, want := range map[string]string{
		"PROFILE": "auditor", "A2A_PROFILE_EXECUTOR": "true", "A2A_PROFILE_WORKER": "true", "A2A_PROFILE_TIER": "read",
		"A2A_POD_NAME": "fieldRef:metadata.name", "A2A_MODEL": "model-x", "A2A_MAX_TURNS": "7",
		"A2A_PROFILE_CONFIG_ROOT":          profileWorkerConfigPath,
		"A2A_WORKDIR":                      profileWorkerScratchPath,
		"CREDENTIAL_PROXY_TASK_TOKEN_FILE": profileWorkerTaskTokenPath + "/token",
		"NATS_URL":                         a2aNATSClientURL(agent),
	} {
		if env[k] != want {
			t.Errorf("env %s = %q, want %q", k, env[k], want)
		}
	}
	if _, ok := env["PROFILE_WORKER_ONE_SHOT"]; ok {
		t.Error("a pool pod is one-shot")
	}
	if !strings.HasSuffix(env["ANTHROPIC_BASE_URL"], ".svc.cluster.local") {
		t.Errorf("the model endpoint %q isn't fully qualified; the profile namespace can't resolve the short name", env["ANTHROPIC_BASE_URL"])
	}
	if p := c.ReadinessProbe; p == nil || p.HTTPGet == nil || p.HTTPGet.Path != "/readyz" || p.HTTPGet.Port.IntValue() != profileWorkerStatusPort {
		t.Errorf("readiness %+v, want the worker's /readyz", c.ReadinessProbe)
	}

	// Volumes: exactly the contract's, and two projected tokens for the two
	// audiences.
	var audiences []string
	for _, v := range spec.Volumes {
		switch {
		case v.Secret != nil || v.HostPath != nil:
			t.Errorf("volume %s is a Secret or a hostPath", v.Name)
		case v.Projected != nil:
			for _, src := range v.Projected.Sources {
				if src.ServiceAccountToken != nil {
					audiences = append(audiences, src.ServiceAccountToken.Audience)
				}
			}
		}
	}
	slices.Sort(audiences)
	if want := []string{a2aBusTokenAudience, credentialProxyProfileAudience}; !slices.Equal(audiences, want) {
		t.Errorf("token audiences %v, want %v", audiences, want)
	}
	mounts := map[string]bool{}
	for _, m := range c.VolumeMounts {
		mounts[m.MountPath] = true
	}
	for _, path := range []string{profileWorkerScratchPath, profileWorkerConfigPath, profileWorkerTaskTokenPath, profilepod.PersonaMountPath} {
		if !mounts[path] {
			t.Errorf("nothing mounted at %s", path)
		}
	}
	volumes := map[string]bool{}
	for _, v := range spec.Volumes {
		volumes[v.Name] = true
	}
	for _, m := range append(append([]corev1.VolumeMount{}, c.VolumeMounts...), initMounts(spec)...) {
		if !volumes[m.Name] {
			t.Errorf("mount %s at %s names no volume on the pod", m.Name, m.MountPath)
		}
	}
	if profileWorkerConfigPath == profileWorkerScratchPath || strings.HasPrefix(profileWorkerConfigPath, profileWorkerScratchPath+"/") {
		t.Error("the harness config root is inside the working directory")
	}

	if spec.NodeSelector != nil || spec.Affinity != nil || len(spec.Tolerations) > 0 || spec.PriorityClassName != "" || spec.SchedulerName != "" {
		t.Errorf("the worker pod sets scheduling fields: %+v", spec)
	}
	if sc := spec.SecurityContext; sc == nil || sc.RunAsNonRoot == nil || !*sc.RunAsNonRoot || sc.FSGroup == nil {
		t.Errorf("pod security context %+v", spec.SecurityContext)
	}

	oneShot := buildProfileWorkerPodSpec(agent, &profile, "unattended", "agentprofile-auditor-unattended", profileWorkerOneShot, false)
	if oneShot.RestartPolicy != corev1.RestartPolicyNever || envOf(oneShot.Containers[0])["PROFILE_WORKER_ONE_SHOT"] != "1" {
		t.Errorf("a one-shot pod: restartPolicy %s, env %v", oneShot.RestartPolicy, envOf(oneShot.Containers[0]))
	}
}

func poolTestReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	bundle := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aNATSCACertKey: "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"},
	}
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(append([]client.Object{agent, chartProfileNamespace(agent), bundle}, objs...)...).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	return &PlatformAgentReconciler{Client: c, Scheme: scheme}, c
}

// A profile gets a warm pool for each interactive tier it allows, and none for
// unattended (a Job per task, PR 6), with the CA bundle copied beside it.
// Dropping the tier removes its pool; a pool under the name without this
// agent's labels is someone else's and stays; and turning the dispatcher off
// removes this agent's pools and the bundle.
func TestWarmPoolsFollowTheProfilesInteractiveTiers(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	})
	pns := profileNamespaceName(agent)
	foreign := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: "agentprofile-other-read", Namespace: pns,
		Labels: map[string]string{a2aComponentLabel: agentProfileWorkerComponent, labelInstance: "someone-else"},
	}}
	r, c := poolTestReconciler(t, agent, foreign)
	ctx := context.Background()
	reconcile := func(profiles ...agentv1alpha1.AgentProfile) {
		t.Helper()
		if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles, true); err != nil || profileErr != nil {
			t.Fatalf("reconcile: %v / %v", err, profileErr)
		}
	}
	pool := func(tier string) error {
		return c.Get(ctx, types.NamespacedName{Namespace: pns, Name: profilePoolName("auditor", tier)}, &appsv1.Deployment{})
	}

	reconcile(profile)
	read := &appsv1.Deployment{}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: profilePoolName("auditor", "read")}, read); err != nil {
		t.Fatalf("no warm pool for the read tier: %v", err)
	}
	if *read.Spec.Replicas != int32(resolveProfileWarmReplicas(agent)) || read.Spec.Template.Spec.ServiceAccountName != "agentprofile-auditor-read" {
		t.Errorf("the read pool: replicas %d, serviceAccount %q", *read.Spec.Replicas, read.Spec.Template.Spec.ServiceAccountName)
	}
	if err := pool("unattended"); !apierrors.IsNotFound(err) {
		t.Errorf("an unattended tier got a warm pool: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aNATSCABundleName(agent)}, &corev1.ConfigMap{}); err != nil {
		t.Errorf("the CA bundle wasn't copied into the profile namespace: %v", err)
	}

	unattendedOnly := profile.DeepCopy()
	unattendedOnly.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierUnattended}
	reconcile(*unattendedOnly)
	if err := pool("read"); !apierrors.IsNotFound(err) {
		t.Errorf("a dropped tier's pool survived: %v", err)
	}

	reconcile(profile)
	t.Setenv(a2aDispatcherImageEnvVar, "")
	reconcile(profile)
	if err := pool("read"); !apierrors.IsNotFound(err) {
		t.Errorf("a pool survived the dispatcher being turned off: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aNATSCABundleName(agent)}, &corev1.ConfigMap{}); !apierrors.IsNotFound(err) {
		t.Errorf("the profile namespace's CA bundle survived the dispatcher being turned off: %v", err)
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(foreign), &appsv1.Deployment{}); err != nil {
		t.Errorf("a foreign pool was deleted: %v", err)
	}
}

func initMounts(spec corev1.PodSpec) []corev1.VolumeMount {
	var out []corev1.VolumeMount
	for _, c := range spec.InitContainers {
		out = append(out, c.VolumeMounts...)
	}
	return out
}

// One pool the API server refuses doesn't hold up the others, or the sweep
// of a removed profile's pool; and a failure deleting the dispatcher's RBAC
// doesn't keep the pools, which nothing else would reclaim, from going.
func TestOnePoolFailureDoesntStrandTheRest(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	pns := profileNamespaceName(agent)
	bad := testAgentProfile(agent.Namespace, "bad")
	good := testAgentProfile(agent.Namespace, "good")
	gone := testAgentProfile(agent.Namespace, "gone")

	scheme := setupScheme()
	bundle := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aNATSCACertKey: "pem"},
	}
	funcs := fakeServerSideApplyInterceptors()
	apply := funcs.Patch
	funcs.Patch = func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
		if obj.GetName() == profilePoolName("bad", "read") {
			return apierrors.NewInvalid(appsv1.SchemeGroupVersion.WithKind("Deployment").GroupKind(), obj.GetName(), nil)
		}
		return apply(ctx, cl, obj, patch, opts...)
	}
	failRoleDelete := false
	funcs.Delete = func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
		if _, isRole := obj.(*rbacv1.Role); isRole && failRoleDelete {
			return apierrors.NewServiceUnavailable("test")
		}
		return cl.Delete(ctx, obj, opts...)
	}
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(agent, chartProfileNamespace(agent), bundle).WithInterceptorFuncs(funcs).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	pool := func(name string) error {
		return c.Get(ctx, types.NamespacedName{Namespace: pns, Name: profilePoolName(name, "read")}, &appsv1.Deployment{})
	}

	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{good, gone}, true); err != nil || profileErr != nil {
		t.Fatalf("setup: %v / %v", err, profileErr)
	}
	profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{bad, good}, true)
	if err != nil || profileErr == nil {
		t.Fatalf("a refused pool should surface as a profile-namespace error: %v / %v", err, profileErr)
	}
	if err := pool("good"); err != nil {
		t.Errorf("a refused pool held up another profile's: %v", err)
	}
	if err := pool("gone"); !apierrors.IsNotFound(err) {
		t.Errorf("a refused pool held up the sweep of a removed profile's: %v", err)
	}

	failRoleDelete = true
	if err := r.deleteDispatcherPodsRBAC(ctx, agent); err == nil {
		t.Error("the RBAC delete failure wasn't reported")
	}
	if err := pool("good"); !apierrors.IsNotFound(err) {
		t.Errorf("a pool survived teardown because the RBAC delete failed: %v", err)
	}
}
