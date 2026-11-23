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
	// it goes idle; every read or watch here ends well inside it.
	taskConsumerIdle = 5 * time.Second
	taskFetchBatch   = 256
	taskFetchWait    = 2 * time.Second
	// maxTaskEvents bounds what one fold holds. Past it the task is
	// reported as too large to read here rather than read without limit.
	maxTaskEvents = 4096

	// maxTaskReaders bounds the consumers this server holds at once, across
	// every caller and every reader (tasks, topics). A slot is held for a
	// read or watch and then for taskConsumerIdle after it, which is how
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

// errReadersBusy is every task reader slot in use.
var errReadersBusy = fmt.Errorf("this server already holds its %d bus readers; try again in a few seconds", maxTaskReaders)

// TaskRef names a task as the bus does: by the addressee it was submitted to
// and its id. The addressee is part of the name on purpose: events for the
// same id under another addressee are another principal's writes, not this
// task's.
type TaskRef struct {
	Addressee string
	TaskID    string
	// Kind is what this server learned of the task's supervisor, which
	// decides how a read takes a five-token supervisor terminal once the
	// install has a cutoff (gke-labs/kube-agents#5301). It is how the task
	// was learned, never the addressee's name, which a session and a
	// profile can share.
	Kind TaskKind
}

// TaskKind says whether a task is a profile task, whose supervisor is the
// dispatcher, or a session or platform task, whose supervisor is the gateway.
type TaskKind uint8

const (
	// TaskKindUnknown is a reference a caller typed, or one read off the
	// stream: an addressee with a live agent card on DIRECTORY is read as a
	// profile (refIsProfile).
	TaskKindUnknown TaskKind = iota
	// TaskKindProfile is a task the gateway's status line named, which it
	// does only for a profile task (noticedTask).
	TaskKindProfile
	// TaskKindSession is a task whose submission this server saw on .in,
	// where only a session or the platform agent takes a new task
	// (submission).
	TaskKindSession
)

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
// without waiting. Release hands the slot back: once the consumer the read
// created has had time to be reaped (taskConsumerIdle) when created says the
// read may have left one, and at once when it cannot have.
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

// consumerMayExist reports whether a failed CreateConsumer can still have left
// a consumer on the server. A JetStream API error is the server's refusal, so
// nothing was made; anything else (a timeout, a dropped connection) may have
// been answered after the client stopped waiting.
func consumerMayExist(err error) bool {
	var refused *jetstream.APIError
	return !errors.As(err, &refused)
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
	if len(w.events) == 0 && w.dropped > 0 {
		// Nothing on the task but what the cutoff refused: no events, as
		// lib's replay answers it.
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
		t, ferr := w.fold()
		if ferr == nil && t.Final {
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
	// accept is the cutoff screen (supervisorScreen); dropped counts what it
	// refused.
	accept  func(subject string, storedAt time.Time) bool
	dropped int
}

func (b *NATSBus) watchTask(ctx context.Context, ref TaskRef) (*taskWatch, error) {
	release, err := b.readerSlot()
	if err != nil {
		return nil, err
	}
	s, err := b.client.JetStream().Stream(ctx, lib.TasksStream)
	if err != nil {
		release(false)
		return nil, err
	}
	cons, err := s.CreateConsumer(ctx, jetstream.ConsumerConfig{
		FilterSubjects:    lib.TaskReplaySubjects(ref.Addressee, ref.TaskID),
		DeliverPolicy:     jetstream.DeliverAllPolicy,
		AckPolicy:         jetstream.AckNonePolicy,
		MemoryStorage:     true,
		InactiveThreshold: taskConsumerIdle,
	})
	if err != nil {
		release(consumerMayExist(err))
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
	return &taskWatch{ref: ref, r: r, pending: pending, release: release, accept: b.supervisorScreen(ctx, ref)}, nil
}

// supervisorScreen is the cutoff filter for one task's read
// (lib.AcceptsSupervisorEvent): on an unlatched install, every message passes.
// With a cutoff, only a five-token supervisor message stored after it is in
// question, and only then is the task's kind settled (refIsProfile), so an
// ordinary read costs no DIRECTORY read. Safe for concurrent use.
func (b *NATSBus) supervisorScreen(ctx context.Context, ref TaskRef) func(subject string, storedAt time.Time) bool {
	cutoff := b.client.ProfileSupervisorCutoff()
	if cutoff.IsZero() {
		return func(string, time.Time) bool { return true }
	}
	var (
		once    sync.Once
		profile bool
	)
	return func(subject string, storedAt time.Time) bool {
		if lib.AcceptsSupervisorEvent(cutoff, subject, storedAt, true) {
			return true
		}
		once.Do(func() { profile = b.refIsProfile(ctx, ref) })
		return b.client.AcceptSupervisorEvent(subject, storedAt, profile)
	}
}

// refIsProfile settles whether ref is a profile task: by how it was learned
// (TaskRef.Kind), and for a reference that says nothing, by the directory: an
// addressee with a live agent card is a profile. That rule reads an old
// session task whose name a profile has since taken as a profile task, so a
// gateway terminal on it stored after the cutoff is not read. A directory
// that can't be read leaves the addressee a profile too: a task wrongly shown
// unfinished is seen and corrected by a later read, while one wrongly shown
// final on a terminal its supervisor never wrote is the read the cutoff
// exists to refuse.
func (b *NATSBus) refIsProfile(ctx context.Context, ref TaskRef) bool {
	switch ref.Kind {
	case TaskKindProfile:
		return true
	case TaskKindSession:
		return false
	}
	return b.directoryProfiles(ctx)(ref.Addressee)
}

// directoryProfiles reads DIRECTORY once, on the first call of what it
// returns, and answers whether an addressee has a live agent card there. A
// read that failed counts every addressee it did not return as a profile
// (refIsProfile). Not safe for concurrent use.
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
			if w.accept == nil || w.accept(msg.Subject(), storedAt) {
				w.events = append(w.events, env)
			} else {
				w.dropped++
			}
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
