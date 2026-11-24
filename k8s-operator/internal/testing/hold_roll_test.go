package testing

import (
	"context"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/controller"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/testing/testutil"
)

// Setting and lifting spec.hold rolls nothing: every Deployment's and
// StatefulSet's pod template, its hash annotations included, and the
// <agent>-config ConfigMap are byte-identical before, during and after, and
// only the hold ConfigMap changes. One fake cluster throughout, so the
// generated TLS and bus secrets (and the hashes over them) stay put. The
// scope-less fixture is the case a hold under spec.scope broke: lifting it
// left a defaulted scope block behind, which rolled the shell sandbox.
func TestAHoldRollsNoPod(t *testing.T) {
	t.Parallel()
	for _, input := range []string{
		filepath.Join("..", "..", "examples", "platformagent.yaml"),
		filepath.Join("testdata", "platform", "platformagent-scope.yaml"),
	} {
		t.Run(filepath.Base(input), func(t *testing.T) {
			t.Parallel()
			data, err := os.ReadFile(input)
			if err != nil {
				t.Fatal(err)
			}
			agent := &agentv1alpha1.PlatformAgent{}
			if err := yaml.Unmarshal(data, agent); err != nil {
				t.Fatal(err)
			}
			scheme := newTestScheme()
			c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
			// The reconciler goes through the golden tests' TrackingClient, which turns
			// a server-side apply into the create or update the fake client can do.
			r := &controller.PlatformAgentReconciler{Client: testutil.NewTrackingClient(c, scheme, agent), Scheme: scheme, OperatorNamespace: goldenOperatorNamespace}
			key := types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}
			ctx := context.Background()

			reconcileAll := func() {
				t.Helper()
				for i := 0; i < 5; i++ {
					if _, err := r.Reconcile(ctx, reconcile.Request{NamespacedName: key}); err != nil {
						t.Fatal(err)
					}
				}
			}
			setHold := func(hold *agentv1alpha1.HoldSpec) {
				t.Helper()
				live := &agentv1alpha1.PlatformAgent{}
				if err := c.Get(ctx, key, live); err != nil {
					t.Fatal(err)
				}
				live.Spec.Hold = hold
				if err := c.Update(ctx, live); err != nil {
					t.Fatal(err)
				}
			}

			reconcileAll()
			before := podTemplates(t, c)
			configBefore := configMapData(t, c, agent.Name+"-config", agent.Namespace)
			if len(before) == 0 {
				t.Fatal("the render has no Deployment or StatefulSet, so this test proves nothing")
			}
			if got := holdFile(t, c, agent); got != "{}\n" {
				t.Fatalf("hold.json before any hold = %q, want {}", got)
			}

			setHold(&agentv1alpha1.HoldSpec{
				Clusters: []agentv1alpha1.ScopeClusterRef{{ProjectID: "payments-prod", Location: "us-central1", ClusterName: "prod-1"}},
				Reason:   "INC-42",
			})
			reconcileAll()
			if got := holdFile(t, c, agent); got == "{}\n" {
				t.Fatal("hold.json did not change with the hold")
			}
			assertSameTemplates(t, before, podTemplates(t, c), "setting a hold")
			if !reflect.DeepEqual(configBefore, configMapData(t, c, agent.Name+"-config", agent.Namespace)) {
				t.Error("setting a hold changed the <agent>-config ConfigMap, which the agent pod's config-hash covers")
			}

			setHold(nil)
			reconcileAll()
			if got := holdFile(t, c, agent); got != "{}\n" {
				t.Errorf("hold.json after the hold is lifted = %q, want {}", got)
			}
			assertSameTemplates(t, before, podTemplates(t, c), "lifting a hold")
			if !reflect.DeepEqual(configBefore, configMapData(t, c, agent.Name+"-config", agent.Namespace)) {
				t.Error("lifting a hold left the <agent>-config ConfigMap different from before the hold")
			}
		})
	}
}

