package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// defaultStopGrace is how long a running task's executor has to stop on
	// its own after a stop, when the config leaves stopGraceSeconds at 0.
	defaultStopGrace = 30 * time.Second
	// stoppedMemory is how long the dispatcher remembers a stop it heard
	// live, for a submission still queued when it comes back. The stream
	// (lib.DispatchStopStream) keeps the record longer; this covers a
	// stream not yet provisioned.
	stoppedMemory = 24 * time.Hour
	// stopCheckTimeout bounds one direct read of the stop stream.
	stopCheckTimeout = 2 * time.Second
	// directGetPrefix is JetStream's direct get by subject: the last message
	// on <subject> in <stream>, with no consumer and no stream info.
	directGetPrefix = "$JS.API.DIRECT.GET."
	// abandonStopped is the abandon reason a stop sends a worker that didn't
	// stop on its own.
	abandonStopped = "stopped by the conversation"
)

func stopGrace(cfg lib.DispatchConfig) time.Duration {
	g := defaultStopGrace
	if cfg.StopGraceSeconds > 0 {
		g = seconds(cfg.StopGraceSeconds)
	}
	// Never shorter than a renewal and two ticks: hold counts a worker
	// silent for a renewal and a tick when the grace ends, so one silent
	// since before the stop is always counted, whatever renewSeconds is.
	renew := seconds(cfg.RenewSeconds)
	tick := max(renew/ticksPerRenewal, minTick)
	return max(g, renew+2*tick)
}

// stopFor reads a stop record (contract addendum 9): the task it stops,
// when the subject is a2a.stop.<profile>.<task> for profile and the body is
// a version-1 record naming the same task. Anything else stops nothing.
func stopFor(profile, subject string, data []byte) (string, bool) {
	rest, ok := strings.CutPrefix(subject, lib.DispatchStopPrefix+profile+".")
	if !ok || rest == "" || strings.Contains(rest, ".") {
		return "", false
	}
	var rec lib.DispatchStop
	if json.Unmarshal(data, &rec) != nil || rec.V != 1 || rec.TaskID != rest {
		return "", false
	}
	return rest, true
}

// syncStops keeps one live subscription to each configured profile's stop
// subject, a2a.stop.<profile>.*, and drops those of profiles gone from the
// config. The subscriptions are the fast path only: one the bus refuses
// (grants are fixed per connection) or a reconnect's gap loses a stop, and
// recheckStops finds it on the stream. The bus calls run outside d.mu, as
// in syncBindings.
func (d *Dispatcher) syncStops() {
	want := map[string]bool{}
	for _, p := range d.src.Config().Profiles {
		want[p.Name] = true
	}
	var gone []lib.Subscription
	var add []string
	d.mu.Lock()
	for profile, sub := range d.stopSubs {
		if !want[profile] {
			gone = append(gone, sub)
			delete(d.stopSubs, profile)
		}
	}
	for profile := range want {
		if _, ok := d.stopSubs[profile]; !ok {
			add = append(add, profile)
		}
	}
	d.mu.Unlock()
	for _, sub := range gone {
		sub.Stop()
	}
	for _, profile := range add {
		sub, err := d.bus.SubscribeCore(lib.DispatchStopPrefix+profile+".*", func(m *nats.Msg) { d.onStop(profile, m) })
		if err != nil {
			d.log.Warn("subscribe to a profile's stops; retrying", "profile", profile, "err", err)
			continue
		}
		d.mu.Lock()
		_, dup := d.stopSubs[profile]
		if !dup {
			d.stopSubs[profile] = sub
		}
		d.mu.Unlock()
		if dup {
			sub.Stop()
		}
	}
}

// recheckStops reads the stop stream for every task the dispatcher holds
// that has no stop yet, so a stop the live subscription missed (refused
// for want of a grant, lost across a reconnect, or published while a
// subscription was being made) still ends the task, within a refresh.
func (d *Dispatcher) recheckStops(ctx context.Context) {
	type task struct{ profile, taskID string }
	var tasks []task
	seen := map[task]bool{}
	d.mu.Lock()
	for k, h := range d.active {
		t := task{k.profile, k.taskID}
		if !h.stopRequested && !seen[t] {
			seen[t] = true
			tasks = append(tasks, t)
		}
	}
	d.mu.Unlock()
	for _, t := range tasks {
		if ctx.Err() != nil || !d.stopRequested(ctx, d.log, t.profile, t.taskID) {
			continue
		}
		d.mu.Lock()
		now := d.now()
		d.stopped[t.profile+"/"+t.taskID] = now
		for k, h := range d.active {
			if k.profile == t.profile && k.taskID == t.taskID {
				d.requestStopLocked(h)
			}
		}
		d.pruneStoppedLocked(now)
		d.mu.Unlock()
	}
}

func (d *Dispatcher) stopAllStops() {
	d.mu.Lock()
	defer d.mu.Unlock()
	for profile, sub := range d.stopSubs {
		sub.Stop()
		delete(d.stopSubs, profile)
	}
}

// onStop is a live stop record: remembered for a submission still queued,
// and passed to every holding of that task.
func (d *Dispatcher) onStop(profile string, m *nats.Msg) {
	taskID, ok := stopFor(profile, m.Subject, m.Data)
	if !ok {
		d.log.Debug("ignoring a malformed stop record", "subject", m.Subject)
		return
	}
	now := d.now()
	d.mu.Lock()
	defer d.mu.Unlock()
	d.stopped[profile+"/"+taskID] = now
	for k, h := range d.active {
		if k.profile == profile && k.taskID == taskID {
			d.requestStopLocked(h)
		}
	}
	d.pruneStoppedLocked(now)
}

