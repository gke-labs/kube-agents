package main

import (
	"fmt"
	"io"
	"log/slog"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// coreAgentGatewayURLEnv is where core-agent's Gemini client (the genai SDK)
// reads its endpoint when no base URL is configured.
const coreAgentGatewayURLEnv = "GOOGLE_GEMINI_BASE_URL"

// coreAgentWithheldPrefixes are dropped from core-agent's environment by
// coreAgentBuilder.env: every model credential, endpoint and project
// setting core-agent and its model library (go-steer/core-models) read.
//   - Its endpoint is always the adapter's loopback proxy
//     (startHarnessGateway sets GOOGLE_GEMINI_BASE_URL after this), so a pod
//     or image value can't take it around the proxy.
//   - No key reaches it: the harness sends a fixed placeholder
//     (a2a/cmd/core-agent-harness), which the proxy doesn't forward, and a
//     caller key lives in the adapter. While the gateway admits keyless
//     callers this is tidiness; once #6706 makes the key the gate, it's what
//     keeps the harness and its tools from calling the gateway around the
//     proxy.
//   - No Vertex project, region or credentials file, and no
//     GOOGLE_GENAI_USE_VERTEXAI, so nothing can select a Vertex backend and
//     its Application Default Credentials.
//
// By prefix, as the tool-child allowlist (#7140) will withhold them,
// because the library adds providers: a new provider's variable under one
// of these is dropped without a change here.
var coreAgentWithheldPrefixes = []string{
	"GOOGLE_", "GEMINI_", "CLOUD_ML_", "ANTHROPIC_", "OPENAI_", "XAI_", "GROQ_", "AICORE_", "GW_",
}

// coreAgentKeptEnv are names under those prefixes that carry no model route
// or credential and stay: the universe domain, which the gateway sets on
// cluster-view session pods for the Google client libraries a tool runs.
var coreAgentKeptEnv = map[string]bool{"GOOGLE_CLOUD_UNIVERSE_DOMAIN": true}

func coreAgentWithheld(key string) bool {
	if coreAgentKeptEnv[key] {
		return false
	}
	for _, p := range coreAgentWithheldPrefixes {
		if strings.HasPrefix(key, p) {
			return true
		}
	}
	return false
}

// gatewayProxyStarter is workeradapter.StartGatewayProxy; tests replace it.
type gatewayProxyStarter func(gateway string, log *slog.Logger) (*workeradapter.GatewayProxy, error)

// startHarnessGateway starts the loopback proxy a core-agent worker's model
// calls go through and returns env with the harness pointed at it, plus the
// proxy to close at worker exit. Any other harness gets env back unchanged
// and a nil closer.
//
// One proxy per worker, in the adapter's own process: the harness runs one
// process per turn on this same env, and every one uses this proxy.
//
// The proxy forwards to the pod's ANTHROPIC_BASE_URL, else the install's
// inference-gateway: the same gateway, by the same rule, as Claude Code's.
func startHarnessGateway(harness persona.Harness, env []string, getenv func(string) string, start gatewayProxyStarter, log *slog.Logger) ([]string, io.Closer, error) {
	if harness != persona.CoreAgent {
		return env, nil, nil
	}
	gateway := getenv(modelBaseURLEnv)
	if gateway == "" {
		gateway = defaultModelBaseURL
	}
	proxy, err := start(gateway, log)
	if err != nil {
		return nil, nil, fmt.Errorf("the harness's gateway proxy: %w", err)
	}
	log.Info("the harness's model calls go through the loopback gateway proxy", "harness", string(harness), "proxy", proxy.URL, "gateway", gateway)
	// coreAgentBuilder.env already dropped any pod value for it.
	out := append(append([]string(nil), env...), coreAgentGatewayURLEnv+"="+proxy.URL)
	return out, proxy, nil
}

// applyHarnessGateway is run()'s step: startHarnessGateway on cfg's harness
// and env, with the result written back to cfg.HarnessEnv, which every
// turn's harness process gets. The closer is nil for other harnesses.
func applyHarnessGateway(cfg *workeradapter.Config, getenv func(string) string, start gatewayProxyStarter, log *slog.Logger) (io.Closer, error) {
	env, closer, err := startHarnessGateway(cfg.Driver.Harness(), cfg.HarnessEnv, getenv, start, log)
	if err != nil {
		return nil, err
	}
	cfg.HarnessEnv = env
	return closer, nil
}
