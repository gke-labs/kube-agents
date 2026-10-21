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
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

// settleBackoff is kubelet's crash-loop BackOff for one pod, sighted count
// times, first at t0 and last at t0+lasted. The count is past the count gate
// so the settle gate is the one deciding.
func settleBackoff(t0 time.Time, lasted time.Duration, count int) TriageEvent {
	ev := backoffEvent(count)
	ev.KindOfObject = "Pod"
	ev.FirstSeen = t0
	ev.LastSeen = t0.Add(lasted)
	return ev
}

func TestSettledReadsTheEventsOwnSpan(t *testing.T) {
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	for _, tc := range []struct {
		name   string
		ev     TriageEvent
		settle time.Duration
		want   bool
	}{
		{"110s of a 2m period is not settled", settleBackoff(t0, 110*time.Second, 4), 2 * time.Minute, false},
		{"exactly the period is settled", settleBackoff(t0, 2*time.Minute, 5), 2 * time.Minute, true},
		{"past the period is settled", settleBackoff(t0, 7*time.Minute, 8), 2 * time.Minute, true},
		{"no first sighting fails open", TriageEvent{LastSeen: t0}, 2 * time.Minute, true},
		{"no last sighting fails open", TriageEvent{FirstSeen: t0}, 2 * time.Minute, true},
	} {
		if got := settled(tc.ev, tc.settle); got != tc.want {
			t.Errorf("%s: settled = %t; want %t", tc.name, got, tc.want)
		}
	}
}

func TestSettleAppliesToTheTransientFamiliesOnly(t *testing.T) {
	for reason, want := range map[string]bool{
		"CrashLoopBackOff": true, "FailedMount": true, "Unhealthy": true,
		"NodeNotReady": false, "OOMKilled": false, "Evicted": false, "ImagePullBackOff": false, "FailedScheduling": false, "FailedToDrainNode": false,
	} {
		if got := settleApplies(reason); got != want {
			t.Errorf("settleApplies(%q) = %t; want %t", reason, got, want)
		}
	}
	// The wire reason is BackOff for both halves of kubelet's back-off; the
	// family is read on the canonical one.
	if !settleApplies(canonicalizeReason("BackOff", "Back-off restarting failed container app in pod api")) {
		t.Error("a crash-loop BackOff is not in the settle family")
	}
	if settleApplies(canonicalizeReason("BackOff", "Back-off pulling image \"x:latest\"")) {
		t.Error("an image-pull BackOff is in the settle family")
	}
}

// TestFilterDecideSettle walks the gate on the motivating incident: a BackOff
// that cleared in 110 seconds at count 4 is held; the same pod still failing
// two minutes on is forwarded; the gate is off at a negative threshold; and
// the exempt reasons never see it.
func TestFilterDecideSettle(t *testing.T) {
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	f := newFilter(newFilterConfig(nil, nil, nil, filterThresholds{backoffMinCount: 3, settle: 2 * time.Minute}))
	if got := f.Decide(settleBackoff(t0, 110*time.Second, 4)); got != gateSettle {
		t.Errorf("110s crash loop at count 4: gate %q; want %q", got, gateSettle)
	}
	if got := f.Decide(settleBackoff(t0, 2*time.Minute+10*time.Second, 5)); got != gateAccepted {
		t.Errorf("130s crash loop: gate %q; want accepted", got)
	}
	// The count gate still runs first: a long-lasting event seen twice is the
	// count's to hold, and says so.
	if got := f.Decide(settleBackoff(t0, 5*time.Minute, 2)); got != gateBackoffMinCount {
		t.Errorf("count 2: gate %q; want %q", got, gateBackoffMinCount)
	}
	oom := settleBackoff(t0, 10*time.Second, 1)
	oom.Key.Reason, oom.Message = "OOMKilled", "Container app exceeded its memory limit"
	if got := f.Decide(oom); got != gateAccepted {
		t.Errorf("OOMKilled: gate %q; want accepted", got)
	}
	pull := settleBackoff(t0, 10*time.Second, 1)
	pull.Message = "Back-off pulling image \"ghcr.io/x/y:nope\""
	if got := f.Decide(pull); got != gateAccepted {
		t.Errorf("image-pull BackOff with no class: gate %q; want accepted", got)
	}

	off := newFilter(newFilterConfig(nil, nil, nil, filterThresholds{backoffMinCount: 3, settle: -1}))
	if got := off.Decide(settleBackoff(t0, 10*time.Second, 3)); got != gateAccepted {
		t.Errorf("--settle=0: gate %q; want accepted", got)
	}
	def := newFilterConfig(nil, nil, nil, filterThresholds{})
	if def.settle != defaultSettle {
		t.Errorf("unset settle = %s; want the default %s", def.settle, defaultSettle)
	}
}

