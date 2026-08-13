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
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestUnreadableSnapshotStartsFresh pins the blast radius of a snapshot the
// process cannot read — EIO on a network-backed volume, a restored PVC whose
// ownership no longer matches, a UID change on an image bump.
//
// The caller treats a construction error as "this cluster will NOT be
// watched", and restore runs once at process start, so returning an error
// here would take that cluster out until someone restarted the pod. With
// other clusters still running, nothing exits and no supervisor alert fires,
// which makes it a silent loss. Starting fresh costs one replay instead.
func TestUnreadableSnapshotStartsFresh(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("running as root, an unreadable file is still readable")
	}
	path := filepath.Join(t.TempDir(), "dedup.json")
	if err := os.WriteFile(path, []byte(`{"u|Reason":{"count":1}}`), 0o000); err != nil {
		t.Fatalf("write: %v", err)
	}
	t.Cleanup(func() { _ = os.Chmod(path, 0o600) })

	c, err := newDedupCache(5*time.Minute, path)
	if err != nil {
		t.Fatalf("an unreadable snapshot must not take the cluster down: %v", err)
	}
	if got := c.Len(); got != 0 {
		t.Errorf("expected an empty cache after an unreadable snapshot, got %d entries", got)
	}
}

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

func TestForgetReopensTheIncidentWithoutWaitingOutTheWindow(t *testing.T) {
	// The window is long on purpose: this is what a deployed install runs,
	// and the point of Forget is that recovery must not depend on outliving
	// it. Waiting out a 24h window is not a recovery path.
	c, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("Failed to create dedup cache: %v", err)
	}
	now := time.Now()
	c.now = func() time.Time { return now }

	key := EventKey{Reason: "CrashLoopBackOff", UID: "pod-123"}

	if res := c.Observe(key, "", now); res.Kind != dedupNewIncident {
		t.Fatalf("Observe (first) got kind %v; want dedupNewIncident", res.Kind)
	}

	// Without Forget, this is the state a failed dispatch leaves behind: an
	// entry for an alert nobody received. Confirm the repeat really is
	// suppressed, so the assertion after Forget means something.
	now = now.Add(5 * time.Minute)
	if res := c.Observe(key, "", now); res.Kind != dedupDuplicate {
		t.Fatalf("Observe (repeat inside window) got kind %v; want dedupDuplicate", res.Kind)
	}

	c.Forget(key, "")
	if c.Len() != 0 {
		t.Errorf("Forget left %d entries; want 0", c.Len())
	}

	now = now.Add(5 * time.Minute)
	res := c.Observe(key, "", now)
	if res.Kind != dedupNewIncident {
		t.Errorf("Observe (after Forget) got kind %v; want dedupNewIncident", res.Kind)
	}
	if res.Count != 1 {
		t.Errorf("Observe (after Forget) got count %d; want 1", res.Count)
	}
}

func TestForgetCanonicalizesTheReason(t *testing.T) {
	// The dispatcher passes the wire-level reason, which Observe stored
	// under its canonical family name. A Forget that skipped the mapping
	// would delete nothing and silently leave the poisoned entry in place.
	c, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("Failed to create dedup cache: %v", err)
	}

	const msg = "Back-off pulling image \"example.com/nope:v1\""
	key := EventKey{Reason: "BackOff", UID: "pod-123"}

	if res := c.Observe(key, msg, time.Now()); res.Kind != dedupNewIncident {
		t.Fatalf("Observe got kind %v; want dedupNewIncident", res.Kind)
	}
	c.Forget(key, msg)
	if c.Len() != 0 {
		t.Errorf("Forget(%q) left %d entries; want 0 (stored as ImagePullBackOff)", key.Reason, c.Len())
	}
}

func TestPerClusterCachesAreIndependent(t *testing.T) {
	// Cluster isolation now comes from each cluster owning a cache, not
	// from the key. Identical UID+Reason on two clusters must still be
	// treated as two separate incidents, and one cluster's activity must
	// not affect the other's state.
	window := 5 * time.Minute
	now := time.Now()

	newCache := func() *dedupCache {
		c, err := newDedupCache(window, "")
		if err != nil {
			t.Fatalf("Failed to create dedup cache: %v", err)
		}
		c.now = func() time.Time { return now }
		return c
	}
	clusterA, clusterB := newCache(), newCache()

	key := EventKey{UID: "pod-shared-uid", Reason: "OOMKilled"}

	if res := clusterA.Observe(key, "", now); res.Kind != dedupNewIncident {
		t.Errorf("cluster-a first observe: got kind %v; want dedupNewIncident", res.Kind)
	}
	// Same key, other cluster: must be a new incident, not suppressed by A.
	if res := clusterB.Observe(key, "", now); res.Kind != dedupNewIncident {
		t.Errorf("cluster-b first observe (same UID+Reason as A): got kind %v; want dedupNewIncident", res.Kind)
	}
	// Each cluster still dedups against itself.
	if res := clusterA.Observe(key, "", now); res.Kind != dedupDuplicate {
		t.Errorf("cluster-a second observe: got kind %v; want dedupDuplicate", res.Kind)
	}
	// Sessions bound in one cache must not leak into the other.
	clusterA.BindSession(key, "", "session-a")
	if res := clusterB.Observe(key, "", now); res.SessionID == "session-a" {
		t.Errorf("cluster-b saw cluster-a's session %q; caches must not share state", res.SessionID)
	}
	if got, want := clusterA.Len(), 1; got != want {
		t.Errorf("cluster-a Len() = %d; want %d", got, want)
	}
	if got, want := clusterB.Len(), 1; got != want {
		t.Errorf("cluster-b Len() = %d; want %d", got, want)
	}
}

func TestSerializeDeserializeKeyRoundTrip(t *testing.T) {
	// UIDs and reasons round-trip through the persist format. Includes a
	// hex UID with hyphens to catch delimiter-handling regressions.
	cases := []EventKey{
		{UID: "8f2a1b6c-1234-4567-89ab-cdef01234567", Reason: "OOMKilled"},
		{UID: "uid-1", Reason: "CrashLoopBackOff"},
		{UID: "u", Reason: "r"},
	}
	for _, want := range cases {
		got, ok := deserializeKey(serializeKey(want))
		if !ok {
			t.Errorf("deserializeKey(serializeKey(%+v)) returned ok=false", want)
			continue
		}
		if got != want {
			t.Errorf("round-trip mismatch: got %+v, want %+v", got, want)
		}
	}
	// A key with no delimiter is malformed and must be skipped, not panic.
	if _, ok := deserializeKey("no-delimiter"); ok {
		t.Errorf("delimiter-less key should be rejected, but deserializeKey returned ok=true")
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
