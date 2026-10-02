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
	"testing"
	"time"
)

func TestScaleUpVerdictFor(t *testing.T) {
	cases := map[string]scaleUpVerdict{
		"TriggeredScaleUp":  scaleUpTriggered,
		"NotTriggerScaleUp": scaleUpDeclined,
		"FailedScheduling":  scaleUpNone,
		"BackOff":           scaleUpNone,
		"":                  scaleUpNone,
	}
	for reason, want := range cases {
		if got := scaleUpVerdictFor(reason); got != want {
			t.Errorf("scaleUpVerdictFor(%q) = %v; want %v", reason, got, want)
		}
	}
}

func TestScaleUpMemoRecordsLatestByEventTime(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 0)
	m.now = func() time.Time { return now }

	m.Record("default", "pod-1", scaleUpTriggered, now.Add(-time.Minute))
	m.Record("default", "pod-1", scaleUpDeclined, now)
	if got := m.Lookup("default", "pod-1"); got.Verdict != scaleUpDeclined || !got.At.Equal(now) {
		t.Errorf("after a newer decline, mark = %+v; want declined at %v", got, now)
	}

	// A replayed older mark must not overwrite the newer verdict.
	m.Record("default", "pod-1", scaleUpTriggered, now.Add(-2*time.Minute))
	if got := m.Lookup("default", "pod-1"); got.Verdict != scaleUpDeclined {
		t.Errorf("older replayed mark overwrote the newer one: %+v", got)
	}

	// A newer TriggeredScaleUp does supersede a decline: the autoscaler changed its mind.
	m.Record("default", "pod-1", scaleUpTriggered, now.Add(time.Minute))
	if got := m.Lookup("default", "pod-1"); got.Verdict != scaleUpTriggered {
		t.Errorf("newer trigger did not supersede the decline: %+v", got)
	}
}

func TestScaleUpMemoZeroTimestampIsNow(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 0)
	m.now = func() time.Time { return now }

	m.Record("default", "pod-1", scaleUpTriggered, time.Time{})
	if got := m.Lookup("default", "pod-1"); !got.At.Equal(now) {
		t.Errorf("zero event time recorded as %v; want now (%v)", got.At, now)
	}
}

func TestScaleUpMemoExpires(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(10*time.Minute, 0)
	m.now = func() time.Time { return now }

	m.Record("default", "pod-1", scaleUpDeclined, now)
	now = now.Add(11 * time.Minute)
	if got := m.Lookup("default", "pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("mark past ttl = %+v; want none", got)
	}
	if got := m.Len(); got != 0 {
		t.Errorf("expired entry left %d entries; want 0", got)
	}
}

func TestScaleUpMemoIsPerUID(t *testing.T) {
	m := newScaleUpMemo(0, 0)
	m.Record("default", "pod-1", scaleUpTriggered, time.Now())
	if got := m.Lookup("default", "pod-2"); got.Verdict != scaleUpNone {
		t.Errorf("unrelated pod inherited a mark: %+v", got)
	}
}

// TestScaleUpMemoIsPerNamespace: a mark is the pod's only in the namespace it
// was written in. The API server does not check that a mark's
// involvedObject.uid names a real object, so a mark written in one namespace
// against another namespace's pod UID must not be found by that pod.
func TestScaleUpMemoIsPerNamespace(t *testing.T) {
	m := newScaleUpMemo(0, 0)
	m.Record("tenant-a", "pod-1", scaleUpTriggered, time.Now())
	if got := m.Lookup("kube-system", "pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("a pod in another namespace inherited a mark written against its UID: %+v", got)
	}
	if got := m.Lookup("tenant-a", "pod-1"); got.Verdict != scaleUpTriggered {
		t.Errorf("the mark is not found in its own namespace: %+v", got)
	}
	if got := m.Lookup("", "pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("an empty namespace found a mark: %+v", got)
	}
}

func TestScaleUpMemoIsBounded(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 4)
	m.now = func() time.Time { return now }

	for i := 0; i < 20; i++ {
		m.Record("default", string(rune('a'+i)), scaleUpTriggered, now)
		now = now.Add(time.Second)
	}
	if got := m.Len(); got > 4 {
		t.Errorf("memo holds %d entries; want <= 4", got)
	}
	// The newest survives the eviction of the oldest.
	if got := m.Lookup("default", string(rune('a'+19))); got.Verdict != scaleUpTriggered {
		t.Errorf("newest entry was evicted: %+v", got)
	}
}

