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
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

// One test per branch of the design's resets section
// (docs/designs/usage-counters-producer.md), each asserting the poll itself
// and, where the branch has a next poll to assert, the poll after. The
// accumulator takes samples and the document and returns the next document,
// so none of these needs a socket.

const (
	foldTestAgentUID = "agent-uid"
	// The two gateway replicas and the broker pod the scenarios use.
	foldPodA      = "gateway-a"
	foldPodB      = "gateway-b"
	foldPodBroker = "broker"
)

func foldClock(minute int) time.Time {
	return time.Date(2026, 10, 2, 12, minute, 0, 0, time.UTC)
}

func foldEntry(name, counter string, sample int64, start *float64, marker time.Time) *usagePodEntry {
	return &usagePodEntry{Name: name, Counter: counter, Sample: sample, StartTime: start, Marker: metav1.NewTime(marker)}
}

// foldDoc is a document first recorded at first with the given entries, keyed
// by the entry's name as its UID, and zero totals.
func foldDoc(first time.Time, entries ...*usagePodEntry) *usageDocument {
	doc := &usageDocument{
		Version:       usageDocumentVersion,
		AgentUID:      foldTestAgentUID,
		FirstRecorded: metav1.NewTime(first),
		Totals:        map[string]int64{usageCounterToolExecutions: 0, usageCounterEventsIngested: 0},
		Pods:          map[string]*usagePodEntry{},
	}
	for _, e := range entries {
		doc.Pods[e.Name] = e
	}
	return doc
}

// foldLive is every pod of doc plus extra, as the live set.
func foldLive(doc *usageDocument, extra ...string) map[string]bool {
	live := map[string]bool{}
	if doc != nil {
		for uid := range doc.Pods {
			live[uid] = true
		}
	}
	for _, uid := range extra {
		live[uid] = true
	}
	return live
}

func scrapedPod(name, counter string, created time.Time, sample int64, start *float64) usageScrapedPod {
	return usageScrapedPod{UID: name, Name: name, Created: created, Counter: counter, Sample: sample, StartTime: start}
}

// foldSteps runs polls over one document and asserts the counter's total and
// LastMoved after each.
type foldStep struct {
	minute  int
	scraped []usageScrapedPod
	live    []string // extra live UIDs beyond the document's entries
	want    int64    // the counter's total after the poll
	moved   bool
}

// movedAt reports whether the poll at at moved a total: the document's
// LastMoved is stamped with the poll that last did.
func movedAt(res usageFoldResult, at time.Time) bool {
	return res.Document.LastMoved != nil && res.Document.LastMoved.Time.Equal(at)
}

func runFoldSteps(t *testing.T, doc *usageDocument, counter string, steps []foldStep) *usageDocument {
	t.Helper()
	for i, step := range steps {
		res := foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc, step.live...), step.scraped, foldClock(step.minute))
		doc = res.Document
		if got := doc.Totals[counter]; got != step.want {
			t.Fatalf("step %d (minute %d): %s = %d, want %d", i, step.minute, counter, got, step.want)
		}
		if moved := movedAt(res, foldClock(step.minute)); moved != step.moved {
			t.Fatalf("step %d (minute %d): moved = %v (lastMoved %v), want %v", i, step.minute, moved, doc.LastMoved, step.moved)
		}
	}
	return doc
}

func TestFoldUsage_FirstPollRecordsEveryPodAndAddsNothing(t *testing.T) {
	t0 := foldClock(0)
	res := foldUsage(nil, foldTestAgentUID, usageSeed{}, map[string]bool{foldPodA: true, foldPodBroker: true}, []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, t0.Add(-time.Hour), 500, ptr.To(100.0)),
		scrapedPod(foldPodBroker, usageCounterToolExecutions, t0.Add(-time.Hour), 70, nil),
	}, t0)
	doc := res.Document
	if !res.Changed || movedAt(res, t0) {
		t.Fatalf("first poll: changed=%v lastMoved=%v, want changed and not moved", res.Changed, res.Document.LastMoved)
	}
	if doc.Totals[usageCounterEventsIngested] != 0 || doc.Totals[usageCounterToolExecutions] != 0 || doc.LastMoved != nil {
		t.Fatalf("first poll added: totals=%v lastMoved=%v", doc.Totals, doc.LastMoved)
	}
	if !doc.FirstRecorded.Time.Equal(t0) || doc.AgentUID != foldTestAgentUID || doc.Version != usageDocumentVersion {
		t.Fatalf("document header: %+v", doc)
	}
	if a := doc.Pods[foldPodA]; a == nil || a.Sample != 500 || a.StartTime == nil || *a.StartTime != 100 || !a.Marker.Time.Equal(t0) {
		t.Fatalf("pod A not recorded at its sample: %+v", a)
	}
	// The poll after counts from the recorded baseline.
	res = foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, t0.Add(-time.Hour), 512, ptr.To(100.0)),
		scrapedPod(foldPodBroker, usageCounterToolExecutions, t0.Add(-time.Hour), 73, nil),
	}, foldClock(5))
	if res.Document.Totals[usageCounterEventsIngested] != 12 || res.Document.Totals[usageCounterToolExecutions] != 3 || !movedAt(res, foldClock(5)) {
		t.Fatalf("second poll: totals=%v lastMoved=%v, want 12/3 and moved", res.Document.Totals, res.Document.LastMoved)
	}
}

