// Package hermesbridge is the stand-in executor for tasks addressed to the
// platform profile: it consumes a2a.tasks.{profile}.*.in, runs one
// `hermes -p {profile} chat -Q -q <prompt>` per task, and publishes the payload
// spec's lifecycle events with the output as the result artifact. It is
// scaffolding for the Hermes-first world - when the stage-3 dispatcher and
// the W4 worker adapter land, the bridge retires. Design:
// a2a/docs/hermes-bridge.md.
package hermesbridge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// taskQueueCapacity bounds the accepted-but-not-started queue; hitting
	// it on a playground bridge is a fault, not load.
	taskQueueCapacity = 1024
	// stderrTailBytes is how much subprocess stderr a failed task's status
	// message can carry.
	stderrTailBytes = 2048
	// finalizePublishTimeout bounds the result+terminal publishes of one
	// finalize; it must outlast a NATS reconnect, not a task.
	finalizePublishTimeout = 20 * time.Second
	// registryClearTimeout bounds the KV delete after a terminal publish.
	registryClearTimeout = 10 * time.Second

	shutdownReason = "reason: bridge-shutdown - the bridge was terminated while this task was in flight"
)

// Config wires one bridge. Zero values get playground defaults in Run.
type Config struct {
	// NATSURL is the bus address.
	NATSURL string
	// Profile is the addressee token the bridge executes for ("platform").
	Profile string
	// Command is the invocation prefix; the task prompt is appended as the
	// final argument. Default: ["hermes", "-p", <profile>, "chat", "-Q", "-q"].
	Command []string
	// Concurrency caps simultaneous hermes subprocesses (default 2, the
	// platform profile's concurrency in the profiles spec).
	Concurrency int
	// TaskDeadline is the per-invocation wall-clock ceiling (default 7200s,
	// matching the platform profile's activeDeadlineSeconds).
	TaskDeadline time.Duration
	// KillGrace is SIGTERM-to-SIGKILL grace on cancel/deadline (default 10s).
	KillGrace time.Duration
	// KVBucket holds the in-flight registry the sweep reads (default
	// "runtime-state", the provisioned bucket).
	KVBucket string
	// ResultChunkSize bounds one result artifact-update's text part so a
	// large answer never trips the client-side max-message-size gate
	// (default 256KiB).
	ResultChunkSize int
	// NATSOptions carries credentials etc; applied to both connections.
	NATSOptions []nats.Option
	Logger      *slog.Logger
}

func (c *Config) defaults() {
	if c.Profile == "" {
		c.Profile = "platform"
	}
	if len(c.Command) == 0 {
		// -Q is hermes's programmatic mode: no banner, no spinner, no TUI
		// box around the answer - stdout is the response.
		c.Command = []string{"hermes", "-p", c.Profile, "chat", "-Q", "-q"}
	}
	if c.Concurrency <= 0 {
		c.Concurrency = 2
	}
	if c.TaskDeadline <= 0 {
		c.TaskDeadline = 7200 * time.Second
	}
	if c.KillGrace <= 0 {
		c.KillGrace = 10 * time.Second
	}
	if c.KVBucket == "" {
		c.KVBucket = "runtime-state"
	}
	if c.ResultChunkSize <= 0 {
		c.ResultChunkSize = 256 * 1024
	}
	if c.Logger == nil {
		c.Logger = slog.Default()
	}
}

// runState is a task's position in the bridge, guarded by taskRun.mu.
type runState int

const (
	statePending runState = iota // accepted, submitted published, queued
	stateRunning                 // subprocess spawned
	stateDone                    // terminal event published
)

type taskRun struct {
	origin *lib.Envelope
	exec   *lib.TaskExecution

	// mu guards state, proc, and killTimers - and is held across the
	// finalize publish, so a steer refusal can never land after the final
	// event: whoever holds the lock sees the true state before publishing.
	mu         sync.Mutex
	state      runState
	proc       *exec.Cmd
	killTimers []*time.Timer

	canceled    atomic.Bool
	deadlineHit atomic.Bool
}

