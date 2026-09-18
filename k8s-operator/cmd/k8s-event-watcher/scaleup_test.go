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

	m.Record("pod-1", scaleUpTriggered, now.Add(-time.Minute))
	m.Record("pod-1", scaleUpDeclined, now)
	if got := m.Lookup("pod-1"); got.Verdict != scaleUpDeclined || !got.At.Equal(now) {
		t.Errorf("after a newer decline, mark = %+v; want declined at %v", got, now)
	}

	// A replayed older mark must not overwrite the newer verdict.
	m.Record("pod-1", scaleUpTriggered, now.Add(-2*time.Minute))
	if got := m.Lookup("pod-1"); got.Verdict != scaleUpDeclined {
		t.Errorf("older replayed mark overwrote the newer one: %+v", got)
	}

	// A newer TriggeredScaleUp does supersede a decline: the autoscaler changed its mind.
	m.Record("pod-1", scaleUpTriggered, now.Add(time.Minute))
	if got := m.Lookup("pod-1"); got.Verdict != scaleUpTriggered {
		t.Errorf("newer trigger did not supersede the decline: %+v", got)
	}
}

func TestScaleUpMemoZeroTimestampIsNow(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 0)
	m.now = func() time.Time { return now }

	m.Record("pod-1", scaleUpTriggered, time.Time{})
	if got := m.Lookup("pod-1"); !got.At.Equal(now) {
		t.Errorf("zero event time recorded as %v; want now (%v)", got.At, now)
	}
}

func TestScaleUpMemoExpires(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(10*time.Minute, 0)
	m.now = func() time.Time { return now }

	m.Record("pod-1", scaleUpDeclined, now)
	now = now.Add(11 * time.Minute)
	if got := m.Lookup("pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("mark past ttl = %+v; want none", got)
	}
	if got := m.Len(); got != 0 {
		t.Errorf("expired entry left %d entries; want 0", got)
	}
}

func TestScaleUpMemoIsPerUID(t *testing.T) {
	m := newScaleUpMemo(0, 0)
	m.Record("pod-1", scaleUpTriggered, time.Now())
	if got := m.Lookup("pod-2"); got.Verdict != scaleUpNone {
		t.Errorf("unrelated pod inherited a mark: %+v", got)
	}
}

func TestScaleUpMemoIsBounded(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	m := newScaleUpMemo(time.Hour, 4)
	m.now = func() time.Time { return now }

	for i := 0; i < 20; i++ {
		m.Record(string(rune('a'+i)), scaleUpTriggered, now)
		now = now.Add(time.Second)
	}
	if got := m.Len(); got > 4 {
		t.Errorf("memo holds %d entries; want <= 4", got)
	}
	// The newest survives the eviction of the oldest.
	if got := m.Lookup(string(rune('a' + 19))); got.Verdict != scaleUpTriggered {
		t.Errorf("newest entry was evicted: %+v", got)
	}
}

func TestScaleUpMemoNilAndEmptyUIDAreInert(t *testing.T) {
	var nilMemo *scaleUpMemo
	nilMemo.Record("pod-1", scaleUpDeclined, time.Now())
	if got := nilMemo.Lookup("pod-1"); got.Verdict != scaleUpNone {
		t.Errorf("nil memo returned %+v; want the zero mark", got)
	}

	m := newScaleUpMemo(0, 0)
	m.Record("", scaleUpDeclined, time.Now())
	m.Record("pod-1", scaleUpNone, time.Now())
	if got := m.Len(); got != 0 {
		t.Errorf("empty uid or none verdict recorded %d entries; want 0", got)
	}
}
