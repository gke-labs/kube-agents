package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/claudecode"
	"github.com/gke-labs/kube-agents/a2a/lib/persona/coreagent"
)

// parityRelay is a relay both renderings accept, so every persona's MCP
// servers are rendered.
var parityRelay = persona.Relay{URL: "http://relay.test", Command: "/bin/relay", Args: []string{"--target", "{url}"}}

// The Claude Code tool names the parity test reads, as claudecode maps the
// neutral built-ins.
var (
	parityClaudeRead  = []string{"Read", "Glob", "Grep", "LS", "NotebookRead"}
	parityClaudeShell = []string{"Bash", "BashOutput", "KillShell"}
	parityClaudeWrite = []string{"Write", "Edit", "MultiEdit", "NotebookEdit"}
)

// grantSummary is what a worker may do, in terms both harnesses' grants
// can be read into.
type grantSummary struct {
	Read, Write, Shell, WebSearch bool
	// MCP are "<server>.<tool>" entries, sorted.
	MCP []string
}

// claudeGrant reads a Claude Code persona rendering's allowlist and
// denylist. An entry it doesn't know fails the test: a new kind of grant
// on the Claude path must be mapped for core-agent before it ships.
func claudeGrant(t *testing.T, out persona.Output) grantSummary {
	t.Helper()
	var g grantSummary
	allows := func(names []string) bool {
		n := 0
		for _, name := range names {
			if slices.Contains(out.Allowed, name) {
				n++
			}
		}
		if n != 0 && n != len(names) {
			t.Fatalf("allowlist %q grants part of %q", out.Allowed, names)
		}
		return n == len(names)
	}
	g.Read, g.Shell, g.Write = allows(parityClaudeRead), allows(parityClaudeShell), allows(parityClaudeWrite)
	for _, a := range out.Allowed {
		switch {
		case slices.Contains(parityClaudeRead, a), slices.Contains(parityClaudeShell, a), slices.Contains(parityClaudeWrite, a):
		case a == "WebSearch":
			g.WebSearch = true
		case a == "Skill":
			// core-agent's skill tools are always offered with the
			// persona's skills.
		case strings.HasPrefix(a, "mcp__"):
			server, tool, ok := strings.Cut(strings.TrimPrefix(a, "mcp__"), "__")
			if !ok {
				t.Fatalf("allowlist entry %q grants a whole MCP server; core-agent lists tools", a)
			}
			g.MCP = append(g.MCP, server+"."+tool)
		default:
			t.Fatalf("allowlist entry %q has no core-agent mapping in this test", a)
		}
	}
	for _, w := range parityClaudeWrite {
		if !g.Write && !slices.Contains(out.Disallowed, w) {
			t.Fatalf("denylist %q doesn't refuse %s", out.Disallowed, w)
		}
	}
	slices.Sort(g.MCP)
	return g
}

