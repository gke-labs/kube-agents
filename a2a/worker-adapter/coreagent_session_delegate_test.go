package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// delegateTemplate is the session's delegate server as cmd/worker-adapter
// builds it, at the pod's own socket.
func delegateTemplate(sock string) *persona.MCPServer {
	return &persona.MCPServer{
		Name:    persona.ReservedServer,
		Command: "/usr/local/bin/worker-adapter",
		Args:    []string{"mcp"},
		Env:     map[string]string{EnvDelegateSocket: sock},
		Tools:   []string{DelegateToolName},
	}
}

const delegatePromptMarker = "DELEGATE-PROMPT-MARKER"

// hasDelegateAllow reports whether a turn's policy lets the gate pass a call
// to the delegate tool.
func hasDelegateAllow(p coreagentwire.Policy) bool {
	name := persona.ReservedServer + "_" + DelegateToolName
	return slices.Contains(p.Allow, "mcp:"+name) && slices.Contains(p.Allow, "mcp:"+name+" *")
}

// hasDelegateParts reports which of the three delegate parts a turn's
// envelope carries: the server, the gate's allow rule, the prompt.
func hasDelegateParts(env coreagentwire.Envelope) (server, grant, prompt bool) {
	server = slices.ContainsFunc(env.MCP, func(s coreagentwire.MCPServer) bool { return s.Name == persona.ReservedServer })
	return server, hasDelegateAllow(env.Policy), slices.Contains(env.Instructions, delegatePromptMarker)
}

