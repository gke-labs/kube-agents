package workeradapter

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Config is the adapter's contract with its pod: everything here arrives as
// env (spec-subagent-profiles.md: "Env is minimal" - the task content itself
// is fetched from the stream, never passed through the pod spec).
type Config struct {
	NATSURL      string
	NATSUser     string
	NATSPassword string

	// TaskID names the one task this process exists for.
	TaskID string
	// Profile is the persona the pod boots (PROFILE env); rides from.profile.
	Profile string
	// Session is the executor's bus session name (A2A_SESSION env) - the
	// addressee token on the task subjects for gateway-spawned session pods.
	// Empty means the addressee is the profile (dispatcher-spawned shape).
	Session string

	// HarnessCommand is the full argv of the harness. Tests point it at a
	// stub; the pod default is the native binary with the stream-json flags.
	HarnessCommand []string
	// HarnessEnv is the complete environment for the harness subprocess.
	HarnessEnv []string

	// TaskDeadline bounds the harness wall clock below the pod's own
	// activeDeadlineSeconds so the failure is ours to report, not the
	// enforcer's.
	TaskDeadline time.Duration
	// KillGrace is SIGTERM-to-SIGKILL escalation time.
	KillGrace time.Duration

	Logger *slog.Logger
}

// Addressee is the executor's token on the task subjects.
func (c Config) Addressee() string {
	if c.Session != "" {
		return c.Session
	}
	return c.Profile
}

func (c *Config) applyDefaults() {
	if c.TaskDeadline <= 0 {
		c.TaskDeadline = defaultTaskDeadline
	}
	if c.KillGrace <= 0 {
		c.KillGrace = defaultKillGrace
	}
	if c.Logger == nil {
		c.Logger = slog.Default()
	}
}

// Result is what Run hands back to main for the exit code: the terminal
// state the task reached (or "" when nothing was published because the task
// was already terminal on the stream).
type Result struct {
	State   lib.TaskState
	Evicted bool
}

// resultChunkSize bounds one result artifact-update well under the bus max
// message size with envelope headroom (the bridge's number).
const (
	// defaultTaskDeadline and defaultKillGrace are the library's fallbacks when
	// Config leaves them unset. They are the same contract as
	// cmd/worker-adapter's defaultTaskDeadlineSeconds/defaultKillGraceSeconds
	// (1800s / 10s) and the gateway's defaultTaskDeadline -- three spellings of
	// two numbers that must agree, which is exactly why each one is named.
	defaultTaskDeadline = 30 * time.Minute
	defaultKillGrace    = 10 * time.Second

	// originFetchDeadline bounds the wait for the submitting envelope; a pod
	// that starts before its own task message is the case it exists for.
	// originFetchBatch is how many messages one FetchNoWait asks for, and
	// originFetchPoll paces the retries between fetches.
	originFetchDeadline = 30 * time.Second
	originFetchBatch    = 16
	originFetchPoll     = 500 * time.Millisecond

	// steerQueueDepth buffers steers arriving while a turn is in flight.
	steerQueueDepth = 16
	// failureEvidenceCap bounds the partial output attached to a failure, so a
	// runaway harness cannot put its whole stdout on the bus.
	failureEvidenceCap = 4096
)

const resultChunkSize = 256 * 1024

// terminalPublishTimeout is the fresh budget terminal publishes get - they
// run when the caller's context may already be dead (eviction), and the
// terminal event is the one thing that must still go out.
const terminalPublishTimeout = 20 * time.Second

// steerRefusalPublishTimeout bounds the refusal publish. It is short because
// it runs ON the supervise loop, ahead of the terminal event that is the
// load-bearing publish: a refusal that cannot go out in this budget must not
// be allowed to delay or displace the terminal.
const steerRefusalPublishTimeout = 5 * time.Second

// steerEchoCap bounds how much of the refused message the refusal quotes
// back. The echo exists so a user with several corrections in flight can tell
// WHICH one missed; the full text is already on the task's own `.in` subject,
// so this is a pointer, not a second copy of the content.
const steerEchoCap = 200

