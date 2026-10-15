package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// watchPoll is how long one idle pull on the task watcher waits before
	// asking again. A pending pull keeps the consumer active, so it is the
	// gap between pulls, not this, that the five-second inactivity
	// threshold measures; the value only paces how often an idle pod asks.
	watchPoll = 30 * time.Second
	// watchRecreateAttempts and watchRecreateBackoff bound the watcher's
	// recovery when its consumer cannot be created or keeps failing, sized
	// like the in consumer's (a nats-server restart, not a blip). Past them
	// the process exits: the pod reaches a terminal phase, the gateway's
	// sweep closes any task routed to it, and the next turn spawns a fresh
	// pod. An idle pod that cannot see its own subjects is worse than none.
	watchRecreateAttempts = 12
	watchRecreateBackoff  = 5 * time.Second
	// maxPendingBehindSubmission is how many messages may wait behind a new
	// task's first message for the watcher to take it as the submission
	// (take). An eviction by the per-subject cap leaves at least the cap less
	// one behind it, so the check holds on any TASKS whose cap is above this
	// plus one: the render's is 4096, and an older stream has none. The
	// gateway publishes one message per chat message, so nothing a person or
	// a wake's follow-ups do stacks this many behind a submission in the
	// seconds the watcher may lag.
	maxPendingBehindSubmission = 32
)

// runSessionLoop is a reused session pod's life after its first task: wait for
// the conversation's next task on the pod's own subjects, run it, report its
// terminal, and wait again, until SIGTERM.
//
// Each task runs exactly as the first did (execute), with two differences
// that are the point of keeping the pod: the harness resumes the session the
// previous run left (--resume), so the model has the whole conversation
// without a primer, and nothing here pays a pod start.
//
// The loop ends, and the process with it, on:
//   - SIGTERM while idle: nothing is owed to anyone, so nothing is published
//     and the Result is zero (exit 0). This is the gateway's reap, its cap
//     eviction, or a node drain.
//   - SIGTERM mid-task: that task's eviction terminal, as in a one-task pod.
//   - a task that ended without its terminal on the bus, or before the
//     adapter could tell: the pod must reach a terminal phase so the
//     gateway's sweep closes the task, rather than sit live with it open.
//   - a harness run that reported no session: a next turn run without one
//     would have neither the conversation nor the primer, and a resumed run
//     that failed before saying may have failed on the session it was
//     given. Exiting hands the next turn to a cold start, which has the
//     primer. The one exception is a resumed turn stopped before the
//     harness's init line: the previous session is still there, so the loop
//     keeps it and goes on.
//   - a turn refused before its harness ran, for a reason in the pod's own
//     state (turnOutcome.podBroken): every later turn here would be refused
//     the same way, and a cold start has a clean pod.
func runSessionLoop(ctx context.Context, cfg Config, b *busConns, firstTask string, firstSeq uint64, first turnOutcome) (Result, error) {
	log := cfg.Logger
	w := &taskWatcher{
		js:      b.js,
		log:     log,
		stem:    cfg.consumerStem(),
		addr:    cfg.Addressee(),
		next:    firstSeq + 1,
		handled: map[string]bool{firstTask: true},
	}
	primer, resume := cfg.Primer, ""
	out := first
	for {
		if out.launched {
			switch {
			case out.harnessSession != "":
				// The primer went into a harness session that said it
				// exists, so every later run resumes it instead.
				resume, primer = out.harnessSession, ""
			case resume != "" && out.res.State == lib.StateCanceled:
				log.Warn("the turn was stopped before the harness reported its session; keeping the one it was resuming",
					"task", out.lastTask(cfg), "session", resume)
			default:
				log.Warn("the harness reported no session to resume; exiting so the next turn starts cold with the conversation so far",
					"task", out.lastTask(cfg))
				return out.res, out.err
			}
		}
		if out.podBroken {
			log.Warn("the turn was refused before its harness ran; exiting so the next turn starts on a fresh pod",
				"task", out.lastTask(cfg))
			return out.res, out.err
		}
		if out.err != nil || out.res.Evicted || ctx.Err() != nil {
			return out.res, out.err
		}
		origin, seq, err := w.nextTask(ctx)
		if err != nil {
			if ctx.Err() != nil {
				log.Info("SIGTERM while idle; exiting with nothing to publish")
				return Result{}, nil
			}
			return Result{}, fmt.Errorf("waiting for the next task: %w", err)
		}
		log.Info("next task taken from the session's own subjects", "task", origin.TaskID, "seq", seq,
			"resume", resume != "")
		turn := cfg
		turn.TaskID = origin.TaskID
		out = newTaskAdapter(turn, b).execute(ctx, origin, seq, turnPlan{primer: primer, resume: resume})
		out.task = origin.TaskID
	}
}

