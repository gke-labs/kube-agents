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
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

// The FailedScheduling gate's log lines are one per pod per decision, not one
// per sighting: the scheduler bumps the Event's count on every retry of every
// Pending pod, and on a fleet with many Pending pods a line per bump is tens
// of lines a second, which is what pushed the node's logging agent over its
// memory limit. The gate counters on events_filtered_total carry the
// per-sighting count; the log carries the transitions.

// fsLogFilter is a filter with the autoscaler reasons allowed (so the
// FailedScheduling gate runs its full path) and a settable clock.
func fsLogFilter(minCount int, now *time.Time) *filter {
	f := newFilter(newFilterConfig(scaleUpReasons, nil, nil, filterThresholds{
		failedSchedulingMinCount: minCount,
	}))
	f.now = func() time.Time { return *now }
	return f
}

// fsSighting is one sighting of a Pending pod's FailedScheduling, fresh at
// now, named after its UID so the log lines tell pods apart, with a span too
// short for the span gate.
func fsSighting(now time.Time, cluster, uid string, count int) TriageEvent {
	ev := failedSchedulingEvent(uid, count, now)
	ev.Cluster, ev.Name, ev.FirstSeen = cluster, uid, now.Add(-time.Second)
	return ev
}

// countLines counts the log lines carrying substr; each substring below
// occurs at most once per line.
func countLines(buf *lockedBuffer, substr string) int {
	return strings.Count(buf.String(), substr)
}

func TestFailedSchedulingGateLogsOncePerPodWhileTheDecisionHolds(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	for count := 1; count <= 50; count++ {
		if gate := f.Decide(fsSighting(now, "c1", "pod-a", count)); gate != gateFailedSchedulingMinCount {
			t.Fatalf("sighting %d: gate = %q; want %q", count, gate, gateFailedSchedulingMinCount)
		}
	}
	if got := countLines(buf, "held FailedScheduling pod=default/pod-a"); got != 1 {
		t.Errorf("50 sightings on one decision logged %d lines; want 1\n%s", got, buf.String())
	}
}

func TestFailedSchedulingGateLogsAgainWhenTheDecisionChanges(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(3, &now)
	for count := 1; count <= 2; count++ {
		f.Decide(fsSighting(now, "c1", "pod-a", count))
	}
	for count := 3; count <= 6; count++ {
		if gate := f.Decide(fsSighting(now, "c1", "pod-a", count)); gate != gateFailedSchedulingMinSpan {
			t.Fatalf("sighting %d: gate = %q; want %q", count, gate, gateFailedSchedulingMinSpan)
		}
	}
	if got := countLines(buf, "no autoscaler verdict on record)"); got != 2 {
		t.Errorf("two decisions logged %d lines; want 2 (one per decision)\n%s", got, buf.String())
	}
	if got := countLines(buf, "count=1 < 3"); got != 1 {
		t.Errorf("the count hold logged %d lines; want 1\n%s", got, buf.String())
	}
	if got := countLines(buf, "lasted 1s <"); got != 1 {
		t.Errorf("the span hold logged %d lines; want 1\n%s", got, buf.String())
	}
}

func TestFailedSchedulingGateLogsAgainAfterTheRepeatInterval(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	f.Decide(fsSighting(now, "c1", "pod-a", 1))
	// Still inside the interval: silent.
	now = now.Add(14 * time.Minute)
	f.Decide(fsSighting(now, "c1", "pod-a", 2))
	if got := countLines(buf, "held FailedScheduling pod=default/pod-a"); got != 1 {
		t.Fatalf("a sighting inside the repeat interval logged; %d lines, want 1\n%s", got, buf.String())
	}
	// Past it: the hold is reported again, so a pod held for an hour is in
	// the log every quarter of it rather than once at the start.
	now = now.Add(time.Minute + time.Second)
	f.Decide(fsSighting(now, "c1", "pod-a", 3))
	if got := countLines(buf, "held FailedScheduling pod=default/pod-a"); got != 2 {
		t.Errorf("a sighting past the repeat interval logged %d lines in total; want 2\n%s", got, buf.String())
	}
}

func TestFailedSchedulingGateLogsEachPodAndClusterOnItsOwn(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	for i := 0; i < 10; i++ {
		f.Decide(fsSighting(now, "c1", "pod-a", i+1))
		f.Decide(fsSighting(now, "c1", "pod-b", i+1))
		// The same UID on another cluster is another pod.
		f.Decide(fsSighting(now, "c2", "pod-a", i+1))
	}
	if got := countLines(buf, "held FailedScheduling pod=default/pod-a"); got != 2 {
		t.Errorf("pod-a on two clusters logged %d lines; want 2\n%s", got, buf.String())
	}
	if got := countLines(buf, "held FailedScheduling pod=default/pod-b"); got != 1 {
		t.Errorf("pod-b logged %d lines; want 1\n%s", got, buf.String())
	}
}

// One filter serves every cluster's delivery goroutine, so the memo behind
// the log lines is written from many goroutines at once.
func TestFailedSchedulingGateIsSafeForConcurrentDispatchers(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(1000, &now)
	const goroutines, sightings = 8, 200
	var wg sync.WaitGroup
	for g := 0; g < goroutines; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			uid := fmt.Sprintf("pod-%d", g)
			for count := 1; count <= sightings; count++ {
				f.Decide(fsSighting(now, "c1", uid, count))
			}
		}(g)
	}
	wg.Wait()
	if got := countLines(buf, "held FailedScheduling pod=default/pod-"); got != goroutines {
		t.Errorf("%d pods × %d sightings logged %d lines; want %d\n%s", goroutines, sightings, got, goroutines, buf.String()[:min(2000, len(buf.String()))])
	}
}