// TestDispatcherSettleHoldsAndCountsASelfResolvedBlip is the 2026-08-10
// incident end to end: four BackOffs inside 110 seconds, then nothing. No
// session, no dedup entry, four held sightings, and once the pod has been
// quiet for the period plus the slack the sweep counts one self_resolved.
func TestDispatcherSettleHoldsAndCountsASelfResolvedBlip(t *testing.T) {
	disp, m, injectCount, injected := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3, settle: 2 * time.Minute})
	disp.settles = newSettleMemo(0, 0)
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	disp.now = func() time.Time { return now }
	ctx := context.Background()

	for i, lasted := range []time.Duration{0, 10 * time.Second, 30 * time.Second, 110 * time.Second} {
		now = t0.Add(lasted)
		disp.Dispatch(ctx, settleBackoff(t0, lasted, i+1))
	}
	if *injectCount != 0 {
		t.Fatalf("a 110s blip fired %d injects; want 0", *injectCount)
	}
	if got := disp.dedup.Len(); got != 0 {
		t.Errorf("held events left %d dedup entries; want 0", got)
	}
	if got := testutil.ToFloat64(m.eventsFiltered.WithLabelValues("test-cluster", "", "", string(gateSettle))); got != 2 {
		t.Errorf("events_filtered_total{gate=settle} = %v; want 2 (counts 3 and 4; 1 and 2 were the count gate's)", got)
	}
	if got := disp.settles.Len(); got != 1 {
		t.Fatalf("settle memo holds %d keys; want 1", got)
	}

	// Inside the period plus the slack nothing is decided.
	now = t0.Add(110*time.Second + 2*time.Minute)
	disp.Sweep(ctx)
	if got := disp.settles.Len(); got != 1 {
		t.Errorf("sweep inside the quiet window dropped the hold")
	}
	now = t0.Add(110*time.Second + 2*time.Minute + settleQuietSlack)
	disp.Sweep(ctx)
	if got := testutil.ToFloat64(m.settleOutcomes.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", settleOutcomeSelfResolved)); got != 1 {
		t.Errorf("settle_outcomes_total{outcome=self_resolved} = %v; want 1", got)
	}
	if got := disp.settles.Len(); got != 0 {
		t.Errorf("settle memo still holds %d keys after the sweep; want 0", got)
	}

	// The same pod blips again forty minutes on. kubelet kept the event
	// object, so the sighting arrives with the old first sighting and a count
	// past the threshold; measured from the blip's own start it is held, and
	// a blip that lasts the period is forwarded with that start as first_seen.
	for i, lasted := range []time.Duration{40 * time.Minute, 40*time.Minute + 20*time.Second, 40*time.Minute + 70*time.Second} {
		now = t0.Add(lasted)
		disp.Dispatch(ctx, settleBackoff(t0, lasted, 5+i))
	}
	if *injectCount != 0 {
		t.Fatalf("a repeat blip 70s long fired %d injects; want 0", *injectCount)
	}
	now = t0.Add(40*time.Minute + 150*time.Second)
	disp.Dispatch(ctx, settleBackoff(t0, 40*time.Minute+150*time.Second, 8))
	if *injectCount != 1 {
		t.Fatalf("a repeat blip past the period fired %d injects; want 1", *injectCount)
	}
	if got := (*injected)[0].FirstSeen; !got.Equal(t0.Add(40 * time.Minute)) {
		t.Errorf("repeat blip forwarded with first_seen=%s; want the blip's own start %s", got, t0.Add(40*time.Minute))
	}
	// A replay of the cleared blip (nothing newer than the tombstone) adjusts
	// nothing and is held on its own short span, as before.
	fresh, _, count2, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3, settle: 2 * time.Minute})
	fresh.settles = newSettleMemo(0, 0)
	fresh.now = func() time.Time { return now }
	now = t0
	fresh.Dispatch(ctx, settleBackoff(t0, 40*time.Second, 3))
	now = t0.Add(time.Hour)
	fresh.Sweep(ctx)
	fresh.Dispatch(ctx, settleBackoff(t0, 40*time.Second, 3))
	if *count2 != 0 {
		t.Errorf("a replayed cleared blip fired %d injects; want 0", *count2)
	}
	if got := fresh.settles.Len(); got != 0 {
		t.Errorf("a replayed cleared blip opened %d hold(s); want none (it would expire into a second self_resolved)", got)
	}
	// The replay left the tombstone standing, so the live blip that follows
	// is measured from its own first sighting and held, not forwarded on the
	// object's hour-old span.
	now = t0.Add(time.Hour + 30*time.Second)
	fresh.Dispatch(ctx, settleBackoff(t0, time.Hour+30*time.Second, 4))
	if *count2 != 0 {
		t.Errorf("a live blip after a replay fired %d injects; want 0 (held from its own start)", *count2)
	}
	now = t0.Add(time.Hour + 30*time.Second + 2*time.Minute)
	fresh.Dispatch(ctx, settleBackoff(t0, time.Hour+30*time.Second+2*time.Minute, 6))
	if *count2 != 1 {
		t.Errorf("a live blip lasting the period fired %d injects; want 1", *count2)
	}
}