// lastTask names the task an outcome belongs to, for a log line.
func (o turnOutcome) lastTask(cfg Config) string {
	if o.task != "" {
		return o.task
	}
	return cfg.TaskID
}

// taskWatcher finds the next task addressed to this session. It reads every
// `…in` subject of the session from one position onward, through the one
// consumer name the callout grants for it with exactly this filter
// (authcallout/session.go: CREATE on `<pod>-origin` filtered
// `a2a.tasks.<pod>.*.in`), so it reaches nothing the first task's origin
// fetch could not. A new task is the first kind:message on a task id this pod
// has not run; everything else on those subjects is a steer, follow-up or
// cancel for a task that has already ended, and is passed over.
type taskWatcher struct {
	js   jetstream.JetStream
	log  *slog.Logger
	stem string
	addr string
	// next is the stream sequence the next read starts at: one past the
	// last message looked at, so a recreated consumer neither repeats nor
	// skips anything.
	next    uint64
	handled map[string]bool
}

// nextTask blocks until a new task's submission arrives, or ctx ends.
func (w *taskWatcher) nextTask(ctx context.Context) (*lib.Envelope, uint64, error) {
	filter := lib.TaskInSubject(w.addr, "*")
	failures := 0
	for {
		cons, err := w.consumer(ctx, filter)
		if err == nil {
			if failures > 0 {
				// The pair to the Warn below, so a live run shows the
				// recovery and not only the loss.
				w.log.Info("task watcher recreated its consumer", "filter", filter, "resumeSeq", w.next)
			}
			var env *lib.Envelope
			var seq uint64
			var healthy bool
			env, seq, healthy, err = w.read(ctx, cons)
			if err == nil {
				return env, seq, nil
			}
			if errors.Is(err, errSubmissionUnproven) {
				// Not the consumer's fault, and a new consumer would read
				// the same thing.
				return nil, 0, err
			}
			if healthy {
				failures = 0
			}
		}
		if ctx.Err() != nil {
			return nil, 0, ctx.Err()
		}
		failures++
		if failures >= watchRecreateAttempts {
			return nil, 0, fmt.Errorf("task watcher on %s failed %d times in a row: %w", filter, failures, err)
		}
		w.log.Warn("task watcher lost its consumer; recreating", "filter", filter, "resumeSeq", w.next, "err", err)
		select {
		case <-ctx.Done():
			return nil, 0, ctx.Err()
		case <-time.After(watchRecreateBackoff):
		}
	}
}

// consumer (re)creates the watcher's consumer at w.next. The name is the
// origin role's, which the first task's fetch already used with another
// filter and start, and a consumer's deliver policy cannot be updated in
// place, so the old one is deleted first; not finding it is the usual case.
func (w *taskWatcher) consumer(ctx context.Context, filter string) (jetstream.Consumer, error) {
	name := lib.SessionConsumerName(w.stem, lib.SessionConsumerOrigin)
	_ = w.js.DeleteConsumer(ctx, lib.TasksStream, name)
	return w.js.CreateOrUpdateConsumer(ctx, lib.TasksStream, sessionConsumerConfig(name, filter, jetstream.ConsumerConfig{
		DeliverPolicy: jetstream.DeliverByStartSequencePolicy,
		OptStartSeq:   w.next,
	}))
}

// read pulls from the consumer until a new task arrives. healthy reports
// whether at least one pull completed before the error, which separates a
// consumer that worked and then was lost (a reconnect, a server restart)
// from one that never worked.
func (w *taskWatcher) read(ctx context.Context, cons jetstream.Consumer) (*lib.Envelope, uint64, bool, error) {
	healthy := false
	for {
		batch, err := cons.Fetch(1, jetstream.FetchMaxWait(watchPoll))
		if err != nil {
			return nil, 0, healthy, err
		}
		msgs := batch.Messages()
		for msgs != nil {
			select {
			case <-ctx.Done():
				return nil, 0, healthy, ctx.Err()
			case msg, ok := <-msgs:
				if !ok {
					msgs = nil
					continue
				}
				env, seq, isTask, err := w.take(msg)
				if err != nil {
					return nil, 0, true, err
				}
				if isTask {
					return env, seq, true, nil
				}
			}
		}
		// An expired pull is the idle case, not a failure.
		if berr := batch.Error(); berr != nil && !errors.Is(berr, nats.ErrTimeout) && !errors.Is(berr, jetstream.ErrNoMessages) {
			return nil, 0, healthy, berr
		}
		healthy = true
	}
}

// errSubmissionUnproven is the watcher refusing a new task whose first message
// it cannot show is the submission (take says how it tells).
var errSubmissionUnproven = errors.New("cannot show the first message of a new task is its submission")