// TestScaleUpMemoEvictsFromTheNamespaceHoldingTheMost: the cap is one per
// cluster, so a burst of marks in one namespace must give up that namespace's
// own marks and not another's. kube-system's two marks are the oldest in the
// memo and survive a tenant writing five times the cap.
func TestScaleUpMemoEvictsFromTheNamespaceHoldingTheMost(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 8)
	m.now = func() time.Time { return now }

	m.Record("kube-system", "ks-1", scaleUpTriggered, now)
	now = now.Add(time.Second)
	m.Record("kube-system", "ks-2", scaleUpDeclined, now)
	for i := 0; i < 40; i++ {
		now = now.Add(time.Second)
		m.Record("tenant-a", fmt.Sprintf("forged-%02d", i), scaleUpTriggered, now)
	}
	if got := m.Len(); got > 8 {
		t.Errorf("memo holds %d entries; want <= 8", got)
	}
	if got := m.Lookup("kube-system", "ks-1"); got.Verdict != scaleUpTriggered {
		t.Errorf("kube-system's oldest mark was evicted by tenant-a's burst: %+v", got)
	}
	if got := m.Lookup("kube-system", "ks-2"); got.Verdict != scaleUpDeclined {
		t.Errorf("kube-system's second mark was evicted by tenant-a's burst: %+v", got)
	}
	if got := m.Lookup("tenant-a", "forged-39"); got.Verdict != scaleUpTriggered {
		t.Errorf("tenant-a's newest mark was not kept: %+v", got)
	}
	if got := m.Lookup("tenant-a", "forged-00"); got.Verdict != scaleUpNone {
		t.Errorf("tenant-a's oldest mark survived its own burst: %+v", got)
	}
}

// TestScaleUpMemoEvictionShareAgainstOneFloodingNamespace: a namespace loses
// a mark once the memo is full and no other namespace holds more, not once it
// holds more than the cap. At cap 8, kube-system's five marks are the most in
// the memo when tenant-a's fourth write fills it, so that write costs
// kube-system its oldest; the tie that follows goes to kube-system again as
// the older namespace; from then on tenant-a holds the most and spends only
// its own, and kube-system keeps three however long the flood runs.
func TestScaleUpMemoEvictionShareAgainstOneFloodingNamespace(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 8)
	m.now = func() time.Time { return now }

	for i := 1; i <= 5; i++ {
		now = now.Add(time.Second)
		m.Record("kube-system", fmt.Sprintf("ks-%d", i), scaleUpTriggered, now)
	}
	for i := 0; i < 3; i++ {
		now = now.Add(time.Second)
		m.Record("tenant-a", fmt.Sprintf("forged-%02d", i), scaleUpTriggered, now)
	}
	if got := m.Len(); got != 8 {
		t.Fatalf("memo holds %d entries before the crossing; want 8", got)
	}
	if got := m.Lookup("kube-system", "ks-1"); got.Verdict != scaleUpTriggered {
		t.Fatalf("kube-system's oldest mark was evicted before the memo was full: %+v", got)
	}

	// The fourth write fills the memo with kube-system holding the most.
	now = now.Add(time.Second)
	m.Record("tenant-a", "forged-03", scaleUpTriggered, now)
	if got := m.Lookup("kube-system", "ks-1"); got.Verdict != scaleUpNone {
		t.Errorf("kube-system held the most and kept its oldest mark: %+v", got)
	}
	if got := m.Lookup("kube-system", "ks-2"); got.Verdict != scaleUpTriggered {
		t.Errorf("kube-system lost more than its oldest mark on one write: %+v", got)
	}

	for i := 4; i < 40; i++ {
		now = now.Add(time.Second)
		m.Record("tenant-a", fmt.Sprintf("forged-%02d", i), scaleUpTriggered, now)
	}
	if got := m.Len(); got != 8 {
		t.Errorf("memo holds %d entries after the flood; want 8", got)
	}
	if got := m.Lookup("kube-system", "ks-2"); got.Verdict != scaleUpNone {
		t.Errorf("kube-system kept its second mark through the tie: %+v", got)
	}
	for i := 3; i <= 5; i++ {
		if got := m.Lookup("kube-system", fmt.Sprintf("ks-%d", i)); got.Verdict != scaleUpTriggered {
			t.Errorf("kube-system/ks-%d was evicted; want its share of three kept: %+v", i, got)
		}
	}
	if got := m.Lookup("tenant-a", "forged-39"); got.Verdict != scaleUpTriggered {
		t.Errorf("tenant-a's newest mark was not kept: %+v", got)
	}
	if got := m.Lookup("tenant-a", "forged-35"); got.Verdict != scaleUpTriggered {
		t.Errorf("tenant-a's share was cut below five: %+v", got)
	}
	if got := m.Lookup("tenant-a", "forged-34"); got.Verdict != scaleUpNone {
		t.Errorf("tenant-a kept more than its share: %+v", got)
	}
}

