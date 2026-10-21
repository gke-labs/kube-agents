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
	// defaultSettleEntries caps the held keys remembered per cluster, the
	// same bound as the other memos: a key is held for the settle period plus
	// settleQuietSlack at most, and a cluster churning through more pending
	// failures than this in seven minutes loses only the count of which of
	// them cleared, never a forward.
	defaultSettleEntries = 4096
	// settleOutcomeForwarded and settleOutcomeSelfResolved are the two
	// readings of k8s_event_watcher_settle_outcomes_total: a held key that
	// was sighted again past the period and forwarded, and one that was never
	// sighted again inside the period plus the slack.
	settleOutcomeForwarded    = "forwarded"
	settleOutcomeSelfResolved = "self_resolved"
	// defaultSettleClearedTTL bounds how long a cleared key's tombstone is
	// kept when the caller passes none: the dispatcher passes the dedup
	// window, past which a repeat is a new incident anyway.
	defaultSettleClearedTTL = 24 * time.Hour
)

// settleHold is one key the settle gate is holding: the event's canonical
// reason, where it was seen (the metric's labels), the watcher's clock at the
// last sighting, which the sweep ages, the event's own last sighting, which a
// tombstone keeps, and Start, the event time this blip began at when it is
// not the event object's first sighting (see settleCleared).
type settleHold struct {
	Reason        string
	Cluster       string
	Project       string
	Location      string
	LastSeen      time.Time
	EventLastSeen time.Time
	Start         time.Time
}

// settleCleared is the tombstone of a key the gate held and then counted
// self-resolved. kubelet keeps one BackOff event object per container for as
// long as the API server's TTL is renewed, so a pod that blips again inside
// it brings the old first sighting and a count already past the threshold:
// measured on the object alone, the second blip's very first sighting would
// span the gap and be forwarded at once. The tombstone remembers the event
// time the cleared blip ended at; the first sighting after it is the new
// blip's start, and the gate measures from there.
type settleCleared struct {
	EventLastSeen time.Time
	Reappeared    time.Time
	// At is the watcher's clock at expiry, which the TTL ages.
	At time.Time
}

// settleMemo remembers the keys the settle gate is holding so the dispatcher
// can say what became of each: forwarded once its sightings spanned the
// period, or self-resolved once it went unsighted for the period plus
// settleQuietSlack. The gate itself needs none of this (filter.go, settled);
// the memo exists so the suppression rate can be read off a counter rather
// than assumed, which the issue asked for. Per cluster, as the dedup cache
// is. Safe on a nil receiver, for a dispatcher built without one in tests.
type settleMemo struct {
	mu         sync.Mutex
	entries    map[string]settleHold
	cleared    map[string]settleCleared
	max        int
	clearedTTL time.Duration
}

func newSettleMemo(max int, clearedTTL time.Duration) *settleMemo {
	if max <= 0 {
		max = defaultSettleEntries
	}
	if clearedTTL <= 0 {
		clearedTTL = defaultSettleClearedTTL
	}
	return &settleMemo{entries: make(map[string]settleHold), cleared: make(map[string]settleCleared), max: max, clearedTTL: clearedTTL}
}

// settleKey is the memo's key: the namespace and UID the dedup key would use,
// with the canonical reason, since one pod can be held on two families.
func settleKey(ev TriageEvent, reason string) string {
	return ev.Namespace + scaleUpMemoKeySep + ev.Key.UID + scaleUpMemoKeySep + reason
}

