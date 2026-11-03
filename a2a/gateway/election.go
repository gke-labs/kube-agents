package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/leaderelection"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
)

// The election decides which of the gateway's replicas does the work
// (docs/designs/gateway-replicas.md, "Decision: active-standby first"). A
// Kubernetes Lease elects the active replica, and only it runs the gateway.
// The others wait for the lease.
const (
	// electionLeaseDuration is how long a standby waits after the holder's
	// last renewal before taking the lease. client-go's 15s default leaves
	// about 3s between a leader giving up (renewDeadline after its first
	// failed renewal, which is retryPeriod after its last good one) and a
	// standby taking over; 20s leaves about 8s for the step-down.
	electionLeaseDuration = 20 * time.Second
	electionRenewDeadline = 10 * time.Second
	electionRetryPeriod   = 2 * time.Second

	// lostLeaseRelayDrain bounds the relay's render drain when the lease is
	// lost, inside the step-down budget above. SIGTERM keeps
	// relayStopDrainTimeout, sized to the pod's grace period.
	lostLeaseRelayDrain = 5 * time.Second

	// relayAckWait is the relay durable's AckWait. The relay creates or
	// updates its durable with none on every bind, so it's always the
	// server's default.
	relayAckWait = 30 * time.Second

	// releasedLeaseSeconds is the duration a released lease records, as
	// client-go's own release writes it.
	releasedLeaseSeconds = 1
)

// ErrLeadershipLost reports a gateway that stopped because it lost its
// lease, or because a registry write showed a newer term had started. The
// process exits on it, and the restarted container waits as a standby.
var ErrLeadershipLost = errors.New("the gateway lost its lease")

// errFencedWrite is the cause a fenced registry write steps the gateway
// down with.
var errFencedWrite = fmt.Errorf("%w: a session-registry write was refused because a newer gateway term has started", ErrLeadershipLost)

// Election is the gateway's campaign for its Lease.
type Election struct {
	Client    kubernetes.Interface
	Namespace string
	// Lease names the Lease, and Identity is this replica's name in it: the
	// pod's name, so a container restarting in its own pod re-takes the
	// lease at once.
	Lease    string
	Identity string
	Log      *slog.Logger

	// The election's timings. Zero means the defaults above; tests shorten
	// them.
	LeaseDuration time.Duration
	RenewDeadline time.Duration
	RetryPeriod   time.Duration
}

// Leader is the gateway as the election drives it.
type Leader interface {
	// BeginTerm starts a new term (Gateway.BeginTerm).
	BeginTerm(ctx context.Context) (uint64, error)
	// OnFenced sets what runs when a write is refused by a newer term
	// (Gateway.OnFenced).
	OnFenced(f func())
}

// Run campaigns for the lease until ctx ends. On acquiring it, Run starts a
// term and then calls lead, with the options an elected gateway runs with
// (RunOptions), and a context that ends on SIGTERM (ctx), on losing the
// lease, or on a write fenced by a newer term. The lease is released only
// after lead returns and only if this replica still holds it, so a standby
// takes over from a gateway that has stopped.
//
// Run returns nil when ctx ended, ErrLeadershipLost when the lease was lost
// or a write was fenced, and otherwise the error that stopped lead.
func (e Election) Run(ctx context.Context, leader Leader, lead func(context.Context, RunOptions) error) error {
	log := e.Log
	if log == nil {
		log = slog.Default()
	}
	lock := &observingLock{Interface: &resourcelock.LeaseLock{
		LeaseMeta:  metav1.ObjectMeta{Namespace: e.Namespace, Name: e.Lease},
		Client:     e.Client.CoordinationV1(),
		LockConfig: resourcelock.ResourceLockConfig{Identity: e.Identity},
	}}
	// The elector's context outlives ctx: on SIGTERM the gateway keeps
	// renewing while it stops, and releases the lease once it has stopped.
	electCtx, stopElecting := context.WithCancel(context.WithoutCancel(ctx))
	defer stopElecting()

	var result error
	led := make(chan struct{})
	elector, err := leaderelection.NewLeaderElector(leaderelection.LeaderElectionConfig{
		Lock:          lock,
		Name:          e.Lease,
		LeaseDuration: orDefault(e.LeaseDuration, electionLeaseDuration),
		RenewDeadline: orDefault(e.RenewDeadline, electionRenewDeadline),
		RetryPeriod:   orDefault(e.RetryPeriod, electionRetryPeriod),
		// Not client-go's release: it runs when renewing gives up, before the
		// work has been told to stop, and on a stale view of who holds the
		// lease. release below runs after both, on a fresh read.
		ReleaseOnCancel: false,
		Callbacks: leaderelection.LeaderCallbacks{
			OnStartedLeading: func(leaseCtx context.Context) {
				defer close(led)
				defer stopElecting()
				result = e.lead(ctx, leaseCtx, lock.afterCrash(), leader, lead, log)
			},
			OnStoppedLeading: func() {},
		},
	})
	if err != nil {
		return fmt.Errorf("gateway election: %w", err)
	}
	// A standby has no work to stop: SIGTERM ends its campaign at once.
	go func() {
		select {
		case <-ctx.Done():
			if !lock.acquired() {
				stopElecting()
			}
		case <-electCtx.Done():
		}
	}()
	log.Info("gateway election: waiting for the lease", "lease", e.Lease, "identity", e.Identity)
	elector.Run(electCtx)
	if !lock.acquired() {
		return result
	}
	// The elector returns as soon as a renewal fails, while the work is
	// still stopping; on the other paths the work has returned and stopped
	// the elector. Either way nothing renews from here on.
	<-led
	e.release(lock, log)
	return result
}

