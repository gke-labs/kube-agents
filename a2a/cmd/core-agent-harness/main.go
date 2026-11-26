// Command core-agent-harness runs one worker turn on core-agent
// (github.com/go-steer/core-agent), linked as a library. The worker adapter
// starts it once per turn: it reads one coreagentwire.Envelope on stdin,
// runs the turn, writes coreagentwire.Event lines on stdout and exits.
// The harness keeps no session file: the conversation's history comes in
// on the Envelope and every event the turn adds goes out as an event line
// (coreagentwire), so the adapter holds the conversation.
//
// It loads nothing on its own: no AGENTS.md, .agents/ config, skills or MCP
// discovery from the workspace or HOME. Everything the turn sees comes from
// its flags (the persona, read from a directory the harness can't write)
// and its Envelope (the tool policy and MCP servers).
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/google/uuid"
	"google.golang.org/adk/v2/session"
	adktool "google.golang.org/adk/v2/tool"

	"github.com/go-steer/core-agent/v3/pkg/agent"
	"github.com/go-steer/core-agent/v3/pkg/childenv"
	"github.com/go-steer/core-agent/v3/pkg/config"
	"github.com/go-steer/core-agent/v3/pkg/permissions"
	"github.com/go-steer/core-agent/v3/pkg/tools"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// userID is the session log's user for every conversation: one worker pod
// holds one tenant's conversations.
const userID = "worker"

type options struct {
	personaDir       string
	personaRequired  bool
	model            string
	compactOverBytes int
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	os.Exit(run(ctx, os.Args[1:], os.Stdin, os.Stdout, os.Stderr))
}

func run(ctx context.Context, args []string, stdin io.Reader, stdout, stderr io.Writer) int {
	refusalLog = stderr
	// Non-dumpable before anything else: a same-UID command the turn runs
	// can then neither read this process's memory or environ nor open its
	// /proc/<pid>/fd. That covers this end of the stdout pipe only; the
	// adapter's end is the adapter's to protect, and its decoder accepts
	// event lines only in the turn's order.
	if err := childenv.Protect(); err != nil {
		fmt.Fprintln(stderr, "core-agent-harness: protect:", err)
		return 1
	}
	withholdFromToolChildren(os.Environ())
	fs := flag.NewFlagSet("core-agent-harness", flag.ContinueOnError)
	fs.SetOutput(stderr)
	var o options
	fs.StringVar(&o.personaDir, "persona-dir", "", "the rendered persona (instructions.md, skills/<name>/SKILL.md), read once into memory")
	fs.BoolVar(&o.personaRequired, "persona-required", false, "fail the turn when --persona-dir is missing, writable, or doesn't match its content manifest, rather than run with no persona")
	fs.IntVar(&o.compactOverBytes, "compact-over-bytes", defaultCompactOverBytes, "compact the conversation before the turn when its history is over this many bytes")
	fs.StringVar(&o.model, "model", "", "the model name (required)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if o.model == "" {
		fmt.Fprintln(stderr, "core-agent-harness: --model is required")
		return 2
	}
	out := json.NewEncoder(stdout)
	// Once ctx is done (the adapter's SIGTERM, for a cancel or the task's
	// deadline) nothing more is written: no result, no history. The adapter
	// decides a stopped task's state itself, and a result line would report
	// the stop as this harness's failure, or as a completed turn. The
	// stopped turn is left out of the conversation's history.
	emit := func(ev coreagentwire.Event) {
		if ctx.Err() != nil {
			return
		}
		_ = out.Encode(ev)
	}

	// The envelope is one line: the adapter keeps stdin open after writing
	// it (as it does for every harness), so read to the newline, never to
	// EOF, and no further than the cap.
	var env coreagentwire.Envelope
	raw, err := bufio.NewReaderSize(io.LimitReader(stdin, coreagentwire.MaxEnvelopeBytes+1), 64<<10).ReadBytes('\n')
	switch {
	case len(bytes.TrimSuffix(raw, []byte("\n"))) > coreagentwire.MaxEnvelopeBytes:
		err = fmt.Errorf("over %d bytes", coreagentwire.MaxEnvelopeBytes)
	case err == io.EOF && len(bytes.TrimSpace(raw)) > 0:
		err = nil // a final line without its newline
	}
	if err == nil {
		err = json.Unmarshal(raw, &env)
	}
	if err != nil {
		emit(coreagentwire.Event{Type: coreagentwire.TypeResult, Failed: true, Error: "reading the turn envelope: " + err.Error()})
		return 1
	}
	if err := turn(ctx, o, env, emit, stderr); err != nil {
		emit(coreagentwire.Event{Type: coreagentwire.TypeResult, Failed: true, Error: err.Error()})
		return 1
	}
	if ctx.Err() != nil {
		return 1
	}
	return 0
}