// coreAgentGrant reads the wire policy a core-agent turn receives, and
// checks each grant has exactly the rules it should: the narrowings the
// persona grant names (workspace-scoped reads, a shell of simple commands
// only) are pinned here, so neither drifts.
func coreAgentGrant(t *testing.T, p coreagentwire.Policy, workdir string) grantSummary {
	t.Helper()
	g := grantSummary{WebSearch: p.WebSearch}
	var reads []string
	for _, name := range p.Tools {
		switch name {
		case "read_file", "read_many_files", "view_file_outline", "stat", "list_dir", "glob", "grep":
			reads = append(reads, name)
			if !slices.Contains(p.Allow, name+":"+workdir+"/*") {
				t.Errorf("%s has no workspace rule in %q", name, p.Allow)
			}
		case "write_file":
			g.Write = true
		case "bash":
			g.Shell = true
		default:
			t.Errorf("tool %q has no grant in this test", name)
		}
	}
	if len(reads) > 0 {
		if len(reads) != 7 {
			t.Errorf("read tools %q: a read grant registers all seven", reads)
		}
		g.Read = true
	}
	var bash []string
	for _, a := range p.Allow {
		switch {
		case strings.HasPrefix(a, "bash:"):
			bash = append(bash, a)
		case strings.HasPrefix(a, "mcp:"):
			name := strings.TrimPrefix(a, "mcp:")
			if strings.HasSuffix(name, " *") {
				if !slices.Contains(p.Allow, "mcp:"+strings.TrimSuffix(name, " *")) {
					t.Errorf("allow %q has no rule for the call without arguments", a)
				}
				continue
			}
			g.MCP = append(g.MCP, name)
		case slices.Contains(p.Tools, strings.TrimSuffix(a, ":"+workdir+"/*")) && a != "bash:"+workdir+"/*":
			// A registered file tool's workspace rule, checked above.
		default:
			t.Errorf("allow rule %q is outside every grant", a)
		}
	}
	// Every deny is a broad deny of a file or shell tool that isn't
	// registered.
	for _, d := range p.Deny {
		tool, pat, _ := strings.Cut(d, ":")
		if pat != "*" || slices.Contains(p.Tools, tool) || !slices.Contains([]string{"read_file", "read_many_files", "view_file_outline", "stat", "list_dir", "glob", "grep", "write_file", "bash"}, tool) {
			t.Errorf("deny rule %q isn't a broad deny of an unregistered file or shell tool", d)
		}
	}
	if g.Shell && !slices.Equal(bash, []string{"bash:*"}) {
		t.Errorf("shell rules %q, want only bash:*", bash)
	}
	if !g.Shell && len(bash) != 0 {
		t.Errorf("shell rules %q without the shell", bash)
	}
	slices.Sort(g.MCP)
	return g
}

// TestEveryShippedPersonaGrantsCoreAgentWhatItGrantsClaudeCode: for each
// persona under agents/, its core-agent grant equals its Claude Code
// grant: reads, writes, the shell, web search and every MCP tool. The
// shell and reads keep core-agent's narrowings (pinned in coreAgentGrant):
// a shell of simple commands only where Claude Code's Bash is
// unrestricted, and reads confined to the workspace.
func TestEveryShippedPersonaGrantsCoreAgentWhatItGrantsClaudeCode(t *testing.T) {
	dirs, err := filepath.Glob("../../agents/*/persona")
	if err != nil {
		t.Fatal(err)
	}
	if len(dirs) < 3 {
		t.Fatalf("found %d shipped personas, want the cluster, fleet-audit and tse ones at least", len(dirs))
	}
	const workdir = "/scratch"
	for _, dir := range dirs {
		t.Run(filepath.Base(filepath.Dir(dir)), func(t *testing.T) {
			p, err := persona.Load(dir)
			if err != nil {
				t.Fatal(err)
			}
			claude, err := claudecode.Adapter{}.Render(p, persona.Target{Relay: parityRelay})
			if err != nil {
				t.Fatal(err)
			}
			ca, err := coreagent.Adapter{}.Render(p, persona.Target{Relay: parityRelay})
			if err != nil {
				t.Fatal(err)
			}
			d, err := coreAgentProfileDriver(NewCoreAgentDriver(coreagentwire.Policy{}, nil), ca, workdir)
			if err != nil {
				t.Fatal(err)
			}
			line, err := d.EncodeUser("hi")
			if err != nil {
				t.Fatal(err)
			}
			var env coreagentwire.Envelope
			if err := json.Unmarshal(line, &env); err != nil {
				t.Fatal(err)
			}
			want, got := claudeGrant(t, claude), coreAgentGrant(t, env.Policy, workdir)
			wantMCP := make([]string, len(want.MCP))
			for i, m := range want.MCP {
				wantMCP[i] = strings.Replace(m, ".", "_", 1)
			}
			if got.Read != want.Read || got.Write != want.Write || got.Shell != want.Shell || got.WebSearch != want.WebSearch || !slices.Equal(got.MCP, wantMCP) {
				t.Errorf("core-agent grant %+v, Claude Code grant %+v (MCP as %q)", got, want, wantMCP)
			}
			// The servers the harness starts list exactly the granted
			// tools, so registration and the gate agree.
			var started []string
			for _, s := range ca.MCPServers {
				for _, tool := range s.Tools {
					started = append(started, s.Name+"."+tool)
				}
			}
			slices.Sort(started)
			if !slices.Equal(started, want.MCP) {
				t.Errorf("rendered MCP servers list %q, Claude Code grants %q", started, want.MCP)
			}
		})
	}
}