// A full memo keeps what it holds. A pod it has no room for is logged on
// every sighting, as every pod was before this memo existed, and nothing it
// already remembers is displaced to make room: evicting live entries under a
// working set larger than the cap would re-log every pod on every sighting
// and scan the whole memo under the fleet-wide lock each time.
func TestFailedSchedulingGateLogMemoKeepsWhatItHoldsWhenFull(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	f.Decide(fsSighting(now, "c1", "first", 1))
	for i := 1; i < maxFailedSchedulingLogEntries; i++ {
		f.Decide(fsSighting(now, "c1", fmt.Sprintf("pod-%d", i), 1))
	}
	if got := f.fsLogged.len(); got != maxFailedSchedulingLogEntries {
		t.Fatalf("memo holds %d entries after filling it; want %d", got, maxFailedSchedulingLogEntries)
	}
	// Over the cap: the new pod's first decision is still logged, on every
	// sighting, and the memo neither grows nor gives anything up for it.
	for count := 1; count <= 3; count++ {
		f.Decide(fsSighting(now, "c1", "overflow", count))
	}
	if got := countLines(buf, "pod=default/overflow"); got != 3 {
		t.Errorf("a pod over the cap logged %d lines in 3 sightings; want 3 (logged, not remembered)", got)
	}
	if got := f.fsLogged.len(); got != maxFailedSchedulingLogEntries {
		t.Errorf("memo holds %d entries after an over-cap pod; want %d", got, maxFailedSchedulingLogEntries)
	}
	f.Decide(fsSighting(now, "c1", "first", 2))
	if got := countLines(buf, "pod=default/first"); got != 1 {
		t.Errorf("the first pod logged %d lines; want 1 (its entry was not displaced by the overflow)", got)
	}
}

// Expired entries are swept out of a full memo at most once per
// failedSchedulingLogSweepEvery, so an over-cap fleet costs one scan a minute
// rather than one per sighting, and room comes back once the entries age out.
func TestFailedSchedulingGateLogMemoSweepsExpiredEntriesWhenFull(t *testing.T) {
	captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	for i := 0; i < maxFailedSchedulingLogEntries; i++ {
		f.Decide(fsSighting(now, "c1", fmt.Sprintf("pod-%d", i), 1))
	}
	// Just inside the entries' life: the sweep runs, frees nothing, and the
	// pod is logged without being remembered.
	now = now.Add(failedSchedulingLogEvery - time.Second)
	f.Decide(fsSighting(now, "c1", "early", 1))
	if got := f.fsLogged.len(); got != maxFailedSchedulingLogEntries {
		t.Fatalf("memo holds %d entries after a sweep that should free nothing; want %d", got, maxFailedSchedulingLogEntries)
	}
	// Two seconds later every entry has expired, but the last sweep was
	// under a minute ago: no second scan, still no room.
	now = now.Add(2 * time.Second)
	f.Decide(fsSighting(now, "c1", "soon", 1))
	if got := f.fsLogged.len(); got != maxFailedSchedulingLogEntries {
		t.Errorf("memo holds %d entries inside the sweep interval; want %d (no sweep yet)", got, maxFailedSchedulingLogEntries)
	}
	// Once the interval has passed the sweep reclaims every expired entry
	// and the next pod is remembered again.
	now = now.Add(failedSchedulingLogSweepEvery)
	f.Decide(fsSighting(now, "c1", "later", 1))
	if got := f.fsLogged.len(); got != 1 {
		t.Errorf("memo holds %d entries after the sweep; want 1 (only the pod just logged)", got)
	}
}

// A GKE cluster name is unique only within its project and location, and a
// fleet can watch two clusters named alike, so the memo keys on the triple.
func TestFailedSchedulingGateLogsSameNamedClustersApart(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	a := fsSighting(now, "prod", "pod-a", 1)
	a.Project, a.Location = "team-one", "us-central1"
	b := fsSighting(now, "prod", "pod-a", 1)
	b.Project, b.Location = "team-two", "europe-west1"
	for i := 0; i < 5; i++ {
		f.Decide(a)
		f.Decide(b)
	}
	if got := countLines(buf, "held FailedScheduling pod=default/pod-a"); got != 2 {
		t.Errorf("one pod UID on two same-named clusters logged %d lines; want 2", got)
	}
}

// An Event with no involvedObject UID or namespace names no one pod, so it is
// not memoised: every such sighting is logged, as the scale-up memo refuses
// the same key rather than file two pods under it.
func TestFailedSchedulingGateDoesNotMemoiseAnEventWithoutAPodKey(t *testing.T) {
	buf := captureLog(t)
	now := time.Unix(1_700_000_000, 0)
	f := fsLogFilter(100, &now)
	noUID := fsSighting(now, "c1", "", 1)
	noUID.Name = "handmade"
	for i := 0; i < 3; i++ {
		f.Decide(noUID)
	}
	noNS := fsSighting(now, "c1", "pod-x", 1)
	noNS.Namespace = ""
	for i := 0; i < 3; i++ {
		f.Decide(noNS)
	}
	if got := countLines(buf, "pod=default/handmade"); got != 3 {
		t.Errorf("3 sightings of an event with no UID logged %d lines; want 3", got)
	}
	if got := countLines(buf, "pod=/pod-x"); got != 3 {
		t.Errorf("3 sightings of an event with no namespace logged %d lines; want 3", got)
	}
	if got := f.fsLogged.len(); got != 0 {
		t.Errorf("memo holds %d entries for events with no pod key; want 0", got)
	}
}