// Bridge is one running instance. Two connections by design: the lib client
// owns the task plane (consume, publish, resilience contract), and a raw
// jetstream handle owns what the lib doesn't speak yet - the KV in-flight
// registry and the sweep's CAS publish.
type Bridge struct {
	cfg  Config
	from lib.Party

	c  *lib.Client
	nc *nats.Conn
	js jetstream.JetStream
	kv jetstream.KeyValue

	mu    sync.Mutex
	tasks map[string]*taskRun
	queue chan *taskRun
	wg    sync.WaitGroup

	// closing marks shutdown, so a worker whose subprocess died to the
	// shutdown SIGKILL reports bridge-shutdown, not a bogus exit code.
	closing atomic.Bool
}

// New connects and sweeps but does not consume yet; Run does.
func New(ctx context.Context, cfg Config) (*Bridge, error) {
	cfg.defaults()
	b := &Bridge{
		cfg: cfg,
		from: lib.Party{
			Session:   cfg.Profile + "-bridge",
			AgentType: "hermes-bridge",
			Profile:   cfg.Profile,
		},
		tasks: make(map[string]*taskRun),
		queue: make(chan *taskRun, taskQueueCapacity),
	}
	var err error
	b.c, err = lib.Connect(ctx, cfg.NATSURL,
		lib.WithName(b.from.Session),
		lib.WithLogger(cfg.Logger),
		lib.WithNATSOptions(cfg.NATSOptions...))
	if err != nil {
		return nil, err
	}
	b.nc, err = nats.Connect(cfg.NATSURL, append([]nats.Option{
		nats.Name(b.from.Session + "-kv"), nats.MaxReconnects(-1),
	}, cfg.NATSOptions...)...)
	if err != nil {
		b.c.Close()
		return nil, fmt.Errorf("kv connection: %w", err)
	}
	b.js, err = jetstream.New(b.nc)
	if err != nil {
		b.close()
		return nil, fmt.Errorf("kv jetstream: %w", err)
	}
	b.kv, err = b.js.KeyValue(ctx, cfg.KVBucket)
	if err != nil {
		b.close()
		return nil, fmt.Errorf("kv bucket %s: %w", cfg.KVBucket, err)
	}
	return b, nil
}

func (b *Bridge) close() {
	b.c.Close()
	b.nc.Close()
}

// Run sweeps orphans from a prior incarnation, then consumes the profile's
// in subjects until ctx is canceled. On shutdown, in-flight tasks get
// terminal failed (reason: bridge-shutdown) - the eviction path the profiles
// spec requires of adapters, so a rollout stays distinguishable from a crash.
func (b *Bridge) Run(ctx context.Context) error {
	defer b.close()
	if err := b.sweep(ctx); err != nil {
		return fmt.Errorf("startup sweep: %w", err)
	}
	for i := 0; i < b.cfg.Concurrency; i++ {
		b.wg.Add(1)
		go b.worker(ctx)
	}
	sub, err := b.c.SubscribeDurable(ctx, lib.SubscribeConfig{
		Stream:  lib.TasksStream,
		Subject: fmt.Sprintf("a2a.tasks.%s.*.in", b.cfg.Profile),
		Durable: "bridge-" + b.cfg.Profile,
		Session: b.cfg.Profile,
	}, func(env *lib.Envelope) { b.handle(ctx, env) })
	if err != nil {
		return fmt.Errorf("subscribe: %w", err)
	}
	b.cfg.Logger.Info("hermes bridge consuming", "profile", b.cfg.Profile)
	<-ctx.Done()
	b.closing.Store(true)
	sub.Stop()
	// The queue is never closed: Stop does not join an in-flight handler
	// callback, and a handler mid-accept sending into a closed channel would
	// panic the whole shutdown. Workers exit on ctx instead; anything still
	// queued is finalized by shutdownTasks below.
	b.shutdownTasks()
	b.wg.Wait()
	return nil
}

// shutdownTasks kills running subprocesses and finalizes every task still
// open. A worker unblocked by the kill may finalize with the real outcome
// first - finalize is idempotent and whoever wins writes exactly once.
func (b *Bridge) shutdownTasks() {
	b.mu.Lock()
	runs := make([]*taskRun, 0, len(b.tasks))
	for _, r := range b.tasks {
		runs = append(runs, r)
	}
	b.mu.Unlock()
	for _, r := range runs {
		r.mu.Lock()
		if r.state == stateRunning && r.proc != nil && r.proc.Process != nil {
			_ = syscall.Kill(-r.proc.Process.Pid, syscall.SIGKILL)
		}
		r.mu.Unlock()
		b.finalize(r, lib.StateFailed, shutdownReason, nil)
	}
}