// adapter is one task's run state.
type adapter struct {
	cfg  Config
	log  *slog.Logger
	c    *lib.Client
	js   jetstream.JetStream
	from lib.Party
	exec *lib.TaskExecution
	// Identifiers bound from the originating message (assertions 14/15);
	// the TaskExecution carries them too, but hand-built artifact and
	// status payloads need them directly.
	taskID, contextID, correlationID string

	mu        sync.Mutex
	finalized bool
	finalErr  error
	appended  map[string]bool // artifact name -> first chunk already out
}

// Run executes the adapter's whole lifecycle for one task and returns the
// terminal state it published. Context cancellation is the eviction path:
// SIGTERM from the kubelet lands here, and the contract is flush, publish
// terminal failed reason worker-evicted, exit 143 (spec-subagent-profiles.md
// "Evicted").
func Run(ctx context.Context, cfg Config) (Result, error) {
	cfg.applyDefaults()
	log := cfg.Logger
	a := &adapter{cfg: cfg, log: log, appended: map[string]bool{}}
	a.from = lib.Party{Session: cfg.Addressee(), AgentType: "claude-code", Profile: cfg.Profile}

	// Two connections, the bridge's split: the lib client owns validated
	// publishes and replay-fold; the raw JetStream handle owns the ordered
	// consumers the lib doesn't expose (origin fetch, live in-subject).
	natsOpts := []nats.Option{nats.Name("worker-adapter-" + cfg.Addressee())}
	if cfg.NATSUser != "" {
		natsOpts = append(natsOpts,
			nats.UserInfo(cfg.NATSUser, cfg.NATSPassword),
			// Push-delivery JS API replies ride the per-user inbox prefix;
			// without this every JS call times out under the deny-by-default
			// grants — measured on the install, not inferred.
			nats.CustomInboxPrefix("_INBOX."+cfg.NATSUser))
	}
	c, err := lib.Connect(ctx, cfg.NATSURL, lib.WithLogger(log), lib.WithNATSOptions(natsOpts...))
	if err != nil {
		return Result{}, fmt.Errorf("connect (lib): %w", err)
	}
	defer c.Close()
	a.c = c

	nc, err := nats.Connect(cfg.NATSURL, natsOpts...)
	if err != nil {
		return Result{}, fmt.Errorf("connect (raw): %w", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		return Result{}, fmt.Errorf("jetstream: %w", err)
	}
	a.js = js

	// The pod exists because the message is already durable, so the fetch
	// cannot miss (spec ordering rule) - but consumer setup can race pod
	// start, so poll briefly rather than trusting one shot.
	origin, originSeq, err := a.fetchOrigin(ctx)
	if err != nil {
		return Result{}, fmt.Errorf("fetch task %s: %w", cfg.TaskID, err)
	}

	// Respawn safety: a task already terminal on the stream is not ours to
	// re-run (the dispatcher rule, worn by the executor while there is no
	// dispatcher). Nothing is published.
	skipSubmitted := false
	switch task, err := c.TasksGet(ctx, cfg.Addressee(), cfg.TaskID); {
	case err == nil && task.Final:
		log.Warn("task already terminal on the stream; refusing to re-run",
			"task", cfg.TaskID, "state", task.State)
		return Result{}, nil
	case err == nil:
		// Events exist but no terminal: a predecessor incarnation died
		// mid-task and the supervisor has not swept it yet. Publishing a
		// second submitted would lie about the lifecycle; resume at working.
		log.Warn("task has prior non-final events; resuming without submitted",
			"task", cfg.TaskID, "state", task.State)
		skipSubmitted = true
	default:
		var a2aErr *lib.A2AError
		if !errors.As(err, &a2aErr) || a2aErr.Code != lib.CodeTaskNotFound {
			return Result{}, fmt.Errorf("terminal check for %s: %w", cfg.TaskID, err)
		}
	}

	exec, err := c.NewTaskExecution(origin, a.from, cfg.Addressee())
	if err != nil {
		return Result{}, fmt.Errorf("task execution: %w", err)
	}
	a.exec = exec
	a.taskID, a.contextID, a.correlationID = origin.TaskID, origin.ContextID, origin.CorrelationID

	if !skipSubmitted {
		if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
			return Result{}, fmt.Errorf("publish submitted: %w", err)
		}
	}

	// The deliverable prompt is the message's text parts. A submission with
	// none is refused before any model spend - terminal rejected, the A2A
	// state for an executor that refuses work before starting it.
	prompt := promptFromOrigin(origin)
	if prompt == "" {
		state := lib.StateRejected
		err := a.finalize(state, "reason: no text parts in submission - nothing to execute", "")
		return Result{State: state}, err
	}

	// The live in-subject consumer opens positioned just after the
	// submission (the dual-reader rule: everything after the submission -
	// steers, follow-ups, cancel - belongs to the executor). Starting at
	// originSeq+1 means input published before this line still arrives.
	steerCh := make(chan string, steerQueueDepth)
	cancelCh := make(chan struct{}, 1)
	stopIn, err := a.consumeIn(ctx, originSeq+1, origin.EnvelopeID, steerCh, cancelCh)
	if err != nil {
		state := lib.StateFailed
		ferr := a.finalize(state, "reason: bus-subscribe-failed - "+err.Error(), "")
		return Result{State: state}, ferr
	}
	defer stopIn()

	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		return Result{State: lib.StateFailed}, fmt.Errorf("publish working: %w", err)
	}

	proc, err := startHarness(cfg.HarnessCommand, cfg.HarnessEnv, prompt, log)
	if err != nil {
		state := lib.StateFailed
		ferr := a.finalize(state, "reason: spawn-failed - "+err.Error(), "")
		return Result{State: state}, ferr
	}

	return a.supervise(ctx, proc, steerCh, cancelCh)
}

