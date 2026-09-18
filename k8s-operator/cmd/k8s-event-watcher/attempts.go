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
	// attemptObjectsPerPod caps how many event objects one pod's tally tracks.
	// The recorder's aggregator folds more than ten distinct messages in ten
	// minutes into one object, so a pod accumulates objects slowly; past the
	// cap the object with the smallest count is dropped, which loses the
	// least of the sum.
	attemptObjectsPerPod = 32
)

// attemptTally sums a pod's FailedScheduling count across the event objects
// the recorder splits it over. Both recorders key an event object on its
// message, so every time the scheduler's text changes ("0/3 nodes are
// available: 3 Insufficient cpu" becoming "0/4 nodes ... 3 Insufficient cpu,
// 1 node(s) had untolerated taint") the API server gets a new object whose
// count starts again at one. Read one object at a time, the count backstop
// would then never reach its threshold on a pod whose message shifts every
// few retries, which on a cluster without an autoscaler, or for a pod the
// autoscaler never rules on, is a stuck pod nobody is told about. The tally
// is per pod UID, keeps the latest count per event object, and hands the
// gate their sum: the number of failed attempts the threshold was meant to
// count. The dedup key and the payload's count are untouched; only the gate
// reads the sum.
//
// Dated by the pod's latest sighting and expiring with the scale-up memo, so a
// tally outlives any event it could still be summed into and no longer.
type attemptTally struct {
	mu      sync.Mutex
	entries boundedEntries[map[string]int]
	now     func() time.Time
}

func newAttemptTally(ttl time.Duration, max int) *attemptTally {
	if ttl <= 0 {
		ttl = defaultScaleUpTTL
	}
	if max <= 0 {
		max = defaultScaleUpEntries
	}
	return &attemptTally{entries: newBoundedEntries[map[string]int](ttl, max)}
}

func (t *attemptTally) clock() time.Time {
	if t.now != nil {
		return t.now()
	}
	return time.Now()
}

// Observe records count as the latest reading of the event object eventUID
// under pod uid, sighted at, and returns the pod's count summed across its
// objects. A lower count for an object already on record (a relist replaying
// an older state of it) does not lower the record. A non-positive count is
// the fail-open "this emitter does not count" and is returned as it is,
// touching nothing, so the gate's zero keeps passing through. Safe on a nil
// receiver and for an empty uid, both of which return count.
func (t *attemptTally) Observe(uid, eventUID string, count int, at time.Time) int {
	if t == nil || uid == "" || count <= 0 {
		return count
	}
	now := t.clock()
	if at.IsZero() {
		at = now
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	objects, seen, ok := t.entries.lookup(uid, now)
	if !ok {
		objects = make(map[string]int, 1)
	}
	if prev, held := objects[eventUID]; !held || count > prev {
		if !held && len(objects) >= attemptObjectsPerPod {
			dropSmallest(objects)
		}
		objects[eventUID] = count
	}
	if seen.After(at) {
		at = seen
	}
	t.entries.store(uid, objects, at, now)
	sum := 0
	for _, c := range objects {
		sum += c
	}
	return sum
}

// dropSmallest removes the object with the smallest count from a full tally.
func dropSmallest(objects map[string]int) {
	var smallestUID string
	smallest := 0
	first := true
	for id, c := range objects {
		if first || c < smallest {
			smallestUID, smallest, first = id, c, false
		}
	}
	delete(objects, smallestUID)
}

// Len reports the number of pods with a tally. Test helper.
func (t *attemptTally) Len() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.entries.len()
}