// A re-seed records both gateway replicas level and, on the first poll they
// advance together, takes the larger of the two deltas. The replicas share one
// event stream, so their lifetime samples order by process age, not stream
// position, and a re-seed cannot tell a replica lagging after the seed (a short
// first delta, ordinary informer lag) from one catching up from before it (a
// long first delta carrying a backlog): the deltas are identical. So neither is
// guessed and the larger, complete delta is taken. Here A and B seed level and
// B's informer trails by four events on the next poll; taking the smaller would
// drop the four B had not yet delivered. This is the case
// TestUsagePoller_TwoGatewayReplicas exercises end to end.
func TestFoldUsage_ALevelReseedTakesTheLargerDeltaOnTheFirstJointAdvance(t *testing.T) {
	t0 := foldClock(0)
	old := t0.Add(-time.Hour)
	res := foldUsage(nil, foldTestAgentUID, usageSeed{}, map[string]bool{foldPodA: true, foldPodB: true}, []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, old, 100, ptr.To(1.0)),
		scrapedPod(foldPodB, usageCounterEventsIngested, old, 100, ptr.To(1.0)),
	}, t0)
	doc := res.Document
	if doc.Totals[usageCounterEventsIngested] != 0 || doc.LastMoved != nil {
		t.Fatalf("re-seed added: totals=%v lastMoved=%v", doc.Totals, doc.LastMoved)
	}
	for _, uid := range []string{foldPodA, foldPodB} {
		if e := doc.Pods[uid]; e == nil || !e.Marker.Time.Equal(t0) {
			t.Fatalf("%s not recorded level at the seed poll: %+v", uid, e)
		}
	}
	// Minute 5, both advance: A reads its full +10 while B's informer trails by
	// four and reads +6. The larger delta is the full interval, so ten is taken;
	// the smaller would lose the four B had not delivered. Minute 10 is ordinary:
	// A reads its next +10 and B's +10 catches its lag up, which the reset absorbs
	// against A's marker, so only A's ten is counted.
	runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, old, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, old, 106, ptr.To(1.0)),
		}, want: 10, moved: true},
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, old, 120, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, old, 116, ptr.To(1.0)),
		}, want: 20, moved: true},
	})
}

// The bounded re-seed over-count the design records (its resets section). When a
// replica that trailed at the seed advances in the same poll as a current
// sibling, its first delta carries a backlog the shared stream already delivered
// through the sibling, and plain Max takes that larger delta. Here A seeds at
// 1000 three events behind the stream and B at 20 current; on the first joint
// poll A reads +13 (its three-event backlog over the same ten B reads as +10),
// and thirteen is taken. The residual is bounded by the informer backlog at the
// seed and is the price of never guessing which replica trailed from samples
// that cannot say; a trailing replica that advances a poll later is reset
// instead, not over-counted (the delayed-catch-up test below).
func TestFoldUsage_AReseedOverCountsASimultaneousCatchUp(t *testing.T) {
	t0 := foldClock(0)
	old := t0.Add(-time.Hour)
	res := foldUsage(nil, foldTestAgentUID, usageSeed{}, map[string]bool{foldPodA: true, foldPodB: true}, []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, old, 1000, ptr.To(1.0)),
		scrapedPod(foldPodB, usageCounterEventsIngested, old, 20, ptr.To(9.0)),
	}, t0)
	// Both advance on the first poll after the seed with level markers, so neither
	// is reset and the larger delta is taken, over-counting by A's three-event
	// backlog: ten distinct events plus three.
	runFoldSteps(t, res.Document, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, old, 1013, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, old, 30, ptr.To(9.0)),
		}, want: 13, moved: true},
	})
}

// A re-seed's trailing replica (A, the larger sample) catches up alone, a poll
// after its current sibling (B) advanced. B's first interval is its own ten
// events, counted -- the old leader-by-sample rule recorded B behind and reset
// that interval to nothing, a permanent loss if the leader then terminated. A's
// later +23 (twenty distinct events plus its three-event seed catch-up) arrives
// after B moved its marker, so the ordinary reset absorbs it against B's marker
// rather than counting it on top.
func TestFoldUsage_AReseedAbsorbsADelayedCatchUpAgainstAQuietSibling(t *testing.T) {
	t0 := foldClock(0)
	old := t0.Add(-time.Hour)
	res := foldUsage(nil, foldTestAgentUID, usageSeed{}, map[string]bool{foldPodA: true, foldPodB: true}, []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, old, 1000, ptr.To(1.0)),
		scrapedPod(foldPodB, usageCounterEventsIngested, old, 20, ptr.To(9.0)),
	}, t0)
	runFoldSteps(t, res.Document, usageCounterEventsIngested, []foldStep{
		// A is quiet (informer still catching up); only B advances by its ten.
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, old, 1000, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, old, 30, ptr.To(9.0)),
		}, want: 10, moved: true},
		// A delivers its backlog and the next ten at once (+23); B reads its ten.
		// A is reset against B's minute-5 marker, so the total takes B's ten.
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, old, 1023, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, old, 40, ptr.To(9.0)),
		}, want: 20, moved: true},
	})
}

// A broker marker ahead of a gateway's must not reset the gateway. The broker's
// Sum branch stamps its marker on every poll it adds, so on an ordinary
// single-replica install a gateway quiet while commands ran is "behind" the
// broker on its next advance. latestSiblingMarker's counter filter keeps the
// broker out of the gateway's sibling set; without it the gateway's events are
// dropped. This pins the "another gateway pod's" qualifier of the reset branch,
// which no other test in the package exercises.
func TestFoldUsage_ABrokerMarkerAheadDoesNotResetTheGateway(t *testing.T) {
	first := foldClock(0)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
		foldEntry(foldPodBroker, usageCounterToolExecutions, 200, nil, foldClock(5)),
	)
	// Only the gateway advances at minute 10; the broker is quiet, its marker
	// one poll ahead of the gateway's.
	res := foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, first.Add(-time.Hour), 105, ptr.To(1.0)),
	}, foldClock(10))
	if got := res.Document.Totals[usageCounterEventsIngested]; got != 5 {
		t.Fatalf("eventsIngested = %d, want 5: the gateway's lone advance was reset against the broker's marker", got)
	}
	if g := res.Document.Pods[foldPodA]; g == nil || !g.Marker.Time.Equal(foldClock(10)) {
		t.Fatalf("gateway marker = %v, want minute 10 (it advanced alone)", g.Marker)
	}
}