// handle dispatches one envelope from the in subject. Anything it publishes
// happens before returning, ie before the consumer ack - a bridge death in
// here just redelivers.
func (b *Bridge) handle(ctx context.Context, env *lib.Envelope) {
	switch env.Kind {
	case lib.KindMessage:
		b.handleMessage(ctx, env)
	case lib.KindCancel:
		b.handleCancel(ctx, env)
	default:
		b.cfg.Logger.Warn("unexpected kind on in subject; ignoring",
			"kind", env.Kind, "task", env.TaskID)
	}
}

func (b *Bridge) handleMessage(ctx context.Context, env *lib.Envelope) {
	b.mu.Lock()
	run := b.tasks[env.TaskID]
	b.mu.Unlock()
	if run != nil {
		b.refuseSteer(ctx, run, env)
		return
	}
	// Unknown task: the dispatcher rule. Empty events subject means new;
	// terminal means acked with a warning; non-final events with no local run
	// is an orphan a follow-up cannot revive.
	task, err := b.c.TasksGet(ctx, b.cfg.Profile, env.TaskID)
	switch {
	case isTaskNotFound(err):
		b.accept(ctx, env)
	case err != nil:
		// The lib acks after this handler returns, so the submission is
		// dropped, not redelivered - no terminal event will follow. Honest
		// gap: the lib exposes no nak path yet.
		b.cfg.Logger.Error("events lookup failed; dropping submission",
			"task", env.TaskID, "err", err)
	case task.Final:
		b.cfg.Logger.Warn("message for a task with a terminal event; ignoring", "task", env.TaskID)
	default:
		b.cfg.Logger.Warn("message for an orphaned task this bridge is not running; ignoring",
			"task", env.TaskID, "state", task.State)
	}
}

// accept is the dispatcher half: register in-flight, publish submitted,
// queue for a worker. KV before submitted, deliberately - a crash between
// the two leaves a key the sweep deletes harmlessly, where the opposite
// order leaves a task the sweep cannot see.
func (b *Bridge) accept(ctx context.Context, env *lib.Envelope) {
	x, err := b.c.NewTaskExecution(env, b.from, b.cfg.Profile)
	if err != nil {
		b.cfg.Logger.Error("rejecting malformed submission", "task", env.TaskID, "err", err)
		return
	}
	run := &taskRun{origin: env, exec: x}
	if err := b.markInFlight(ctx, env.TaskID); err != nil {
		b.cfg.Logger.Error("in-flight registry write failed; dropping submission",
			"task", env.TaskID, "err", err)
		return
	}
	if err := x.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		b.cfg.Logger.Error("submitted publish failed; dropping submission",
			"task", env.TaskID, "err", err)
		b.clearInFlight(ctx, env.TaskID)
		return
	}
	b.mu.Lock()
	b.tasks[env.TaskID] = run
	b.mu.Unlock()
	b.cfg.Logger.Info("task accepted", "task", env.TaskID, "correlation", env.CorrelationID, "from", env.From.Session)
	select {
	case b.queue <- run:
	default:
		// taskQueueCapacity queued tasks on a playground bridge is a fault,
		// not load.
		b.finalize(run, lib.StateFailed, "reason: bridge-queue-overflow", nil)
	}
}

func (b *Bridge) handleCancel(ctx context.Context, env *lib.Envelope) {
	b.mu.Lock()
	run := b.tasks[env.TaskID]
	b.mu.Unlock()
	if run == nil {
		b.cancelOrphan(ctx, env)
		return
	}
	run.canceled.Store(true)
	run.mu.Lock()
	pending := run.state == statePending
	if run.state == stateRunning && run.proc != nil && run.proc.Process != nil {
		b.killGroup(run, run.proc.Process.Pid)
	}
	run.mu.Unlock()
	if pending {
		// Not yet spawned: terminal now; the worker skips done runs.
		b.finalize(run, lib.StateCanceled, "reason: canceled-before-start", nil)
	}
	// For a running task the runner publishes terminal canceled on exit.
}

