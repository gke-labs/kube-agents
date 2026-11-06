package dispatch

import (
	"context"
	"encoding/json"
	"errors"
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
	// Launch creates the task's Job, or finds the one already created, and
	// says when it was created (zero if it can't tell): the Job's own
	// deadline counts from then.
	Launch(ctx context.Context, profile, tier, taskID string) (created time.Time, err error)
	// Pod is the Job's pod once it is Ready and "" before. failed is a Job
	// that will never have a Ready pod (its pod failed, or it is gone).
	Pod(ctx context.Context, profile, tier, taskID string) (pod string, failed bool, err error)
	// Delete removes the Job and its pod; a Job already gone is no error.
	Delete(ctx context.Context, profile, tier, taskID string) error
}

// UseJobs sets the JobRunner unattended tasks run on. Set it before Run.
// Without one, unattended tasks go to their tier's pool as interactive ones
// do, and the operator builds pools for the read tier only, so admission
// refuses them no-worker (noRunner).
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
// rejected), its Job deleted, the submission tombstoned before the terminal
// (as a refusal is: no worker ever held it, so there is no lease record a
// redelivery would adopt), then terminated and its slot released, so a
// replay never launches it again; it counts against the breaker as an
// expiry does. The start window counts from the Job's creation, so a Job
// found again on a later pass never gets a confirm its own deadline would
// cut short.
func (d *Dispatcher) runJob(ctx context.Context, log *slog.Logger, del lib.Delivery, snap lib.DispatchConfig,
	prof lib.DispatchProfile, h *holding) (rec lib.LeaseRecord, key string, ended bool, err error) {
	noWorker := func(detail string) (lib.LeaseRecord, string, bool, error) {
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.taskID); err != nil {
			log.Warn("delete the task's Job", "err", err)
		}
		d.breakers.expired(snap, h.profile, h.tier, d.now())
		d.endUnstarted(ctx, log, del, h.key(), lib.StateFailed, &refusal{ReasonNoWorker, detail})
		d.release(h, ctx.Err() == nil)
		return lib.LeaseRecord{}, "", true, nil
	}
	envelope, err := json.Marshal(del.Envelope())
	if err != nil {
		return lib.LeaseRecord{}, "", false, err
	}
	h.lease, h.renew = seconds(snap.LeaseSeconds), seconds(snap.RenewSeconds)
	stoppedJob := func() (lib.LeaseRecord, string, bool, error) {
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.taskID); err != nil {
			log.Warn("delete the stopped task's Job", "err", err)
		}
		return lib.LeaseRecord{}, "", false, errStopped
	}
	// A stop already heard launches nothing (a Job a restart left goes).
	d.mu.Lock()
	stopped := h.stopRequested
	d.mu.Unlock()
	if stopped {
		return stoppedJob()
	}
	created, err := d.jobs.Launch(ctx, h.profile, h.tier, h.taskID)
	if err != nil {
		if errors.Is(err, ErrJobRefusedByPolicy) {
			d.jobRefused(log, err)
		}
		return lib.LeaseRecord{}, "", false, fmt.Errorf("launch the task's Job: %w", err)
	}
	d.jobAdmitted()
	limit := jobStart(snap)
	if created.IsZero() {
		created = d.now()
	}
	until := created.Add(limit)
	var pod string
	var pollErr error
	for {
		d.mu.Lock()
		stopped := h.stopRequested
		d.mu.Unlock()
		if stopped {
			return stoppedJob()
		}
		p, failed, err := d.jobs.Pod(ctx, h.profile, h.tier, h.taskID)
		pollErr = err
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
			if pollErr != nil {
				// The Job couldn't be read, not seen failing: no worker's
				// fault, so no breaker count. The submission stays queued,
				// and the next pass finds the Job again by its task.
				return lib.LeaseRecord{}, "", false, fmt.Errorf("the task's Job could not be read at its start limit: %w", pollErr)
			}
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
	switch taken {
	case offerStopped:
		return stoppedJob()
	case offerUnsent:
		// Not the worker's failure: the submission stays queued, and the
		// next pass finds this Job by its task.
		return lib.LeaseRecord{}, "", false, fmt.Errorf("the offer to the task's Job worker %s was not sent", pod)
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
	if !h.onJob || d.jobs == nil || ctx.Err() != nil {
		return
	}
	if err := d.jobs.Delete(ctx, h.profile, h.tier, h.taskID); err != nil {
		log.Warn("delete the finished task's Job", "err", err)
	}
}

// dropUnstartedJob deletes the Job of a task that ends at admission (stopped,
// or refused) without ever being confirmed: one launched before a dispatcher
// restart, whose pod would otherwise wait for an offer until its deadline,
// outside every cap. Not held to the profile's current Job template: the
// operator drops it (a revoke, a tier removed) while a Job an earlier pass
// launched may still be there, and the Job's name follows from the task. A
// Job never launched, or already gone, is no error.
func (d *Dispatcher) dropUnstartedJob(ctx context.Context, log *slog.Logger, hk holdKey) {
	if d.jobs == nil || !d.cfg.unattended(hk.tier) {
		return
	}
	if err := d.jobs.Delete(ctx, hk.profile, hk.tier, hk.taskID); err != nil {
		log.Warn("delete the Job of a task that ended at admission", "err", err)
	}
}

// jobRefusedHint is what the dispatcher says when the Job admission policy
// refuses a Job built from the operator's own template.
const jobRefusedHint = "the Job admission policy refused a Job built from the operator's template: the policy's DispatchJobTemplate params and the dispatch config disagree (an operator pass may be under way, or a param was edited or removed), or a mutating webhook changed the Job (check the operator's events). The task stays queued."

// jobRefused records a Job the admission policy refused: reported in Health
// and logged once as an error until a Job is admitted.
func (d *Dispatcher) jobRefused(log *slog.Logger, err error) {
	d.mu.Lock()
	first := d.jobAdmissionErr == ""
	d.jobAdmissionErr = err.Error()
	d.mu.Unlock()
	if first {
		log.Error(jobRefusedHint, "err", err)
	}
}

func (d *Dispatcher) jobAdmitted() {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.jobAdmissionErr = ""
}