// supervise is the main loop: harness stdout events out, steering in, cancel
// and eviction and the deadline racing all of it. The terminal decision
// waits until the harness has exited AND its stdout is fully drained - a
// result event buffered behind a fast exit must still win.
func (a *adapter) supervise(ctx context.Context, proc *harnessProc, steerCh <-chan string, cancelCh <-chan struct{}) (Result, error) {
	log := a.log
	deadline := time.NewTimer(a.cfg.TaskDeadline)
	defer deadline.Stop()

	waitDone := make(chan error, 1)
	go func() {
		// The scanner owns the pipe until EOF; Wait tears the pipe down.
		<-proc.scanDone
		err := proc.cmd.Wait()
		proc.reaped()
		waitDone <- err
	}()

	var (
		canceled    bool
		deadlineHit bool
		sawResult   bool
		exited      bool
		waitErr     error
		resultText  string
		resultErr   string // failure subtype from the harness, if any
		// pendingTurns counts user messages written minus result events
		// seen: the opening prompt is turn one, every absorbed steer runs
		// another turn, and the task's deliverable is the result that
		// settles the count.
		pendingTurns = 1
	)

	events := proc.events
	for events != nil || !exited {
		select {
		case ev, ok := <-events:
			if !ok {
				events = nil
				continue
			}
			switch ev.Type {
			case "system":
				if ev.Subtype == "init" {
					log.Info("harness session started", "harnessSession", ev.SessionID)
				}
			case "assistant":
				a.publishAssistant(ctx, ev)
			case "result":
				// Drain steers that raced this result: they were published
				// while the task was working and must be delivered, not
				// dropped (assertion 21).
				for drained := false; !drained; {
					select {
					case text := <-steerCh:
						if err := proc.writeUser(text); err == nil {
							pendingTurns++
							log.Info("steer absorbed at turn boundary", "task", a.taskID)
						} else {
							log.Warn("steer write failed", "err", err)
						}
					default:
						drained = true
					}
				}
				pendingTurns--
				if ev.IsError || ev.Subtype != "success" {
					sawResult = true
					resultErr = ev.Subtype
					resultText = ev.Result
				} else if pendingTurns <= 0 {
					sawResult = true
					resultText = ev.Result
				} else {
					log.Info("turn result absorbed; steered turns pending",
						"task", a.taskID, "pending", pendingTurns)
					continue
				}
				// The deliverable (or the failure) is decided: close stdin
				// so the harness wraps up and exits.
				proc.closeStdin()
			}

		case text := <-steerCh:
			if sawResult || exited {
				// Post-deliverable steer: the deliverable is chosen and
				// the harness is wrapping up, so this correction cannot be
				// absorbed. The payload spec requires the refusal be
				// VISIBLE - a non-final status-update carrying the task's
				// current state, on the stream before the terminal event -
				// because the alternative is that a message the user typed
				// disappears with nothing anywhere saying so. Nothing else
				// marks this window: the choice of deliverable is
				// adapter-internal.
				log.Warn("steer after deliverable; refusing visibly", "task", a.taskID)
				a.publishSteerRefusal(text, "")
				continue
			}
			if err := proc.writeUser(text); err != nil {
				log.Warn("steer write failed", "task", a.taskID, "err", err)
				continue
			}
			pendingTurns++
			log.Info("steer forwarded onto harness stdin", "task", a.taskID)

		case <-cancelCh:
			if canceled {
				continue
			}
			canceled = true
			log.Info("cancel received; killing harness", "task", a.taskID)
			proc.kill(a.cfg.KillGrace)

		case <-deadline.C:
			deadlineHit = true
			proc.kill(a.cfg.KillGrace)

		case <-ctx.Done():
			// Eviction: kubelet SIGTERM landed. Flush what the harness
			// already produced, state the reason honestly, exit 143.
			proc.kill(0)
			state := lib.StateFailed
			err := a.finalize(state, "reason: worker-evicted - infrastructure delivered SIGTERM before the task finished", resultText)
			return Result{State: state, Evicted: true}, err

		case werr := <-waitDone:
			exited = true
			waitErr = werr
			waitDone = nil
		}
	}

	// Harness exited and stdout is drained. Decide the terminal in strict
	// precedence: a clean deliverable beats everything (cancel lost the
	// race, legal per the payload spec), then deadline, then cancel, then
	// failure with evidence.
	switch {
	case sawResult && resultErr == "":
		if err := a.publishResult(resultText); err != nil {
			state := lib.StateFailed
			ferr := a.finalize(state, "reason: bus-publish-failed at result - "+err.Error(), "")
			return Result{State: state}, ferr
		}
		state := lib.StateCompleted
		return Result{State: state}, a.finalize(state, "", "")
	case sawResult:
		state := lib.StateFailed
		return Result{State: state}, a.finalize(state,
			fmt.Sprintf("reason: %s", resultErr), resultText)
	case deadlineHit:
		state := lib.StateFailed
		return Result{State: state}, a.finalize(state, "reason: deadline-exceeded", "")
	case canceled:
		state := lib.StateCanceled
		return Result{State: state}, a.finalize(state, "reason: canceled-by-request", "")
	default:
		state := lib.StateFailed
		evidence := strings.TrimSpace(proc.stderr.String())
		reason := "reason: stream-ended-without-result"
		if waitErr != nil {
			reason += " - " + waitErr.Error()
		}
		if serr := proc.scanErr(); serr != nil {
			// Name the ceiling and its value rather than relaying
			// "token too long", which says nothing an operator can act on.
			// The deliverable is refused, never truncated: a silently
			// shortened answer is worse than a loud failure.
			if errors.Is(serr, bufio.ErrTooLong) {
				reason += fmt.Sprintf(
					" - the harness emitted a single output line over the %d-byte limit"+
						" (%d MiB, scannerMaxBytes in harness.go); the deliverable was refused"+
						" rather than truncated. A line this size is usually a file dumped"+
						" into the answer.",
					scannerMaxBytes, scannerMaxBytes/(1024*1024))
			} else {
				reason += " - stdout: " + serr.Error()
			}
		}
		if evidence != "" {
			reason += "\nstderr tail:\n" + evidence
		}
		return Result{State: state}, a.finalize(state, reason, "")
	}
}

