package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// cfg renders one model_list entry. Rows concatenate entries; the test puts them under one
// model_list, and a row that is not a run of entries is passed as written.
func cfg(name, model string) string {
	return fmt.Sprintf("- model_name: %s\n  litellm_params:\n    model: %s\n", name, model)
}

func TestParseLiteLLMDefaultModel(t *testing.T) {
	cases := []struct {
		name, yaml, provider, model string
		ok                          bool
	}{
		{"plain", cfg("model-default", "gemini/gemini-3.5-flash"), "gemini", "gemini-3.5-flash", true},
		{"org prefix kept", cfg("model-default", "hosted_vllm/meta-llama/Llama-3.1-8B-Instruct"), "hosted_vllm", "meta-llama/Llama-3.1-8B-Instruct", true},
		{"vertex endpoint path", cfg("model-default", "vertex_ai/projects/acme-prod-123/locations/us-central1/endpoints/987"), "vertex_ai", "987", true},
		{"bedrock arn slash form", cfg("model-default", "bedrock/arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3"), "bedrock", "us.anthropic.claude-3", true},
		{"bedrock arn colon form", cfg("model-default", "bedrock/arn:aws:bedrock:us-east-1:123456789012:provisioned-model:abc"), "bedrock", "abc", true},
		{"bedrock routed form", cfg("model-default", "bedrock/converse/arn:aws:bedrock:us-east-1:123456789012:inference-profile/p1"), "bedrock", "p1", true},
		{"unqualified default beside a qualified entry", cfg("model-default", "gpt-4o") + cfg("other", "anthropic/claude-opus-5"), "", "", false},
		{"empty name", cfg("model-default", "gemini/"), "", "", false},
		{"empty provider", cfg("model-default", "/gpt-4o"), "", "", false},
		{"no model-default entry falls back to the first qualified", cfg("hermes-agent", "anthropic/claude-opus-5") + cfg("x", "openai/gpt-5.4"), "anthropic", "claude-opus-5", true},
		{"bare arn with a slash", cfg("model-default", "arn:aws:bedrock:us-east-1:123456789012:inference-profile/p1"), "", "", false},
		{"bare arn without a slash", cfg("model-default", "arn:aws:bedrock:us-east-1:123456789012:provisioned-model:abc"), "", "", false},
		{"bare projects path", cfg("model-default", "projects/acme-prod/locations/us-central1/endpoints/987"), "", "", false},
		{"bare arn skipped by the fallback", cfg("hermes-agent", "arn:aws:bedrock:us-east-1:123456789012:inference-profile/p1") + cfg("x", "projects/acme-prod/locations/us-central1/endpoints/987") + cfg("y", "openai/gpt-5.4"), "openai", "gpt-5.4", true},
		{"trailing slash after a name", cfg("model-default", "hosted_vllm/meta-llama/"), "", "", false},
		{"no qualified entry", cfg("hermes-agent", "gpt-4o"), "", "", false},
		{"no model_list", "litellm_settings: {}\n", "", "", false},
		{"not yaml", "{{{", "", "", false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			doc := c.yaml
			if strings.HasPrefix(doc, "- ") {
				doc = "model_list:\n" + doc
			}
			got, ok := parseLiteLLMDefaultModel(doc)
			if ok != c.ok || got.Provider != c.provider || got.Model != c.model {
				t.Errorf("parseLiteLLMDefaultModel() = (%q, %q, %v), want (%q, %q, %v)",
					got.Provider, got.Model, ok, c.provider, c.model, c.ok)
			}
		})
	}
}

func litellmConfigMap(name string, created time.Time, model string) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns", CreationTimestamp: metav1.NewTime(created)},
		Data:       map[string]string{litellmConfigKey: "model_list:\n" + cfg(agentModelName, model)},
	}
}

func litellmDeploymentMounting(configMapName string) *appsv1.Deployment {
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: litellmDeploymentName, Namespace: "test-ns"},
		Spec: appsv1.DeploymentSpec{Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{Volumes: []corev1.Volume{
			{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
			{Name: "config-volume", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{Name: configMapName}}}},
		}}}},
	}
}

func inferenceReconciler(t *testing.T, funcs *interceptor.Funcs, objs ...client.Object) *PlatformAgentReconciler {
	t.Helper()
	scheme := setupScheme()
	b := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...)
	if funcs != nil {
		b = b.WithInterceptorFuncs(*funcs)
	}
	for _, o := range objs {
		if _, ok := o.(*agentv1alpha1.PlatformAgent); ok {
			b = b.WithStatusSubresource(o)
		}
	}
	cl := b.Build()
	return &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
}

