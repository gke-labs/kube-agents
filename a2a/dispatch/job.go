package dispatch

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// defaultJobStart is how long a task's Job may take to have a Ready pod when
// the dispatch config leaves jobStartSeconds at 0.
const defaultJobStart = 5 * time.Minute

// jobPoll is how often the dispatcher looks for a starting Job's pod.
const jobPoll = time.Second

// JobRunner starts and removes the Job that runs one unattended task (Job
// mode, #3655 PR 6). The Job's pod runs the profile worker in its one-task
// mode, and the dispatcher hands it the task as it does a warm worker: offer,
// accept, confirm, renewals. Every method is keyed by the task, so a Job is
// found again after a dispatcher restart.
type JobRunner interface {
	// Launch creates the task's Job, or finds the one already created.
	Launch(ctx context.Context, profile, tier, taskID string) error
	// Pod is the Job's pod once it is Ready and "" before. failed is a Job
	// that will never have a Ready pod (its pod failed, or it is gone).
	Pod(ctx context.Context, profile, tier, taskID string) (pod string, failed bool, err error)
	// Delete removes the Job and its pod; a Job already gone is no error.
	Delete(ctx context.Context, profile, tier, taskID string) error
}

// UseJobs sets the JobRunner unattended tasks run on. Set it before Run.
// Without one, unattended tasks go to the warm pool as interactive ones do.
func (d *Dispatcher) UseJobs(j JobRunner) { d.jobs = j }

func jobStart(cfg lib.DispatchConfig) time.Duration {
	if cfg.JobStartSeconds > 0 {
		return seconds(cfg.JobStartSeconds)
	}
	return defaultJobStart
}

// runJob hands h's task to a Job of its own: it launches the Job, waits up
// to jobStartSeconds for its pod to be Ready, and offers the task to that pod
// once. It returns what handOff does, the lease record and key of a task
// confirmed to the pod, or an error for a submission that stays queued. ended
// is a task this call has already finished: a Job that never came up, or
// whose pod didn't take the task, fails no-worker (admitted, so failed, not
// rejected), its Job deleted, the submission tombstoned and acked and its
// slot released (fail), so a replay never launches it again; it counts
// against the breaker as an expiry does.
func (d *Dispatcher) runJob(ctx context.Context, log *slog.Logger, del lib.Delivery, snap lib.DispatchConfig,
	prof lib.DispatchProfile, h *holding) (rec lib.LeaseRecord, key string, ended bool, err error) {
	noWorker := func(detail string) (lib.LeaseRecord, string, bool, error) {
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.taskID); err != nil {
			log.Warn("delete the task's Job", "err", err)
		}
		d.breakers.expired(snap, h.profile, h.tier, d.now())
		// No worker ever held it: tombstoned under the refused-pod key.
		key := lib.ProfileLeaseKey(h.profile, h.tier, lib.ProfileLeaseRefusedPod, h.taskID)
		d.fail(ctx, log, del, h, key, lib.LeaseRecord{TaskID: h.taskID, Pod: lib.ProfileLeaseRefusedPod, Kind: h.kind}, ReasonNoWorker, detail)
		return lib.LeaseRecord{}, "", true, nil
	}
	envelope, err := json.Marshal(del.Envelope())
	if err != nil {
		return lib.LeaseRecord{}, "", false, err
	}
	h.lease, h.renew = seconds(snap.LeaseSeconds), seconds(snap.RenewSeconds)
	if err := d.jobs.Launch(ctx, h.profile, h.tier, h.taskID); err != nil {
		return lib.LeaseRecord{}, "", false, fmt.Errorf("launch the task's Job: %w", err)
	}
	limit := jobStart(snap)
	until := d.now().Add(limit)
	var pod string
	stoppedJob := func() (lib.LeaseRecord, string, bool, error) {
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.taskID); err != nil {
			log.Warn("delete the stopped task's Job", "err", err)
		}
		return lib.LeaseRecord{}, "", false, errStopped
	}
	for {
		d.mu.Lock()
		stopped := h.stopRequested
		d.mu.Unlock()
		if stopped {
			return stoppedJob()
		}
		p, failed, err := d.jobs.Pod(ctx, h.profile, h.tier, h.taskID)
		switch {
		case err != nil:
			log.Warn("look for the task's Job pod", "err", err)
		case failed:
			return noWorker("the task's Job failed before its worker was ready")
		case p != "":
			pod = p
		}
		if pod != "" {
			break
		}
		if !d.now().Before(until) {
			return noWorker(fmt.Sprintf("the task's Job had no ready worker within %s", limit))
		}
		if !sleepCtx(ctx, jobPoll) {
			// Stopping: nothing was confirmed, so the next instance
			// dispatches the task afresh and finds this Job by its task.
			return lib.LeaseRecord{}, "", false, ctx.Err()
		}
	}
	d.mu.Lock()
	ch := d.registerOfferLocked(h, pod)
	d.mu.Unlock()
	rec, key, taken, err := d.offerTo(ctx, log, envelope, snap, prof, h, pod, ch, seconds(snap.AcceptTimeoutSeconds))
	if err != nil {
		return lib.LeaseRecord{}, "", false, err
	}
	if taken == offerStopped {
		return stoppedJob()
	}
	if taken != offerTaken {
		return noWorker(fmt.Sprintf("the task's Job worker %s did not take the task", pod))
	}
	return rec, key, false, nil
}

// endJob deletes an unattended task's Job once the task is over. hold
// returns either after the task's outcome is settled or because the
// dispatcher is stopping; only the first ends the Job, since a restarted
// dispatcher re-adopts a live task and needs its Job.
func (d *Dispatcher) endJob(ctx context.Context, log *slog.Logger, h *holding) {
	if !h.unattended || d.jobs == nil || ctx.Err() != nil {
		return
	}
	if err := d.jobs.Delete(ctx, h.profile, h.tier, h.taskID); err != nil {
		log.Warn("delete the finished task's Job", "err", err)
	}
}
