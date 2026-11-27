package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestGenerateModelHelmValuesYAML(t *testing.T) {
	yaml := generateModelHelmValuesYAML("gemini-3.8-flash", "vertex_ai", "vertex_ai/gemini-3.8-flash", "high")
	for _, want := range []string{
		"modelProvider: vertex_ai",
		"modelDefaultName: gemini-3.8-flash",
		"reasoningEffort: high",
		"kubeagents.x-k8s.io/active-model: gemini-3.8-flash",
		"kubeagents.x-k8s.io/active-model-target: vertex_ai/gemini-3.8-flash",
		"kubeagents.x-k8s.io/reasoning-level: high",
	} {
		if !strings.Contains(yaml, want) {
			t.Fatalf("generateModelHelmValuesYAML missing %q:\n%s", want, yaml)
		}
	}
}

func TestModelsGetAndPost(t *testing.T) {
	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ModelName:         "gemini-3.5-flash",
		ModelProvider:     "vertex_ai",
	})

	// 1. GET /api/models
	req := httptest.NewRequest(http.MethodGet, "/api/models", nil)
	rec := httptest.NewRecorder()
	s.handleModelsGet(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/models code %d: %s", rec.Code, rec.Body.String())
	}
	var res ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&res); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if res.ActiveModel != "gemini-3.5-flash" {
		t.Errorf("active_model: got %q, want 'gemini-3.5-flash'", res.ActiveModel)
	}
	if len(res.Models) < 5 {
		t.Errorf("expected at least 5 models, got %d", len(res.Models))
	}

	// 2. POST /api/models - Switch to gemini-4.0-argon
	postBody := `{"model":"gemini-4.0-argon"}`
	req = httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(postBody))
	rec = httptest.NewRecorder()
	s.handleModelsPost(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/models code %d: %s", rec.Code, rec.Body.String())
	}
	var postRes ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&postRes); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if postRes.ActiveModel != "gemini-4.0-argon" {
		t.Errorf("active_model after post: got %q, want 'gemini-4.0-argon'", postRes.ActiveModel)
	}
	if postRes.ActiveTarget != "vertex_ai/gemini-4.0-argon" {
		t.Errorf("active_target: got %q, want 'vertex_ai/gemini-4.0-argon'", postRes.ActiveTarget)
	}

	// 3. Verify in-memory state and declarative Helm YAML updated without mutating s.cfg
	if !strings.Contains(postRes.HelmValuesYAML, "modelDefaultName: gemini-4.0-argon") {
		t.Errorf("helm_values_yaml missing modelDefaultName: %q", postRes.HelmValuesYAML)
	}
	m := s.currentModel(context.Background())
	if m.Name != "gemini-4.0-argon" {
		t.Errorf("cached model: got %q, want 'gemini-4.0-argon'", m.Name)
	}

	// 4. Change reasoning level independently
	req = httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(`{"reasoning_level":"high"}`))
	rec = httptest.NewRecorder()
	s.handleModelsPost(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/models reasoning_level=high code %d: %s", rec.Code, rec.Body.String())
	}
	var reasoningRes ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&reasoningRes); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if reasoningRes.ReasoningLevel != "high" {
		t.Errorf("reasoning_level after update: got %q, want 'high'", reasoningRes.ReasoningLevel)
	}
	if reasoningRes.ActiveModel != "gemini-4.0-argon" {
		t.Errorf("active_model after reasoning update: got %q, want 'gemini-4.0-argon'", reasoningRes.ActiveModel)
	}

	// 5. Change both model and reasoning level
	req = httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(`{"model":"gemini-3.8-flash","reasoning_level":"low"}`))
	rec = httptest.NewRecorder()
	s.handleModelsPost(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("POST /api/models model+reasoning code %d: %s", rec.Code, rec.Body.String())
	}
	var bothRes ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&bothRes); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if bothRes.ActiveModel != "gemini-3.8-flash" {
		t.Errorf("active_model: got %q, want 'gemini-3.8-flash'", bothRes.ActiveModel)
	}
	if bothRes.ReasoningLevel != "low" {
		t.Errorf("reasoning_level: got %q, want 'low'", bothRes.ReasoningLevel)
	}

	// 6. Bad request: invalid reasoning level
	req = httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(`{"reasoning_level":"super-extreme"}`))
	rec = httptest.NewRecorder()
	s.handleModelsPost(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for invalid reasoning level, got %d", rec.Code)
	}

	// 7. Bad request: empty model and empty reasoning
	req = httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(`{"model":""}`))
	rec = httptest.NewRecorder()
	s.handleModelsPost(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("expected 400 for empty model, got %d", rec.Code)
	}
}

