package mcp

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// taskReadTimeout bounds one plain read of a task.
	taskReadTimeout = 10 * time.Second
	// taskConsumerIdle is how long the server keeps a read's consumer once
	// it goes idle; the gap between one pull and the next has to stay well
	// inside it.
	taskConsumerIdle = 5 * time.Second
	taskFetchBatch   = 256
	taskFetchWait    = 2 * time.Second
	// maxTaskEvents bounds what one fold holds. Past it the task is
	// reported as too large to read here rather than read without limit.
	maxTaskEvents = 4096

	// maxTaskReaders bounds the consumers this server holds at once, across
	// every caller and every reader (tasks, topics). A slot is held for a
	// read or watch, or for a create attempt that timed out
	// (createReadConsumer), and then for taskConsumerIdle after it, which is how
	// long the server keeps the consumer before reaping it: the console
	// grant cannot delete one, so a slot freed at the end of the read would
	// let rapid reads pile consumers up against the stream's cap, which the
	// gateway, the bridges, every session pod and the console page share.
	maxTaskReaders = 8

	// gatewaySupervisor is the gateway's session on the bus, the writer the
	// supervisor subject is checked against, as the gateway's own fold does.
	gatewaySupervisor = "gateway"

	// taskRefSep joins an addressee and a task id in the reference the tools
	// show and take.
	taskRefSep = "/"
)

// ErrTaskNotFound is a task with no events on TASKS: never submitted, or aged
// out of the stream's retention.
var ErrTaskNotFound = errors.New("no such task on the bus")

// errReadersBusy is every task reader slot in use, by reads or by create
// attempts that timed out.
var errReadersBusy = fmt.Errorf("this server already holds its %d bus readers; try again in a few seconds", maxTaskReaders)

// TaskRef names a task as the bus does: by the addressee it was submitted to
// and its id. The addressee is part of the name on purpose: events for the
// same id under another addressee are another principal's writes, not this
// task's.
type TaskRef struct {
	Addressee string
	TaskID    string
}

// TaskKind says whether a task is a profile task, whose supervisor is the
// dispatcher, or a session or platform task, whose supervisor is the gateway.
// It decides how a read takes a five-token supervisor terminal once the
// install has a cutoff (gke-labs/kube-agents#5301). It is how this server
// learned the task, never the addressee's name, which a session and a profile
// can share, and it is kept beside the reference (taskKinds), not in it, so a
// reference names the same task however it was learned.
type TaskKind uint8

const (
	// TaskKindUnknown is a task this server learned nothing of: a reference
	// a caller typed, one read off the stream, or a turn's own submission,
	// which may be a steer into a running profile task. An addressee with a
	// live agent card on DIRECTORY is read as a profile (refIsProfile).
	TaskKindUnknown TaskKind = iota
	// TaskKindProfile is a task the gateway's status line named, which it
	// does only for a profile task (noticedTask).
	TaskKindProfile
	// TaskKindSession is a task delegated within a turn's chain whose
	// submission this server saw on .in (follow): a new task there is a
	// session's or the platform agent's, since a profile takes its new tasks
	// on a2a.submit.
	TaskKindSession
)

// maxTaskKinds bounds the kinds a server remembers. Past it the oldest is
// forgotten, and its task is read as TaskKindUnknown: the directory decides.
const maxTaskKinds = 4096

// taskKinds is what this server learned of its turns' tasks' kinds, by
// reference. The zero value is ready; a nil one remembers nothing. Safe for
// concurrent use.
type taskKinds struct {
	mu    sync.Mutex
	kinds map[TaskRef]TaskKind
	order []TaskRef
}

