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
	NATSURL string

	// NATSUser/NATSPassword are the static-credential path. Nothing in this
	// repo spawns a worker with them any more — the gateway's spawner is the
	// only thing that creates a session pod, and it stopped: that is
	// gke-labs#1270, and BusTokenFile is what replaced them. The path stays
	// for a run by hand against a bus with no callout in front of it, which
	// is still a bus this deployment renders. When the last static user goes
	// from nats.conf, these go with it.
	NATSUser     string
	NATSPassword string

	// BusTokenFile is the projected ServiceAccount token the pod
	// authenticates with, audience-bound to the bus and bound by the kubelet
	// to this pod. Set, it wins over NATSUser: the callout derives this
	// session's grants from the pod the API server attests, so the identity
	// is the pod rather than anything this process was told.
	BusTokenFile string

	// PodName is the pod's own name from the downward API, and under the
	// callout it is the identity: the NATS user is named for it and the
	// grants — subjects, consumer names, inbox prefix — are all built from
	// it. It equals Session by construction (the gateway names the pod after
	// the bus session), and the adapter checks that rather than trusting
	// either, because a mismatch is silent: the connection succeeds and
	// every reply goes to an inbox the grants do not cover.
	PodName string

	// TaskID names the one task this process exists for.
	TaskID string
	// Profile is the persona the pod boots (PROFILE env); rides from.profile.
	Profile string
	// Session is the executor's bus session name (A2A_SESSION env) - the
	// addressee token on the task subjects for gateway-spawned session pods.
	// Empty means the addressee is the profile (dispatcher-spawned shape).
	Session string

	// OriginSeq is the TASKS stream sequence of the submission this process
	// exists to execute, from the spawner's own PubAck (lib.EnvOriginSeq).
	//
	// Zero means nobody told us, and the two ways that happens are not the
	// same. A spawner older than the variable, a by-hand run, and the
	// dispatcher-spawned shape all leave it unset, and those fall back to
	// scanning the subject. A current spawner that could not determine a
	// sequence sends lib.OriginSeqUnknown instead, which OriginSeqStated
	// records: the fallback is the same read, but it is a read we know is
	// unverifiable rather than one we assume is fine.
	OriginSeq uint64
	// OriginSeqStated is true when the spawner set lib.EnvOriginSeq at all,
	// whatever it said. It separates "not told" from "told: unknown".
	OriginSeqStated bool

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

