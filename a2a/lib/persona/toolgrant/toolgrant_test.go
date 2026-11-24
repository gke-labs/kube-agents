package toolgrant

import (
	"slices"
	"strings"
	"testing"
)

const ws = "/scratch/ws"

// TestEachGrantRegistersItsToolsAndRules: the core-agent mapping, grant by
// grant: the built-ins registered, their workspace allow rules, and a broad
// deny for each category not granted.
func TestEachGrantRegistersItsToolsAndRules(t *testing.T) {
	reads := []string{"glob", "grep", "list_dir", "read_file", "read_many_files", "stat", "view_file_outline"}
	readAllow := func() []string {
		var out []string
		for _, r := range reads {
			out = append(out, r+":/scratch/ws/*")
		}
		return out
	}
	readDeny := func() []string {
		var out []string
		for _, r := range reads {
			out = append(out, r+":*")
		}
		return out
	}
	for _, c := range []struct {
		name            string
		p               Policy
		builtins, allow []string
		deny            []string
		search          bool
	}{
		{"nothing", Policy{}, nil, nil, append([]string{"bash:*", "write_file:*"}, readDeny()...), false},
		{"read", Policy{ReadFiles: true}, reads, readAllow(), []string{"bash:*", "write_file:*"}, false},
		{"read and write", Policy{ReadFiles: true, WriteFiles: true}, append(slices.Clone(reads), "write_file"),
			append(readAllow(), "write_file:/scratch/ws/*"), []string{"bash:*"}, false},
		{"cluster view", Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl", "gcloud"}},
			append([]string{"bash"}, append(slices.Clone(reads), "write_file")...),
			append(append(readAllow(), "write_file:/scratch/ws/*"), "bash:kubectl *", "bash:gcloud *"), nil, false},
		{"web search alone", Policy{WebSearch: true}, nil, nil, append([]string{"bash:*", "write_file:*"}, readDeny()...), true},
	} {
		t.Run(c.name, func(t *testing.T) {
			g, err := c.p.CoreAgent(ws)
			if err != nil {
				t.Fatal(err)
			}
			slices.Sort(c.builtins)
			if !slices.Equal(g.Builtins, c.builtins) {
				t.Errorf("builtins %q, want %q", g.Builtins, c.builtins)
			}
			if !slices.Equal(g.Allow, c.allow) {
				t.Errorf("allow %q, want %q", g.Allow, c.allow)
			}
			gotDeny, wantDeny := slices.Clone(g.Deny), slices.Clone(c.deny)
			slices.Sort(gotDeny)
			slices.Sort(wantDeny)
			if !slices.Equal(gotDeny, wantDeny) {
				t.Errorf("deny %q, want %q", g.Deny, c.deny)
			}
			if g.GeminiGoogleSearch != c.search {
				t.Errorf("gemini search %v, want %v", g.GeminiGoogleSearch, c.search)
			}
		})
	}
}

// TestNoGrantRegistersAToolNoWorkerHas: whatever the policy, the built-ins
// that no grant covers (edits, deletes, URL fetch, alerts, plans, todo,
// json_query, scion status) are never registered.
func TestNoGrantRegistersAToolNoWorkerHas(t *testing.T) {
	g, err := Policy{ReadFiles: true, WriteFiles: true, WebSearch: true, ShellPrefixes: []string{"kubectl"},
		MCP: []MCPGrant{{Server: "a2a", Tools: []string{"delegate"}}}}.CoreAgent(ws)
	if err != nil {
		t.Fatal(err)
	}
	for _, never := range []string{"edit_file", "delete_file", "fetch_url", "alert", "wait_and_verify", "todo", "record_plan", "json_query", "sciontool_status"} {
		if slices.Contains(g.Builtins, never) || g.Permits(never) {
			t.Errorf("the widest grant registers %s", never)
		}
	}
	for _, b := range g.Builtins {
		if _, known := coreAgentBuiltins[b]; !known {
			t.Errorf("registered %s is not a classified core-agent built-in", b)
		}
	}
}

// TestPermitsIsTheGrantAndNothingElse: the before-tool check's view.
func TestPermitsIsTheGrantAndNothingElse(t *testing.T) {
	g, err := Policy{ReadFiles: true, MCP: []MCPGrant{{Server: "a2a", Tools: []string{"delegate"}}}}.CoreAgent(ws)
	if err != nil {
		t.Fatal(err)
	}
	for _, ok := range []string{"read_file", "grep", "a2a_delegate"} {
		if !g.Permits(ok) {
			t.Errorf("Permits(%q) false", ok)
		}
	}
	for _, no := range []string{"", "bash", "write_file", "a2a_", "a2a_other", "a2a", "delegate", "other_delegate", "spawn_agent", "stop_agent", "read_file "} {
		if g.Permits(no) {
			t.Errorf("Permits(%q) true", no)
		}
	}
}

// TestAnAmbiguousMCPToolNameIsRefused: core-agent's <server>_<tool> naming
// can make two grants one name; such a grant is refused.
func TestAnAmbiguousMCPToolNameIsRefused(t *testing.T) {
	for name, p := range map[string]Policy{
		"two servers meet": {MCP: []MCPGrant{{Server: "a", Tools: []string{"b_c"}}, {Server: "a_b", Tools: []string{"c"}}}},
		"meets a built-in": {ReadFiles: true, MCP: []MCPGrant{{Server: "read", Tools: []string{"file"}}}},
	} {
		if _, err := p.CoreAgent(ws); err == nil || !strings.Contains(err.Error(), "ambiguous") {
			t.Errorf("%s: err %v", name, err)
		}
	}
}

// TestBadInputsAreRefused: a workspace, shell prefix or MCP name that
// would land in a rule or a tool name unchecked is refused.
func TestBadInputsAreRefused(t *testing.T) {
	for _, bad := range []string{"", "scratch", "/", "/scratch/..", "/scratch/ws/", "/scratch/*", "/scratch/w?", "/scratch/[w]", "/scratch/a:b"} {
		if _, err := (Policy{ReadFiles: true}).CoreAgent(bad); err == nil {
			t.Errorf("workspace %q accepted", bad)
		}
	}
	for _, bad := range []string{"", "*", "kubectl; rm", "kubectl | sh", "$(x)", "/bin/sh", "kubectl  get", " kubectl"} {
		if _, err := (Policy{ShellPrefixes: []string{bad}}).CoreAgent(ws); err == nil {
			t.Errorf("prefix %q accepted", bad)
		}
	}
	for name, m := range map[string]MCPGrant{
		"empty server":  {Server: "", Tools: []string{"t"}},
		"dashed server": {Server: "a-b", Tools: []string{"t"}},
		"dotted tool":   {Server: "a", Tools: []string{"t.x"}},
		"whole server":  {Server: "a"},
		"empty tool":    {Server: "a", Tools: []string{""}},
	} {
		if _, err := (Policy{MCP: []MCPGrant{m}}).CoreAgent(ws); err == nil {
			t.Errorf("%s accepted", name)
		}
	}
	if _, err := (Policy{MCP: []MCPGrant{{Server: "a", Tools: []string{"t"}}, {Server: "a", Tools: []string{"u"}}}}).CoreAgent(ws); err == nil {
		t.Error("a server granted twice accepted")
	}
}

// TestCoreAgentModeIsAllow: the one mode a worker runs in.
func TestCoreAgentModeIsAllow(t *testing.T) {
	if CoreAgentMode != "allow" {
		t.Errorf("mode %q", CoreAgentMode)
	}
}
