package main

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/antigravity"
)

// TestAgyReadsTheClaudeAllowlistAsANeutralGrant: each Claude Code entry a
// session pod's allowlist can carry maps to its grant; a disallowed name
// grants nothing; TodoWrite and the delegate tool are dropped; an
// unrestricted Bash or a name agy has no grant for is an error, never a
// guess.
func TestAgyReadsTheClaudeAllowlistAsANeutralGrant(t *testing.T) {
	for _, c := range []struct {
		name, allowed, disallowed string
		want                      antigravity.Policy
		wantErr                   string
	}{
		{"session default", defaultAllowedTools, defaultDisallowedTools, antigravity.Policy{ReadFiles: true, WriteFiles: true}, ""},
		{"cluster view", clusterViewAllowedTools, clusterViewDisallowed, antigravity.Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl", "gcloud"}}, ""},
		{"with the delegate tool", defaultAllowedTools + "," + delegateToolID, defaultDisallowedTools, antigravity.Policy{ReadFiles: true, WriteFiles: true}, ""},
		{"web search", "WebSearch", "", antigravity.Policy{WebSearch: true}, ""},
		{"read alone", "Read", "", antigravity.Policy{ReadFiles: true}, ""},
		{"disallowed wins", "Read,Write", "Write", antigravity.Policy{ReadFiles: true}, ""},
		{"a disallowed Bash drops its prefixes", "Bash(kubectl:*)", "Bash", antigravity.Policy{}, ""},
		{"spaces and empties", " Read , ,Write ", "", antigravity.Policy{ReadFiles: true, WriteFiles: true}, ""},
		{"unrestricted Bash", "Read,Bash", "", antigravity.Policy{}, "unrestricted Bash"},
		{"WebFetch", "WebFetch", "", antigravity.Policy{}, `"WebFetch" has no agy grant`},
		{"Edit", "Edit", "", antigravity.Policy{}, `"Edit" has no agy grant`},
		{"another MCP tool", "mcp__gke__get_cluster", "", antigravity.Policy{}, "has no agy grant"},
		{"a Bash pattern without :*", "Bash(kubectl get pods)", "", antigravity.Policy{}, "has no agy grant"},
	} {
		t.Run(c.name, func(t *testing.T) {
			got, err := agyPolicy(c.allowed, c.disallowed)
			if c.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), c.wantErr) {
					t.Fatalf("err %v, want one containing %q", err, c.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got.ReadFiles != c.want.ReadFiles || got.WriteFiles != c.want.WriteFiles || got.WebSearch != c.want.WebSearch || !slices.Equal(got.ShellPrefixes, c.want.ShellPrefixes) {
				t.Errorf("policy %+v, want %+v", got, c.want)
			}
		})
	}
}

// TestAnAgyWorkerRefusesAnAllowlistOverrideItCantGrant: A2A_ALLOWED_TOOLS is
// the one input that can name a tool agy has no grant for, so the selector
// refuses it at startup instead of starting with nothing granted.
func TestAnAgyWorkerRefusesAnAllowlistOverrideItCantGrant(t *testing.T) {
	base := map[string]string{lib.EnvHarness: "antigravity", lib.EnvHarnessUnisolated: "allow", "A2A_HARNESS_PATH": "/bin/agy"}
	with := func(k, v string) func(string) string {
		return func(key string) string {
			if key == k {
				return v
			}
			return base[key]
		}
	}
	if _, _, _, err := selectHarness(with("A2A_ALLOWED_TOOLS", "Read,Bash")); err == nil || !strings.Contains(err.Error(), "A2A_ALLOWED_TOOLS") {
		t.Errorf("an override with an unrestricted Bash: err %v", err)
	}
	if _, _, _, err := selectHarness(with("A2A_ALLOWED_TOOLS", "Read,Bash(kubectl:*)")); err != nil {
		t.Errorf("a grantable override refused: %v", err)
	}
	// Claude Code is unaffected: its override is its own business.
	claude := func(key string) string {
		if key == "A2A_ALLOWED_TOOLS" {
			return "Read,Bash"
		}
		return ""
	}
	if _, _, _, err := selectHarness(claude); err != nil {
		t.Errorf("Claude Code refused an override: %v", err)
	}
}

// TestEveryHarnessGrantsExactlyTheMappedTools (an enum-flip precondition,
// #6711): for each session-pod configuration, the agy argv's --settings is
// the grant read from the very allowlist Claude Code's argv carries, and the
// tools agy may be offered are exactly the mapped set, never an ungated one.
func TestEveryHarnessGrantsExactlyTheMappedTools(t *testing.T) {
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })
	t.Setenv("A2A_HARNESS_CMD", "")
	t.Setenv("A2A_HARNESS_PATH", "/bin/harness")
	t.Setenv("A2A_HARNESS_EXTRA_ARGS", "")
	t.Setenv("A2A_ALLOWED_TOOLS", "")
	t.Setenv("A2A_WORKDIR", "/scratch")
	for _, c := range []struct {
		name, clusterView, delegate string
		tools                       []string
	}{
		{"default", "", "", []string{"read_file", "write_file"}},
		{"no delegate", "", "off", []string{"read_file", "write_file"}},
		{"cluster view", "true", "", []string{"read_file", "run_command", "write_file"}},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Setenv(lib.EnvClusterView, c.clusterView)
			t.Setenv(lib.EnvDelegateTool, c.delegate)
			activeHarness = claudeBuilder{}
			claudeArgv := harnessCommand()
			activeHarness = agyBuilder{}
			agyArgv := harnessCommand()

			flag := func(argv []string, f string) string {
				if i := slices.Index(argv, f); i >= 0 && i+1 < len(argv) {
					return argv[i+1]
				}
				return ""
			}
			p, err := agyPolicy(flag(claudeArgv, "--allowedTools"), flag(claudeArgv, "--disallowedTools"))
			if err != nil {
				t.Fatalf("Claude Code's allowlist has no agy grant: %v", err)
			}
			g, err := p.Render("/scratch")
			if err != nil {
				t.Fatal(err)
			}
			if !slices.Equal(g.Tools, c.tools) {
				t.Errorf("agy may be offered %q, want %q", g.Tools, c.tools)
			}
			if got := flag(agyArgv, agySettingsFlag); got != g.Settings() {
				t.Errorf("agy argv settings %s, want %s", got, g.Settings())
			}
			var s map[string]map[string][]string
			if err := json.Unmarshal([]byte(flag(agyArgv, agySettingsFlag)), &s); err != nil || len(s) != 1 {
				t.Errorf("agy settings %q: want one key, permissions (%v)", flag(agyArgv, agySettingsFlag), err)
			}
			for _, a := range agyArgv {
				if a == "--dangerously-skip-permissions" {
					t.Error("agy argv skips permissions")
				}
			}
		})
	}
}

// TestAProfileWorkersAgyArgvGrantsNothing: a profile worker's base argv has
// no tool policy of its own (the persona supplies it, #6713), so agy's
// rendering there denies every category.
func TestAProfileWorkersAgyArgvGrantsNothing(t *testing.T) {
	g := agyGrant(harnessSpec{path: "agy"})
	if len(g.Tools) != 0 || len(g.Allow) != 0 {
		t.Errorf("a base argv grants tools %q allow %q", g.Tools, g.Allow)
	}
	for _, d := range []string{"read_file(*)", "write_file(*)", "command(*)"} {
		if !slices.Contains(g.Deny, d) {
			t.Errorf("deny %q missing from %q", d, g.Deny)
		}
	}
}
