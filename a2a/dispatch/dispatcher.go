package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/authcallout"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/taskcred"
)

const (
	// The dispatcher's wildcard subscriptions on every worker's answers:
	// a2a.dispatch.<profile>.<tier>.<pod>.accept|lease.
	acceptWildcard = "a2a.dispatch.*.*.*.accept"
	leaseWildcard  = "a2a.dispatch.*.*.*.lease"
	// answerTokens is the token count of an accept or lease subject.
	answerTokens = 6

	// ticksPerRenewal is how often a hold checks its lease and deadline,
	// per renewal interval.
	ticksPerRenewal = 4
	// minTick floors that check interval.
	minTick = 10 * time.Millisecond
	// kvOpTimeout bounds one lease-bucket operation.
	kvOpTimeout = 5 * time.Second
	// renewalBuffer is how many renewals a hold may have unread.
	renewalBuffer = 16
	// settleRetry paces retries of a terminal publish or an ack that
	// failed: the task's outcome is decided, only its delivery is owed.
	settleRetry = 500 * time.Millisecond
	// shutdownWait bounds Run's wait for handlers to return on shutdown.
	shutdownWait = 10 * time.Second
	// The abandon reasons.
	abandonDeadline     = "deadline exceeded"
	abandonWithdrawn    = "offer withdrawn: the accept came after the dispatcher stopped waiting"
	abandonTimedOut     = "offer withdrawn: no accept within the accept window"
	abandonLeaseExpired = "lease expired: the dispatcher heard no renewal in time"
	abandonStopping     = "offer withdrawn: the dispatcher is stopping"
	abandonUnrecorded   = "offer withdrawn: the dispatcher could not record the lease"

	// Answers a worker sends for a task this dispatcher doesn't hold for it
	// (an accept no offer waits on, a renewal for a task not confirmed to
	// that pod) cost a KV read, an abandon or a log line each, and the pod
	// picks the task id. A pod runs one task at a time, so a legitimate one
	// sends a few such answers at most (a late accept, the renewals between
	// a restart and its submission's redelivery); each pod gets
	// unknownAnswersPerWindow per unknownAnswerWindow and the rest are
	// dropped and counted. Its warnings are logged at most once per
	// unknownAnswerLogEvery.
	unknownAnswerWindow     = time.Second
	unknownAnswersPerWindow = 4
	unknownAnswerLogEvery   = 10 * time.Second
	// orphanLookups caps the concurrent lease-bucket lookups for renewals
	// of tasks not held here, across all pods.
	orphanLookups = 8
	// verifiedFor is how long an admission's verifier permit is reused for
	// the same submission's retries over a cap: long enough to spare the
	// verifier a call per retry delay, short enough that a revoked grant
	// stops admitting soon.
	verifiedFor = time.Minute
	// tombstoneMargin is how long past the submit stream's retention a
	// tombstone is kept: a replay can come no later than the retention, and
	// the margin covers a clock or a stream's age check running late.
	tombstoneMargin = time.Hour
)

// The kinds a profile's submissions come in; one durable per tier each.
var submitKinds = []string{lib.SubmitKindTop, lib.SubmitKindChild}

// holdKey names one held submission. A task id is unique only within its
// (profile, tier, kind) durable, so in-flight state is keyed by all four.
type holdKey struct{ profile, tier, kind, taskID string }

// holding is one task this dispatcher holds: offered to a pod, running on
// it, or re-adopted after a restart. It is counted against the caps from
// the moment it is reserved until it is settled.
type holding struct {
	profile, tier, kind, taskID string
	unattended                  bool
	// lease and renew are what the task was offered (or, re-adopted, what
	// its record shows): a config change never moves them.
	lease, renew time.Duration
	// cluster is the cluster admission checked the task against, nil when
	// it names none: the target of its credential-proxy token (#3889).
	cluster *taskcred.Target
	// changeRequested is the submission's ChangeRequested: whether its
	// credential-proxy token carries the vcs:proposal-create permit (#3993).
	changeRequested bool

	// Guarded by Dispatcher.mu. pod is set from the offer on, so a pod
	// with an offer out is busy before anything is written; confirmed is
	// set once the lease record is written and the confirm sent, and only
	// then are the pod's renewals this task's. settled says the task's
	// outcome was acked, for a second delivery waiting on done.
	pod       string
	confirmed bool
	settled   bool

	renewals chan renewal
	done     chan struct{}
	// finalQueued keeps a flood of final renewals to one waiting goroutine.
	finalQueued atomic.Bool
}

func (h *holding) key() holdKey { return holdKey{h.profile, h.tier, h.kind, h.taskID} }

// answerBudget is one pod's allowance of answers for tasks it doesn't hold.
type answerBudget struct {
	window  time.Time
	n       int
	lastLog time.Time
	dropped int
}

// renewal is a renewal from the pod the subject names.
type renewal struct {
	pod   string
	final bool
}

type offerKey struct{ profile, tier, pod, taskID string }

// bindBackoff is when a durable's bind is next tried, and the delay after.
type bindBackoff struct {
	next  time.Time
	delay time.Duration
}

// waiting is a submission over a cap, remembered for the health line.
type waiting struct {
	durable  string
	queued   time.Time
	lastSeen time.Time
	why      string
}

// Dispatcher is the profile dispatcher. New builds it; Run runs it.
type Dispatcher struct {
	cfg      Config
	bus      *lib.Client
	src      Source
	pool     Pool
	verifier Verifier
	log      *slog.Logger
	now      func() time.Time
	// mint signs one task credential with the task-signing key, and
	// taskKeys is that key's public half, which the dispatcher reads back
	// each credential it mints with. mint is nil with no key: confirms then
	// carry no credentials. taskMaxLifetime caps their expiry.
	mint            func(taskcred.Claims) (string, error)
	taskKeys        []string
	taskMaxLifetime time.Duration

	mu        sync.Mutex
	started   time.Time
	active    map[holdKey]*holding
	offers    map[offerKey]chan lib.DispatchAccept
	waiting   map[string]waiting
	lastAdmit map[string]time.Time
	// released is the lease keys of tasks whose final renewal came while no
	// hold here had them, by when: settled, not failed, when the
	// submission comes back.
	released map[string]time.Time
	// verified is the verifier's last permit for a waiting submission:
	// when, and for which ref and target.
	verified map[holdKey]permit
	// answers is each pod's budget of answers for tasks it doesn't hold,
	// by <profile>.<tier>.<pod>.
	answers map[string]*answerBudget
	// orphanBusy is the lease keys with a lookup in flight (".final"
	// appended for a final renewal's), and orphanSem bounds how many run.
	orphanBusy map[string]bool
	orphanSem  chan struct{}
	subs       map[string]lib.HeldSubscription
	binding    map[string]bool
	draining   []lib.HeldSubscription
	// granted is the durables, and grantedProfiles the profiles (their
	// `.supervisor` publish), the current connection's grants cover: the callout
	// fixes a principal's grants per connection, from the config the
	// operator rendered when it connected. A config listing a durable or a
	// profile outside them needs a reconnect first.
	granted         map[string]bool
	grantedProfiles map[string]bool
	// bindRetry paces the retries of durables that failed to bind.
	bindRetry map[string]bindBackoff
	// regrantNotBefore holds off another round of reconnects after one
	// that left a durable unbindable.
	regrantNotBefore time.Time
	reconnects       atomic.Int64
	// regranting is set while a regrant runs, off the Run loop.
	regranting atomic.Bool
	// bg is Run's background work (a regrant, a sweep), waited for on the
	// way out.
	bg sync.WaitGroup
	// droppedAnswers counts answers dropped over a pod's budget.
	droppedAnswers atomic.Int64
	// running is set while Run holds its answer subscriptions.
	running atomic.Bool
}

