package main

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"time"

	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// harnessSpec is the worker's harness policy for one argv, in terms a
// harnessBuilder renders as its harness's flags. The tool names are Claude
// Code's until the permissions vocabulary is neutral (#6711).
type harnessSpec struct {
	path, model, maxTurns string
	// tools is a session pod's full argv: the tool allowlist and denylist,
	// the settings and MCP isolation, the delegate server and the system
	// prompts. A profile worker's base argv carries none of them; the
	// persona's rendering supplies its own (worker-adapter/profile.go).
	tools               bool
	allowed, disallowed string
	delegate            *delegateServer
	prompts             []string
	extra               []string
	// turnBudget is the task deadline less the kill grace: how long the
	// harness may run before the adapter stops it. A harness with its own
	// wall-clock bound sets it below this, so the harness ends the turn
	// with a result rather than the adapter killing it (agyBuilder).
	turnBudget time.Duration
}

// delegateServer is the delegate tool's MCP server: this binary run as
// `mcp`, told the adapter's socket.
type delegateServer struct {
	command, socket string
}

// harnessBuilder renders a harnessSpec as one harness's argv, and the pod's
// environment as that harness's. The stream contract is the matching
// workeradapter.Driver's.
type harnessBuilder interface {
	command(harnessSpec) []string
	// env adds the harness's model-auth and switch defaults to base, the pod
	// environment minus the bus credential; has reports whether the pod set
	// a variable.
	env(base []string, has func(string) bool) []string
	// userSettings is the settings file the harness reads for its user
	// source, or "".
	userSettings(getenv func(string) string) string
	defaultPath() string
}

// activeHarness is the pod's harness builder, chosen once by selectHarness
// before anything is built from it. Claude Code until then, as for a test
// that builds an argv directly.
var activeHarness harnessBuilder = claudeBuilder{}

// claudeBuilder is Claude Code's argv and env: the native binary driven over
// the headless stream-json contract.
type claudeBuilder struct{}

const (
	// strictMCPConfigFlag makes the harness load MCP servers from
	// --mcp-config only, ignoring .mcp.json in the cwd and the user config's
	// servers. Without --mcp-config it means no MCP servers at all. Every
	// argv carries it: in a reused pod the cwd (/scratch) and the config dir
	// have hosted an earlier turn's harness, which had Write.
	strictMCPConfigFlag = "--strict-mcp-config"
	// settingSourcesFlag and adapterSettingSources: the harness reads
	// settings only from the user source ($CLAUDE_CONFIG_DIR, where the image
	// puts the persona's CLAUDE.md and skills, which load under that source
	// and no other) plus the flag and managed sources it always reads. The
	// project and local sources, .claude/ under the cwd, are where an earlier
	// turn's Write could leave hooks, permissions or env for the next turn's
	// harness; the adapter puts nothing there.
	settingSourcesFlag    = "--setting-sources"
	adapterSettingSources = "user"
	// The user source's file (harnessUserSettings): the harness reads
	// settings.json in $CLAUDE_CONFIG_DIR, which defaults to ~/.claude.
	claudeConfigDirEnv     = "CLAUDE_CONFIG_DIR"
	claudeDefaultConfigDir = ".claude"
	claudeUserSettingsFile = "settings.json"

	// defaultModelBaseURL is the install's inference gateway. defaultModelAPIKey
	// is not a credential: the gateway here runs keyless and the harness only
	// checks that the variable is non-empty.
	defaultModelBaseURL = "http://inference-gateway"
	defaultModelAPIKey  = "a2a-playground" // #nosec G101 -- placeholder, not a secret

	// The env names that tell the harness how to reach the model.
	modelBaseURLEnv   = "ANTHROPIC_BASE_URL"
	modelAPIKeyEnv    = "ANTHROPIC_API_KEY"    // #nosec G101 -- env name, not a credential
	modelAuthTokenEnv = "ANTHROPIC_AUTH_TOKEN" // #nosec G101 -- env name, not a credential
	modelUseVertexEnv = "CLAUDE_CODE_USE_VERTEX"
)

func (claudeBuilder) defaultPath() string { return workeradapter.DefaultHarnessPath }

func (claudeBuilder) command(spec harnessSpec) []string {
	argv := []string{
		spec.path,
		"--print",
		"--output-format", "stream-json",
		"--input-format", "stream-json",
		"--verbose",
		"--model", spec.model,
		"--max-turns", spec.maxTurns,
	}
	if spec.tools {
		argv = append(argv,
			"--allowedTools", spec.allowed,
			"--disallowedTools", spec.disallowed,
			settingSourcesFlag, adapterSettingSources,
			// With or without the delegate tool: a wake's argv has no
			// --mcp-config, and without this flag it would load whatever MCP
			// config an earlier turn in the same pod left on disk.
			strictMCPConfigFlag,
		)
		if d := spec.delegate; d != nil {
			mcpCfg, _ := json.Marshal(map[string]any{workeradapter.MCPServersKey: map[string]any{
				workeradapter.DelegateMCPServer: map[string]any{
					"command":               d.command,
					"args":                  []string{"mcp"},
					workeradapter.MCPEnvKey: map[string]string{workeradapter.EnvDelegateSocket: d.socket},
				},
			}})
			argv = append(argv, workeradapter.MCPConfigFlag, string(mcpCfg))
		}
		if len(spec.prompts) > 0 {
			argv = append(argv, "--append-system-prompt", strings.Join(spec.prompts, "\n\n"))
		}
	}
	return append(argv, spec.extra...)
}

// env: with nothing configured, the harness talks to the install's own
// LiteLLM - Vertex-backed via the install's credentials, no per-worker key
// (playground posture; the deployment spec's auth story replaces this). The
// placeholder key goes in whenever the pod sets no key, no bearer token and
// no Vertex, so a pod spec never needs one.
func (claudeBuilder) env(env []string, has func(string) bool) []string {
	if !has(modelBaseURLEnv) && !has(modelUseVertexEnv) && !has(modelAPIKeyEnv) {
		env = append(env, modelBaseURLEnv+"="+defaultModelBaseURL)
	}
	// The gateway runs keyless. The placeholder only satisfies the harness's
	// "an API key exists" check. The adapter adds it here, in the child env,
	// also when the pod sets ANTHROPIC_BASE_URL (an AgentProfile worker sets
	// the full gateway name). Thus a pod spec does not need a literal model
	// key, which the AI workload audit flags (fleet-audit check 3.5,
	// model-credential-plaintext-env). Vertex and a bearer token do not use
	// the key, so the adapter does not add it.
	if !has(modelAPIKeyEnv) && !has(modelUseVertexEnv) && !has(modelAuthTokenEnv) {
		env = append(env, modelAPIKeyEnv+"="+defaultModelAPIKey)
	}
	for _, kv := range []string{
		"CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
		"DISABLE_AUTOUPDATER=1",
		"DISABLE_TELEMETRY=1",
		"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
	} {
		key := kv[:strings.Index(kv, "=")]
		if !has(key) {
			env = append(env, kv)
		}
	}
	return env
}

// userSettings: settings.json in $CLAUDE_CONFIG_DIR, else in ~/.claude.
func (claudeBuilder) userSettings(getenv func(string) string) string {
	dir := getenv(claudeConfigDirEnv)
	if dir == "" {
		home := getenv("HOME")
		if home == "" {
			return ""
		}
		dir = filepath.Join(home, claudeDefaultConfigDir)
	}
	return filepath.Join(dir, claudeUserSettingsFile)
}