// release gives the lease up once the work has stopped, so a standby can
// take it at once and knows nobody holds events. It writes only if a fresh
// read still names this replica: after a lost lease or a fenced write
// another replica usually holds it, and that holder must not be cleared.
func (e Election) release(lock *observingLock, log *slog.Logger) {
	ctx, cancel := context.WithTimeout(context.Background(), orDefault(e.RenewDeadline, electionRenewDeadline))
	defer cancel()
	rec, _, err := lock.Interface.Get(ctx)
	if err != nil {
		log.Warn("gateway election: lease not released: it couldn't be read; a standby takes it once it expires", "lease", e.Lease, "err", err)
		return
	}
	if rec.HolderIdentity != e.Identity {
		log.Info("gateway election: lease not released: another replica holds it", "lease", e.Lease, "holder", rec.HolderIdentity)
		return
	}
	now := metav1.NewTime(time.Now())
	// The record client-go's own release writes: no holder, and a duration
	// of one second, so a standby that reads it takes the lease at once.
	released := resourcelock.LeaderElectionRecord{
		LeaderTransitions:    rec.LeaderTransitions,
		LeaseDurationSeconds: releasedLeaseSeconds,
		AcquireTime:          now,
		RenewTime:            now,
	}
	// Update is conditional on the revision Get read, so a replica that
	// took the lease in between isn't overwritten.
	if err := lock.Interface.Update(ctx, released); err != nil {
		log.Warn("gateway election: lease not released; a standby takes it once it expires", "lease", e.Lease, "err", err)
		return
	}
	log.Info("gateway election: lease released", "lease", e.Lease)
}

// lead is one term: start it, run the work, and say why it ended.
func (e Election) lead(ctx, leaseCtx context.Context, afterCrash bool, leader Leader, lead func(context.Context, RunOptions) error, log *slog.Logger) error {
	work, stop := context.WithCancelCause(ctx)
	defer stop(nil)
	go func() {
		select {
		case <-leaseCtx.Done():
			stop(ErrLeadershipLost)
		case <-work.Done():
		}
	}()
	leader.OnFenced(func() { stop(errFencedWrite) })
	lost := func() error {
		if cause := context.Cause(work); errors.Is(cause, ErrLeadershipLost) && ctx.Err() == nil {
			return cause
		}
		return nil
	}

	term, err := leader.BeginTerm(work)
	if err != nil {
		if cause := lost(); cause != nil {
			return cause
		}
		if ctx.Err() != nil {
			return nil
		}
		log.Error("gateway election: could not start a term, so this gateway won't lead", "err", err)
		return fmt.Errorf("starting a gateway term: %w", err)
	}
	opts := RunOptions{StopDrain: func() time.Duration {
		if errors.Is(context.Cause(work), ErrLeadershipLost) {
			return lostLeaseRelayDrain
		}
		return relayStopDrainTimeout
	}}
	if afterCrash {
		opts.RelayBindDelay = relayAckWait
	}
	log.Info("gateway election: leading", "lease", e.Lease, "identity", e.Identity, "term", term, "afterCrash", afterCrash)
	err = lead(work, opts)
	if cause := lost(); cause != nil {
		log.Warn("gateway election: stepped down", "lease", e.Lease, "term", term, "reason", cause)
		return cause
	}
	if ctx.Err() != nil {
		return nil
	}
	return err
}

func orDefault(d, def time.Duration) time.Duration {
	if d <= 0 {
		return def
	}
	return d
}

// observingLock is the Lease lock, noting what the lease held just before
// this process acquired it. A process acquires at most once (it exits when
// it stops leading), so its first successful write is the acquisition.
type observingLock struct {
	resourcelock.Interface

	mu sync.Mutex
	// lastHolder and lastMissing are what the most recent read found: the
	// holder's identity, or no Lease at all.
	lastHolder  string
	lastMissing bool
	took        bool
	// crashed is what the read before the acquisition found: a holder that
	// didn't release the lease (it crashed, or it's this pod's previous
	// container), or no Lease to read.
	crashed bool
}

func (l *observingLock) Get(ctx context.Context) (*resourcelock.LeaderElectionRecord, []byte, error) {
	rec, raw, err := l.Interface.Get(ctx)
	l.mu.Lock()
	defer l.mu.Unlock()
	switch {
	case apierrors.IsNotFound(err):
		l.lastHolder, l.lastMissing = "", true
	case err == nil:
		l.lastHolder, l.lastMissing = rec.HolderIdentity, false
	}
	return rec, raw, err
}

func (l *observingLock) Create(ctx context.Context, rec resourcelock.LeaderElectionRecord) error {
	err := l.Interface.Create(ctx, rec)
	l.wrote(err)
	return err
}

func (l *observingLock) Update(ctx context.Context, rec resourcelock.LeaderElectionRecord) error {
	err := l.Interface.Update(ctx, rec)
	l.wrote(err)
	return err
}

func (l *observingLock) wrote(err error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if err != nil || l.took {
		return
	}
	l.took = true
	// A release leaves the holder empty; anything else means the last
	// holder never gave its events back.
	l.crashed = l.lastMissing || l.lastHolder != ""
}

func (l *observingLock) acquired() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.took
}

func (l *observingLock) afterCrash() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.crashed
}