// New builds a dispatcher on bus. src is the install config, pool the
// workers it may offer to, verifier the profile.run check (a nil one refuses
// every submission).
func New(cfg Config, bus *lib.Client, src Source, pool Pool, verifier Verifier, log *slog.Logger) (*Dispatcher, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	if bus == nil || src == nil || pool == nil {
		return nil, errors.New("dispatch.New: bus, source and pool are required")
	}
	if log == nil {
		log = slog.Default()
	}
	var mint func(taskcred.Claims) (string, error)
	var taskKeys []string
	if key := cfg.TaskSigningKey; key != nil {
		pub, err := key.PublicKey()
		if err != nil {
			return nil, fmt.Errorf("dispatch.New: the task-signing key: %w", err)
		}
		mint = func(c taskcred.Claims) (string, error) { return taskcred.Mint(key, c) }
		taskKeys = []string{pub}
	}
	maxLifetime := cfg.TaskCredentialMaxLifetime
	if maxLifetime == 0 {
		maxLifetime = authcallout.DefaultTaskCredentialMaxLifetime
	}
	return &Dispatcher{
		cfg: cfg, bus: bus, src: src, pool: pool, verifier: verifier, log: log, now: time.Now,
		mint: mint, taskKeys: taskKeys, taskMaxLifetime: maxLifetime,
		active:     map[holdKey]*holding{},
		offers:     map[offerKey]chan lib.DispatchAccept{},
		waiting:    map[string]waiting{},
		lastAdmit:  map[string]time.Time{},
		released:   map[string]time.Time{},
		verified:   map[holdKey]permit{},
		answers:    map[string]*answerBudget{},
		orphanBusy: map[string]bool{},
		orphanSem:  make(chan struct{}, orphanLookups),
		subs:       map[string]lib.HeldSubscription{},
		binding:    map[string]bool{},
		granted:    map[string]bool{},

		grantedProfiles: map[string]bool{},
		bindRetry:       map[string]bindBackoff{},
	}, nil
}

// Run binds the durables and the answer subjects, then serves until ctx
// ends. On the way out it stops every hold without settling it: each held
// submission goes back to the stream at once, its lease record kept, for
// the next instance to re-adopt.
func (d *Dispatcher) Run(ctx context.Context) error {
	d.mu.Lock()
	d.started = d.now()
	// The connection was made against the config as it stands now.
	for _, p := range d.src.Config().Profiles {
		d.grantedProfiles[p.Name] = true
		for _, dur := range p.Durables {
			d.granted[dur.Name] = true
		}
	}
	d.mu.Unlock()
	acceptSub, err := d.bus.SubscribeCore(acceptWildcard, d.onAccept)
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", acceptWildcard, err)
	}
	defer acceptSub.Stop()
	leaseSub, err := d.bus.SubscribeCore(leaseWildcard, d.onRenewal)
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", leaseWildcard, err)
	}
	defer leaseSub.Stop()
	d.running.Store(true)
	defer d.running.Store(false)

	d.syncBindings(ctx)
	refresh := time.NewTicker(d.cfg.RefreshInterval)
	defer refresh.Stop()
	health := time.NewTicker(d.cfg.HealthInterval)
	defer health.Stop()
	sweep := time.NewTicker(d.cfg.SweepInterval)
	defer sweep.Stop()
	var sweeping atomic.Bool
	for {
		select {
		case <-ctx.Done():
			d.shutdown()
			return nil
		case <-refresh.C:
			d.syncBindings(ctx)
		case <-health.C:
			d.logHealth()
		case <-sweep.C:
			d.prune()
			// The sweep's bucket reads run off the loop, one at a time.
			if sweeping.CompareAndSwap(false, true) {
				d.bg.Add(1)
				go func() {
					defer d.bg.Done()
					defer sweeping.Store(false)
					d.sweep(ctx)
				}()
			}
		}
	}
}

func (d *Dispatcher) shutdown() {
	d.mu.Lock()
	subs := append([]lib.HeldSubscription(nil), d.draining...)
	for _, s := range d.subs {
		subs = append(subs, s)
	}
	d.mu.Unlock()
	for _, s := range subs {
		s.Stop()
	}
	wctx, cancel := context.WithTimeout(context.Background(), shutdownWait)
	defer cancel()
	for _, s := range subs {
		if err := s.Wait(wctx); err != nil {
			d.log.Warn("dispatcher shutdown: a hold did not return in time", "err", err)
		}
	}
	bgDone := make(chan struct{})
	go func() { d.bg.Wait(); close(bgDone) }()
	select {
	case <-bgDone:
	case <-wctx.Done():
		d.log.Warn("dispatcher shutdown: a regrant or sweep did not return in time")
	}
}

