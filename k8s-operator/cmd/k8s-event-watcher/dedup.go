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
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
	"sync"
	"time"
)

// dedupEntry holds deduplication metadata tracked for a specific event key.
type dedupEntry struct {
	// SessionID identifies the active troubleshooter session created for this event.
	SessionID string `json:"session_id"`
	// FirstSeen records when this event key was first cached.
	FirstSeen time.Time `json:"first_seen"`
	// LastSeen tracks the wall-clock time of the last real (non-replay) observation.
	LastSeen time.Time `json:"last_seen"`
	// EventLastTS stores the LastTimestamp of the raw Kubernetes event to recognize replays.
	EventLastTS time.Time `json:"event_last_ts"`
	// Count is the total occurrences observed within the current deduplication window.
	Count int `json:"count"`
}

// dedupResult dictates whether an event should trigger a new session or be suppressed.
type dedupResult struct {
	Kind      dedupResultKind
	SessionID string // only set when Kind==dedupDuplicate (referencing the existing active session)
	Count     int    // window count (1 for new incident, N for duplicates)
}

type dedupResultKind int

const (
	// dedupNewIncident: no prior entry exists, or the prior window has expired.
	// Caller must create a new session.
	dedupNewIncident dedupResultKind = iota
	// dedupDuplicate: an active deduplication entry is running within the window.
	// Caller suppresses this event.
	dedupDuplicate
)

// dedupCache is an in-memory store that suppresses repeat alerts for the same failure
// within a rolling time window (e.g., 5 minutes). To prevent the program from running out
// of memory, the cache is limited to a maximum of 10,000 active incidents. If this limit
// is reached, the oldest (least recently seen) incidents are automatically evicted to make room.
type dedupCache struct {
	mu          sync.Mutex
	entries     map[EventKey]*dedupEntry
	window      time.Duration
	max         int
	persistPath string
	now         func() time.Time
}

const maxDedupEntries = 10_000

// newDedupCache constructs a new deduplication cache with a rolling window.
func newDedupCache(window time.Duration, persistPath string) (*dedupCache, error) {
	if window <= 0 {
		return nil, fmt.Errorf("dedup: window must be > 0 (got %s)", window)
	}
	c := &dedupCache{
		entries:     make(map[EventKey]*dedupEntry),
		window:      window,
		max:         maxDedupEntries,
		persistPath: persistPath,
	}
	if persistPath != "" {
		if err := c.restore(); err != nil {
			return nil, fmt.Errorf("dedup: restore from %s: %w", persistPath, err)
		}
	}
	return c, nil
}

// clock returns the current time, supporting time overrides in tests.
func (c *dedupCache) clock() time.Time {
	if c.now != nil {
		return c.now()
	}
	return time.Now()
}

// canonicalizeReason returns the canonical reason name, checking the event message for
// generic reasons (like Failed or BackOff) to group them into their specific failure families.
func canonicalizeReason(reason, message string) string {
	if reason == "BackOff" {
		if strings.Contains(message, "pulling image") {
			return "ImagePullBackOff"
		}
		return "CrashLoopBackOff"
	}
	if reason == "Failed" {
		if strings.Contains(message, "Failed to pull image") ||
			strings.Contains(message, "ErrImagePull") ||
			strings.Contains(message, "ImagePullBackOff") {
			return "ImagePullBackOff"
		}
	}
	if reason == "ErrImagePull" {
		return "ImagePullBackOff"
	}
	return reason
}

// Observe evaluates an incoming event target key, message, and timestamp against cached state.
// It returns a dedupResult indicating whether to create a new session or suppress the event.
//
// Evaluates the following three cases:
// 1. Replays: EventLastTS is unchanged (caused by Informer connection rotations).
//    Result: suppressed as a duplicate. LastSeen is NOT advanced.
// 2. Cooldown Expiry: New EventLastTS observed after the rolling window has elapsed.
//    Result: classified as a new incident to trigger a retry session.
// 3. Ongoing Incidents: New EventLastTS observed within the rolling window.
//    Result: suppressed as a duplicate. LastSeen is advanced.
func (c *dedupCache) Observe(key EventKey, message string, eventLastTS time.Time) dedupResult {
	key.Reason = canonicalizeReason(key.Reason, message)
	now := c.clock()
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok {
		// First sighting for this key.
		c.evictIfFull()
		c.entries[key] = &dedupEntry{
			FirstSeen:   now,
			LastSeen:    now,
			EventLastTS: eventLastTS,
			Count:       1,
		}
		return dedupResult{Kind: dedupNewIncident, Count: 1}
	}
	if !eventLastTS.After(entry.EventLastTS) {
		// Case 1: Replay of an event we already processed.
		entry.Count++
		return dedupResult{Kind: dedupDuplicate, SessionID: entry.SessionID, Count: entry.Count}
	}
	if now.Sub(entry.LastSeen) > c.window {
		// Case 2: Cooldown expired. Create a new session.
		c.evictIfFull()
		c.entries[key] = &dedupEntry{
			FirstSeen:   now,
			LastSeen:    now,
			EventLastTS: eventLastTS,
			Count:       1,
		}
		return dedupResult{Kind: dedupNewIncident, Count: 1}
	}
	// Case 3: Incident is ongoing within the active window.
	entry.Count++
	entry.LastSeen = now
	entry.EventLastTS = eventLastTS
	return dedupResult{Kind: dedupDuplicate, SessionID: entry.SessionID, Count: entry.Count}
}

