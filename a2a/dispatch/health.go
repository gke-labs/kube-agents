package dispatch

import (
	"sort"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// waitingForgetAfter is how many retry delays a waiting entry outlives its
// last redelivery before the health line forgets it (the task was settled
// some other way, or its durable is gone).
const waitingForgetAfter = 3

// Health is the dispatcher's state at one moment, as the health line logs
// it.
type Health struct {
	// Running is every task a worker accepted (its lease is written), by
	// profile.
	Running map[string]int
	// Offering is every task out on offer to a pod that has not accepted
	// yet, by profile. A submission re-offered after no worker took it is
	// also in Waiting and NoWorker meanwhile.
	Offering map[string]int
	// Waiting is the submissions waiting, over a cap or for a free worker,
	// by durable.
	Waiting map[string]int
	// NoWorker is the part of Waiting that no free worker took (the pool
	// had no free pod, or none accepted), by durable.
	NoWorker map[string]int
	// Breaker is the part of Waiting a breaker holds, open or half open,
	// by durable, and HandOffFailed the part whose hand-off errored (a
	// lease that couldn't be written, or an unattended task's Job that
	// couldn't be launched or read), retried each delay.
	Breaker       map[string]int
	HandOffFailed map[string]int
	// OldestWait is the longest a waiting submission has been queued, by
	// durable.
	OldestWait map[string]time.Duration
	// Stalled are the durables with a submission waiting longer than
	// StallAfter and no admission from them in that time: what a
	// cross-chain admission cycle looks like until the deadlines end it, or
	// a pool with no ready worker (every wait in NoWorker).
	Stalled []string
	// DroppedAnswers counts worker answers for tasks they don't hold that
	// were dropped over a pod's budget, since start.
	DroppedAnswers int64
	// OpenBreakers are the (profile, tier) pairs, as profile/tier, whose
	// breaker is open, holding every new submission queued, and
	// HalfOpenBreakers those past the cooldown, trying one task at a time
	// and holding the rest (breaker.go).
	OpenBreakers     []string
	HalfOpenBreakers []string
	// StopStream is why the dispatcher can't read the stop stream, or ""
	// (stop.go): while it is set, a stop counts only if heard live.
	StopStream string
	// JobAdmission is the last refusal of a Job by the dispatcher Job
	// admission policy, or "" (job.go).
	JobAdmission string
}

func (d *Dispatcher) noteWaiting(hk holdKey, queued time.Time, why string) {
	now := d.now()
	if queued.IsZero() {
		queued = now
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	w, ok := d.waiting[hk]
	if !ok {
		w = waiting{durable: lib.ProfileDurableName(hk.profile, hk.tier, hk.kind), queued: queued}
	}
	w.lastSeen, w.why = now, why
	d.waiting[hk] = w
}

// offeringWaiting is a reserved submission's waiting entry as its offer
// round starts. Waiting over a cap ends there: the slot is taken. Waiting
// for a worker doesn't, nor waiting on a hand-off that keeps failing: until a
// pod accepts, the submission still waits, and the health line keeps saying
// so through the round.
func (d *Dispatcher) offeringWaiting(hk holdKey) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if w, ok := d.waiting[hk]; ok && w.why != ReasonNoWorker && w.why != waitHandOffFailed {
		delete(d.waiting, hk)
	}
}

func (d *Dispatcher) clearWaiting(hk holdKey) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.waiting, hk)
}

