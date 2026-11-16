package controller

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The bridge is in the agent pod from the first render (#5962), and it reads
// its bus password and the activity-hook key from the bus creds Secret
// through SecretKeyRef; the password ref is not optional. A Deployment
// applied before that Secret exists starts a pod the kubelet holds in
// CreateContainerConfigError, and the pod-template digest records the
// Secret as absent, so the next pass changes the template and rolls the
// agent again. Reconcile ensures the Secret before reconcileWorkload; this
// test checks, at the moment each apply of the agent Deployment reaches the
// API server, that every non-optional Secret ref in its template already
// resolves, and that the template is the same on every later pass.
func TestTheAgentDeploymentIsAppliedOnlyOnceItsSecretRefsResolve(t *testing.T) {
	t.Run("the rendered bridge", func(t *testing.T) {
		checkFirstRenderSecretRefs(t, secretHashTestAgent(), true)
	})
	// A CR that declares its own api bridge gets no rendered one, but the
	// agent still holds the activity-hook key from the creds Secret.
	t.Run("a declared api bridge", func(t *testing.T) {
		agent := secretHashTestAgent()
		if agent.Spec.Deployment == nil {
			agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{}
		}
		agent.Spec.Deployment.Sidecars = append(agent.Spec.Deployment.Sidecars, corev1.Container{
			Name: a2aBridgeContainerName, Image: "example.com/hermes-bridge:dev",
			Env: []corev1.EnvVar{{Name: "API_SERVER_KEY", Value: "k"}, {Name: "BRIDGE_EXECUTOR", Value: "api"}, {Name: "BRIDGE_CONCURRENCY", Value: "2"}},
		})
		checkFirstRenderSecretRefs(t, agent, false)
	})
}

func checkFirstRenderSecretRefs(t *testing.T, agent *agentv1alpha1.PlatformAgent, rendered bool) {
	t.Helper()
	scheme := setupScheme()
	secret := secretHashTestSecret("platform-agent-secrets", map[string][]byte{
		"API_SERVER_KEY": []byte("k"), "SESSION_KV_API_KEY": []byte("s"), "SESSION_KV_SALT": []byte("t"),
	})
	gatewayKey := types.NamespacedName{Name: "test-agent-gateway", Namespace: "test-ns"}
	ssa := fakeServerSideApplyInterceptors()
	var applies int
	var cl client.WithWatch
	cl = fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, secret).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{
			Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
				if dep, ok := obj.(*appsv1.Deployment); ok && client.ObjectKeyFromObject(dep) == gatewayKey {
					applies++
					for _, ref := range unresolvedSecretRefs(ctx, t, c, &dep.Spec.Template.Spec, dep.Namespace) {
						t.Errorf("apply %d of the agent Deployment references %s before it exists; the pod would sit in CreateContainerConfigError", applies, ref)
					}
				}
				return ssa.Patch(ctx, c, obj, patch, opts...)
			},
		}).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: "test-agent", Namespace: "test-ns"}}
	ctx := context.Background()

	var first *corev1.PodTemplateSpec
	for pass := 1; pass <= 4; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", pass, err)
		}
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, gatewayKey, dep); err != nil {
			continue // the first pass only adds the finalizer
		}
		if len(containersNamed(dep.Spec.Template, a2aBridgeContainerName)) != 1 {
			t.Fatalf("pass %d: the agent Deployment has no %s container", pass, a2aBridgeContainerName)
		}
		if !hasEnv(dep.Spec.Template.Spec.Containers[0].Env, a2aActivitySecretEnvVar) {
			t.Fatalf("pass %d: the agent holds no %s, so this case reads nothing from the creds Secret (rendered=%v)", pass, a2aActivitySecretEnvVar, rendered)
		}
		if first == nil {
			first = dep.Spec.Template.DeepCopy()
			continue
		}
		if !equalTemplates(first, &dep.Spec.Template) {
			t.Errorf("pass %d changed the agent pod template from the first render; a fresh install would roll it again", pass)
		}
	}
	if applies == 0 || first == nil {
		t.Fatal("the agent Deployment was never applied")
	}
}

// unresolvedSecretRefs lists the non-optional SecretKeyRefs in spec whose
// Secret or key does not exist yet.
func unresolvedSecretRefs(ctx context.Context, t *testing.T, c client.Client, spec *corev1.PodSpec, ns string) []string {
	t.Helper()
	var out []string
	for _, containers := range [][]corev1.Container{spec.InitContainers, spec.Containers} {
		for _, ctr := range containers {
			for _, env := range ctr.Env {
				ref := env.ValueFrom
				if ref == nil || ref.SecretKeyRef == nil || (ref.SecretKeyRef.Optional != nil && *ref.SecretKeyRef.Optional) {
					continue
				}
				s := &corev1.Secret{}
				if err := c.Get(ctx, types.NamespacedName{Name: ref.SecretKeyRef.Name, Namespace: ns}, s); err != nil {
					out = append(out, ctr.Name+": "+ref.SecretKeyRef.Name+"/"+ref.SecretKeyRef.Key)
					continue
				}
				if _, ok := s.Data[ref.SecretKeyRef.Key]; !ok {
					out = append(out, ctr.Name+": "+ref.SecretKeyRef.Name+"/"+ref.SecretKeyRef.Key+" (key)")
				}
			}
		}
	}
	return out
}

func equalTemplates(a, b *corev1.PodTemplateSpec) bool {
	return apiequality.Semantic.DeepEqual(a, b)
}