// TestDispatcherSettleForwardsALastingFailureWithItsContext: the same pod
// keeps crashing. The sighting that spans the period is forwarded, once, with
// the first sighting and the count the gate held, and the memo counts one
// forwarded and no self_resolved however long the sweep runs after.
func TestDispatcherSettleForwardsALastingFailureWithItsContext(t *testing.T) {
	disp, m, injectCount, injected := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3, settle: 2 * time.Minute})
	disp.settles = newSettleMemo(0, 0)
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	disp.now = func() time.Time { return now }
	disp.dedup.now = func() time.Time { return now }
	ctx := context.Background()

	for i, lasted := range []time.Duration{0, 10 * time.Second, 30 * time.Second, 70 * time.Second, 150 * time.Second} {
		now = t0.Add(lasted)
		disp.Dispatch(ctx, settleBackoff(t0, lasted, i+1))
	}
	if *injectCount != 1 {
		t.Fatalf("a lasting crash loop fired %d injects; want 1", *injectCount)
	}
	// The payload's first_seen is the event's own, from before the hold. Its
	// count is the dedup window's as it always was (1 for a new incident; it
	// is the ledger's occurrences column), not kubelet's, so the hold changes
	// nothing there either.
	got := (*injected)[0]
	if !got.FirstSeen.Equal(t0) || !got.LastSeen.Equal(t0.Add(150*time.Second)) || got.Count != 1 {
		t.Errorf("forwarded payload first_seen=%s last_seen=%s count=%d; want %s, %s and 1", got.FirstSeen, got.LastSeen, got.Count, t0, t0.Add(150*time.Second))
	}
	if got := testutil.ToFloat64(m.settleOutcomes.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", settleOutcomeForwarded)); got != 1 {
		t.Errorf("settle_outcomes_total{outcome=forwarded} = %v; want 1", got)
	}
	// The next sighting, inside the dedup window (5m in this fixture, 90s
	// after the forwarded one), is the window's to suppress, as before.
	now = t0.Add(4 * time.Minute)
	disp.Dispatch(ctx, settleBackoff(t0, 4*time.Minute, 6))
	if *injectCount != 1 {
		t.Errorf("a later sighting inside the window fired again (%d injects)", *injectCount)
	}
	now = t0.Add(time.Hour)
	disp.Sweep(ctx)
	if got := testutil.ToFloat64(m.settleOutcomes.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", settleOutcomeSelfResolved)); got != 0 {
		t.Errorf("a forwarded key was later counted self_resolved (%v)", got)
	}
}

// TestDispatcherSettleRemembersACountGatedBlip: a blip the count gate stopped
// at two sightings leaves memo state too, so its repeat inside the event's
// TTL is measured from its own start rather than forwarded at count three.
func TestDispatcherSettleRemembersACountGatedBlip(t *testing.T) {
	// Both of the family's count gates: BackOff's and Unhealthy's.
	for _, reason := range []string{"BackOff", "Unhealthy"} {
		t.Run(reason, func(t *testing.T) {
			disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3, unhealthyMinCount: 3, settle: 2 * time.Minute})
			disp.settles = newSettleMemo(0, 0)
			t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
			now := t0
			disp.now = func() time.Time { return now }
			ctx := context.Background()
			sighting := func(lasted time.Duration, count int) TriageEvent {
				ev := settleBackoff(t0, lasted, count)
				ev.Key.Reason = reason
				return ev
			}
			disp.Dispatch(ctx, sighting(0, 1))
			now = t0.Add(10 * time.Second)
			disp.Dispatch(ctx, sighting(10*time.Second, 2))
			now = t0.Add(30 * time.Minute)
			disp.Sweep(ctx) // cleared: a tombstone stands
			now = t0.Add(40 * time.Minute)
			disp.Dispatch(ctx, sighting(40*time.Minute, 3))
			if *injectCount != 0 {
				t.Errorf("a repeat of a count-gated %s blip fired %d injects on its first sighting; want 0", reason, *injectCount)
			}
		})
	}
}

func TestSettleMemoEvictsItsOldestHoldWhenFull(t *testing.T) {
	m := newSettleMemo(2, 0)
	t0 := time.Now()
	for i, uid := range []string{"a", "b", "c"} {
		ev := backoffEvent(3)
		ev.Key.UID = uid
		m.Hold(ev, "CrashLoopBackOff", t0.Add(time.Duration(i)*time.Second))
	}
	if m.Len() != 2 {
		t.Fatalf("memo holds %d; want the cap of 2", m.Len())
	}
	oldest := backoffEvent(3)
	oldest.Key.UID = "a"
	if m.Release(oldest, "CrashLoopBackOff") {
		t.Error("the oldest hold survived the cap")
	}
	var nilMemo *settleMemo
	nilMemo.Hold(oldest, "CrashLoopBackOff", t0)
	if nilMemo.Release(oldest, "CrashLoopBackOff") || nilMemo.Expire(t0, 0) != nil || nilMemo.Len() != 0 {
		t.Error("a nil memo is not inert")
	}
}