func TestReadInferenceModelReadsTheMountedConfigMap(t *testing.T) {
	agent := observedGenerationAgent(1)
	now := time.Now()
	r := inferenceReconciler(t, nil, agent,
		litellmDeploymentMounting("litellm-config-abc123"),
		litellmConfigMap("litellm-config-abc123", now.Add(-time.Hour), "gemini/gemini-3.5-flash"),
		litellmConfigMap("litellm-config", now, "anthropic/claude-opus-5"))
	got, read := r.readInferenceModel(context.Background(), agent)
	if read != inferenceReadFound || got != (inferenceModel{Provider: "gemini", Model: "gemini-3.5-flash"}) {
		t.Errorf("readInferenceModel() = (%+v, %v), want the mounted ConfigMap's gemini/gemini-3.5-flash, found", got, read)
	}
}

func TestReadInferenceModelFallsBackToTheNewestConfigMapWithoutTheDeployment(t *testing.T) {
	agent := observedGenerationAgent(1)
	now := time.Now()
	r := inferenceReconciler(t, nil, agent,
		litellmConfigMap("litellm-config", now.Add(-time.Hour), "anthropic/claude-opus-5"),
		litellmConfigMap("litellm-config-new", now, "openai/gpt-5.4"),
		litellmConfigMap("litellm-configs", now.Add(time.Hour), "gemini/not-a-candidate"))
	got, read := r.readInferenceModel(context.Background(), agent)
	if read != inferenceReadFound || got != (inferenceModel{Provider: "openai", Model: "gpt-5.4"}) {
		t.Errorf("readInferenceModel() = (%+v, %v), want the newest candidate's openai/gpt-5.4, found", got, read)
	}
}

func TestReadInferenceModelNegatives(t *testing.T) {
	agent := observedGenerationAgent(1)
	noKey := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "litellm-config", Namespace: "test-ns"}, Data: map[string]string{"other": "x"}}
	cases := []struct {
		name string
		objs []client.Object
	}{
		{"no ConfigMap", nil},
		{"unqualified model-default", []client.Object{litellmConfigMap("litellm-config", time.Now(), "gpt-4o")}},
		{"a ConfigMap without config.yaml", []client.Object{noKey}},
		{"the mounted ConfigMap is missing", []client.Object{litellmDeploymentMounting("litellm-config-gone")}},
		{"the mounted ConfigMap has no config.yaml", []client.Object{litellmDeploymentMounting("litellm-config"), noKey}},
		{"the Deployment mounts no LiteLLM config", []client.Object{litellmDeploymentMounting("other-config"), litellmConfigMap("litellm-config", time.Now(), "gemini/leftover")}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := inferenceReconciler(t, nil, append([]client.Object{agent.DeepCopy()}, c.objs...)...)
			got, read := r.readInferenceModel(context.Background(), agent)
			if read != inferenceReadNegative || got != (inferenceModel{}) {
				t.Errorf("readInferenceModel() = (%+v, %v), want a negative", got, read)
			}
		})
	}
}

func TestReadInferenceModelFailsOnAnAPIError(t *testing.T) {
	agent := observedGenerationAgent(1)
	injected := fmt.Errorf("injected: rate limited")
	for _, kind := range []string{"deployment", "configmap", "list"} {
		t.Run(kind, func(t *testing.T) {
			funcs := interceptor.Funcs{
				Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
					switch obj.(type) {
					case *appsv1.Deployment:
						if kind == "deployment" {
							return injected
						}
					case *corev1.ConfigMap:
						if kind == "configmap" {
							return injected
						}
					}
					return c.Get(ctx, key, obj, opts...)
				},
				List: func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
					if kind == "list" {
						return injected
					}
					return c.List(ctx, list, opts...)
				},
			}
			objs := []client.Object{agent.DeepCopy(), litellmConfigMap("litellm-config", time.Now(), "gemini/g")}
			if kind == "configmap" {
				objs = append(objs, litellmDeploymentMounting("litellm-config"))
			}
			r := inferenceReconciler(t, &funcs, objs...)
			if _, read := r.readInferenceModel(context.Background(), agent); read != inferenceReadFailed {
				t.Errorf("readInferenceModel() read = %v, want failed", read)
			}
		})
	}
}

func readBack(t *testing.T, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.PlatformAgent {
	t.Helper()
	stored := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("failed to read the agent back: %v", err)
	}
	return stored
}