// cancelOrphan handles cancel for a task the bridge is not running: if its
// events show it non-final (a prior incarnation died mid-task and the sweep
// has no key for it), synthesize terminal canceled under CAS. Terminal or
// absent tasks get a warning and nothing else.
func (b *Bridge) cancelOrphan(ctx context.Context, env *lib.Envelope) {
	task, err := b.c.TasksGet(ctx, b.cfg.Profile, env.TaskID)
	switch {
	case isTaskNotFound(err):
		b.cfg.Logger.Warn("cancel for a task with no events; ignoring", "task", env.TaskID)
	case err != nil:
		b.cfg.Logger.Error("cancel events lookup failed", "task", env.TaskID, "err", err)
	case task.Final:
		b.cfg.Logger.Warn("cancel for a task with a terminal event; ignoring", "task", env.TaskID)
	default:
		if err := b.synthesizeTerminal(ctx, env.TaskID, lib.StateCanceled,
			"reason: canceled-while-orphaned - no live executor held this task"); err != nil {
			b.cfg.Logger.Error("orphan cancel synthesis failed", "task", env.TaskID, "err", err)
		}
	}
}

// refuseSteer answers a mid-run follow-up honestly: hermes chat -q is
// one-shot, there is no stdin to inject into. The refusal is a non-final
// status carrying the task's CURRENT state - a follow-up must not change
// folded state by itself (assertion 12), so a queued task answers
// submitted, a spawned one working. Published under run.mu, so it can
// never land after the final event finalize writes under the same lock.
func (b *Bridge) refuseSteer(ctx context.Context, run *taskRun, steer *lib.Envelope) {
	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state == stateDone {
		b.cfg.Logger.Warn("message for a task with a terminal event; ignoring", "task", steer.TaskID)
		return
	}
	state := lib.StateWorking
	if run.state == statePending {
		state = lib.StateSubmitted
	}
	msg := "steering received but not absorbed: the Hermes CLI runs one-shot and cannot " +
		"accept mid-run input. The task continues on its original instruction; cancel if that is wrong."
	if err := b.publishStatusMessage(ctx, run, state, false, msg); err != nil {
		b.cfg.Logger.Error("steer refusal publish failed", "task", steer.TaskID, "err", err)
	}
}

func (b *Bridge) worker(ctx context.Context) {
	defer b.wg.Done()
	for {
		var run *taskRun
		select {
		case <-ctx.Done():
			return
		case run = <-b.queue:
		}
		run.mu.Lock()
		if run.state != statePending {
			run.mu.Unlock()
			continue
		}
		run.state = stateRunning
		run.mu.Unlock()
		b.runTask(ctx, run)
	}
}

func (b *Bridge) runTask(ctx context.Context, run *taskRun) {
	taskID := run.origin.TaskID
	prompt, ok := promptFromMessage(run.origin.Payload)
	if !ok {
		b.finalize(run, lib.StateRejected,
			"reason: no-text-parts - the submission message carries nothing the hermes CLI can be asked", nil)
		return
	}
	if err := run.exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		b.cfg.Logger.Error("working publish failed", "task", taskID, "err", err)
		b.finalize(run, lib.StateFailed, "reason: bus-publish-failed at working", nil)
		return
	}

	argv := append(append([]string(nil), b.cfg.Command...), prompt)
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var stdout strings.Builder
	stderr := newTailBuffer(stderrTailBytes)
	cmd.Stdout = &stdout
	cmd.Stderr = stderr

	run.mu.Lock()
	if run.state != stateRunning {
		// Shutdown or cancel finalized first.
		run.mu.Unlock()
		return
	}
	if err := cmd.Start(); err != nil {
		run.mu.Unlock()
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: spawn-failed - %v", err), nil)
		return
	}
	run.proc = cmd
	// Cancel may have raced the spawn: its kill saw no process, so re-check
	// under the same lock its kill path takes.
	if run.canceled.Load() {
		b.killGroup(run, cmd.Process.Pid)
	}
	run.mu.Unlock()

	deadline := time.AfterFunc(b.cfg.TaskDeadline, func() {
		run.deadlineHit.Store(true)
		run.mu.Lock()
		if run.state == stateRunning && run.proc != nil && run.proc.Process != nil {
			b.killGroup(run, run.proc.Process.Pid)
		}
		run.mu.Unlock()
	})
	err := cmd.Wait()
	deadline.Stop()
	// The group is gone; stop any armed grace-period SIGKILLs before the
	// pgid can be recycled onto an innocent process.
	run.mu.Lock()
	for _, t := range run.killTimers {
		t.Stop()
	}
	run.killTimers = nil
	run.mu.Unlock()

	switch {
	case err == nil:
		// A canceled task that finished anyway won the race: completed wins,
		// per the payload spec's cancel mapping.
		out := stdout.String()
		b.finalize(run, lib.StateCompleted, "", &out)
	case run.deadlineHit.Load():
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: deadline-exceeded - killed after %s", b.cfg.TaskDeadline), nil)
	case run.canceled.Load():
		b.finalize(run, lib.StateCanceled, "reason: canceled-by-request", nil)
	case b.closing.Load():
		// Killed by shutdownTasks; name the real cause, not the exit code.
		b.finalize(run, lib.StateFailed, shutdownReason, nil)
	default:
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: hermes-exited-nonzero - %v; stderr tail: %s", err, stderr.String()), nil)
	}
}