// Baseline absent with counters present: something removed the state after
// the counters were written. The totals start from the status, every pod is
// recorded, and the time the status carries is kept rather than zeroed.
func TestFoldUsage_SeedsTheTotalsFromTheStatus(t *testing.T) {
	t0 := foldClock(0)
	earlier := metav1.NewTime(t0.Add(-30 * time.Minute))
	res := foldUsage(nil, foldTestAgentUID, usageSeed{
		Totals:    map[string]int64{usageCounterToolExecutions: 1000, usageCounterEventsIngested: 40},
		LastMoved: &earlier,
	}, map[string]bool{foldPodBroker: true}, []usageScrapedPod{
		scrapedPod(foldPodBroker, usageCounterToolExecutions, t0.Add(-time.Hour), 2000, nil),
	}, t0)
	doc := res.Document
	if doc.Totals[usageCounterToolExecutions] != 1000 || doc.Totals[usageCounterEventsIngested] != 40 {
		t.Fatalf("totals not seeded from the status: %v", doc.Totals)
	}
	if doc.LastMoved == nil || !doc.LastMoved.Equal(&earlier) {
		t.Fatalf("lastMoved = %v, want the status's %v", doc.LastMoved, earlier)
	}
	if doc.Pods[foldPodBroker].Sample != 2000 {
		t.Fatalf("the broker was not recorded at its sample: %+v", doc.Pods[foldPodBroker])
	}
	res = foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), []usageScrapedPod{
		scrapedPod(foldPodBroker, usageCounterToolExecutions, t0.Add(-time.Hour), 2004, nil),
	}, foldClock(5))
	if res.Document.Totals[usageCounterToolExecutions] != 1004 {
		t.Fatalf("the poll after seeding added %d, want 4", res.Document.Totals[usageCounterToolExecutions]-1000)
	}
}

func TestFoldUsage_TheDifferenceBranch(t *testing.T) {
	first := foldClock(0)
	doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(50.0), first))
	created := first.Add(-time.Hour)
	doc = runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 130, ptr.To(50.0))}, want: 30, moved: true},
		{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 130, ptr.To(50.0))}, want: 30},
		{minute: 15, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 131, ptr.To(50.0))}, want: 31, moved: true},
	})
	if e := doc.Pods[foldPodBroker]; e.Sample != 131 || !e.Marker.Time.Equal(foldClock(15)) {
		t.Fatalf("entry after the steps: %+v", e)
	}
}

// A pod the document does not know, created after it was first recorded,
// started from zero: its whole sample adds, under the ceiling. Above the
// ceiling it is recorded and adds nothing, and the next poll counts from the
// recorded sample.
func TestFoldUsage_TheWholeSampleBranchForANewPod(t *testing.T) {
	first := foldClock(0)
	after := first.Add(time.Minute)
	t.Run("under the ceiling", func(t *testing.T) {
		doc := foldDoc(first)
		runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 5, live: []string{foldPodA}, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, after, 25, ptr.To(1.0))}, want: 25, moved: true},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, after, 27, ptr.To(1.0))}, want: 27, moved: true},
		})
	})
	t.Run("above the ceiling: recorded, adds nothing", func(t *testing.T) {
		doc := foldDoc(first)
		doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 5, live: []string{foldPodA}, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, after, usageDeltaCeiling+1, ptr.To(1.0))}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, after, usageDeltaCeiling+4, ptr.To(1.0))}, want: 3, moved: true},
		})
		if doc.Pods[foldPodA] == nil {
			t.Fatal("a pod above the ceiling was not recorded")
		}
	})
}

// Absence from the document is not newness: a pod created before the document
// was first recorded, which the first poll could not scrape, is recorded on
// its next scrape and adds nothing, on the first document and on a re-seed
// alike; the re-seed records its own time, not a predecessor's.
func TestFoldUsage_AnOldPodAbsentFromTheDocumentIsRecordedWithoutAdding(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	t.Run("on the first document", func(t *testing.T) {
		doc := foldDoc(first)
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, live: []string{foldPodBroker}, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 900, nil)}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 905, nil)}, want: 5, moved: true},
		})
	})
	t.Run("on a re-seed", func(t *testing.T) {
		reseedAt := foldClock(20)
		// The pod was created between an earlier document's first-recorded
		// time and the re-seed: under a carried-forward time it would read as
		// new on its next scrape and add its lifetime a second time.
		betweenCreated := foldClock(10)
		res := foldUsage(nil, foldTestAgentUID, usageSeed{Totals: map[string]int64{usageCounterToolExecutions: 50}}, map[string]bool{}, nil, reseedAt)
		doc := res.Document
		if !doc.FirstRecorded.Time.Equal(reseedAt) {
			t.Fatalf("the re-seed recorded %v as first recorded, want its own time %v", doc.FirstRecorded, reseedAt)
		}
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 25, live: []string{foldPodBroker}, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, betweenCreated, 300, nil)}, want: 50},
			{minute: 30, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, betweenCreated, 302, nil)}, want: 52, moved: true},
		})
	})
}

