package antigravity

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

const ws = "/scratch/ws"

// TestEachGrantMapsToItsToolsAndRules: the mapping table, grant by grant. A
// category the policy doesn't grant gets an explicit deny where agy would
// otherwise allow or could be told to allow, and its tools are not offered.
func TestEachGrantMapsToItsToolsAndRules(t *testing.T) {
	for _, c := range []struct {
		name        string
		p           Policy
		tools       []string
		allow, deny []string
	}{
		{"nothing", Policy{}, nil, nil, []string{"read_file(*)", "write_file(*)", "command(*)"}},
		{"read", Policy{ReadFiles: true}, []string{"read_file"}, nil, []string{"write_file(*)", "command(*)"}},
		{"read and write", Policy{ReadFiles: true, WriteFiles: true}, []string{"read_file", "write_file"}, []string{"write_file(/scratch/ws)"}, []string{"command(*)"}},
		{"web search alone", Policy{WebSearch: true}, []string{"search_web"}, nil, []string{"read_file(*)", "write_file(*)", "command(*)"}},
		{"cluster view", Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl", "gcloud"}},
			[]string{"read_file", "run_command", "write_file"}, []string{"write_file(/scratch/ws)", "command(kubectl)", "command(gcloud)"}, nil},
		{"a subcommand prefix", Policy{ShellPrefixes: []string{"gh pr list"}}, []string{"run_command"}, []string{"command(gh pr list)"}, []string{"read_file(*)", "write_file(*)"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			g, err := c.p.Render(ws)
			if err != nil {
				t.Fatal(err)
			}
			if !slices.Equal(g.Tools, c.tools) || !slices.Equal(g.Allow, c.allow) || !slices.Equal(g.Deny, c.deny) {
				t.Errorf("got tools %q allow %q deny %q\nwant tools %q allow %q deny %q", g.Tools, g.Allow, g.Deny, c.tools, c.allow, c.deny)
			}
		})
	}
}

// TestNoGrantOffersAnUngatedTool: no policy offers the model a tool agy runs
// without a permission check and no worker grants: the subagent tools,
// schedule, swarm, send_message, manage_task, run_workflow, edit_file and
// ask_question (which can't be answered headless).
func TestNoGrantOffersAnUngatedTool(t *testing.T) {
	never := []string{ToolDefineSubagent, ToolInvokeSubagent, ToolManageSubagents, ToolSchedule, ToolSwarm,
		ToolSendMessage, ToolManageTask, ToolRunWorkflow, ToolEditFile, ToolAskQuestion}
	all := Policy{ReadFiles: true, WriteFiles: true, WebSearch: true, ShellPrefixes: []string{"kubectl"}}
	g, err := all.Render(ws)
	if err != nil {
		t.Fatal(err)
	}
	for _, tool := range g.Tools {
		if slices.Contains(never, tool) {
			t.Errorf("the widest policy offers %s", tool)
		}
		if !slices.Contains(KnownTools, tool) {
			t.Errorf("offered tool %s is not one agy 1.5.13 has", tool)
		}
	}
}

// TestAShellPrefixIsABinaryAndItsSubcommandsOnly: a prefix carrying shell
// syntax, a path, a glob or a parenthesis is refused, not rendered into a
// rule agy might read differently or silently drop.
func TestAShellPrefixIsABinaryAndItsSubcommandsOnly(t *testing.T) {
	for _, bad := range []string{"", " ", "*", "kubectl; rm -rf /", "kubectl && sh", "kubectl | sh", "$(sh)", "`sh`",
		"/bin/sh", "kubectl get (pods)", "kubectl\tget", "kubectl  get", " kubectl", "kubectl "} {
		if _, err := (Policy{ShellPrefixes: []string{bad}}).Render(ws); err == nil {
			t.Errorf("prefix %q accepted", bad)
		}
	}
}

// TestTheWorkspaceIsAPlainAbsolutePath: the workspace lands inside a rule, so
// a relative path or one with rule syntax is refused.
func TestTheWorkspaceIsAPlainAbsolutePath(t *testing.T) {
	for _, bad := range []string{"", "scratch/ws", "/scratch/(ws)", "/scratch/*"} {
		if _, err := (Policy{WriteFiles: true}).Render(bad); err == nil {
			t.Errorf("workspace %q accepted", bad)
		}
	}
}