// publishAssistant maps one assistant message's content blocks onto the
// reserved artifact names: thinking deltas to thinking, tool invocations to
// activity, and the model's own prose to progress - the milestone stream the
// gateway's rolling line renders at zero model cost. (The spec's explicit
// progress tool is the stage 3 shape; mapping the narration is the
// playground stand-in, recorded in the findings.)
func (a *adapter) publishAssistant(ctx context.Context, ev harnessEvent) {
	if ev.Message == nil {
		return
	}
	for _, block := range ev.Message.Content {
		switch block.Type {
		case "thinking":
			if block.Thinking != "" {
				a.publishArtifactChunk(ctx, lib.ArtifactThinking, lib.Part{Kind: "text", Text: block.Thinking}, false)
			}
		case "text":
			if block.Text != "" {
				a.publishArtifactChunk(ctx, lib.ArtifactProgress, lib.Part{Kind: "text", Text: block.Text}, false)
			}
		case "tool_use":
			entry, err := json.Marshal(map[string]any{
				"tool":  block.Name,
				"input": json.RawMessage(block.Input),
			})
			if err != nil {
				continue
			}
			a.publishArtifactChunk(ctx, lib.ArtifactActivity, lib.Part{Kind: "data", Data: entry}, false)
		}
	}
}

// publishArtifactChunk publishes one part onto a named artifact, appending
// after the first chunk. Stream artifacts are best-effort: a failed publish
// is logged and the task continues - the deliverable and the terminal event
// are the load-bearing publishes, not the telemetry.
func (a *adapter) publishArtifactChunk(ctx context.Context, name string, part lib.Part, last bool) {
	a.mu.Lock()
	appendChunk := a.appended[name]
	a.appended[name] = true
	a.mu.Unlock()
	update := lib.ArtifactUpdate{
		TaskID:    a.taskID,
		ContextID: a.contextID,
		Artifact: lib.Artifact{
			ArtifactID: "artifact-" + a.taskID + "-" + name,
			Name:       name,
			Parts:      []lib.Part{part},
		},
		Append:    appendChunk,
		LastChunk: last,
	}
	payload, err := json.Marshal(update)
	if err != nil {
		a.log.Warn("artifact marshal failed", "name", name, "err", err)
		return
	}
	env, err := lib.NewArtifactUpdateEnvelope(a.from, a.taskID, a.contextID, a.correlationID, payload)
	if err != nil {
		a.log.Warn("artifact envelope failed", "name", name, "err", err)
		return
	}
	if err := a.c.Publish(ctx, lib.TaskEventsSubject(a.cfg.Addressee(), a.taskID), env); err != nil {
		a.log.Warn("artifact publish failed", "name", name, "err", err)
	}
}