func TestModelsPreFlightValidationFailure(t *testing.T) {
	var seenModels []string
	fakeGateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if m, ok := body["model"].(string); ok {
			seenModels = append(seenModels, m)
		}
		if body["model"] == "model-default" || body["model"] == "hermes-agent" {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"pong"}}]}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":{"message":"The requested model 'invalid-model' does not exist."}}`))
	}))
	defer fakeGateway.Close()

	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ModelName:         "gemini-3.5-flash",
		ModelProvider:     "vertex_ai",
		LiteLLMURL:        fakeGateway.URL,
	})

	postBody := `{"model":"invalid-model","model_target":"vertex_ai/invalid-model"}`
	req := httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(postBody))
	rec := httptest.NewRecorder()
	s.handleModelsPost(rec, req)

	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("expected 422 for failing pre-flight test, got %d: %s", rec.Code, rec.Body.String())
	}
	for _, m := range seenModels {
		if m == "model-default" || m == "hermes-agent" {
			t.Errorf("testModel fell back to alias %q when testing candidate model", m)
		}
	}
	var errResp map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&errResp)
	if errResp["error"] != "model_test_failed" {
		t.Errorf("expected error=model_test_failed, got %v", errResp["error"])
	}
}

func TestModelsTestEndpoint(t *testing.T) {
	fakeGateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		_ = json.NewDecoder(r.Body).Decode(&req)
		if req["model"] == "working-model" {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"pong"}}]}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"error":"model_not_found"}`))
	}))
	defer fakeGateway.Close()

	s := newServer(config{
		LiteLLMURL: fakeGateway.URL,
	})

	// 1. Success case
	req := httptest.NewRequest(http.MethodPost, "/api/models/test", strings.NewReader(`{"model":"working-model"}`))
	rec := httptest.NewRecorder()
	s.handleModelsTest(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d: %s", rec.Code, rec.Body.String())
	}
	var okResp map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&okResp)
	if okResp["verified"] != true {
		t.Errorf("expected verified=true, got %v", okResp["verified"])
	}

	// 2. Failure case
	req = httptest.NewRequest(http.MethodPost, "/api/models/test", strings.NewReader(`{"model":"failing-model"}`))
	rec = httptest.NewRecorder()
	s.handleModelsTest(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d: %s", rec.Code, rec.Body.String())
	}
	var failResp map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&failResp)
	if failResp["verified"] != false {
		t.Errorf("expected verified=false, got %v", failResp["verified"])
	}
}

// The model probe calls the inference gateway keyless: the agent API key is the
// agent's own, not a gateway credential. Both requests are covered: the first
// try, and the alias fallback testModel sends when the target is not mapped in
// the gateway.
func TestModelsTestSendsTheGatewayNoAgentKey(t *testing.T) {
	var models, auths []string
	fakeGateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var req map[string]any
		_ = json.NewDecoder(r.Body).Decode(&req)
		model, _ := req["model"].(string)
		models = append(models, model)
		auths = append(auths, r.Header.Get("Authorization"))
		if model == "working-model" || model == "alias-model" {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"pong"}}]}`))
			return
		}
		w.WriteHeader(http.StatusNotFound)
	}))
	defer fakeGateway.Close()

	s := newServer(config{LiteLLMURL: fakeGateway.URL, APIServerKey: "agent-api-key-not-for-the-gateway"})
	for _, body := range []string{
		`{"model":"working-model"}`,
		`{"model":"failing-model"}`,
		// An unmapped target: the probe retries under the model's own id.
		`{"model":"alias-model","model_target":"unmapped/target"}`,
	} {
		rec := httptest.NewRecorder()
		s.handleModelsTest(rec, httptest.NewRequest(http.MethodPost, "/api/models/test", strings.NewReader(body)))
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: code %d: %s", body, rec.Code, rec.Body.String())
		}
	}
	sawFallback := false
	for i, model := range models {
		if model == "alias-model" && i > 0 && models[i-1] == "unmapped/target" {
			sawFallback = true
		}
	}
	if !sawFallback {
		t.Fatalf("the alias fallback request never reached the gateway; models seen: %v", models)
	}
	for i, a := range auths {
		if a != "" {
			t.Errorf("gateway request %d (%s) carried Authorization %q; want none", i, models[i], a)
		}
	}
}

