package controller

import (
	"context"
	"fmt"
	"k8s.io/apimachinery/pkg/api/resource"
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
	// The root filesystem is read-only, so every place the worker and its
	// harness write is an emptyDir: the working directory, $HOME, /tmp, the
	// harness config root and the task token.
	if sc := c.SecurityContext; sc == nil || sc.ReadOnlyRootFilesystem == nil || !*sc.ReadOnlyRootFilesystem {
		t.Errorf("the worker container's root filesystem is writable: %+v", c.SecurityContext)
	}
	emptyDirs := map[string]bool{}
	for _, v := range spec.Volumes {
		if v.EmptyDir != nil {
			emptyDirs[v.Name] = true
		}
	}
	for _, path := range []string{env["A2A_WORKDIR"], env["HOME"], "/tmp", env["A2A_PROFILE_CONFIG_ROOT"], strings.TrimSuffix(env["CREDENTIAL_PROXY_TASK_TOKEN_FILE"], "/token")} {
		var writable bool
		for _, m := range c.VolumeMounts {
			if m.MountPath == path && !m.ReadOnly && emptyDirs[m.Name] {
				writable = true
			}
		}
		if path == "" || !writable {
			t.Errorf("%q is not a writable emptyDir mount under the read-only root", path)
		}
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

// A profile with tools has PATH rebuilt with the tools' bin directories in
// front, and the credential proxy's shims stay ahead of the system
// directories, so kubectl and gcloud still go through the broker.
func TestAToolEquippedWorkerKeepsTheBrokerShimsOnPath(t *testing.T) {
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Tools = []agentv1alpha1.AgentProfileTool{{Name: "jq", Image: "registry.example/jq:1"}}
	})
	spec := buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", profileWorkerPool, false)
	path := envOf(spec.Containers[0])["PATH"]
	dirs := strings.Split(path, ":")
	shims, system := -1, -1
	for i, d := range dirs {
		if d == "/opt/credential-proxy/bin" && shims < 0 {
			shims = i
		}
		if d == "/usr/bin" && system < 0 {
			system = i
		}
	}
	if !strings.Contains(path, "jq") {
		t.Fatalf("PATH %q has no tool directory; the profile's tools aren't on it", path)
	}
	if shims < 0 || system < 0 || shims > system {
		t.Errorf("PATH %q: the credential proxy's shims must come before the system directories", path)
	}
}

// The sweep of removed profiles' pools runs past failures: a CA bundle copy
// the profile namespace refuses doesn't skip it, and one pool whose delete
// is refused doesn't keep the others running.
func TestTheSweepRunsPastFailures(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	pns := profileNamespaceName(agent)
	scheme := setupScheme()
	bundle := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aNATSCACertKey: "pem"},
	}
	funcs := fakeServerSideApplyInterceptors()
	apply := funcs.Patch
	failBundle := false
	funcs.Patch = func(ctx context.Context, cl client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
		if failBundle && obj.GetNamespace() == pns && obj.GetName() == a2aNATSCABundleName(agent) {
			return apierrors.NewForbidden(corev1.Resource("configmaps"), obj.GetName(), nil)
		}
		return apply(ctx, cl, obj, patch, opts...)
	}
	funcs.Delete = func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
		if obj.GetName() == profilePoolName("b", "read") {
			return apierrors.NewForbidden(appsv1.Resource("deployments"), obj.GetName(), nil)
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
	profiles := func(names ...string) []agentv1alpha1.AgentProfile {
		var out []agentv1alpha1.AgentProfile
		for _, n := range names {
			out = append(out, testAgentProfile(agent.Namespace, n))
		}
		return out
	}

	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles("a", "b", "c", "keep"), true); err != nil || profileErr != nil {
		t.Fatalf("setup: %v / %v", err, profileErr)
	}
	for _, n := range []string{"a", "b", "c", "keep"} {
		if err := pool(n); err != nil {
			t.Fatalf("setup: pool %s: %v", n, err)
		}
	}
	// a, b and c are removed; the bundle copy is now refused, and so is b's
	// delete.
	failBundle = true
	profileErr, err := r.reconcileA2ADispatcher(ctx, agent, profiles("keep"), true)
	if err == nil && profileErr == nil {
		t.Error("the refused bundle copy and pool delete weren't reported")
	}
	for _, n := range []string{"a", "c"} {
		if err := pool(n); !apierrors.IsNotFound(err) {
			t.Errorf("removed profile %s's pool survived the sweep: %v", n, err)
		}
	}
	if err := pool("b"); err != nil {
		t.Errorf("the pool whose delete was refused is gone anyway (the test's premise): %v", err)
	}
}