// TestDispatcherSweepSendsOneQuietFollowUp: an alert goes out, the object is
// sighted once more, then nothing. At --quiet-after the sweep sends one
// k8s-event-followup on the alert's own session, naming the object and the
// last sighting, and never a second one; a sighting inside the period, an
// informer that has not synced or has just resumed, a cluster silent for the
// period, --quiet-after 0 and shared mode each send none.
func TestDispatcherSweepSendsOneQuietFollowUp(t *testing.T) {
	disp, m, injectCount, injected := newCountingDispatcher(t, filterThresholds{backoffMinCount: 1, settle: -1})
	disp.quietAfter = 15 * time.Minute
	disp.cluster = targetCluster{Name: "test-cluster", ProjectID: "p", Location: "us-central1"}
	disp.readyAt.Store(1) // delivering since long before the fixture's clock
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	disp.now = func() time.Time { return now }
	disp.dedup.now = func() time.Time { return now }
	ctx := context.Background()

	first := backoffEvent(3)
	first.KindOfObject = "Pod"
	first.FirstSeen, first.LastSeen = t0, t0
	disp.Dispatch(ctx, first)
	if *injectCount != 1 {
		t.Fatalf("alert: %d injects; want 1", *injectCount)
	}
	now = t0.Add(time.Minute)
	again := first
	again.Count, again.LastSeen = 4, now
	disp.Dispatch(ctx, again) // deduplicated, but it moves LastSeen

	now = t0.Add(10 * time.Minute)
	disp.Sweep(ctx)
	if *injectCount != 1 {
		t.Fatalf("a sweep 9m after the last sighting sent a follow-up")
	}
	// Before the informer's first list has completed, nothing is quiet yet,
	// even on a cluster that has just spoken.
	now = t0.Add(time.Minute + 15*time.Minute)
	heard := backoffEvent(1)
	heard.Key.UID, heard.Key.Reason = "pod-heard", "Scheduled" // the cluster speaks inside the quiet period; not a reason on the list, so no alert
	disp.Dispatch(ctx, heard)
	disp.readyAt.Store(0)
	disp.Sweep(ctx)
	if *injectCount != 1 {
		t.Fatalf("a sweep before the informer synced sent a follow-up")
	}
	// A watch that has just returned re-delivers the live entries' events in
	// its relist; the sweep waits followupResumeGrace behind it.
	disp.readyAt.Store(now.UnixNano())
	disp.Sweep(ctx)
	if *injectCount != 1 {
		t.Fatalf("a sweep inside the resume grace sent a follow-up")
	}
	disp.readyAt.Store(1) // delivering since long before the fixture's clock
	disp.Sweep(ctx)
	if *injectCount != 2 {
		t.Fatalf("at quiet-after: %d injects; want 2 (the alert and one follow-up)", *injectCount)
	}
	fu := (*injected)[1]
	if fu.Kind != injectKindFollowup || fu.Reason != "CrashLoopBackOff" || fu.Namespace != "default" || fu.Name != "api" || fu.KindOfObject != "Pod" {
		t.Errorf("follow-up payload = %+v; want kind %s naming default/Pod/api on CrashLoopBackOff", fu, injectKindFollowup)
	}
	// first_seen and count are the alerted event's own (kubelet's 3), as the
	// alert carried them; last_seen is the last real sighting.
	if !fu.LastSeen.Equal(t0.Add(time.Minute)) || !fu.FirstSeen.Equal(t0) || fu.Count != 3 {
		t.Errorf("follow-up first_seen=%s last_seen=%s count=%d; want %s, %s, 3", fu.FirstSeen, fu.LastSeen, fu.Count, t0, t0.Add(time.Minute))
	}
	if fu.Cluster != "test-cluster" || fu.Project != "p" || fu.Location != "us-central1" {
		t.Errorf("follow-up cluster labels = %s/%s/%s; want the dispatcher's", fu.Cluster, fu.Project, fu.Location)
	}
	if got := testutil.ToFloat64(m.followups.WithLabelValues("test-cluster", "p", "us-central1", "CrashLoopBackOff", followupOutcomeSent)); got != 1 {
		t.Errorf("followups_total{outcome=sent} = %v; want 1", got)
	}
	now = t0.Add(2 * time.Hour)
	disp.Dispatch(ctx, heard) // the cluster speaks, so the sweep may judge
	disp.Sweep(ctx)
	if *injectCount != 2 {
		t.Errorf("a second sweep sent another follow-up (%d injects)", *injectCount)
	}
	// A cluster that has said nothing for the quiet period is not judged:
	// the watch may be down for a reason the watcher does not report.
	silent := backoffEvent(3)
	silent.Key.UID = "pod-3"
	now = t0.Add(3 * time.Hour)
	disp.Dispatch(ctx, silent) // alert: injects 3
	now = t0.Add(4 * time.Hour)
	disp.Sweep(ctx)
	if *injectCount != 3 {
		t.Errorf("a sweep on a cluster silent for an hour sent a follow-up (%d injects)", *injectCount)
	}
	now = t0.Add(4*time.Hour + time.Minute)
	disp.Dispatch(ctx, heard) // a delivery, no alert
	disp.Sweep(ctx)
	if *injectCount != 4 {
		t.Errorf("once the cluster spoke again the quiet incident got no follow-up (%d injects)", *injectCount)
	}

	// Off at zero, and never in shared mode.
	disp.quietAfter = 0
	disp.dedup.MarkQuietNotified(EventKey{}) // no-op on an unknown key
	fresh := backoffEvent(3)
	fresh.Key.UID = "pod-2"
	now = t0.Add(5 * time.Hour)
	disp.Dispatch(ctx, fresh) // alert: injects 5
	now = t0.Add(5*time.Hour + time.Minute)
	disp.Dispatch(ctx, heard)
	disp.Sweep(ctx)
	if *injectCount != 5 {
		t.Errorf("with quiet-after=0 the sweep sent a follow-up (%d injects)", *injectCount)
	}
	disp.quietAfter = 15 * time.Minute
	disp.mode = "shared"
	now = t0.Add(5*time.Hour + 16*time.Minute)
	disp.Dispatch(ctx, heard)
	disp.Sweep(ctx)
	if *injectCount != 5 {
		t.Errorf("in shared mode the sweep sent a follow-up (%d injects)", *injectCount)
	}
}

