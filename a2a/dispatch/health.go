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
}

func (d *Dispatcher) noteWaiting(taskID, durable string, queued time.Time, why string) {
	now := d.now()
	if queued.IsZero() {
		queued = now
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	w, ok := d.waiting[taskID]
	if !ok {
		w = waiting{durable: durable, queued: queued}
	}
	w.lastSeen, w.why = now, why
	d.waiting[taskID] = w
}

// offeringWaiting is a reserved submission's waiting entry as its offer
// round starts. Waiting over a cap ends there: the slot is taken. Waiting
// for a worker doesn't: until a pod accepts, the submission still waits for
// one, and the health line keeps saying so through the round.
func (d *Dispatcher) offeringWaiting(taskID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if w, ok := d.waiting[taskID]; ok && w.why != ReasonNoWorker {
		delete(d.waiting, taskID)
	}
}

func (d *Dispatcher) clearWaiting(taskID string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	delete(d.waiting, taskID)
}

// Health reports the current state.
func (d *Dispatcher) Health() Health {
	now := d.now()
	d.mu.Lock()
	defer d.mu.Unlock()
	h := Health{Running: map[string]int{}, Offering: map[string]int{}, Waiting: map[string]int{}, NoWorker: map[string]int{},
		OldestWait: map[string]time.Duration{}, DroppedAnswers: d.droppedAnswers.Load()}
	// offering is the tasks out on offer, by durable: a waiting entry for
	// one is not forgotten while its offer round runs, however long the
	// accept timeouts make it.
	offering := map[string]map[string]bool{}
	for _, a := range d.active {
		if a.confirmed {
			h.Running[a.profile]++
			continue
		}
		h.Offering[a.profile]++
		durable := lib.ProfileDurableName(a.profile, a.tier, a.kind)
		if offering[durable] == nil {
			offering[durable] = map[string]bool{}
		}
		offering[durable][a.taskID] = true
	}
	forget := time.Duration(waitingForgetAfter) * d.cfg.RetryDelay
	for id, w := range d.waiting {
		if now.Sub(w.lastSeen) > forget && !offering[w.durable][id] {
			delete(d.waiting, id)
			continue
		}
		h.Waiting[w.durable]++
		if w.why == ReasonNoWorker {
			h.NoWorker[w.durable]++
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
	return h
}

func (d *Dispatcher) logHealth() {
	h := d.Health()
	d.log.Info("dispatcher health", "running", h.Running, "offering", h.Offering, "waiting", h.Waiting, "noWorker", h.NoWorker,
		"oldestWait", h.OldestWait, "droppedAnswers", h.DroppedAnswers)
	var overCap, noWorker []string
	for _, durable := range h.Stalled {
		if h.NoWorker[durable] == h.Waiting[durable] {
			noWorker = append(noWorker, durable)
		} else {
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
}

// Ready reports whether the dispatcher is serving: Run holds its accept and
// lease subscriptions, and every durable the current config lists is bound.
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
		for _, dur := range p.Durables {
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
