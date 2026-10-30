package dispatch

import (
	"sort"
	"strings"
	"time"
)

// waitingForgetAfter is how many retry delays a waiting entry outlives its
// last redelivery before the health line forgets it (the task was settled
// some other way, or its durable is gone).
const waitingForgetAfter = 3

// Health is the dispatcher's state at one moment, as the health line logs
// it.
type Health struct {
	// Running is every held task, by profile.
	Running map[string]int
	// Waiting is the submissions over a cap, by durable.
	Waiting map[string]int
	// OldestWait is the longest a waiting submission has been queued, by
	// durable.
	OldestWait map[string]time.Duration
	// Stalled are the durables with a submission waiting longer than
	// StallAfter and no admission from them in that time: what a
	// cross-chain admission cycle looks like until the deadlines end it.
	Stalled []string
	// DroppedAnswers counts worker answers for tasks they don't hold that
	// were dropped over a pod's budget, since start.
	DroppedAnswers int64
	// OpenBreakers are the (profile, tier) pairs, as profile/tier, whose
	// breaker is refusing new submissions (breaker.go).
	OpenBreakers []string
	// StopStream is why the dispatcher can't read the stop stream, or ""
	// (stop.go): while it is set, a stop counts only if heard live.
	StopStream string
	// JobAdmission is the last refusal of a Job by the dispatcher Job
	// admission policy, or "" (job.go).
	JobAdmission string
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
	h := Health{Running: map[string]int{}, Waiting: map[string]int{}, OldestWait: map[string]time.Duration{},
		DroppedAnswers: d.droppedAnswers.Load()}
	for _, a := range d.active {
		h.Running[a.profile]++
	}
	forget := time.Duration(waitingForgetAfter) * d.cfg.RetryDelay
	for id, w := range d.waiting {
		if now.Sub(w.lastSeen) > forget {
			delete(d.waiting, id)
			continue
		}
		h.Waiting[w.durable]++
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
	h.OpenBreakers = d.breakers.open(now)
	h.StopStream = d.stopStreamErr
	h.JobAdmission = d.jobAdmissionErr
	return h
}

func (d *Dispatcher) logHealth() {
	h := d.Health()
	d.log.Info("dispatcher health", "running", h.Running, "waiting", h.Waiting, "oldestWait", h.OldestWait,
		"droppedAnswers", h.DroppedAnswers)
	if h.JobAdmission != "" {
		d.log.Warn(jobRefusedHint, "err", h.JobAdmission)
	}
	if h.StopStream != "" {
		d.log.Warn("the stop stream can't be read; a stop counts only if heard live", "err", h.StopStream)
	}
	if len(h.Stalled) > 0 {
		d.log.Warn("admission stalled: submissions waiting over a cap with no admission; a cross-chain cycle holds until the parents' deadlines",
			"durables", h.Stalled, "stallAfter", d.cfg.StallAfter)
	}
	if len(h.OpenBreakers) > 0 {
		d.log.Warn("breakers open: refusing new submissions where tasks kept losing their workers", "open", h.OpenBreakers)
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