// TestDispatcherSweepInDryRunCallsNoDaemon: a quiet incident restored from a
// live run's snapshot reaches a dry run's sweep, which has no injector; it
// prints the follow-up, marks the entry and asks the daemon nothing.
func TestDispatcherSweepInDryRunCallsNoDaemon(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 1, settle: -1})
	disp.quietAfter = 15 * time.Minute
	disp.readyAt.Store(1)
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	disp.now = func() time.Time { return now }
	disp.dedup.now = func() time.Time { return now }
	ctx := context.Background()
	alert := backoffEvent(3)
	alert.FirstSeen, alert.LastSeen = t0, t0
	disp.Dispatch(ctx, alert) // live: the session is bound, follow-ups supported
	if *injectCount != 1 {
		t.Fatalf("alert: %d injects; want 1", *injectCount)
	}
	disp.dryRun, disp.injector = true, nil
	now = t0.Add(16 * time.Minute)
	heard := backoffEvent(1)
	heard.Key.UID, heard.Key.Reason = "pod-heard", "Scheduled"
	disp.Dispatch(ctx, heard) // the cluster speaks
	disp.Sweep(ctx)           // must not dereference the nil injector
	if got := disp.dedup.QuietIncidents(now, disp.quietAfter); len(got) != 0 {
		t.Errorf("after a dry-run sweep %d incidents are still quiet candidates; want 0 (marked)", len(got))
	}
	if *injectCount != 1 {
		t.Errorf("a dry-run sweep reached the daemon (%d injects)", *injectCount)
	}
}

// TestDispatcherSweepHearsAnObjectTheScaleUpHoldKeeps: a FailedScheduling
// alert followed by a TriggeredScaleUp has its later sightings held, not
// accepted; they still move the quiet clock, so a Pending pod the scheduler
// keeps re-emitting for is not reported quiet during the hold.
func TestDispatcherSweepHearsAnObjectTheScaleUpHoldKeeps(t *testing.T) {
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	disp, _, injectCount := newScaleUpDispatcher(t, filterThresholds{failedSchedulingMinCount: 1}, &now)
	disp.quietAfter = 15 * time.Minute
	disp.readyAt.Store(1)
	disp.now = func() time.Time { return now }
	disp.dedup.now = func() time.Time { return now }
	ctx := context.Background()
	disp.Dispatch(ctx, failedSchedulingEvent("pod-1", 1, now)) // the alert
	if *injectCount != 1 {
		t.Fatalf("alert: %d injects; want 1", *injectCount)
	}
	now = t0.Add(3 * time.Minute)
	disp.Dispatch(ctx, autoscalerEvent("pod-1", "TriggeredScaleUp", now)) // the hold begins
	for i := 1; i <= 10; i++ {
		now = t0.Add(time.Duration(3+i) * time.Minute)
		disp.Dispatch(ctx, failedSchedulingEvent("pod-1", 1+i, now)) // held, not accepted
	}
	if *injectCount != 1 {
		t.Fatalf("held sightings fired %d injects; want 1", *injectCount)
	}
	now = t0.Add(16 * time.Minute) // 16m after the accepted sighting, 3m after the last held one
	disp.Sweep(ctx)
	if *injectCount != 1 {
		t.Fatalf("a sweep called an object the scale-up hold still hears quiet")
	}
	heard := backoffEvent(1)
	heard.Key.UID, heard.Key.Reason = "pod-heard", "Scheduled"
	now = t0.Add(29 * time.Minute) // 16m after the last held sighting
	disp.Dispatch(ctx, heard)      // the cluster speaks
	if quiet := disp.dedup.QuietIncidents(now, disp.quietAfter); len(quiet) != 1 || !quiet[0].LastSeen.Equal(t0.Add(13*time.Minute)) {
		t.Fatalf("quiet incidents = %+v; want one quiet since the last held sighting %s", quiet, t0.Add(13*time.Minute))
	}
	disp.Sweep(ctx)
	if *injectCount != 2 {
		t.Errorf("once the held sightings stopped: %d injects; want 2 (the alert and one follow-up)", *injectCount)
	}
}

