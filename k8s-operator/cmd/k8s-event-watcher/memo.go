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

import "time"

// boundedEntries is the per-UID map the watcher's memos share: a value and
// the time it is dated, expiring after ttl and capped at max entries with
// expired entries evicted before the oldest. pullClassMemo dates an entry by
// arrival and scaleUpMemo by the event's own timestamp; what "latest" means
// and what supersedes what stays with each memo. Not safe for concurrent use:
// each memo holds its own lock around these calls.
type boundedEntries[V any] struct {
	entries map[string]memoEntry[V]
	ttl     time.Duration
	max     int
	// groupOf names the group a key belongs to, when set. A full map then
	// evicts the oldest entry of the group holding the most entries rather
	// than the oldest entry overall, so a burst of keys in one group
	// displaces that group's own entries before another's; nil groups
	// nothing, and the oldest entry in the map goes.
	groupOf func(key string) string
}

type memoEntry[V any] struct {
	value V
	at    time.Time
}

func newBoundedEntries[V any](ttl time.Duration, max int) boundedEntries[V] {
	return boundedEntries[V]{entries: make(map[string]memoEntry[V]), ttl: ttl, max: max}
}

// lookup returns the entry for uid and its date, dropping and reporting
// absent one that has aged past ttl as of now.
func (b *boundedEntries[V]) lookup(uid string, now time.Time) (V, time.Time, bool) {
	e, ok := b.entries[uid]
	if !ok {
		var zero V
		return zero, time.Time{}, false
	}
	if now.Sub(e.at) > b.ttl {
		delete(b.entries, uid)
		var zero V
		return zero, time.Time{}, false
	}
	return e.value, e.at, true
}

// store writes value for uid dated at, making room first when uid is new.
func (b *boundedEntries[V]) store(uid string, value V, at, now time.Time) {
	if _, ok := b.entries[uid]; !ok {
		b.evictIfFull(now)
	}
	b.entries[uid] = memoEntry[V]{value: value, at: at}
}

// evictIfFull drops expired entries first, and only if that frees nothing
// evicts one live entry (evictee) — the same bounded-scan approach dedupCache
// uses, on a map an order of magnitude smaller.
func (b *boundedEntries[V]) evictIfFull(now time.Time) {
	if len(b.entries) < b.max {
		return
	}
	for uid, e := range b.entries {
		if now.Sub(e.at) > b.ttl {
			delete(b.entries, uid)
		}
	}
	if len(b.entries) < b.max {
		return
	}
	delete(b.entries, b.evictee())
}

// evictee picks the live entry a full map gives up: the oldest in the map
// when keys are not grouped, otherwise the oldest in the group holding the
// most entries. Between groups of equal size the one whose oldest entry is
// older gives it up, and between those the lower group name, so the choice
// does not depend on map order. A group can therefore lose an entry only to
// a group holding at least as many, which is what keeps a burst of keys in
// one group, forged or real, from emptying another's share.
func (b *boundedEntries[V]) evictee() string {
	type groupOldest struct {
		key   string
		at    time.Time
		count int
	}
	groups := make(map[string]*groupOldest)
	for key, e := range b.entries {
		var group string
		if b.groupOf != nil {
			group = b.groupOf(key)
		}
		g, ok := groups[group]
		if !ok {
			groups[group] = &groupOldest{key: key, at: e.at, count: 1}
			continue
		}
		g.count++
		if e.at.Before(g.at) || (e.at.Equal(g.at) && key < g.key) {
			g.key, g.at = key, e.at
		}
	}
	var chosenName string
	var chosen *groupOldest
	for name, g := range groups {
		if chosen == nil ||
			g.count > chosen.count ||
			(g.count == chosen.count && g.at.Before(chosen.at)) ||
			(g.count == chosen.count && g.at.Equal(chosen.at) && name < chosenName) {
			chosenName, chosen = name, g
		}
	}
	return chosen.key
}

func (b *boundedEntries[V]) len() int {
	return len(b.entries)
}