// publishResult publishes the deliverable as the result artifact, chunked.
// Empty output still yields one empty chunk: completed must carry a result
// artifact (assertion 18).
func (a *adapter) publishResult(text string) error {
	ctx, cancel := context.WithTimeout(context.Background(), terminalPublishTimeout)
	defer cancel()
	chunks := chunkString(text, resultChunkSize)
	for i, chunk := range chunks {
		update := lib.ArtifactUpdate{
			TaskID:    a.taskID,
			ContextID: a.contextID,
			Artifact: lib.Artifact{
				ArtifactID: "artifact-" + a.taskID + "-result",
				Name:       lib.ArtifactResult,
				Parts:      []lib.Part{{Kind: "text", Text: chunk}},
			},
			Append:    i > 0,
			LastChunk: i == len(chunks)-1,
		}
		payload, err := json.Marshal(update)
		if err != nil {
			return err
		}
		env, err := lib.NewArtifactUpdateEnvelope(a.from, a.taskID, a.contextID, a.correlationID, payload)
		if err != nil {
			return err
		}
		if err := a.c.Publish(ctx, lib.TaskEventsSubject(a.cfg.Addressee(), a.taskID), env); err != nil {
			return err
		}
	}
	return nil
}

// publishSteerRefusal answers one steer that arrived too late to absorb with
// a non-final status-update carrying the task's CURRENT state - `working`,
// because nothing terminal has been published yet and a refusal must not be
// the thing that moves the task. One refusal per refused message: the
// contract is that no message the user typed is dropped silently, so the
// count matches rather than coalescing.
//
// Best-effort, like the other stream telemetry. A refusal that fails to
// publish is logged loudly and the run continues to its terminal event; the
// terminal is the load-bearing publish and must not be lost to this one.
func (a *adapter) publishSteerRefusal(steer, reason string) {
	ctx, cancel := context.WithTimeout(context.Background(), steerRefusalPublishTimeout)
	defer cancel()

	if reason == "" {
		reason = "this task's deliverable was already decided when the message arrived, so it was not applied. Send it as a new request."
	}
	text := "steer refused: " + reason
	if echo := truncateRunes(strings.TrimSpace(steer), steerEchoCap); echo != "" {
		text += "\nrefused message: " + echo
	}
	update := lib.StatusUpdate{
		TaskID:    a.taskID,
		ContextID: a.contextID,
		Status: lib.TaskStatus{
			State: lib.StateWorking,
			Message: &lib.Message{
				Role:      "agent",
				MessageID: "msg-" + nuid.Next(),
				Parts:     []lib.Part{{Kind: "text", Text: text}},
			},
		},
		Final: false,
	}
	payload, err := json.Marshal(update)
	if err != nil {
		a.log.Error("steer refusal marshal failed", "task", a.taskID, "err", err)
		return
	}
	env, err := lib.NewStatusUpdateEnvelope(a.from, a.taskID, a.contextID, a.correlationID, payload)
	if err != nil {
		a.log.Error("steer refusal envelope failed", "task", a.taskID, "err", err)
		return
	}
	if err := a.c.Publish(ctx, lib.TaskEventsSubject(a.cfg.Addressee(), a.taskID), env); err != nil {
		// Loud: the user's correction is now lost with nothing on the
		// stream saying so, which is exactly the failure this publish
		// exists to prevent.
		a.log.Error("steer refusal publish failed; the refused message is now silent",
			"task", a.taskID, "err", err)
		return
	}
	a.log.Info("steer refusal published", "task", a.taskID)
}

