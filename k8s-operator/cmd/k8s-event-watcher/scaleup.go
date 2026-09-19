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

package main

import (
	"sync"
	"time"
)

const (
	// reasonFailedScheduling is the scheduler's event for a pod it could not
	// place. It is the scheduler's normal output during a scale-up as much as
	// the first sign of a stuck pod, which is why it has a gate of its own.
	reasonFailedScheduling = "FailedScheduling"
	// reasonTriggeredScaleUp is cluster-autoscaler's event on a pod it has
	// decided to add a node for. While one is live the pod is waiting for
	// capacity that is on its way, not stuck.
	reasonTriggeredScaleUp = "TriggeredScaleUp"
	// reasonNotTriggerScaleUp is cluster-autoscaler's event on a pod it has
	// decided it cannot help ("max node group size reached", "pod didn't
	// trigger scale-up"). It is the point at which an unschedulable pod stops
	// being a scale-up in progress and becomes an incident.
	reasonNotTriggerScaleUp = "NotTriggerScaleUp"
	// scaleUpReporter is the component name cluster-autoscaler records its
	// events under (source.component on the legacy recorder, which client-go
	// copies to reportingController as well). A TriggeredScaleUp or
	// NotTriggerScaleUp from any other reporter is not a verdict: the reason
	// alone would let any controller that reused the two names, on purpose
	// or by accident, hold or release a pod's FailedScheduling. The check
	// authenticates nothing — a principal that can create events in a
	// namespace writes the reporter field too — so it narrows the marks to
	// what says it is the autoscaler's; who may write events there is the
	// cluster's RBAC and admission policy, not this binary's.
	scaleUpReporter = "cluster-autoscaler"

	// defaultScaleUpTTL bounds how long a verdict is remembered when the
	// caller passes none. The dispatcher passes scaleUpMemoTTL, the dedup
	// window with a floor; this is what a caller that passes nothing gets,
	// and the deployed install's dedup window.
	defaultScaleUpTTL = 24 * time.Hour
	// defaultScaleUpEntries caps the memo per cluster. The same bound as
	// pullClassMemo: pending pods are a small fraction of a cluster's pods,
	// and a cluster churning through them must not grow the map without limit.
	defaultScaleUpEntries = 4096
)

// scaleUpMemoTTL is how long a dispatcher remembers a pod's marks: the dedup
// window, past which the pod's next FailedScheduling
// is a new incident anyway, but never less than the hold plus the staleness
// check. The floor is what the hold needs. A TriggeredScaleUp holds any
// FailedScheduling sighted within scaleUpHold of it, and such an event is
// still judged rather than held as stale for failedSchedulingStaleAfter after
// that sighting, so a mark can be consulted up to the sum of the two after it
// was recorded. Bounded by the dedup window alone, the binary's own defaults
// (a 5m window, a 15m hold) would drop a mark five minutes after the
// scale-up triggered and the next FailedScheduling would read as having no
// verdict at all, with nothing in the log saying the hold had been cut short.
func scaleUpMemoTTL(dedupWindow, scaleUpHold time.Duration) time.Duration {
	floor := scaleUpHold + failedSchedulingStaleAfter
	if dedupWindow > floor {
		return dedupWindow
	}
	return floor
}

// scaleUpVerdict is cluster-autoscaler's most recent ruling on a pod, read off
// the events it records against the pod itself.
type scaleUpVerdict int

const (
	// scaleUpNone means the autoscaler has said nothing about the pod: the
	// cluster has no autoscaler, or it has not evaluated the pod yet.
	scaleUpNone scaleUpVerdict = iota
	// scaleUpTriggered means a node is being provisioned for the pod.
	scaleUpTriggered
	// scaleUpDeclined means the autoscaler will not add a node that helps.
	scaleUpDeclined
)

func (v scaleUpVerdict) String() string {
	switch v {
	case scaleUpTriggered:
		return "triggered"
	case scaleUpDeclined:
		return "declined"
	default:
		return "none"
	}
}