// note records ref's kind. An unknown kind records nothing, and a known one
// is never overwritten by another: the first way a task was learned stands.
func (k *taskKinds) note(ref TaskRef, kind TaskKind) {
	if k == nil || kind == TaskKindUnknown {
		return
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.kinds == nil {
		k.kinds = map[TaskRef]TaskKind{}
	}
	if _, ok := k.kinds[ref]; ok {
		return
	}
	for len(k.order) >= maxTaskKinds {
		delete(k.kinds, k.order[0])
		k.order = k.order[1:]
	}
	k.kinds[ref] = kind
	k.order = append(k.order, ref)
}

// of is ref's kind, TaskKindUnknown when nothing was learned of it.
func (k *taskKinds) of(ref TaskRef) TaskKind {
	if k == nil {
		return TaskKindUnknown
	}
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.kinds[ref]
}

func (r TaskRef) String() string { return r.Addressee + taskRefSep + r.TaskID }

// ParseTaskRef reads "<addressee>/<task-id>", or a bare task id with the
// addressee given separately.
func ParseTaskRef(ref, addressee string) (TaskRef, error) {
	if a, id, ok := strings.Cut(ref, taskRefSep); ok {
		ref, addressee = id, a
	}
	r := TaskRef{Addressee: addressee, TaskID: ref}
	if !lib.ValidSubjectToken(r.TaskID) {
		return TaskRef{}, fmt.Errorf("%q is not a task id", r.TaskID)
	}
	if !lib.ValidSubjectToken(r.Addressee) {
		return TaskRef{}, fmt.Errorf("task %q needs its addressee, as <addressee>%s<task-id>", r.TaskID, taskRefSep)
	}
	return r, nil
}

// readerSlot takes one of the server's reader slots, or reports them all busy
// without waiting. Release hands the slot back: once the consumer the read (or
// one create attempt of it) created has had time to be reaped
// (taskConsumerIdle) when created says it may have left one, and at once when
// it cannot have.
func (b *NATSBus) readerSlot() (release func(created bool), err error) {
	select {
	case b.readers <- struct{}{}:
		var once sync.Once
		return func(created bool) {
			once.Do(func() {
				if !created {
					<-b.readers
					return
				}
				time.AfterFunc(b.reapDelay, func() { <-b.readers })
			})
		}, nil
	default:
		return nil, errReadersBusy
	}
}

// createReadConsumer creates one of this server's read consumers on s, which
// the log lines call stream, bounding and retrying each attempt
// (lib.CreateEphemeralConsumer), which splits a short deadline across its
// attempts. Each attempt takes a reader slot: one whose create timed out stays
// taken until the consumer it may have left would be reaped, and one the
// server refused goes back at once. The returned release is the consumer's
// slot.
func (b *NATSBus) createReadConsumer(ctx context.Context, s lib.ConsumerCreator, stream string, cfg jetstream.ConsumerConfig) (jetstream.Consumer, func(created bool), error) {
	return lib.CreateEphemeralConsumer(ctx, s, cfg, lib.EphemeralCreateOptions{
		Slot: b.readerSlot, Logger: b.logger(), Stream: stream, AttemptTimeout: b.createAttemptTimeout,
	})
}

// ReadTask folds a task from TASKS as it stands now.
func (b *NATSBus) ReadTask(ctx context.Context, ref TaskRef) (*lib.Task, error) {
	ctx, cancel := context.WithTimeout(ctx, taskReadTimeout)
	defer cancel()
	w, err := b.watchTask(ctx, ref)
	if err != nil {
		return nil, err
	}
	defer w.close()
	if w.pending == 0 {
		return nil, ErrTaskNotFound
	}
	for w.pending > 0 {
		if err := w.next(ctx); err != nil {
			return nil, err
		}
	}
	if len(w.events) == 0 && (w.dropped > 0 || len(w.doubted) > 0) {
		// Nothing on the task but what the cutoff refused, or holds until
		// the directory answers: no events, as lib's replay answers it.
		return nil, ErrTaskNotFound
	}
	return w.fold()
}

// WaitForTask folds a task until it is final or wait has passed, on one
// consumer for the whole wait: the read follows the task's events as they
// land rather than opening a consumer per look. A task with no events is not
// found at once, as ReadTask says: waiting on a reference a caller named
// would hold a reader for the whole wait to say the same.
func (b *NATSBus) WaitForTask(ctx context.Context, ref TaskRef, wait time.Duration) (*lib.Task, error) {
	return b.waitForTask(ctx, ref, wait, false)
}

// waitForSubmittedTask is WaitForTask for a task whose submission was seen:
// its first event can trail the submission by a cold start, so having none
// yet is not "not found" until the wait has passed.
func (b *NATSBus) waitForSubmittedTask(ctx context.Context, ref TaskRef, wait time.Duration) (*lib.Task, error) {
	return b.waitForTask(ctx, ref, wait, true)
}

func (b *NATSBus) waitForTask(ctx context.Context, ref TaskRef, wait time.Duration, submitted bool) (*lib.Task, error) {
	ctx, cancel := context.WithTimeout(ctx, wait)
	defer cancel()
	w, err := b.watchTask(ctx, ref)
	if err != nil {
		return nil, err
	}
	defer w.close()
	if w.pending == 0 && !submitted {
		return nil, ErrTaskNotFound
	}
	for {
		if t, final := w.ended(); final {
			return t, nil
		}
		if err := w.next(ctx); err != nil {
			if ctx.Err() != nil {
				if len(w.events) == 0 {
					return nil, ErrTaskNotFound
				}
				return w.fold()
			}
			return nil, err
		}
		if !submitted && w.pending == 0 && len(w.events) == 0 && (w.dropped > 0 || len(w.doubted) > 0) {
			// Everything the task had was refused by the cutoff or is held
			// undecided: not found at once, as ReadTask answers it, rather
			// than holding a reader for the rest of the wait.
			return nil, ErrTaskNotFound
		}
	}
}

// taskWatch is one plain pull consumer on one task's subjects: no acks, in
// memory, reaped by the server once idle. Not an ordered consumer, whose reset
// deletes its consumer, a delete the console grant refuses.
type taskWatch struct {
	ref     TaskRef
	r       *budgetReader
	events  []*lib.Envelope
	seen    int
	pending uint64
	release func(created bool)
	// finalUnfolded is a final status-update taken since ended last folded.
	finalUnfolded bool
	// screen is the cutoff screen, nil on an unlatched install; dropped
	// counts what it refused, and doubted holds what it could not decide yet.
	screen  *cutoffScreen
	dropped int
	doubted []doubtedEvent
}

func (b *NATSBus) watchTask(ctx context.Context, ref TaskRef) (*taskWatch, error) {
	s, err := b.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		return nil, err
	}
	cons, release, err := b.createReadConsumer(ctx, s, lib.TasksStream, jetstream.ConsumerConfig{
		FilterSubjects:    lib.TaskReplaySubjects(ref.Addressee, ref.TaskID),
		DeliverPolicy:     jetstream.DeliverAllPolicy,
		AckPolicy:         jetstream.AckNonePolicy,
		MemoryStorage:     true,
		InactiveThreshold: taskConsumerIdle,
	})
	if err != nil {
		return nil, err
	}
	pending := cons.CachedInfo().NumPending
	// The watch keeps every event it folds, so its byte budget runs over
	// its whole life, as maxTaskEvents does.
	r, err := b.newBudgetReader(cons, taskFetchBatch, readMaxBytes)
	if err != nil {
		release(true)
		return nil, err
	}
	// The screen settles the task's kind now, on this call's ctx, which the
	// watch may outlive.
	return &taskWatch{ref: ref, r: r, pending: pending, release: release, screen: b.newCutoffScreen(ctx, ref)}, nil
}