func podTemplates(t *testing.T, c client.Client) map[string]corev1.PodTemplateSpec {
	t.Helper()
	out := map[string]corev1.PodTemplateSpec{}
	var deps appsv1.DeploymentList
	if err := c.List(context.Background(), &deps); err != nil {
		t.Fatal(err)
	}
	for _, d := range deps.Items {
		out["Deployment/"+d.Name] = d.Spec.Template
	}
	var sets appsv1.StatefulSetList
	if err := c.List(context.Background(), &sets); err != nil {
		t.Fatal(err)
	}
	for _, s := range sets.Items {
		out["StatefulSet/"+s.Name] = s.Spec.Template
	}
	return out
}

func assertSameTemplates(t *testing.T, before, after map[string]corev1.PodTemplateSpec, what string) {
	t.Helper()
	if len(before) != len(after) {
		t.Errorf("%s: %d workloads before, %d after", what, len(before), len(after))
	}
	for name, tmpl := range before {
		if !reflect.DeepEqual(tmpl, after[name]) {
			a, _ := yaml.Marshal(tmpl)
			b, _ := yaml.Marshal(after[name])
			t.Errorf("%s changed %s's pod template, which rolls it\nbefore:\n%s\nafter:\n%s", what, name, a, b)
		}
	}
}

func holdFile(t *testing.T, c client.Client, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	return configMapData(t, c, agent.Name+"-hold", agent.Namespace)["hold.json"]
}

func configMapData(t *testing.T, c client.Client, name, namespace string) map[string]string {
	t.Helper()
	cm := &corev1.ConfigMap{}
	if err := c.Get(context.Background(), types.NamespacedName{Name: name, Namespace: namespace}, cm); err != nil {
		t.Fatal(err)
	}
	return cm.Data
}

// The hold ConfigMap is written before any step that can park the reconcile:
// a CR the operator refuses to render (an override naming one of its own
// ServiceAccounts, which returns Degraded before any workload) still gets its
// hold, so an unrelated fault never delays the kill switch.
func TestAHoldIsWrittenWhenTheReconcileParksEarly(t *testing.T) {
	t.Parallel()
	data, err := os.ReadFile(filepath.Join("..", "..", "examples", "platformagent.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	agent := &agentv1alpha1.PlatformAgent{}
	if err := yaml.Unmarshal(data, agent); err != nil {
		t.Fatal(err)
	}
	if agent.Spec.Security == nil {
		agent.Spec.Security = &agentv1alpha1.SecuritySpec{}
	}
	reserved := ""
	for _, suffix := range []string{"-gateway", "-shell", "-scope-publisher", "-kube-proxy", "-a2a-gateway"} {
		agent.Spec.Security.ServiceAccountName = agent.Name + suffix
		if controller.ReservedServiceAccountOverride(agent) != "" {
			reserved = agent.Spec.Security.ServiceAccountName
			break
		}
	}
	if reserved == "" {
		t.Fatal("no candidate name is one the operator reserves, so this test proves nothing")
	}
	agent.Spec.Hold = &agentv1alpha1.HoldSpec{All: true, Reason: "INC-7"}
	scheme := newTestScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
	r := &controller.PlatformAgentReconciler{Client: testutil.NewTrackingClient(c, scheme, agent), Scheme: scheme, OperatorNamespace: goldenOperatorNamespace}
	key := types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}
	for i := 0; i < 5; i++ {
		if _, err := r.Reconcile(context.Background(), reconcile.Request{NamespacedName: key}); err != nil {
			t.Fatal(err)
		}
	}
	if templates := podTemplates(t, c); len(templates) != 0 {
		t.Fatalf("the refused CR rendered workloads %v, so the reconcile did not park early and this test proves nothing", templates)
	}
	if got := holdFile(t, c, agent); got != `{"all":true,"reason":"INC-7"}`+"\n" {
		t.Errorf("hold.json = %q, want the hold", got)
	}
}