// TestTheSettingsAreOnlyPermissionsAndNeverAsk: agy rejects every --settings
// key but `permissions` (a hard error), and an ask rule headless is a
// disguised deny, so the rendered value has exactly that one key and no ask
// list, whatever the policy.
func TestTheSettingsAreOnlyPermissionsAndNeverAsk(t *testing.T) {
	for _, p := range []Policy{{}, {ReadFiles: true}, {ReadFiles: true, WriteFiles: true, WebSearch: true, ShellPrefixes: []string{"kubectl", "gcloud"}}} {
		g, err := p.Render(ws)
		if err != nil {
			t.Fatal(err)
		}
		var top map[string]map[string][]string
		if err := json.Unmarshal([]byte(g.Settings()), &top); err != nil {
			t.Fatalf("settings %s: %v", g.Settings(), err)
		}
		if len(top) != 1 || top["permissions"] == nil {
			t.Errorf("settings %s: want exactly the permissions key", g.Settings())
		}
		for k := range top["permissions"] {
			if k != "allow" && k != "deny" {
				t.Errorf("settings %s: permissions key %q", g.Settings(), k)
			}
		}
		if strings.Contains(g.Settings(), `"ask"`) {
			t.Errorf("settings %s carry an ask list", g.Settings())
		}
	}
}

// TestEveryRenderedRuleIsOneAgyReads: agy ignores a malformed rule without a
// word, so each rule the renderer emits matches agy's documented grammar.
func TestEveryRenderedRuleIsOneAgyReads(t *testing.T) {
	g, err := (Policy{WriteFiles: true, ShellPrefixes: []string{"kubectl", "gh pr list"}}).Render(ws)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range slices.Concat(g.Allow, g.Deny) {
		if !ruleGrammar.MatchString(r) {
			t.Errorf("rule %q", r)
		}
	}
	for _, bad := range []string{"command(touch", "command()", "ask(x)", "command(a(b))", "command(x) "} {
		if ruleGrammar.MatchString(bad) {
			t.Errorf("grammar accepts %q", bad)
		}
	}
}

// TestTheSettingsCarryEveryRule: the rendered value is the allow and deny
// rules exactly, so a rule the grant holds can't fall out on the way to agy.
func TestTheSettingsCarryEveryRule(t *testing.T) {
	for _, c := range []struct {
		p    Policy
		want string
	}{
		{Policy{}, `{"permissions":{"deny":["read_file(*)","write_file(*)","command(*)"]}}`},
		{Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl", "gcloud"}},
			`{"permissions":{"allow":["write_file(/scratch/ws)","command(kubectl)","command(gcloud)"]}}`},
		{Policy{ReadFiles: true}, `{"permissions":{"deny":["write_file(*)","command(*)"]}}`},
	} {
		g, err := c.p.Render(ws)
		if err != nil {
			t.Fatal(err)
		}
		if got := g.Settings(); got != c.want {
			t.Errorf("settings\n got %s\nwant %s", got, c.want)
		}
	}
}

// TestTheToolPolicyOffersOnlyTheGrant: the proxy's view of a grant. Known is
// agy's fixed set; Granted is the grant's tools and nothing else, and never a
// name agy doesn't have.
func TestTheToolPolicyOffersOnlyTheGrant(t *testing.T) {
	g, err := (Policy{ReadFiles: true, ShellPrefixes: []string{"kubectl"}}).Render(ws)
	if err != nil {
		t.Fatal(err)
	}
	tp := NewToolPolicy(g)
	for _, name := range KnownTools {
		if !tp.Known(name) {
			t.Errorf("Known(%s) false", name)
		}
		if want := name == ToolReadFile || name == ToolRunCommand; tp.Granted(name) != want {
			t.Errorf("Granted(%s) = %v, want %v", name, tp.Granted(name), want)
		}
	}
	for _, name := range []string{"", "call_mcp_tool", "view_file", "browser_click_element", "Read"} {
		if tp.Known(name) || tp.Granted(name) {
			t.Errorf("%q known=%v granted=%v", name, tp.Known(name), tp.Granted(name))
		}
	}
	if NewToolPolicy(Grant{Tools: []string{"not_a_tool"}}).Granted("not_a_tool") {
		t.Error("a grant naming an unknown tool granted it")
	}
}