// servedGateway is an inference gateway whose /model/info lists the given
// model under the chart's three aliases (model-default, hermes-agent, the
// model's own name) plus every extra model under its own name, and whose
// chat completions answer for exactly those names.
func servedGateway(t *testing.T, defaultModel string, extra ...string) *httptest.Server {
	t.Helper()
	entry := func(alias, model string) map[string]any {
		return map[string]any{
			"model_name":     alias,
			"litellm_params": map[string]string{"model": "vertex_ai/" + model},
			"model_info":     map[string]string{"litellm_provider": "vertex_ai"},
		}
	}
	data := []map[string]any{
		entry("model-default", defaultModel),
		entry("hermes-agent", defaultModel),
		entry(defaultModel, defaultModel),
	}
	answers := map[string]bool{"model-default": true, "hermes-agent": true, defaultModel: true}
	for _, m := range extra {
		data = append(data, entry(m, m))
		answers[m] = true
	}
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/model/info" {
			_ = json.NewEncoder(w).Encode(map[string]any{"data": data})
			return
		}
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if m, _ := body["model"].(string); answers[m] {
			_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"pong"}}]}`))
			return
		}
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"error":{"message":"Invalid model name passed in"}}`))
	}))
	t.Cleanup(gw.Close)
	return gw
}

// recordedAgent is a Kubernetes API holding one PlatformAgent with the given
// annotations. It counts the writes made to it.
func recordedAgent(t *testing.T, annotations map[string]string) (*kubeClient, *int) {
	t.Helper()
	writes := 0
	api := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writes++
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"metadata": map[string]any{"annotations": annotations},
		})
	}))
	t.Cleanup(api.Close)
	return &kubeClient{base: api.URL, client: api.Client()}, &writes
}

func modelsServer(gatewayURL string, kube *kubeClient) *server {
	s := newServer(config{
		PodNamespace:      "kubeagents-system",
		PlatformAgentName: "platform-agent",
		ModelName:         "chart-model",
		ModelProvider:     "vertex_ai",
		LiteLLMURL:        gatewayURL,
	})
	s.kube = kube
	return s
}

func getModels(t *testing.T, s *server) ModelsResponse {
	t.Helper()
	rec := httptest.NewRecorder()
	s.handleModelsGet(rec, httptest.NewRequest(http.MethodGet, "/api/models", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /api/models code %d: %s", rec.Code, rec.Body.String())
	}
	var res ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&res); err != nil {
		t.Fatalf("decode: %v", err)
	}
	return res
}

func modelIDs(models []ModelOption) []string {
	ids := make([]string, 0, len(models))
	for _, m := range models {
		ids = append(ids, m.ID)
	}
	return ids
}

