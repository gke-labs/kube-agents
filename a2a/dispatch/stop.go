package dispatch

import (
	"context"
	"encoding/json"
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
	if cfg.StopGraceSeconds > 0 {
		return seconds(cfg.StopGraceSeconds)
	}
	return defaultStopGrace
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
// config. Its grant is per profile, so a profile added later is subscribed
// once the connection has its grant (a regrant reconnects); until then the
// subscribe fails and is retried on the next refresh.
func (d *Dispatcher) syncStops() {
	want := map[string]bool{}
	for _, p := range d.src.Config().Profiles {
		want[p.Name] = true
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for profile, sub := range d.stopSubs {
		if !want[profile] {
			sub.Stop()
			delete(d.stopSubs, profile)
		}
	}
	for profile := range want {
		if _, ok := d.stopSubs[profile]; ok {
			continue
		}
		profile := profile
		sub, err := d.bus.SubscribeCore(lib.DispatchStopPrefix+profile+".*", func(m *nats.Msg) { d.onStop(profile, m) })
		if err != nil {
			d.log.Warn("subscribe to a profile's stops; retrying", "profile", profile, "err", err)
			continue
		}
		d.stopSubs[profile] = sub
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
	for key, at := range d.stopped {
		if now.Sub(at) > stoppedMemory {
			delete(d.stopped, key)
		}
	}
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
	d.clearWaiting(hk.taskID)
	d.endUnstarted(ctx, log, del, hk, lib.StateCanceled, &refusal{ReasonStopped, "stopped by the conversation before it started"})
}

// stopDeadline is when a running task whose stop came at now is ended for
// its executor if it hasn't ended itself.
func stopDeadline(cfg lib.DispatchConfig, now time.Time) time.Time { return now.Add(stopGrace(cfg)) }

// stopHeld ends a running task whose executor didn't stop within the grace:
// the worker is told to abandon it (a warm one resets), and the task ends
// canceled, tombstoned and acked; its Job, if it has one, goes after hold.
func (d *Dispatcher) stopHeld(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding, key string, rec lib.LeaseRecord) {
	log.Info("the task didn't stop within the grace; abandoning it", "pod", rec.Pod)
	// Ended by its requester, not lost by its worker: a half-open breaker
	// closes as on a finish.
	d.breakers.finished(h.profile, h.tier)
	_ = d.send(lib.DispatchSubject(h.profile, h.tier, rec.Pod), lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: h.taskID, Reason: abandonStopped})
	d.failAs(ctx, log, del, h, key, rec, lib.StateCanceled, ReasonStopped,
		fmt.Sprintf("stopped by the conversation; the worker %s didn't stop within %s", rec.Pod, stopGrace(d.src.Config())))
}