// scaleUpVerdictFor maps the two autoscaler reasons onto a verdict, and
// everything else onto scaleUpNone.
func scaleUpVerdictFor(reason string) scaleUpVerdict {
	switch reason {
	case reasonTriggeredScaleUp:
		return scaleUpTriggered
	case reasonNotTriggerScaleUp:
		return scaleUpDeclined
	default:
		return scaleUpNone
	}
}

// scaleUpMark is one remembered verdict. At is the event's own timestamp, not
// the time the watcher saw it: an informer relist after a restart replays every
// event still inside the API server's TTL, and a TriggeredScaleUp from forty
// minutes ago must not read as a scale-up in progress for a pod that is
// pending now.
type scaleUpMark struct {
	Verdict scaleUpVerdict
	At      time.Time
}

// scaleUpMemo remembers, per involved-object UID, the latest verdict
// cluster-autoscaler recorded against the pod. It exists because the verdict
// and the FailedScheduling it qualifies are two different events, and the
// dedup key is (UID, Reason), so nothing downstream would correlate them.
//
// Latest is by event time, not arrival order, so a replayed older mark cannot
// overwrite a newer one. A later TriggeredScaleUp supersedes a NotTriggerScaleUp
// (the autoscaler changed its mind, a node group was resized) and the reverse
// supersedes too (the scale-up it started did not help). The map, the expiry
// and the eviction are boundedEntries (memo.go), shared with pullClassMemo;
// an entry here is dated by the event, so both age from the mark's At.
type scaleUpMemo struct {
	mu      sync.Mutex
	entries boundedEntries[scaleUpVerdict]
	now     func() time.Time
}

func newScaleUpMemo(ttl time.Duration, max int) *scaleUpMemo {
	if ttl <= 0 {
		ttl = defaultScaleUpTTL
	}
	if max <= 0 {
		max = defaultScaleUpEntries
	}
	return &scaleUpMemo{entries: newBoundedEntries[scaleUpVerdict](ttl, max)}
}

func (m *scaleUpMemo) clock() time.Time {
	if m.now != nil {
		return m.now()
	}
	return time.Now()
}

// Record remembers verdict for uid as of at, unless a newer mark is already
// held. A zero at (an emitter that set no timestamp) is taken as now, which is
// the most recent reading the mark can honestly claim, and so is an at in the
// future: the hold is measured from the mark, so a TriggeredScaleUp stamped
// ahead of the watcher's clock (skew, or an author who chose the stamp) would
// otherwise hold the pod's FailedScheduling for the hold plus the lead, and
// the memo's own expiry, which ages from the same stamp, would keep it for as
// long again. Safe on a nil receiver and a no-op for an empty uid or
// scaleUpNone.
func (m *scaleUpMemo) Record(uid string, verdict scaleUpVerdict, at time.Time) {
	if m == nil || uid == "" || verdict == scaleUpNone {
		return
	}
	now := m.clock()
	if at.IsZero() || at.After(now) {
		at = now
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if _, prevAt, ok := m.entries.lookup(uid, now); ok && prevAt.After(at) {
		return
	}
	m.entries.store(uid, verdict, at, now)
}

// Lookup returns the live mark for uid, or the zero mark when none is held or
// the held one has aged past ttl. Safe on a nil receiver.
func (m *scaleUpMemo) Lookup(uid string) scaleUpMark {
	if m == nil || uid == "" {
		return scaleUpMark{}
	}
	now := m.clock()
	m.mu.Lock()
	defer m.mu.Unlock()
	verdict, at, ok := m.entries.lookup(uid, now)
	if !ok {
		return scaleUpMark{}
	}
	return scaleUpMark{Verdict: verdict, At: at}
}

// Len reports the current entry count. Test helper.
func (m *scaleUpMemo) Len() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.entries.len()
}