// syncBindings brings the bound durables in line with the config. A
// durable the config adds outside the current connection's grants first
// costs a reconnect (regrant), since grants are fixed per connection; the
// regrant runs off the Run loop, so its backoff never holds up the other
// durables' binds or the health line. Every
// granted durable not bound yet is bound on its own goroutine (a durable
// the operator hasn't created yet retries for the lib's bind window, and
// must not hold up the rest). A bound durable the config no longer lists
// has its intake stopped, with no reconnect: a profile absent from the
// config has no durables to read, and tasks already held from it run to
// their end.
func (d *Dispatcher) syncBindings(ctx context.Context) {
	cfg := d.src.Config()
	want := map[string]string{} // durable name -> profile
	durables := map[string]lib.DispatchDurable{}
	var added, addedProfiles []string
	d.mu.Lock()
	for _, p := range cfg.Profiles {
		if !d.grantedProfiles[p.Name] {
			addedProfiles = append(addedProfiles, p.Name)
		}
		for _, dur := range p.Durables {
			want[dur.Name], durables[dur.Name] = p.Name, dur
			if !d.granted[dur.Name] {
				added = append(added, dur.Name)
			}
		}
	}
	cooling := d.now().Before(d.regrantNotBefore)
	d.mu.Unlock()
	if (len(added) > 0 || len(addedProfiles) > 0) && !cooling && d.regranting.CompareAndSwap(false, true) {
		d.bg.Add(1)
		go func() {
			defer d.bg.Done()
			defer d.regranting.Store(false)
			d.regrant(ctx, want, durables, added, addedProfiles)
		}()
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for name := range d.grantedProfiles {
		if _, ok := profileOf(cfg, name); !ok {
			delete(d.grantedProfiles, name)
		}
	}
	for name := range d.granted {
		if want[name] == "" {
			delete(d.granted, name)
		}
	}
	now := d.now()
	for name, profile := range want {
		if !d.granted[name] || d.subs[name] != nil || d.binding[name] || now.Before(d.bindRetry[name].next) {
			continue
		}
		d.binding[name] = true
		go d.bind(ctx, profile, durables[name])
	}
	for name, sub := range d.subs {
		if want[name] == "" {
			sub.StopIntake()
			d.draining = append(d.draining, sub)
			delete(d.subs, name)
			d.log.Info("unbound a durable the config no longer lists", "durable", name)
		}
	}
}

// regrant reconnects so the bus connection carries grants for the durables
// and profiles the config added, then binds the durables. A bind still refused (the callout's
// watch lagging the config) gets another reconnect, with backoff, up to
// RegrantAttempts; then it is logged as an error and left for the next
// refresh. Held work is untouched throughout (lib.Client.Reconnect).
func (d *Dispatcher) regrant(ctx context.Context, want map[string]string, durables map[string]lib.DispatchDurable, added, addedProfiles []string) {
	backoff := d.cfg.RegrantBackoff
	pending := added
	for attempt := 1; attempt <= d.cfg.RegrantAttempts; attempt++ {
		if attempt > 1 && !sleepCtx(ctx, backoff) {
			return
		}
		if attempt > 1 {
			backoff *= 2
		}
		rctx, cancel := context.WithTimeout(ctx, d.cfg.ReconnectTimeout)
		err := d.bus.Reconnect(rctx)
		cancel()
		d.reconnects.Add(1)
		if err != nil {
			d.log.Warn("reconnect for new durables' grants", "attempt", attempt, "err", err)
			continue
		}
		// A profile's `.supervisor` grant can't be probed without publishing;
		// the reconnect is what carries it.
		d.mu.Lock()
		for _, p := range addedProfiles {
			d.grantedProfiles[p] = true
		}
		d.mu.Unlock()
		var refused []string
		for _, name := range pending {
			err := d.bindNow(ctx, want[name], durables[name])
			switch {
			case err == nil:
			case errors.Is(err, jetstream.ErrConsumerNotFound):
				// The bus answered, so the grant is there: the durable
				// just isn't yet, and the bind retries pick it up.
				d.noteBindFailure(name, err)
			default:
				refused = append(refused, name)
				d.log.Warn("bind refused after a reconnect; the grants may lag the config", "durable", name, "attempt", attempt, "err", err)
			}
		}
		d.mu.Lock()
		for _, name := range pending {
			if !slices.Contains(refused, name) {
				d.granted[name] = true
			}
		}
		d.mu.Unlock()
		if len(refused) == 0 {
			d.log.Info("reconnected for new durables and profiles", "durables", added, "profiles", addedProfiles, "attempts", attempt)
			return
		}
		pending = refused
	}
	d.mu.Lock()
	d.regrantNotBefore = d.now().Add(d.cfg.RegrantCooldown)
	d.mu.Unlock()
	d.log.Error("durables still not bindable after reconnecting; trying again after the cooldown",
		"durables", pending, "attempts", d.cfg.RegrantAttempts, "cooldown", d.cfg.RegrantCooldown)
}

// noteBindFailure backs a durable's next bind off, doubling to the max.
// Callers don't hold d.mu.
func (d *Dispatcher) noteBindFailure(name string, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	b := d.bindRetry[name]
	b.delay = min(max(2*b.delay, d.cfg.BindRetryBase), d.cfg.BindRetryMax)
	b.next = d.now().Add(b.delay)
	d.bindRetry[name] = b
	if errors.Is(err, jetstream.ErrConsumerNotFound) {
		d.log.Info("durable not created yet; retrying", "durable", name, "in", b.delay)
	} else {
		d.log.Warn("durable bind failed; retrying", "durable", name, "in", b.delay, "err", err)
	}
}

// bindNow binds one durable within BindAttemptTimeout.
func (d *Dispatcher) bindNow(ctx context.Context, profile string, dur lib.DispatchDurable) error {
	bctx, cancel := context.WithTimeout(ctx, d.cfg.BindAttemptTimeout)
	defer cancel()
	sub, err := d.subscribe(bctx, profile, dur)
	if err != nil {
		return err
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if old := d.subs[dur.Name]; old != nil {
		old.StopIntake()
		d.draining = append(d.draining, old)
	}
	d.subs[dur.Name] = sub
	delete(d.bindRetry, dur.Name)
	return nil
}

func (d *Dispatcher) subscribe(ctx context.Context, profile string, dur lib.DispatchDurable) (lib.HeldSubscription, error) {
	return d.bus.SubscribeHeld(ctx, lib.SubscribeConfig{
		Stream:    d.cfg.Stream,
		Durable:   dur.Name,
		Subject:   lib.ProfileDurableFilter(profile, dur.Tier, dur.Kind),
		Session:   profile,
		Agreement: &lib.AgreementPolicy{Submitter: d.cfg.Submitter},
	}, d.handle)
}

// Reconnects counts the reconnects config changes have cost.
func (d *Dispatcher) Reconnects() int64 { return d.reconnects.Load() }

func (d *Dispatcher) bind(ctx context.Context, profile string, dur lib.DispatchDurable) {
	name := dur.Name
	bctx, cancel := context.WithTimeout(ctx, d.cfg.BindAttemptTimeout)
	sub, err := d.subscribe(bctx, profile, dur)
	cancel()
	if err != nil {
		d.noteBindFailure(name, err)
		d.mu.Lock()
		delete(d.binding, name)
		d.mu.Unlock()
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.binding, name)
	delete(d.bindRetry, name)
	if ctx.Err() != nil || d.subs[name] != nil {
		// Stopping, or a regrant bound it meanwhile: two bindings would
		// both take deliveries.
		sub.Stop()
		return
	}
	d.subs[name] = sub
	d.log.Info("bound a profile durable", "durable", name)
}

// handle is one held submission, from delivery to settlement.
func (d *Dispatcher) handle(ctx context.Context, del lib.Delivery) {
	profile, tier, kind, taskID, ok := lib.ParseSubmitSubject(del.Subject())
	if !ok {
		d.log.Error("a held delivery off a non-submission subject", "subject", del.Subject())
		_ = del.Term()
		return
	}
	log := d.log.With("profile", profile, "tier", tier, "kind", kind, "task", taskID)
	hk := holdKey{profile, tier, kind, taskID}

	// A second delivery of a task this process still holds (the first
	// holder's heartbeats were lost for longer than AckWait): the first
	// holder decides the outcome, and this delivery follows it.
	if h := d.holder(hk); h != nil {
		d.followHolder(ctx, log, del, h)
		return
	}

	snap := d.src.Config()
	prof, ok := profileOf(snap, profile)
	if !ok {
		// A durable for a profile the config doesn't know yet (or any
		// more): the config lags the operator, so the task stays held.
		log.Warn("no config for this profile; the submission stays queued")
		_ = del.Nak(d.cfg.RetryDelay)
		return
	}

	// Started already? A lease record means the task was confirmed to a
	// worker, so it is never offered again: re-adopted or failed.
	rec, key, found, err := d.findLease(ctx, profile, tier, taskID)
	if err != nil {
		// Unknown is not "never started": offering it could run it twice.
		log.Warn("could not read the task's lease; the submission stays queued", "err", err)
		_ = del.Nak(d.cfg.RetryDelay)
		return
	}
	if found && rec.Finished() {
		// A durable re-created over the stream (DeliverAll) replays
		// submissions long since settled: the task is over, its terminal
		// written by its worker or by this dispatcher. Ack it and do
		// nothing else: no offer, no adoption, no second terminal.
		log.Info("a replay of a finished task; skipped", "key", key, "finishedAt", rec.FinishedAt)
		d.ackSyncRetry(ctx, del)
		return
	}
	if found {
		d.adopt(ctx, log, del, snap, prof, hk, rec, key)
		return
	}

	now := d.now()
	adm, ref, err := d.admit(ctx, del, snap, prof, hk, now)
	if err != nil {
		return // stopping: the lib gives the delivery back
	}
	if ref != nil {
		log.Info("submission refused at admission", "reason", ref.reason, "detail", ref.detail)
		d.clearWaiting(taskID)
		d.reject(ctx, log, del, hk, ref)
		return
	}

	h, why := d.reserve(snap, prof, tier, kind, taskID)
	if h == nil {
		if why == capHeld {
			if h := d.holder(hk); h != nil {
				d.followHolder(ctx, log, del, h)
				return
			}
		}
		d.noteWaiting(taskID, lib.ProfileDurableName(profile, tier, kind), del.Received(), why)
		log.Debug("over a cap; the submission stays queued", "cap", why)
		_ = del.Nak(d.cfg.RetryDelay)
		return
	}
	d.clearWaiting(taskID)
	h.cluster, h.changeRequested = adm.cluster, adm.changeRequested

	rec, key, err = d.handOff(ctx, log, del, snap, prof, h)
	if err != nil || key == "" {
		d.release(h, false)
		if err != nil {
			log.Warn("hand-off failed; the submission stays queued", "err", err)
		} else {
			d.noteWaiting(taskID, lib.ProfileDurableName(profile, tier, kind), del.Received(), ReasonNoWorker)
			log.Info("no worker took the task; the submission stays queued")
		}
		if ctx.Err() == nil {
			_ = del.Nak(d.cfg.RetryDelay)
		}
		return
	}
	d.mu.Lock()
	d.lastAdmit[lib.ProfileDurableName(profile, tier, kind)] = d.now()
	d.mu.Unlock()
	log.Info("task confirmed to a worker", "pod", rec.Pod)
	d.hold(ctx, log, del, h, rec, key, time.Time{})
}

// holder is the holding for a submission, if this process has one.
func (d *Dispatcher) holder(hk holdKey) *holding {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.active[hk]
}

// followHolder settles a second delivery of a task the way its first holder
// does: acked once the first holder has settled the task, given back if the
// first holder let it go unsettled (whichever delivery comes next decides
// then).
func (d *Dispatcher) followHolder(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding) {
	log.Warn("redelivered while still held here; following the first holder")
	select {
	case <-h.done:
	case <-ctx.Done():
		return
	}
	d.mu.Lock()
	settled := h.settled
	d.mu.Unlock()
	if settled {
		d.ackRetry(ctx, del)
		return
	}
	_ = del.Nak(d.cfg.RetryDelay)
}

// reject publishes a refusal's terminal, writes the task's tombstone (under
// ProfileLeaseRefusedPod: no pod ever held it) and terminates the
// submission, so it is never redelivered, nor run or refused again if a
// re-created durable replays it (a grant given since would otherwise run a
// task once refused). A terminal or tombstone that can't be written leaves
// the delivery to come back and be refused again (the terminal's envelope id
// is fixed, so a repeat is a duplicate the stream drops).
func (d *Dispatcher) reject(ctx context.Context, log *slog.Logger, del lib.Delivery, hk holdKey, ref *refusal) {
	for {
		err := d.publishTerminal(ctx, hk.profile, del.Envelope(), lib.StateRejected, ref.reason, ref.detail)
		if err == nil {
			break
		}
		log.Warn("publish the rejection terminal; retrying", "err", err)
		if !sleepCtx(ctx, settleRetry) {
			return
		}
	}
	key := lib.ProfileLeaseKey(hk.profile, hk.tier, lib.ProfileLeaseRefusedPod, hk.taskID)
	if !d.entomb(ctx, log, key, lib.LeaseRecord{TaskID: hk.taskID, Pod: lib.ProfileLeaseRefusedPod, Kind: hk.kind}) {
		return
	}
	for del.Term() != nil {
		if !sleepCtx(ctx, settleRetry) {
			return
		}
	}
}

// capHeld is reserve's answer for a submission this process already holds.
const capHeld = "already held here"

// reserve takes a slot for the task if every cap allows it, or names the
// cap that doesn't. The counts are this process's live holdings, read under
// the lock, so caps follow the config from one decision to the next.
//
// The caps:
//   - the profile's concurrency, for its top-level tasks and, as its own
//     cap, for its children;
//   - the install ceiling C, every kind counted, split into three disjoint
//     shares: the child reserve, the interactive reserve, and the shared
//     rest (at least one slot whenever C is; the config's Validate holds
//     the reserves to C-1 together). A child fills the child reserve first,
//     then the shared slots; an interactive top-level task the interactive
//     reserve first, then the shared slots; unattended top-level work only
//     the shared slots. So parents never hold the slots their children
//     need, and unattended work never holds the slots people need.
//
// The reserves come resolved in the config. A task over a cap is never
// failed for it: it stays queued. A submission already held here (a second
// delivery that raced the first past handle's check) is refused as capHeld,
// never given a second holding.
func (d *Dispatcher) reserve(snap lib.DispatchConfig, prof lib.DispatchProfile, tier, kind, taskID string) (*holding, string) {
	unattended := d.cfg.unattended(tier)
	shared := snap.Ceiling - snap.ChildReserve - snap.InteractiveReserve

	d.mu.Lock()
	defer d.mu.Unlock()
	hk := holdKey{prof.Name, tier, kind, taskID}
	if d.active[hk] != nil {
		return nil, capHeld
	}
	var total, children, interactiveTop, unattendedTop, profKind int
	for _, h := range d.active {
		total++
		switch {
		case h.kind == lib.SubmitKindChild:
			children++
		case h.unattended:
			unattendedTop++
		default:
			interactiveTop++
		}
		if h.profile == prof.Name && h.kind == kind {
			profKind++
		}
	}
	sharedUsed := max(0, children-snap.ChildReserve) + max(0, interactiveTop-snap.InteractiveReserve) + unattendedTop
	var fits bool
	switch {
	case kind == lib.SubmitKindChild:
		fits = children < snap.ChildReserve || sharedUsed < shared
	case unattended:
		fits = sharedUsed < shared
	default:
		fits = interactiveTop < snap.InteractiveReserve || sharedUsed < shared
	}
	switch {
	case profKind >= prof.Concurrency:
		return nil, "profile concurrency"
	case total >= snap.Ceiling:
		return nil, "install ceiling"
	case !fits:
		return nil, "the free slots are reserved for other kinds"
	}
	h := &holding{profile: prof.Name, tier: tier, kind: kind, taskID: taskID, unattended: unattended,
		renewals: make(chan renewal, renewalBuffer), done: make(chan struct{})}
	d.active[hk] = h
	return h, ""
}

// release frees a holding's slot. settled says the task's submission was
// acked, which a second delivery waiting on the holding follows.
func (d *Dispatcher) release(h *holding, settled bool) {
	d.mu.Lock()
	defer d.mu.Unlock()
	hk := h.key()
	if d.active[hk] == h {
		delete(d.active, hk)
		if settled {
			delete(d.verified, hk)
		}
		h.settled = settled
		close(h.done)
	}
}

// handOff offers the task to each free pod of its pool in turn until one
// accepts, then confirms it to that pod. Nothing is written while an offer
// is out: the holding's pod marks the pod busy in memory, and a dispatcher
// that restarts mid-offer finds no record and dispatches the task afresh
// (the pod it had offered it to never hears a confirm, so never starts).
//
// Only the pod the live offer is with is confirmed. An offer that times out
// is abandoned on its pod before the task is offered to anyone else, and any
// accept that comes after (onAccept) is answered with an abandon too, so a
// lost or late accept never leaves two workers running the task. On an
// accept the lease record is written, which starts the lease clock, and then
// the confirm is sent: a record whose confirm is lost costs the task (no
// renewal comes, and it fails lease-expired), never a second run. An empty
// key with a nil error means no pod took the task.
func (d *Dispatcher) handOff(ctx context.Context, log *slog.Logger, del lib.Delivery, snap lib.DispatchConfig,
	prof lib.DispatchProfile, h *holding) (lib.LeaseRecord, string, error) {
	envelope, err := json.Marshal(del.Envelope())
	if err != nil {
		return lib.LeaseRecord{}, "", err
	}
	// The intervals this task is offered with, kept for its life.
	h.lease, h.renew = seconds(snap.LeaseSeconds), seconds(snap.RenewSeconds)
	acceptTimeout := seconds(snap.AcceptTimeoutSeconds)
	pods := d.pool.Pods(h.profile, h.tier)
	// A pod holding a live lease in the bucket is busy even if this process
	// doesn't hold its task yet (a restart's redeliveries come up to an
	// AckWait later): each worker runs one task at a time.
	tried, err := d.leasedPods(ctx, h.profile, h.tier, seconds(snap.RestartGraceSeconds))
	if err != nil {
		return lib.LeaseRecord{}, "", fmt.Errorf("read which pods hold leases: %w", err)
	}
	for {
		d.mu.Lock()
		pod := d.freePodLocked(pods, h.profile, h.tier, tried)
		var ch chan lib.DispatchAccept
		var ok offerKey
		if pod != "" {
			h.pod = pod
			ch = make(chan lib.DispatchAccept, 1)
			ok = offerKey{h.profile, h.tier, pod, h.taskID}
			d.offers[ok] = ch
		}
		d.mu.Unlock()
		if pod == "" {
			return lib.LeaseRecord{}, "", nil
		}
		tried[pod] = true
		subject := lib.DispatchSubject(h.profile, h.tier, pod)
		// giveUp withdraws the offer, abandons it on the pod when there is
		// a reason to, and only then frees the pod for another offer.
		giveUp := func(reason string) {
			d.mu.Lock()
			delete(d.offers, ok)
			d.mu.Unlock()
			if reason != "" {
				_ = d.send(subject, lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: h.taskID, Reason: reason})
			}
			d.mu.Lock()
			h.pod = ""
			d.mu.Unlock()
		}

		// The pod's UID as the pool sees it at the offer, which its task
		// credentials name (#3889). With a key and no UID they can't be
		// minted, so the pod isn't offered the task at all: offered, it
		// would accept and then never be confirmed, and the task would fail.
		var uid string
		if d.mint != nil {
			if uid = d.pool.PodUID(h.profile, h.tier, pod); uid == "" {
				giveUp("")
				log.Warn("the pool has no UID for the pod, so no task credentials can name it; not offering it the task", "pod", pod)
				continue
			}
		}
		deadline := d.now().Add(seconds(prof.ActiveDeadlineSeconds))
		msg := lib.DispatchMessage{Type: lib.DispatchOffer, TaskID: h.taskID, Kind: h.kind, Envelope: envelope,
			Deadline: deadline, LeaseSeconds: snap.LeaseSeconds, RenewSeconds: snap.RenewSeconds,
			AcceptSeconds: snap.AcceptTimeoutSeconds}
		if err := d.send(subject, msg); err != nil {
			giveUp("")
			log.Warn("offer not sent", "pod", pod, "err", err)
			continue
		}
		timer := time.NewTimer(acceptTimeout)
		var acc lib.DispatchAccept
		var answered bool
		select {
		case acc = <-ch:
			answered = true
		case <-timer.C:
		case <-ctx.Done():
			// Stopping mid-offer: nothing was written, so the next
			// instance dispatches the task afresh; this pod is told to
			// drop it (and drops it anyway, never confirmed).
			timer.Stop()
			giveUp(abandonStopping)
			return lib.LeaseRecord{}, "", ctx.Err()
		}
		timer.Stop()
		switch {
		case answered && acc.Accepted:
			now := d.now()
			key := lib.ProfileLeaseKey(h.profile, h.tier, pod, h.taskID)
			rec := lib.LeaseRecord{TaskID: h.taskID, Pod: pod, Kind: h.kind,
				ConfirmedAt: now, RenewedAt: now, ExpiresAt: now.Add(h.lease),
				Deadline: deadline, LeaseSeconds: snap.LeaseSeconds, RenewSeconds: snap.RenewSeconds}
			if err := d.putLease(ctx, key, rec); err != nil {
				// Unrecorded, the task can't be confirmed: a restart
				// would not know it had started.
				giveUp(abandonUnrecorded)
				return lib.LeaseRecord{}, "", fmt.Errorf("record the lease before the confirm: %w", err)
			}
			d.mu.Lock()
			delete(d.offers, ok)
			h.confirmed = true
			d.mu.Unlock()
			// The task's credentials are minted here and only here: for
			// the one pod being confirmed, once its lease is recorded.
			// With a key, a confirm never goes out without them: a
			// credential that can't be minted is a confirm not sent.
			confirm := lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: h.taskID}
			if d.mint != nil {
				var err error
				confirm.TaskCredential, confirm.ProxyToken, err = d.taskCredentials(log, h, pod, uid, deadline)
				if err != nil {
					log.Error("task credentials not minted, so no confirm was sent; the task fails when its lease runs out", "pod", pod, "err", err)
					return rec, key, nil
				}
			}
			if err := d.send(subject, confirm); err != nil {
				// The lease is running; with no confirm the worker never
				// starts, no renewal comes, and the task fails
				// lease-expired. It is never offered again.
				log.Warn("confirm not sent; the task fails when its lease runs out", "pod", pod, "err", err)
			}
			return rec, key, nil
		case answered:
			log.Info("offer declined", "pod", pod, "reason", acc.Reason)
			giveUp("")
		default:
			log.Info("offer not accepted in time; abandoning it on that pod", "pod", pod, "timeout", acceptTimeout)
			giveUp(abandonTimedOut)
		}
	}
}