// TestAPersonaGrantThatDoesntMatchItsRenderingIsRefused: anything the
// manifest's tools and the rendered servers don't agree on is an error,
// never a guess.
func TestAPersonaGrantThatDoesntMatchItsRenderingIsRefused(t *testing.T) {
	srv := func(name string, tools ...string) persona.MCPServer {
		return persona.MCPServer{Name: name, Command: "/bin/relay", Tools: tools}
	}
	for _, c := range []struct {
		name    string
		out     persona.Output
		wantErr string
	}{
		{"an unknown built-in", persona.Output{Allowed: []string{"write_file"}}, "has no grant"},
		{"a tool on a server not rendered", persona.Output{Allowed: []string{"gke.get_cluster"}}, "has no grant"},
		{"a server rendered but not granted", persona.Output{Allowed: []string{"read_file"}, MCPServers: []persona.MCPServer{srv("dk", "a")}}, "no tool grants it"},
		{"a server tool outside a scoped grant", persona.Output{Allowed: []string{"dk.a"}, MCPServers: []persona.MCPServer{srv("dk", "a", "b")}}, `lists tool "b"`},
		{"the delegate server's name", persona.Output{Allowed: []string{"a2a"}, MCPServers: []persona.MCPServer{srv("a2a", "delegate")}}, "delegate server"},
		{"a server rendered twice", persona.Output{Allowed: []string{"dk"}, MCPServers: []persona.MCPServer{srv("dk", "a"), srv("dk", "b")}}, "rendered twice"},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, err := PersonaToolPolicy(c.out)
			if err == nil || !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("err %v, want one containing %q", err, c.wantErr)
			}
			if _, err := coreAgentProfileDriver(NewCoreAgentDriver(coreagentwire.Policy{}, nil), c.out, "/scratch"); err == nil {
				t.Error("the profile driver took a grant PersonaToolPolicy refuses")
			}
		})
	}
	got, err := PersonaToolPolicy(persona.Output{Allowed: []string{"read_file", "dk.a", "dk.b", "web_search"}, MCPServers: []persona.MCPServer{srv("dk", "a", "b")}})
	if err != nil {
		t.Fatal(err)
	}
	if !got.ReadFiles || got.WriteFiles || got.Shell || !got.WebSearch || len(got.MCP) != 1 || !slices.Equal(got.MCP[0].Tools, []string{"a", "b"}) {
		t.Errorf("policy %+v", got)
	}
	if _, err := coreAgentProfileDriver(NewCoreAgentDriver(coreagentwire.Policy{}, nil), persona.Output{Allowed: []string{"shell"}}, "scratch"); err == nil {
		t.Error("a relative workdir rendered")
	}
	if _, err := coreAgentProfileDriver(claudeDriver{}, persona.Output{Allowed: []string{"shell"}}, "/scratch"); err == nil {
		t.Error("a driver without a policy took one")
	}
}

// TestAProfileDriverKeepsItsHistory: the profile grant replaces only
// the policy; the history store is the driver's and stays shared.
func TestAProfileDriverKeepsItsHistory(t *testing.T) {
	base := NewCoreAgentDriver(coreagentwire.Policy{Tools: []string{"read_file"}}, nil)
	d, err := coreAgentProfileDriver(base, persona.Output{Allowed: []string{"shell"}}, "/scratch")
	if err != nil {
		t.Fatal(err)
	}
	cd, ok := d.(coreAgentDriver)
	if !ok || cd.conv == nil || cd.conv != base.(coreAgentDriver).conv {
		t.Fatalf("driver %#v: the history store must carry over", d)
	}
	if !slices.Equal(cd.Policy.Tools, []string{"bash"}) || slices.Equal(base.(coreAgentDriver).Policy.Tools, cd.Policy.Tools) {
		t.Errorf("policy tools %q (base %q)", cd.Policy.Tools, base.(coreAgentDriver).Policy.Tools)
	}
}