// Every mount of the worker is either read-only or on the list the worker
// empties after each task, for a plain profile and one with tools, so a
// writable mount can't slip past the reset. Everything on the list is a
// writable emptyDir, so the reset never meets a directory it can't empty.
func TestEveryWritableWorkerMountIsResetBetweenTasks(t *testing.T) {
	agent := a2aTestAgent()
	for name, mutate := range map[string]func(*agentv1alpha1.AgentProfile){
		"plain": func(*agentv1alpha1.AgentProfile) {},
		"with tools": func(p *agentv1alpha1.AgentProfile) {
			p.Spec.Tools = []agentv1alpha1.AgentProfileTool{{Name: "jq", Image: "registry.example/jq:1"}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			profile := testAgentProfile(agent.Namespace, "auditor", mutate)
			spec := buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", profileWorkerPool, false)
			c := spec.Containers[0]
			reset := map[string]bool{}
			for _, p := range strings.Split(envOf(c)[profileWorkerResetPathsEnv], ":") {
				if p != "" {
					reset[p] = true
				}
			}
			emptyDirs := map[string]bool{}
			for _, v := range spec.Volumes {
				if v.EmptyDir != nil {
					emptyDirs[v.Name] = true
				}
			}
			listed := map[string]bool{}
			for _, m := range c.VolumeMounts {
				switch {
				case reset[m.MountPath]:
					listed[m.MountPath] = true
					if m.ReadOnly || !emptyDirs[m.Name] {
						t.Errorf("%s is on the reset list but isn't a writable emptyDir", m.MountPath)
					}
				case !m.ReadOnly:
					t.Errorf("%s is writable and not on the reset list (%s): a task's files there would reach the next task", m.MountPath, envOf(c)[profileWorkerResetPathsEnv])
				}
			}
			for p := range reset {
				if !listed[p] {
					t.Errorf("the reset list names %s, which the worker doesn't mount", p)
				}
			}
			for _, want := range []string{profileWorkerScratchPath, profileWorkerHomePath, profileWorkerTmpPath, profileWorkerConfigPath, profileWorkerTaskTokenPath} {
				if !reset[want] {
					t.Errorf("%s isn't reset between tasks", want)
				}
			}
		})
	}
}

// The worker asks for the ephemeral storage its pod's disk-backed emptyDirs
// can hold, plus the logs allowance, with the limit equal to the request, so
// Autopilot's 1Gi default doesn't cap it below its own volumes. With image
// volumes off, the staged persona and tools are counted too.
func TestTheWorkerRequestsTheEphemeralStorageItsVolumesHold(t *testing.T) {
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Tools = []agentv1alpha1.AgentProfileTool{{Name: "jq", Image: "registry.example/jq:1"}}
	})
	for _, imageVolumes := range []bool{true, false} {
		spec := buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", profileWorkerPool, imageVolumes)
		want := resource.MustParse(emptyDirLogsAllowance)
		for _, v := range spec.Volumes {
			if v.EmptyDir != nil && v.EmptyDir.Medium != corev1.StorageMediumMemory && v.EmptyDir.SizeLimit != nil {
				want.Add(*v.EmptyDir.SizeLimit)
			}
		}
		r := spec.Containers[0].Resources
		req, lim := r.Requests[corev1.ResourceEphemeralStorage], r.Limits[corev1.ResourceEphemeralStorage]
		if req.Cmp(want) != 0 || lim.Cmp(want) != 0 {
			t.Errorf("imageVolumes=%v: ephemeral-storage request %s limit %s, want both %s", imageVolumes, req.String(), lim.String(), want.String())
		}
		if floor := resource.MustParse("4Gi"); req.Cmp(floor) < 0 {
			t.Errorf("imageVolumes=%v: ephemeral-storage %s is under the writable volumes alone (4Gi+)", imageVolumes, req.String())
		}
	}
}