func TestUpdateStatusReadyWritesAndClearsTheModel(t *testing.T) {
	agent := observedGenerationAgent(1)
	cm := litellmConfigMap("litellm-config", time.Now(), "gemini/gemini-3.5-flash")
	r := inferenceReconciler(t, nil, agent, cm)

	settleReady(t, r, agent)
	stored := readBack(t, r, agent)
	if stored.Status.Inference != (agentv1alpha1.InferenceStatus{Provider: "gemini", Model: "gemini-3.5-flash"}) {
		t.Fatalf("status.inference = %+v, want gemini/gemini-3.5-flash", stored.Status.Inference)
	}

	cm.Data[litellmConfigKey] = "model_list:\n" + cfg(agentModelName, "anthropic/claude-opus-5")
	if err := r.Update(context.Background(), cm); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, stored)
	stored = readBack(t, r, agent)
	if stored.Status.Inference != (agentv1alpha1.InferenceStatus{Provider: "anthropic", Model: "claude-opus-5"}) {
		t.Fatalf("status.inference = %+v after the edit, want anthropic/claude-opus-5", stored.Status.Inference)
	}

	if err := r.Delete(context.Background(), cm); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, stored)
	stored = readBack(t, r, agent)
	if stored.Status.Inference != (agentv1alpha1.InferenceStatus{}) {
		t.Errorf("status.inference = %+v after the ConfigMap was deleted, want both fields empty", stored.Status.Inference)
	}
}

func TestUpdateStatusReadyKeepsTheModelOnAReadError(t *testing.T) {
	agent := observedGenerationAgent(1)
	failing := false
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	funcs.Get = func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
		if _, ok := obj.(*corev1.ConfigMap); ok && failing {
			return fmt.Errorf("injected: the API server timed out")
		}
		return c.Get(ctx, key, obj, opts...)
	}
	r := inferenceReconciler(t, &funcs, agent, litellmDeploymentMounting("litellm-config"),
		litellmConfigMap("litellm-config", time.Now(), "gemini/gemini-3.5-flash"))

	settleReady(t, r, agent)
	stored := readBack(t, r, agent)
	if stored.Status.Inference.Model != "gemini-3.5-flash" {
		t.Fatalf("status.inference = %+v, want gemini/gemini-3.5-flash", stored.Status.Inference)
	}
	writes := counter.writes

	failing = true
	stored.Generation = 2
	if err := r.Update(context.Background(), stored); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, stored)
	if counter.writes != writes+1 {
		t.Fatalf("%d status writes after a generation bump, want %d: the other fields must still be written", counter.writes, writes+1)
	}
	stored = readBack(t, r, agent)
	if stored.Status.Inference != (agentv1alpha1.InferenceStatus{Provider: "gemini", Model: "gemini-3.5-flash"}) {
		t.Errorf("status.inference = %+v after a failed read, want the last written pair kept", stored.Status.Inference)
	}
	if readyConditionGeneration(t, stored) != stored.Generation {
		t.Errorf("the Ready condition did not move to generation %d", stored.Generation)
	}
}