// taskCredentials mints the task credential (audience nats) and, when the
// task names a cluster, the credential-proxy token for the pod being
// confirmed (#3889). Both name the task, its profile and tier, the pod and
// the UID it had at the offer; a pod whose UID has changed since (deleted
// and re-created under its name) is not the pod that accepted, and gets
// nothing. Both expire at the offer's deadline plus the callout's clock-skew
// allowance, capped at the max lifetime from now so the callout never
// refuses one as too long-lived. The proxy token carries the
// vcs:proposal-create permit when the submission asked for a change, and no
// permits claim at all otherwise (#3993); the bus credential never carries
// one. Each is read back with the key's public
// half before it is used, and only its jti is logged, never the token.
func (d *Dispatcher) taskCredentials(log *slog.Logger, h *holding, pod, uid string, deadline time.Time) (natsToken, proxyToken string, err error) {
	if now := d.pool.PodUID(h.profile, h.tier, pod); now != uid {
		return "", "", fmt.Errorf("the pod's UID changed since the offer (%q, now %q)", uid, now)
	}
	now := d.now()
	expires := deadline.Add(authcallout.TaskCredentialSkew)
	if limit := now.Add(d.taskMaxLifetime); expires.After(limit) {
		expires = limit
	}
	base := taskcred.Claims{Task: h.taskID, Profile: h.profile, Tier: h.tier, Pod: pod, PodUID: uid, Expires: expires}
	natsToken, err = d.mintOne(log, base, taskcred.AudienceNATS, nil, now)
	if err != nil {
		return "", "", err
	}
	if h.cluster != nil {
		proxy := base
		if h.changeRequested {
			proxy.Permits = []taskcred.Permit{taskcred.PermitVCSProposalCreate}
		}
		proxyToken, err = d.mintOne(log, proxy, taskcred.AudienceCredentialProxy, h.cluster, now)
		if err != nil {
			return "", "", err
		}
	}
	return natsToken, proxyToken, nil
}

