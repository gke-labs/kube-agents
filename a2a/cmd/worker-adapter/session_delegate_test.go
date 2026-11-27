package main

import (
	"slices"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// A core-agent session pod with a delegate socket gets the delegate server
// template (this binary's own mcp subcommand at the pod's socket, the one
// tool) and the delegate prompt together; agy, a missing socket, and a
// profile worker get neither.
func TestSessionDelegateServerAndPromptComeTogether(t *testing.T) {
	ca, err := workeradapter.DriverFor(persona.CoreAgent)
	if err != nil {
		t.Fatal(err)
	}
	agy, err := workeradapter.DriverFor(persona.Antigravity)
	if err != nil {
		t.Fatal(err)
	}
	s := sessionDelegateMCP(ca, "/run/d.sock")
	if s == nil || s.Name != persona.ReservedServer || s.Command != selfPath() ||
		!slices.Equal(s.Args, []string{delegateMCPSubcommand}) ||
		s.Env[workeradapter.EnvDelegateSocket] != "/run/d.sock" ||
		!slices.Equal(s.Tools, []string{workeradapter.DelegateToolName}) {
		t.Fatalf("core-agent's delegate server = %+v", s)
	}
	if got := sessionDelegateInstructions(ca, "/run/d.sock"); !slices.Equal(got, []string{delegatePrompt}) {
		t.Errorf("core-agent's delegate instructions = %q, want the delegate prompt", got)
	}
	for name, c := range map[string]struct {
		d    workeradapter.Driver
		sock string
	}{"agy": {agy, "/run/d.sock"}, "no socket": {ca, ""}, "no driver": {nil, "/run/d.sock"}} {
		if sessionDelegateMCP(c.d, c.sock) != nil || sessionDelegateInstructions(c.d, c.sock) != nil {
			t.Errorf("%s got a delegate server or prompt", name)
		}
	}

	cfg := profileModeConfig(workeradapter.Config{
		Driver:               ca,
		DelegateMCP:          s,
		DelegateInstructions: []string{delegatePrompt},
	})
	if cfg.DelegateMCP != nil || cfg.DelegateInstructions != nil {
		t.Errorf("a profile worker kept the session's delegate: %+v %q", cfg.DelegateMCP, cfg.DelegateInstructions)
	}
}