// The process restarted inside the same pod: a later start time, and the
// whole sample adds, whatever the sample did.
func TestFoldUsage_ALaterStartTimeAddsTheWholeSample(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 80, ptr.To(100.0), first))
	t.Run("a sample below the last", func(t *testing.T) {
		d := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 80, ptr.To(100.0), first))
		d = runFoldSteps(t, d, usageCounterEventsIngested, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 5, ptr.To(200.0))}, want: 5, moved: true},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 8, ptr.To(200.0))}, want: 8, moved: true},
		})
		if e := d.Pods[foldPodA]; *e.StartTime != 200 || e.Sample != 8 {
			t.Fatalf("entry after the restart: %+v", e)
		}
	})
	t.Run("a sample that overtook the last inside one interval", func(t *testing.T) {
		runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 90, ptr.To(200.0))}, want: 90, moved: true},
		})
	})
}

// The first body carrying the gauge for an entry that recorded none is a
// process that started, a container restarted in place onto a newer image:
// the whole sample, under the ceiling, and the start time recorded.
func TestFoldUsage_TheFirstBodyWithTheGaugeTakesTheWholeSample(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 80, nil, first))
	doc = runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 30, ptr.To(300.0))}, want: 30, moved: true},
		{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 31, ptr.To(300.0))}, want: 31, moved: true},
	})
	if e := doc.Pods[foldPodBroker]; e.StartTime == nil || *e.StartTime != 300 {
		t.Fatalf("the start time was not recorded: %+v", e)
	}
	// Above the ceiling, the same branch records and adds nothing.
	big := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 80, nil, first))
	runFoldSteps(t, big, usageCounterToolExecutions, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, usageDeltaCeiling+1, ptr.To(300.0))}, want: 0},
		{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, usageDeltaCeiling+2, ptr.To(300.0))}, want: 1, moved: true},
	})
}

// A forged later start time with a large sample is evidence of a restart too;
// the per-poll ceiling is what sizes the damage: nothing added, the baseline
// advanced, and the next poll counts the interval alone.
func TestFoldUsage_AForgedLaterStartTimeWithALargeSampleAddsNothing(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 80, ptr.To(100.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 1000000, ptr.To(200.0))}, want: 0},
		{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 1000005, ptr.To(200.0))}, want: 5, moved: true},
	})
	if e := doc.Pods[foldPodA]; e.Sample != 1000005 || *e.StartTime != 200 {
		t.Fatalf("entry after the forged body: %+v", e)
	}
}

// The two refused shapes: a start time earlier than the recorded one, and a
// fall under an unchanged start time. Refused with the baseline advanced to
// the body's sample and start time, nothing added, and the next poll counting
// from it.
func TestFoldUsage_RefusedBodiesAdvanceTheBaseline(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	t.Run("an earlier start time", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 80, ptr.To(100.0), first))
		doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 300, ptr.To(50.0))}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 303, ptr.To(50.0))}, want: 3, moved: true},
		})
		if e := doc.Pods[foldPodA]; *e.StartTime != 50 || e.Sample != 303 {
			t.Fatalf("entry: %+v", e)
		}
	})
	t.Run("a fall under an unchanged start time", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 80, ptr.To(100.0), first))
		res := foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 70, ptr.To(100.0)),
		}, foldClock(5))
		if movedAt(res, foldClock(5)) || !res.Changed || res.Document.Pods[foldPodA].Sample != 70 {
			t.Fatalf("the fall: lastMoved=%v changed=%v entry=%+v", res.Document.LastMoved, res.Changed, res.Document.Pods[foldPodA])
		}
		runFoldSteps(t, res.Document, usageCounterEventsIngested, []foldStep{
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 75, ptr.To(100.0))}, want: 5, moved: true},
		})
	})
}

// The rule without the gauge, for listeners from a release before it: the
// difference, a new pod's whole sample, a fall that advances the baseline, and
// a body without the gauge after a start time was recorded, refused the same
// way and staying refused, since a listener does not lose the gauge inside one
// pod.
func TestFoldUsage_TheRuleWithoutTheGauge(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	t.Run("the difference", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, nil, first))
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 107, nil)}, want: 7, moved: true},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 107, nil)}, want: 7},
		})
	})
	t.Run("a new pod's whole sample", func(t *testing.T) {
		doc := foldDoc(first)
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, live: []string{foldPodBroker}, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, first.Add(time.Minute), 9, nil)}, want: 9, moved: true},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, first.Add(time.Minute), 10, nil)}, want: 10, moved: true},
		})
	})
	t.Run("a fall advances the baseline", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, nil, first))
		doc = runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 3, nil)}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 5, nil)}, want: 2, moved: true},
		})
		if doc.Pods[foldPodBroker].Sample != 5 {
			t.Fatalf("baseline after the fall and the next poll: %+v", doc.Pods[foldPodBroker])
		}
	})
	t.Run("a body without the gauge after a start time was recorded", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(300.0), first))
		doc = runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 150, nil)}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 160, nil)}, want: 0},
		})
		if e := doc.Pods[foldPodBroker]; e.Sample != 160 || e.StartTime == nil || *e.StartTime != 300 {
			t.Fatalf("the refusal did not advance the sample and keep the start time: %+v", e)
		}
		// The listener's real body, once the pod restarts, is counted again.
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 15, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 4, ptr.To(400.0))}, want: 4, moved: true},
		})
	})
}

