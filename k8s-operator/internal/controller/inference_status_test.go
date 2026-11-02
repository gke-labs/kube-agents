package controller

import (
	"fmt"
	"strings"
	"testing"
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
