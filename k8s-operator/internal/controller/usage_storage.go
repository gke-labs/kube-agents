// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The data volume's usage on the CR: status.storageStatus's byte fields and
// the DataVolumeNearlyFull condition, from the reading the usage poller takes
// off the gateway pod's event watcher. A full volume fails every kanban and
// session write with a disk I/O error and nothing else says so, which is why
// the condition exists; it does not touch Ready, since the agent still answers.
const (
	dataVolumeNearlyFullCondition = "DataVolumeNearlyFull"
	// The condition turns True at or above dataVolumeFullPercent used and back
	// to False only below dataVolumeClearPercent, so a volume sitting at the
	// line does not flap.
	dataVolumeFullPercent  = 90
	dataVolumeClearPercent = 85
	// dataVolumeWriteStepDivisor sets how far free space moves before the byte
	// fields are rewritten: a hundredth of the volume. Free space changes on
	// every poll of a working agent, and every status write wakes the
	// reconciler, so the fields are a recent reading rather than the latest.
	dataVolumeWriteStepDivisor = 100

	dataVolumeReasonNearlyFull = "NearlyFull"
	dataVolumeReasonHasSpace   = "HasSpace"

	dataVolumeNearlyFullHint = "Remove unused cluster profiles, or ask an admin to raise spec.storage.size on the PlatformAgent."

	// dataVolumeNewerField keys the byte fields' pruning record: a served CRD
	// from before this release prunes them, and without the record every poll
	// would see them missing and write them again. The condition is in every
	// schema and keeps landing.
	dataVolumeNewerField = "storageStatus.capacityBytes and storageStatus.freeBytes"
)

// usageDataVolumeForPoll is the data volume reading this poll projects, and
// whether it projects one at all, on the same terms as the cluster gauges
// (usageClusterGaugesForPoll): known false while the watcher is on and no
// gateway replica has been read for less than the event streak; known true
// with a nil reading when the watcher is off, down, or was read without a
// volume reading. The replicas share one volume, so of several readings the
// one with the least free space is kept.
func usageDataVolumeForPoll(agent *agentv1alpha1.PlatformAgent, scraped []usageScrapedPod, gatewayRead, watcherDown bool) (reading *usageDataVolume, known bool) {
	if !eventWatcherEnabled(agent) || watcherDown {
		return nil, true
	}
	if !gatewayRead {
		return nil, false
	}
	for _, s := range scraped {
		if s.DataVolume == nil || s.DataVolume.Total <= 0 {
			continue
		}
		if reading == nil || s.DataVolume.Free < reading.Free {
			reading = s.DataVolume
		}
	}
	return reading, true
}

// dataVolumeUsedPercent is how full the volume is, rounded down. In floating
// point, because (Total-Free)*100 overflows int64 on a large enough volume.
func dataVolumeUsedPercent(v *usageDataVolume) int64 {
	return int64(float64(v.Total-v.Free) * 100 / float64(v.Total))
}

// dataVolumeCondition is the condition a reading calls for, given the one the
// CR carries. nil means remove it: with no current reading the condition is
// absent, as the byte fields and the cluster gauges are, rather than held at
// a value the operator can no longer see.
func dataVolumeCondition(agent *agentv1alpha1.PlatformAgent, reading *usageDataVolume) *metav1.Condition {
	if reading == nil {
		return nil
	}
	cond := &metav1.Condition{Type: dataVolumeNearlyFullCondition, ObservedGeneration: agent.Generation}
	used := dataVolumeUsedPercent(reading)
	wasFull := meta.IsStatusConditionTrue(agent.Status.Conditions, dataVolumeNearlyFullCondition)
	full := used >= dataVolumeFullPercent || (wasFull && used >= dataVolumeClearPercent)
	usage := fmt.Sprintf("The agent's data volume is %d%% full (%.1f of %.1f GiB used).",
		used, float64(reading.Total-reading.Free)/float64(bytesPerGiB), float64(reading.Total)/float64(bytesPerGiB))
	if full {
		cond.Status, cond.Reason, cond.Message = metav1.ConditionTrue, dataVolumeReasonNearlyFull, usage+" "+dataVolumeNearlyFullHint
	} else {
		cond.Status, cond.Reason, cond.Message = metav1.ConditionFalse, dataVolumeReasonHasSpace, usage
	}
	return cond
}

// dataVolumeFieldsMoved reports whether the byte fields should be rewritten:
// a reading appeared or went, the size changed, or free space moved by at
// least the write step.
func dataVolumeFieldsMoved(status agentv1alpha1.StorageStatus, reading *usageDataVolume) bool {
	if reading == nil {
		return status.CapacityBytes != nil || status.FreeBytes != nil
	}
	if status.CapacityBytes == nil || status.FreeBytes == nil || *status.CapacityBytes != reading.Total {
		return true
	}
	delta := *status.FreeBytes - reading.Free
	if delta < 0 {
		delta = -delta
	}
	return delta >= max(reading.Total/dataVolumeWriteStepDivisor, 1)
}

// projectDataVolume writes this poll's data volume reading to the CR: the byte
// fields when they moved, and the condition when its status or reason changed.
// The message is left as written while the status holds, so a volume filling
// slowly does not rewrite the condition every poll; the byte fields carry the
// current figures. The patch carries the resource version, because it replaces
// the conditions list whole and the reconciler writes other conditions in it;
// a conflict is left for the next poll rather than reported as a failure.
func (p *UsageCounterPoller) projectDataVolume(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reading *usageDataVolume, known bool) error {
	if !known {
		return nil
	}
	base := agent.DeepCopy()
	changed := false
	wroteBytes := false
	if !p.newerFieldPruned(agent, dataVolumeNewerField) && dataVolumeFieldsMoved(agent.Status.StorageStatus, reading) {
		if reading == nil {
			agent.Status.StorageStatus.CapacityBytes, agent.Status.StorageStatus.FreeBytes = nil, nil
		} else {
			agent.Status.StorageStatus.CapacityBytes, agent.Status.StorageStatus.FreeBytes = ptr.To(reading.Total), ptr.To(reading.Free)
			wroteBytes = true
		}
		changed = true
	}
	want := dataVolumeCondition(agent, reading)
	existing := meta.FindStatusCondition(agent.Status.Conditions, dataVolumeNearlyFullCondition)
	switch {
	case want == nil:
		changed = meta.RemoveStatusCondition(&agent.Status.Conditions, dataVolumeNearlyFullCondition) || changed
	case existing == nil || existing.Status != want.Status || existing.Reason != want.Reason:
		meta.SetStatusCondition(&agent.Status.Conditions, *want)
		changed = true
	}
	if !changed {
		return nil
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		if apierrors.IsConflict(err) {
			logf.FromContext(ctx).WithName(usagePollerLogName).Info("the CR changed under the data volume status write; it is retried next poll",
				"platformagent", client.ObjectKeyFromObject(agent).String())
			return nil
		}
		return fmt.Errorf("patching status.storageStatus and the %s condition: %w", dataVolumeNearlyFullCondition, err)
	}
	if wroteBytes {
		p.noteNewerFieldEcho(ctx, agent, dataVolumeNewerField, agent.Status.StorageStatus.CapacityBytes != nil)
	}
	return nil
}
