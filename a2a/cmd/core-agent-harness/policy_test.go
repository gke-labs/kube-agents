package main

import (
	"context"
	"fmt"
	"iter"
	"reflect"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	adkagent "google.golang.org/adk/v2/agent"
	adkmodel "google.golang.org/adk/v2/model"
	adktool "google.golang.org/adk/v2/tool"
	"google.golang.org/adk/v2/tool/functiontool"
	"google.golang.org/genai"

	"github.com/go-steer/core-agent/v3/pkg/agent"
	"github.com/go-steer/core-agent/v3/pkg/config"
	"github.com/go-steer/core-agent/v3/pkg/permissions"
	"github.com/go-steer/core-agent/v3/pkg/tools"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// TestEveryCoreAgentBuiltinIsClassified: toolgrant's table names every
// built-in core-agent has. A core-agent upgrade that adds one fails here, so
// a new tool is classified before any worker could be offered it.
func TestEveryCoreAgentBuiltinIsClassified(t *testing.T) {
	got, want := tools.BuiltinToolNames(), toolgrant.CoreAgentBuiltinNames()
	slices.Sort(got)
	if !slices.Equal(got, want) {
		t.Errorf("core-agent's built-ins %q, toolgrant classifies %q", got, want)
	}
}

// TestAPolicyMayRegisterOnlyGrantableBuiltins: a tool name the grant table
// doesn't allow, or core-agent doesn't have, refuses the turn's setup.
func TestAPolicyMayRegisterOnlyGrantableBuiltins(t *testing.T) {
	for _, bad := range []string{"edit_file", "delete_file", "fetch_url", "alert", "todo", "json_query", "record_plan",
		"wait_and_verify", "sciontool_status", "spawn_agent", "stop_agent", "nonsense", ""} {
		if _, _, _, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file", bad}}, nil, "/scratch"); err == nil {
			t.Errorf("tool %q accepted", bad)
		}
	}
	if _, _, _, err := policySetup(coreagentwire.Policy{Allow: []string{"read_file:["}}, nil, "/scratch"); err == nil {
		t.Error("a malformed pattern accepted")
	}
}

// TestTheSetupIsAllowModeWithThePolicyPluginFirst: the gate runs in allow
// mode, and the tool-policy plugin is the first (here the only) plugin, so
// nothing earlier in the chain can run a tool before its check.
func TestTheSetupIsAllowModeWithThePolicyPluginFirst(t *testing.T) {
	_, opts, plugins, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file"}}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	if opts.Mode != permissions.ModeAllow {
		t.Errorf("mode %q, want allow", opts.Mode)
	}
	if len(plugins) == 0 || plugins[0].Name() != toolPolicyPluginName || plugins[0].BeforeToolCallback() == nil {
		t.Fatalf("plugins %v: the tool-policy plugin with a before-tool callback must come first", plugins)
	}
}

// TestTheRegisteredToolsAreExactlyThePolicys: built in-process through
// core-agent's own tools.Build, the registry holds the policy's tools and
// nothing else: no spawn or background tool, no tool the grant left out.
func TestTheRegisteredToolsAreExactlyThePolicys(t *testing.T) {
	for _, c := range []struct {
		name string
		p    toolgrant.Policy
	}{
		{"nothing", toolgrant.Policy{}},
		{"session default", toolgrant.Policy{ReadFiles: true, WriteFiles: true}},
		{"cluster view", toolgrant.Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl", "gcloud"}}},
	} {
		t.Run(c.name, func(t *testing.T) {
			g, err := c.p.CoreAgent("/scratch")
			if err != nil {
				t.Fatal(err)
			}
			b, opts, _, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil, "/scratch")
			if err != nil {
				t.Fatal(err)
			}
			reg, err := tools.Build(&config.Config{}, permissions.New(opts), "", b)
			if err != nil {
				t.Fatal(err)
			}
			var names []string
			for _, tl := range reg.Tools {
				names = append(names, tl.Name())
			}
			slices.Sort(names)
			if !slices.Equal(names, g.Builtins) {
				t.Errorf("registered %q, want exactly %q", names, g.Builtins)
			}
		})
	}
}

// fakeLLM answers each model call from a script: a function call, then text.
type fakeLLM struct {
	mu    sync.Mutex
	calls []*genai.FunctionCall
	n     int
}

func (*fakeLLM) Name() string { return "fake" }

