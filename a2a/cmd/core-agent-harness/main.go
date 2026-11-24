// Command core-agent-harness runs one worker turn on core-agent
// (github.com/go-steer/core-agent), linked as a library. The worker adapter
// starts it once per turn: it reads one coreagentwire.Envelope on stdin,
// runs the turn, writes coreagentwire.Event lines on stdout and exits.
// The conversation persists in the session log (--session-db) between
// turns, so the next process resumes it by id.
//
// It loads nothing on its own: no AGENTS.md, .agents/ config, skills or MCP
// discovery from the workspace or HOME. Everything the turn sees comes from
// its flags (the persona, read from a directory the harness can't write)
// and its Envelope (the tool policy and MCP servers).
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/glebarez/sqlite"
	"github.com/google/uuid"
	adktool "google.golang.org/adk/v2/tool"

	"github.com/go-steer/core-agent/v3/pkg/agent"
	"github.com/go-steer/core-agent/v3/pkg/config"
	"github.com/go-steer/core-agent/v3/pkg/eventlog"
	"github.com/go-steer/core-agent/v3/pkg/permissions"
	"github.com/go-steer/core-agent/v3/pkg/tools"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// userID is the session log's user for every conversation: one worker pod
// holds one tenant's conversations.
const userID = "worker"

type options struct {
	personaDir string
	sessionDB  string
	session    string
	model      string
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("core-agent-harness", flag.ContinueOnError)
	fs.SetOutput(stderr)
	var o options
	fs.StringVar(&o.personaDir, "persona-dir", "", "the rendered persona (instructions.md, skills/<name>/SKILL.md), read once into memory")
	fs.StringVar(&o.session, "session", "", "the conversation to resume from the session log; empty starts one")
	fs.StringVar(&o.sessionDB, "session-db", "", "the SQLite session log the conversation is stored in and resumed from (required)")
	fs.StringVar(&o.model, "model", "", "the model name (required)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if o.sessionDB == "" || o.model == "" {
		fmt.Fprintln(stderr, "core-agent-harness: --session-db and --model are required")
		return 2
	}
	out := json.NewEncoder(stdout)
	emit := func(ev coreagentwire.Event) { _ = out.Encode(ev) }

	var env coreagentwire.Envelope
	if err := json.NewDecoder(stdin).Decode(&env); err != nil {
		emit(coreagentwire.Event{Type: coreagentwire.TypeResult, Failed: true, Error: "reading the turn envelope: " + err.Error()})
		return 1
	}
	if err := turn(ctx, o, env, emit); err != nil {
		emit(coreagentwire.Event{Type: coreagentwire.TypeResult, Failed: true, Error: err.Error()})
		return 1
	}
	return 0
}

// turn builds the agent for one turn and runs it, emitting its events.
func turn(ctx context.Context, o options, env coreagentwire.Envelope, emit func(coreagentwire.Event)) error {
	llm, err := buildModel(ctx, o.model)
	if err != nil {
		return fmt.Errorf("model: %w", err)
	}
	builtins, permOpts, plugins, err := policySetup(env.Policy)
	if err != nil {
		return fmt.Errorf("tool policy: %w", err)
	}
	gate := permissions.New(permOpts)
	reg, err := tools.Build(&config.Config{}, gate, "", builtins)
	if err != nil {
		return fmt.Errorf("tools: %w", err)
	}
	instructions, skillsets, err := loadPersona(ctx, o.personaDir)
	if err != nil {
		return fmt.Errorf("persona: %w", err)
	}
	mcpsets, closeMCP, err := buildMCP(ctx, env.MCP, gate)
	if err != nil {
		return fmt.Errorf("mcp: %w", err)
	}
	defer closeMCP()

	log, err := eventlog.Open(ctx, sqlite.Open(o.sessionDB))
	if err != nil {
		return fmt.Errorf("session log: %w", err)
	}
	defer log.Close()
	session := o.session
	if session == "" {
		session = env.Session
	}
	if session == "" {
		session = uuid.NewString()
	}

	opts := []agent.Option{
		agent.WithMode(agent.ModeAutonomous),
		agent.WithGate(gate),
		agent.WithTools(reg.Tools),
		agent.WithToolsets(append(append([]adktool.Toolset{}, skillsets...), mcpsets...)),
		agent.WithPlugins(plugins...),
		agent.WithEventLog(log),
		agent.WithSession(userID, session),
	}
	if instructions != "" {
		opts = append(opts, agent.WithExtraInstruction(instructions))
	}
	a, err := agent.New(llm, opts...)
	if err != nil {
		return fmt.Errorf("agent: %w", err)
	}

	emit(coreagentwire.Event{Type: coreagentwire.TypeInit, Session: session, Model: o.model})
	m := newMapper(emit)
	for ev, err := range a.Run(ctx, env.Message) {
		if err != nil {
			m.finish(err)
			return nil
		}
		m.observe(ev)
	}
	m.finish(nil)
	return nil
}