// TestSightedIgnoresAReplay: a relist re-delivers a held sighting with the
// timestamp it had; that moves nothing, as it moves nothing in Observe.
func TestSightedIgnoresAReplay(t *testing.T) {
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	c, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatal(err)
	}
	c.now = func() time.Time { return t0 }
	key := EventKey{UID: "pod-1", Reason: "FailedScheduling"}
	c.Observe(key, "0/3 nodes are available", t0)
	c.BindSession(key, "", "sess-1")
	c.MarkFollowupSupported(key, "")
	c.now = func() time.Time { return t0.Add(10 * time.Minute) }
	c.Sighted(key, "", t0.Add(10*time.Minute))
	c.now = func() time.Time { return t0.Add(20 * time.Minute) }
	// The same event again (a replay), then a key with no entry: no-ops.
	c.Sighted(key, "", t0.Add(10*time.Minute))
	c.Sighted(EventKey{UID: "pod-9", Reason: "FailedScheduling"}, "", t0.Add(20*time.Minute))
	if got := c.QuietIncidents(t0.Add(24*time.Minute), 15*time.Minute); len(got) != 0 {
		t.Errorf("14m after the held sighting the incident is quiet: %+v", got)
	}
	got := c.QuietIncidents(t0.Add(26*time.Minute), 15*time.Minute)
	if len(got) != 1 || !got[0].LastSeen.Equal(t0.Add(10*time.Minute)) {
		t.Errorf("quiet incidents = %+v; want one quiet since %s (the replay moved it)", got, t0.Add(10*time.Minute))
	}
}

// TestQuietIncidentsSurviveASnapshot: the follow-up's state rides the dedup
// entry, so a restart in the quiet period neither loses the incident's object
// nor sends the follow-up twice.
func TestQuietIncidentsSurviveASnapshot(t *testing.T) {
	path := t.TempDir() + "/dedup.json"
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	c, err := newDedupCache(24*time.Hour, path)
	if err != nil {
		t.Fatal(err)
	}
	c.now = func() time.Time { return t0 }
	key := EventKey{UID: "pod-1", Reason: "BackOff"}
	msg := "Back-off restarting failed container app in pod api"
	c.Observe(key, msg, t0)
	c.BindSession(key, msg, "sess-1")
	c.BindObject(key, msg, "default", "Pod", "api", t0.Add(-2*time.Minute), 7)
	c.MarkFollowupSupported(key, msg)
	c.now = func() time.Time { return t0.Add(3 * time.Minute) }
	c.Sighted(key, msg, t0.Add(3*time.Minute)) // a sighting a gate stopped
	if err := c.Snapshot(); err != nil {
		t.Fatal(err)
	}
	restored, err := newDedupCache(24*time.Hour, path)
	if err != nil {
		t.Fatal(err)
	}
	quiet := restored.QuietIncidents(t0.Add(20*time.Minute), 15*time.Minute)
	if len(quiet) != 1 || quiet[0].SessionID != "sess-1" || quiet[0].Name != "api" || quiet[0].Key.Reason != "CrashLoopBackOff" {
		t.Fatalf("restored quiet incidents = %+v; want one for sess-1 naming api", quiet)
	}
	if !quiet[0].LastSeen.Equal(t0.Add(3 * time.Minute)) {
		t.Errorf("quiet incident last_seen=%s; want the gate-stopped sighting's %s", quiet[0].LastSeen, t0.Add(3*time.Minute))
	}
	if !quiet[0].FirstSeen.Equal(t0.Add(-2*time.Minute)) || quiet[0].Count != 7 {
		t.Errorf("quiet incident first_seen=%s count=%d; want the alerted event's own %s and 7", quiet[0].FirstSeen, quiet[0].Count, t0.Add(-2*time.Minute))
	}
	restored.MarkQuietNotified(quiet[0].Key)
	if err := restored.Snapshot(); err != nil {
		t.Fatal(err)
	}
	again, err := newDedupCache(24*time.Hour, path)
	if err != nil {
		t.Fatal(err)
	}
	if got := again.QuietIncidents(t0.Add(time.Hour), 15*time.Minute); len(got) != 0 {
		t.Errorf("a notified incident came back as quiet after a restart: %+v", got)
	}
	// An entry with no session (the alert never went out) and one the daemon
	// policy-filtered are not incidents anyone was told about. On the same
	// clock as the sweep, so the flags and not the wall clock decide.
	again.now = func() time.Time { return t0 }
	none := EventKey{UID: "pod-2", Reason: "BackOff"}
	again.Observe(none, msg, t0)
	filtered := EventKey{UID: "pod-3", Reason: "BackOff"}
	again.Observe(filtered, msg, t0)
	again.BindSession(filtered, msg, "sess-3")
	again.MarkFollowupSupported(filtered, msg)
	again.MarkPolicyFiltered(filtered, msg)
	older := EventKey{UID: "pod-4", Reason: "BackOff"}
	again.Observe(older, msg, t0)
	again.BindSession(older, msg, "sess-4") // a daemon that listed no features
	if got := again.QuietIncidents(t0.Add(time.Hour), 15*time.Minute); len(got) != 0 {
		t.Errorf("unalerted entries, or one on an older daemon, reported quiet: %+v", got)
	}
}