// mintOne mints one credential for aud, reads it back and logs its jti.
func (d *Dispatcher) mintOne(log *slog.Logger, c taskcred.Claims, aud taskcred.Audience, target *taskcred.Target, now time.Time) (string, error) {
	c.Audience = aud
	if target != nil {
		t := *target
		c.Target = &t
	}
	token, err := d.mint(c)
	if err != nil {
		return "", fmt.Errorf("mint the %s credential: %w", aud, err)
	}
	back, err := taskcred.Verify(token, d.taskKeys, aud, now, d.taskMaxLifetime, authcallout.TaskCredentialSkew)
	if err != nil {
		return "", fmt.Errorf("the %s credential just minted does not verify: %w", aud, err)
	}
	log.Info("task credential minted", "pod", c.Pod, "audience", string(aud), "jti", back.ID, "expires", back.Expires, "permits", back.Permits)
	return token, nil
}

// freePodLocked is a pod of the (profile, tier) pool that holds no task
// here (an outstanding offer counts) and hasn't been tried for this one.
// Callers hold d.mu.
func (d *Dispatcher) freePodLocked(pods []string, profile, tier string, tried map[string]bool) string {
	busy := map[string]bool{}
	for _, h := range d.active {
		if h.profile == profile && h.tier == tier && h.pod != "" {
			busy[h.pod] = true
		}
	}
	for _, pod := range pods {
		if !busy[pod] && !tried[pod] {
			return pod
		}
	}
	return ""
}

// adopt takes back a task a lease record shows was confirmed: after a
// restart, or after an earlier holder here gave it up without settling it.
// It is never offered again. It is failed lease-expired only once its
// record has expired and the restart grace, counted from this dispatcher's
// start, has passed without a renewal. Its deadline and intervals are the
// ones its offer carried, read from the record.
func (d *Dispatcher) adopt(ctx context.Context, log *slog.Logger, del lib.Delivery, snap lib.DispatchConfig, prof lib.DispatchProfile,
	hk holdKey, rec lib.LeaseRecord, key string) {
	pod := keyPod(key)
	lease := recordLease(rec, seconds(snap.LeaseSeconds))
	renew := seconds(rec.RenewSeconds)
	if renew <= 0 {
		renew = min(seconds(snap.RenewSeconds), lease/3)
	}
	h := &holding{profile: hk.profile, tier: hk.tier, kind: hk.kind, taskID: hk.taskID, unattended: d.cfg.unattended(hk.tier),
		lease: lease, renew: renew, pod: pod, confirmed: true,
		renewals: make(chan renewal, renewalBuffer), done: make(chan struct{})}
	d.mu.Lock()
	if first := d.active[hk]; first != nil {
		d.mu.Unlock()
		d.followHolder(ctx, log, del, first)
		return
	}
	d.active[hk] = h
	_, released := d.released[key]
	delete(d.released, key)
	graceUntil := d.started.Add(seconds(snap.RestartGraceSeconds))
	d.mu.Unlock()
	if released {
		log.Info("re-adopted a task its worker had already finished; settling it", "pod", pod)
		d.settle(ctx, log, del, h, key, rec)
		return
	}
	if rec.Deadline.IsZero() {
		// A record that doesn't say (unreadable, or not this dispatcher's
		// shape): the profile's current limit from the confirm.
		started := rec.ConfirmedAt
		if started.IsZero() {
			started = rec.RenewedAt
		}
		rec.Deadline = started.Add(seconds(prof.ActiveDeadlineSeconds))
	}
	log.Info("re-adopted a started task", "pod", pod, "expiresAt", rec.ExpiresAt, "graceUntil", graceUntil)
	d.hold(ctx, log, del, h, rec, key, graceUntil)
}