// Turning the dispatcher off removes the pools first: a refused delete of
// the dispatcher's own Deployment doesn't keep them running.
func TestGateOffRemovesThePoolsWhateverElseFails(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	pns := profileNamespaceName(agent)
	scheme := setupScheme()
	bundle := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSCABundleName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aNATSCACertKey: "pem"},
	}
	funcs := fakeServerSideApplyInterceptors()
	funcs.Delete = func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
		if _, isDep := obj.(*appsv1.Deployment); isDep && obj.GetName() == a2aDispatcherName(agent) {
			return apierrors.NewForbidden(appsv1.Resource("deployments"), obj.GetName(), nil)
		}
		return cl.Delete(ctx, obj, opts...)
	}
	c := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(agent, chartProfileNamespace(agent), bundle).WithInterceptorFuncs(funcs).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	p := testAgentProfile(agent.Namespace, "auditor")
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{p}, true); err != nil || profileErr != nil {
		t.Fatalf("setup: %v / %v", err, profileErr)
	}
	poolKey := types.NamespacedName{Namespace: pns, Name: profilePoolName("auditor", "read")}
	if err := c.Get(ctx, poolKey, &appsv1.Deployment{}); err != nil {
		t.Fatalf("setup: no pool: %v", err)
	}

	t.Setenv(a2aDispatcherImageEnvVar, "")
	if _, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{p}, true); err == nil {
		t.Error("the refused dispatcher delete wasn't reported")
	}
	if err := c.Get(ctx, poolKey, &appsv1.Deployment{}); !apierrors.IsNotFound(err) {
		t.Errorf("the pool survived the dispatcher being turned off: %v", err)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: a2aNATSCABundleName(agent)}, &corev1.ConfigMap{}); !apierrors.IsNotFound(err) {
		t.Errorf("the bundle copy survived the dispatcher being turned off: %v", err)
	}
}

// With image volumes off (Autopilot's case) each tool is staged into its own
// emptyDir, so the worker's ephemeral request grows with the tools. Up to the
// documented limit it stays inside Autopilot's 10Gi per pod, so a profile at
// the limit still schedules.
func TestTheWorkerFitsAutopilotUpToTheToolLimit(t *testing.T) {
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		for i := 0; i < profileWorkerAutopilotToolLimit; i++ {
			p.Spec.Tools = append(p.Spec.Tools, agentv1alpha1.AgentProfileTool{Name: fmt.Sprintf("tool%d", i), Image: fmt.Sprintf("registry.example/tool%d:1", i)})
		}
	})
	spec := buildProfileWorkerPodSpec(agent, &profile, "read", "agentprofile-auditor-read", profileWorkerPool, false)
	req := spec.Containers[0].Resources.Requests[corev1.ResourceEphemeralStorage]
	if ceiling := resource.MustParse("10Gi"); req.Cmp(ceiling) > 0 {
		t.Errorf("with %d tools the worker requests %s of ephemeral storage, past Autopilot's 10Gi per pod", profileWorkerAutopilotToolLimit, req.String())
	}
}

// Past the tool limit with image volumes off, the pool isn't applied, the
// reconcile reports the profile as a profile-namespace error, and the other
// profiles' pools still go out. With image volumes on, the same profile gets
// its pool.
func TestAProfilePastTheToolLimitGetsNoPoolWithoutImageVolumes(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	pns := profileNamespaceName(agent)
	big := testAgentProfile(agent.Namespace, "big", func(p *agentv1alpha1.AgentProfile) {
		for i := 0; i <= profileWorkerAutopilotToolLimit; i++ {
			p.Spec.Tools = append(p.Spec.Tools, agentv1alpha1.AgentProfileTool{Name: fmt.Sprintf("tool%d", i), Image: fmt.Sprintf("registry.example/tool%d:1", i)})
		}
	})
	small := testAgentProfile(agent.Namespace, "small")
	r, c := poolTestReconciler(t, agent)
	ctx := context.Background()
	pool := func(name string) error {
		return c.Get(ctx, types.NamespacedName{Namespace: pns, Name: profilePoolName(name, "read")}, &appsv1.Deployment{})
	}

	agent.Annotations = map[string]string{"kubeagents.x-k8s.io/enable-image-volumes": "false"}
	profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{big, small}, true)
	if err != nil || profileErr == nil {
		t.Fatalf("a profile past the tool limit should surface as a profile-namespace error: %v / %v", err, profileErr)
	}
	if want := fmt.Sprintf("AgentProfile big has %d tools", profileWorkerAutopilotToolLimit+1); !strings.Contains(profileErr.Error(), want) {
		t.Errorf("the error %q doesn't name the profile and its tools (%q)", profileErr, want)
	}
	if err := pool("big"); !apierrors.IsNotFound(err) {
		t.Errorf("a profile past the tool limit got a pool without image volumes: %v", err)
	}
	if err := pool("small"); err != nil {
		t.Errorf("a profile past the tool limit held up another profile's pool: %v", err)
	}

	agent.Annotations["kubeagents.x-k8s.io/enable-image-volumes"] = "true"
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, []agentv1alpha1.AgentProfile{big, small}, true); err != nil || profileErr != nil {
		t.Fatalf("with image volumes: %v / %v", err, profileErr)
	}
	if err := pool("big"); err != nil {
		t.Errorf("with image volumes the profile got no pool: %v", err)
	}
}