func TestParseServedModels(t *testing.T) {
	served, err := parseServedModels([]byte(`{"data":[
		{"model_name":"model-default","litellm_params":{"model":"vertex_ai/gemini-4-hydrogen"},"model_info":{"litellm_provider":"vertex_ai"}},
		{"model_name":"hermes-agent","litellm_params":{"model":"vertex_ai/gemini-4-hydrogen"},"model_info":{"litellm_provider":"vertex_ai"}},
		{"model_name":"gemini-4-hydrogen","litellm_params":{"model":"vertex_ai/gemini-4-hydrogen"},"model_info":{"litellm_provider":"vertex_ai"}},
		{"model_name":"fast","litellm_params":{"model":"vertex_ai/gemini-3.8-flash"},"model_info":{"litellm_provider":"vertex_ai"}}
	]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(modelIDs(served.options), ","); got != "gemini-4-hydrogen,gemini-3.8-flash" {
		t.Errorf("served models: got %q, want each model once: gemini-4-hydrogen,gemini-3.8-flash", got)
	}
	if served.inUse.ID != "gemini-4-hydrogen" {
		t.Errorf("model in use: got %q, want gemini-4-hydrogen", served.inUse.ID)
	}
	unknown, known := served.options[0], served.options[1]
	if unknown.Name != "gemini-4-hydrogen" || unknown.ModelTarget != "vertex_ai/gemini-4-hydrogen" || unknown.Provider != "vertex_ai" {
		t.Errorf("a model outside the preset catalog: got %+v", unknown)
	}
	if known.Name != "Gemini 3.8 Flash" {
		t.Errorf("a model in the preset catalog keeps its display name: got %q", known.Name)
	}
	for _, c := range []struct {
		model, target string
		want          bool
	}{
		{"gemini-4-hydrogen", "", true},
		{"", "vertex_ai/gemini-3.8-flash", true},
		{"fast", "", true},
		{"gemini-3.5-pro", "vertex_ai/gemini-3.5-pro", false},
		{"", "", false},
	} {
		if got := served.serves(c.model, c.target); got != c.want {
			t.Errorf("serves(%q, %q) = %v, want %v", c.model, c.target, got, c.want)
		}
	}

	// A target with more than one path segment keeps them all in the ID: it
	// is the modelDefaultName that selects the model. An entry with no model
	// after the provider is not a model.
	nested, err := parseServedModels([]byte(`{"data":[
		{"model_name":"model-default","litellm_params":{"model":"openrouter/anthropic/claude-3-5-sonnet"},"model_info":{"litellm_provider":"openrouter"}},
		{"model_name":"broken","litellm_params":{"model":"vertex_ai/"},"model_info":{}}
	]}`))
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(modelIDs(nested.options), ","); got != "anthropic/claude-3-5-sonnet" {
		t.Errorf("nested target: got models %q, want anthropic/claude-3-5-sonnet alone", got)
	}
	if o := nested.inUse; o.ID != "anthropic/claude-3-5-sonnet" || o.Provider != "openrouter" || o.ModelTarget != "openrouter/anthropic/claude-3-5-sonnet" {
		t.Errorf("nested target in use: got %+v", o)
	}

	for _, body := range []string{`{"data":[]}`, `not json`} {
		if _, err := parseServedModels([]byte(body)); err == nil {
			t.Errorf("parseServedModels(%q): want an error", body)
		}
	}
}

// After a Helm change of the default model, the PlatformAgent still records
// the model picked before it. The gateway no longer serves that model, so
// the console shows the model in use, not the record.
func TestRecordedModelTheGatewayNoLongerServesIsIgnored(t *testing.T) {
	gw := servedGateway(t, "gemini-4-hydrogen")
	kube, writes := recordedAgent(t, map[string]string{
		activeModelAnnotation:  "gemini-3.8-flash",
		activeTargetAnnotation: "vertex_ai/gemini-3.8-flash",
	})
	s := modelsServer(gw.URL, kube)

	res := getModels(t, s)
	if res.ActiveModel != "gemini-4-hydrogen" || res.ActiveTarget != "vertex_ai/gemini-4-hydrogen" {
		t.Errorf("active model: got %q (%q), want gemini-4-hydrogen (vertex_ai/gemini-4-hydrogen)", res.ActiveModel, res.ActiveTarget)
	}
	if res.ModelInUse != "gemini-4-hydrogen" {
		t.Errorf("model in use: got %q, want gemini-4-hydrogen", res.ModelInUse)
	}
	if got := strings.Join(modelIDs(res.Models), ","); got != "gemini-4-hydrogen" {
		t.Errorf("models: got %q, want only gemini-4-hydrogen", got)
	}
	if strings.Contains(res.HelmValuesYAML, "gemini-3.8-flash") {
		t.Errorf("Helm values name the model no longer served:\n%s", res.HelmValuesYAML)
	}
	if *writes != 0 {
		t.Errorf("reading the models wrote to the PlatformAgent %d times, want 0", *writes)
	}

	insights := decode[insightsResponse](t, serve(s.routes(), insightsReq()))
	if insights.Model.Name != "gemini-4-hydrogen" {
		t.Errorf("insights model: got %q, want gemini-4-hydrogen", insights.Model.Name)
	}
}

// A recorded model the gateway still serves stays the preferred model, and
// the response says the agent runs on another one.
func TestRecordedModelTheGatewayServesIsKept(t *testing.T) {
	gw := servedGateway(t, "gemini-4-hydrogen", "gemini-3.8-flash")
	kube, _ := recordedAgent(t, map[string]string{
		activeModelAnnotation:  "gemini-3.8-flash",
		activeTargetAnnotation: "vertex_ai/gemini-3.8-flash",
	})
	res := getModels(t, modelsServer(gw.URL, kube))

	if res.ActiveModel != "gemini-3.8-flash" {
		t.Errorf("active model: got %q, want the recorded gemini-3.8-flash", res.ActiveModel)
	}
	if res.ModelInUse != "gemini-4-hydrogen" {
		t.Errorf("model in use: got %q, want gemini-4-hydrogen", res.ModelInUse)
	}
	if !strings.Contains(res.HelmValuesYAML, "modelDefaultName: gemini-3.8-flash") {
		t.Errorf("Helm values do not switch to the preferred model:\n%s", res.HelmValuesYAML)
	}
}

// A gateway that cannot be asked says nothing about the record, so it stands,
// and the selector falls back to the preset catalog.
func TestRecordedModelStandsWhenTheGatewayCannotBeAsked(t *testing.T) {
	down := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer down.Close()
	kube, _ := recordedAgent(t, map[string]string{
		activeModelAnnotation:  "gemini-3.8-flash",
		activeTargetAnnotation: "vertex_ai/gemini-3.8-flash",
	})
	res := getModels(t, modelsServer(down.URL, kube))

	if res.ActiveModel != "gemini-3.8-flash" {
		t.Errorf("active model: got %q, want the recorded gemini-3.8-flash", res.ActiveModel)
	}
	if res.ModelsSource != "presets" || len(res.Models) != len(candidateModelCatalog) {
		t.Errorf("models: got %d from %q, want the %d presets", len(res.Models), res.ModelsSource, len(candidateModelCatalog))
	}
	if res.ModelInUse != "chart-model" {
		t.Errorf("model in use: got %q, want MODEL_NAME (chart-model)", res.ModelInUse)
	}
}

// A gateway that routes by pattern serves models its list does not name, so
// the list cannot call a recorded model unserved: the record stands. The
// models it does name are still listed, the pattern is not offered as a
// model, and the model in use is still the default alias's.
func TestRecordedModelStandsWhenTheGatewayRoutesByPattern(t *testing.T) {
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
			{"model_name":"model-default","litellm_params":{"model":"vertex_ai/gemini-4-hydrogen"},"model_info":{"litellm_provider":"vertex_ai"}},
			{"model_name":"vertex_ai/*","litellm_params":{"model":"vertex_ai/*"},"model_info":{}}
		]}`))
	}))
	defer gw.Close()
	kube, _ := recordedAgent(t, map[string]string{
		activeModelAnnotation:  "gemini-3.8-flash",
		activeTargetAnnotation: "vertex_ai/gemini-3.8-flash",
	})
	res := getModels(t, modelsServer(gw.URL, kube))

	if res.ActiveModel != "gemini-3.8-flash" {
		t.Errorf("active model: got %q, want the recorded gemini-3.8-flash", res.ActiveModel)
	}
	if res.ModelInUse != "gemini-4-hydrogen" {
		t.Errorf("model in use: got %q, want the default alias's gemini-4-hydrogen", res.ModelInUse)
	}
	if res.ModelsSource != "gateway" {
		t.Errorf("models source: got %q, want gateway", res.ModelsSource)
	}
	for _, m := range res.Models {
		if strings.Contains(m.ID, "*") {
			t.Errorf("the selector offers the pattern %q as a model", m.ID)
		}
	}

	// With nothing recorded, the preferred model is the model in use.
	unrecorded := getModels(t, modelsServer(gw.URL, nil))
	if unrecorded.ActiveModel != "gemini-4-hydrogen" || unrecorded.ModelInUse != "gemini-4-hydrogen" {
		t.Errorf("nothing recorded: got active %q, in use %q; want gemini-4-hydrogen for both", unrecorded.ActiveModel, unrecorded.ModelInUse)
	}

	// A list of patterns alone names no model: it reads as no list.
	if _, err := parseServedModels([]byte(`{"data":[{"model_name":"vertex_ai/*","litellm_params":{"model":"vertex_ai/*"}}]}`)); err == nil {
		t.Error("a list holding only a pattern: want an error")
	}
}