// TestScaleUpMemoEvictionTieGoesToTheOlderNamespace: between namespaces
// holding the same number of marks, the one whose oldest mark is older gives
// it up, and a third namespace's first mark displaces neither of the other's
// newer ones.
func TestScaleUpMemoEvictionTieGoesToTheOlderNamespace(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 4)
	m.now = func() time.Time { return now }

	// The clock advances with the stamps: a stamp ahead of the memo's clock
	// is read as now, which would collapse the four onto one instant.
	for _, mark := range []struct{ ns, uid string }{
		{"ns-a", "a-old"}, {"ns-b", "b-old"}, {"ns-a", "a-new"}, {"ns-b", "b-new"}, {"ns-c", "c-1"},
	} {
		now = now.Add(time.Second)
		m.Record(mark.ns, mark.uid, scaleUpTriggered, now)
	}

	if got := m.Len(); got != 4 {
		t.Errorf("memo holds %d entries; want 4", got)
	}
	if got := m.Lookup("ns-a", "a-old"); got.Verdict != scaleUpNone {
		t.Errorf("the older of the two equal namespaces kept its oldest mark: %+v", got)
	}
	for ns, uid := range map[string]string{"ns-a": "a-new", "ns-b": "b-old", "ns-c": "c-1"} {
		if got := m.Lookup(ns, uid); got.Verdict == scaleUpNone {
			t.Errorf("%s/%s was evicted; want kept", ns, uid)
		}
	}
	if got := m.Lookup("ns-b", "b-new"); got.Verdict != scaleUpTriggered {
		t.Errorf("ns-b/b-new was evicted; want kept: %+v", got)
	}
}

func TestScaleUpMemoNilAndEmptyUIDAreInert(t *testing.T) {
	var nilMemo *scaleUpMemo
	nilMemo.Record("default", "pod-1", scaleUpDeclined, time.Now())
	if got := nilMemo.Lookup("default", "pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("nil memo returned %+v; want the zero mark", got)
	}

	m := newScaleUpMemo(0, 0)
	m.Record("default", "", scaleUpDeclined, time.Now())
	m.Record("", "pod-1", scaleUpDeclined, time.Now())
	m.Record("default", "pod-1", scaleUpNone, time.Now())
	if got := m.Len(); got != 0 {
		t.Errorf("empty uid, empty namespace or none verdict recorded %d entries; want 0", got)
	}
}

// TestScaleUpMemoTTL: the memo outlives the dedup window whenever the window
// is shorter than the hold plus the staleness check, since a mark can be
// consulted for that long after it was recorded. Bounded by the window alone,
// the binary's own defaults would cut a 15m hold to 5m with nothing logged.
func TestScaleUpMemoTTL(t *testing.T) {
	tests := []struct {
		name        string
		dedupWindow time.Duration
		hold        time.Duration
		want        time.Duration
	}{
		{name: "binary defaults: 5m window, 15m hold", dedupWindow: 5 * time.Minute, hold: defaultScaleUpHold, want: defaultScaleUpHold + failedSchedulingStaleAfter},
		{name: "deployed install: 24h window keeps its window", dedupWindow: 24 * time.Hour, hold: defaultScaleUpHold, want: 24 * time.Hour},
		{name: "a longer hold raises the floor", dedupWindow: 24 * time.Hour, hold: 24 * time.Hour, want: 24*time.Hour + failedSchedulingStaleAfter},
		{name: "window equal to the floor keeps the floor", dedupWindow: defaultScaleUpHold + failedSchedulingStaleAfter, hold: defaultScaleUpHold, want: defaultScaleUpHold + failedSchedulingStaleAfter},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := scaleUpMemoTTL(tc.dedupWindow, tc.hold); got != tc.want {
				t.Errorf("scaleUpMemoTTL(%v, %v) = %v; want %v", tc.dedupWindow, tc.hold, got, tc.want)
			}
		})
	}
}

// TestScaleUpMemoFutureTimestampIsReadAsNow: a mark stamped ahead of the
// watcher's clock is recorded as of now. The hold is measured from the mark,
// so a stamp with a lead would hold the pod's FailedScheduling for the hold
// plus the lead, and the memo's expiry, aged from the same stamp, would keep
// the mark for as long again.
func TestScaleUpMemoFutureTimestampIsReadAsNow(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 0)
	m.now = func() time.Time { return now }

	m.Record("default", "pod-1", scaleUpTriggered, now.Add(48*time.Hour))
	if got := m.Lookup("default", "pod-1"); got.Verdict != scaleUpTriggered || !got.At.Equal(now) {
		t.Fatalf("a future mark was recorded as %+v; want triggered at %v", got, now)
	}
	// Clamped to now, it expires with the TTL like any other mark.
	now = now.Add(time.Hour + time.Second)
	if got := m.Lookup("default", "pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("the clamped mark outlived the TTL: %+v", got)
	}
}