func (f *fakeLLM) GenerateContent(_ context.Context, _ *adkmodel.LLMRequest, _ bool) iter.Seq2[*adkmodel.LLMResponse, error] {
	f.mu.Lock()
	i := f.n
	f.n++
	f.mu.Unlock()
	return func(yield func(*adkmodel.LLMResponse, error) bool) {
		part := &genai.Part{Text: "done"}
		if i < len(f.calls) {
			part = &genai.Part{FunctionCall: f.calls[i]}
		}
		yield(&adkmodel.LLMResponse{Content: &genai.Content{Role: "model", Parts: []*genai.Part{part}}, TurnComplete: true}, nil)
	}
}

// canary is a tool that records whether it ran.
func canary(t *testing.T, name string, ran *atomic.Int32) adktool.Tool {
	t.Helper()
	tl, err := functiontool.New(functiontool.Config{Name: name, Description: "canary"},
		func(adkagent.Context, map[string]any) (map[string]any, error) {
			ran.Add(1)
			return map[string]any{"ok": true}, nil
		})
	if err != nil {
		t.Fatal(err)
	}
	return tl
}

// TestAToolOutsideTheGrantIsRefusedBeforeItRuns: end to end through
// core-agent's agent and ADK's runner, with a fake model. A tool that is
// registered but outside the turn's grant (a stand-in for anything that
// reaches the model by another path), an MCP tool of a granted server that
// the grant doesn't name, and either of them called with nil arguments,
// never run. A granted MCP tool, with or without arguments, does.
func TestAToolOutsideTheGrantIsRefusedBeforeItRuns(t *testing.T) {
	mcp := []coreagentwire.MCPServer{{Name: "srv", Tools: []string{"granted"}}}
	for _, c := range []struct {
		name    string
		call    *genai.FunctionCall
		wantRan bool
	}{
		{"registered but ungranted", &genai.FunctionCall{Name: "extra", Args: map[string]any{"x": 1}}, false},
		{"registered but ungranted, nil args", &genai.FunctionCall{Name: "extra"}, false},
		{"an MCP tool the grant doesn't name", &genai.FunctionCall{Name: "srv_other", Args: map[string]any{}}, false},
		{"a granted MCP tool", &genai.FunctionCall{Name: "srv_granted", Args: map[string]any{"q": "x"}}, true},
		{"a granted MCP tool, nil args", &genai.FunctionCall{Name: "srv_granted"}, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, opts, plugins, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file"}}, mcp, "/scratch")
			if err != nil {
				t.Fatal(err)
			}
			var extra, other, granted atomic.Int32
			llm := &fakeLLM{calls: []*genai.FunctionCall{c.call}}
			a, err := agent.New(llm,
				agent.WithMode(agent.ModeAutonomous),
				agent.WithGate(permissions.New(opts)),
				agent.WithTools([]adktool.Tool{canary(t, "extra", &extra), canary(t, "srv_other", &other), canary(t, "srv_granted", &granted)}),
				agent.WithPlugins(plugins...))
			if err != nil {
				t.Fatal(err)
			}
			var refusal bool
			for ev, err := range a.Run(context.Background(), "go") {
				if err != nil {
					break
				}
				if ev == nil || ev.Content == nil {
					continue
				}
				for _, p := range ev.Content.Parts {
					if p.FunctionResponse != nil && strings.Contains(strings.ToLower(fmtAny(p.FunctionResponse.Response)), "not in this turn's grant") {
						refusal = true
					}
				}
			}
			ran := extra.Load() + other.Load() + granted.Load()
			if c.wantRan && granted.Load() != 1 {
				t.Errorf("the granted tool ran %d times, want 1", granted.Load())
			}
			if !c.wantRan && ran != 0 {
				t.Errorf("an ungranted tool ran (extra %d, other %d, granted %d)", extra.Load(), other.Load(), granted.Load())
			}
			if !c.wantRan && !refusal {
				t.Error("no refusal reached the model as the tool's result")
			}
		})
	}
}

func fmtAny(v any) string { return fmt.Sprint(v) }

// TestEveryBuiltinHasASwitch: the harness can turn on any core-agent
// built-in, so toolgrant's table alone decides which a worker gets.
func TestEveryBuiltinHasASwitch(t *testing.T) {
	var b tools.BuiltinTools
	for _, name := range tools.BuiltinToolNames() {
		if builtinSwitch(&b, name) == nil {
			t.Errorf("%s has no switch", name)
		}
	}
}