// Hold records a sighting the gate held, at now. A full memo drops its oldest
// hold first, which costs one self-resolved count and nothing else. A hold
// that follows a tombstone takes the blip's start from it.
func (m *settleMemo) Hold(ev TriageEvent, reason string, now time.Time) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	key := settleKey(ev, reason)
	prev, held := m.entries[key]
	// A sighting that does not postdate a standing tombstone is a replay of
	// the cleared blip (an informer relist inside the event's TTL): it opens
	// no hold, or it would expire into a second self_resolved for one blip.
	if c, ok := m.cleared[key]; ok && !held && c.Reappeared.IsZero() && !ev.LastSeen.After(c.EventLastSeen) {
		return
	}
	if !held && len(m.entries) >= m.max {
		var oldestKey string
		var oldest time.Time
		first := true
		for k, h := range m.entries {
			if first || h.LastSeen.Before(oldest) {
				oldestKey, oldest, first = k, h.LastSeen, false
			}
		}
		delete(m.entries, oldestKey)
	}
	start := prev.Start
	// A tombstone is taken over only once BlipStart has recorded a
	// reappearance on it. A replay of the cleared blip is held too (its span
	// is the old short one), and must leave the tombstone where it is, or the
	// live sighting that follows would measure from the object's first
	// sighting and be forwarded at once.
	if c, ok := m.cleared[key]; ok && !c.Reappeared.IsZero() {
		start = c.Reappeared
		delete(m.cleared, key)
	}
	m.entries[key] = settleHold{Reason: reason, Cluster: ev.Cluster, Project: ev.Project, Location: ev.Location,
		LastSeen: now, EventLastSeen: ev.LastSeen, Start: start}
}

// BlipStart reports the event time the current blip of key began at, when it
// is later than the event object's own first sighting: the Start a hold
// carries, or, for a key whose last blip cleared, the first sighting after
// the tombstone's last one, recorded on this call. A sighting that does not
// postdate the tombstone is a replay of the cleared blip and adjusts nothing.
// Dispatch sets the event's FirstSeen to it before the gate reads the span,
// so a repeat blip is held like a first one.
func (m *settleMemo) BlipStart(ev TriageEvent, reason string, now time.Time) (time.Time, bool) {
	if m == nil {
		return time.Time{}, false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	key := settleKey(ev, reason)
	if h, ok := m.entries[key]; ok && !h.Start.IsZero() {
		return h.Start, true
	}
	// No hold, or a hold a replay opened with no start of its own: the
	// tombstone, if one stands, says where this blip begins.
	c, ok := m.cleared[key]
	if !ok {
		return time.Time{}, false
	}
	if now.Sub(c.At) > m.clearedTTL {
		delete(m.cleared, key)
		return time.Time{}, false
	}
	if !ev.LastSeen.After(c.EventLastSeen) {
		return time.Time{}, false
	}
	if c.Reappeared.IsZero() {
		c.Reappeared = ev.LastSeen
		m.cleared[key] = c
	}
	if h, ok := m.entries[key]; ok {
		h.Start = c.Reappeared
		m.entries[key] = h
		delete(m.cleared, key)
	}
	return c.Reappeared, true
}

// Release forgets a hold whose event has now passed the gate, and reports
// whether one was held: true is a forward the memo should count.
func (m *settleMemo) Release(ev TriageEvent, reason string) bool {
	if m == nil {
		return false
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	key := settleKey(ev, reason)
	_, ok := m.entries[key]
	delete(m.entries, key)
	return ok
}

// Expire drops every hold unsighted for quiet or longer, as of now, and
// returns them: each is a failure that cleared before the gate let it
// through. Each leaves a tombstone (settleCleared), and tombstones older
// than the memo's TTL go with them; the cap is shared with the holds.
func (m *settleMemo) Expire(now time.Time, quiet time.Duration) []settleHold {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []settleHold
	for key, h := range m.entries {
		if now.Sub(h.LastSeen) >= quiet {
			out = append(out, h)
			delete(m.entries, key)
			m.cleared[key] = settleCleared{EventLastSeen: h.EventLastSeen, At: now}
		}
	}
	for key, c := range m.cleared {
		if now.Sub(c.At) > m.clearedTTL {
			delete(m.cleared, key)
		}
	}
	for len(m.cleared) > m.max {
		var oldestKey string
		var oldest time.Time
		first := true
		for k, c := range m.cleared {
			if first || c.At.Before(oldest) {
				oldestKey, oldest, first = k, c.At, false
			}
		}
		delete(m.cleared, oldestKey)
	}
	return out
}

// Len reports the current hold count. Test helper.
func (m *settleMemo) Len() int {
	if m == nil {
		return 0
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.entries)
}
