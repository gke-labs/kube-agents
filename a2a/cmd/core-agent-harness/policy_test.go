package main

import (
	"context"
	"fmt"
	"iter"
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
		if _, _, _, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file", bad}}, nil); err == nil {
			t.Errorf("tool %q accepted", bad)
		}
	}
	if _, _, _, err := policySetup(coreagentwire.Policy{Allow: []string{"read_file:["}}, nil); err == nil {
		t.Error("a malformed pattern accepted")
	}
}

// TestTheSetupIsAllowModeWithThePolicyPluginFirst: the gate runs in allow
// mode, and the tool-policy plugin is the first (here the only) plugin, so
// nothing earlier in the chain can run a tool before its check.
func TestTheSetupIsAllowModeWithThePolicyPluginFirst(t *testing.T) {
	_, opts, plugins, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file"}}, nil)
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
			b, opts, _, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil)
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
			_, opts, plugins, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file"}}, mcp)
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