// recordingCoreAgentStub is a core-agent harness that keeps each run's stdin
// envelope as envelope.<n> under dir and answers with a recorded text turn.
func recordingCoreAgentStub(t *testing.T, dir string) []string {
	t.Helper()
	fixture, err := filepath.Abs("testdata/coreagent/text-only.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	return stub(t, fmt.Sprintf(`
dir=%q
n=$(( $(cat "$dir/n" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$dir/n"
read -r first || exit 1
printf '%%s' "$first" > "$dir/envelope.$n"
cat %q
`, dir, fixture))
}

func readEnvelope(t *testing.T, dir string, n int) coreagentwire.Envelope {
	t.Helper()
	var env coreagentwire.Envelope
	if err := json.Unmarshal([]byte(readRun(t, dir, "envelope", n)), &env); err != nil {
		t.Fatalf("run %d's envelope doesn't parse: %v", n, err)
	}
	return env
}

// Through a reused session pod's own runs on core-agent: a human turn gets
// the delegate server (at that turn's own socket, with the gateway's targets
// for it), the gate's allow rule for the delegate tool beside the session's
// own grant, and the delegate prompt, all three together; a wake gets none
// of them, and nothing the first turn was given carries over.
func TestACoreAgentSessionTurnGetsTheDelegateServerGrantAndPromptTogetherExceptOnAWake(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-otter-ca1"
	dir := t.TempDir()
	sock := delegateSock(t)
	targets := lib.DelegateTargets{Targets: []lib.DelegateTarget{{Name: "platform"}, {Name: "cluster", Description: "reads one cluster"}}}
	part, err := lib.DelegateTargetsPart(targets)
	if err != nil {
		t.Fatal(err)
	}
	submitWithParts(t, c, session, "task-ca-1", "use cluster", part)
	cfg := reuseConfig(url, "task-ca-1", session, recordingCoreAgentStub(t, dir))
	cfg.DelegateSocket = sock
	cfg.Driver = NewCoreAgentDriver(coreagentwire.Policy{Tools: []string{"read_file"}, Allow: []string{"read_file:/workspace/*"}}, nil)
	cfg.Driver = WithInstructions(cfg.Driver, []string{"session prompt"})
	cfg.DelegateMCP = delegateTemplate(sock)
	cfg.DelegateInstructions = []string{delegatePromptMarker}
	// The core-agent driver checks it is the container's init before every
	// process; a test stands in for that.
	useRespawnScope(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitState(t, c, session, "task-ca-1", lib.StateCompleted)

	first := readEnvelope(t, dir, 1)
	if s, g, p := hasDelegateParts(first); !s || !g || !p {
		t.Fatalf("the human turn's delegate parts: server %v, grant %v, prompt %v; want all three", s, g, p)
	}
	if len(first.MCP) != 1 {
		t.Fatalf("the human turn's servers = %+v, want the delegate server alone", first.MCP)
	}
	d := first.MCP[0]
	if d.Env[EnvDelegateSocket] == "" || d.Env[EnvDelegateSocket] == sock {
		t.Errorf("the reused pod's turn dials %q, want a socket of its own beside %q", d.Env[EnvDelegateSocket], sock)
	}
	var got lib.DelegateTargets
	if err := json.Unmarshal([]byte(d.Env[EnvDelegateTargets]), &got); err != nil || len(got.Targets) != 2 || got.Targets[1].Name != "cluster" {
		t.Errorf("the turn's targets %q (%v), want the gateway's two", d.Env[EnvDelegateTargets], err)
	}
	if !slices.Contains(first.Policy.Allow, "read_file:/workspace/*") {
		t.Errorf("the human turn's allow rules = %v, want the session's beside the delegate tool's", first.Policy.Allow)
	}
	if !slices.Contains(first.Instructions, "session prompt") {
		t.Errorf("the human turn's instructions = %q, want the session's beside the delegate prompt", first.Instructions)
	}

	submitWithAuthority(t, c, session, "task-ca-2", "the delegation came back", authorityWithVia(t, c, "task-ca-2", session))
	waitRun(t, dir, "envelope", 2)
	waitState(t, c, session, "task-ca-2", lib.StateCompleted)
	wake := readEnvelope(t, dir, 2)
	if s, g, p := hasDelegateParts(wake); s || g || p {
		t.Errorf("the wake's delegate parts: server %v, grant %v, prompt %v; want none", s, g, p)
	}
	if len(wake.MCP) != 0 {
		t.Errorf("the wake's servers = %+v, want none", wake.MCP)
	}
	if !slices.Contains(wake.Policy.Allow, "read_file:/workspace/*") || !slices.Contains(wake.Instructions, "session prompt") {
		t.Errorf("the wake lost the session's own grant or prompt: allow %v, instructions %q", wake.Policy.Allow, wake.Instructions)
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
}

// One turn's delegate server: the turn's socket over the template's, the
// submission's delegateTargets part when it has one, no stale list when it
// hasn't, nothing for a turn that may not delegate, and the template left as
// it was.
func TestTurnDelegateServerCarriesTheTurnsSocketAndTargets(t *testing.T) {
	tmpl := delegateTemplate("/run/base.sock")
	tmpl.Env[EnvDelegateTargets] = `{"targets":[{"name":"stale"}]}`

	targets := lib.DelegateTargets{Targets: []lib.DelegateTarget{{Name: "platform"}, {Name: "e2e5"}}}
	part, err := lib.DelegateTargetsPart(targets)
	if err != nil {
		t.Fatal(err)
	}
	msg := func(parts ...lib.Part) *lib.Envelope {
		raw, err := json.Marshal(lib.Message{Role: "user", Parts: append([]lib.Part{{Kind: "text", Text: "hi"}}, parts...)})
		if err != nil {
			t.Fatal(err)
		}
		return &lib.Envelope{Payload: raw}
	}

	s := turnDelegateServer(tmpl, true, "/run/turn-1.sock", msg(part))
	if s.Env[EnvDelegateSocket] != "/run/turn-1.sock" {
		t.Errorf("socket %q, want the turn's", s.Env[EnvDelegateSocket])
	}
	var gotTargets lib.DelegateTargets
	if err := json.Unmarshal([]byte(s.Env[EnvDelegateTargets]), &gotTargets); err != nil || len(gotTargets.Targets) != 2 || gotTargets.Targets[1].Name != "e2e5" {
		t.Errorf("targets %q (%v), want the turn's two", s.Env[EnvDelegateTargets], err)
	}
	if s := turnDelegateServer(tmpl, true, "/run/turn-2.sock", msg()); s.Env[EnvDelegateTargets] != "" {
		t.Errorf("a turn with no part kept a list: %q", s.Env[EnvDelegateTargets])
	}
	if turnDelegateServer(tmpl, false, "/run/turn-3.sock", msg(part)) != nil || turnDelegateServer(nil, true, "/run/t.sock", msg()) != nil || turnDelegateServer(tmpl, true, "", msg()) != nil {
		t.Error("a turn that may not delegate got a server")
	}
	if tmpl.Env[EnvDelegateSocket] != "/run/base.sock" || tmpl.Env[EnvDelegateTargets] == "" {
		t.Errorf("the template was changed: %+v", tmpl.Env)
	}
}

// The grant is exactly the delegate tool's two mcp: rules and nothing wider,
// added once beside the policy's own, with the driver it came from untouched.
func TestWithDelegateGrantAddsExactlyTheDelegateRules(t *testing.T) {
	base := NewCoreAgentDriver(coreagentwire.Policy{Tools: []string{"read_file"}, Allow: []string{"read_file:/workspace/*"}}, nil)
	d, err := withDelegateGrant(base, delegateTemplate("/run/d.sock"))
	if err != nil {
		t.Fatal(err)
	}
	d, err = withDelegateGrant(d, delegateTemplate("/run/d.sock"))
	if err != nil {
		t.Fatal(err)
	}
	name := "mcp:" + persona.ReservedServer + "_" + DelegateToolName
	want := []string{"read_file:/workspace/*", name, name + " *"}
	got := d.(coreAgentDriver).Policy.Allow
	if !slices.Equal(got, want) {
		t.Errorf("allow = %q, want exactly %q", got, want)
	}
	if b := base.(coreAgentDriver).Policy.Allow; !slices.Equal(b, []string{"read_file:/workspace/*"}) {
		t.Errorf("the base driver's allow changed: %q", b)
	}
}

// A scopeClusters part beside the targets reaches the core-agent session's
// delegate server too, as it does Claude Code's (withDelegateTargetsFor),
// and a template's stale page never does.
func TestTurnDelegateServerCarriesTheScopeClustersBesideTheTargets(t *testing.T) {
	tmpl := delegateTemplate("/run/base.sock")
	tmpl.Env[EnvScopeClusters] = `{"count":9}`
	targets, err := lib.DelegateTargetsPart(lib.DelegateTargets{Targets: []lib.DelegateTarget{{Name: "platform"}}})
	if err != nil {
		t.Fatal(err)
	}
	scope, err := lib.ScopeClustersPart(lib.ScopeClusters{Count: 1, Clusters: []lib.ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}})
	if err != nil {
		t.Fatal(err)
	}
	msg := func(parts ...lib.Part) *lib.Envelope {
		raw, err := json.Marshal(lib.Message{Role: "user", Parts: append([]lib.Part{{Kind: "text", Text: "hi"}}, parts...)})
		if err != nil {
			t.Fatal(err)
		}
		return &lib.Envelope{Payload: raw}
	}
	s := turnDelegateServer(tmpl, true, "/run/t.sock", msg(targets, scope))
	var got lib.ScopeClusters
	if err := json.Unmarshal([]byte(s.Env[EnvScopeClusters]), &got); err != nil || got.Count != 1 || len(got.Clusters) != 1 {
		t.Errorf("scope %q (%v), want the turn's page", s.Env[EnvScopeClusters], err)
	}
	if s := turnDelegateServer(tmpl, true, "/run/t.sock", msg(targets)); s.Env[EnvScopeClusters] != "" {
		t.Errorf("a turn with no page kept the template's: %q", s.Env[EnvScopeClusters])
	}
}