// finalize is the single writer of a task's terminal event, idempotent: the
// first caller wins, later callers see stateDone and leave. A non-nil
// resultOutput publishes the result artifact ahead of the terminal event
// inside the same critical section, so a racing finalizer cannot slip its
// final in between. Publishes ride a fresh bounded context, never the
// caller's - the terminal event must go out even when the caller's context
// is already canceled, which is exactly what shutdown looks like.
func (b *Bridge) finalize(run *taskRun, state lib.TaskState, msg string, resultOutput *string) {
	run.mu.Lock()
	if run.state == stateDone {
		run.mu.Unlock()
		return
	}
	run.state = stateDone
	ctx, cancel := context.WithTimeout(context.Background(), finalizePublishTimeout)
	if resultOutput != nil {
		if err := b.publishResult(ctx, run, *resultOutput); err != nil {
			b.cfg.Logger.Error("result publish failed", "task", run.origin.TaskID, "err", err)
			state, msg = lib.StateFailed, "reason: bus-publish-failed at result"
		}
	}
	err := b.publishTerminal(ctx, run, state, msg)
	cancel()
	run.mu.Unlock()
	if err != nil {
		// The task stays in the KV registry, so a restart's sweep writes the
		// terminal event this publish could not.
		b.cfg.Logger.Error("terminal publish failed; sweep will finalize",
			"task", run.origin.TaskID, "state", state, "err", err)
		return
	}
	cctx, ccancel := context.WithTimeout(context.Background(), registryClearTimeout)
	b.clearInFlight(cctx, run.origin.TaskID)
	ccancel()
	b.mu.Lock()
	delete(b.tasks, run.origin.TaskID)
	b.mu.Unlock()
	b.cfg.Logger.Info("task finished", "task", run.origin.TaskID, "state", state)
}

func (b *Bridge) publishTerminal(ctx context.Context, run *taskRun, state lib.TaskState, msg string) error {
	if msg == "" {
		return run.exec.PublishStatus(ctx, state, true)
	}
	return b.publishStatusMessage(ctx, run, state, true, msg)
}

// publishStatusMessage is PublishStatus with a status.message attached -
// the lib's TaskExecution doesn't carry one, and reasons ride there.
func (b *Bridge) publishStatusMessage(ctx context.Context, run *taskRun, state lib.TaskState, final bool, text string) error {
	origin := run.origin
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    origin.TaskID,
		ContextID: origin.ContextID,
		Status: lib.TaskStatus{
			State: state,
			Message: &lib.Message{
				Role:      "agent",
				MessageID: "msg-" + nuid.Next(),
				Parts:     []lib.Part{{Kind: "text", Text: text}},
				TaskID:    origin.TaskID,
				ContextID: origin.ContextID,
			},
		},
		Final: final,
	})
	if err != nil {
		return err
	}
	env, err := lib.NewStatusUpdateEnvelope(b.from, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		return err
	}
	return b.c.Publish(ctx, lib.TaskEventsSubject(b.cfg.Profile, origin.TaskID), env)
}