// The per-poll ceiling on both adding branches: an honest burst past it costs
// that interval, and the next poll's delta is the new interval alone; a gap of
// several polls whose delta passes one ceiling is refused whole, the baseline
// advanced, that gap's count lost once.
func TestFoldUsage_ThePerPollCeiling(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	t.Run("an honest burst on the difference branch", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(1.0), first))
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 100+usageDeltaCeiling+1, ptr.To(1.0))}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 100+usageDeltaCeiling+6, ptr.To(1.0))}, want: 5, moved: true},
		})
	})
	t.Run("exactly the ceiling adds", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(1.0), first))
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 100+usageDeltaCeiling, ptr.To(1.0))}, want: usageDeltaCeiling, moved: true},
		})
	})
	t.Run("a gap of several polls is the same ceiling", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(1.0), first))
		// Minute 60: the first poll after an operator outage of eleven intervals.
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 60, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 100+3*usageDeltaCeiling, ptr.To(1.0))}, want: 0},
			{minute: 65, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 100+3*usageDeltaCeiling+2, ptr.To(1.0))}, want: 2, moved: true},
		})
	})
	t.Run("a restart with a sample past the ceiling", func(t *testing.T) {
		doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(1.0), first))
		runFoldSteps(t, doc, usageCounterToolExecutions, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, usageDeltaCeiling+1, ptr.To(2.0))}, want: 0},
			{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, created, usageDeltaCeiling+2, ptr.To(2.0))}, want: 1, moved: true},
		})
	})
}

// A scrape that produced no body leaves the pod out of the samples; its entry
// is kept as it was and the totals stay where they were.
func TestFoldUsage_AMissingScrapeKeepsTheBaseline(t *testing.T) {
	first := foldClock(0)
	doc := foldDoc(first, foldEntry(foldPodBroker, usageCounterToolExecutions, 100, ptr.To(1.0), first))
	res := foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), nil, foldClock(5))
	if res.Changed || movedAt(res, foldClock(5)) || res.Document.Pods[foldPodBroker].Sample != 100 {
		t.Fatalf("a poll with no body changed the document: changed=%v lastMoved=%v entry=%+v", res.Changed, res.Document.LastMoved, res.Document.Pods[foldPodBroker])
	}
	// Readable again: the whole gap since the kept baseline is added.
	runFoldSteps(t, res.Document, usageCounterToolExecutions, []foldStep{
		{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodBroker, usageCounterToolExecutions, first.Add(-time.Hour), 107, ptr.To(1.0))}, want: 7, moved: true},
	})
}

// A quiet poll writes nothing: unchanged samples under an unchanged start time
// change no entry and move no total.
func TestFoldUsage_AQuietPollChangesNothing(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 40, ptr.To(1.0), first),
		foldEntry(foldPodBroker, usageCounterToolExecutions, 100, nil, first))
	res := foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, created, 40, ptr.To(1.0)),
		scrapedPod(foldPodBroker, usageCounterToolExecutions, created, 100, nil),
	}, foldClock(5))
	if res.Changed || movedAt(res, foldClock(5)) {
		t.Fatalf("a quiet poll reported changed=%v lastMoved=%v", res.Changed, res.Document.LastMoved)
	}
}

// Entries for pods that no longer exist are dropped; their counts are already
// in the totals.
func TestFoldUsage_DepartedPodsAreDropped(t *testing.T) {
	first := foldClock(0)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 40, ptr.To(1.0), first),
		foldEntry(foldPodB, usageCounterEventsIngested, 40, ptr.To(1.0), first))
	doc.Totals[usageCounterEventsIngested] = 40
	res := foldUsage(doc, foldTestAgentUID, usageSeed{}, map[string]bool{foldPodA: true}, nil, foldClock(5))
	if !res.Changed || res.Document.Pods[foldPodB] != nil || res.Document.Totals[usageCounterEventsIngested] != 40 {
		t.Fatalf("departed pod: changed=%v pods=%v totals=%v", res.Changed, res.Document.Pods, res.Document.Totals)
	}
	// With no sibling left, A's next lone advance is counted.
	runFoldSteps(t, res.Document, usageCounterEventsIngested, []foldStep{
		{minute: 10, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, first.Add(-time.Hour), 45, ptr.To(1.0))}, want: 45, moved: true},
	})
}

// Two gateway replicas: the largest delta in a poll, not the sum, and its
// agreement with the sum for one pod.
func TestFoldUsage_TheLargestDeltaAcrossGatewayPods(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
		foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 106, ptr.To(1.0)),
		}, want: 10, moved: true},
	})
	a, b := doc.Pods[foldPodA], doc.Pods[foldPodB]
	if a.Sample != 110 || b.Sample != 106 {
		t.Fatalf("both baselines move to their samples: a=%+v b=%+v", a, b)
	}
	if !a.Marker.Time.Equal(foldClock(5)) || !b.Marker.Time.Equal(first) {
		t.Fatalf("only the taken delta moves its marker: a=%v b=%v", a.Marker, b.Marker)
	}
	// One pod alone: the largest is the sum.
	single := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	runFoldSteps(t, single, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 107, ptr.To(1.0))}, want: 7, moved: true},
	})
	// The broker sums: two broker pods never broker the same command.
	brokers := foldDoc(first,
		foldEntry("broker-old", usageCounterToolExecutions, 10, nil, first),
		foldEntry("broker-new", usageCounterToolExecutions, 0, nil, first))
	runFoldSteps(t, brokers, usageCounterToolExecutions, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod("broker-old", usageCounterToolExecutions, created, 12, nil),
			scrapedPod("broker-new", usageCounterToolExecutions, created, 3, nil),
		}, want: 5, moved: true},
	})
}

// A partial straddle: both replicas move by different amounts, the total takes
// the larger once, and the lagger's catch-up alone the poll after is reset
// rather than counted on top.
func TestFoldUsage_APartialStraddleIsCountedOnce(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
		foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 106, ptr.To(1.0)),
		}, want: 10, moved: true},
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
		}, want: 10},
	})
	if b := doc.Pods[foldPodB]; b.Sample != 110 || !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("the reset lagger takes the sibling's marker and its sample: %+v", b)
	}
	// With both level, the next lone advance by either is counted.
	runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 15, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 113, ptr.To(1.0)),
		}, want: 13, moved: true},
	})
}