// The selector lists what the gateway serves: a model the preset catalog
// does not know appears, and a preset the gateway does not serve does not.
func TestModelsListComesFromTheGateway(t *testing.T) {
	gw := servedGateway(t, "gemini-4-hydrogen", "gemini-3.8-flash")
	res := getModels(t, modelsServer(gw.URL, nil))

	if res.ModelsSource != "gateway" {
		t.Errorf("models source: got %q, want gateway", res.ModelsSource)
	}
	if got := strings.Join(modelIDs(res.Models), ","); got != "gemini-4-hydrogen,gemini-3.8-flash" {
		t.Errorf("models: got %q, want gemini-4-hydrogen,gemini-3.8-flash", got)
	}
	if res.ActiveModel != "gemini-4-hydrogen" {
		t.Errorf("active model: got %q, want the gateway's default gemini-4-hydrogen", res.ActiveModel)
	}
}

// A model whose target has more than one path segment is listed, in use and
// turned into Helm values under one ID: the chart joins modelProvider and
// modelDefaultName into the route, so a shortened name would switch the
// agent to another model.
func TestNestedModelTargetKeepsItsWholeName(t *testing.T) {
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/model/info" {
			_, _ = w.Write([]byte(`{"data":[
				{"model_name":"model-default","litellm_params":{"model":"openrouter/anthropic/claude-3-5-sonnet"},"model_info":{"litellm_provider":"openrouter"}},
				{"model_name":"other","litellm_params":{"model":"openrouter/meta-llama/llama-3-70b"},"model_info":{"litellm_provider":"openrouter"}}
			]}`))
			return
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"pong"}}]}`))
	}))
	defer gw.Close()
	s := modelsServer(gw.URL, nil)

	res := getModels(t, s)
	if res.ActiveModel != "anthropic/claude-3-5-sonnet" || res.ModelInUse != "anthropic/claude-3-5-sonnet" {
		t.Errorf("nothing recorded: got active %q, in use %q; want anthropic/claude-3-5-sonnet for both", res.ActiveModel, res.ModelInUse)
	}
	if res.ActiveTarget != "openrouter/anthropic/claude-3-5-sonnet" || res.ActiveProvider != "openrouter" {
		t.Errorf("got target %q, provider %q; want openrouter/anthropic/claude-3-5-sonnet, openrouter", res.ActiveTarget, res.ActiveProvider)
	}

	// The page sends a listed model's id, provider and target as they are.
	rec := httptest.NewRecorder()
	s.handleModelsPost(rec, httptest.NewRequest(http.MethodPost, "/api/models",
		strings.NewReader(`{"model":"meta-llama/llama-3-70b","provider":"openrouter","model_target":"openrouter/meta-llama/llama-3-70b"}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var picked ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&picked); err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"modelProvider: openrouter\n", "modelDefaultName: meta-llama/llama-3-70b\n"} {
		if !strings.Contains(picked.HelmValuesYAML, want) {
			t.Errorf("Helm values missing %q:\n%s", want, picked.HelmValuesYAML)
		}
	}
	if picked.ActiveModel != "meta-llama/llama-3-70b" || picked.ModelInUse != "anthropic/claude-3-5-sonnet" {
		t.Errorf("got active %q, in use %q; want meta-llama/llama-3-70b, anthropic/claude-3-5-sonnet", picked.ActiveModel, picked.ModelInUse)
	}

	// A custom pick typed as the full target records the same name.
	rec = httptest.NewRecorder()
	s.handleModelsPost(rec, httptest.NewRequest(http.MethodPost, "/api/models",
		strings.NewReader(`{"model":"openrouter/anthropic/claude-3-5-sonnet"}`)))
	var custom ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&custom); err != nil {
		t.Fatal(err)
	}
	if custom.ActiveModel != custom.ModelInUse {
		t.Errorf("a custom pick of the model in use: got active %q, in use %q; want them equal", custom.ActiveModel, custom.ModelInUse)
	}
}