// pruneStoppedLocked forgets the stops remembered longer than
// stoppedMemory. d.mu must be held.
func (d *Dispatcher) pruneStoppedLocked(now time.Time) {
	for key, at := range d.stopped {
		if now.Sub(at) > stoppedMemory {
			delete(d.stopped, key)
		}
	}
}

// stopRequestedOn reports whether h has a stop. A stopped task never closes
// the breaker, however it ends (its own end, its deadline, the grace running
// out); its worker's loss still counts as an expiry, so a stop can't hide
// the workers a poisoned input kills.
func (d *Dispatcher) stopRequestedOn(h *holding) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return h.stopRequested
}

// requestStopLocked marks h stopped and wakes its hold. d.mu must be held.
func (d *Dispatcher) requestStopLocked(h *holding) {
	h.stopRequested = true
	if h.stopc != nil {
		select {
		case h.stopc <- struct{}{}:
		default:
		}
	}
}

// stopRequested reports whether taskID of profile has a stop: one heard
// live, or one on the stop stream. A stream that can't be read counts as no
// stop, logged: a stop the dispatcher can't see must not hold every task.
func (d *Dispatcher) stopRequested(ctx context.Context, log *slog.Logger, profile, taskID string) bool {
	d.mu.Lock()
	_, heard := d.stopped[profile+"/"+taskID]
	d.mu.Unlock()
	if heard {
		return true
	}
	if d.bus == nil { // a Dispatcher not built by New, in tests
		return false
	}
	now := d.now()
	d.mu.Lock()
	backingOff := now.Before(d.stopReadRetry)
	d.mu.Unlock()
	if backingOff {
		return false
	}
	rctx, cancel := context.WithTimeout(ctx, stopCheckTimeout)
	defer cancel()
	subject := lib.DispatchStopSubject(profile, taskID)
	msg, err := d.bus.Conn().RequestWithContext(rctx, directGetPrefix+lib.DispatchStopStream+"."+subject, nil)
	switch {
	case errors.Is(err, nats.ErrConnectionClosed), errors.Is(err, nats.ErrConnectionDraining),
		errors.Is(err, nats.ErrInvalidConnection), errors.Is(err, nats.ErrConnectionReconnecting):
		// The connection, not the stream: mid-reconnect or regrant. No
		// backoff and no report; the next admission or refresh reads again.
		log.Debug("the stop stream read found the connection unready; reading again next time", "err", err)
		return false
	case err != nil:
		d.stopStreamFailed(log, err.Error())
		return false
	case msg.Header.Get("Status") != "":
		// 404: no record for the task; anything else (503: no stream) is
		// an error status, read as no stop.
		if s := msg.Header.Get("Status"); s != "404" {
			d.stopStreamFailed(log, "status "+s+" "+msg.Header.Get("Description"))
			return false
		}
	default:
		got, ok := stopFor(profile, msg.Header.Get("Nats-Subject"), msg.Data)
		d.stopStreamRead()
		return ok && got == taskID
	}
	d.stopStreamRead()
	return false
}

// stopReadBackoff is how long after a failed read of the stop stream the
// dispatcher takes queued tasks as not stopped without reading it, so a
// missing stream or grant costs one stalled read a period, not one per
// admission.
const stopReadBackoff = 30 * time.Second

// stopStreamFailed records a failed read of the stop stream: reported in
// Health and logged as an error, since every stop published while it lasts
// is missed by any dispatcher that didn't hear it live.
func (d *Dispatcher) stopStreamFailed(log *slog.Logger, why string) {
	d.mu.Lock()
	first := d.stopStreamErr == ""
	d.stopStreamErr, d.stopReadRetry = why, d.now().Add(stopReadBackoff)
	d.mu.Unlock()
	if first {
		log.Error("cannot read the stop stream; queued tasks are taken as not stopped, and a stop is honoured only if heard live",
			"stream", lib.DispatchStopStream, "err", why, "retryAfter", stopReadBackoff)
	}
}

func (d *Dispatcher) stopStreamRead() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.stopStreamErr = ""
}

// stopQueued ends a submission that was stopped before any worker had it:
// canceled, tombstoned as a refusal, never run.
func (d *Dispatcher) stopQueued(ctx context.Context, log *slog.Logger, del lib.Delivery, hk holdKey) {
	log.Info("stopped before it was handed out; canceling it")
	d.clearWaiting(hk)
	d.endUnstarted(ctx, log, del, hk, lib.StateCanceled, &refusal{ReasonStopped, "stopped by the conversation before it started"})
}

// stopDeadline is when a running task whose stop came at now is ended for
// its executor if it hasn't ended itself.
func stopDeadline(cfg lib.DispatchConfig, now time.Time) time.Time { return now.Add(stopGrace(cfg)) }

// stopHeld ends a running task whose executor didn't stop within the grace:
// the task ends canceled, the worker is told to abandon it (a warm one
// resets), and it is tombstoned and acked; its Job, if it has one, goes
// after hold. The canceled terminal is written before the abandon: the
// worker's own terminal for an abandoned task says failed, and whichever
// lands first is the task's end.
func (d *Dispatcher) stopHeld(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding, key string, rec lib.LeaseRecord) {
	log.Info("the task didn't stop within the grace; abandoning it", "pod", rec.Pod)
	// Ended by its requester: the breaker doesn't close on it, so stopping
	// one's own task can't reset a breaker one's other tasks opened.
	if !d.publishTerminalRetry(ctx, log, del, h, lib.StateCanceled, ReasonStopped,
		fmt.Sprintf("stopped by the conversation; the worker %s didn't stop within %s", rec.Pod, stopGrace(d.src.Config()))) {
		return
	}
	_ = d.send(lib.DispatchSubject(h.profile, h.tier, rec.Pod), lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: h.taskID, Reason: abandonStopped})
	d.settle(ctx, log, del, h, key, rec)
}