// truncateRunes cuts on a rune boundary. The steer echo is user text and may
// be any script; byte truncation would emit invalid UTF-8 that marshals to
// replacement characters.
func truncateRunes(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// finalize publishes the one terminal event, exactly once per process, on a
// fresh context (the caller's may already be dead - eviction). A reason
// travels as the status message; evidence (partial output) rides along when
// present.
func (a *adapter) finalize(state lib.TaskState, reason, evidence string) error {
	a.mu.Lock()
	if a.finalized {
		defer a.mu.Unlock()
		return a.finalErr
	}
	a.finalized = true
	a.mu.Unlock()

	ctx, cancel := context.WithTimeout(context.Background(), terminalPublishTimeout)
	defer cancel()

	var err error
	if reason == "" {
		err = a.exec.PublishStatus(ctx, state, true)
	} else {
		text := reason
		if evidence != "" {
			text += "\npartial output:\n" + truncate(evidence, failureEvidenceCap)
		}
		update := lib.StatusUpdate{
			TaskID:    a.taskID,
			ContextID: a.contextID,
			Status: lib.TaskStatus{
				State: state,
				Message: &lib.Message{
					Role:      "agent",
					MessageID: "msg-" + nuid.Next(),
					Parts:     []lib.Part{{Kind: "text", Text: text}},
				},
			},
			Final: true,
		}
		var payload []byte
		payload, err = json.Marshal(update)
		if err == nil {
			var env *lib.Envelope
			env, err = lib.NewStatusUpdateEnvelope(a.from, a.taskID, a.contextID, a.correlationID, payload)
			if err == nil {
				err = a.c.Publish(ctx, lib.TaskEventsSubject(a.cfg.Addressee(), a.taskID), env)
			}
		}
	}
	if err != nil {
		// The supervisor (the gateway for session pods) sweeps tasks whose
		// executor died without a terminal event; leaving the failure loud
		// is the correct fallback.
		a.log.Error("terminal publish failed; supervisor sweep will declare this task",
			"task", a.taskID, "state", state, "err", err)
	} else {
		a.log.Info("terminal published", "task", a.taskID, "state", state, "reason", reason)
	}
	a.mu.Lock()
	a.finalErr = err
	a.mu.Unlock()
	return err
}

// fetchOrigin reads the task's originating kind:message envelope off the
// TASKS stream by subject and returns it with its stream sequence.
func (a *adapter) fetchOrigin(ctx context.Context) (*lib.Envelope, uint64, error) {
	subject := lib.TaskInSubject(a.cfg.Addressee(), a.cfg.TaskID)
	deadline := time.Now().Add(originFetchDeadline)
	// One consumer for the whole wait. Creating it per iteration left up to
	// sixty ephemeral consumers on TASKS behind a slow pod start, each living
	// until its inactivity threshold.
	cons, consErr := a.js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{subject},
		DeliverPolicy:  jetstream.DeliverAllPolicy,
	})
	for {
		if consErr == nil {
			batch, err := cons.FetchNoWait(originFetchBatch)
			if err == nil {
				for msg := range batch.Messages() {
					env, perr := lib.ParseEnvelope(msg.Data())
					if perr != nil {
						a.log.Error("unparseable message on in subject", "subject", subject, "err", perr)
						continue
					}
					if env.Kind != lib.KindMessage {
						continue
					}
					meta, merr := msg.Metadata()
					if merr != nil {
						return nil, 0, fmt.Errorf("origin metadata: %w", merr)
					}
					return env, meta.Sequence.Stream, nil
				}
			}
		}
		if time.Now().After(deadline) {
			if consErr != nil {
				return nil, 0, fmt.Errorf("consumer on %s: %w", subject, consErr)
			}
			return nil, 0, fmt.Errorf("no kind:message on %s within %s", subject, originFetchDeadline)
		}
		select {
		case <-ctx.Done():
			return nil, 0, ctx.Err()
		case <-time.After(originFetchPoll):
		}
		if consErr != nil {
			cons, consErr = a.js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
				FilterSubjects: []string{subject},
				DeliverPolicy:  jetstream.DeliverAllPolicy,
			})
		}
	}
}

