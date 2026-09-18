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

func TestAttemptTallySumsLatestCountPerObject(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	tally := newAttemptTally(time.Hour, 0)
	tally.now = func() time.Time { return now }

	if got := tally.Observe("pod-1", "evt-a", 1, now); got != 1 {
		t.Errorf("first object at 1: sum = %d; want 1", got)
	}
	if got := tally.Observe("pod-1", "evt-a", 3, now); got != 3 {
		t.Errorf("same object at 3: sum = %d; want 3 (latest count, not added)", got)
	}
	if got := tally.Observe("pod-1", "evt-b", 1, now); got != 4 {
		t.Errorf("second object at 1: sum = %d; want 4", got)
	}
	if got := tally.Observe("pod-1", "evt-a", 2, now); got != 4 {
		t.Errorf("a replayed lower count for evt-a: sum = %d; want 4 (the record is not lowered)", got)
	}
	if got := tally.Observe("pod-2", "evt-c", 1, now); got != 1 {
		t.Errorf("another pod: sum = %d; want 1", got)
	}
	if got := tally.Len(); got != 2 {
		t.Errorf("Len() = %d; want 2 pods", got)
	}
}

func TestAttemptTallyFailOpenAndInertInputs(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	tally := newAttemptTally(time.Hour, 0)
	tally.now = func() time.Time { return now }
	tally.Observe("pod-1", "evt-a", 4, now)

	if got := tally.Observe("pod-1", "evt-b", 0, now); got != 0 {
		t.Errorf("count 0 returned %d; want 0 (the fail-open zero passes through untouched)", got)
	}
	if got := tally.Observe("pod-1", "evt-a", 4, now); got != 4 {
		t.Errorf("a zero-count sighting changed the tally: sum = %d; want 4", got)
	}
	if got := tally.Observe("", "evt-a", 3, now); got != 3 {
		t.Errorf("empty uid returned %d; want the count back, 3", got)
	}
	var nilTally *attemptTally
	if got := nilTally.Observe("pod-1", "evt-a", 3, now); got != 3 {
		t.Errorf("nil tally returned %d; want the count back, 3", got)
	}
}

func TestAttemptTallyExpiresFromTheLatestSighting(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	tally := newAttemptTally(time.Hour, 0)
	tally.now = func() time.Time { return now }
	tally.Observe("pod-1", "evt-a", 2, now)
	tally.Observe("pod-1", "evt-b", 2, now.Add(30*time.Minute))

	now = now.Add(80 * time.Minute)
	if got := tally.Observe("pod-1", "evt-c", 1, now); got != 5 {
		t.Errorf("50 minutes after the latest sighting: sum = %d; want 5 (dated by the latest sighting, not the first)", got)
	}
	now = now.Add(2 * time.Hour)
	if got := tally.Observe("pod-1", "evt-d", 1, now); got != 1 {
		t.Errorf("past the ttl: sum = %d; want 1 (the tally started over)", got)
	}
}

func TestAttemptTallyCapsObjectsPerPod(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	tally := newAttemptTally(time.Hour, 0)
	tally.now = func() time.Time { return now }
	sum := 0
	for i := 0; i < attemptObjectsPerPod; i++ {
		sum = tally.Observe("pod-1", fmt.Sprintf("evt-%d", i), i+1, now)
	}
	wantFull := attemptObjectsPerPod * (attemptObjectsPerPod + 1) / 2
	if sum != wantFull {
		t.Fatalf("full tally sum = %d; want %d", sum, wantFull)
	}
	// One more object drops the smallest count (evt-0 at 1), not the newest.
	if got := tally.Observe("pod-1", "evt-new", 7, now); got != wantFull-1+7 {
		t.Errorf("past the cap: sum = %d; want %d (smallest object dropped)", got, wantFull-1+7)
	}
}

func TestAttemptTallyIsBoundedAcrossPods(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	tally := newAttemptTally(time.Hour, 3)
	tally.now = func() time.Time { return now }
	for i := 0; i < 5; i++ {
		tally.Observe(fmt.Sprintf("pod-%d", i), "evt", 1, now.Add(time.Duration(i)*time.Second))
	}
	if got := tally.Len(); got != 3 {
		t.Errorf("Len() = %d; want the cap, 3", got)
	}
}
