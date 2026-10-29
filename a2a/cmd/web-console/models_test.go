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
