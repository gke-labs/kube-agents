package controller

import (
	"context"
	"reflect"
	"strings"
	"testing"

	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// kube-proxy reads the pod a TokenReview names, to hold the request to it
// (podBinder in k8s-api-proxy/pkg/tokenreview). This grant is `get` on pods
// and nothing else, as a Role in the agent's namespace, bound to kube-proxy's
// own ServiceAccount.
func TestKubeProxyPodReadRoleIsGetOnPodsAlone(t *testing.T) {
	agent := a2aTestAgent()
	role := buildKubeProxyPodReadRole(agent)
	want := []rbacv1.PolicyRule{{APIGroups: []string{""}, Resources: []string{"pods"}, Verbs: []string{"get"}}}
	if !reflect.DeepEqual(role.Rules, want) {
		t.Errorf("kube-proxy's pod-read Role grants %+v, want %+v", role.Rules, want)
	}
	if role.Kind != "Role" || role.Namespace != agent.Namespace || role.Name != agent.Name+"-kube-proxy-pod-read" {
		t.Errorf("rendered as %s %s/%s", role.Kind, role.Namespace, role.Name)
	}
	binding := buildKubeProxyPodReadRoleBinding(agent)
	if binding.Namespace != agent.Namespace || binding.RoleRef.Kind != "Role" || binding.RoleRef.Name != role.Name {
		t.Errorf("the binding refers to %+v in %q, want Role %s", binding.RoleRef, binding.Namespace, role.Name)
	}
	wantSubject := []rbacv1.Subject{{Kind: "ServiceAccount", Name: agentServiceAccountName(agent), Namespace: agent.Namespace}}
	if !reflect.DeepEqual(binding.Subjects, wantSubject) {
		t.Errorf("the binding's subjects are %+v, want only kube-proxy's ServiceAccount %+v", binding.Subjects, wantSubject)
	}
	// The ServiceAccount the binding names is the one kube-proxy runs as, and it
	// follows spec.security.serviceAccountName.
	agent.Spec.Security = &agentv1alpha1.SecuritySpec{ServiceAccountName: "custom-sa"}
	dep := buildKubeProxyDeployment(agent, "", true, "")
	if got, sa := buildKubeProxyPodReadRoleBinding(agent).Subjects[0].Name, dep.Spec.Template.Spec.ServiceAccountName; got != sa || got != "custom-sa" {
		t.Errorf("the binding names %q, kube-proxy runs as %q; want both custom-sa", got, sa)
	}
}

// The Role covers every namespace whose pods call kube-proxy: the
// NetworkPolicy admits callers by pod selector alone (so only from the agent's
// namespace), and every caller ServiceAccount the Deployment admits lives
// there. A caller added in another namespace needs a Role there too.
func TestKubeProxyCallersAreAllInTheAgentNamespace(t *testing.T) {
	t.Setenv(a2aSessionClusterViewEnvVar, "true")
	agent := a2aTestAgent()
	np := buildKubeProxyNetworkPolicy(agent)
	for _, peer := range np.Spec.Ingress[0].From {
		if peer.NamespaceSelector != nil || peer.IPBlock != nil || peer.PodSelector == nil {
			t.Errorf("kube-proxy admits a caller outside the agent's namespace or by address: %+v", peer)
		}
	}
	dep := buildKubeProxyDeployment(agent, "", true, "")
	prefix := "system:serviceaccount:" + agent.Namespace + ":"
	for _, env := range dep.Spec.Template.Spec.Containers[0].Env {
		if env.Name != "CREDENTIAL_PROXY_ALLOWED_CALLERS" && env.Name != "CREDENTIAL_PROXY_SESSION_CALLERS" {
			continue
		}
		for _, caller := range strings.Split(env.Value, ",") {
			if !strings.HasPrefix(caller, prefix) {
				t.Errorf("%s names %q, outside the agent's namespace", env.Name, caller)
			}
		}
	}
}

// Applied with the rest of kube-proxy, owned by the agent, and spared by the
// RBAC sweep for an agent whose name carries the prefix it reaps.
func TestKubeProxyPodReadRBACRendersAndSurvivesTheSweep(t *testing.T) {
	ctx := context.Background()
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Name = "kubeagents-prod"
	agent.UID = "agent-uid"
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	if err := r.reconcileKubeProxy(ctx, agent, "", true); err != nil {
		t.Fatal(err)
	}
	key := types.NamespacedName{Namespace: agent.Namespace, Name: kubeProxyPodReadName(agent)}
	for _, obj := range []client.Object{&rbacv1.Role{}, &rbacv1.RoleBinding{}} {
		if err := cl.Get(ctx, key, obj); err != nil {
			t.Fatalf("kube-proxy's pod-read %T: %v", obj, err)
		}
		if !metav1.IsControlledBy(obj, agent) {
			t.Errorf("kube-proxy's pod-read %T is not owned by the agent", obj)
		}
	}
	if err := r.cleanupAgentRBAC(ctx, agent, false); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &rbacv1.RoleBinding{}); err != nil {
		t.Errorf("the RBAC sweep removed kube-proxy's pod-read RoleBinding: %v", err)
	}
}

// The rendered kube-proxy authenticates callers with token-review, the binary's default
// (KUBE_PROXY_AUTH_MODE unset, no command or args to pass --auth-mode), with a caller
// allowlist, which the binary refuses to start with under --auth-mode=none. Nothing the
// operator renders turns caller authentication off.
func TestKubeProxyRendersTokenReviewAuthentication(t *testing.T) {
	for _, sessionView := range []string{"true", "false"} {
		t.Run("session view "+sessionView, func(t *testing.T) {
			t.Setenv(a2aSessionClusterViewEnvVar, sessionView)
			dep := buildKubeProxyDeployment(a2aTestAgent(), "", true, "")
			c := dep.Spec.Template.Spec.Containers[0]
			if len(c.Command) > 0 || len(c.Args) > 0 {
				t.Errorf("kube-proxy renders command %q args %q; the auth mode must stay the binary's default", c.Command, c.Args)
			}
			allowed := ""
			for _, env := range c.Env {
				switch env.Name {
				case "KUBE_PROXY_AUTH_MODE", "KUBE_PROXY_JWT_ISSUER":
					t.Errorf("kube-proxy renders %s=%q", env.Name, env.Value)
				case "CREDENTIAL_PROXY_ALLOWED_CALLERS":
					allowed = env.Value
				}
				if env.ValueFrom != nil && env.Name == "KUBE_PROXY_AUTH_MODE" {
					t.Errorf("kube-proxy reads KUBE_PROXY_AUTH_MODE from %+v", env.ValueFrom)
				}
			}
			if allowed == "" {
				t.Error("kube-proxy renders no CREDENTIAL_PROXY_ALLOWED_CALLERS")
			}
			for _, from := range c.EnvFrom {
				t.Errorf("kube-proxy takes environment from %+v, which could set its auth mode", from)
			}
		})
	}
}