// cutoffScreen is the cutoff filter for one task's read
// (lib.AcceptsSupervisorEvent), nil on an unlatched install, where every
// message passes. With a cutoff, only a five-token supervisor message stored
// at or after it is in question, and whether it is taken turns on the task's
// kind (refIsProfile). The kind is settled when the watch is made, on the
// ctx of the call that makes it, and never later on that ctx: a step poll
// keeps its task watches across tool calls, long after the call that made
// one has ended. A settle that failed is not kept; the watch's next read
// tries it again on its own ctx (taskWatch.resolveDoubts). Not safe for
// concurrent use, as the watch it belongs to isn't.
type cutoffScreen struct {
	b       *NATSBus
	cutoff  time.Time
	ref     TaskRef
	kind    TaskKind
	settled bool
	profile bool
}

func (b *NATSBus) newCutoffScreen(ctx context.Context, ref TaskRef) *cutoffScreen {
	cutoff := b.client.ProfileSupervisorCutoff()
	if cutoff.IsZero() {
		return nil
	}
	sc := &cutoffScreen{b: b, cutoff: cutoff, ref: ref, kind: b.kinds.of(ref)}
	sc.settle(ctx)
	return sc
}

// inQuestion reports whether the message is one the task's kind decides.
func (sc *cutoffScreen) inQuestion(subject string, storedAt time.Time) bool {
	return sc != nil && !lib.AcceptsSupervisorEvent(sc.cutoff, subject, storedAt, true)
}

// settle decides the task's kind if it is not decided yet, and reports
// whether it now is. A directory read that failed decides nothing.
func (sc *cutoffScreen) settle(ctx context.Context) bool {
	if sc.settled {
		return true
	}
	profile, err := sc.b.refIsProfile(ctx, sc.ref, sc.kind)
	if err != nil {
		sc.b.logger().Warn("agent directory read failed; holding the task's five-token supervisor messages after the cutoff unread until a read succeeds",
			"task", sc.ref.String(), "err", err)
		return false
	}
	sc.settled, sc.profile = true, profile
	return true
}

