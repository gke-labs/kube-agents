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
	// PolicyFiltered records that the daemon accepted the event this entry was
	// opened for and then dropped it on purpose, grading it Info. The entry is
	// kept in that case — see injectStatusFiltered — so the flag marks a key
	// that is held on behalf of an alert nobody received.
	PolicyFiltered bool `json:"policy_filtered,omitempty"`
	// Reopened records that ReopenIfPolicyFiltered has already fired for this
	// window, and is deliberately not cleared when it does. It bounds the
	// escape hatch at one extra session per window per key.
	Reopened bool `json:"reopened,omitempty"`
}

// dedupResult dictates whether an event should trigger a new session or be suppressed.
type dedupResult struct {
	Kind      dedupResultKind
	SessionID string // only set when Kind==dedupDuplicate (referencing the existing active session)
	Count     int    // window count (1 for new incident, N for duplicates)
	// Replay marks a duplicate whose LastTimestamp had not advanced. Observe
	// consumes that timestamp, so the caller cannot re-derive this afterwards.
	Replay bool
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
		c.restore()
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
//  1. Replays: EventLastTS is unchanged (caused by Informer connection rotations).
//     Result: suppressed as a duplicate. LastSeen is NOT advanced.
//  2. Cooldown Expiry: New EventLastTS observed after the rolling window has elapsed.
//     Result: classified as a new incident to trigger a retry session.
//  3. Ongoing Incidents: New EventLastTS observed within the rolling window.
//     Result: suppressed as a duplicate. LastSeen is advanced.
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
		return dedupResult{Kind: dedupDuplicate, SessionID: entry.SessionID, Count: entry.Count, Replay: true}
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

// MarkPolicyFiltered flags the entry for a key as being held on behalf
// of an event the daemon graded Info and dropped. Called by the dispatch
// path when an inject comes back injectStatusFiltered, which — unlike
// every other undelivered outcome — keeps its entry rather than
// forgetting it.
//
// Canonicalizes the reason exactly as Observe does. No-op if the entry
// is already gone.
//
// An entry that has already been reopened is *deleted* rather than
// flagged, because for that entry there is no state left worth holding.
// ReopenIfPolicyFiltered needs PolicyFiltered set and Reopened clear, and
// a reopened entry fails the second clause whatever this method writes —
// so flagging it, or declining to, leaves the same dead key either way,
// with Observe's Case 3 sliding LastSeen on every later sighting so it
// never expires and all three Forget callers sitting behind an attempted
// inject no sighting now reaches. Restarting does not help; restore
// rehydrates the flags verbatim. Deleting gives the family its way back:
// the next sighting opens a new incident, at the cost of one session per
// sighting until the daemon stops filtering it — the same price the
// "suppressed" path already pays, and cheap against permanent silence.
//
// It should not be reachable at all. reopenPolicyFiltered admits only
// events daemonWouldAlert says the daemon posts, so the reopened entry's
// own inject should never come back filtered. That mirror is a rule
// written in another language on another image, which is exactly the kind
// of agreement that decays without anything failing, so the recovery is
// here rather than in a comment asserting it cannot happen.
func (c *dedupCache) MarkPolicyFiltered(key EventKey, message string) {
	key.Reason = canonicalizeReason(key.Reason, message)
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok {
		return
	}
	if entry.Reopened {
		delete(c.entries, key)
		return
	}
	entry.PolicyFiltered = true
}

// ReopenIfPolicyFiltered re-opens an incident whose entry is held by a
// policy-filtered event, and reports whether it did. The caller should
// then treat its own event as a new incident.
//
// The problem it solves is that the dedup key is (uid, *canonical*
// reason), and canonicalizeReason deliberately folds a whole failure
// family onto one key: kubelet's Normal-type `BackOff` ("Back-off
// pulling image"), the `ErrImagePull` beside it and the Warning-type
// `Failed` that follows are one incident, not three. That is right for
// deduplication and wrong for a policy filter. The daemon grades the
// Normal member Info and asks us to keep its entry, and the key is then
// held on behalf of the one member of the family nobody needed to hear
// about. Every Warning behind it takes Case 3, which slides LastSeen
// forward, so an image pull that keeps failing keeps its own window
// alive and the alert never comes — permanent silence on a real
// failure, with only k8s_event_watcher_events_policy_filtered_total to
// show for it.
//
// Bounded at one firing per entry by the sticky Reopened flag, so the
// escape hatch cannot turn into a session per sighting — the churn
// keeping the entry exists to avoid. Dispatch withholds replays, so only
// a fresh sighting can spend that firing. reopenPolicyFiltered admits only
// events daemonWouldAlert says the daemon posts, which rules out the
// obvious way to spend the firing repeatedly; the flag is what holds if
// that mirror is ever wrong. An emitter leaving Event.Type empty is not
// an example of such a family: inject_message coerces an absent type to
// Warning, so the daemon posts those rather than grading them Info.
func (c *dedupCache) ReopenIfPolicyFiltered(key EventKey, message string, eventLastTS time.Time) bool {
	key.Reason = canonicalizeReason(key.Reason, message)
	now := c.clock()
	c.mu.Lock()
	defer c.mu.Unlock()
	entry, ok := c.entries[key]
	if !ok || !entry.PolicyFiltered || entry.Reopened {
		return false
	}
	c.entries[key] = &dedupEntry{
		FirstSeen:   now,
		LastSeen:    now,
		EventLastTS: eventLastTS,
		Count:       1,
		Reopened:    true,
	}
	return true
}

// Forget drops the entry for a key, so the next sighting of the same
// failure is classified as a new incident rather than suppressed.
//
// This exists for one caller: the dispatch path, when the daemon did
// not accept the alert the entry was created for — a failed
// CreateSession, a non-2xx inject, or a 2xx inject the daemon reports
// as suppressed against its daily ceiling. It is not a claim that the
// alert reached a human; the daemon answers the inject before it posts
// to chat, so a failure after that point is beyond what any caller here
// can observe. Observe writes the entry before CreateSession or Inject
// is attempted, and nothing else
// removes entries — evictIfFull is capacity-driven and there is no
// expiry sweep — so without this an entry created for an alert that
// never went out stays live, and every later sighting of that failure
// takes Case 3 and slides LastSeen forward. Case 2 is then the only
// escape, and it needs a quiet gap longer than the whole window, which
// a steadily-failing workload never produces. The result is a failure
// that is silently suppressed for as long as it keeps failing.
//
// The cost of forgetting is that a daemon outage is retried at the
// event's own repeat cadence rather than once, which is the intended
// trade: an alert nobody received is not a duplicate.
//
// Canonicalizes the reason exactly as Observe and BindSession do, so a
// caller can pass the wire-level reason. No-op if the entry is already
// gone.
func (c *dedupCache) Forget(key EventKey, message string) {
	key.Reason = canonicalizeReason(key.Reason, message)
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.entries, key)
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
// Called by newDedupCache during construction.
//
// No read failure is fatal. A missing file is the normal first startup, and
// anything else — EIO on a network-backed volume, a restored PVC whose
// ownership no longer matches, a UID change on an image bump — costs at most
// one replay of the events still inside the API server's TTL. The alternative
// is worse: the caller treats a construction error as "this cluster will NOT
// be watched", and because restore runs once at process start, a transient
// read error would silently take that cluster out until someone restarts the
// pod. Other clusters keep the process alive, so nothing would exit and no
// supervisor alert would fire. Corrupt JSON below is tolerated for the same
// reason; an unreadable file is not a stronger signal than an unparseable one.
//
// It returns nothing for that reason: there is no failure a caller could act
// on, and an error return here previously meant dropping the cluster.
func (c *dedupCache) restore() {
	data, err := os.ReadFile(c.persistPath)
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			log.Printf("dedup: read %s (starting fresh, incidents inside the dedup window may be re-reported): %v", c.persistPath, err)
		}
		return
	}
	var snapshot map[string]dedupEntry
	if err := json.Unmarshal(data, &snapshot); err != nil {
		log.Printf("dedup: unmarshal snapshot (starting fresh): %v", err)
		return
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
}

// serializeKey / deserializeKey encode an EventKey for use as a
// JSON map key (which must be a string). Using a delimiter that
// can't appear in a k8s UID (which is hex + hyphens) or an Event
// reason (which is CamelCase alphanumeric).
//
// No cluster component: each watched cluster owns a cache and so a
// separate snapshot file, and the file path already identifies the
// cluster (see dedupPersistPath).
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
