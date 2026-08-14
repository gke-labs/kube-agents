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
		"not found",    // "404 Not Found", "repository does not exist or may require ..."
		"notfound",     // "rpc error: code = NotFound desc = ..." — no space, distinct marker
		"no such image",
		"denied", // "denied: requested access to the resource is denied"
		"unauthorized",
		"401 unauthorized",
		"403 forbidden",
		"authentication required",
		"invalid reference format",
		"invalidimagename",
		"errimageneverpull",
		"no space left on device",
	}
	// retryablePullMarkers are the failures kubelet's own backoff resolves.
	//
	// Every HTTP status here is spelled with its reason phrase rather than as a
	// bare number. A bare "503" or "429" matches any three digits in the message,
	// and pull failures are full of unrelated digits that stripQuoted does not
	// remove — Artifact Registry's quota error alone carries a 12-digit
	// project_number in single quotes and a region name. Matching a status code
	// out of a project number would hold a failure nobody classified. The cost of
	// the stricter form is a bare "unexpected status 503" going unrecognized,
	// which fires immediately: the safe direction.
	retryablePullMarkers = []string{
		"too many requests", // covers "429 Too Many Requests"
		"toomanyrequests",   // registry error code form
		"quota exceeded",
		"500 internal server error",
		"502 bad gateway",
		"503 service unavailable",
		"504 gateway timeout",
		"i/o timeout",
		"tls handshake timeout",
		"connection reset by peer",
		"connection refused",
		"unexpected eof",
		"context deadline exceeded",
		"temporary failure in name resolution",
	}
)

// stripQuoted removes double-quoted spans from a message. Pull failures are
// formatted `Failed to pull image "<ref>": <error>`, and the reference is free
// text as far as marker matching is concerned — a repository path or tag can
// contain any marker word at all. Only the error text outside the quotes is
// evidence of what went wrong.
//
// Defence in depth rather than the sole guard: the markers above are also
// spelled strictly enough not to fire on a stray digit. This catches the other
// half, a reference like "registry/denied-team/app:v1" whose path would
// otherwise read as an authorization failure.
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

// pullClassMemo remembers, per involved-object UID, both the class of the most
// recent classifiable pull failure and the text of the most recent event that
// actually named a cause.
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
// The remembered text serves the other half of the same problem. All four events
// canonicalize to one dedup key, so whichever passes the gate first is the only one
// the agent is ever handed — there is no follow-up inject. Which one that is depends
// on a race between four independently incrementing Event.Count values, and three of
// the four say nothing about what went wrong. Measured on the cluster above, the
// causeless events do eventually pull ahead (12 versus 5 over three minutes), because
// "still backing off" is re-emitted on the pod-worker sync while the cause is
// re-emitted only when a retry actually fires. They overtake once the backoff
// interval exceeds the sync interval, which at the default threshold of 3 is well
// after the gate has released — all three live runs opened with the cause. Raise the
// threshold and that stops being true, so the cause is remembered rather than left to
// the race.
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
	cause    string
	recorded time.Time
}

// pullResolution is everything the dispatcher needs to know about an image-pull
// event that the event itself cannot tell it. Returned as one value so the class
// and the cause are read under a single lock and cannot disagree.
type pullResolution struct {
	// Class gates the event. See pullClass.
	Class pullClass
	// Cause is the most recent cause-bearing message seen for this object, which
	// may have arrived on an earlier event than the one being dispatched. Empty
	// when none has been seen.
	Cause string
}

// causeMarker is the prefix kubelet uses for the one event per pull attempt that
// carries the underlying error. The other three ("Error: ErrImagePull",
// "Back-off pulling image …", "Error: ImagePullBackOff") are bare statuses.
//
// Deliberately independent of whether the message classifies: an unrecognized
// cause is still a cause, and it is the text the agent most needs when the
// classifier has nothing to say about it.
const causeMarker = "Failed to pull image"

func carriesCause(message string) bool {
	return strings.Contains(message, causeMarker)
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

// Resolve classifies this event's own message and returns the class to act on
// together with the best cause text known for the object, remembering both for
// later causeless events about the same object.
//
// A message that classifies is recorded and returned. A message that does not —
// the causeless "Back-off pulling image" — falls back to what an earlier event
// about the same UID established. Terminal supersedes a remembered retryable:
// within one pod UID the image is fixed, so a terminal verdict arriving after a
// retryable one means the retry finally surfaced the real problem.
//
// The cause is tracked separately from the class because the two are not the same
// question. A message can name a cause the classifier does not recognize, and that
// text is worth keeping precisely because nothing else explains the failure.
//
// Safe on a nil receiver so a dispatcher built without a memo (tests, dry-run
// wiring) degrades to per-message resolution rather than panicking.
func (m *pullClassMemo) Resolve(uid, message string) pullResolution {
	class := classifyPullFailure(message)
	var cause string
	if carriesCause(message) {
		cause = message
	}
	if m == nil || uid == "" {
		return pullResolution{Class: class, Cause: cause}
	}
	now := m.clock()
	m.mu.Lock()
	defer m.mu.Unlock()

	// Whether this event said anything of its own. A bare "Error: ErrImagePull"
	// did not, and must not extend the entry's life: ttl is measured from the last
	// informative event, so a pod that recovers and later fails differently still
	// stops inheriting on schedule.
	informative := class != pullClassUnknown || cause != ""

	prev, ok := m.entries[uid]
	if ok && now.Sub(prev.recorded) > m.ttl {
		delete(m.entries, uid)
		ok = false
	}
	if ok {
		if cause == "" {
			cause = prev.cause
		}
		switch {
		case class == pullClassUnknown:
			// Nothing new about the class; inherit what an earlier event established.
			class = prev.class
		case class == pullClassRetryable && prev.class == pullClassTerminal:
			// Terminal already established for this object; a later retryable-looking
			// message does not un-break a bad tag. Recorded anyway, below, so a long
			// incident cannot age the terminal verdict out and start suppressing the
			// bad tag it already ruled on.
			class = pullClassTerminal
		}
	}
	if !informative {
		// Inheritance only: report what is known, write nothing.
		return pullResolution{Class: class, Cause: cause}
	}
	if !ok {
		m.evictIfFull(now)
	}
	m.entries[uid] = pullClassEntry{class: class, cause: cause, recorded: now}
	return pullResolution{Class: class, Cause: cause}
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