// hold keeps a running task's submission held (the lib sends the in-progress
// acks) until the task ends: a final renewal settles it, an expired lease or
// a passed deadline fails it and tells the worker to abandon it. Neither ever
// runs it again. graceUntil, set on re-adoption, keeps an expired record from
// failing the task before then.
func (d *Dispatcher) hold(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding, rec lib.LeaseRecord,
	key string, graceUntil time.Time) {
	tick := time.NewTicker(max(h.renew/ticksPerRenewal, minTick))
	defer tick.Stop()
	subject := lib.DispatchSubject(h.profile, h.tier, rec.Pod)
	for {
		select {
		case <-ctx.Done():
			// Stopping: no settlement; the record stays for the next
			// instance and the lib gives the submission back.
			return
		case r := <-h.renewals:
			if r.pod != rec.Pod {
				continue
			}
			if r.final {
				log.Info("task finished", "pod", rec.Pod)
				d.settle(ctx, log, del, h, key, rec)
				return
			}
			now := d.now()
			rec.RenewedAt, rec.ExpiresAt = now, now.Add(h.lease)
			graceUntil = time.Time{}
			if err := d.putLease(ctx, key, rec); err != nil {
				log.Warn("record a renewal", "err", err)
			}
		case <-tick.C:
			now := d.now()
			switch {
			case now.After(rec.Deadline):
				log.Warn("task passed its deadline; failing it", "pod", rec.Pod, "deadline", rec.Deadline)
				_ = d.send(subject, lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: h.taskID, Reason: abandonDeadline})
				d.fail(ctx, log, del, h, key, rec, ReasonDeadlineExceeded,
					fmt.Sprintf("ran past the profile's %s limit", deadlineLimit(rec)))
				return
			case now.Before(graceUntil):
			case rec.Expired(now, 0):
				log.Warn("task's lease expired; failing it", "pod", rec.Pod, "renewedAt", rec.RenewedAt)
				// A worker that is alive but unheard must not go on with a
				// task that has failed.
				_ = d.send(subject, lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: h.taskID, Reason: abandonLeaseExpired})
				d.fail(ctx, log, del, h, key, rec, ReasonLeaseExpired,
					fmt.Sprintf("worker %s stopped renewing at %s", rec.Pod, rec.RenewedAt.UTC().Format(time.RFC3339)))
				return
			}
		}
	}
}

// recordLease is the lease the task was offered: the record's own, else its
// expiry less its last renewal; fallback when the record can't say.
func recordLease(rec lib.LeaseRecord, fallback time.Duration) time.Duration {
	if rec.LeaseSeconds > 0 {
		return seconds(rec.LeaseSeconds)
	}
	if span := rec.ExpiresAt.Sub(rec.RenewedAt); span > 0 {
		return span
	}
	return fallback
}

func deadlineLimit(rec lib.LeaseRecord) time.Duration {
	start := rec.ConfirmedAt
	if start.IsZero() {
		start = rec.RenewedAt
	}
	return rec.Deadline.Sub(start).Round(time.Second)
}

// fail publishes a terminal failure and settles the task. If the terminal
// can't be published before shutdown the task isn't settled: it comes back
// to the next instance, whose record says started, and is failed there.
func (d *Dispatcher) fail(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding, key string, rec lib.LeaseRecord, reason, detail string) {
	for {
		err := d.publishTerminal(ctx, h.profile, del.Envelope(), lib.StateFailed, reason, detail)
		if err == nil {
			break
		}
		log.Warn("publish the failure terminal; retrying", "err", err)
		if !sleepCtx(ctx, settleRetry) {
			return
		}
	}
	d.settle(ctx, log, del, h, key, rec)
}

// settle ends a task whose outcome is decided (its final renewal came, or
// its failure terminal is written): it turns the lease record into a
// tombstone, acks the submission and waits for the server to confirm the
// ack, then frees the slot. The tombstone comes first, so the submission is
// never without a record: one that comes back before the ack lands (a lost
// ack, a crash) or long after (a durable re-created with DeliverAll replays
// the stream's retention) finds the tombstone and is acked as a replay,
// never offered again or failed a second time. A fire-and-forget ack can be
// lost, so the ack is confirmed. The sweep deletes the tombstone once no
// replay can come.
func (d *Dispatcher) settle(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding, key string, rec lib.LeaseRecord) {
	if !d.entomb(ctx, log, key, rec) {
		return
	}
	if !d.ackSyncRetry(ctx, del) {
		return
	}
	d.release(h, true)
}

// entomb writes rec under key as a tombstone, finished now, until the write
// lands or ctx ends. Every outcome the dispatcher sees or writes (a final
// renewal, a failure, a refusal) is one: the task is over.
func (d *Dispatcher) entomb(ctx context.Context, log *slog.Logger, key string, rec lib.LeaseRecord) bool {
	rec.FinishedAt = d.now()
	if err := rec.Validate(); err != nil {
		// Not a write that can ever land: the outcome is decided all the
		// same, so the task settles without its replay guard rather than
		// hold its slot for good.
		log.Error("a tombstone that can't be written; settling without it", "key", key, "err", err)
		return true
	}
	for {
		err := d.putLease(ctx, key, rec)
		if err == nil {
			return true
		}
		log.Warn("write the task's tombstone; retrying", "key", key, "err", err)
		if !sleepCtx(ctx, settleRetry) {
			return false
		}
	}
}

// ackSyncRetry acks del until the server confirms it. A repeat ack of a
// message already acked is confirmed too, so a retry after a lost reply
// ends.
func (d *Dispatcher) ackSyncRetry(ctx context.Context, del lib.Delivery) bool {
	for {
		actx, cancel := context.WithTimeout(ctx, kvOpTimeout)
		err := del.AckSync(actx)
		cancel()
		if err == nil {
			return true
		}
		if !sleepCtx(ctx, settleRetry) {
			return false
		}
	}
}

func (d *Dispatcher) ackRetry(ctx context.Context, del lib.Delivery) bool {
	for del.Ack() != nil {
		if !sleepCtx(ctx, settleRetry) {
			return false
		}
	}
	return true
}

// heldOnLocked is the holding for a task in (profile, tier) confirmed to
// pod, of either kind, or nil. Callers hold d.mu.
func (d *Dispatcher) heldOnLocked(profile, tier, pod, taskID string) *holding {
	for _, kind := range submitKinds {
		if h := d.active[holdKey{profile, tier, kind, taskID}]; h != nil && h.pod == pod && h.confirmed {
			return h
		}
	}
	return nil
}

// heldAnywhereLocked reports whether a task in (profile, tier) is held here
// at all. Callers hold d.mu.
func (d *Dispatcher) heldAnywhereLocked(profile, tier, taskID string) bool {
	for _, kind := range submitKinds {
		if d.active[holdKey{profile, tier, kind, taskID}] != nil {
			return true
		}
	}
	return false
}

// allowUnknown spends one of a pod's answers for tasks it doesn't hold, and
// says whether to log about it. An answer over the budget is dropped and
// counted.
func (d *Dispatcher) allowUnknown(profile, tier, pod string) (allowed, logIt bool) {
	now := d.now()
	id := profile + "." + tier + "." + pod
	d.mu.Lock()
	defer d.mu.Unlock()
	b := d.answers[id]
	if b == nil {
		b = &answerBudget{}
		d.answers[id] = b
	}
	if now.Sub(b.window) >= unknownAnswerWindow {
		b.window, b.n = now, 0
	}
	if b.n >= unknownAnswersPerWindow {
		b.dropped++
		d.droppedAnswers.Add(1)
		return false, false
	}
	b.n++
	if now.Sub(b.lastLog) >= unknownAnswerLogEvery {
		b.lastLog = now
		return true, true
	}
	return true, false
}