// A pod missing this poll and back in the next, with and without a second
// gateway pod counted in between: the reset pod takes the sibling's marker,
// and the sibling's next lone advance is counted.
func TestFoldUsage_AMissedPodIsResetAgainstACountedSibling(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	t.Run("the sibling was counted in between", func(t *testing.T) {
		doc := foldDoc(first,
			foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
			foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
		doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			// B cannot be read; A is counted.
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 108, ptr.To(1.0))}, want: 8, moved: true},
			// B is back, presenting what A already supplied: reset, nothing added.
			{minute: 10, scraped: []usageScrapedPod{
				scrapedPod(foldPodA, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
				scrapedPod(foldPodB, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
			}, want: 8},
		})
		if b := doc.Pods[foldPodB]; !b.Marker.Time.Equal(foldClock(5)) || b.Sample != 108 {
			t.Fatalf("B after the reset: %+v, want A's marker %v", b, foldClock(5))
		}
		// A's next lone advance is counted: B's marker is A's, not behind it.
		runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 15, scraped: []usageScrapedPod{
				scrapedPod(foldPodA, usageCounterEventsIngested, created, 111, ptr.To(1.0)),
				scrapedPod(foldPodB, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
			}, want: 11, moved: true},
		})
	})
	t.Run("nothing was counted in between", func(t *testing.T) {
		doc := foldDoc(first,
			foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
			foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
		runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 5, scraped: []usageScrapedPod{scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0))}, want: 0},
			// B is back with its own advance, and A was quiet: counted.
			{minute: 10, scraped: []usageScrapedPod{
				scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
				scrapedPod(foldPodB, usageCounterEventsIngested, created, 104, ptr.To(1.0)),
			}, want: 4, moved: true},
		})
	})
}

// An in-place restart of the lagging replica with a later start time is reset
// rather than taken whole: the events its new process replayed are the ones
// the sibling already supplied.
func TestFoldUsage_ALaggingReplicaThatRestartedIsResetNotTakenWhole(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), foldClock(5)),
		foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 30, ptr.To(2.0)),
		}, want: 0},
	})
	if b := doc.Pods[foldPodB]; b.Sample != 30 || *b.StartTime != 2 || !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("B after the reset: %+v", b)
	}
	// Level with A now: B's next lone advance from its new process is counted.
	runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 15, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 33, ptr.To(2.0)),
		}, want: 3, moved: true},
	})
}

// During a rollout a new pod's whole sample competes with the old pod's
// difference and the larger wins; the new entry resets no sibling in the poll
// that records it.
func TestFoldUsage_ANewReplicaCompetesWithTheOldOnesDifference(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, live: []string{foldPodB}, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 105, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, foldClock(3), 20, ptr.To(9.0)),
		}, want: 20, moved: true},
	})
	if a := doc.Pods[foldPodA]; a.Sample != 105 || !a.Marker.Time.Equal(first) {
		t.Fatalf("the old pod's baseline moves and its marker stays: %+v", a)
	}
	if b := doc.Pods[foldPodB]; !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("the new entry takes the current poll: %+v", b)
	}
	// The old pod's difference wins when it is the larger.
	doc2 := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	runFoldSteps(t, doc2, usageCounterEventsIngested, []foldStep{
		{minute: 5, live: []string{foldPodB}, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 150, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, foldClock(3), 20, ptr.To(9.0)),
		}, want: 50, moved: true},
	})
}

func TestFoldUsage_ALosingNewReplicaResetsItsCatchUpNextPoll(t *testing.T) {
	// Two gateway replicas share the event stream (Max). A new replica that
	// loses the fold to a live sibling is recorded behind that sibling, so its
	// first catch-up is reset next poll rather than counted on top of what the
	// sibling already supplied. Before the marker fix it was stamped with the
	// poll it lost, read as current next poll, and its catch-up double-counted.
	first := foldClock(0)
	oldCreated := first.Add(-time.Hour)
	newCreated := foldClock(3)
	doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first))

	// Poll 5: the old replica advances by 10 and wins; the new replica, read at
	// 2, loses and is recorded at the sibling's as-read marker, not this poll's.
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, live: []string{foldPodB}, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, oldCreated, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, newCreated, 2, ptr.To(9.0)),
		}, want: 10, moved: true},
	})
	if b := doc.Pods[foldPodB]; !b.Marker.Time.Equal(first) {
		t.Fatalf("the losing new replica takes the sibling's as-read marker, not the poll it lost: %+v", b)
	}

	// Poll 10: the old replica is flat and the new replica catches up to 5. The
	// catch-up is the sibling's events, already counted, so it resets and adds
	// nothing; the total stays 10. (Before the fix the new replica read as
	// current, its catch-up of 3 was taken, and the total reached 13.)
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, oldCreated, 110, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, newCreated, 5, ptr.To(9.0)),
		}, want: 10, moved: false},
	})
	if b := doc.Pods[foldPodB]; b.Sample != 5 || !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("the reset advances the new replica's baseline and takes the live sibling's marker: %+v", b)
	}
}