// TestTheGateRunsExactlyTheGrantedShell: through core-agent's real gate,
// in allow mode, a granted prefix or skill script runs as a single literal
// command, with or without arguments, and nothing else does: no chaining,
// pipe, redirection, env prefix, expansion, interpreter word, sibling
// script or path escape.
func TestTheGateRunsExactlyTheGrantedShell(t *testing.T) {
	const script = "/var/lib/agent-persona/skills/stall/scripts/stall_report.py"
	g, err := toolgrant.Policy{ShellPrefixes: []string{"kubectl"}, Scripts: []string{script}, ScriptRoot: "/var/lib/agent-persona/skills"}.CoreAgent("/scratch")
	if err != nil {
		t.Fatal(err)
	}
	_, opts, _, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	gate := permissions.New(opts)
	for cmd, want := range map[string]bool{
		"kubectl":                       true,
		"kubectl get pods -n x":         true,
		script:                          true,
		script + " --namespace x":       true,
		"kubectl get pods; rm -rf /tmp": false,
		"kubectl get pods | sh":         false,
		"kubectl get pods > /tmp/out":   false,
		"kubectl $(id)":                 false,
		"FOO=1 kubectl get pods":        false,
		"kubectlx get pods":             false,
		"gcloud info":                   false,
		script + " --namespace x; id":   false,
		script + " | sh":                false,
		// The trailing glob of "<path> *" can't smuggle a second command
		// through the argument part.
		script + " $(id)":         false,
		script + " `id`":          false,
		script + " ; x":           false,
		script + " && x":          false,
		script + " || x":          false,
		script + " & x":           false,
		script + " --out=$HOME":   false,
		script + " < /etc/passwd": false,
		"python3 " + script:       false,
		"python3 -c 'import os'":  false,
		script + "x":              false,
		"/var/lib/agent-persona/skills/stall/scripts/../../other/scripts/evil.py": false,
		"/var/lib/agent-persona/skills/other/scripts/evil.py":                     false,
		"sh -c id": false,
	} {
		if got := gate.CheckBash(context.Background(), cmd) == nil; got != want {
			t.Errorf("CheckBash(%q) allowed=%v, want %v", cmd, got, want)
		}
	}
}

// TestAPersonaShellRunsAnySimpleCommandAndNoCompound: a persona's shell
// grant (toolgrant.Policy.Shell, which Claude Code grants as an unrestricted
// Bash) through core-agent's real gate: any single simple command with a
// literal argv runs, a persona script and an interpreter included; a
// pipe, chain, redirection, expansion or substitution never does, and
// core-agent's own verb checks still hold. This is the narrowing against
// Claude Code the persona grant names.
func TestAPersonaShellRunsAnySimpleCommandAndNoCompound(t *testing.T) {
	g, err := toolgrant.Policy{Shell: true}.CoreAgent("/scratch")
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(g.Builtins, "bash") {
		t.Fatalf("builtins %q: a shell grant registers bash", g.Builtins)
	}
	_, opts, _, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	gate := permissions.New(opts)
	for cmd, want := range map[string]bool{
		"kubectl get pods -n x":                                                       true,
		"gcloud container clusters describe c --zone z":                               true,
		"/var/lib/agent-persona/skills/stall/scripts/stall_report.py --ns x":          true,
		"python3 /var/lib/agent-persona/skills/version-control/scripts/vcs.py status": true,
		"jq -r .items /scratch/pods.json":                                             true,
		"git status":                                                                  true,
		"kubectl get pods | jq .":                                                     false,
		"kubectl get pods; id":                                                        false,
		"kubectl get pods && id":                                                      false,
		"kubectl get pods > /scratch/out":                                             false,
		"kubectl get pods 2>&1":                                                       false,
		"kubectl $(id)":                                                               false,
		"kubectl `id`":                                                                false,
		"echo $HOME":                                                                  false,
		"(id)":                                                                        false,
		"find / -exec id ;":                                                           false,
		"git -c core.pager=id log":                                                    false,
	} {
		if got := gate.CheckBash(context.Background(), cmd) == nil; got != want {
			t.Errorf("CheckBash(%q) allowed=%v, want %v", cmd, got, want)
		}
	}
}

