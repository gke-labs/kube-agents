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

func TestDedupObserve(t *testing.T) {
	window := 5 * time.Minute
	c, err := newDedupCache(window, "")
	if err != nil {
		t.Fatalf("Failed to create dedup cache: %v", err)
	}

	// Mock clock
	now := time.Now()
	c.now = func() time.Time { return now }

	key := EventKey{Reason: "CrashLoopBackOff", UID: "pod-123"}
	eventTime := now

	// 1. First event should be a new incident
	res := c.Observe(key, "", eventTime)
	if res.Kind != dedupNewIncident {
		t.Errorf("Observe (first) got kind %v; want dedupNewIncident", res.Kind)
	}
	if res.Count != 1 {
		t.Errorf("Observe (first) got count %d; want 1", res.Count)
	}

	// Bind a mock session to it
	c.BindSession(key, "", "session-abc")

	// 2. Immediate repeat should be a duplicate
	res = c.Observe(key, "", eventTime)
	if res.Kind != dedupDuplicate {
		t.Errorf("Observe (immediate repeat) got kind %v; want dedupDuplicate", res.Kind)
	}
	if res.SessionID != "session-abc" {
		t.Errorf("Observe (immediate repeat) got session %q; want 'session-abc'", res.SessionID)
	}
	if res.Count != 2 {
		t.Errorf("Observe (immediate repeat) got count %d; want 2", res.Count)
	}

	// 3. Advancing time past window should result in a new incident
	now = now.Add(6 * time.Minute)
	res = c.Observe(key, "", now)
	if res.Kind != dedupNewIncident {
		t.Errorf("Observe (expired window) got kind %v; want dedupNewIncident", res.Kind)
	}
	if res.Count != 1 {
		t.Errorf("Observe (expired window) got count %d; want 1", res.Count)
	}
}

func TestCanonicalReasonMatching(t *testing.T) {
	window := 5 * time.Minute
	c, err := newDedupCache(window, "")
	if err != nil {
		t.Fatalf("Failed to create dedup cache: %v", err)
	}

	now := time.Now()
	c.now = func() time.Time { return now }

	// ErrImagePull should map to ImagePullBackOff canonical key
	key1 := EventKey{Reason: "ErrImagePull", UID: "pod-image-pull"}
	key2 := EventKey{Reason: "ImagePullBackOff", UID: "pod-image-pull"}

	// First event: ErrImagePull
	res1 := c.Observe(key1, "", now)
	if res1.Kind != dedupNewIncident {
		t.Errorf("Observe key1 got kind %v; want dedupNewIncident", res1.Kind)
	}
	c.BindSession(key1, "", "session-image-pull")

	// Second event: ImagePullBackOff for same pod should be duplicate
	res2 := c.Observe(key2, "", now)
	if res2.Kind != dedupDuplicate {
		t.Errorf("Observe key2 got kind %v; want dedupDuplicate", res2.Kind)
	}
	if res2.SessionID != "session-image-pull" {
		t.Errorf("Observe key2 got session %q; want 'session-image-pull'", res2.SessionID)
	}
}

func TestMessageAwareReasonMatching(t *testing.T) {
	window := 5 * time.Minute
	c, err := newDedupCache(window, "")
	if err != nil {
		t.Fatalf("Failed to create dedup cache: %v", err)
	}

	now := time.Now()
	c.now = func() time.Time { return now }

	podUID := "pod-pull-failure"

	// All these events should canonicalize to ImagePullBackOff:
	// 1. Failed (msg: Failed to pull image...)
	// 2. Failed (msg: Error: ErrImagePull)
	// 3. BackOff (msg: Back-off pulling image...)
	// 4. Failed (msg: Error: ImagePullBackOff)

	e1Key := EventKey{Reason: "Failed", UID: podUID}
	e1Msg := `Failed to pull image "nginx:invalid-tag-for-testing": rpc error: code = NotFound ...`

	e2Key := EventKey{Reason: "Failed", UID: podUID}
	e2Msg := "Error: ErrImagePull"

	e3Key := EventKey{Reason: "BackOff", UID: podUID}
	e3Msg := `Back-off pulling image "nginx:invalid-tag-for-testing"`

	e4Key := EventKey{Reason: "Failed", UID: podUID}
	e4Msg := "Error: ImagePullBackOff"

	// 1st: Failed (Failed to pull image)
	res1 := c.Observe(e1Key, e1Msg, now)
	if res1.Kind != dedupNewIncident {
		t.Errorf("1st event got %v; want dedupNewIncident", res1.Kind)
	}
	c.BindSession(e1Key, e1Msg, "session-shared")

	// 2nd: Failed (ErrImagePull)
	res2 := c.Observe(e2Key, e2Msg, now)
	if res2.Kind != dedupDuplicate {
		t.Errorf("2nd event got %v; want dedupDuplicate", res2.Kind)
	}
	if res2.SessionID != "session-shared" {
		t.Errorf("2nd event got session %q; want 'session-shared'", res2.SessionID)
	}

	// 3rd: BackOff (Back-off pulling image)
	res3 := c.Observe(e3Key, e3Msg, now)
	if res3.Kind != dedupDuplicate {
		t.Errorf("3rd event got %v; want dedupDuplicate", res3.Kind)
	}
	if res3.SessionID != "session-shared" {
		t.Errorf("3rd event got session %q; want 'session-shared'", res3.SessionID)
	}

	// 4th: Failed (ImagePullBackOff)
	res4 := c.Observe(e4Key, e4Msg, now)
	if res4.Kind != dedupDuplicate {
		t.Errorf("4th event got %v; want dedupDuplicate", res4.Kind)
	}
	if res4.SessionID != "session-shared" {
		t.Errorf("4th event got session %q; want 'session-shared'", res4.SessionID)
	}
}