func TestUpdateStatusReadyRecordsAPrunedInferenceBlock(t *testing.T) {
	agent := observedGenerationAgent(1)
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	persist := funcs.SubResourceUpdate
	pruning := true
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && pruning {
			pa.Status.Inference = agentv1alpha1.InferenceStatus{}
		}
		return persist(ctx, cl, subResourceName, obj, opts...)
	}
	r := inferenceReconciler(t, &funcs, agent, litellmConfigMap("litellm-config", time.Now(), "gemini/gemini-3.5-flash"))

	settleReady(t, r, agent)
	if agent.Status.Inference != (agentv1alpha1.InferenceStatus{}) {
		t.Fatalf("the fixture did not prune status.inference (got %+v)", agent.Status.Inference)
	}
	if !r.inferencePruned(agent) {
		t.Fatal("the echo dropped status.inference and no record was kept")
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Errorf("%d status writes across three passes under a pruning CRD, want 1", counter.writes)
	}

	// The record ages out: one probe write, recorded again, then quiet.
	age := func() {
		r.prunedNewerFieldsMu.Lock()
		r.prunedNewerFields[usagePollKey(agent)][inferenceNewerField] = time.Now().Add(-2 * usageStatusReprobeInterval)
		r.prunedNewerFieldsMu.Unlock()
	}
	age()
	if r.inferencePruned(agent) {
		t.Fatal("an aged record still reads as pruned")
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("%d writes after the record aged under a still-pruning CRD, want 2", counter.writes)
	}

	// The CRD is applied; once the record ages the probe lands the pair and clears it.
	pruning = false
	age()
	settleReady(t, r, agent)
	if counter.writes != 3 || agent.Status.Inference != (agentv1alpha1.InferenceStatus{Provider: "gemini", Model: "gemini-3.5-flash"}) {
		t.Fatalf("after the CRD was applied: %d writes, status.inference=%+v; want 3 and gemini/gemini-3.5-flash", counter.writes, agent.Status.Inference)
	}
	if r.inferencePruned(agent) {
		t.Error("the echo carried the pair and the record was not cleared")
	}
	r.prunedNewerFieldsMu.Lock()
	_, held := r.prunedNewerFields[usagePollKey(agent)][inferenceNewerField]
	r.prunedNewerFieldsMu.Unlock()
	if held {
		t.Error("the record is still held after an echo that carried the pair")
	}
	settleReady(t, r, agent)
	if counter.writes != 3 {
		t.Errorf("%d writes on an unchanged pass, want 3", counter.writes)
	}

	// forgetUsageStatus drops the whole per-CR map, this record included.
	pruning = true
	agent.Generation = 2
	if err := r.Update(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, agent)
	if !r.inferencePruned(agent) {
		t.Fatal("the pruning echo was not recorded again")
	}
	r.forgetUsageStatus(agent)
	if r.inferencePruned(agent) {
		t.Error("forgetUsageStatus left the status.inference record behind")
	}
}

func TestInferenceGaugePublishesOneSampleAndSwapsOnAChange(t *testing.T) {
	g := newInferenceGauges(prometheus.NewRegistry())
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "pa", Namespace: "ns"}}
	other := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "other", Namespace: "ns"}}
	other.Status.Inference = agentv1alpha1.InferenceStatus{Provider: "openai", Model: "gpt-5.4"}
	g.publish(other)

	agent.Status.Inference = agentv1alpha1.InferenceStatus{Provider: "gemini", Model: "a"}
	g.publish(agent)
	if n := testutil.CollectAndCount(g.model, inferenceGaugeName); n != 2 {
		t.Fatalf("%d samples after two agents published, want 2", n)
	}
	if v := testutil.ToFloat64(g.model.WithLabelValues("ns", "pa", "gemini", "a")); v != 1 {
		t.Errorf("model a sample = %v, want 1", v)
	}

	agent.Status.Inference.Model = "b"
	g.publish(agent)
	if n := testutil.CollectAndCount(g.model, inferenceGaugeName); n != 2 {
		t.Fatalf("%d samples after the model changed, want 2: the old pair was not deleted", n)
	}
	if g.model.DeleteLabelValues("ns", "pa", "gemini", "a") {
		t.Error("the model a sample survived the change")
	}
	if v := testutil.ToFloat64(g.model.WithLabelValues("ns", "pa", "gemini", "b")); v != 1 {
		t.Errorf("model b sample = %v, want 1", v)
	}

	agent.Status.Inference = agentv1alpha1.InferenceStatus{}
	g.publish(agent)
	if n := testutil.CollectAndCount(g.model, inferenceGaugeName); n != 1 {
		t.Errorf("%d samples after the pair cleared, want 1 (the other agent's)", n)
	}
	agent.Status.Inference = agentv1alpha1.InferenceStatus{Provider: "gemini"}
	g.publish(agent)
	if n := testutil.CollectAndCount(g.model, inferenceGaugeName); n != 1 {
		t.Errorf("%d samples with only a provider, want 1: one field alone is no sample", n)
	}

	agent.Status.Inference = agentv1alpha1.InferenceStatus{Provider: "gemini", Model: "c"}
	g.publish(agent)
	g.forget("ns", "pa")
	if n := testutil.CollectAndCount(g.model, inferenceGaugeName); n != 1 {
		t.Errorf("%d samples after forget, want 1 (the other agent's)", n)
	}
}

func TestIsLiteLLMConfigMapName(t *testing.T) {
	for name, want := range map[string]bool{
		"litellm-config":     true,
		"litellm-config-abc": true,
		"litellm-config-":    false,
		"litellm-configs":    false,
		"x":                  false,
	} {
		if got := isLiteLLMConfigMapName(name); got != want {
			t.Errorf("isLiteLLMConfigMapName(%q) = %v, want %v", name, got, want)
		}
	}
}