// refIsProfile settles whether ref is a profile task: by how this server
// learned it (kind, from taskKinds), and for a task it learned nothing of, by
// the directory: an addressee with a live agent card is a profile. That rule
// reads an old session task whose name a profile has since taken as a profile
// task, so a gateway terminal on it stored after the cutoff is not read.
//
// A directory that can't be read decides nothing, and the error says so; the
// screen then holds the messages in question unread (fail closed, the same
// way as a profile), and asks again on the next read. Fail closed here, where
// the gateway fails open (gateway's taskIsProfile): the gateway's record
// names each profile task it started by its tier, so its fallback meets
// only a task the record doesn't hold, while here an unknown kind is the common case
// (every typed reference, every turn's own submission), and a task wrongly
// shown unfinished is seen and corrected by a later read, while one wrongly
// shown final on a terminal its supervisor never wrote is the read the
// cutoff exists to refuse.
func (b *NATSBus) refIsProfile(ctx context.Context, ref TaskRef, kind TaskKind) (bool, error) {
	switch kind {
	case TaskKindProfile:
		return true, nil
	case TaskKindSession:
		return false, nil
	}
	cards, err := b.agentCards(ctx)
	if err != nil {
		return true, err
	}
	_, ok := cards[ref.Addressee]
	return ok, nil
}

// directoryProfiles reads DIRECTORY once, on the first call of what it
// returns, and answers whether an addressee has a live agent card there. A
// read that failed counts every addressee it did not return as a profile:
// one swarm read, on its own live ctx, fails closed for the reason
// refIsProfile gives, and the next swarm read asks again. Not safe for
// concurrent use.
func (b *NATSBus) directoryProfiles(ctx context.Context) func(addressee string) bool {
	var (
		read  bool
		cards map[string]AgentCard
		err   error
	)
	return func(addressee string) bool {
		if !read {
			read = true
			if cards, err = b.agentCards(ctx); err != nil {
				b.logger().Warn("agent directory read failed; reading tasks as profile tasks for the supervisor cutoff", "err", err)
			}
		}
		_, ok := cards[addressee]
		return ok || err != nil
	}
}

// doubtedEvent is a message the cutoff screen could not yet decide, held
// with its place among the watch's events.
type doubtedEvent struct {
	at       int
	subject  string
	storedAt time.Time
	env      *lib.Envelope
}

// admit adds one event read off the stream to the watch, as the cutoff
// screen decides: taken, dropped (counted), or held until the task's kind is
// settled.
func (w *taskWatch) admit(ctx context.Context, subject string, storedAt time.Time, env *lib.Envelope) {
	if !w.screen.inQuestion(subject, storedAt) {
		w.events = append(w.events, env)
		w.noteTaken(env)
		return
	}
	if !w.screen.settle(ctx) {
		w.doubted = append(w.doubted, doubtedEvent{at: len(w.events), subject: subject, storedAt: storedAt, env: env})
		return
	}
	if w.screen.b.client.AcceptSupervisorEvent(subject, storedAt, w.screen.profile) {
		w.events = append(w.events, env)
		w.noteTaken(env)
	} else {
		w.dropped++
	}
}

// noteTaken records, for ended, that an event the watch took is a final
// status-update. A held event is noted when resolveDoubts takes it, not when
// it is read: until then it is not among the events a fold sees.
func (w *taskWatch) noteTaken(env *lib.Envelope) {
	w.finalUnfolded = w.finalUnfolded || lib.IsFinalStatus(env)
}

// resolveDoubts settles the task's kind if events are held for it, and puts
// each held event back where it was read, or drops it.
func (w *taskWatch) resolveDoubts(ctx context.Context) {
	if len(w.doubted) == 0 || !w.screen.settle(ctx) {
		return
	}
	events := make([]*lib.Envelope, 0, len(w.events)+len(w.doubted))
	prev := 0
	for _, d := range w.doubted {
		events = append(events, w.events[prev:d.at]...)
		prev = d.at
		if w.screen.b.client.AcceptSupervisorEvent(d.subject, d.storedAt, w.screen.profile) {
			events = append(events, d.env)
			w.noteTaken(d.env)
		} else {
			w.dropped++
		}
	}
	w.events = append(events, w.events[prev:]...)
	w.doubted = nil
}

