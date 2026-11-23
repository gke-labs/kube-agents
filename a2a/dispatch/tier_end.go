package dispatch

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// An administrator ending a draining tier's running work (#5850). The
// operator marks the tier's DrainingTier Ending in the dispatch config when
// the AgentProfile carries the end-tier-drain annotation; on each refresh the
// dispatcher marks every task it holds at that (profile, tier) and wakes its
// hold, which ends it at once: canceled with ReasonTierEnded, the worker told
// to abandon it, the submission tombstoned and acked, and its Job deleted
// after the hold. A task whose Job is still starting is ended the same way
// by runJob, its Job deleted there (#6134). There is no executor grace, as a stop has: the executor
// hears a requester's stop on the stop subject, and nothing publishes one
// here. The breaker doesn't count it: an administrator's end is no worker
// loss.

// syncEndingTiers marks and wakes every held task whose tier the config says
// is ending.
func (d *Dispatcher) syncEndingTiers() {
	ending := map[string]map[string]bool{}
	for _, p := range d.src.Config().Profiles {
		for _, dt := range p.DrainingTiers {
			if dt.Ending {
				if ending[p.Name] == nil {
					ending[p.Name] = map[string]bool{}
				}
				ending[p.Name][dt.Tier] = true
			}
		}
	}
	if len(ending) == 0 {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	for k, h := range d.active {
		if h.tierEnded || !ending[k.profile][h.tier] {
			continue
		}
		h.tierEnded = true
		if h.stopc != nil {
			select {
			case h.stopc <- struct{}{}:
			default:
			}
		}
	}
}

// tierEndedOn reports whether h's tier is being ended.
func (d *Dispatcher) tierEndedOn(h *holding) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	return h.tierEnded
}

// endTierHeld ends a running task whose tier's drain an administrator ended:
// the canceled terminal first, then the abandon (the worker's own terminal
// for an abandoned task says failed, and whichever lands first is the
// task's end), then the settlement.
func (d *Dispatcher) endTierHeld(ctx context.Context, log *slog.Logger, del lib.Delivery, h *holding, key string, rec lib.LeaseRecord) {
	log.Info("an administrator ended this tier's drain; ending the task", "pod", rec.Pod, "tier", h.tier)
	if !d.publishTerminalRetry(ctx, log, del, h, lib.StateCanceled, ReasonTierEnded,
		fmt.Sprintf("an administrator ended tier %s's drain on profile %s", h.tier, h.profile)) {
		return
	}
	_ = d.send(lib.DispatchSubject(h.profile, h.tier, rec.Pod), lib.DispatchMessage{Type: lib.DispatchAbandon, TaskID: h.taskID, Reason: abandonTierEnded})
	d.settle(ctx, log, del, h, key, rec)
}