// The sibling the test above does not cover: both Max replicas are read for the
// first time in one poll, so neither has a live sibling to inherit a marker
// from. The loser must still land behind this poll -- at FirstRecorded -- so its
// first catch-up is reset next poll rather than counted on top of what the
// winner already supplied. Before the fix the loser kept this poll's stamp, read
// as current next poll, and its catch-up double-counted (100 -> 140).
func TestFoldUsage_TwoNewReplicasInOnePollDoNotDoubleCount(t *testing.T) {
	first := foldClock(0)
	createdA := foldClock(1)
	createdB := foldClock(2)
	doc := foldDoc(first) // Empty: no Max entry to inherit a marker from.

	// Poll 5: both replicas are new. The larger sample (A at 100) wins the fold
	// and its whole sample is taken; B at 60 loses and must be recorded behind
	// this poll. want is the winner's sample only.
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, live: []string{foldPodA, foldPodB}, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, createdA, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, createdB, 60, ptr.To(1.0)),
		}, want: 100, moved: true},
	})
	if b := doc.Pods[foldPodB]; !b.Marker.Time.Equal(first) {
		t.Fatalf("the losing new replica takes FirstRecorded, not the poll it lost: %+v", b)
	}

	// Poll 10: A is flat and B catches up by 40. The catch-up is the events the
	// winner already supplied, so it resets and adds nothing; the total stays
	// 100. (Before the fix B read as current, its catch-up was taken, and the
	// total reached 140.)
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, createdA, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, createdB, 100, ptr.To(1.0)),
		}, want: 100, moved: false},
	})
	if b := doc.Pods[foldPodB]; b.Sample != 100 || !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("the reset advances the new replica's baseline and takes the live sibling's marker: %+v", b)
	}
}

func TestAddUsageTotal_RefusesPastTheCeiling(t *testing.T) {
	doc := foldDoc(foldClock(0))
	doc.Totals[usageCounterToolExecutions] = usageTotalCeiling - 1
	if addUsageTotal(doc, usageCounterToolExecutions, 2) {
		t.Fatal("an add past the ceiling was taken")
	}
	if !addUsageTotal(doc, usageCounterToolExecutions, 1) || doc.Totals[usageCounterToolExecutions] != usageTotalCeiling {
		t.Fatalf("an add up to the ceiling was refused: %d", doc.Totals[usageCounterToolExecutions])
	}
	if addUsageTotal(doc, usageCounterToolExecutions, 0) {
		t.Fatal("a zero delta counted as an add")
	}
}

// A replica reset against its sibling's marker by an advancing body without
// the gauge keeps its recorded start time, so the listener's next body,
// carrying the gauge and an unchanged sample, adds nothing rather than its
// whole sample.
func TestFoldUsage_AResetByAGaugelessBodyKeepsTheStartTime(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), foldClock(5)),
		foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 10, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 102, nil),
		}, want: 0},
	})
	if b := doc.Pods[foldPodB]; b.Sample != 102 || b.StartTime == nil || *b.StartTime != 1 || !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("B after the reset: %+v, want the start time kept and A's marker", b)
	}
	runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 15, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 102, ptr.To(1.0)),
		}, want: 0},
		{minute: 20, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 105, ptr.To(1.0)),
		}, want: 3, moved: true},
	})
}

// The reset fires when the behind replica next advances, not on a quiet body:
// a replica that trails its sibling on the same events by more than one
// interval keeps its old marker through the quiet poll, so its catch-up is
// reset rather than counted on top of what the sibling supplied, and the
// quiet poll writes nothing.
func TestFoldUsage_AQuietBehindReplicaIsNotResetUntilItAdvances(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
		foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
		}, want: 8, moved: true},
	})
	// The quiet poll: B is behind A's marker and unchanged; nothing moves.
	res := foldUsage(doc, foldTestAgentUID, usageSeed{}, foldLive(doc), []usageScrapedPod{
		scrapedPod(foldPodA, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
		scrapedPod(foldPodB, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
	}, foldClock(10))
	if res.Changed || movedAt(res, foldClock(10)) || !res.Document.Pods[foldPodB].Marker.Time.Equal(first) {
		t.Fatalf("the quiet poll touched B: changed=%v lastMoved=%v B=%+v", res.Changed, res.Document.LastMoved, res.Document.Pods[foldPodB])
	}
	// The catch-up: the same eight events, reset rather than counted.
	doc = runFoldSteps(t, res.Document, usageCounterEventsIngested, []foldStep{
		{minute: 15, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
		}, want: 8},
	})
	if b := doc.Pods[foldPodB]; b.Sample != 108 || !b.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("B after the catch-up: %+v, want sample 108 and A's marker", b)
	}
	// Level again: the next lone advance by either is counted.
	runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 20, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 108, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
		}, want: 10, moved: true},
	})
}

// In the HA steady state both replicas inject the same events and tie on the
// delta every poll. A candidate whose delta equalled the taken one moves its
// marker too, since it has no catch-up pending; otherwise the tie-loser's
// next lone advance, while the winner is terminating or unreadable, would be
// reset and lost.
func TestFoldUsage_ATieLoserIsNotResetOnItsNextLoneAdvance(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first,
		foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first),
		foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	both := func(a, b int64) []usageScrapedPod {
		return []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, a, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, created, b, ptr.To(1.0)),
		}
	}
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, scraped: both(108, 108), want: 8, moved: true},
		{minute: 10, scraped: both(113, 113), want: 13, moved: true},
	})
	if a, b := doc.Pods[foldPodA], doc.Pods[foldPodB]; !a.Marker.Time.Equal(foldClock(10)) || !b.Marker.Time.Equal(foldClock(10)) {
		t.Fatalf("after two ties the markers differ: A %v B %v, want both at the last poll", a.Marker, b.Marker)
	}
	// The winner is terminating: listed, not scraped. B's lone advance counts.
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 15, scraped: []usageScrapedPod{scrapedPod(foldPodB, usageCounterEventsIngested, created, 119, ptr.To(1.0))}, want: 19, moved: true},
	})
	// The winner is gone; B carries on alone.
	res := foldUsage(doc, foldTestAgentUID, usageSeed{}, map[string]bool{foldPodB: true}, []usageScrapedPod{
		scrapedPod(foldPodB, usageCounterEventsIngested, created, 122, ptr.To(1.0)),
	}, foldClock(20))
	if res.Document.Totals[usageCounterEventsIngested] != 22 || res.Document.Pods[foldPodA] != nil {
		t.Fatalf("after the winner left: totals=%v pods=%v, want 22 and A dropped", res.Document.Totals, res.Document.Pods)
	}
	// A partial straddle still leaves the lagger's marker behind, so its
	// catch-up alone is reset (TestFoldUsage_APartialStraddleIsCountedOnce).
}

