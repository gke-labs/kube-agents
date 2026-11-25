package main

import (
	"context"

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
// SEAM (tigerteam-dev-2): the base URL and proxy wiring.
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
	return p.Model(ctx, name)
}