// publishResult ships stdout as the result artifact, chunked per A2A rules
// so one huge answer never trips the max-message-size gate.
func (b *Bridge) publishResult(ctx context.Context, run *taskRun, output string) error {
	chunks := chunkString(output, b.cfg.ResultChunkSize)
	artifactID := "artifact-" + run.origin.TaskID + "-result"
	for i, chunk := range chunks {
		payload, err := json.Marshal(lib.ArtifactUpdate{
			TaskID:    run.origin.TaskID,
			ContextID: run.origin.ContextID,
			Artifact: lib.Artifact{
				ArtifactID: artifactID,
				Name:       lib.ArtifactResult,
				Parts:      []lib.Part{{Kind: "text", Text: chunk}},
			},
			Append:    i > 0,
			LastChunk: i == len(chunks)-1,
		})
		if err != nil {
			return err
		}
		env, err := lib.NewArtifactUpdateEnvelope(b.from, run.origin.TaskID, run.origin.ContextID, run.origin.CorrelationID, payload)
		if err != nil {
			return err
		}
		if err := b.c.Publish(ctx, lib.TaskEventsSubject(b.cfg.Profile, run.origin.TaskID), env); err != nil {
			return err
		}
	}
	return nil
}

// killGroup SIGTERMs the subprocess group and arms a SIGKILL for the grace
// period. Caller holds run.mu. The timer is remembered so the reaper stops
// it once the group is gone - an unstopped timer could SIGKILL a recycled
// pgid belonging to somebody else.
func (b *Bridge) killGroup(run *taskRun, pid int) {
	_ = syscall.Kill(-pid, syscall.SIGTERM)
	t := time.AfterFunc(b.cfg.KillGrace, func() {
		_ = syscall.Kill(-pid, syscall.SIGKILL)
	})
	run.killTimers = append(run.killTimers, t)
}

// chunkString splits s into pieces of at most size bytes, never inside a
// UTF-8 rune - each chunk is JSON-marshaled independently, and a rune split
// across chunks would be corrupted to U+FFFD on both sides. An empty s is
// one empty chunk, because a completed task must still carry a result
// artifact (assertion 18).
func chunkString(s string, size int) []string {
	if len(s) <= size {
		return []string{s}
	}
	var out []string
	for len(s) > size {
		cut := size
		for cut > 0 && !utf8.RuneStart(s[cut]) {
			cut--
		}
		if cut == 0 {
			// No rune boundary inside the window (only possible for
			// size < utf8.UTFMax with a multi-byte rune first): take the
			// whole rune rather than loop forever.
			_, cut = utf8.DecodeRuneInString(s)
		}
		out = append(out, s[:cut])
		s = s[cut:]
	}
	return append(out, s)
}

// promptFromMessage joins the submission message's text parts. ok is false
// when there is nothing textual to ask.
func promptFromMessage(payload json.RawMessage) (string, bool) {
	var m lib.Message
	if err := json.Unmarshal(payload, &m); err != nil {
		return "", false
	}
	var texts []string
	for _, p := range m.Parts {
		if p.Kind == "text" && strings.TrimSpace(p.Text) != "" {
			texts = append(texts, p.Text)
		}
	}
	if len(texts) == 0 {
		return "", false
	}
	return strings.Join(texts, "\n\n"), true
}

func isTaskNotFound(err error) bool {
	var a2aErr *lib.A2AError
	return errors.As(err, &a2aErr) && a2aErr.Code == lib.CodeTaskNotFound
}

// tailBuffer keeps the last cap bytes written - stderr evidence for the
// failed reason without holding a runaway stream.
type tailBuffer struct {
	mu  sync.Mutex
	buf []byte
	cap int
}

func newTailBuffer(capacity int) *tailBuffer {
	return &tailBuffer{cap: capacity}
}

func (t *tailBuffer) Write(p []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, p...)
	if len(t.buf) > t.cap {
		t.buf = t.buf[len(t.buf)-t.cap:]
	}
	return len(p), nil
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}