// errTooManyEvents is a task with more than maxTaskEvents events.
var errTooManyEvents = errors.New("too many to read here")

// tooLargeToRead is a read that will fail the same way every time: the task
// has more events, or more bytes of them, than a read here takes.
func tooLargeToRead(err error) bool {
	return errors.Is(err, ErrReadByteBudget) || errors.Is(err, errTooManyEvents)
}

func (w *taskWatch) close() {
	w.r.stop()
	w.release(true)
}

// next reads what is pending, up to taskFetchBatch events at a time, or
// waits up to taskFetchWait for the next event when nothing is.
func (w *taskWatch) next(ctx context.Context) error {
	return w.nextWait(ctx, taskFetchWait)
}

// nextWait is next with a caller-chosen maximum wait when nothing is pending.
func (w *taskWatch) nextWait(ctx context.Context, maxWait time.Duration) error {
	wait := fetchWait(ctx, maxWait)
	// Never below minFetchWait, even with less than that left: what is
	// pending still arrives at once, and an empty fetch ends just past the
	// deadline, which the caller then sees as ctx done and keeps what it has
	// folded. Refusing the fetch instead would end the read on an error
	// with ctx not yet done, and drop the fold.
	wait = max(wait, minFetchWait)
	// Events held for the task's kind are settled after this read, on its
	// ctx.
	defer w.resolveDoubts(ctx)
	got := 0
	for got < taskFetchBatch {
		msg, err := w.r.next(wait)
		if errors.Is(err, nats.ErrTimeout) {
			break
		}
		if err != nil {
			return fmt.Errorf("task %s: %w", w.ref, err)
		}
		got++
		w.seen++
		if w.seen > maxTaskEvents {
			return fmt.Errorf("task %s has more than %d events: %w", w.ref, maxTaskEvents, errTooManyEvents)
		}
		md, mdErr := msg.Metadata()
		var storedAt time.Time
		if mdErr == nil {
			storedAt = md.Timestamp
		}
		if env := agreedEnvelope(msg.Subject(), msg.Data()); env != nil {
			w.admit(ctx, msg.Subject(), storedAt, env)
		}
		if mdErr == nil {
			w.pending = md.NumPending
		}
		if w.pending == 0 {
			break
		}
	}
	if got == 0 {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		// Events were pending and the fetch brought none: the read stalled,
		// which is not the end of the task. Saying so beats folding what was
		// read as the task's state.
		if w.pending > 0 {
			return fmt.Errorf("task %s: a fetch came back empty with %d events still pending", w.ref, w.pending)
		}
	}
	return nil
}

func (w *taskWatch) fold() (*lib.Task, error) { return lib.FoldTask(w.ref.TaskID, w.events) }

// ended reports a final task, one time for each final status-update taken: it
// folds only when one has been taken since the last fold, because nothing
// else ends a task (FoldTask sets Final from a status-update's final field
// alone). A fold parses every payload read so far, so folding after each
// artifact or progress status of a task near readMaxBytes costs the whole
// task again between one pull and the next. On a loaded test runner that took
// the gap between pulls past taskConsumerIdle, the server reaped the consumer
// under the read, and the next pull failed with "no responders".
func (w *taskWatch) ended() (*lib.Task, bool) {
	if !w.finalUnfolded {
		return nil, false
	}
	w.finalUnfolded = false
	t, err := w.fold()
	if err != nil || !t.Final {
		return nil, false
	}
	return t, true
}

// taskAgreement is the supervisor check agreedEnvelope applies, the same
// writers gateway.SupervisorAgreement accepts.
var taskAgreement = lib.AgreementPolicy{Supervisor: gatewaySupervisor, OtherSupervisors: []string{lib.DispatcherSession}}

// agreedEnvelope parses one TASKS message, dropping what the gateway's own
// fold drops: an unparseable write, and an envelope that disagrees with the
// subject it arrived on. The supervisor subject is checked by name against
// the gateway and the profile dispatcher, which writes the terminal of a
// profile task it fails or refuses.
func agreedEnvelope(subject string, data []byte) *lib.Envelope {
	env, err := lib.ParseEnvelope(data)
	if err != nil {
		return nil
	}
	if aerr := lib.CheckSubjectAgreement(subject, env, taskAgreement); aerr != nil && !lib.IsAdvisoryDisagreement(aerr) {
		return nil
	}
	return env
}