// onAccept routes a worker's accept to the offer waiting on it. The pod is
// the subject's token, never a payload field, so an accept on one pod's
// subject never answers another pod's offer. Any other accept (a late one
// for an offer that timed out, one for a task never offered to this pod) is
// answered with an abandon, so the worker never runs a task it was not
// confirmed; a repeat of an accept already confirmed is ignored. Those
// answers are held to the pod's budget.
func (d *Dispatcher) onAccept(msg *nats.Msg) {
	profile, tier, pod, ok := answerSubject(msg.Subject, "accept")
	if !ok {
		return
	}
	var acc lib.DispatchAccept
	if err := json.Unmarshal(msg.Data, &acc); err != nil || acc.Validate() != nil {
		if allowed, logIt := d.allowUnknown(profile, tier, pod); allowed && logIt {
			d.log.Warn("ignored a malformed accept", "subject", msg.Subject)
		}
		return
	}
	d.mu.Lock()
	ch := d.offers[offerKey{profile, tier, pod, acc.TaskID}]
	repeat := ch == nil && d.heldOnLocked(profile, tier, pod, acc.TaskID) != nil
	d.mu.Unlock()
	if ch != nil {
		select {
		case ch <- acc:
		default:
		}
		return
	}
	if repeat || !acc.Accepted {
		return // already confirmed, or a decline that needs nothing
	}
	allowed, logIt := d.allowUnknown(profile, tier, pod)
	if !allowed {
		return
	}
	if logIt {
		d.log.Warn("abandoning an accept no live offer waits on", "subject", msg.Subject, "task", acc.TaskID)
	}
	_ = d.send(lib.DispatchSubject(profile, tier, pod),
		lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: acc.TaskID, Reason: abandonWithdrawn})
}

// onRenewal routes a renewal to the hold for that task, if this dispatcher
// confirmed the task to the pod the subject names. A renewal for a task held
// here otherwise is ignored. One for a task not held here at all (a
// restart's redeliveries arrive up to an AckWait later) refreshes the record
// only if the bucket shows the task on that same pod. Everything but a held
// task's own renewals is held to the pod's budget.
func (d *Dispatcher) onRenewal(msg *nats.Msg) {
	profile, tier, pod, ok := answerSubject(msg.Subject, "lease")
	if !ok {
		return
	}
	var r lib.LeaseRenewal
	if err := json.Unmarshal(msg.Data, &r); err != nil || r.Validate() != nil {
		if allowed, logIt := d.allowUnknown(profile, tier, pod); allowed && logIt {
			d.log.Warn("ignored a malformed renewal", "subject", msg.Subject)
		}
		return
	}
	d.mu.Lock()
	h := d.heldOnLocked(profile, tier, pod, r.TaskID)
	elsewhere := h == nil && d.heldAnywhereLocked(profile, tier, r.TaskID)
	d.mu.Unlock()
	if h != nil {
		rn := renewal{pod: pod, final: r.Final}
		select {
		case h.renewals <- rn:
		default:
			// A full buffer drops a plain renewal (the next one renews);
			// a final one must land, by one goroutine per hold at most.
			if rn.final && h.finalQueued.CompareAndSwap(false, true) {
				go func() {
					select {
					case h.renewals <- rn:
					case <-h.done:
					}
				}()
			}
		}
		return
	}
	allowed, logIt := d.allowUnknown(profile, tier, pod)
	if !allowed {
		return
	}
	if elsewhere {
		if logIt {
			d.log.Warn("ignored a renewal for a task not confirmed to this pod", "subject", msg.Subject, "task", r.TaskID)
		}
		return
	}
	key := lib.ProfileLeaseKey(profile, tier, pod, r.TaskID)
	// A plain renewal with one already being looked up is redundant; a
	// final one never is.
	busyKey := key
	if r.Final {
		busyKey += ".final"
	}
	d.mu.Lock()
	skip := d.orphanBusy[busyKey]
	if !skip {
		select {
		case d.orphanSem <- struct{}{}:
			d.orphanBusy[busyKey] = true
		default:
			skip = true // every lookup slot is taken: drop it
			d.droppedAnswers.Add(1)
		}
	}
	d.mu.Unlock()
	if skip {
		return
	}
	go func() {
		defer func() {
			d.mu.Lock()
			delete(d.orphanBusy, busyKey)
			d.mu.Unlock()
			<-d.orphanSem
		}()
		d.renewOrphan(key, r, logIt)
	}()
}

// renewOrphan handles a renewal for a task not held here: refreshed (by
// compare-and-set, so a record deleted meanwhile stays deleted) when the
// bucket holds it under this pod's key, ignored otherwise. A final one is
// remembered, so the task is settled, not failed, when its submission comes
// back. A tombstone is never renewed: its task is over.
func (d *Dispatcher) renewOrphan(key string, r lib.LeaseRenewal, logIt bool) {
	ctx, cancel := context.WithTimeout(context.Background(), kvOpTimeout)
	defer cancel()
	kv, err := d.bus.KV(ctx, lib.ProfileLeaseBucket)
	if err != nil {
		if logIt {
			d.log.Warn("renewal for a task not held here: lease bucket unavailable", "key", key, "err", err)
		}
		return
	}
	entry, err := kv.Get(ctx, key)
	if err != nil {
		if logIt {
			d.log.Warn("ignored a renewal for a task not confirmed to this pod", "key", key, "err", err)
		}
		return
	}
	var rec lib.LeaseRecord
	recErr := json.Unmarshal(entry.Value(), &rec)
	if recErr == nil && rec.Finished() {
		// The task is over: a tombstone is never renewed back to life.
		if logIt {
			d.log.Warn("ignored a renewal for a finished task", "key", key, "final", r.Final)
		}
		return
	}
	if r.Final {
		d.mu.Lock()
		d.released[key] = d.now()
		d.mu.Unlock()
		return
	}
	if err := recErr; err != nil {
		if logIt {
			d.log.Warn("renewal for a task not held here: unreadable record", "key", key, "err", err)
		}
		return
	}
	now := d.now()
	rec.RenewedAt, rec.ExpiresAt = now, now.Add(recordLease(rec, seconds(d.src.Config().LeaseSeconds)))
	data, err := json.Marshal(rec)
	if err != nil {
		return
	}
	if _, err := kv.Update(ctx, key, data, entry.Revision()); err != nil {
		d.log.Debug("renewal for a task not held here: record moved meanwhile", "key", key, "err", err)
	}
}

// prune drops the in-memory state nothing will read again: finals heard
// for tasks whose submission never came back, verifier permits past their
// use, idle answer budgets, and unbound durables whose last hold returned.
func (d *Dispatcher) prune() {
	now := d.now()
	d.mu.Lock()
	for key, at := range d.released {
		if now.Sub(at) > d.cfg.SweepAfter {
			delete(d.released, key)
		}
	}
	for hk, p := range d.verified {
		if now.Sub(p.at) > verifiedFor {
			delete(d.verified, hk)
		}
	}
	for id, b := range d.answers {
		if now.Sub(b.window) > unknownAnswerLogEvery {
			delete(d.answers, id)
		}
	}
	draining := d.draining
	d.draining = nil
	d.mu.Unlock()
	// An unbound durable with no hold left running is done with. Wait on a
	// context already done answers at once.
	done, cancel := context.WithCancel(context.Background())
	cancel()
	var keep []lib.HeldSubscription
	for _, s := range draining {
		if s.Wait(done) == nil {
			s.Stop()
			continue
		}
		keep = append(keep, s)
	}
	d.mu.Lock()
	d.draining = append(d.draining, keep...)
	d.mu.Unlock()
}

