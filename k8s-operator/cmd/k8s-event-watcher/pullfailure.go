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
	"strings"
	"sync"
	"time"
)

// pullClass sorts an image-pull failure by what actually went wrong, which the
// reason family cannot express. ImagePullBackOff covers both "this tag does not
// exist", which no amount of waiting fixes, and "the registry is rate-limiting
// us", which kubelet clears by itself on its next retry (10s → 20s → 40s → …).
// Those are opposite incidents that canonicalize identically.
type pullClass int

const (
	// pullClassUnknown is a pull failure whose message matched no marker. Treated
	// as fire-immediately: the gate this feeds is subtractive by design and only
	// ever holds a failure it positively recognizes as self-clearing.
	pullClassUnknown pullClass = iota
	// pullClassTerminal will not fix itself — a bad tag, a denied pull, a full disk.
	pullClassTerminal
	// pullClassRetryable is expected to clear on kubelet's own retry schedule.
	pullClassRetryable
)

func (c pullClass) String() string {
	switch c {
	case pullClassTerminal:
		return "terminal"
	case pullClassRetryable:
		return "retryable"
	default:
		return "unknown"
	}
}

// Marker matching on free-text error strings carries the same honesty note as the
// canonical-reason table in dedup.go: these are substrings of messages produced by
// kubelet, containerd and the registry, none of which is a stable API. A registry
// that rewords its quota error stops matching and its failures become "unknown",
// which fires immediately — the same behaviour as before any of this existed. The
// lists are lowercase; matching lowercases the message first.
var (
	// terminalPullMarkers are checked first and win ties. A message carrying both
	// (a 429 while retrying a tag that also does not exist) must not be held.
	terminalPullMarkers = []string{
		"manifest unknown",
		"manifest for", // "manifest for …:tag not found"
		"not found",    // covers "repository does not exist or may require ..."
		"no such image",
		"denied", // "denied: requested access to the resource is denied"
		"unauthorized",
		"authentication required",
		"invalid reference format",
		"no space left on device",
	}
	// retryablePullMarkers are the failures kubelet's own backoff resolves.
	retryablePullMarkers = []string{
		"429",
		"too many requests",
		"toomanyrequests",
		"quota exceeded",
		"500 internal server error",
		"502",
		"503",
		"504",
		"server error",
		"i/o timeout",
		"tls handshake timeout",
		"connection reset by peer",
		"connection refused",
		"unexpected eof",
		"context deadline exceeded",
	}
)

// stripQuoted removes double-quoted spans from a message. Pull failures are
// formatted `Failed to pull image "<ref>": <error>`, and the reference is
// attacker-adjacent free text as far as marker matching is concerned: an image
// legitimately tagged :1.503 or :v429 would otherwise match a retryable HTTP-status
// marker and hold a bad tag. Only the error text outside the quotes is evidence.
func stripQuoted(msg string) string {
	var b strings.Builder
	inQuote := false
	for _, r := range msg {
		if r == '"' {
			inQuote = !inQuote
			continue
		}
		if !inQuote {
			b.WriteRune(r)
		}
	}
	return b.String()
}

// classifyPullFailure sorts a pull-failure message into terminal, retryable, or
// unknown. Terminal is checked first so the classification is purely subtractive:
// it can only ever delay a failure it positively recognizes as self-clearing, and
// anything ambiguous keeps the pre-existing fire-on-event-1 behaviour.
func classifyPullFailure(message string) pullClass {
	msg := strings.ToLower(stripQuoted(message))
	for _, m := range terminalPullMarkers {
		if strings.Contains(msg, m) {
			return pullClassTerminal
		}
	}
	for _, m := range retryablePullMarkers {
		if strings.Contains(msg, m) {
			return pullClassRetryable
		}
	}
	return pullClassUnknown
}