// With a cluster to record on and nothing recorded, the preferred model is
// the model in use even after an Insights read has cached that model under
// its shorter display name, which here is also another served model's ID.
func TestInsightsDisplayNameIsNotTakenForARecord(t *testing.T) {
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"data":[
			{"model_name":"model-default","litellm_params":{"model":"openrouter/anthropic/claude-3-5-sonnet"},"model_info":{"litellm_provider":"openrouter"}},
			{"model_name":"vx","litellm_params":{"model":"vertex_ai/claude-3-5-sonnet"},"model_info":{"litellm_provider":"vertex_ai"}}
		]}`))
	}))
	defer gw.Close()
	kube, _ := recordedAgent(t, nil)
	s := modelsServer(gw.URL, kube)

	insights := decode[insightsResponse](t, serve(s.routes(), insightsReq()))
	if insights.Model.Name != "claude-3-5-sonnet" {
		t.Fatalf("insights model: got %q, want the display name claude-3-5-sonnet", insights.Model.Name)
	}
	res := getModels(t, s)
	if res.ActiveModel != "anthropic/claude-3-5-sonnet" || res.ModelInUse != "anthropic/claude-3-5-sonnet" {
		t.Errorf("got active %q, in use %q; want anthropic/claude-3-5-sonnet for both", res.ActiveModel, res.ModelInUse)
	}
	if res.ActiveTarget != "openrouter/anthropic/claude-3-5-sonnet" {
		t.Errorf("active target: got %q, want openrouter/anthropic/claude-3-5-sonnet", res.ActiveTarget)
	}
}

// An openai_compatible install's gateway names its provider hosted_vllm.
// The Helm values the console shows name openai_compatible, the chart value
// that keeps the endpoint settings; the target stays the gateway's.
func TestOpenAICompatibleInstallKeepsItsChartProvider(t *testing.T) {
	var tested []string
	gw := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path != "/model/info" {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			m, _ := body["model"].(string)
			tested = append(tested, m)
		}
		if r.URL.Path == "/model/info" {
			_, _ = w.Write([]byte(`{"data":[
				{"model_name":"model-default","litellm_params":{"model":"hosted_vllm/qwen-coder"},"model_info":{"litellm_provider":"hosted_vllm"}},
				{"model_name":"other","litellm_params":{"model":"hosted_vllm/llama-70b"},"model_info":{}}
			]}`))
			return
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"pong"}}]}`))
	}))
	defer gw.Close()
	s := modelsServer(gw.URL, nil)
	s.cfg.ModelProvider = "openai_compatible"

	res := getModels(t, s)
	if res.ActiveProvider != "openai_compatible" || res.ActiveTarget != "hosted_vllm/qwen-coder" {
		t.Errorf("got provider %q, target %q; want openai_compatible, hosted_vllm/qwen-coder", res.ActiveProvider, res.ActiveTarget)
	}
	if !strings.Contains(res.HelmValuesYAML, "modelProvider: openai_compatible\n") || strings.Contains(res.HelmValuesYAML, "modelProvider: hosted_vllm") {
		t.Errorf("Helm values for the model in use:\n%s", res.HelmValuesYAML)
	}
	for _, m := range res.Models {
		if m.Provider != "openai_compatible" {
			t.Errorf("model %q: provider %q, want openai_compatible", m.ID, m.Provider)
		}
	}

	// A listed pick sends the listed provider, with or without the target;
	// a custom pick names the gateway's. Each is tested under the gateway's
	// target and comes back under the chart's provider.
	for _, body := range []string{
		`{"model":"llama-70b","provider":"openai_compatible","model_target":"hosted_vllm/llama-70b"}`,
		`{"model":"llama-70b","provider":"openai_compatible"}`,
		`{"model":"hosted_vllm/llama-70b"}`,
	} {
		tested = nil
		rec := httptest.NewRecorder()
		s.handleModelsPost(rec, httptest.NewRequest(http.MethodPost, "/api/models", strings.NewReader(body)))
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: code %d: %s", body, rec.Code, rec.Body.String())
		}
		var picked ModelsResponse
		if err := json.NewDecoder(rec.Body).Decode(&picked); err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(picked.HelmValuesYAML, "modelProvider: openai_compatible\n  modelDefaultName: llama-70b\n") {
			t.Errorf("%s: Helm values:\n%s", body, picked.HelmValuesYAML)
		}
		if len(tested) == 0 || tested[0] != "hosted_vllm/llama-70b" || picked.ActiveTarget != "hosted_vllm/llama-70b" {
			t.Errorf("%s: gateway tested %q, target %q; want hosted_vllm/llama-70b for both", body, tested, picked.ActiveTarget)
		}
	}

	// A recorded pick carries no provider; the one read from the gateway
	// for it is renamed too.
	kube, _ := recordedAgent(t, map[string]string{
		activeModelAnnotation:  "llama-70b",
		activeTargetAnnotation: "hosted_vllm/llama-70b",
	})
	recorded := modelsServer(gw.URL, kube)
	recorded.cfg.ModelProvider = "openai_compatible"
	if res := getModels(t, recorded); !strings.Contains(res.HelmValuesYAML, "modelProvider: openai_compatible\n  modelDefaultName: llama-70b\n") {
		t.Errorf("a recorded pick: Helm values:\n%s", res.HelmValuesYAML)
	}

	// On any other install hosted_vllm is a provider slug of its own.
	if res := getModels(t, modelsServer(gw.URL, nil)); res.ActiveProvider != "hosted_vllm" {
		t.Errorf("an install that is not openai_compatible: provider %q, want hosted_vllm", res.ActiveProvider)
	}
}

