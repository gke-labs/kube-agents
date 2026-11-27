package main

import (
	"encoding/json"
	"os"
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

// TestASessionPodsCoreAgentGrantMatchesItsClaudeArgv: for the session
// default and the cluster view, the core-agent grant offers what the argv
// harnessCommand gives Claude Code on the same pod: reads, the write, and
// exactly the granted Bash prefixes, nothing more. Two entries are named
// differences: TodoWrite, a planning aid with no capability, is dropped,
// and the delegate tool isn't offered until the session delegate server
// lands (when it does, this test's delegate line flips).
func TestASessionPodsCoreAgentGrantMatchesItsClaudeArgv(t *testing.T) {
	reads := []string{"glob", "grep", "list_dir", "read_file", "read_many_files", "stat", "view_file_outline"}
	for _, view := range []bool{false, true} {
		t.Run(map[bool]string{false: "default", true: "cluster view"}[view], func(t *testing.T) {
			t.Setenv("A2A_HARNESS_CMD", "")
			t.Setenv("A2A_ALLOWED_TOOLS", "")
			t.Setenv(lib.EnvDelegateTool, "")
			t.Setenv(lib.EnvClusterView, map[bool]string{false: "", true: "true"}[view])
			argv := harnessCommand()
			flag := func(name string) []string {
				i := slices.Index(argv, name)
				if i < 0 || i+1 >= len(argv) {
					t.Fatalf("argv %q has no %s", argv, name)
				}
				return splitTools(argv[i+1])
			}
			allowed, disallowed := flag("--allowedTools"), flag("--disallowedTools")
			// Claude Code's grant, read independently of sessionToolPolicy.
			var claudeRead, claudeWrite, claudeDelegate bool
			var claudePrefixes []string
			for _, a := range allowed {
				if slices.Contains(disallowed, a) {
					continue
				}
				switch {
				case slices.Contains([]string{"Read", "Glob", "Grep", "LS"}, a):
					claudeRead = true
				case a == "Write":
					claudeWrite = true
				case a == "TodoWrite":
				case a == delegateToolID:
					claudeDelegate = true
				case strings.HasPrefix(a, "Bash(") && strings.HasSuffix(a, ":*)"):
					claudePrefixes = append(claudePrefixes, strings.TrimSuffix(strings.TrimPrefix(a, "Bash("), ":*)"))
				default:
					t.Fatalf("Claude Code allowlist entry %q has no core-agent mapping in this test", a)
				}
			}
			if !claudeDelegate {
				t.Fatalf("argv %q: the session argv lost its delegate tool", argv)
			}
			p, err := coreAgentSessionGrant(os.Getenv, "/work")
			if err != nil {
				t.Fatal(err)
			}
			var gotPrefixes []string
			for _, a := range p.Allow {
				switch {
				case strings.HasPrefix(a, "bash:") && !strings.HasSuffix(a, " *"):
					gotPrefixes = append(gotPrefixes, strings.TrimPrefix(a, "bash:"))
					if !slices.Contains(p.Allow, a+" *") {
						t.Errorf("prefix rule %q has no form with arguments", a)
					}
				case strings.HasPrefix(a, "bash:"):
					if !slices.Contains(p.Allow, strings.TrimSuffix(a, " *")) {
						t.Errorf("shell rule %q has no bare prefix rule: it isn't a prefix grant", a)
					}
				case strings.HasSuffix(a, ":/work/*") && slices.Contains(p.Tools, strings.TrimSuffix(a, ":/work/*")):
				default:
					t.Errorf("allow rule %q is outside the Claude Code grant", a)
				}
			}
			var tools []string
			if claudeRead {
				tools = append(tools, reads...)
			}
			if claudeWrite {
				tools = append(tools, "write_file")
			}
			if len(claudePrefixes) > 0 {
				tools = append(tools, "bash")
			}
			slices.Sort(tools)
			slices.Sort(claudePrefixes)
			slices.Sort(gotPrefixes)
			if !slices.Equal(p.Tools, tools) || !slices.Equal(gotPrefixes, claudePrefixes) {
				t.Errorf("core-agent tools %q prefixes %q, Claude Code's %q prefixes %q", p.Tools, gotPrefixes, tools, claudePrefixes)
			}
			if view != (len(claudePrefixes) > 0) {
				t.Errorf("cluster view %v but Claude Code prefixes %q", view, claudePrefixes)
			}
			// The named difference: no delegate tool on core-agent yet.
			if p.AnyShellCommand {
				t.Errorf("a session's shell runs any command: %+v", p)
			}
			if slices.ContainsFunc(p.Allow, func(a string) bool { return strings.HasPrefix(a, "mcp:") }) {
				t.Errorf("policy %+v grants MCP; only the delegate server may add MCP", p)
			}
		})
	}
}

// TestACoreAgentProfileWorkerStartsWithNoSessionGrant: a profile worker's
// grant comes from its persona, so the selector gives its core-agent driver
// an empty one (every tool refused), never the session pod's allowlist,
// and a session allowlist the profile can't use doesn't stop it starting.
func TestACoreAgentProfileWorkerStartsWithNoSessionGrant(t *testing.T) {
	for name, extra := range map[string]map[string]string{
		"default":              nil,
		"cluster view":         {lib.EnvClusterView: "true"},
		"an unusable override": {"A2A_ALLOWED_TOOLS": "Read,Bash,WebFetch", lib.EnvClusterView: "true"},
	} {
		t.Run(name, func(t *testing.T) {
			env := map[string]string{
				lib.EnvProfileWorker: "true", lib.EnvProfileHarness: "core-agent", lib.EnvHarnessUnisolated: "allow",
				"A2A_HARNESS_PATH": "/bin/core-agent-harness", "A2A_WORKDIR": "/work",
			}
			for k, v := range extra {
				env[k] = v
			}
			_, d, _, err := selectHarness(func(k string) string { return env[k] })
			if err != nil {
				t.Fatal(err)
			}
			line, err := d.EncodeUser("hi")
			if err != nil {
				t.Fatal(err)
			}
			var e coreagentwire.Envelope
			if err := json.Unmarshal(line, &e); err != nil {
				t.Fatal(err)
			}
			if len(e.Policy.Tools) != 0 || len(e.Policy.Allow) != 0 {
				t.Errorf("a profile worker's envelope policy %+v, want an empty grant", e.Policy)
			}
		})
	}
}