// A new replica Running and injecting but unreadable on the first poll after
// it started, whose sibling was counted in that poll, is recorded at its
// sample with the sibling's marker and adds nothing when first read: the
// sibling supplied the events it injected in the meantime. Read in the poll
// it started, its whole sample still competes with the old pod's difference
// (TestFoldUsage_ANewReplicaCompetesWithTheOldOnesDifference).
func TestFoldUsage_ANewReplicaReadLateIsRecordedWithoutAdding(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	doc := foldDoc(first, foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), first))
	// Poll 5: C (created at minute 3) is live but unreadable; A is counted.
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 5, live: []string{foldPodB}, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 110, ptr.To(1.0)),
		}, want: 10, moved: true},
		// Poll 10: C readable at 12 (7 events A supplied at poll 5, then 5
		// more that A also injected); A +5. The total takes 5, not 12.
		{minute: 10, live: []string{foldPodB}, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 115, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, foldClock(3), 12, ptr.To(9.0)),
		}, want: 15, moved: true},
	})
	if c := doc.Pods[foldPodB]; c == nil || c.Sample != 12 || !c.Marker.Time.Equal(foldClock(5)) {
		t.Fatalf("C after its first read: %+v, want recorded at 12 with A's marker from poll 5", c)
	}
	// C took A's marker as read (5) while A moved to 10, so C is one behind:
	// in the next poll both advance, C is reset against A's 10 and A alone is
	// taken; C levels in a poll in which it advances and A does not. The
	// design's resets section names this as the under-count it prefers.
	doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		{minute: 15, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 118, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, foldClock(3), 15, ptr.To(9.0)),
		}, want: 18, moved: true},
	})
	if a, c := doc.Pods[foldPodA], doc.Pods[foldPodB]; !a.Marker.Time.Equal(foldClock(15)) || !c.Marker.Time.Equal(foldClock(10)) {
		t.Fatalf("after poll 15: A %v C %v, want A taken (15) and C reset to A's as-read marker (10)", a.Marker, c.Marker)
	}
	runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
		// C advances alone: reset once more, level with A from here.
		{minute: 20, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 118, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, foldClock(3), 17, ptr.To(9.0)),
		}, want: 18},
		{minute: 25, scraped: []usageScrapedPod{
			scrapedPod(foldPodA, usageCounterEventsIngested, created, 118, ptr.To(1.0)),
			scrapedPod(foldPodB, usageCounterEventsIngested, foldClock(3), 20, ptr.To(9.0)),
		}, want: 21, moved: true},
	})
}

// Three replicas: a replica behind the newest sibling marker is reset against
// the latest marker, not the first the map yields, and takes that marker;
// one level with the latest is counted.
func TestFoldUsage_ThreeReplicasResetAgainstTheLatestMarker(t *testing.T) {
	first := foldClock(0)
	created := first.Add(-time.Hour)
	const podC = "gateway-c"
	// The reset must pick the latest sibling marker whatever order the map
	// yields its entries, so the loop re-rolls that order. A first-wins bug
	// escapes a handful of rounds often (~30-50% at five); 64 drive the escape
	// below 1e-6, deterministic for correct code.
	for round := 0; round < 64; round++ {
		doc := foldDoc(first,
			foldEntry(foldPodA, usageCounterEventsIngested, 100, ptr.To(1.0), foldClock(10)),
			foldEntry(foldPodB, usageCounterEventsIngested, 100, ptr.To(1.0), foldClock(5)),
			foldEntry(podC, usageCounterEventsIngested, 100, ptr.To(1.0), first))
		doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			// C advances alone: behind both, reset against A's 10.
			{minute: 15, scraped: []usageScrapedPod{
				scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
				scrapedPod(foldPodB, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
				scrapedPod(podC, usageCounterEventsIngested, created, 104, ptr.To(1.0)),
			}, want: 0},
		})
		if c := doc.Pods[podC]; !c.Marker.Time.Equal(foldClock(10)) || c.Sample != 104 {
			t.Fatalf("round %d: C after the reset: %+v, want A's marker (10) and sample 104", round, c)
		}
		// B, behind A's 10 and ahead of C's old marker, is reset against the latest too.
		doc = runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 20, scraped: []usageScrapedPod{
				scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
				scrapedPod(foldPodB, usageCounterEventsIngested, created, 103, ptr.To(1.0)),
				scrapedPod(podC, usageCounterEventsIngested, created, 104, ptr.To(1.0)),
			}, want: 0},
		})
		if b := doc.Pods[foldPodB]; !b.Marker.Time.Equal(foldClock(10)) {
			t.Fatalf("round %d: B after the reset: %+v, want A's marker (10)", round, b)
		}
		// All level: a lone advance is counted.
		runFoldSteps(t, doc, usageCounterEventsIngested, []foldStep{
			{minute: 25, scraped: []usageScrapedPod{
				scrapedPod(foldPodA, usageCounterEventsIngested, created, 100, ptr.To(1.0)),
				scrapedPod(foldPodB, usageCounterEventsIngested, created, 103, ptr.To(1.0)),
				scrapedPod(podC, usageCounterEventsIngested, created, 106, ptr.To(1.0)),
			}, want: 2, moved: true},
		})
	}
}