// turn builds the agent for one turn and runs it, emitting its events.
func turn(ctx context.Context, o options, env coreagentwire.Envelope, emit func(coreagentwire.Event), stderr io.Writer) error {
	llm, err := newModel(ctx, o.model)
	if err != nil {
		return fmt.Errorf("model: %w", err)
	}
	workdir, err := os.Getwd()
	if err != nil {
		return fmt.Errorf("workspace: %w", err)
	}
	builtins, permOpts, plugins, err := policySetup(env.Policy, env.MCP, workdir)
	if err != nil {
		return fmt.Errorf("tool policy: %w", err)
	}
	gate := permissions.New(permOpts)
	reg, err := tools.Build(&config.Config{}, gate, "", builtins)
	if err != nil {
		return fmt.Errorf("tools: %w", err)
	}
	instructions, skillsets, err := loadPersona(ctx, o.personaDir, o.personaRequired)
	if err != nil {
		return fmt.Errorf("persona: %w", err)
	}
	mcpsets, closeMCP, err := buildMCP(ctx, env.MCP, gate)
	if err != nil {
		return fmt.Errorf("mcp: %w", err)
	}
	defer closeMCP()

	sessions, session, seeded, err := seedSession(ctx, env)
	if err != nil {
		return fmt.Errorf("history: %w", err)
	}

	historyBytes := 0
	for _, raw := range env.History {
		historyBytes += len(raw)
	}
	compactor := newHistoryCompactor(historyBytes, o.compactOverBytes)

	opts := []agent.Option{
		agent.WithMode(agent.ModeAutonomous),
		agent.WithCompactor(compactor),
		agent.WithGate(gate),
		agent.WithTools(reg.Tools),
		agent.WithToolsets(append(append([]adktool.Toolset{}, skillsets...), mcpsets...)),
		agent.WithPlugins(plugins...),
		agent.WithSessionService(sessions),
		agent.WithSession(userID, session),
	}
	if instructions != "" {
		opts = append(opts, agent.WithExtraInstruction(instructions))
	}
	// The worker's own lines for the turn (a session pod's cluster access),
	// after the persona, in the instruction layer: never in the history,
	// which the envelope carries back on the next turn.
	for _, line := range env.Instructions {
		opts = append(opts, agent.WithExtraInstruction(line))
	}
	a, err := agent.New(llm, opts...)
	if err != nil {
		return fmt.Errorf("agent: %w", err)
	}

	emit(coreagentwire.Event{Type: coreagentwire.TypeInit, Session: session, Model: o.model})
	// A history over the threshold is summarised before the turn: the
	// summary is a marker event that core-agent's next request starts
	// from, and the adapter keeps only the events from it on. A summary
	// that fails leaves the history whole; the turn still runs.
	if _, err := a.CompactIfNeeded(ctx, ""); err != nil {
		fmt.Fprintln(stderr, "core-agent-harness: compaction failed; running on the whole history:", err)
	}
	m := newMapper(emit)
	var runErr error
	for ev, err := range a.Run(ctx, env.Message) {
		if err != nil {
			runErr = err
			break
		}
		m.observe(ev)
	}
	// The events the turn added, for the adapter to keep, before the result
	// that ends the turn.
	if err := emitAdded(context.WithoutCancel(ctx), sessions, session, seeded, emit); err != nil && runErr == nil {
		runErr = fmt.Errorf("history: %w", err)
	}
	m.finish(runErr)
	return nil
}

// seedSession is an in-memory session service holding the Envelope's
// history under the conversation's id (a new id when it has none), and the
// number of events seeded.
func seedSession(ctx context.Context, env coreagentwire.Envelope) (session.Service, string, int, error) {
	id := env.Session
	if id == "" {
		id = uuid.NewString()
	}
	svc := session.InMemoryService()
	created, err := svc.Create(ctx, &session.CreateRequest{AppName: agent.DefaultAppName, UserID: userID, SessionID: id})
	if err != nil {
		return nil, "", 0, err
	}
	seeded := 0
	// The adapter keeps a compacted conversation from its summary on, so
	// such a history starts with the summary. core-agent frames a summary
	// for the model only when an event comes before it; a content-less one,
	// which no request carries, goes first.
	if len(env.History) > 0 && coreagentwire.IsHistoryBoundary(env.History[0]) {
		if err := svc.AppendEvent(ctx, created.Session, session.NewEvent(ctx, "history-start")); err != nil {
			return nil, "", 0, fmt.Errorf("history start: %w", err)
		}
		seeded++
	}
	for i, raw := range env.History {
		var ev session.Event
		if err := json.Unmarshal(raw, &ev); err != nil {
			return nil, "", 0, fmt.Errorf("event %d: %w", i, err)
		}
		if err := svc.AppendEvent(ctx, created.Session, &ev); err != nil {
			return nil, "", 0, fmt.Errorf("event %d: %w", i, err)
		}
		seeded++
	}
	return svc, id, seeded, nil
}

// emitAdded writes an event line for each event the session holds past the
// first seeded.
func emitAdded(ctx context.Context, svc session.Service, id string, seeded int, emit func(coreagentwire.Event)) error {
	got, err := svc.Get(ctx, &session.GetRequest{AppName: agent.DefaultAppName, UserID: userID, SessionID: id})
	if err != nil {
		return err
	}
	i := 0
	for ev := range got.Session.Events().All() {
		if i >= seeded {
			raw, err := json.Marshal(ev)
			if err != nil {
				return err
			}
			emit(coreagentwire.Event{Type: coreagentwire.TypeEvent, Event: raw})
		}
		i++
	}
	return nil
}

// withholdFromToolChildren withholds, from every process a turn starts (the
// shell tool, an MCP stdio server), each variable in env off the worker's
// tool-child allowlist (workeradapter.WithheldFromToolChildren): the model,
// provider, bus and chat credentials this process carries, whatever their
// names. This process keeps them. An MCP server still gets its own entry's
// env on top.
func withholdFromToolChildren(env []string) {
	childenv.Withhold(workeradapter.WithheldFromToolChildren(env)...)
}