// sweep deletes lease records nothing will read again, by revision, so a
// record written meanwhile stays:
//   - a tombstone once it is older than the submit stream's retention plus
//     tombstoneMargin: no replay of its submission can come any more;
//   - a running lease expired for longer than SweepAfter, held by no hold
//     here, in a (profile, tier) whose durables are all bound (so a
//     submission still on the stream would have come back and been
//     re-adopted long since).
func (d *Dispatcher) sweep(ctx context.Context) {
	kctx, cancel := context.WithTimeout(ctx, kvOpTimeout)
	defer cancel()
	kv, err := d.bus.KV(kctx, lib.ProfileLeaseBucket)
	if err != nil {
		d.log.Warn("lease sweep: bucket unavailable", "err", err)
		return
	}
	lister, err := kv.ListKeys(kctx)
	if err != nil {
		d.log.Warn("lease sweep: list the records", "err", err)
		return
	}
	var keys []string
	for k := range lister.Keys() {
		keys = append(keys, k)
	}
	cfg := d.src.Config()
	now := d.now()
	for _, k := range keys {
		t := strings.Split(k, ".")
		if len(t) != 4 {
			continue
		}
		profile, tier, taskID := t[0], t[1], t[3]
		d.mu.Lock()
		held := d.heldAnywhereLocked(profile, tier, taskID)
		bound := d.tierBoundLocked(cfg, profile, tier)
		d.mu.Unlock()
		if held {
			continue
		}
		entry, err := kv.Get(kctx, k)
		if err != nil {
			continue
		}
		var rec lib.LeaseRecord
		readable := json.Unmarshal(entry.Value(), &rec) == nil
		if readable && rec.Finished() {
			// Bound or not: the stream drops the submission at its age
			// limit either way.
			if keepUntil := rec.FinishedAt.Add(submitRetention(cfg) + tombstoneMargin); now.Before(keepUntil) {
				continue
			}
			if err := kv.Delete(kctx, k, jetstream.LastRevision(entry.Revision())); err != nil {
				d.log.Debug("lease sweep: tombstone moved meanwhile", "key", k, "err", err)
				continue
			}
			d.log.Info("swept a tombstone past the submit stream's retention", "key", k, "finishedAt", rec.FinishedAt)
			continue
		}
		if !bound || (readable && !rec.Expired(now, d.cfg.SweepAfter)) {
			continue
		}
		if err := kv.Delete(kctx, k, jetstream.LastRevision(entry.Revision())); err != nil {
			d.log.Debug("lease sweep: record moved meanwhile", "key", k, "err", err)
			continue
		}
		d.log.Info("swept a lease record nothing holds", "key", k, "expiresAt", rec.ExpiresAt)
	}
}

// submitRetention is how long the submit stream keeps a submission, which
// a tombstone must outlive. The install config will carry the stream's
// max age (DispatchConfig.SubmitRetentionSeconds, a follow-up); until then
// it is the operator's fixed value.
func submitRetention(lib.DispatchConfig) time.Duration {
	return lib.ProfileSubmitRetention
}

// tierBoundLocked reports whether every durable of a (profile, tier) the
// config lists is bound. Callers hold d.mu.
func (d *Dispatcher) tierBoundLocked(cfg lib.DispatchConfig, profile, tier string) bool {
	prof, ok := profileOf(cfg, profile)
	if !ok {
		return false
	}
	n := 0
	for _, dur := range prof.Durables {
		if dur.Tier != tier {
			continue
		}
		if d.subs[dur.Name] == nil {
			return false
		}
		n++
	}
	return n > 0
}

// answerSubject splits a2a.dispatch.<profile>.<tier>.<pod>.<class>.
func answerSubject(subject, class string) (profile, tier, pod string, ok bool) {
	t := strings.Split(subject, ".")
	if len(t) != answerTokens || t[len(t)-1] != class {
		return "", "", "", false
	}
	return t[2], t[3], t[4], true
}

// keyPod is the pod token of a lease key, <profile>.<tier>.<pod>.<task>:
// the key, which only the dispatcher writes, is the pod's identity, not
// the record's Pod field.
func keyPod(key string) string {
	t := strings.Split(key, ".")
	if len(t) != strings.Count(lib.ProfileLeaseKey("p", "t", "x", "k"), ".")+1 {
		return ""
	}
	return t[2]
}

func (d *Dispatcher) send(subject string, msg lib.DispatchMessage) error {
	if err := msg.Validate(); err != nil {
		return err
	}
	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	nc := d.bus.Conn()
	if nc == nil {
		return errors.New("not connected")
	}
	return nc.Publish(subject, data)
}

// findLease looks the task up by ProfileLeaseKeyFilter. With more than one
// record a tombstone wins (the task is over, whatever else the bucket
// holds), else the latest renewed.
func (d *Dispatcher) findLease(ctx context.Context, profile, tier, taskID string) (lib.LeaseRecord, string, bool, error) {
	kctx, cancel := context.WithTimeout(ctx, kvOpTimeout)
	defer cancel()
	kv, err := d.bus.KV(kctx, lib.ProfileLeaseBucket)
	if err != nil {
		return lib.LeaseRecord{}, "", false, err
	}
	lister, err := kv.ListKeysFiltered(kctx, lib.ProfileLeaseKeyFilter(profile, tier, taskID))
	if err != nil {
		return lib.LeaseRecord{}, "", false, err
	}
	var keys []string
	for k := range lister.Keys() {
		keys = append(keys, k)
	}
	if kctx.Err() != nil {
		return lib.LeaseRecord{}, "", false, kctx.Err()
	}
	sort.Strings(keys)
	var best lib.LeaseRecord
	var bestKey string
	for _, k := range keys {
		entry, err := kv.Get(kctx, k)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return lib.LeaseRecord{}, "", false, err
		}
		var rec lib.LeaseRecord
		if err := json.Unmarshal(entry.Value(), &rec); err != nil {
			// A record nobody can read still says the task started.
			d.log.Warn("unreadable lease record; treating the task as started", "key", k, "err", err)
			rec = lib.LeaseRecord{TaskID: taskID, Pod: keyPod(k)}
		}
		if bestKey == "" || (rec.Finished() && !best.Finished()) ||
			(rec.Finished() == best.Finished() && rec.RenewedAt.After(best.RenewedAt)) {
			best, bestKey = rec, k
		}
	}
	if bestKey == "" {
		return lib.LeaseRecord{}, "", false, nil
	}
	best.TaskID = taskID
	best.Pod = keyPod(bestKey)
	return best, bestKey, true, nil
}

// leasedPods is the pods that hold a live lease record in a (profile,
// tier): one not expired, given grace. A record past that is no worker's
// current task (its worker died, or it is garbage the sweep has yet to
// remove), so it doesn't keep its pod from new work; nor does a tombstone,
// which Expired always reports expired.
func (d *Dispatcher) leasedPods(ctx context.Context, profile, tier string, grace time.Duration) (map[string]bool, error) {
	kctx, cancel := context.WithTimeout(ctx, kvOpTimeout)
	defer cancel()
	kv, err := d.bus.KV(kctx, lib.ProfileLeaseBucket)
	if err != nil {
		return nil, err
	}
	lister, err := kv.ListKeysFiltered(kctx, lib.ProfileLeaseKeyFilter(profile, tier, "*"))
	if err != nil {
		return nil, err
	}
	var keys []string
	for k := range lister.Keys() {
		keys = append(keys, k)
	}
	if kctx.Err() != nil {
		return nil, kctx.Err()
	}
	now := d.now()
	pods := map[string]bool{}
	for _, k := range keys {
		pod := keyPod(k)
		if pod == "" || pods[pod] {
			continue
		}
		entry, err := kv.Get(kctx, k)
		if errors.Is(err, jetstream.ErrKeyNotFound) {
			continue
		}
		if err != nil {
			return nil, err
		}
		var rec lib.LeaseRecord
		if err := json.Unmarshal(entry.Value(), &rec); err == nil && rec.Expired(now, grace) {
			continue
		}
		pods[pod] = true
	}
	return pods, nil
}

func (d *Dispatcher) putLease(ctx context.Context, key string, rec lib.LeaseRecord) error {
	if err := rec.Validate(); err != nil {
		return err
	}
	kctx, cancel := context.WithTimeout(ctx, kvOpTimeout)
	defer cancel()
	data, err := json.Marshal(rec)
	if err != nil {
		return err
	}
	kv, err := d.bus.KV(kctx, lib.ProfileLeaseBucket)
	if err != nil {
		return err
	}
	_, err = kv.Put(kctx, key, data)
	return err
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}