// pullClassMemo remembers the class of the most recent classifiable pull failure
// per involved-object UID.
//
// It exists because kubelet splits one incident across several events and only the
// first carries the cause. Observed on GKE v1.36.2-gke.2064000, in order:
//
//	reason=Failed   "Failed to pull image "…": … i/o timeout"  ← the only cause
//	reason=Failed   "Error: ErrImagePull"                      ← no cause
//	reason=BackOff  "Back-off pulling image "…""               ← no cause
//	reason=Failed   "Error: ImagePullBackOff"                  ← no cause
//
// Classifying each message where it stands would hold the first and then fire on
// the next one a second later, which is worse than not classifying at all — it adds
// latency without suppressing anything. Verified against a live cluster: with
// resolution bypassed, "Error: ErrImagePull" is what opens the session.
// Resolving through this memo lets the causeless back-off inherit the cause the
// Failed event named. Carry-forward applies to terminal causes too, which is
// exactly why a bad tag keeps firing fast.
//
// Bounded in both directions. Entries expire after ttl because a pod that recovers
// and later fails for a different reason must not inherit the stale class, and the
// map is capped so a cluster churning through pods cannot grow it without limit.
type pullClassMemo struct {
	mu      sync.Mutex
	entries map[string]pullClassEntry
	ttl     time.Duration
	max     int
	now     func() time.Time
}

type pullClassEntry struct {
	class    pullClass
	recorded time.Time
}

const (
	defaultPullClassTTL     = 10 * time.Minute
	defaultPullClassEntries = 4096
)

func newPullClassMemo(ttl time.Duration, max int) *pullClassMemo {
	if ttl <= 0 {
		ttl = defaultPullClassTTL
	}
	if max <= 0 {
		max = defaultPullClassEntries
	}
	return &pullClassMemo{
		entries: make(map[string]pullClassEntry),
		ttl:     ttl,
		max:     max,
	}
}

func (m *pullClassMemo) clock() time.Time {
	if m.now != nil {
		return m.now()
	}
	return time.Now()
}

// Resolve classifies this event's own message and returns the class to act on,
// remembering it for later causeless events about the same object.
//
// A message that classifies is recorded and returned. A message that does not —
// the causeless "Back-off pulling image" — falls back to what an earlier event
// about the same UID established. Terminal supersedes a remembered retryable:
// within one pod UID the image is fixed, so a terminal verdict arriving after a
// retryable one means the retry finally surfaced the real problem.
//
// Safe on a nil receiver so a dispatcher built without a memo (tests, dry-run
// wiring) degrades to per-message classification rather than panicking.
func (m *pullClassMemo) Resolve(uid, message string) pullClass {
	class := classifyPullFailure(message)
	if m == nil || uid == "" {
		return class
	}
	now := m.clock()
	m.mu.Lock()
	defer m.mu.Unlock()

	prev, ok := m.entries[uid]
	if ok && now.Sub(prev.recorded) > m.ttl {
		delete(m.entries, uid)
		ok = false
	}
	if class == pullClassUnknown {
		if ok {
			return prev.class
		}
		return pullClassUnknown
	}
	if ok && prev.class == pullClassTerminal && class == pullClassRetryable {
		// Terminal already established for this object; a later retryable-looking
		// message does not un-break a bad tag.
		return pullClassTerminal
	}
	m.evictIfFull(now)
	m.entries[uid] = pullClassEntry{class: class, recorded: now}
	return class
}

// evictIfFull is called under lock. Drops expired entries first, and only if that
// frees nothing evicts the oldest — the same bounded-scan approach dedupCache uses,
// on a map an order of magnitude smaller.
func (m *pullClassMemo) evictIfFull(now time.Time) {
	if len(m.entries) < m.max {
		return
	}
	for uid, e := range m.entries {
		if now.Sub(e.recorded) > m.ttl {
			delete(m.entries, uid)
		}
	}
	if len(m.entries) < m.max {
		return
	}
	var oldestUID string
	var oldest time.Time
	first := true
	for uid, e := range m.entries {
		if first || e.recorded.Before(oldest) {
			oldestUID, oldest, first = uid, e.recorded, false
		}
	}
	delete(m.entries, oldestUID)
}

// Len reports the current entry count. Test helper.
func (m *pullClassMemo) Len() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.entries)
}
