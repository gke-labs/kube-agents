package dispatch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
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
	Launch(ctx context.Context, profile, tier, kind, taskID string) (created time.Time, err error)
	// Pod is the Job's pod once it is Ready and "" before. failed is a Job
	// that will never have a Ready pod (its pod failed, or it is gone).
	Pod(ctx context.Context, profile, tier, kind, taskID string) (pod string, failed bool, err error)
	// Delete removes the Job and its pod; a Job already gone is no error.
	Delete(ctx context.Context, profile, tier, kind, taskID string) error
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
// is a task this call has already finished: one whose tier an administrator
// ended before it was confirmed ends canceled tier-ended (tierEndedJob), and
// otherwise a Job that never came up, or whose pod didn't take the task, fails no-worker (admitted, so failed, not
// rejected), its Job deleted, the submission tombstoned before the terminal
// (as a refusal is: no worker ever held it, so there is no lease record a
// redelivery would adopt), then terminated and its slot released, so a
// replay never launches it again; it counts against the breaker as an
// expiry does. The start window counts from the Job's creation, so a Job
// found again on a later pass never gets a confirm its own deadline would
// cut short.
func (d *Dispatcher) runJob(ctx context.Context, log *slog.Logger, del lib.Delivery, snap lib.DispatchConfig,
	prof lib.DispatchProfile, h *holding) (rec lib.LeaseRecord, key string, ended bool, err error) {
	// A tier an administrator ended (#5850) while the task's Job was still
	// coming up: the task ends canceled tier-ended, as a held one does
	// (endTierHeld), not failed no-worker when the Job never gets a worker
	// (the tier's ServiceAccount goes at the shortened end). Its Job is
	// deleted and its submission tombstoned before the terminal, as for any
	// task no worker held; the breaker doesn't count it (#6134).
	tierEndedJob := func() (lib.LeaseRecord, string, bool, error) {
		log.Info("an administrator ended this tier's drain while its Job was starting; ending the task", "tier", h.tier)
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.kind, h.taskID); err != nil {
			log.Warn("delete the task's Job", "err", err)
		}
		d.endUnstarted(ctx, log, del, h.key(), lib.StateCanceled,
			&refusal{ReasonTierEnded, fmt.Sprintf("an administrator ended tier %s's drain on profile %s", h.tier, h.profile)})
		d.release(h, ctx.Err() == nil)
		return lib.LeaseRecord{}, "", true, nil
	}
	noWorker := func(detail string) (lib.LeaseRecord, string, bool, error) {
		// A Job that failed, or a pod that didn't take the task, after
		// its tier was ended is the end's, not a worker's loss.
		if d.tierEndedOn(h) {
			return tierEndedJob()
		}
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.kind, h.taskID); err != nil {
			log.Warn("delete the task's Job", "err", err)
		}
		// The breaker guards the tier's runner. At an unattended tier that is
		// Job mode; a permit-carrying task's Job at any other tier is not that
		// tier's warm pool, so its failure doesn't hold back the pool's tasks.
		if !h.permitJob {
			d.breakers.expired(snap, h.profile, h.tier, d.now())
		}
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
		if err := d.jobs.Delete(ctx, h.profile, h.tier, h.kind, h.taskID); err != nil {
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
	if d.tierEndedOn(h) {
		return tierEndedJob()
	}
	created, err := d.jobs.Launch(ctx, h.profile, h.tier, h.kind, h.taskID)
	if err != nil {
		if errors.Is(err, ErrJobRefusedByPolicy) || errors.Is(err, ErrJobTemplateNotCurrent) {
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
		if d.tierEndedOn(h) {
			return tierEndedJob()
		}
		p, failed, err := d.jobs.Pod(ctx, h.profile, h.tier, h.kind, h.taskID)
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
	if d.podHeldLocked(h, pod) {
		// A Job's pod is the task's own (#5646): one another holding here
		// already has is never offered a second task, whose credentials
		// would then bind to a pod running someone else's.
		d.mu.Unlock()
		return noWorker(fmt.Sprintf("the task's Job worker %s already holds another task", pod))
	}
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
	if err := d.jobs.Delete(ctx, h.profile, h.tier, h.kind, h.taskID); err != nil {
		log.Warn("delete the finished task's Job", "err", err)
	}
}

// dropUnstartedJob deletes the Job of a task that ends at admission (stopped,
// or refused), or whose profile drains or leaves the config, without ever
// being confirmed: one launched before a dispatcher restart or a failed
// hand-off, whose pod would otherwise wait for an offer until its deadline,
// outside every cap. Not held to the profile's current Job template: the
// operator drops it (a revoke, a tier removed) while a Job an earlier pass
// launched may still be there, and the Job's name follows from the task. A
// Job never launched, or already gone, is no error.
func (d *Dispatcher) dropUnstartedJob(ctx context.Context, log *slog.Logger, hk holdKey) {
	if d.jobs == nil || !d.mayHaveJob(hk) {
		return
	}
	if err := d.jobs.Delete(ctx, hk.profile, hk.tier, hk.kind, hk.taskID); err != nil {
		log.Warn("delete the Job of a task never started", "err", err)
	}
}

// dropUnconfirmedJob is dropUnstartedJob for a task whose profile the config
// doesn't list, so its lease hasn't been read yet: only a task with no lease
// record (never confirmed to a worker) has its Job deleted. A lease that
// can't be read deletes nothing: unknown is not "never started".
func (d *Dispatcher) dropUnconfirmedJob(ctx context.Context, log *slog.Logger, hk holdKey) {
	if d.jobs == nil || !d.mayHaveJob(hk) {
		return
	}
	_, _, found, err := d.findLease(ctx, hk.profile, hk.tier, hk.taskID)
	if err != nil {
		log.Warn("could not read the task's lease; its Job, if any, stays", "err", err)
		return
	}
	if !found {
		d.dropUnstartedJob(ctx, log, hk)
	}
}

// podHeldLocked is whether a holding other than h has pod. Call with d.mu
// held.
func (d *Dispatcher) podHeldLocked(h *holding, pod string) bool {
	for _, other := range d.active {
		if other != h && other.pod == pod {
			return true
		}
	}
	return false
}

// jobRefusedHint is what the dispatcher says when the Job admission policy
// refuses a Job built from the operator's own template.
const jobRefusedHint = "a Job couldn't be made from the operator's template: its DispatchJobTemplate param is missing or isn't the rendering the dispatch config names (an operator pass may be under way, or a param was edited or removed), or the Job admission policy refused the Job (a param changed between the read and the create, or a mutating webhook changed the Job; check the operator's events). The task stays queued."

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

// mayHaveJob is whether hk's task could have a Job: its tier is unattended
// for its profile (Config.unattendedFor, which reads the profile's own
// unattended-mode tier, #3659), the profile's templates are per tier, so a
// permit-carrying task at any tier may have one (#5646), or the config no
// longer lists the profile, so its tier's mode can't be told and a delete,
// which is no error for a Job that was never made, is the safe answer.
//
// Under Job mode (the config carries jobLabels: some profile has a template)
// any task may have a Job, a permit-carrying one at any tier (#5646), whatever
// its profile's template says now: a reference lost since the Job was
// launched mustn't leave it. The delete is no error for a Job never made.
// Without Job mode the dispatcher holds no Jobs Role, and the operator's
// teardown removes the Jobs.
func (d *Dispatcher) mayHaveJob(hk holdKey) bool {
	if d.cfg.unattended(hk.tier) {
		return true
	}
	snap := d.src.Config()
	if len(snap.JobLabels) > 0 {
		return true
	}
	prof, ok := profileOf(snap, hk.profile)
	return !ok || d.cfg.jobPossible(prof, hk.tier)
}

// jobPossible is whether a task of prof at tier could run on a Job: the tier
// is unattended for the profile, the profile has a template per tier (a
// permit-carrying task runs on a Job at any tier, #5646), or the profile no
// longer lists the tier. A custom unattended-mode tier dropped from the
// profile leaves UnattendedTier empty, so its mode can't be told any more,
// and its Jobs must still be deleted (#3659); a delete of a Job never made
// is no error. Not held to the current template: a task's Job is found by
// its name, whatever the config says now.
func (c Config) jobPossible(prof lib.DispatchProfile, tier string) bool {
	return c.unattendedFor(prof, tier) || prof.JobTemplatesPerTier() || !slices.Contains(prof.Tiers, tier)
}