// A model picked earlier in this process and no longer served is dropped
// like a recorded one.
func TestPickHeldInMemoryIsDroppedWhenNoLongerServed(t *testing.T) {
	gw := servedGateway(t, "gemini-4-hydrogen")
	s := modelsServer(gw.URL, nil)
	s.model.value = modelInfo{Name: "gemini-3.8-flash", Provider: "vertex_ai"}

	if res := getModels(t, s); res.ActiveModel != "gemini-4-hydrogen" {
		t.Errorf("active model: got %q, want gemini-4-hydrogen", res.ActiveModel)
	}
}

// A pick the gateway does not answer for records nothing and returns the
// Helm values that would make the gateway serve it.
func TestFailedPickReturnsHelmValuesAndRecordsNothing(t *testing.T) {
	gw := servedGateway(t, "gemini-4-hydrogen")
	kube, writes := recordedAgent(t, nil)
	s := modelsServer(gw.URL, kube)

	rec := httptest.NewRecorder()
	s.handleModelsPost(rec, httptest.NewRequest(http.MethodPost, "/api/models",
		strings.NewReader(`{"model":"vertex_ai/gemini-9-new"}`)))
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("code %d, want 422: %s", rec.Code, rec.Body.String())
	}
	var res modelTestFailedResponse
	if err := json.NewDecoder(rec.Body).Decode(&res); err != nil {
		t.Fatal(err)
	}
	if res.Error != "model_test_failed" || res.Model != "gemini-9-new" || res.Detail == "" {
		t.Errorf("got %+v, want error model_test_failed for gemini-9-new with a detail", res)
	}
	if res.Target != "vertex_ai/gemini-9-new" || res.Reason != "HTTP 400: Invalid model name passed in" {
		t.Errorf("got target %q, reason %q; want vertex_ai/gemini-9-new and the gateway's answer alone", res.Target, res.Reason)
	}
	for _, want := range []string{"modelProvider: vertex_ai", "modelDefaultName: gemini-9-new"} {
		if !strings.Contains(res.HelmValuesYAML, want) {
			t.Errorf("Helm values missing %q:\n%s", want, res.HelmValuesYAML)
		}
	}
	if *writes != 0 {
		t.Errorf("a failed pick wrote to the PlatformAgent %d times, want 0", *writes)
	}
}

