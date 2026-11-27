package main

import (
	"context"
	"iter"

	adkmodel "google.golang.org/adk/v2/model"

	"github.com/go-steer/core-agent/v3/pkg/config"
	"github.com/go-steer/core-agent/v3/pkg/models"
	_ "github.com/go-steer/core-agent/v3/pkg/models/gemini"
)

// newModel builds the turn's model; tests replace it.
var newModel = buildModel

// placeholderKey stands in for the model credential: the Gemini client
// refuses an empty key, and the worker's gateway proxy, the only endpoint it
// reaches, drops it and authenticates the call itself.
const placeholderKey = "gateway-proxy-owns-the-key"

// buildModel is the turn's model: Gemini, at the base URL the adapter put in
// GOOGLE_GEMINI_BASE_URL (the loopback gateway proxy), with URL context and
// code execution always off and Google Search on only when the turn's
// policy grants web search, so every other tool the model can call is one
// the policy offers. Search runs server-side, not gated per call, as
// WebSearch does on Claude Code; it works only where the endpoint behind
// the proxy offers it.
//
// The adapter strips every other model endpoint and credential from this
// process's environment (a2a/cmd/worker-adapter, startHarnessGateway).
func buildModel(ctx context.Context, name string, webSearch bool) (adkmodel.LLM, error) {
	cfg := config.DefaultConfig()
	cfg.Model.Provider = config.ProviderGemini
	cfg.Model.Name = name
	cfg.Model.APIKey = placeholderKey
	off, search := false, webSearch
	cfg.Model.BuiltinTools = &config.BuiltinToolsConfig{WebSearch: &search, URLContext: &off, CodeExecution: &off}
	p, err := models.Resolve(cfg)
	if err != nil {
		return nil, err
	}
	m, err := p.Model(ctx, name)
	if err != nil {
		return nil, err
	}
	return sideCallsWithoutBuiltins{m}, nil
}

// sideCallsWithoutBuiltins sends each side call, a one-shot request core-agent
// makes beside the turn's loop (the summarizer that compacts the history is
// the one a turn makes), with no server-side built-ins. core-agent sends a
// side call with no tools of its own, but the Gemini client adds Google
// Search to every request unless the call is marked; and core-agent's check
// that drops search for a model older than Gemini 3 applies only to a
// request with function declarations, so a side call would carry search on
// every model name. The marker only takes tools away.
type sideCallsWithoutBuiltins struct{ adkmodel.LLM }

func (m sideCallsWithoutBuiltins) GenerateContent(ctx context.Context, req *adkmodel.LLMRequest, stream bool) iter.Seq2[*adkmodel.LLMResponse, error] {
	if models.SideCallName(ctx) != "" {
		ctx = models.WithoutBuiltins(ctx)
	}
	return m.LLM.GenerateContent(ctx, req, stream)
}

// WithoutBuiltins keeps core-agent's model-level unwrap reachable through
// the wrapper.
func (m sideCallsWithoutBuiltins) WithoutBuiltins() adkmodel.LLM {
	if wb, ok := m.LLM.(interface{ WithoutBuiltins() adkmodel.LLM }); ok {
		return sideCallsWithoutBuiltins{wb.WithoutBuiltins()}
	}
	return m
}