// validate refuses a configuration whose failure mode is a hang.
//
// Both checks here are for combinations that connect successfully and then go
// quiet, which is the hardest thing in this system to diagnose from the
// outside: a wrong inbox prefix means every JetStream call and every request
// waits out its timeout with no error anywhere.
func (c Config) validate() error {
	if c.BusTokenFile == "" {
		return nil
	}
	if c.PodName == "" {
		return fmt.Errorf("a bus token file is set but %s is not; the inbox prefix the callout grants is named for the pod, and without it every reply times out", lib.EnvPodName)
	}
	// Empty is the same failure as mismatched, and quieter: Addressee falls
	// back to Profile, so the adapter publishes as `chat` while its grants are
	// derived from the pod. The spawner always sets A2A_SESSION, which is why
	// this is defence in depth rather than a live bug -- but it is the one
	// combination where the wrong addressee is a default rather than a typo.
	if c.Session != c.PodName {
		return fmt.Errorf("%s is %q but A2A_SESSION is %q; the callout derives this session's grants from the pod name, so publishing as %q would be refused and replies would never arrive",
			lib.EnvPodName, c.PodName, c.Session, c.Addressee())
	}
	return nil
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

	// consumerInactiveThreshold reaps a session's named consumers when the
	// pod goes. Named consumers replaced ordered ones (see sessionConsumer),
	// and a named ephemeral is only ephemeral because of this: without it a
	// reaped session leaves three consumers on TASKS forever, and the
	// successor incarnation — which mints a fresh name — leaves three more.
	//
	// Five seconds is this adapter's own number, not an inherited one:
	// nats.go's ordered consumers default to five MINUTES
	// (jetstream/ordered.go, v1.53.1), and an earlier version of this
	// comment cited that default as five seconds, which is wrong by 60x.
	// The short threshold is a deliberate departure from it, and NOT
	// because the threshold only fires after the pod is gone -- an earlier
	// version of this comment claimed that too and it is not true. A
	// disconnect longer than five seconds reaps these consumers with
	// the adapter still very much alive, and because they are MemoryStorage
	// with Replicas 1 a nats-server restart destroys them outright. Both are
	// routine. What makes the short threshold safe is not that the window
	// never opens; it is that consumeIn supervises its own consumer and
	// rebuilds it when it does.
	consumerInactiveThreshold = 5 * time.Second

	// inRecreateAttempts and inRecreateBackoff bound the in consumer's
	// recovery after the server drops it. Five seconds apart for a minute is
	// sized against the thing being waited for -- a nats-server restart, not
	// a network blip -- and against the alternative, which is a task that
	// runs to its 30-minute deadline with steer and cancel silently dead.
	inRecreateAttempts = 12
	inRecreateBackoff  = 5 * time.Second

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
	if err := cfg.validate(); err != nil {
		return Result{}, err
	}
	log := cfg.Logger
	a := &adapter{cfg: cfg, log: log, appended: map[string]bool{}}
	a.from = lib.Party{Session: cfg.Addressee(), AgentType: "claude-code", Profile: cfg.Profile}

	// Two connections, the bridge's split: the lib client owns validated
	// publishes and replay-fold; the raw JetStream handle owns the ordered
	// consumers the lib doesn't expose (origin fetch, live in-subject).
	natsOpts := []nats.Option{
		nats.Name("worker-adapter-" + cfg.Addressee()),
		// Permission violations are asynchronous, and under the callout that
		// makes them nearly invisible: a refused $JS.API publish gets no
		// reply, so the JetStream call does not fail — it waits out its
		// context while the actual reason sits unread on the error handler.
		// The first symptom is a task that does nothing for thirty seconds
		// and then reports a deadline, which names the wrong problem.
		//
		// This does not fix the wait. It makes the reason appear at the
		// moment of refusal, naming the subject, which is the difference
		// between "the bus is slow" and "this session was not granted
		// CONSUMER.CREATE on that name".
		nats.ErrorHandler(func(_ *nats.Conn, sub *nats.Subscription, err error) {
			subject := ""
			if sub != nil {
				subject = sub.Subject
			}
			if errors.Is(err, nats.ErrPermissionViolation) || errors.Is(err, nats.ErrAuthorization) {
				log.Error("the bus refused this session", "err", err, "subject", subject,
					"session", cfg.Addressee(), "pod", cfg.PodName)
				return
			}
			log.Warn("nats async error", "err", err, "subject", subject)
		}),
	}
	switch {
	case cfg.BusTokenFile != "":
		// The per-session credential. The inbox owner is the pod name, which
		// validate() has already checked against A2A_SESSION.
		tokenOpts, err := lib.KSATokenNATSOptions(cfg.BusTokenFile, cfg.PodName)
		if err != nil {
			return Result{}, err
		}
		natsOpts = append(natsOpts, tokenOpts...)
	case cfg.NATSUser != "":
		natsOpts = append(natsOpts,
			nats.UserInfo(cfg.NATSUser, cfg.NATSPassword),
			// JS API replies ride the per-user inbox prefix; without this
			// every JS call times out under the deny-by-default grants —
			// measured on the install, not inferred.
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
	switch prior, err := a.priorEvents(ctx); {
	case err != nil:
		return Result{}, fmt.Errorf("terminal check for %s: %w", cfg.TaskID, err)
	case prior != nil && prior.Final:
		log.Warn("task already terminal on the stream; refusing to re-run",
			"task", cfg.TaskID, "state", prior.State)
		return Result{}, nil
	case prior != nil:
		// Events exist but no terminal: a predecessor incarnation died
		// mid-task and the supervisor has not swept it yet. Publishing a
		// second submitted would lie about the lifecycle; resume at working.
		log.Warn("task has prior non-final events; resuming without submitted",
			"task", cfg.TaskID, "state", prior.State)
		skipSubmitted = true
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

// sessionConsumer creates one of the adapter's three consumers, by name.
//
// Named, not ordered, and the reason is a permission rather than a preference.
// Under per-session credentials a consumer name is a subject token in the
// grant $JS.API.CONSUMER.MSG.NEXT.TASKS.<name>, and a wildcard there would let
// any session pull from — or delete — any consumer on the stream whose name it
// could guess, the gateway's own gateway-relay durable included. So the grant
// pins exact names, and nats.go's ordered consumers cannot be used: the library
// names them <prefix>_<serial>, which no exact grant can cover.
//
// The filter subject rides the CREATE subject when exactly one is set
// (nats.go's apiConsumerCreateWithFilterSubjectT), which is what lets the
// callout pin the filter into the grant itself — so a consumer this session is
// allowed to create can only ever read this session's own subjects. Setting
// FilterSubjects (plural) instead would move the filter into the request body,
// where no subject permission can see it. That is not a style choice; it is
// the difference between a scoped consumer and an unscoped one.
func (a *adapter) sessionConsumer(ctx context.Context, role, filter string, cfg jetstream.ConsumerConfig) (jetstream.Consumer, error) {
	cfg.Name = lib.SessionConsumerName(a.cfg.Addressee(), role)
	cfg.FilterSubject = filter
	cfg.FilterSubjects = nil
	// Ack-none: the adapter reads a durable stream it does not own and its
	// own dedup set is what makes delivery exactly once per envelopeId. No
	// acks also means no $JS.ACK grant, which is one fewer subject a session
	// can reach.
	cfg.AckPolicy = jetstream.AckNonePolicy
	cfg.InactiveThreshold = consumerInactiveThreshold
	cfg.MemoryStorage = true
	cfg.Replicas = 1
	return a.js.CreateOrUpdateConsumer(ctx, lib.TasksStream, cfg)
}

// priorEvents answers the respawn question — has this task already run? — by
// folding the executor's own events subject, or nil when there are none.
//
// It used to be lib.TasksGet, which is a fuller answer and an unreachable one:
// TasksGet calls STREAM.INFO and GetLastMsgForSubject, and neither is a
// subject-scoped operation. STREAM.INFO with a subjects filter enumerates every
// addressee on the bus and a get-by-subject reads any subject in the stream, so
// a session able to make those calls could read the whole task plane. The
// grants withhold both. What is reachable is a consumer on this session's own
// events subject, which is where the answer was all along.
//
// The fold itself is lib.FoldTask, behind the same poison screens the replay
// path uses: one hostile or foreign write on the subject must not turn a
// perfectly runnable task into a boot failure.
func (a *adapter) priorEvents(ctx context.Context) (*lib.Task, error) {
	subject := lib.TaskEventsSubject(a.cfg.Addressee(), a.cfg.TaskID)
	name := lib.SessionConsumerName(a.cfg.Addressee(), lib.SessionConsumerEvents)
	cons, err := a.sessionConsumer(ctx, lib.SessionConsumerEvents, subject, jetstream.ConsumerConfig{
		DeliverPolicy: jetstream.DeliverAllPolicy,
	})
	if err != nil {
		return nil, fmt.Errorf("events consumer on %s: %w", subject, err)
	}
	// Best effort: the inactivity threshold reaps it anyway, and a delete
	// that fails must not fail the task.
	defer func() { _ = a.js.DeleteConsumer(ctx, lib.TasksStream, name) }()

	// FetchNoWait drains what the stream holds now and stops. A task still
	// emitting is not a case here: this runs before the adapter has published
	// anything, so whatever is present belongs to a predecessor.
	var events []*lib.Envelope
	for {
		batch, err := cons.FetchNoWait(originFetchBatch)
		if err != nil {
			return nil, fmt.Errorf("events fetch on %s: %w", subject, err)
		}
		n := 0
		for msg := range batch.Messages() {
			n++
			env, perr := lib.ParseEnvelope(msg.Data())
			switch {
			case perr != nil:
				a.log.Error("respawn check skipping unparseable event", "subject", subject, "err", perr)
			case env.Kind != lib.KindStatusUpdate && env.Kind != lib.KindArtifactUpdate:
				a.log.Error("respawn check skipping non-event kind", "subject", subject, "kind", env.Kind)
			case env.TaskID != a.cfg.TaskID:
				a.log.Error("respawn check skipping event for another task", "subject", subject, "taskId", env.TaskID)
			case env.To != nil && env.To.Session != a.cfg.Addressee():
				a.log.Error("respawn check skipping to/addressee mismatch", "subject", subject, "to", env.To.Session)
			default:
				events = append(events, env)
			}
		}
		if err := batch.Error(); err != nil {
			return nil, fmt.Errorf("events batch on %s: %w", subject, err)
		}
		if n < originFetchBatch {
			break
		}
	}
	if len(events) == 0 {
		return nil, nil
	}
	task, err := lib.FoldTask(a.cfg.TaskID, events)
	if err != nil {
		return nil, fmt.Errorf("folding prior events for %s: %w", a.cfg.TaskID, err)
	}
	return task, nil
}

// fetchOrigin reads the task's originating kind:message envelope off the
// TASKS stream by subject and returns it with its stream sequence.
//
// Told which sequence the submission is (lib.EnvOriginSeq, which the gateway
// sets from its own PubAck), it reads that one and refuses anything else.
// Not told, it falls back to the scan below, which is the historical
// behaviour and is unsafe in one specific way the scan cannot detect — see
// fetchOriginByScan.
func (a *adapter) fetchOrigin(ctx context.Context) (*lib.Envelope, uint64, error) {
	if a.cfg.OriginSeq > 0 {
		return a.fetchOriginAtSeq(ctx, a.cfg.OriginSeq)
	}
	if a.cfg.OriginSeqStated {
		// The spawner is current and still could not name the submission.
		// The scan is all that is left, but a reader should not have to
		// infer from silence that it ran unverified.
		a.log.Warn("the spawner could not name this task's submission; falling back to scanning the in subject, which cannot tell an evicted submission from a steer",
			"task", a.cfg.TaskID)
	}
	return a.fetchOriginByScan(ctx)
}

// fetchOriginAtSeq reads exactly the message the spawner named.
//
// It is a start-sequence consumer rather than a direct get on purpose. A
// get-by-sequence (STREAM.MSG.GET, or DIRECT.GET on an allow-direct stream)
// is not a subject-scoped operation: one grant for it reads every message in
// TASKS, which is the whole task plane, and the callout withholds it for that
// reason (authcallout/session.go). A consumer's filter subject rides its
// CREATE subject and is already granted per session, so this costs no new
// reach at all — only a different DeliverPolicy on a consumer the worker
// already creates.
//
// The refusal is the point. nats-server does not reject a start sequence that
// has been evicted; it silently starts at the next message the filter
// matches, which on this subject is the oldest surviving steer — measured, not
// assumed (TestFetchOriginRefusesASteerWhenTheCapEvictedTheSubmission). So
// the check is on what came back rather than on whether the call errored.
func (a *adapter) fetchOriginAtSeq(ctx context.Context, want uint64) (*lib.Envelope, uint64, error) {
	subject := lib.TaskInSubject(a.cfg.Addressee(), a.cfg.TaskID)
	deadline := time.Now().Add(originFetchDeadline)
	cons, consErr := a.sessionConsumer(ctx, lib.SessionConsumerOrigin, subject, jetstream.ConsumerConfig{
		DeliverPolicy: jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:   want,
	})
	for {
		if consErr == nil {
			batch, err := cons.FetchNoWait(1)
			if err == nil {
				for msg := range batch.Messages() {
					meta, merr := msg.Metadata()
					if merr != nil {
						return nil, 0, fmt.Errorf("origin metadata: %w", merr)
					}
					if meta.Sequence.Stream != want {
						return nil, 0, fmt.Errorf("the submission for task %s is gone from %s: it was stream sequence %d and the oldest message left is %d, so TASKS evicted it (max_msgs_per_subject with discard=old). Refusing to run: the message at %d is a steer or follow-up, not the request",
							a.cfg.TaskID, subject, want, meta.Sequence.Stream, meta.Sequence.Stream)
					}
					env, perr := lib.ParseEnvelope(msg.Data())
					if perr != nil {
						return nil, 0, fmt.Errorf("the submission for task %s at %s sequence %d does not parse: %w", a.cfg.TaskID, subject, want, perr)
					}
					if env.Kind != lib.KindMessage {
						return nil, 0, fmt.Errorf("the submission for task %s at %s sequence %d is kind %q, not %q", a.cfg.TaskID, subject, want, env.Kind, lib.KindMessage)
					}
					return env, meta.Sequence.Stream, nil
				}
			}
		}
		if time.Now().After(deadline) {
			if consErr != nil {
				return nil, 0, fmt.Errorf("consumer on %s: %w", subject, consErr)
			}
			return nil, 0, fmt.Errorf("nothing at or after stream sequence %d on %s within %s", want, subject, originFetchDeadline)
		}
		select {
		case <-ctx.Done():
			return nil, 0, ctx.Err()
		case <-time.After(originFetchPoll):
		}
		if consErr != nil {
			cons, consErr = a.sessionConsumer(ctx, lib.SessionConsumerOrigin, subject, jetstream.ConsumerConfig{
				DeliverPolicy: jetstream.DeliverByStartSequencePolicy,
				OptStartSeq:   want,
			})
		}
	}
}

// fetchOriginByScan takes the first kind:message on the subject.
//
// This is what every worker did before the spawner started naming the
// submission, and it is kept for the shapes that have no spawner to name it:
// a run by hand against a bus with no callout, and the dispatcher-spawned
// shape where the addressee is the profile. It is wrong in one case it cannot
// see. The subject's head is the submission and everything after it is a
// steer, follow-up or cancel, all under one max_msgs_per_subject; steers are
// kind:message too and no envelope field marks the submission, so past the cap
// this returns the oldest surviving steer and the worker executes it as the
// request. Detecting that needs either the stream's first sequence
// (STREAM.INFO) or a get-by-subject, and a session's grants withhold both —
// which is why the fix was to have the spawner say, not to have the worker
// look.
func (a *adapter) fetchOriginByScan(ctx context.Context) (*lib.Envelope, uint64, error) {
	subject := lib.TaskInSubject(a.cfg.Addressee(), a.cfg.TaskID)
	deadline := time.Now().Add(originFetchDeadline)
	// One consumer for the whole wait. Creating it per iteration left up to
	// sixty ephemeral consumers on TASKS behind a slow pod start, each living
	// until its inactivity threshold.
	cons, consErr := a.sessionConsumer(ctx, lib.SessionConsumerOrigin, subject, jetstream.ConsumerConfig{
		DeliverPolicy: jetstream.DeliverAllPolicy,
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
			cons, consErr = a.sessionConsumer(ctx, lib.SessionConsumerOrigin, subject, jetstream.ConsumerConfig{
				DeliverPolicy: jetstream.DeliverAllPolicy,
			})
		}
	}
}

// consumeIn opens the executor's consumer on the task's in subject just after
// the submission - the dual-reader rule's executor half. Steers (kind:message)
// and cancel land here. Delivery to the harness is exactly once per envelopeId
// (assertion 21): the consumer reads without acks, and the dedup set absorbs
// republished duplicates, including the ones a reconnect replays.
//
// It supervises its own consumer, which a named consumer needs and an ordered
// one does not. sessionConsumer explains why these cannot be ordered; the cost
// of that is here. nats.go's ordered consumers reset themselves on
// ErrConsumerDeleted, ErrNoHeartbeat and reconnect (ordered.go), recreating at
// the last delivered sequence. A named pull consumer gets the opposite
// treatment: pull.go classifies ErrConsumerDeleted as terminal and, with no
// ConsumeErrHandler set, calls Stop() on the subscription and returns --
// silently, with nothing logged and nothing closed that the caller can select
// on. The raw nats.Conn reconnects, the harness keeps running to TaskDeadline,
// and every steer and cancel published meanwhile sits unread in TASKS while
// finalize publishes a clean terminal over the top. A lost cancel that looks
// like a completed task is the worst shape this failure could take.
//
// It is not an exotic case. These consumers are MemoryStorage with Replicas 1,
// so a nats-server restart destroys them outright, and InactiveThreshold is
// five seconds, so any disconnect longer than that reaps them without one.
// Server restarts are routine by spec-nats-deployment.md, and NR-4 wants the
// recreate-or-bind decision after a reconnect made explicitly. This is that
// decision: recreate, from the sequence after the last message actually
// handled, so nothing between the old consumer's death and the new one's start
// is skipped. Redelivery across the seam is what the dedup set is for.
func (a *adapter) consumeIn(ctx context.Context, startSeq uint64, originEnvelopeID string, steerCh chan<- string, cancelCh chan<- struct{}) (func(), error) {
	subject := lib.TaskInSubject(a.cfg.Addressee(), a.cfg.TaskID)
	seen := map[string]bool{originEnvelopeID: true}
	var seenMu sync.Mutex
	// nextSeq is the resume point: one past the last message handled, so a
	// recreate picks up where the dead consumer left off rather than at the
	// task's start. Guarded because the supervisor reads it while the
	// Consume callback writes it.
	var seqMu sync.Mutex
	nextSeq := startSeq
	handler := func(msg jetstream.Msg) {
		if md, mdErr := msg.Metadata(); mdErr == nil {
			seqMu.Lock()
			if md.Sequence.Stream >= nextSeq {
				nextSeq = md.Sequence.Stream + 1
			}
			seqMu.Unlock()
		}
		a.handleInMsg(msg, subject, seen, &seenMu, steerCh, cancelCh)
	}

	// The first consumer is created synchronously: a task whose in subject
	// cannot be opened at all has not started, and the caller needs that as
	// an error rather than as a goroutine that fails later.
	cons, err := a.sessionConsumer(ctx, lib.SessionConsumerIn, subject, jetstream.ConsumerConfig{
		DeliverPolicy: jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:   nextSeq,
	})
	if err != nil {
		return nil, fmt.Errorf("in consumer: %w", err)
	}

	superCtx, stopSuper := context.WithCancel(ctx)
	// Buffered by one: the error handler must never block the nats.go
	// callback it runs on, and only the first terminal error of a given
	// consumer matters -- the supervisor replaces the whole thing.
	dead := make(chan error, 1)
	errHandler := func(_ jetstream.ConsumeContext, cErr error) {
		// Non-terminal errors reach here too (missed heartbeats that
		// recovered, leadership changes). They are worth a line and not a
		// recreate; only the terminal ones take the consumer down, and
		// pull.go has already stopped the subscription by the time we see
		// one, so the supervisor's job is to notice, not to race it.
		if errors.Is(cErr, jetstream.ErrConsumerDeleted) || errors.Is(cErr, jetstream.ErrConsumerNotFound) ||
			errors.Is(cErr, jetstream.ErrNoHeartbeat) || errors.Is(cErr, nats.ErrNoResponders) {
			select {
			case dead <- cErr:
			default:
			}
			return
		}
		a.log.Warn("in consumer reported a non-terminal error", "task", a.cfg.TaskID, "err", cErr)
	}

	cc, err := cons.Consume(handler, jetstream.ConsumeErrHandler(errHandler))
	if err != nil {
		stopSuper()
		return nil, fmt.Errorf("consume in subject: %w", err)
	}

	var ccMu sync.Mutex
	current := cc
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			select {
			case <-superCtx.Done():
				return
			case cErr := <-dead:
				seqMu.Lock()
				resume := nextSeq
				seqMu.Unlock()
				// Loud on purpose. The silence is the defect; a steer
				// dropped while this recovers must at least be findable
				// afterwards.
				a.log.Error("in consumer died; recreating",
					"task", a.cfg.TaskID, "resumeSeq", resume, "err", cErr)
				ccMu.Lock()
				current.Stop()
				ccMu.Unlock()
				next, rErr := a.recreateIn(superCtx, subject, resume, handler, errHandler)
				if rErr != nil {
					if superCtx.Err() != nil {
						return
					}
					// Nothing left to try that would not be a second
					// implementation of the retry above it. Say so once,
					// at the level that says steers are gone.
					a.log.Error("in consumer could not be recreated; steer and cancel are dead for this task",
						"task", a.cfg.TaskID, "err", rErr)
					return
				}
				ccMu.Lock()
				current = next
				ccMu.Unlock()
				a.log.Info("in consumer recreated", "task", a.cfg.TaskID, "resumeSeq", resume)
			}
		}
	}()

	return func() {
		stopSuper()
		<-done
		ccMu.Lock()
		current.Stop()
		ccMu.Unlock()
	}, nil
}

// recreateIn rebuilds the in consumer after the server dropped it, retrying
// while the task is still running.
//
// The retry budget is the reconnect budget: a consumer is most often lost
// because the server went away, so the first several attempts are expected to
// fail with no responders and the useful behaviour is to keep asking until the
// bus is back. It is bounded rather than infinite so that a permission change
// mid-task -- a revoked CONSUMER.CREATE grant -- produces a log line and a
// stop instead of a goroutine hammering the bus for the rest of the deadline.
func (a *adapter) recreateIn(ctx context.Context, subject string, resume uint64, handler jetstream.MessageHandler, errHandler jetstream.ConsumeErrHandlerFunc) (jetstream.ConsumeContext, error) {
	var lastErr error
	for attempt := range inRecreateAttempts {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return nil, ctx.Err()
			case <-time.After(inRecreateBackoff):
			}
		}
		cons, err := a.sessionConsumer(ctx, lib.SessionConsumerIn, subject, jetstream.ConsumerConfig{
			DeliverPolicy: jetstream.DeliverByStartSequencePolicy,
			OptStartSeq:   resume,
		})
		if err != nil {
			lastErr = err
			continue
		}
		cc, err := cons.Consume(handler, jetstream.ConsumeErrHandler(errHandler))
		if err != nil {
			lastErr = err
			continue
		}
		return cc, nil
	}
	return nil, fmt.Errorf("after %d attempts: %w", inRecreateAttempts, lastErr)
}

// handleInMsg is the body of the in subject's Consume callback, lifted out so
// that the consumer can be recreated around it without the handler being
// rebuilt: the dedup set has to survive the recreate, or a redelivered steer
// is applied twice.
func (a *adapter) handleInMsg(msg jetstream.Msg, subject string, seen map[string]bool, seenMu *sync.Mutex, steerCh chan<- string, cancelCh chan<- struct{}) {
	{
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
	}
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