// consumeIn opens the executor's ephemeral consumer on the task's in subject
// just after the submission - the dual-reader rule's executor half. Steers
// (kind:message) and cancel land here. Delivery to the harness is exactly
// once per envelopeId (assertion 21): the ordered consumer replays without
// acks, and the dedup set absorbs republished duplicates.
func (a *adapter) consumeIn(ctx context.Context, startSeq uint64, originEnvelopeID string, steerCh chan<- string, cancelCh chan<- struct{}) (func(), error) {
	subject := lib.TaskInSubject(a.cfg.Addressee(), a.cfg.TaskID)
	cons, err := a.js.OrderedConsumer(ctx, lib.TasksStream, jetstream.OrderedConsumerConfig{
		FilterSubjects: []string{subject},
		DeliverPolicy:  jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:    startSeq,
	})
	if err != nil {
		return nil, fmt.Errorf("in consumer: %w", err)
	}
	seen := map[string]bool{originEnvelopeID: true}
	var seenMu sync.Mutex
	cc, err := cons.Consume(func(msg jetstream.Msg) {
		env, err := lib.ParseEnvelope(msg.Data())
		if err != nil {
			a.log.Error("a2a envelope rejected on in subject", "subject", subject, "err", err)
			return
		}
		// Assertion 4: addressed-elsewhere is ignored; a to/subject
		// disagreement is a protocol error, surfaced and skipped.
		if env.To != nil && env.To.Session != a.cfg.Addressee() {
			a.log.Error("a2a envelope to/addressee mismatch on in subject",
				"subject", subject, "to", env.To.Session)
			return
		}
		seenMu.Lock()
		dup := seen[env.EnvelopeID]
		seen[env.EnvelopeID] = true
		seenMu.Unlock()
		if dup {
			return
		}
		switch env.Kind {
		case lib.KindMessage:
			var m lib.Message
			if err := json.Unmarshal(env.Payload, &m); err != nil {
				a.log.Error("follow-up message unparseable", "err", err)
				return
			}
			text := textFromParts(m.Parts)
			if text == "" {
				a.log.Warn("follow-up with no text parts dropped", "task", a.cfg.TaskID)
				return
			}
			select {
			case steerCh <- text:
			default:
				// Same rule as the post-deliverable window: a message the
				// user typed does not disappear with nothing on the stream
				// saying so. A log line is not "anywhere" — nobody holding
				// the conversation reads the pod's logs.
				a.log.Warn("steer queue full; refusing visibly", "task", a.cfg.TaskID)
				a.publishSteerRefusal(text, "the queue of pending corrections for this task is full, so it was not applied. Wait for the current turn to land, then send it again.")
			}
		case lib.KindCancel:
			select {
			case cancelCh <- struct{}{}:
			default:
			}
		default:
			a.log.Warn("unexpected kind on in subject", "kind", env.Kind)
		}
	})
	if err != nil {
		return nil, fmt.Errorf("consume in subject: %w", err)
	}
	return cc.Stop, nil
}

