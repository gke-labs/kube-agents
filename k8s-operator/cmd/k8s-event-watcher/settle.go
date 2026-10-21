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
)

// settleHold is one key the settle gate is holding: the event's canonical
// reason, where it was seen (the metric's labels), and the watcher's clock at
// the last sighting, which the sweep ages.
type settleHold struct {
	Reason   string
	Cluster  string
	Project  string
	Location string
	LastSeen time.Time
}

// settleMemo remembers the keys the settle gate is holding so the dispatcher
// can say what became of each: forwarded once its sightings spanned the
// period, or self-resolved once it went unsighted for the period plus
// settleQuietSlack. The gate itself needs none of this (filter.go, settled);
// the memo exists so the suppression rate can be read off a counter rather
// than assumed, which the issue asked for. Per cluster, as the dedup cache
// is. Safe on a nil receiver, for a dispatcher built without one in tests.
type settleMemo struct {
	mu      sync.Mutex
	entries map[string]settleHold
	max     int
}

func newSettleMemo(max int) *settleMemo {
	if max <= 0 {
		max = defaultSettleEntries
	}
	return &settleMemo{entries: make(map[string]settleHold), max: max}
}

// settleKey is the memo's key: the namespace and UID the dedup key would use,
// with the canonical reason, since one pod can be held on two families.
func settleKey(ev TriageEvent, reason string) string {
	return ev.Namespace + scaleUpMemoKeySep + ev.Key.UID + scaleUpMemoKeySep + reason
}

// Hold records a sighting the gate held, at now. A full memo drops its oldest
// hold first, which costs one self-resolved count and nothing else.
func (m *settleMemo) Hold(ev TriageEvent, reason string, now time.Time) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	key := settleKey(ev, reason)
	if _, ok := m.entries[key]; !ok && len(m.entries) >= m.max {
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
	m.entries[key] = settleHold{Reason: reason, Cluster: ev.Cluster, Project: ev.Project, Location: ev.Location, LastSeen: now}
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
// returns them: each is a failure that cleared before the gate let it through.
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
		}
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
