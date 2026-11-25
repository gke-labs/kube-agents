package main

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/toolgrant"
)

// TestASessionAllowlistReadsAsANeutralGrant: each Claude Code entry a
// session pod's allowlist can carry maps to its grant; a denied name grants
// nothing; TodoWrite and the delegate tool are dropped; an unrestricted
// Bash, a scoped deny or a name with no grant is an error.
func TestASessionAllowlistReadsAsANeutralGrant(t *testing.T) {
	for _, c := range []struct {
		name, allowed, disallowed string
		want                      toolgrant.Policy
		wantErr                   string
	}{
		{"session default", defaultAllowedTools, defaultDisallowedTools, toolgrant.Policy{ReadFiles: true, WriteFiles: true}, ""},
		{"cluster view", clusterViewAllowedTools, clusterViewDisallowed, toolgrant.Policy{ReadFiles: true, WriteFiles: true, ShellPrefixes: []string{"kubectl", "gcloud"}}, ""},
		{"with the delegate tool", defaultAllowedTools + "," + delegateToolID, defaultDisallowedTools, toolgrant.Policy{ReadFiles: true, WriteFiles: true}, ""},
		{"denied wins", "Read,Write", "Write", toolgrant.Policy{ReadFiles: true}, ""},
		{"a denied Bash drops its prefixes", "Bash(kubectl:*)", "Bash", toolgrant.Policy{}, ""},
		{"space-separated", "Read Write", "", toolgrant.Policy{ReadFiles: true, WriteFiles: true}, ""},
		{"a space inside a scoped entry", "Read Bash(gh pr list:*)", "", toolgrant.Policy{ReadFiles: true, ShellPrefixes: []string{"gh pr list"}}, ""},
		{"unrestricted Bash", "Read,Bash", "", toolgrant.Policy{}, "unrestricted Bash"},
		{"WebFetch", "WebFetch", "", toolgrant.Policy{}, `"WebFetch" has no grant`},
		{"Edit", "Edit", "", toolgrant.Policy{}, `"Edit" has no grant`},
		{"another MCP tool", "mcp__gke__get_cluster", "", toolgrant.Policy{}, "has no grant"},
		{"a scoped deny", "Bash(kubectl:*)", "Bash(kubectl delete:*)", toolgrant.Policy{}, "scoped disallowed"},
	} {
		t.Run(c.name, func(t *testing.T) {
			got, err := sessionToolPolicy(c.allowed, c.disallowed)
			if c.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), c.wantErr) {
					t.Fatalf("err %v, want one containing %q", err, c.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if got.ReadFiles != c.want.ReadFiles || got.WriteFiles != c.want.WriteFiles || got.WebSearch != c.want.WebSearch ||
				!slices.Equal(got.ShellPrefixes, c.want.ShellPrefixes) || len(got.MCP) != 0 {
				t.Errorf("policy %+v, want %+v", got, c.want)
			}
		})
	}
}

// TestASessionPodsCoreAgentGrantIsItsAllowlist: the wire policy for each
// session configuration. Read tools and write_file in the workspace by
// default, bash for the cluster view's prefixes, and never a tool outside
// them.
func TestASessionPodsCoreAgentGrantIsItsAllowlist(t *testing.T) {
	reads := []string{"glob", "grep", "list_dir", "read_file", "read_many_files", "stat", "view_file_outline"}
	for _, c := range []struct {
		name  string
		env   map[string]string
		tools []string
		shell bool
	}{
		{"default", nil, append(slices.Clone(reads), "write_file"), false},
		{"cluster view", map[string]string{lib.EnvClusterView: "true"}, append([]string{"bash"}, append(slices.Clone(reads), "write_file")...), true},
	} {
		t.Run(c.name, func(t *testing.T) {
			p, err := coreAgentSessionGrant(func(k string) string { return c.env[k] }, "/work")
			if err != nil {
				t.Fatal(err)
			}
			slices.Sort(c.tools)
			if !slices.Equal(p.Tools, c.tools) {
				t.Errorf("tools %q, want %q", p.Tools, c.tools)
			}
			if !slices.Contains(p.Allow, "write_file:/work/*") {
				t.Errorf("allow %q: the write rule must name the workdir", p.Allow)
			}
			if got := slices.Contains(p.Allow, "bash:kubectl *"); got != c.shell {
				t.Errorf("bash:kubectl * allowed %v, want %v", got, c.shell)
			}
		})
	}
}

// TestACoreAgentWorkerCarriesItsGrant: the selector hands the core-agent
// driver the session grant, so every turn's envelope carries it, and stops
// a worker whose grant won't map or render.
func TestACoreAgentWorkerCarriesItsGrant(t *testing.T) {
	base := map[string]string{lib.EnvHarness: "core-agent", lib.EnvHarnessUnisolated: "allow", "A2A_HARNESS_PATH": "/bin/core-agent-harness", "A2A_WORKDIR": "/work"}
	getenv := func(extra map[string]string) func(string) string {
		return func(k string) string {
			if v, ok := extra[k]; ok {
				return v
			}
			return base[k]
		}
	}
	_, driver, _, err := selectHarness(getenv(nil))
	if err != nil {
		t.Fatal(err)
	}
	line, err := driver.EncodeUser("hi")
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(line, &env); err != nil {
		t.Fatal(err)
	}
	// Without the cluster view the denylist drops a Bash override: nothing
	// shell-related is granted.
	if _, d, _, err := selectHarness(getenv(map[string]string{"A2A_ALLOWED_TOOLS": "Read,Bash"})); err != nil {
		t.Errorf("a denied Bash override refused: %v", err)
	} else if line, _ := d.EncodeUser("x"); strings.Contains(string(line), `"bash"`) {
		t.Errorf("a denied Bash override granted bash: %s", line)
	}
	if !slices.Contains(env.Policy.Tools, "read_file") || !slices.Contains(env.Policy.Allow, "write_file:/work/*") || slices.Contains(env.Policy.Tools, "bash") {
		t.Errorf("envelope policy %+v, want the session default's grant", env.Policy)
	}
	for name, extra := range map[string]map[string]string{
		// Under the cluster view Bash isn't denied, so these reach the
		// grant and are refused; without it the denylist drops them.
		"unrestricted Bash": {"A2A_ALLOWED_TOOLS": "Read,Bash", lib.EnvClusterView: "true"},
		"web search":        {"A2A_ALLOWED_TOOLS": "Read,WebSearch"},
		"bad prefix":        {"A2A_ALLOWED_TOOLS": "Bash(kubectl;sh:*)", lib.EnvClusterView: "true"},
		"relative workdir":  {"A2A_WORKDIR": "work"},
	} {
		if _, _, _, err := selectHarness(getenv(extra)); err == nil || !strings.Contains(err.Error(), "tool grant") {
			t.Errorf("%s: err %v, want a tool-grant refusal", name, err)
		}
	}
}
