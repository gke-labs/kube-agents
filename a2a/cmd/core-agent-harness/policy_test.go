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
	// The persona shell's policy, whose turn runs in the mode that doesn't
	// refuse an unmatched call: the plugin still refuses an ungranted tool
	// before it runs.
	persona := coreagentwire.Policy{Tools: []string{"bash", "read_file"}, Allow: []string{"bash:*", "read_file:/scratch/*"}, AnyShellCommand: true}
	for _, policy := range []coreagentwire.Policy{{Tools: []string{"read_file"}}, persona} {
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
			t.Run(fmt.Sprintf("%s, any shell command %v", c.name, policy.AnyShellCommand), func(t *testing.T) {
				_, opts, plugins, err := policySetup(policy, mcp, "/scratch")
				if err != nil {
					t.Fatal(err)
				}
				if policy.AnyShellCommand != (opts.Mode == permissions.ModeYolo) {
					t.Fatalf("mode %q for any-shell-command %v", opts.Mode, policy.AnyShellCommand)
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
		// A prefix grant is the real boundary a session pod has: a
		// wrapper that would run a granted command (or anything) through
		// an interpreter or launcher never matches it.
		"bash -c 'kubectl get pods'":           false,
		"bash -c 'kubectl get pods | sh'":      false,
		"python3 -c 'import os; os.system(1)'": false,
		"env kubectl get pods":                 false,
		"xargs kubectl get pods":               false,
		"nohup kubectl get pods":               false,
		"timeout 5 kubectl get pods":           false,
		"command kubectl get pods":             false,
		"exec kubectl get pods":                false,
	} {
		if got := gate.CheckBash(context.Background(), cmd) == nil; got != want {
			t.Errorf("CheckBash(%q) allowed=%v, want %v", cmd, got, want)
		}
	}
}

// TestAPersonaShellIsInEffectUnrestricted: a persona's shell grant
// (toolgrant.Policy.Shell, which Claude Code grants as an unrestricted Bash)
// through core-agent's real gate. Top-level composition is refused and the
// verb checks hold, but a single command may be an interpreter that
// composes anything (`bash -c`, `python3 -c`), and passes: the grant is in
// effect an unrestricted shell, as on Claude Code, bounded by the sandbox
// and the broker. Pinned both ways so neither is misread.
func TestAPersonaShellIsInEffectUnrestricted(t *testing.T) {
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
		"bash -c 'kubectl get pods | jq . > /scratch/o; id'":                          true,
		"python3 -c 'import os; os.system(\"id\")'":                                   true,
		"cat /etc/passwd":                                                             true,
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

// TestEachRefusedCallLogsOneLineWithoutItsArguments: every layer's refusal,
// the plugin's and the gate's, logs one "tool refused:" line naming the
// tool and why, and never the command or path the gate's error quotes. An
// error that isn't a refusal logs nothing.
func TestEachRefusedCallLogsOneLineWithoutItsArguments(t *testing.T) {
	g, err := toolgrant.Policy{ReadFiles: true, ShellPrefixes: []string{"kubectl"}}.CoreAgent("/scratch")
	if err != nil {
		t.Fatal(err)
	}
	g.Deny = append(g.Deny, "bash:kubectl delete*")
	_, opts, plugins, err := policySetup(coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: g.Deny}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	gate := permissions.New(opts)
	_, pluginErr := plugins[0].BeforeToolCallback()(nil, fakeTool("edit_file"), nil)
	for _, c := range []struct {
		name, tool string
		err        error
		want       string
	}{
		{"the plugin", "edit_file", pluginErr, `tool refused: "edit_file" (not in the turn's grant)`},
		{"no allow rule", "bash", gate.CheckBash(context.Background(), "gcloud secrets-xyz list"), `tool refused: "bash" (no allow rule matches)`},
		{"a deny rule", "bash", gate.CheckBash(context.Background(), "kubectl delete pod secret-pod"), `tool refused: "bash" (a deny rule matches)`},
		{"a newline in a model-made name", "x\nforged", fmt.Errorf("tool 'x' not found.\nAvailable tools: a"), `tool refused: "x\nforged" (no such tool)`},
	} {
		t.Run(c.name, func(t *testing.T) {
			if c.err == nil {
				t.Fatal("no refusal to log")
			}
			var b strings.Builder
			logRefusal(&b, c.tool, "", c.err)
			if got := strings.TrimSuffix(b.String(), "\n"); got != c.want {
				t.Errorf("logged %q, want %q", got, c.want)
			}
			for _, leak := range []string{"secret", "xyz"} {
				if strings.Contains(b.String(), leak) {
					t.Errorf("the line carries the call's arguments: %q", b.String())
				}
			}
		})
	}
	var b strings.Builder
	logRefusal(&b, "read_file", "", fmt.Errorf("open /scratch/missing: no such file or directory"))
	if b.Len() != 0 {
		t.Errorf("a tool error that isn't a refusal logged %q", b.String())
	}
}

// TestThePluginLogsEveryToolError: the before-tool plugin carries the error
// callback, writing to refusalLog, and leaves the error as the result.
func TestThePluginLogsEveryToolError(t *testing.T) {
	var b strings.Builder
	prev := refusalLog
	refusalLog = &b
	defer func() { refusalLog = prev }()
	_, _, plugins, err := policySetup(coreagentwire.Policy{Tools: []string{"read_file"}}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	cb := plugins[0].OnToolErrorCallback()
	if cb == nil {
		t.Fatal("the plugin has no tool-error callback")
	}
	res, cbErr := cb(nil, fakeTool("bash"), map[string]any{"command": "id"}, fmt.Errorf("bash requires an allowlist entry in 'allow' mode: %q", "id"))
	if res != nil || cbErr != nil {
		t.Errorf("the callback replaced the error: %v, %v", res, cbErr)
	}
	if got := b.String(); got != "tool refused: \"bash\" (no allow rule matches)\n" {
		t.Errorf("logged %q", got)
	}
}

// namedTool is a tool that is only a name, for callbacks that read only
// that.
type namedTool string

func (n namedTool) Name() string        { return string(n) }
func (n namedTool) Description() string { return "" }
func (n namedTool) IsLongRunning() bool { return false }

func fakeTool(name string) adktool.Tool { return namedTool(name) }

// TestShellCommandsReadsEitherRuleForm: the commands a grant names come from
// a bare rule or its with-arguments form alike, and "*" stands for any.
func TestShellCommandsReadsEitherRuleForm(t *testing.T) {
	for _, c := range []struct {
		allow []string
		want  []string
	}{
		{[]string{"bash:kubectl", "bash:kubectl *", "read_file:/w/*"}, []string{"kubectl"}},
		{[]string{"bash:gcloud *"}, []string{"gcloud"}},
		{[]string{"bash:*"}, []string{"*"}},
		{[]string{"read_file:/w/*"}, nil},
	} {
		if got := shellCommands(c.allow); !slices.Equal(got, c.want) {
			t.Errorf("shellCommands(%q) = %q, want %q", c.allow, got, c.want)
		}
	}
}

// TestAPersonaShellRunsAnyCommand: a persona's shell grant
// (Policy.AnyShellCommand) runs any command, compound ones included, as
// Claude Code's Bash does, through core-agent's real gate: the decision
// run's refused commands among them (a skill script whose --body quotes
// markdown backticks, a multi-line python3 -c). Deny rules and core-agent's
// built-in denylist still refuse what they match (a deny rule matches the
// whole command, so it narrows rather than bounds), and the registered
// tools stay the policy's.
func TestAPersonaShellRunsAnyCommand(t *testing.T) {
	g, err := toolgrant.Policy{ReadFiles: true, Shell: true}.CoreAgent("/scratch")
	if err != nil {
		t.Fatal(err)
	}
	if !g.AnyShellCommand {
		t.Fatal("a persona's shell grant doesn't ask for any command")
	}
	p := coreagentwire.Policy{Tools: g.Builtins, Allow: g.Allow, Deny: append(slices.Clone(g.Deny), "bash:kubectl delete*"), AnyShellCommand: g.AnyShellCommand}
	b, opts, _, err := policySetup(p, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	if b.WriteFile || b.EditFile || b.FetchURL || !b.Bash {
		t.Errorf("registered built-ins %+v, want the policy's only", b)
	}
	gate := permissions.New(opts)
	const vcs = "python3 /var/run/kube-agents/harness-config/config-task-0016b23ba04607c7-1159668367/skills/version-control/scripts/vcs.py"
	for cmd, want := range map[string]bool{
		vcs + " proposal create --repo gke-agentic/kube-agents-evals-3-infra --title \"Add PodDisruptionBudget for checkout-gateway in seeded-reliability\" --body \"Adds a policy/v1 PodDisruptionBudget (`checkout-gateway-pdb`) in `clusters/seeded-a/seeded-reliability/checkout-gateway-pdb.yaml` selecting pods with `app: checkout-gateway` and setting `maxUnavailable: 1` so at least one of the two `checkout-gateway` replicas remains available during voluntary disruptions.\"": true,
		vcs + " proposal create --repo r --title t --body \"line one\nline two with $VAR\"":                             true,
		"python3 -c \"\nimport sys\nsys.path.insert(0, '/var/run/x/scripts')\nimport vcs_client\nprint(vcs_client)\n\"": true,
		"kubectl get deployment d -o yaml && kubectl get pdb -A && kubectl get pods -n x":                               true,
		"kubectl get pods -A | grep -v Running > /scratch/out 2>&1":                                                     true,
		"kubectl delete pod p": false,
		// A deny rule matches the command as a whole, so a compound that
		// starts with something else passes it: deny rules narrow, they are
		// not this shell's boundary (the sandbox and the broker are).
		"echo x; kubectl delete pod p": true,
		"rm -rf /":                     false,
	} {
		if got := gate.CheckBash(context.Background(), cmd) == nil; got != want {
			t.Errorf("CheckBash(%q) allowed=%v, want %v", cmd, got, want)
		}
	}
}

// TestAnyShellCommandNeedsTheShellAndNoWrite: a policy that runs any shell
// command must grant the shell, and may not grant a file write, whose
// workspace scope that mode drops.
func TestAnyShellCommandNeedsTheShellAndNoWrite(t *testing.T) {
	for name, p := range map[string]coreagentwire.Policy{
		"no shell": {Tools: []string{"read_file"}, AnyShellCommand: true},
		"a write":  {Tools: []string{"bash", "write_file"}, Allow: []string{"bash:*", "write_file:/scratch/*"}, AnyShellCommand: true},
	} {
		if _, _, _, err := policySetup(p, nil, "/scratch"); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	_, opts, _, err := policySetup(coreagentwire.Policy{Tools: []string{"bash"}, Allow: []string{"bash:kubectl", "bash:kubectl *"}}, nil, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	if opts.Mode != permissions.ModeAllow {
		t.Errorf("a prefix-limited shell runs in mode %q, want allow", opts.Mode)
	}
}
