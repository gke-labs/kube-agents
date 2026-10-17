package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestUpdateConfigYAMLWithModel(t *testing.T) {
	orig := `model_list:
  - model_name: model-default
    litellm_params:
      model: vertex_ai/gemini-3.5-flash
  - model_name: hermes-agent
    litellm_params:
      model: vertex_ai/gemini-3.5-flash
  - model_name: gemini-3.5-flash
    litellm_params:
      model: vertex_ai/gemini-3.5-flash
litellm_settings:
  callbacks: ["prometheus", "litellm_thinking_replay_callback.proxy_handler_instance"]
`

	updated := updateConfigYAMLWithModel(orig, "gemini-3.8-flash", "vertex_ai/gemini-3.8-flash")

	if !strings.Contains(updated, "model_name: model-default\n    litellm_params:\n      model: vertex_ai/gemini-3.8-flash") {
		t.Fatalf("model-default not updated:\n%s", updated)
	}
	if !strings.Contains(updated, "model_name: hermes-agent\n    litellm_params:\n      model: vertex_ai/gemini-3.8-flash") {
		t.Fatalf("hermes-agent not updated:\n%s", updated)
	}
	if !strings.Contains(updated, "  - model_name: gemini-3.8-flash\n    litellm_params:\n      model: vertex_ai/gemini-3.8-flash") {
		t.Fatalf("gemini-3.8-flash alias missing or misindented:\n%s", updated)
	}
	if !strings.Contains(updated, `callbacks: ["prometheus", "litellm_thinking_replay_callback.proxy_handler_instance"]`) {
		t.Fatalf("callbacks lost or corrupted:\n%s", updated)
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

	// 3. Verify in-memory state updated
	if s.cfg.AssistModel != "gemini-4.0-argon" {
		t.Errorf("s.cfg.AssistModel: got %q, want 'gemini-4.0-argon'", s.cfg.AssistModel)
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
	fakeGateway := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
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
