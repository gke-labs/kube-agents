// Package hermesbridge is the stand-in executor for tasks addressed to the
// platform profile: it consumes a2a.tasks.{profile}.*.in, answers each task
// as a turn on the pod's Hermes API server (one session per contextId) or,
// on the cli executor, by one `hermes -p {profile} chat -Q --query=<prompt>` per
// task, with a follow-up message that arrives mid-task queued and run as the
// next turn in the same session (`--resume` on the cli executor), and
// publishes the payload spec's lifecycle events with the answer as
// the result artifact. It is
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
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unicode/utf8"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// taskQueueCapacity bounds the accepted-but-not-started queue; hitting
	// it on a playground bridge is a fault, not load.
	taskQueueCapacity = 1024
	// steerQueueCapacity bounds the follow-ups one task queues in total,
	// those whose turn has started or that left the queue refused included,
	// not just those waiting: a task runs at most this many follow-ups, so
	// at most this many turns past the opening one. The number is the
	// worker adapter's steer bound (16). The next one is refused queue-full
	// with a notice, never dropped silently.
	steerQueueCapacity = 16
	// stderrTailBytes and stdoutTailBytes are how much of each stream a
	// failed task's status message carries. stdout matters on failure too:
	// `hermes chat -Q` prints a failed turn's final_response (its own
	// "Error: …" summary when the retries gave up) on stdout and exits 1,
	// so a terminal that kept stderr alone threw the diagnosis away (#2036).
	stderrTailBytes = 2048
	stdoutTailBytes = 2048
	// publishErrTailBytes bounds the publish error a bus-publish-failed
	// terminal quotes. The tail, because a wrapped error ends in its cause.
	publishErrTailBytes = 2048
	// rateLimitedExitCode is EX_TEMPFAIL, the code Hermes exits with when a
	// turn gave up on the provider's rate limit; the terminal names it so a
	// quota storm is not graded as the persona's failure.
	rateLimitedExitCode = 75
	// The task lookups behind handleMessage and cancelOrphan retry, because
	// the lib acks after the handler returns and exposes no nak, so a
	// transient read failure used to drop the delivery for good (#2043).
	// Two schedules, because the two reads meet different faults. A new
	// submission has no events, so its lookup is answered by the direct
	// horizon gets without opening a consumer and cannot meet the TASKS
	// consumer cap; what it can meet is a bus hiccup on those gets, worth one
	// quick retry and no more, since the durable's handler is serial and a
	// long wait here holds every other delivery behind a message that ends
	// as "ignoring" anyway. An orphan's cancel reads a task that has events,
	// which opens the consumer and can be refused at the cap; that refusal
	// clears when the consumers holding the cap are reaped, after the lib's
	// inactive threshold (lib.EphemeralConsumerInactiveThreshold, 5s), so
	// its waits (1s, 2s, 3s) outlast it.
	submissionLookupAttempts = 2
	submissionLookupBackoff  = 200 * time.Millisecond
	cancelLookupAttempts     = 4
	cancelLookupBackoff      = time.Second
	// finalizePublishTimeout bounds the result+terminal publishes of one
	// finalize; it must outlast a NATS reconnect, not a task.
	finalizePublishTimeout = 20 * time.Second
	// registryClearTimeout bounds the KV delete after a terminal publish.
	registryClearTimeout = 10 * time.Second
	// lookAheadTimeout bounds the worker's pre-spawn read of the task's in
	// subject. A read that outlives it is a read failure, and a read failure
	// spawns: the bound keeps a slow bus from parking a worker slot, it never
	// drops the task.
	lookAheadTimeout = 10 * time.Second
	// steerNoticeTimeout bounds the steer notices of one call (a follow-up's
	// one notice, or the refusals of a closing queue). They publish outside
	// run.mu, so a stalled bus holds no lock a kill needs; what waits is
	// whatever orders itself behind them (noticeMu) and, because the
	// consumer runs one handler at a time, a cancel delivered behind the
	// stalled one. The bound keeps that wait finite.
	steerNoticeTimeout = 10 * time.Second

	shutdownReason            = "reason: bridge-shutdown - the bridge was terminated while this task was in flight"
	canceledBeforeStartReason = "reason: canceled-before-start"
)

// sessionIDLine is the last thing `hermes chat -Q` writes on stderr:
// "session_id: <id>". The id finds the transcript under the profile's session
// store, which is the evidence the status message cannot carry whole, and a
// follow-up turn resumes it. The id's shape is checked, not just its
// presence: it lands in a child's argv after --resume, and Hermes's own ids
// (<timestamp>_<hex>) need nothing wider.
var sessionIDLine = regexp.MustCompile(`^session_id:[ \t]*([A-Za-z0-9][A-Za-z0-9_.:-]{0,127})$`)