// Health reports the current state.
func (d *Dispatcher) Health() Health {
	now := d.now()
	d.mu.Lock()
	defer d.mu.Unlock()
	h := Health{Running: map[string]int{}, Offering: map[string]int{}, Waiting: map[string]int{}, NoWorker: map[string]int{}, Breaker: map[string]int{}, HandOffFailed: map[string]int{},
		OldestWait: map[string]time.Duration{}, DroppedAnswers: d.droppedAnswers.Load()}
	// offering is the tasks out on offer: a waiting entry for one is not
	// forgotten while its offer round runs, however long the accept
	// timeouts make it.
	offering := map[holdKey]bool{}
	for _, a := range d.active {
		if a.confirmed {
			h.Running[a.profile]++
			continue
		}
		h.Offering[a.profile]++
		offering[a.key()] = true
	}
	forget := time.Duration(waitingForgetAfter) * d.cfg.RetryDelay
	for hk, w := range d.waiting {
		if now.Sub(w.lastSeen) > forget && !offering[hk] {
			delete(d.waiting, hk)
			continue
		}
		h.Waiting[w.durable]++
		switch w.why {
		case ReasonNoWorker:
			h.NoWorker[w.durable]++
		case waitBreaker:
			h.Breaker[w.durable]++
		case waitHandOffFailed:
			h.HandOffFailed[w.durable]++
		}
		if age := now.Sub(w.queued); age > h.OldestWait[w.durable] {
			h.OldestWait[w.durable] = age
		}
	}
	for durable, oldest := range h.OldestWait {
		if oldest > d.cfg.StallAfter && now.Sub(d.lastAdmit[durable]) > d.cfg.StallAfter {
			h.Stalled = append(h.Stalled, durable)
		}
	}
	sort.Strings(h.Stalled)
	h.OpenBreakers, h.HalfOpenBreakers = d.breakers.open(now)
	h.StopStream = d.stopStreamErr
	h.JobAdmission = d.jobAdmissionErr
	return h
}

func (d *Dispatcher) logHealth() {
	h := d.Health()
	d.log.Info("dispatcher health", "running", h.Running, "offering", h.Offering, "waiting", h.Waiting, "noWorker", h.NoWorker,
		"oldestWait", h.OldestWait, "droppedAnswers", h.DroppedAnswers)
	if h.JobAdmission != "" {
		d.log.Warn(jobRefusedHint, "err", h.JobAdmission)
	}
	if h.StopStream != "" {
		d.log.Warn("the stop stream can't be read; a stop counts only if heard live", "err", h.StopStream)
	}
	var overCap, noWorker, held, failing []string
	for _, durable := range h.Stalled {
		switch h.Waiting[durable] {
		case h.NoWorker[durable]:
			noWorker = append(noWorker, durable)
		case h.Breaker[durable]:
			held = append(held, durable)
		case h.HandOffFailed[durable]:
			failing = append(failing, durable)
		default:
			overCap = append(overCap, durable)
		}
	}
	if len(overCap) > 0 {
		d.log.Warn("admission stalled: submissions waiting over a cap with no admission; a cross-chain cycle holds until the parents' deadlines",
			"durables", overCap, "stallAfter", d.cfg.StallAfter)
	}
	if len(noWorker) > 0 {
		d.log.Warn("admission stalled: no ready worker took a submission; check the pool's pods",
			"durables", noWorker, "stallAfter", d.cfg.StallAfter)
	}
	if len(failing) > 0 {
		d.log.Warn("admission stalled: every waiting submission's hand-off keeps failing (the lease can't be written, or an unattended task's Job can't be launched or read); check the bus and the lease bucket, or the Job (quota, RBAC, admission policy)",
			"durables", failing, "stallAfter", d.cfg.StallAfter)
	}
	if len(held) > 0 {
		d.log.Warn("admission stalled: a breaker holds every waiting submission", "durables", held, "stallAfter", d.cfg.StallAfter)
	}
	if len(h.OpenBreakers) > 0 || len(h.HalfOpenBreakers) > 0 {
		d.log.Warn("breakers holding new submissions queued where tasks kept losing their workers",
			"open", h.OpenBreakers, "halfOpen", h.HalfOpenBreakers)
	}
}

// Ready reports whether the dispatcher is serving: Run holds its accept and
// lease subscriptions, and every durable the current config derives is bound.
// When it isn't, the string says what is missing. A durable the operator
// hasn't created yet, or one the grants don't cover, keeps it unready, so a
// config the dispatcher can't fully serve shows on the pod and not only in
// the log.
func (d *Dispatcher) Ready() (bool, string) {
	if !d.running.Load() {
		return false, "not serving yet"
	}
	cfg := d.src.Config()
	d.mu.Lock()
	defer d.mu.Unlock()
	var unbound []string
	for _, p := range cfg.Profiles {
		for _, dur := range p.DerivedDurables() {
			if d.subs[dur.Name] == nil {
				unbound = append(unbound, dur.Name)
			}
		}
	}
	if len(unbound) > 0 {
		sort.Strings(unbound)
		return false, "durables not bound yet: " + strings.Join(unbound, ", ")
	}
	return true, ""
}