// take decides whether one message is a new task's submission, advancing the
// watcher's position either way. An error is a new task it refuses to run.
//
// The submission is the first message on its task's `…in` subject, and the
// gateway publishes nothing else there until it has published it. The
// watcher reads this session's subjects in stream order from a position
// before that submission, so the first message it sees for a task it has not
// run is the submission, unless TASKS evicted it: the per-subject cap
// (max_msgs_per_subject, discard old) drops a subject's oldest message, and
// the oldest surviving steer then comes first. A steer is kind:message on the
// same subject with the same task id, and nothing in the envelope tells it
// from a submission (fetchOriginAtSeq says the same of the first task), so
// the check is on how many messages wait behind it. An eviction leaves the
// subject full, every message on it behind the one delivered, so NumPending
// is at least the cap less one; fewer than maxPendingBehindSubmission pending
// means nothing was evicted on any cap above it. NumPending counts every
// subject the watcher reads, not only this task's, which can only make it
// refuse more. The refusal ends the pod: the gateway's sweep closes the task
// and the next turn starts a fresh pod with the conversation so far.
func (w *taskWatcher) take(msg jetstream.Msg) (*lib.Envelope, uint64, bool, error) {
	md, err := msg.Metadata()
	if err != nil {
		return nil, 0, false, w.unreadable(msg.Subject(), "a message with no metadata", err)
	}
	seq := md.Sequence.Stream
	if seq >= w.next {
		w.next = seq + 1
	}
	env, err := lib.ParseEnvelope(msg.Data())
	if err != nil {
		return nil, 0, false, w.unreadable(msg.Subject(), "an unparseable envelope", err)
	}
	if env.Kind != lib.KindMessage || w.handled[env.TaskID] {
		return nil, 0, false, nil
	}
	// The task id has to be the one its subject names, or the task would
	// run under one id and read its steers and cancel from another. Only
	// the gateway can publish here, so this is a protocol error, surfaced
	// and skipped, like a to/addressee mismatch.
	if msg.Subject() != lib.TaskInSubject(w.addr, env.TaskID) {
		w.log.Error("task watcher skipping a submission whose task id disagrees with its subject",
			"subject", msg.Subject(), "taskId", env.TaskID)
		return nil, 0, false, nil
	}
	if env.To != nil && env.To.Session != w.addr {
		w.log.Error("task watcher skipping a to/addressee mismatch", "subject", msg.Subject(), "to", env.To.Session)
		return nil, 0, false, nil
	}
	w.handled[env.TaskID] = true
	if md.NumPending >= maxPendingBehindSubmission {
		return nil, 0, false, fmt.Errorf("task %s at %s sequence %d has %d message(s) behind it, so TASKS may have evicted its submission and this may be a steer (max_msgs_per_subject with discard=old); refusing to run it, and ending this pod so the next turn starts fresh: %w",
			env.TaskID, msg.Subject(), seq, md.NumPending, errSubmissionUnproven)
	}
	return env, seq, true, nil
}

// unreadable decides a message take cannot read. On a task this pod has run
// it is a late steer or cancel, skipped. Otherwise it may be a new task's
// submission, and the next message on its subject could be a steer that
// nothing would tell from one, so the task id (from the subject) is refused,
// as on NumPending: the pod ends and the next turn starts fresh.
func (w *taskWatcher) unreadable(subject, what string, cause error) error {
	_, taskID, _, ok := lib.ParseTaskSubject(subject)
	if ok && w.handled[taskID] {
		w.log.Error("task watcher skipping "+what+" for a task it has run", "subject", subject, "err", cause)
		return nil
	}
	if ok {
		w.handled[taskID] = true
	}
	return fmt.Errorf("the first message for task %q at %s is %s (%v), so what follows it may be a steer; refusing it, and ending this pod so the next turn starts fresh: %w",
		taskID, subject, what, cause, errSubmissionUnproven)
}

// delegateOffForTask reports whether a task runs without the delegate tool,
// from its own submission rather than from how the pod was started: a reused
// pod runs human turns and wakes alike, and the tool has to follow the turn.
//
// The gateway sets `via` on the two tasks it mints for a delegation
// (gateway/delegation.go): the delegated child, which runs on its own
// `delegate:` pod, and the wake, which reports back to the session that
// delegated. In a reused session pod the one that arrives is the wake. It
// exists to report what a delegation came back with; given the tool it can
// read an interim answer as a reason to ask again, and the chain loops to the
// depth bound with nothing answered (#2819). The child runs without the tool
// by the same rule. Only the gateway can publish a submission, so the signal
// is the gateway's.
//
// No authority block at all is an older gateway's or a hand-run task's, and
// is no wake. One that does not parse is treated as a wake: the tool is the
// thing to withhold when the submission cannot be read.
func delegateOffForTask(origin *lib.Envelope) bool {
	if len(origin.Authority) == 0 {
		return false
	}
	var block struct {
		Via json.RawMessage `json:"via"`
	}
	if err := json.Unmarshal(origin.Authority, &block); err != nil {
		return true
	}
	return len(block.Via) > 0 && string(block.Via) != "null"
}