// promptFromOrigin joins the submission's text parts into the opening
// prompt.
func promptFromOrigin(origin *lib.Envelope) string {
	var m lib.Message
	if err := json.Unmarshal(origin.Payload, &m); err != nil {
		return ""
	}
	return textFromParts(m.Parts)
}

func textFromParts(parts []lib.Part) string {
	var texts []string
	for _, p := range parts {
		if p.Kind == "text" && strings.TrimSpace(p.Text) != "" {
			texts = append(texts, p.Text)
		}
	}
	return strings.Join(texts, "\n\n")
}

// chunkString splits the deliverable into publishable pieces, cutting on rune
// boundaries rather than byte ones. Same rule as truncate and truncateRunes,
// and here for a harder reason: those two trim a log line and a status
// message, while this one is on the load-bearing publish. Each chunk becomes a
// text Part that is json.Marshalled, and encoding/json does not error on
// invalid UTF-8 -- it substitutes U+FFFD. A byte cut through a multi-byte
// sequence therefore yields a replacement character at the tail of one chunk
// and another at the head of the next, lib.Task.mergeArtifact concatenates
// them, and the character the user asked for is gone from the answer the
// gateway relays into chat, with nothing saying so.
//
// A rune wider than size has no boundary inside the budget. It is emitted
// whole -- over budget by at most utf8.UTFMax-1 bytes -- because the
// alternative is a cut at zero and a loop that never advances. size is
// resultChunkSize (256 KiB) against a 4-byte worst case, so the overshoot
// cannot reach the bus ceiling resultChunkSize leaves headroom under.
func chunkString(s string, size int) []string {
	if s == "" {
		return []string{""}
	}
	var chunks []string
	for len(s) > size {
		cut := size
		for cut > 0 && !utf8.RuneStart(s[cut]) {
			cut--
		}
		if cut == 0 {
			// One rune, wider than the whole budget.
			_, cut = utf8.DecodeRuneInString(s)
		}
		chunks = append(chunks, s[:cut])
		s = s[cut:]
	}
	// The remainder, unless walking back an unsplittable rune consumed the
	// input exactly -- appending "" there would publish an empty trailing
	// chunk the byte-cutting version never produced.
	if s != "" || len(chunks) == 0 {
		chunks = append(chunks, s)
	}
	return chunks
}