// BindSession attaches the SessionID from a successful CreateSession
// call to the entry created by the preceding Observe. No-op if the
// entry has since been evicted (window elapsed AND the LRU sweep
// dropped it), which is a possible but harmless race.
//
// Applies the same reason canonicalization Observe does so a caller
// that saw a `dedupNewIncident` result on one reason variant can
// bind the session using the wire-level reason without having to
// know about the family mapping.
func (c *dedupCache) BindSession(key EventKey, message string, sessionID string) {
	key.Reason = canonicalizeReason(key.Reason, message)
	c.mu.Lock()
	defer c.mu.Unlock()
	if entry, ok := c.entries[key]; ok {
		entry.SessionID = sessionID
	}
}

// evictIfFull is called under lock. If the cache is at capacity,
// evicts the LRU entry (lowest LastSeen). Bounded O(N) scan; called
// only on new-incident cache-miss paths so amortized cost is fine.
func (c *dedupCache) evictIfFull() {
	if len(c.entries) < c.max {
		return
	}
	var oldestKey EventKey
	var oldestTs time.Time
	first := true
	for k, e := range c.entries {
		if first || e.LastSeen.Before(oldestTs) {
			oldestKey = k
			oldestTs = e.LastSeen
			first = false
		}
	}
	delete(c.entries, oldestKey)
}

// Len returns the current cache size. Test / metrics helper.
func (c *dedupCache) Len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.entries)
}

// Snapshot writes the current cache state to persistPath. Idempotent;
// no-op when persistPath is empty. Callers should call this on
// graceful shutdown (SIGTERM handler in main.go) and periodically
// while running (e.g., every 30s ticker) so a crash doesn't lose
// more than 30s of dedup state.
//
// Format: pretty-printed JSON — small enough that a human can
// inspect it during incident debugging, and simple enough that the
// on-disk shape doesn't need its own migration story.
func (c *dedupCache) Snapshot() error {
	if c.persistPath == "" {
		return nil
	}
	c.mu.Lock()
	// Copy values under lock; encode outside so we don't hold the mutex during I/O.
	snapshot := make(map[string]dedupEntry, len(c.entries))
	for k, v := range c.entries {
		snapshot[serializeKey(k)] = *v
	}
	c.mu.Unlock()
	data, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		return fmt.Errorf("dedup: marshal snapshot: %w", err)
	}
	// Atomic write: temp file + rename so an interrupted write
	// doesn't corrupt the persisted state.
	tmp := c.persistPath + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return fmt.Errorf("dedup: write %s: %w", tmp, err)
	}
	if err := os.Rename(tmp, c.persistPath); err != nil {
		return fmt.Errorf("dedup: rename %s → %s: %w", tmp, c.persistPath, err)
	}
	return nil
}

// restore reads persistPath (if it exists) and hydrates the cache.
// Missing file is not an error — first-time startup has nothing to
// restore. Called by newDedupCache during construction.
func (c *dedupCache) restore() error {
	data, err := os.ReadFile(c.persistPath)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil // first startup; nothing to restore
		}
		return fmt.Errorf("dedup: read %s: %w", c.persistPath, err)
	}
	var snapshot map[string]dedupEntry
	if err := json.Unmarshal(data, &snapshot); err != nil {
		log.Printf("dedup: unmarshal snapshot (starting fresh): %v", err)
		return nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	for keyStr, entry := range snapshot {
		key, ok := deserializeKey(keyStr)
		if !ok {
			continue // silently skip malformed keys
		}
		e := entry
		c.entries[key] = &e
	}
	return nil
}

// serializeKey / deserializeKey encode an EventKey for use as a
// JSON map key (which must be a string). Using a delimiter that
// can't appear in a k8s UID (which is hex + hyphens) or an Event
// reason (which is CamelCase alphanumeric).
func serializeKey(k EventKey) string {
	return k.UID + "|" + k.Reason
}

func deserializeKey(s string) (EventKey, bool) {
	for i := 0; i < len(s); i++ {
		if s[i] == '|' {
			return EventKey{UID: s[:i], Reason: s[i+1:]}, true
		}
	}
	return EventKey{}, false
}