// TestDispatcherSweepCountsAnUnroutedFollowUp: a daemon that answers the
// follow-up `unrouted` posted nothing; the incident is marked (there is no
// thread to tell) and the outcome says so rather than `sent`.
func TestDispatcherSweepCountsAnUnroutedFollowUp(t *testing.T) {
	injects := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == healthzPath {
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", injectFeaturesKey: []string{injectFeatureFollowup}})
			return
		}
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "sess-u"})
			return
		}
		injects++
		w.WriteHeader(http.StatusOK)
		if injects == 1 {
			_ = json.NewEncoder(w).Encode(injectResponse{Status: "injected", Features: []string{injectFeatureFollowup}})
			return
		}
		_ = json.NewEncoder(w).Encode(injectResponse{Status: injectStatusUnrouted})
	}))
	t.Cleanup(server.Close)
	inj, err := newInjector(injectorConfig{daemonURL: server.URL, bearerToken: "t", httpClient: server.Client()})
	if err != nil {
		t.Fatal(err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	m := newMetrics()
	disp := &dispatcher{
		filter: newFilter(newFilterConfig(nil, nil, nil, filterThresholds{backoffMinCount: 1, settle: -1})),
		dedup:  dedup, pullClasses: newPullClassMemo(0, 0), injector: inj, metrics: m,
		mode: "per-incident", quietAfter: 15 * time.Minute, now: func() time.Time { return now },
		cluster: targetCluster{Name: "test-cluster"},
	}
	disp.dedup.now = func() time.Time { return now }
	disp.readyAt.Store(1) // delivering since long before the fixture's clock
	disp.Dispatch(context.Background(), backoffEvent(3))
	now = t0.Add(time.Hour)
	speaks := backoffEvent(1)
	speaks.Key.UID = "pod-other"
	disp.Dispatch(context.Background(), speaks) // the cluster speaks: a second alert, injects 2
	disp.Sweep(context.Background())
	disp.Sweep(context.Background())
	injects-- // the second alert is not what this test counts
	if injects != 2 {
		t.Errorf("%d injects; want the alert and one follow-up", injects)
	}
	if got := testutil.ToFloat64(m.followups.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", followupOutcomeUnrouted)); got != 1 {
		t.Errorf("followups_total{outcome=unrouted} = %v; want 1", got)
	}
	if got := testutil.ToFloat64(m.followups.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", followupOutcomeSent)); got != 0 {
		t.Errorf("followups_total{outcome=sent} = %v; want 0", got)
	}
}

// TestDispatcherSweepSendsNoFollowUpToAnOlderDaemon: a daemon that answers
// the alert without the followup feature would take a k8s-event-followup
// down its event path and post a second alert, so it gets none.
func TestDispatcherSweepSendsNoFollowUpToAnOlderDaemon(t *testing.T) {
	injectCount := 0
	healthzLists := true // flipped below: the daemon of the moment, as /healthz tells it
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == healthzPath {
			if healthzLists {
				_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", injectFeaturesKey: []string{injectFeatureFollowup}})
			} else {
				_, _ = w.Write([]byte(`{"status": "ok", "inject_kinds": ["k8s-event", "k8s-event-followup"]}`)) // an older daemon: the kind, no feature
			}
			return
		}
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "sess-old"})
			return
		}
		injectCount++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status": "injected"}`)) // the alert's answer lists no features
	}))
	t.Cleanup(server.Close)
	inj, err := newInjector(injectorConfig{daemonURL: server.URL, bearerToken: "t", httpClient: server.Client()})
	if err != nil {
		t.Fatal(err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	disp := &dispatcher{
		filter: newFilter(newFilterConfig(nil, nil, nil, filterThresholds{backoffMinCount: 1, settle: -1})),
		dedup:  dedup, pullClasses: newPullClassMemo(0, 0), injector: inj, metrics: newMetrics(),
		mode: "per-incident", quietAfter: 15 * time.Minute, now: func() time.Time { return now },
	}
	disp.dedup.now = func() time.Time { return now }
	disp.readyAt.Store(1) // delivering since long before the fixture's clock
	disp.Dispatch(context.Background(), backoffEvent(3))
	now = t0.Add(16 * time.Minute)
	speaks := backoffEvent(1)
	speaks.Key.UID = "pod-other"
	disp.Dispatch(context.Background(), speaks) // the cluster speaks (a second alert); the first incident is 16m quiet
	disp.Sweep(context.Background())
	if injectCount != 2 {
		t.Errorf("%d injects; want the two alerts alone: /healthz lists the feature but the alerts' answers did not, so no session is marked", injectCount)
	}
	// The session-level promise is not enough on its own either: a daemon
	// rolled back since the alert lists no feature on /healthz and gets none.
	disp.dedup.MarkFollowupSupported(backoffEvent(3).Key, backoffEvent(3).Message)
	healthzLists = false
	disp.Sweep(context.Background())
	if injectCount != 2 {
		t.Errorf("%d injects; a daemon whose /healthz lists no follow-up feature was sent one", injectCount)
	}
	// Both conditions met, the follow-up goes.
	healthzLists = true
	disp.Sweep(context.Background())
	if injectCount != 3 {
		t.Errorf("%d injects; want the follow-up once both the session's answer and /healthz list the feature", injectCount)
	}
}

// TestDispatcherSweepRetriesAFailedFollowUp: an inject the daemon fails is
// counted error, left unmarked, and sent again on the next sweep.
func TestDispatcherSweepRetriesAFailedFollowUp(t *testing.T) {
	injects, failNext := 0, false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == healthzPath {
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", injectFeaturesKey: []string{injectFeatureFollowup}})
			return
		}
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "sess-r"})
			return
		}
		injects++
		if failNext {
			failNext = false
			w.WriteHeader(http.StatusBadGateway)
			return
		}
		_ = json.NewEncoder(w).Encode(injectResponse{Status: "injected", Features: []string{injectFeatureFollowup}})
	}))
	t.Cleanup(server.Close)
	inj, err := newInjector(injectorConfig{daemonURL: server.URL, bearerToken: "t", httpClient: server.Client()})
	if err != nil {
		t.Fatal(err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatal(err)
	}
	t0 := time.Date(2026, 8, 10, 15, 27, 0, 0, time.UTC)
	now := t0
	m := newMetrics()
	disp := &dispatcher{
		filter: newFilter(newFilterConfig(nil, nil, nil, filterThresholds{backoffMinCount: 1, settle: -1})),
		dedup:  dedup, pullClasses: newPullClassMemo(0, 0), injector: inj, metrics: m,
		mode: "per-incident", quietAfter: 15 * time.Minute, now: func() time.Time { return now },
		cluster: targetCluster{Name: "test-cluster"},
	}
	disp.dedup.now = func() time.Time { return now }
	disp.readyAt.Store(1)                                // delivering since long before the fixture's clock
	disp.Dispatch(context.Background(), backoffEvent(3)) // inject 1: the alert
	now = t0.Add(16 * time.Minute)
	speaks := backoffEvent(1)
	speaks.Key.UID, speaks.Key.Reason = "pod-other", "Scheduled"
	disp.Dispatch(context.Background(), speaks)
	failNext = true
	disp.Sweep(context.Background()) // inject 2: the follow-up, refused
	if got := testutil.ToFloat64(m.followups.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", followupOutcomeError)); got != 1 {
		t.Errorf("followups_total{outcome=error} = %v; want 1", got)
	}
	disp.Sweep(context.Background()) // inject 3: sent
	if injects != 3 {
		t.Errorf("%d injects; want the alert, the refused follow-up and its retry", injects)
	}
	if got := testutil.ToFloat64(m.followups.WithLabelValues("test-cluster", "", "", "CrashLoopBackOff", followupOutcomeSent)); got != 1 {
		t.Errorf("followups_total{outcome=sent} = %v; want 1", got)
	}
	disp.Sweep(context.Background())
	if injects != 3 {
		t.Errorf("a sent follow-up was sent again (%d injects)", injects)
	}
}