// TestAGrantedMCPToolPassesTheGateAndNoOtherDoes: through core-agent's real
// gate in allow mode, as it checks an MCP call (namespace "mcp", key
// "<server>_<tool>" plus a space and its JSON arguments), a granted MCP tool
// passes and a neighbour's name does not.
func TestAGrantedMCPToolPassesTheGateAndNoOtherDoes(t *testing.T) {
	g, err := toolgrant.Policy{MCP: []toolgrant.MCPGrant{{Server: "fake", Tools: []string{"a"}}}}.CoreAgent("/scratch")
	if err != nil {
		t.Fatal(err)
	}
	_, opts, _, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny},
		[]coreagentwire.MCPServer{{Name: "fake", Tools: []string{"a"}}}, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	gate := permissions.New(opts)
	// The keys as core-agent's summarizeRequest builds them: the name alone
	// for untyped nil args, "name null" for the typed nil map ADK passes,
	// otherwise "name <json>".
	for _, c := range []struct {
		tool, key string
		want      bool
	}{
		{"fake_a", "fake_a", true},
		{"fake_a", "fake_a null", true},
		{"fake_a", `fake_a {"q":"x"}`, true},
		{"fake_ab", "fake_ab", false},
		{"fake_ab", "fake_ab null", false},
		{"fake_ab", `fake_ab {"q":"x"}`, false},
		{"fake_b", "fake_b {}", false},
		{"other_a", "other_a {}", false},
	} {
		err := gate.CheckToolCallWithArgs(context.Background(), "mcp", c.tool, c.key, map[string]any{})
		if got := err == nil; got != c.want {
			t.Errorf("gate on %q: allowed=%v (%v), want %v", c.key, got, err, c.want)
		}
	}
}

// TestTheHarnessRechecksTheEnvelopesMCPNames: names that would collide with
// a built-in, a skill tool, each other, or break the name grammar are
// refused by the harness itself.
func TestTheHarnessRechecksTheEnvelopesMCPNames(t *testing.T) {
	for name, mcp := range map[string][]coreagentwire.MCPServer{
		"meets a built-in":   {{Name: "read", Tools: []string{"file"}}},
		"meets a skill tool": {{Name: "load", Tools: []string{"skill"}}},
		"two meet":           {{Name: "a", Tools: []string{"b_c"}}, {Name: "a_b", Tools: []string{"c"}}},
		"dashed":             {{Name: "a-b", Tools: []string{"c"}}},
		"named twice":        {{Name: "a", Tools: []string{"x"}}, {Name: "a", Tools: []string{"y"}}},
	} {
		if _, _, _, err := policySetup(coreagentwire.Policy{}, mcp, "/scratch"); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
}

// TestNoRegisteredToolIsAStreamingTool: ADK runs a streaming tool without
// any before-tool callback, so a worker registers none.
func TestNoRegisteredToolIsAStreamingTool(t *testing.T) {
	g, err := toolgrant.Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl"}}.CoreAgent("/scratch")
	if err != nil {
		t.Fatal(err)
	}
	b, opts, _, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	reg, err := tools.Build(&config.Config{}, permissions.New(opts), "", b)
	if err != nil {
		t.Fatal(err)
	}
	for _, tl := range reg.Tools {
		if isStreamingTool(tl) {
			t.Errorf("registered tool %s streams, past the before-tool callbacks", tl.Name())
		}
	}
	// The check itself recognises a streaming tool.
	st, err := functiontool.NewStreaming(functiontool.Config{Name: "streamer", Description: "s"},
		func(adkagent.Context, map[string]any) iter.Seq2[string, error] {
			return func(func(string, error) bool) {}
		})
	if err != nil {
		t.Fatal(err)
	}
	if !isStreamingTool(st) {
		t.Fatal("isStreamingTool doesn't recognise ADK's streaming tool")
	}
}

// isStreamingTool reports whether ADK would run tl through RunStream, the
// path that skips every before-tool callback (ADK's StreamingFunctionTool).
func isStreamingTool(tl adktool.Tool) bool {
	_, ok := tl.(interface {
		RunStream(adkagent.Context, any) iter.Seq2[string, error]
	})
	return ok
}

// TestEachGrantableBuiltinTurnsOnOnlyItsOwnSwitch: an empty policy offers
// nothing, and each built-in a grant may register turns on exactly its own
// BuiltinTools switch (the driver's floor check, kept for the table-driven
// policy).
func TestEachGrantableBuiltinTurnsOnOnlyItsOwnSwitch(t *testing.T) {
	b, _, _, err := policySetup(coreagentwire.Policy{}, nil, t.TempDir())
	if err != nil || b != (tools.BuiltinTools{}) {
		t.Fatalf("an empty policy offered %+v (err %v)", b, err)
	}
	for _, name := range toolgrant.CoreAgentBuiltinNames() {
		if !toolgrant.CoreAgentRegistrable(name) {
			continue
		}
		b, _, _, err := policySetup(coreagentwire.Policy{Tools: []string{name}}, nil, t.TempDir())
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		on := 0
		v := reflect.ValueOf(b)
		for i := 0; i < v.NumField(); i++ {
			if v.Field(i).Kind() == reflect.Bool && v.Field(i).Bool() {
				on++
			}
		}
		probe := b
		if err := probe.Disable(name); err != nil || on != 1 || probe != (tools.BuiltinTools{}) {
			t.Errorf("%s turned on %d switches, or not its own: %+v", name, on, b)
		}
	}
}
