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
// evicts the oldest — the same bounded-scan approach dedupCache uses, on a map
// an order of magnitude smaller.
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
	var oldestUID string
	var oldest time.Time
	first := true
	for uid, e := range b.entries {
		if first || e.at.Before(oldest) {
			oldestUID, oldest, first = uid, e.at, false
		}
	}
	delete(b.entries, oldestUID)
}

func (b *boundedEntries[V]) len() int {
	return len(b.entries)
}