func TestParseFlagsSettleAndQuietAfter(t *testing.T) {
	f, err := parseFlags(nil)
	if err != nil {
		t.Fatal(err)
	}
	if f.settle != defaultSettle || f.quietAfter != defaultQuietAfter {
		t.Errorf("defaults: settle=%s quiet-after=%s; want %s and %s", f.settle, f.quietAfter, defaultSettle, defaultQuietAfter)
	}
	if got := f.filterThresholds().settle; got != defaultSettle {
		t.Errorf("default --settle reaches the filter as %s; want %s", got, defaultSettle)
	}
	f, err = parseFlags([]string{"--settle=0", "--quiet-after=0"})
	if err != nil {
		t.Fatal(err)
	}
	if got := f.filterThresholds().settle; got >= 0 {
		t.Errorf("--settle=0 reaches the filter as %s; want a negative (off)", got)
	}
	if cfg := newFilterConfig(nil, nil, nil, f.filterThresholds()); cfg.settle != 0 {
		t.Errorf("--settle=0 left the gate at %s", cfg.settle)
	}
	f, err = parseFlags([]string{"--settle=90s", "--quiet-after=20m", "--daemon-url=http://d", "--token-env=T", "--owner=o", "--cluster-name=c"})
	if err != nil {
		t.Fatal(err)
	}
	if f.filterThresholds().settle != 90*time.Second || f.quietAfter != 20*time.Minute {
		t.Errorf("explicit values did not travel: %+v", f)
	}
	if err := f.validate(); err != nil {
		t.Errorf("validate: %v", err)
	}
	f.settle = -time.Second
	if err := f.validate(); err == nil {
		t.Error("a negative --settle validated")
	}
}