// Config wires one bridge. Zero values get playground defaults in Run.
type Config struct {
	// NATSURL is the bus address.
	NATSURL string
	// Profile is the addressee token the bridge executes for ("platform").
	Profile string
	// Executor is how a task runs: ExecutorAPI posts it as a turn in the
	// conversation's Hermes session through the pod's API server;
	// ExecutorCLI spawns Command per task (api.go has the why). The zero
	// value is ExecutorCLI, so a bridge under test calls no server it did
	// not ask for; the daemon defaults to ExecutorAPI.
	Executor string
	// APIURL, APIKey and APIModel are the API executor's endpoint, bearer
	// token (the pod's API_SERVER_KEY, which the server needs before it
	// honours the session headers) and model name. URL and model default
	// to DefaultAPIURL and DefaultAPIModel; the key has no default.
	APIURL   string
	APIKey   string
	APIModel string
	// APIConnectRetry is how long a refused connection to the API server is
	// retried before the task ends hermes-api-unreachable; zero is
	// DefaultAPIConnectRetry.
	APIConnectRetry time.Duration
	// ActivitySecret signs the API executor's hook deliveries: the pod's
	// managed config carries one hooks.outbound entry for the whole
	// gateway with secret_env A2A_ACTIVITY_SECRET, and the door attributes
	// a delivery to a task by the session id in its payload. Empty leaves
	// the API executor's tasks without a trace (the CLI executor's children
	// carry their own per-task key either way).
	ActivitySecret string
	// Command is the CLI executor's invocation prefix; the task prompt is
	// appended as the final argument, or replaces a trailing -q as one
	// "--query=<prompt>" token (promptArgv). Default: ["hermes", "-p",
	// <profile>, "chat", "-Q", "-q"].
	Command []string
	// Concurrency caps simultaneous tasks, hermes subprocesses or API
	// requests (default 2, the platform profile's concurrency in the
	// profiles spec).
	Concurrency int
	// TaskDeadline is the task's wall-clock ceiling, counted from its first
	// turn and spanning every follow-up turn after it (default 7200s,
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
	// ActivityListen is the loopback address of the activity door, where
	// hermes's outbound webhooks deliver the persona's tool calls
	// (activity.go). Empty leaves the door closed: no listener, no key in
	// the child's environment, no activity artifact. The daemon defaults it
	// to DefaultActivityListen; the zero value here is "off" so a bridge
	// under test binds nothing it did not ask for.
	ActivityListen string
	// ManagedScopeDir is hermes's managed scope as this process sees it:
	// the directory whose config.yaml and .env each child's own scope is
	// copied from before the hook is added (activity.go). Empty means
	// nothing to copy and a hook-only scope; the daemon resolves it from
	// $HERMES_MANAGED_DIR, else /etc/hermes (cmd/hermes-bridge), so the
	// library reads no environment and a test's bridge copies nothing from
	// the machine it runs on.
	ManagedScopeDir string
	// ScratchDir holds the per-task managed scopes (default: hermes-bridge
	// under the temp dir). Each is removed when its child exits.
	ScratchDir string
	// ProgressInterval is the heartbeat cadence on the progress artifact.
	// Zero takes the default (60s), as every other field here does; a
	// negative value turns the heartbeat off. The daemon maps its
	// environment's 0 to that, since "0 seconds" can only mean off there.
	ProgressInterval time.Duration
	// Scope is the resource path this executor operates in, and it is what
	// the capability is checked against: not "may this capability do
	// anything" but "may it execute here". It has to be a scope the
	// gateway's ceiling contains, which for an unconfigured install is
	// `namespace/<the agent's namespace>`; the bridge shares that pod, so
	// the two agree by construction. Resolved by the caller, never
	// defaulted here — an executor that invented its own scope would be
	// answering the question it was asked to pose.
	Scope capability.Scope
	// CapabilityOptional governs exactly one thing: what a submission with
	// no capability at all means. Zero value — the safe one — refuses it.
	// Set, it executes and says so at WARN. That is the mixed-version
	// window: a gateway that predates the mint in front of an executor
	// that enforces it, and nothing else.
	//
	// It is NOT a switch for enforcement. A capability that is present is
	// always checked and its refusal is always honoured; there is no
	// configuration in which this bridge runs work a verifier refused.
	CapabilityOptional bool
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
	if c.Executor == "" {
		c.Executor = ExecutorCLI
	}
	if c.APIURL == "" {
		c.APIURL = DefaultAPIURL
	}
	if c.APIConnectRetry <= 0 {
		c.APIConnectRetry = DefaultAPIConnectRetry
	}
	if c.APIModel == "" {
		c.APIModel = DefaultAPIModel
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
	if c.ProgressInterval == 0 {
		c.ProgressInterval = DefaultProgressInterval
	}
	if c.ScratchDir == "" {
		c.ScratchDir = filepath.Join(os.TempDir(), "hermes-bridge")
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

	// noticeMu orders this run's status publishes that cannot ride under
	// mu: every steer notice, the working status, and finalize, which holds
	// it from its first look at the run to the terminal. A notice decided
	// under mu is published under noticeMu with mu released, so a stalled
	// bus never holds mu - a cancel's kill still runs - while finalize,
	// queued behind noticeMu, still writes its terminal after every notice.
	// Lock order: noticeMu, then mu; never the reverse.
	noticeMu sync.Mutex
	// mu guards state, proc, and killTimers - and is held across the
	// finalize's terminal publish: whoever holds it sees the true state.
	mu         sync.Mutex
	state      runState
	proc       *exec.Cmd
	killTimers []*time.Timer
	// cancelReq ends the API executor's request (api.go); nil outside one.
	// Under mu like proc, which it is the counterpart of.
	cancelReq context.CancelFunc

	// steers are the follow-ups waiting for the current turn to end, oldest
	// first. steersQueued counts every follow-up this run has queued, the
	// ones since taken off steers included, and is what steerQueueCapacity
	// bounds: the task's total, not what waits at one moment. seenSteers is
	// every follow-up envelope this run has answered, so a redelivery is
	// answered once.
	// turnsClosed is set when the worker has chosen the current answer as
	// the deliverable (nothing queued to run) or finalize began: a
	// follow-up after the first is refused task-ending, never queued behind
	// a terminal. One after finalize began waits on noticeMu, finds the run
	// done and is dropped with a warning; the gateway's relay reports it as
	// missed. All three under mu.
	steers       []*lib.Envelope
	steersQueued int
	seenSteers   map[string]bool
	turnsClosed  bool
	// workingSent is set once the working status is on the stream
	// (publishWorking), under mu: a notice reads it for the task's current
	// state, so none can say submitted after working.
	workingSent bool

	canceled    atomic.Bool
	deadlineHit atomic.Bool

	// act is the task's side of the activity door (activity.go): its
	// signing key, the calls seen, the heartbeat's lifecycle. Stored before
	// the subprocess starts, so no delivery can precede it, and atomic
	// because the door reads it with no lock held, after copying the runs
	// under b.mu, while the worker writes it under mu - the two locks never
	// nest, on purpose.
	act atomic.Pointer[activityState]
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

	// turns serializes the API executor's turns per Hermes session (api.go).
	turns sessionTurns

	// closing marks shutdown, so a worker whose subprocess died to the
	// shutdown SIGKILL reports bridge-shutdown, not a bogus exit code.
	closing atomic.Bool

	// lookAhead is the worker's pre-spawn read for a trailing cancel,
	// cancelInStream by default; a field so a test can stall it or make it
	// fail without a bus that misbehaves on cue.
	lookAhead func(ctx context.Context, run *taskRun) (bool, error)

	// deliver is what the durable calls with each envelope on the in
	// subject, handle by default; a field so a test can hold one delivery
	// back and pin which path wrote a record.
	deliver func(ctx context.Context, env *lib.Envelope)

	// replaySlots paces the look-ahead's fallback replay: one slot per
	// worker, held from the replay's start until the ephemeral's inactive
	// threshold after it returns, so the slots in hand are the consumers the
	// look-ahead is holding, Concurrency at most plus whatever the server
	// has not yet reaped, whatever shape the backlog has. The operator's
	// TASKS reserve counts twice the default Concurrency, not the configured
	// one (a2aTasksReplayBridgeLookAhead and its tail factor), because it
	// leaves BRIDGE_CONCURRENCY unset.
	replaySlots chan struct{}

	// holdReplaySlot schedules release of a slot whose replay opened a
	// consumer: after the ephemeral's inactive threshold by default; a field
	// so a test can keep the release pending and count slots in hand without
	// racing the timer.
	holdReplaySlot func(release func())

	// apiClient is the API executor's HTTP client (api.go).
	apiClient *http.Client
	// The activity door (activity.go); nil when Config.ActivityListen is "".
	activityLn   net.Listener
	activitySrv  *http.Server
	activityDone chan struct{} // closed by closeActivity, so serveActivity's shutdown waiter stops too
	activityOnce sync.Once
	// tasksGet is the task lookup lookupTask retries, in the shape of
	// lib.Client.TasksGetOpened; nil means the client's. Tests set it to
	// drive the retry without a bus fault.
	tasksGet func(ctx context.Context, addressee, taskID string) (*lib.Task, bool, error)
}

// New connects and sweeps but does not consume yet; Run does.
func New(ctx context.Context, cfg Config) (*Bridge, error) {
	cfg.defaults()
	switch cfg.Executor {
	case ExecutorAPI:
		if err := apiExecutorValid(&cfg); err != nil {
			return nil, err
		}
	case ExecutorCLI:
	default:
		return nil, fmt.Errorf("unknown executor %q: %q or %q", cfg.Executor, ExecutorAPI, ExecutorCLI)
	}
	b := &Bridge{
		cfg: cfg,
		from: lib.Party{
			Session:   cfg.Profile + "-bridge",
			AgentType: "hermes-bridge",
			Profile:   cfg.Profile,
		},
		tasks:       make(map[string]*taskRun),
		queue:       make(chan *taskRun, taskQueueCapacity),
		replaySlots: make(chan struct{}, cfg.Concurrency),
		apiClient:   newAPIClient(),
	}
	b.lookAhead = b.cancelInStream
	b.holdReplaySlot = func(release func()) { time.AfterFunc(lib.EphemeralConsumerInactiveThreshold, release) }
	b.deliver = b.handle
	var err error
	b.c, err = lib.Connect(ctx, cfg.NATSURL,
		lib.WithName(b.from.Session),
		lib.WithLogger(cfg.Logger),
		lib.WithNATSOptions(cfg.NATSOptions...))
	if err != nil {
		return nil, err
	}
	// An async error handler, for the same reason the session executor has one
	// (worker-adapter/adapter.go) and one this connection made sharper.
	//
	// This is the connection the capability check rides: capability.NewClient
	// publishes on a2a.cap.verify.<profile> and subscribes to
	// a2a.cap.reply.<profile>.*. NATS refuses either asynchronously, and nats.go
	// delivers that -ERR only to the async handler -- with none installed it is
	// dropped on the floor. Check then simply times out, and the bridge
	// publishes "the verifier could not be reached" as the task's terminal
	// reason. So a bridge missing or mis-spelling one of its two verify grants
	// refuses every task while every log line and every terminal event blames
	// the verifier Deployment, which is the wrong team's pager and the wrong
	// hour of debugging.
	//
	// The handler does not change any of those outcomes. It makes the refusal
	// name itself at the moment it happens, which is the difference between "the
	// verifier is down" and "this bridge was never granted the subject".
	natsOpts := append([]nats.Option{
		nats.Name(b.from.Session + "-kv"), nats.MaxReconnects(-1),
		nats.ErrorHandler(func(_ *nats.Conn, sub *nats.Subscription, err error) {
			subject := ""
			if sub != nil {
				subject = sub.Subject
			}
			if errors.Is(err, nats.ErrPermissionViolation) || errors.Is(err, nats.ErrAuthorization) {
				cfg.Logger.Error("the bus refused this bridge", "err", err, "subject", subject,
					"profile", cfg.Profile, "session", b.from.Session)
				return
			}
			cfg.Logger.Warn("nats async error", "err", err, "subject", subject)
		}),
	}, cfg.NATSOptions...)
	b.nc, err = nats.Connect(cfg.NATSURL, natsOpts...)
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
	if err := b.listenActivity(); err != nil {
		b.close()
		return nil, err
	}
	return b, nil
}

func (b *Bridge) close() {
	b.c.Close()
	b.nc.Close()
	b.closeActivity()
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
	b.serveActivity(ctx)
	for i := 0; i < b.cfg.Concurrency; i++ {
		b.wg.Add(1)
		go b.worker(ctx)
	}
	sub, err := b.c.SubscribeDurable(ctx, lib.SubscribeConfig{
		Stream:  lib.TasksStream,
		Subject: fmt.Sprintf("a2a.tasks.%s.*.in", b.cfg.Profile),
		Durable: "bridge-" + b.cfg.Profile,
		Session: b.cfg.Profile,
	}, func(env *lib.Envelope) { b.deliver(ctx, env) })
	if err != nil {
		return fmt.Errorf("subscribe: %w", err)
	}
	b.cfg.Logger.Info("hermes bridge consuming", "profile", b.cfg.Profile, "executor", b.cfg.Executor)
	if b.cfg.Executor == ExecutorAPI && b.activityLn != nil && b.cfg.ActivitySecret == "" {
		// The door is open but nothing can be attributed through it: the
		// pod's hook signs with a secret this bridge was not given.
		b.cfg.Logger.Warn("activity door open with no ActivitySecret: API tasks carry no tool trace",
			"env", ActivitySecretEnv)
	}
	if b.cfg.Executor == ExecutorAPI && b.activityLn != nil && !activityHookReaches(b.activityLn.Addr()) {
		// The pod's hook posts to one address, and the operator renders it
		// only for a door there; a door elsewhere hears nothing.
		b.cfg.Logger.Warn("activity door not where the pod-wide hook posts: API tasks carry no tool trace",
			"listen", b.activityLn.Addr().String(), "hook", DefaultActivityListen)
	}
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
		if r.state == stateRunning && r.cancelReq != nil {
			r.cancelReq()
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
		b.queueSteer(ctx, run, env)
		return
	}
	// Unknown task: the dispatcher rule. Empty events subject means new;
	// terminal means acked with a warning; non-final events with no local run
	// is an orphan a follow-up cannot revive.
	task, attempts, err := b.lookupTask(ctx, env.TaskID, submissionLookupAttempts, submissionLookupBackoff)
	switch {
	case isTaskNotFound(err):
		b.accept(ctx, env)
	case err != nil && ctx.Err() != nil:
		// The bridge is stopping and the lookup did not run its course. The
		// outcome is the same as the drop below (the lib acks after this
		// handler returns, on a connection Run closes only after the
		// handlers), so the submission is lost either way; its own line
		// keeps a reader counting cap incidents by the drop line from
		// counting restarts.
		b.cfg.Logger.Error("events lookup interrupted by shutdown; submission dropped (acked, no nak path)",
			"task", env.TaskID, "attempts", attempts, "err", err)
	case err != nil:
		// The lib acks after this handler returns, so the submission is
		// dropped, not redelivered - no terminal event will follow. Honest
		// gap: the lib exposes no nak path yet; lookupTask's retries are
		// what stands in for one.
		b.cfg.Logger.Error("events lookup failed after retries; dropping submission",
			"task", env.TaskID, "attempts", attempts, "err", err)
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
	if run.state == stateRunning && run.cancelReq != nil {
		run.cancelReq()
	}
	run.mu.Unlock()
	if pending {
		// Not yet spawned: terminal now; the worker skips done runs.
		b.finalize(run, lib.StateCanceled, canceledBeforeStartReason, nil)
	}
	// For a running task the runner publishes terminal canceled on exit.
}

// cancelOrphan handles cancel for a task the bridge is not running: if its
// events show it non-final (a prior incarnation died mid-task and the sweep
// has no key for it), synthesize terminal canceled under CAS. Terminal or
// absent tasks get a warning and nothing else.
func (b *Bridge) cancelOrphan(ctx context.Context, env *lib.Envelope) {
	// The usual orphan cancel is the durable delivering, behind a
	// submission the look-ahead already refused, the very cancel it read:
	// the task is final, and its newest event says so with no consumer. The
	// fold below, and the ephemeral it costs, are for everything else.
	if b.lastEventIsFinal(ctx, env.TaskID) {
		b.cfg.Logger.Warn("cancel for a task with a terminal event; ignoring", "task", env.TaskID)
		return
	}
	task, attempts, err := b.lookupTask(ctx, env.TaskID, cancelLookupAttempts, cancelLookupBackoff)
	switch {
	case isTaskNotFound(err):
		b.cfg.Logger.Warn("cancel for a task with no events; ignoring", "task", env.TaskID)
	case err != nil && ctx.Err() != nil:
		b.cfg.Logger.Error("cancel events lookup interrupted by shutdown; cancel dropped",
			"task", env.TaskID, "attempts", attempts, "err", err)
	case err != nil:
		b.cfg.Logger.Error("cancel events lookup failed after retries", "task", env.TaskID, "attempts", attempts, "err", err)
	case task.Final:
		b.cfg.Logger.Warn("cancel for a task with a terminal event; ignoring", "task", env.TaskID)
	default:
		if err := b.synthesizeTerminal(ctx, env.TaskID, lib.StateCanceled,
			"reason: canceled-while-orphaned - no live executor held this task"); err != nil {
			b.cfg.Logger.Error("orphan cancel synthesis failed", "task", env.TaskID, "err", err)
		}
	}
}

// lastEventIsFinal reads the newest message on each of the task's replay
// subjects and reports whether one is a final status-update. A read that
// fails, or finds nothing final, answers false and leaves the question to
// the fold: this is a shortcut past the fold's consumer, not the decision.
func (b *Bridge) lastEventIsFinal(ctx context.Context, taskID string) bool {
	for _, subject := range lib.TaskReplaySubjects(b.cfg.Profile, taskID) {
		env, err := b.c.LastEnvelope(ctx, subject)
		if err != nil {
			b.cfg.Logger.Warn("newest event read failed; folding instead",
				"task", taskID, "subject", subject, "err", err)
			return false
		}
		if lib.IsFinalStatus(env) {
			return true
		}
	}
	return false
}

// queueSteer answers a follow-up to a task this bridge holds. Queued, it
// runs as a further turn in the task's Hermes session after the current
// one; refused, the requester is told why. Either way one non-final notice
// carrying the task's CURRENT state - a follow-up must not change folded
// state by itself (assertion 12). The decision is made under mu; the notice
// goes out under noticeMu with mu released (publishSteerNotices), so it
// still lands ahead of the final event finalize writes behind noticeMu. A
// follow-up that reaches noticeMu after finalize gets no notice: the run is
// done, and the gateway's relay counts it as missed.
func (b *Bridge) queueSteer(ctx context.Context, run *taskRun, steer *lib.Envelope) {
	run.noticeMu.Lock()
	defer run.noticeMu.Unlock()
	run.mu.Lock()
	if run.state == stateDone {
		run.mu.Unlock()
		b.cfg.Logger.Warn("follow-up for a task with a terminal event; ignoring", "task", steer.TaskID)
		return
	}
	if steer.EnvelopeID == run.origin.EnvelopeID || run.seenSteers[steer.EnvelopeID] {
		run.mu.Unlock()
		return // the submission or a follow-up redelivered: already answered
	}
	if run.seenSteers == nil {
		run.seenSteers = make(map[string]bool)
	}
	run.seenSteers[steer.EnvelopeID] = true
	n := lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: steer.EnvelopeID}
	_, hasText := promptFromMessage(steer.Payload)
	switch {
	case !hasText:
		n.Reason = lib.SteerReasonNoText
	case run.turnsClosed:
		n.Reason = lib.SteerReasonTaskEnding
	case run.steersQueued >= steerQueueCapacity:
		n.Reason = lib.SteerReasonQueueFull
	default:
		run.steers = append(run.steers, steer)
		run.steersQueued++
		n = lib.SteerNotice{Steer: lib.SteerQueued, EnvelopeID: steer.EnvelopeID}
	}
	run.mu.Unlock()
	b.publishSteerNotices(ctx, run, []lib.SteerNotice{n})
}

// refuseQueued closes the run's turns and refuses every follow-up still
// queued, for reason, oldest first. The caller holds run.noticeMu and NOT
// run.mu: the queue is taken under mu and the refusals publish without it.
// After it returns nothing more can be queued on the run (turnsClosed); a
// later follow-up is refused task-ending by queueSteer, unless the run is
// done by the time it gets noticeMu, when it is dropped.
func (b *Bridge) refuseQueued(ctx context.Context, run *taskRun, reason string) {
	run.mu.Lock()
	run.turnsClosed = true
	queued := run.steers
	run.steers = nil
	run.mu.Unlock()
	ns := make([]lib.SteerNotice, 0, len(queued))
	for _, s := range queued {
		ns = append(ns, lib.SteerNotice{Steer: lib.SteerRefused, EnvelopeID: s.EnvelopeID, Reason: reason})
	}
	b.publishSteerNotices(ctx, run, ns)
}

// nextSteer is the follow-up to run next: the oldest queued one, left at the
// head of the queue until its turn starts (takeSteerLocked), so a finalize
// that comes first still finds it there and refuses it task-ended. With none
// to run - the queue is empty, or the task was canceled, hit its deadline or
// the bridge is stopping - it closes the queue and returns nil: the current
// answer is the deliverable, and whatever is still queued is refused
// task-ended by the caller's finalize. Closing and that finalize are two
// critical sections; a follow-up between them sees turnsClosed and is
// refused task-ending, still before the final event.
func (b *Bridge) nextSteer(run *taskRun) *lib.Envelope {
	run.mu.Lock()
	defer run.mu.Unlock()
	if run.state != stateRunning || run.canceled.Load() || run.deadlineHit.Load() || b.closing.Load() || len(run.steers) == 0 {
		run.turnsClosed = true
		return nil
	}
	return run.steers[0]
}

// takeSteerLocked removes steer from the head of the queue, where nextSteer
// left it, and reports whether it was there: false means finalize has taken
// the queue (and refused it). Caller holds run.mu. A follow-up leaves the
// queue only here - when its turn starts, or when it is refused at the head -
// so it is never out of the queue without its turn or its notice, and
// nothing ever has to go back to the head.
func takeSteerLocked(run *taskRun, steer *lib.Envelope) bool {
	if len(run.steers) == 0 || run.steers[0] != steer {
		return false
	}
	run.steers = run.steers[1:]
	return true
}

// runnableSteer finds the follow-up to run next: the capability it carries
// (the task's, minted at submission) must still pass the check, because it
// is a turn now, not a note: a revoked or expired capability stops it. A refused one
// is told so, taken off the queue and skipped. Runs on the worker, never on
// the durable's callback, for capabilityPermits's reason. nil means there is
// none to run and the caller's answer is the deliverable; anything still
// queued is refused task-ended by the caller's finalize.
func (b *Bridge) runnableSteer(ctx context.Context, run *taskRun) (*lib.Envelope, string) {
	for {
		steer := b.nextSteer(run)
		if steer == nil {
			return nil, ""
		}
		reason := b.capabilityRefusal(ctx, steer)
		if reason == "" {
			prompt, _ := promptFromMessage(steer.Payload) // text was checked when it was queued
			return steer, prompt
		}
		if ctx.Err() != nil {
			// Stopping, not refused (capabilityPermits says why the two
			// differ): left queued, for finalize to refuse task-ended.
			return nil, ""
		}
		run.noticeMu.Lock()
		run.mu.Lock()
		took := run.state == stateRunning && takeSteerLocked(run, steer)
		run.mu.Unlock()
		if took {
			b.publishSteerNotices(ctx, run, []lib.SteerNotice{
				{Steer: lib.SteerRefused, EnvelopeID: steer.EnvelopeID, Reason: lib.SteerReasonCapability}})
		}
		run.noticeMu.Unlock()
		if !took {
			return nil, "" // finalized meanwhile; it refused this one with the rest
		}
	}
}

// closeTurns closes the queue and refuses what it holds, for reason. A no-op
// once the task is final: its finalize refused the queue already.
func (b *Bridge) closeTurns(run *taskRun, reason string) {
	run.noticeMu.Lock()
	defer run.noticeMu.Unlock()
	run.mu.Lock()
	done := run.state == stateDone
	run.mu.Unlock()
	if !done {
		b.refuseQueued(context.Background(), run, reason)
	}
}

// publishTurnAnswer ships a finished turn's answer as a turn artifact,
// because a follow-up is about to run after it. Under noticeMu with the run
// still running, so it lands before the final event finalize writes behind
// noticeMu. false means the task is final: it already was, or the publish
// failed and this finalized it failed. Either way the follow-up is still at
// the head of the queue, and that finalize refused it task-ended.
func (b *Bridge) publishTurnAnswer(run *taskRun, turn int, text string) bool {
	run.noticeMu.Lock()
	run.mu.Lock()
	running := run.state == stateRunning
	run.mu.Unlock()
	if !running {
		run.noticeMu.Unlock()
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), finalizePublishTimeout)
	err := b.publishTextArtifact(ctx, run, fmt.Sprintf("artifact-%s-turn-%d", run.origin.TaskID, turn), lib.ArtifactTurn, text)
	cancel()
	run.noticeMu.Unlock()
	if err != nil {
		b.cfg.Logger.Error("turn answer publish failed", "task", run.origin.TaskID, "turn", turn, "err", err)
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: bus-publish-failed at turn %d - %s", turn, tail(err.Error(), publishErrTailBytes)), nil)
		return false
	}
	return true
}

// turnNote names the turn in a failed terminal's reason after the first.
func turnNote(turn int) string {
	if turn <= 1 {
		return ""
	}
	return fmt.Sprintf("; turn: %d", turn)
}

// noticeStateLocked is the task's current state for a non-final notice:
// working once the working status is on the stream, submitted before it -
// while queued, or on the API executor while waiting for its session's
// turn. It reads workingSent, not anything set later (the activity state),
// so a notice just after working never folds the task back. Caller holds
// run.mu.
func (b *Bridge) noticeStateLocked(run *taskRun) lib.TaskState {
	if run.state == statePending || !run.workingSent {
		return lib.StateSubmitted
	}
	return lib.StateWorking
}

// steerNoticeText is the notice's text part, for a reader that does not
// know the data part (an older gateway posts it as "ℹ️ …"). Each says only
// what is true for its reason: a task that has ended does not "continue".
func steerNoticeText(n lib.SteerNotice) string {
	if n.Steer == lib.SteerQueued {
		return "follow-up queued: it runs as the next turn in this conversation when the current turn finishes"
	}
	prefix := "follow-up not taken (" + n.Reason + "): "
	switch n.Reason {
	case lib.SteerReasonQueueFull:
		return prefix + fmt.Sprintf("this task has already taken its %d follow-ups (run or waiting); it continues without this one. "+
			"Send it again after the answer.", steerQueueCapacity)
	case lib.SteerReasonNoText:
		return prefix + "the message has no text to ask; the task continues without it."
	case lib.SteerReasonTaskEnding:
		return prefix + "the task's answer was already chosen, so no further turn will run. Send it again after the answer."
	case lib.SteerReasonTaskEnded:
		return prefix + "the task ended before this follow-up's turn, so it never ran. Send it again as a new message."
	case lib.SteerReasonCapability:
		return prefix + "the task's capability check did not pass when its turn came (refused, or the verifier could not be reached), so it did not run."
	case lib.SteerReasonNoResume:
		return prefix + "the conversation could not be continued for it, so it did not run."
	}
	return prefix + "it did not run."
}

// publishSteerNotices publishes each notice on a non-final status carrying
// the task's current state, read under mu just before each publish, on one
// context bounded by steerNoticeTimeout and detached from ctx's
// cancellation (a notice owed at shutdown still goes out, like finalize's).
// The caller holds run.noticeMu and NOT run.mu.
func (b *Bridge) publishSteerNotices(ctx context.Context, run *taskRun, ns []lib.SteerNotice) {
	if len(ns) == 0 {
		return
	}
	pctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), steerNoticeTimeout)
	defer cancel()
	for _, n := range ns {
		part, err := lib.SteerNoticePart(n)
		if err == nil {
			run.mu.Lock()
			state := b.noticeStateLocked(run)
			run.mu.Unlock()
			err = b.publishStatusParts(pctx, run, state, false,
				[]lib.Part{{Kind: "text", Text: steerNoticeText(n)}, part})
		}
		if err != nil {
			b.cfg.Logger.Error("steer notice publish failed", "task", run.origin.TaskID,
				"steer", n.Steer, "reason", n.Reason, "envelope", n.EnvelopeID, "err", err)
		}
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
		if !run.pending() {
			continue
		}
		// The authorization gate runs BEFORE the look-ahead, and the order
		// is deliberate. The look-ahead is not free and it is not private:
		// its fallback replay opens an ephemeral consumer and holds a
		// replaySlot for the inactive threshold, and replaySlots is bounded
		// at Concurrency and shared by every worker. Running it first would
		// let an unauthorized submission spend a bus consumer and park a
		// slot that authorized tasks queue behind -- work done on behalf of
		// a requester who was never entitled to any, which is the property
		// this branch exists to establish. The verifier round trip is a
		// request-reply on a subject with no shared bounded resource behind
		// it, so it is the cheaper of the two to spend on a task that turns
		// out to be refused.
		//
		// It also decides the record: an unauthorized submission terminates
		// `rejected` with the capability's reason rather than `canceled`,
		// which is the honest terminal and the one an audit can act on.
		if !b.capabilityPermits(ctx, run) {
			continue
		}
		// The look-ahead runs with the task still pending, so a cancel the
		// durable delivers meanwhile takes handleCancel's queued path as
		// before, and the re-check below sees its finalize.
		canceled, err := b.lookAhead(ctx, run)
		switch {
		case canceled:
			// The requester's word, read in full, outranks a shutdown that
			// lands the same instant: finalize publishes on its own context,
			// so the cancel is recorded even then.
			b.cfg.Logger.Info("cancel already on the stream; not spawning", "task", run.origin.TaskID)
			b.finalize(run, lib.StateCanceled, canceledBeforeStartReason, nil)
			continue
		case ctx.Err() != nil:
			// Shutdown reached the worker mid-read. Leave the run pending
			// for shutdownTasks, whose terminal names the real cause; a
			// spawn now would fail its working publish on the dead context
			// and report bus-publish-failed instead.
			return
		case err != nil:
			// A read failure spawns. The cancel, if there is one, still
			// arrives on the durable and kills the run - today's bound -
			// where a failure that dropped the task would leave it open
			// with no terminal event.
			b.cfg.Logger.Warn("cancel look-ahead failed; spawning anyway",
				"task", run.origin.TaskID, "err", err)
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

// pending reports whether the run is still queued: neither spawned nor
// finalized.
func (r *taskRun) pending() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.state == statePending
}

// cancelInStream is the worker's look-ahead: has a cancel for this task
// already landed on its in subject, behind the submission the durable just
// delivered? The durable delivers serially and acks after the handler, so a
// cancel published before this bridge bound - the eval harness's abandonment
// of a submission nobody took, or any cancel inside the retention window - is
// dispatched only after accept returns, by which time an idle worker has the
// run. Reading the subject closes that gap. It is a read, not a consume: the
// durable still delivers the cancel to handle afterwards. By then finalize
// has normally removed the run from the table, so that cancel takes the
// orphan path, which finds the terminal and does nothing; one that lands
// before the removal finds a finalized run and does nothing either.
//
// The subject's newest message answers almost every bind with one direct
// get and no consumer: a cancel there is newer than the submission, and the
// submission there means nothing followed it. Only a subject whose newest
// message is something else, a follow-up behind a cancel say, is replayed in
// full, on the five-second ephemeral the replay costs. That keeps a bind
// that finds a backlog of abandoned submissions from turning each into a
// consumer slot at bus speed, which on a 64-consumer TASKS would have failed
// the look-ahead and spawned the stale prompts the read exists to refuse.
// The replay that remains is paced through replaySlots, so a backlog of the
// shape that needs it, a follow-up behind a cancel or a newest message the
// screen drops, is refused at Concurrency tasks per threshold window rather
// than at bus speed: the look-ahead's consumer cost has the ceiling the
// operator's reserve gives it, whatever the backlog looks like. The wait
// for a slot is bounded by the threshold itself and is not charged to
// either read's own bound, and it is not spent on a run the durable's
// cancel has already ended on handleCancel's queued path, which on a live
// bridge is where that cancel usually lands while the direct get is out.
//
// The negative answer rests on the submission's envelope id being on the
// subject once. The server dedups a re-publish of the same id inside the
// stream's duplicates window, and the durable drops one on delivery; a copy
// stored after that window would sit newest, hide a cancel between the two
// copies from the direct get, and give that task the record the bridge
// gave every cancelled task before the look-ahead: a spawn the durable's
// cancel kills inside the kill grace, canceled-by-request. That takes a
// writer with rights on the task's in subject re-sending an identical
// envelope minutes later, which no publisher in the tree does and which
// could as well submit a fresh task; it is named here rather than paid for
// with a replay on every spawn.
//
// Newer than the submission means after it in stream order. The submission
// is normally in the replay, since the durable delivered it moments ago;
// when it is not - the per-subject cap evicted it - everything left is
// newer, and a cancel among it counts. Nothing here filters on `to`: the
// replay already drops an envelope whose `to` disagrees with the subject's
// addressee, the same screen the durable applies before handle sees one.
func (b *Bridge) cancelInStream(ctx context.Context, run *taskRun) (bool, error) {
	getCtx, cancelGet := context.WithTimeout(ctx, lookAheadTimeout)
	last, err := b.c.LastEnvelope(getCtx, lib.TaskInSubject(b.cfg.Profile, run.origin.TaskID))
	cancelGet()
	if err != nil {
		return false, err
	}
	switch {
	case last != nil && last.Kind == lib.KindCancel:
		return true, nil
	case last != nil && last.EnvelopeID == run.origin.EnvelopeID:
		return false, nil
	}
	// A finalized run has nothing to look ahead for; the worker's re-check
	// reads the same state after this returns.
	if !run.pending() {
		return false, nil
	}
	release, err := b.takeReplaySlot(ctx)
	if err != nil {
		return false, err
	}
	if !run.pending() {
		release(false)
		return false, nil
	}
	replayCtx, cancelReplay := context.WithTimeout(ctx, lookAheadTimeout)
	defer cancelReplay()
	envs, opened, err := b.c.TaskInReplay(replayCtx, b.cfg.Profile, run.origin.TaskID)
	release(opened)
	if err != nil {
		return false, err
	}
	from := 0
	for i, env := range envs {
		if env.EnvelopeID == run.origin.EnvelopeID {
			from = i + 1
			break
		}
	}
	for _, env := range envs[from:] {
		if env.Kind == lib.KindCancel {
			return true, nil
		}
	}
	return false, nil
}

// takeReplaySlot admits one fallback replay, waiting when Concurrency of them
// are in hand. The caller invokes release when the replay has returned,
// saying whether it opened a consumer: if it did, the slot is held from that
// instant for the ephemeral's inactive threshold, which is the clock the
// server reaps the consumer on, so the slot and the consumer live the same
// span and the slots in hand are the consumers the look-ahead is holding;
// if it did not (the run was finalized during the wait, the subject was
// empty, the read failed before its consumer existed; a read that failed
// after creating it, a timeout mid-iteration say, reports it opened), the
// slot comes back at once,
// since holding it would delay the next replay for a consumer that never
// existed. Releasing at acquisition instead would have let a replay slower
// than the threshold hold a third consumer per worker. A canceled context is
// the only other way out of the wait.
func (b *Bridge) takeReplaySlot(ctx context.Context) (release func(opened bool), err error) {
	select {
	case b.replaySlots <- struct{}{}:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	return func(opened bool) {
		if !opened {
			<-b.replaySlots
			return
		}
		b.holdReplaySlot(func() { <-b.replaySlots })
	}, nil
}

// capabilityPermits is the authorization gate, and it runs HERE — on a
// worker, after the queue — rather than in accept, which is where the check
// first landed. Both placements are before `working` publishes, before hermes
// is invoked and before a model is called, so nothing about what is refused
// changes. What changes is who waits: accept runs on the bridge's one durable
// consumer callback, which is serial and which also carries the KindCancel
// envelopes for tasks that are already running. Check blocks for
// capability.DefaultTimeout when nothing answers on the verify subject, so a
// verifier outage made every cancel queue behind 5s per pending submission —
// a user unable to stop a running hermes subprocess because of an outage in
// the thing that authorizes new ones. The session executor has always checked
// in its own process for the same reason (a2a/worker-adapter/adapter.go).
//
// The cost of the move: a submission now occupies a queue slot while it is
// being verified, so a verifier outage long enough to queue taskQueueCapacity
// of them ends in bridge-queue-overflow rather than capability-refused. At
// Concurrency workers and one DefaultTimeout each that is thousands of
// submissions inside one outage on a single-profile bridge, which is the
// "fault, not load" the capacity already stands for.
//
// Returns false when it finalized the task; the caller must not run it.
func (b *Bridge) capabilityPermits(ctx context.Context, run *taskRun) bool {
	reason := b.capabilityRefusal(ctx, run.origin)
	if reason == "" {
		return true
	}
	// "The verifier could not be reached" and "we stopped asking" are
	// different facts, and Check cannot tell them apart: it turns any
	// error out of NextMsgWithContext, context.Canceled included, into
	// a refusal. So a submission whose check was in flight when the
	// bridge was terminated would land as terminal `rejected` -- which
	// no supervisor retries -- instead of the retryable shutdown every
	// other pending task gets. The verifier guards the mirror image of
	// this on its own side; see capability.DrainAndCancel.
	if ctx.Err() != nil {
		b.finalize(run, lib.StateFailed, shutdownReason, nil)
		return false
	}
	b.finalize(run, lib.StateRejected, reason, nil)
	return false
}

func (b *Bridge) runTask(ctx context.Context, run *taskRun) {
	if b.cfg.Executor == ExecutorAPI {
		b.runTaskAPI(ctx, run)
		return
	}
	taskID := run.origin.TaskID
	prompt, ok := promptFromMessage(run.origin.Payload)
	if !ok {
		b.finalize(run, lib.StateRejected,
			"reason: no-text-parts - the submission message carries nothing the hermes CLI can be asked", nil)
		return
	}
	if err := b.publishWorking(ctx, run); errors.Is(err, errRunEnded) {
		return // canceled or shut down first; its finalize wrote the terminal
	} else if err != nil {
		b.cfg.Logger.Error("working publish failed", "task", taskID, "err", err)
		b.finalize(run, lib.StateFailed, "reason: bus-publish-failed at working", nil)
		return
	}

	// The activity door's side of this task: a signing key in the child's
	// environment when the door is open, and the heartbeat either way. One
	// for the task: every turn's child carries the same key.
	act := newActivityState(b.activityLn != nil)
	var env []string // nil: the child inherits, as with the door closed
	if b.activityLn != nil {
		scope, err := b.childManagedScope(taskID)
		if err != nil {
			b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: spawn-failed - %v", err), nil)
			return
		}
		defer func() {
			// The scope holds the managed .env; a removal that fails leaves
			// it in the shared scratch dir until the next start's sweep.
			if err := os.RemoveAll(scope); err != nil {
				b.cfg.Logger.Warn("child scope not removed", "task", run.origin.TaskID, "scope", scope, "err", err)
			}
		}()
		env = append(os.Environ(), act.childEnv(b.ActivityURL(), scope)...)
	}
	// One deadline for the task, every turn included: the profile's
	// activeDeadlineSeconds is the task's, not a turn's.
	deadlineAt := time.Now().Add(b.cfg.TaskDeadline)
	deadline := time.AfterFunc(b.cfg.TaskDeadline, func() {
		run.deadlineHit.Store(true)
		run.mu.Lock()
		if run.state == stateRunning && run.proc != nil && run.proc.Process != nil {
			b.killGroup(run, run.proc.Process.Pid)
		}
		run.mu.Unlock()
	})
	defer deadline.Stop()

	argv := promptArgv(b.cfg.Command, prompt)
	var steer *lib.Envelope // the follow-up this turn runs; nil on turn 1
	for turn := 1; ; turn++ {
		out, stderr, err, spawned := b.cliTurn(run, argv, env, act, steer, deadlineAt)
		if !spawned {
			return
		}
		if err != nil {
			b.finalizeCLIError(run, err, out, stderr, turn)
			return
		}
		// Resumable at all? Asked before a follow-up is looked at, so a
		// refusal covers every queued one and this answer is the result.
		sessionID := lastSessionID(stderr)
		if _, rerr := resumeArgv(b.cfg.Command, "x", "x"); sessionID == "" || rerr != nil {
			b.closeTurns(run, lib.SteerReasonNoResume)
			b.finalize(run, lib.StateCompleted, "", &out)
			return
		}
		next, prompt := b.runnableSteer(ctx, run)
		if next == nil {
			// A canceled task whose turn finished anyway won the race:
			// completed wins, per the payload spec's cancel mapping.
			b.finalize(run, lib.StateCompleted, "", &out)
			return
		}
		if !b.publishTurnAnswer(run, turn, out) {
			return
		}
		steer = next
		argv, _ = resumeArgv(b.cfg.Command, sessionID, prompt)
	}
}

// promptArgv is a turn's command: the configured command with the prompt as
// its final argument. A command ending in Hermes's -q gets the prompt as one
// "--query=<prompt>" token instead, so a prompt that starts with "-" stays
// the query: after a bare -q, argparse reads a dash-led token with no space
// in it ("--force") as an option and the child exits 2, and Hermes's own
// pre-parse scans match whole tokens ("--help", "--tui", "-p"). Any other
// command gets the prompt appended as it is. Either way the prompt is
// argvText's, without NUL bytes.
func promptArgv(command []string, prompt string) []string {
	n := len(command)
	if n == 0 || command[n-1] != "-q" {
		return append(append([]string(nil), command...), argvText(prompt))
	}
	return append(append([]string(nil), command[:n-1]...), "--query="+argvText(prompt))
}

// argvText is a turn's prompt as an argv string can carry it: without NUL
// bytes, which exec refuses in any argument (EINVAL), so a message holding
// one would fail its turn, and with it the task, as spawn-failed. Every
// turn's prompt passes through here, the opening one and each follow-up's.
func argvText(prompt string) string {
	return strings.ReplaceAll(prompt, "\x00", "")
}

// resumeArgv is a follow-up turn's command: the configured command with
// "--resume <id>" in place of its trailing -q, then the prompt as
// promptArgv writes it. A command that does not end in -q has no place for
// the flag; the follow-ups are refused no-resume rather than guessed at.
func resumeArgv(command []string, sessionID, prompt string) ([]string, error) {
	n := len(command)
	if n == 0 || command[n-1] != "-q" {
		return nil, fmt.Errorf("command %q does not end in -q", command)
	}
	argv := append([]string(nil), command[:n-1]...)
	return append(argv, "--resume", sessionID, "--query="+argvText(prompt)), nil
}

// lastSessionID is the id on a child's last non-blank stderr line, or "".
// The CLI prints its own "session_id:" line last, after anything a tool's
// nested run echoed. Only that line is read: stderr is shared with the
// child's tool subprocesses, so an earlier match may be another session's
// id, and a later line means the CLI's own was not last. A label with
// nothing after it, or an id outside sessionIDLine's shape, is no id at all,
// and a follow-up then has nothing to resume.
func lastSessionID(stderr string) string {
	lines := strings.Split(stderr, "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		line := strings.TrimRight(lines[i], " \t\r")
		if line == "" {
			continue
		}
		if m := sessionIDLine.FindStringSubmatch(line); m != nil {
			return m[1]
		}
		return ""
	}
	return ""
}

// cliTurn runs one hermes child for run and waits for it. steer is the
// follow-up the turn runs, nil on turn 1; it leaves the queue as the child
// starts. spawned is false when the task is final: it already was, or this
// finalized it - the spawn failed, the turn found the task past deadlineAt,
// or a follow-up's turn found it canceled or the bridge stopping (no child
// starts once the deadline has fired, or it would outlive the kill). The
// activity state is stored and its publisher started on turn 1 only; later
// children reuse the same key in env.
func (b *Bridge) cliTurn(run *taskRun, argv, env []string, act *activityState, steer *lib.Envelope, deadlineAt time.Time) (stdout, stderr string, err error, spawned bool) {
	first := steer == nil
	cmd := exec.Command(argv[0], argv[1:]...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var out strings.Builder
	errTail := newTailBuffer(stderrTailBytes)
	cmd.Stdout, cmd.Stderr, cmd.Env = &out, errTail, env
	if !time.Now().Before(deadlineAt) {
		run.deadlineHit.Store(true) // due, its timer just has not run yet
	}
	run.mu.Lock()
	if run.state != stateRunning {
		// Shutdown or cancel finalized first.
		run.mu.Unlock()
		return "", "", nil, false
	}
	// The deadline on every turn, turn 1 included: its timer is armed before
	// turn 1's spawn, and one that fired first found no child to kill. A
	// cancel on turn 1 still spawns and kills below, as before.
	if run.deadlineHit.Load() || (!first && (run.canceled.Load() || b.closing.Load())) {
		// Checked under the lock the deadline timer and the cancel kill take,
		// after each has stored its flag: one that lands after this check
		// finds run.proc set and kills the child.
		run.mu.Unlock()
		b.finalizeCLIError(run, errTurnNotStarted, "", "", 0)
		return "", "", nil, false
	}
	if !first && (len(run.steers) == 0 || run.steers[0] != steer) {
		// A finalize has taken the queue (and refused it) and is on its
		// way to the terminal. Unreachable while every finalizer sets a flag
		// checked above; a child here would contradict its notice.
		run.mu.Unlock()
		b.cfg.Logger.Error("follow-up gone from the queue head before its turn; not spawning",
			"task", run.origin.TaskID, "envelope", steer.EnvelopeID)
		return "", "", nil, false
	}
	if first {
		run.act.Store(act)
	}
	if serr := cmd.Start(); serr != nil {
		// The follow-up stays at the head: the finalize below refuses it
		// task-ended.
		if first {
			run.act.Store(nil) // no child, so no publisher to join
		}
		run.mu.Unlock()
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: spawn-failed - %v", serr), nil)
		return "", "", nil, false
	}
	if !first {
		takeSteerLocked(run, steer) // its turn has started: no notice owed now; at the head, checked above under this lock
	}
	run.proc = cmd
	if first {
		go b.runActivity(run)
		// Cancel may have raced the spawn: its kill saw no process, so
		// re-check under the same lock its kill path takes.
		if run.canceled.Load() {
			b.killGroup(run, cmd.Process.Pid)
		}
	}
	run.mu.Unlock()
	werr := cmd.Wait()
	// The group is gone; stop any armed grace-period SIGKILLs before the
	// pgid can be recycled onto an innocent process, and forget the child,
	// so a deadline between turns signals nothing.
	run.mu.Lock()
	for _, t := range run.killTimers {
		t.Stop()
	}
	run.killTimers = nil
	run.proc = nil
	run.mu.Unlock()
	return out.String(), errTail.String(), werr, true
}

// errTurnNotStarted is what a turn that found the task stopped before its
// spawn carries into finalizeCLIError: no child ran, so none was killed.
var errTurnNotStarted = errors.New("the turn's child was not started")

// finalizeCLIError names why a child's turn ended without an answer: the
// deadline, a cancel, shutdown, else the child's non-zero exit and its
// evidence. A follow-up turn (turn ≥ 2) the deadline or shutdown killed is
// named, as a non-zero exit is.
func (b *Bridge) finalizeCLIError(run *taskRun, err error, stdout, stderr string, turn int) {
	switch {
	case run.deadlineHit.Load() && errors.Is(err, errTurnNotStarted):
		// Found before the spawn: between two turns, or before the first.
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: deadline-exceeded - reached after %s before the next turn started; no child was running", b.cfg.TaskDeadline), nil)
	case run.deadlineHit.Load():
		b.finalize(run, lib.StateFailed,
			fmt.Sprintf("reason: deadline-exceeded - killed after %s%s", b.cfg.TaskDeadline, turnNote(turn)), nil)
	case run.canceled.Load():
		b.finalize(run, lib.StateCanceled, "reason: canceled-by-request", nil)
	case b.closing.Load():
		// Killed by shutdownTasks; name the real cause, not the exit code.
		b.finalize(run, lib.StateFailed, shutdownReason+turnNote(turn), nil)
	default:
		b.finalize(run, lib.StateFailed, failureReason(err, stdout, stderr, turn), nil)
	}
}

// failureReason is the terminal message for a subprocess that exited
// non-zero: the reason token, the exit error, the session id if hermes
// printed one, and a bounded tail of each stream. Exit 75 (EX_TEMPFAIL) is
// the rate-limit exit and gets its own token; everything else is
// hermes-exited-nonzero. After turn 1 the reason names the turn that failed
// (turnNote), straight after the session. Newlines are kept: the message is a text part, and
// the tails are read by a person.
func failureReason(err error, stdout, stderr string, turn int) string {
	token := "hermes-exited-nonzero"
	var exit *exec.ExitError
	if errors.As(err, &exit) && exit.ExitCode() == rateLimitedExitCode {
		token = "hermes-rate-limited"
	}
	var sb strings.Builder
	fmt.Fprintf(&sb, "reason: %s - %v", token, err)
	if id := lastSessionID(stderr); id != "" {
		fmt.Fprintf(&sb, "; session: %s", id)
	}
	sb.WriteString(turnNote(turn))
	fmt.Fprintf(&sb, "; stdout tail: %s; stderr tail: %s", tail(stdout, stdoutTailBytes), stderr)
	return sb.String()
}

// resultPublishFailedReason is the terminal message for a result artifact
// the bus refused: the token, then a bounded tail of the cause, so the
// reason a person reads says why (an envelope over the bus's maximum, a
// timeout) and not only that it failed.
func resultPublishFailedReason(err error) string {
	return "reason: bus-publish-failed at result - " + tail(err.Error(), publishErrTailBytes)
}

// tail is the last n bytes of s, cut on a rune boundary so the text part
// stays valid UTF-8.
func tail(s string, n int) string {
	if len(s) <= n {
		return s
	}
	cut := len(s) - n
	for cut < len(s) && !utf8.RuneStart(s[cut]) {
		cut++
	}
	return s[cut:]
}

// lookupTask is TasksGet with a bounded retry (the schedules are the
// constants above). A not-found answer is an answer and returns at once. An error
// from before the read opened its consumer (a creation refusal, which is
// what a consumer-cap refusal is) is retried with a growing backoff; an error
// from after it is not, because that consumer is now live for the inactive
// threshold and each retry would open another against the same cap, which
// is the multiplication the look-ahead's replay slots exist to prevent. The
// count is how many lookups were made, which is what the drop log reports: a
// context that ends the loop after one attempt (a shutdown mid-handler) is
// one, not the bound.
func (b *Bridge) lookupTask(ctx context.Context, taskID string, maxAttempts int, backoff time.Duration) (task *lib.Task, attempts int, err error) {
	get := b.tasksGet
	if get == nil {
		get = b.c.TasksGetOpened
	}
	for attempts = 1; attempts <= maxAttempts; attempts++ {
		var opened bool
		task, opened, err = get(ctx, b.cfg.Profile, taskID)
		if err == nil || isTaskNotFound(err) || ctx.Err() != nil {
			return task, attempts, err
		}
		if opened {
			b.cfg.Logger.Warn("task lookup failed after its consumer was created; not retried",
				"task", taskID, "attempt", attempts, "err", err)
			return task, attempts, err
		}
		if attempts < maxAttempts {
			b.cfg.Logger.Warn("task lookup failed; retrying",
				"task", taskID, "attempt", attempts, "of", maxAttempts, "err", err)
			select {
			case <-time.After(backoff * time.Duration(attempts)):
			case <-ctx.Done():
				return nil, attempts, ctx.Err()
			}
		}
	}
	return task, maxAttempts, err
}

// finalize is the single writer of a task's terminal event, idempotent: the
// first caller wins, later callers see stateDone and leave. A non-nil
// resultOutput publishes the result artifact ahead of the terminal event
// inside the same critical section, so a racing finalizer cannot slip its
// final in between. Publishes ride a fresh bounded context, never the
// caller's - the terminal event must go out even when the caller's context
// is already canceled, which is exactly what shutdown looks like.
func (b *Bridge) finalize(run *taskRun, state lib.TaskState, msg string, resultOutput *string) {
	// noticeMu first and for the whole of it: no steer notice can be in
	// flight past this, and a second finalizer waits here, then leaves on
	// stateDone, as it always waited on mu.
	run.noticeMu.Lock()
	defer run.noticeMu.Unlock()
	run.mu.Lock()
	if run.state == stateDone {
		run.mu.Unlock()
		return
	}
	run.mu.Unlock()
	// What is still queued will never get its turn: each is refused
	// task-ended ahead of the terminal, published with mu released so a
	// stalled bus cannot hold a cancel's kill. turnsClosed keeps the queue
	// empty from here; nothing but finalize sets stateDone, and finalize is
	// serialized on noticeMu, so the state is still not done below.
	b.refuseQueued(context.Background(), run, lib.SteerReasonTaskEnded)
	run.mu.Lock()
	// The trace first, while the state still admits it: any call still
	// open, and the budget marker if calls were cut, go out ahead of the
	// result inside this critical section, so the activity artifact is
	// complete and nothing of it can follow the final event.
	b.drainActivity(run)
	run.state = stateDone
	ctx, cancel := context.WithTimeout(context.Background(), finalizePublishTimeout)
	if resultOutput != nil {
		if err := b.publishResult(ctx, run, *resultOutput); err != nil {
			b.cfg.Logger.Error("result publish failed", "task", run.origin.TaskID, "err", err)
			state, msg = lib.StateFailed, resultPublishFailedReason(err)
		}
	}
	err := b.publishTerminal(ctx, run, state, msg)
	cancel()
	run.mu.Unlock()
	b.waitActivity(run)
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

// errRunEnded is publishWorking finding the run already finalized: there is
// nothing to report and nothing to finalize, the caller just returns.
var errRunEnded = errors.New("run finalized before its working status")

// publishWorking publishes the run's working status under noticeMu, so a
// steer notice that read the run as still submitted lands before it, never
// after: a late "submitted" would fold the task backwards. It publishes only
// if the run is still running when noticeMu is held, and answers errRunEnded
// otherwise. finalize drops mu while it refuses the queue, so a worker can
// take a pending run in that gap and get here; finalize sets stateDone only
// under noticeMu, so the check under it is final both ways - a finalize that
// began first has written its terminal, and one that begins later waits for
// this working.
func (b *Bridge) publishWorking(ctx context.Context, run *taskRun) error {
	run.noticeMu.Lock()
	defer run.noticeMu.Unlock()
	run.mu.Lock()
	running := run.state == stateRunning
	run.mu.Unlock()
	if !running {
		return errRunEnded
	}
	if err := run.exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		return err
	}
	run.mu.Lock()
	run.workingSent = true
	run.mu.Unlock()
	return nil
}

// publishStatusMessage is PublishStatus with a status.message attached -
// the lib's TaskExecution doesn't carry one, and reasons ride there.
func (b *Bridge) publishStatusMessage(ctx context.Context, run *taskRun, state lib.TaskState, final bool, text string) error {
	return b.publishStatusParts(ctx, run, state, final, []lib.Part{{Kind: "text", Text: text}})
}

// publishStatusParts is publishStatusMessage with the message's parts given.
func (b *Bridge) publishStatusParts(ctx context.Context, run *taskRun, state lib.TaskState, final bool, parts []lib.Part) error {
	origin := run.origin
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID:    origin.TaskID,
		ContextID: origin.ContextID,
		Status: lib.TaskStatus{
			State: state,
			Message: &lib.Message{
				Role:      "agent",
				MessageID: "msg-" + nuid.Next(),
				Parts:     parts,
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

// publishResult ships stdout as the result artifact.
func (b *Bridge) publishResult(ctx context.Context, run *taskRun, output string) error {
	return b.publishTextArtifact(ctx, run, "artifact-"+run.origin.TaskID+"-result", lib.ArtifactResult, output)
}

// publishTextArtifact ships text as one artifact, chunked per A2A rules so
// one huge answer never trips the max-message-size gate.
func (b *Bridge) publishTextArtifact(ctx context.Context, run *taskRun, artifactID, name, text string) error {
	chunks := chunkString(text, b.cfg.ResultChunkSize)
	for i, chunk := range chunks {
		payload, err := json.Marshal(lib.ArtifactUpdate{
			TaskID:    run.origin.TaskID,
			ContextID: run.origin.ContextID,
			Artifact: lib.Artifact{
				ArtifactID: artifactID,
				Name:       name,
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

// String is the kept tail, opened on a rune boundary so the text part it
// becomes is valid UTF-8 (the byte cut in Write can land mid-rune).
func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	start := 0
	for start < len(t.buf) && !utf8.RuneStart(t.buf[start]) {
		start++
	}
	return string(t.buf[start:])
}