// A pick the gateway answers for is recorded, and the response carries the
// model in use and the Helm values that switch to the pick.
func TestPassedPickIsRecordedWithTheModelInUse(t *testing.T) {
	gw := servedGateway(t, "gemini-4-hydrogen", "gemini-3.8-flash")
	kube, writes := recordedAgent(t, map[string]string{
		activeModelAnnotation:  "gemini-3.8-flash",
		activeTargetAnnotation: "vertex_ai/gemini-3.8-flash",
	})
	s := modelsServer(gw.URL, kube)

	rec := httptest.NewRecorder()
	s.handleModelsPost(rec, httptest.NewRequest(http.MethodPost, "/api/models",
		strings.NewReader(`{"model":"gemini-3.8-flash","provider":"vertex_ai","model_target":"vertex_ai/gemini-3.8-flash"}`)))
	if rec.Code != http.StatusOK {
		t.Fatalf("code %d, want 200: %s", rec.Code, rec.Body.String())
	}
	var res ModelsResponse
	if err := json.NewDecoder(rec.Body).Decode(&res); err != nil {
		t.Fatal(err)
	}
	if res.ActiveModel != "gemini-3.8-flash" || res.ModelInUse != "gemini-4-hydrogen" || res.ModelsSource != "gateway" {
		t.Errorf("got active %q, in use %q, source %q; want gemini-3.8-flash, gemini-4-hydrogen, gateway", res.ActiveModel, res.ModelInUse, res.ModelsSource)
	}
	if !strings.Contains(res.HelmValuesYAML, "modelDefaultName: gemini-3.8-flash") {
		t.Errorf("Helm values do not switch to the pick:\n%s", res.HelmValuesYAML)
	}
	if *writes != 1 {
		t.Errorf("a passed pick wrote to the PlatformAgent %d times, want 1", *writes)
	}
}

// The page shows what a pick did: a note with the Helm values for a
// preferred model that is not in use, and for a pick that failed its test.
func TestPageShowsTheHelmValuesOfAPick(t *testing.T) {
	raw, err := staticFS.ReadFile("static/index.html")
	if err != nil {
		t.Fatal(err)
	}
	page := string(raw)
	for _, want := range []string{
		`id="model-note"`,
		`id="model-note-yaml"`,
		`id="model-note-copy"`,
		`data.error === "model_test_failed"`,
		"showModelTestFailedNote(data);",
		"Gateway's answer for ${data.target}: ${data.reason}.",
		"renderPreferredModelNote(data);",
		"data.model_in_use",
		"preferred === inUse",
		"if (failedModelNote) return;",
		"preferred === pickedModel",
	} {
		if !strings.Contains(page, want) {
			t.Errorf("index.html is missing %q", want)
		}
	}
}
