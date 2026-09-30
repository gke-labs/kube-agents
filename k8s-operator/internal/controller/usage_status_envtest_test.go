package controller

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	platformAgentCRDFile = "kubeagents.x-k8s.io_platformagents.yaml"
	platformAgentCRDName = "platformagents.kubeagents.x-k8s.io"
	// crdSettleTimeout bounds how long the API server may take to serve an
	// updated CRD schema; it is usually well under a second.
	crdSettleTimeout = 30 * time.Second
	crdSettlePoll    = 500 * time.Millisecond
)

// platformAgentCRDWithoutUsage is this release's CRD with status.usage removed
// from every version's schema: the CRD an install serves while its operator
// runs ahead of it.
func platformAgentCRDWithoutUsage(t *testing.T) (pruned []byte, full map[string]any) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(envtestCRDDir, platformAgentCRDFile))
	if err != nil {
		t.Fatalf("reading the CRD: %v", err)
	}
	if err := yaml.Unmarshal(raw, &full); err != nil {
		t.Fatalf("parsing the CRD: %v", err)
	}
	var doc map[string]any
	if err := yaml.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parsing the CRD: %v", err)
	}
	removed := 0
	for _, version := range doc["spec"].(map[string]any)["versions"].([]any) {
		status := version.(map[string]any)["schema"].(map[string]any)["openAPIV3Schema"].(map[string]any)["properties"].(map[string]any)["status"].(map[string]any)["properties"].(map[string]any)
		if _, ok := status["usage"]; ok {
			delete(status, "usage")
			removed++
		}
	}
	if removed == 0 {
		t.Fatal("the CRD has no status.usage to remove; the fixture would not be a skew")
	}
	pruned, err = yaml.Marshal(doc)
	if err != nil {
		t.Fatalf("serialising the pruned CRD: %v", err)
	}
	return pruned, full
}

// TestAPrunedUsageStatusOnAServedCRDEnvtest is the skew against a real API
// server, which is the only place the pruning and the write's echo are real:
// the fake blanks the field on the request object, and the unit test would stay
// green if controller-runtime stopped zeroing the target before decoding the
// response. Serve this release's CRD without status.usage, write status three
// times and see one write; then apply the full CRD, let the record expire, and
// see the field land on the next pass and the writer go quiet again.
func TestAPrunedUsageStatusOnAServedCRDEnvtest(t *testing.T) {
	pruned, full := platformAgentCRDWithoutUsage(t)
	crdDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(crdDir, platformAgentCRDFile), pruned, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, scheme := startEnvtestConfigWithCRDs(t, crdDir)
	direct, err := client.NewWithWatch(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("envtest client: %v", err)
	}
	writes := 0
	cl := interceptor.NewClient(direct, interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			writes++
			return c.SubResource(subResourceName).Update(ctx, obj, opts...)
		},
	})
	ctx := context.Background()
	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: envtestNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: envtestNamespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
		},
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating PlatformAgent: %v", err)
	}
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	settle := func(pass string) {
		t.Helper()
		if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aProvisionState{}); err != nil {
			t.Fatalf("updateStatusReady (%s): %v", pass, err)
		}
	}

	settle("first")
	if len(agent.Status.Usage.ActiveInterfaces) != 0 {
		t.Fatalf("the served CRD kept status.usage (%v); the fixture is not a skew", agent.Status.Usage.ActiveInterfaces)
	}
	settle("second")
	settle("third")
	if writes != 1 {
		t.Fatalf("%d status writes across three passes under a CRD without status.usage, want 1: this is the write-every-pass loop", writes)
	}

	// Apply this release's CRD, as the log line asks.
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"})
	if err := cl.Get(ctx, client.ObjectKey{Name: platformAgentCRDName}, live); err != nil {
		t.Fatalf("reading the served CRD: %v", err)
	}
	live.Object["spec"] = full["spec"]
	if err := cl.Update(ctx, live); err != nil {
		t.Fatalf("applying the full CRD: %v", err)
	}

	// The record holds until it expires; expire it and the next pass lands the
	// field, once the API server serves the new schema.
	key := client.ObjectKeyFromObject(agent)
	deadline := time.Now().Add(crdSettleTimeout)
	for !slices.Equal(agent.Status.Usage.ActiveInterfaces, []string{"dashboard"}) {
		if time.Now().After(deadline) {
			t.Fatalf("status.usage.activeInterfaces never landed after the CRD was applied: %v", agent.Status.Usage.ActiveInterfaces)
		}
		r.prunedUsageStatus.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
		settle("after the CRD")
		if !slices.Equal(agent.Status.Usage.ActiveInterfaces, []string{"dashboard"}) {
			time.Sleep(crdSettlePoll)
		}
	}
	if r.usageStatusPruned(agent) {
		t.Error("the echo carried the field and the record was not cleared")
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, key, stored); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(stored.Status.Usage.ActiveInterfaces, []string{"dashboard"}) {
		t.Errorf("a fresh read shows activeInterfaces=%v, want [dashboard]", stored.Status.Usage.ActiveInterfaces)
	}
	before := writes
	settle("quiet 1")
	settle("quiet 2")
	if writes != before {
		t.Errorf("%d writes across two unchanged passes under the applied CRD, want 0", writes-before)
	}
}