// TestACoreAgentProfileTurnCarriesThePersonasGrant: end to end, a core-agent
// profile worker with the cluster persona runs a dispatched task, and the
// envelope the harness reads on stdin carries the persona's grant (the
// shell, web search, developer_knowledge's two tools), not the session
// grant the pod's driver started with.
func TestACoreAgentProfileTurnCarriesThePersonasGrant(t *testing.T) {
	useRespawnScope(t)
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	root := t.TempDir()
	harness := stub(t, fmt.Sprintf(`
read first || exit 1
printf '%%s\n' "$first" > %s/envelope
echo '{"type":"init","session":"s1","model":"m"}'
echo '{"type":"result","text":"ok"}'
`, root))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	session := coreagentwire.Policy{Tools: []string{"read_file", "write_file"}, Allow: []string{"write_file:/scratch/*"}}
	cfg.Driver = NewCoreAgentDriver(session, nil)
	ln, addr := readyListener(t)
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{
			Tier: pwTier, ReadyAddr: addr, readyListener: ln, deadlineMargin: time.Second,
			PersonaDir: "../../agents/cluster/persona", Harness: persona.CoreAgent, ConfigRoot: t.TempDir(),
			Relay: parityRelay, Workdir: "/scratch",
		})
	}()
	waitReady(t, addr)

	const task = "task-coreagent-grant"
	d.offer(task, profileSubmission(t, task, "look", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if _, final := drainLeases(d, task, 20*time.Second); !final {
		t.Fatal("no final renewal")
	}
	raw, err := os.ReadFile(filepath.Join(root, "envelope"))
	if err != nil {
		t.Fatal(err)
	}
	var env coreagentwire.Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		t.Fatalf("envelope %q: %v", raw, err)
	}
	p := env.Policy
	if !slices.Contains(p.Tools, "bash") || slices.Contains(p.Tools, "write_file") || !p.WebSearch ||
		!slices.Contains(p.Allow, "bash:*") || !slices.Contains(p.Allow, "mcp:developer_knowledge_search_documents") ||
		!slices.Contains(p.Allow, "mcp:developer_knowledge_get_documents") || !slices.Contains(p.Allow, "read_file:/scratch/*") {
		t.Errorf("the turn's policy %+v, want the cluster persona's grant", p)
	}
}

// TestACoreAgentProfileWorkerWithoutAUsableGrantStops: a core-agent profile
// worker with no persona, or one whose grant won't render, stops before it
// takes a task rather than run on the session grant.
func TestACoreAgentProfileWorkerWithoutAUsableGrantStops(t *testing.T) {
	url := startServer(t)
	for name, pw := range map[string]ProfileWorkerConfig{
		"no persona":       {Tier: pwTier, Harness: persona.CoreAgent, ConfigRoot: t.TempDir(), Workdir: "/scratch"},
		"relative workdir": {Tier: pwTier, PersonaDir: "../../agents/fleet-audit/persona", Harness: persona.CoreAgent, ConfigRoot: t.TempDir(), Workdir: "scratch"},
	} {
		t.Run(name, func(t *testing.T) {
			cfg := profileWorkerConfig(t, url, stub(t, "exit 0"))
			cfg.Driver = NewCoreAgentDriver(coreagentwire.Policy{}, nil)
			err := RunProfileWorker(context.Background(), cfg, pw)
			if err == nil || !strings.Contains(err.Error(), "grant") {
				t.Errorf("err %v, want a tool-grant refusal at start", err)
			}
		})
	}
}
