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
	"context"
	"errors"
	"log"
	"sync"
	"time"
)

const (
	// excludedFlushInterval is the least time between two records for one key.
	// A chronic failure is sighted every few minutes for days; one record per
	// key per interval, carrying the sightings since the last, keeps that to
	// a few calls an hour while the daemon folds them into one row an hour.
	excludedFlushInterval = 15 * time.Minute
	// excludedFeatureRecheck is how long the daemon's answer to "do you take
	// excluded events?" is trusted before /healthz is asked again, so an agent
	// image rolled forward or back is noticed without asking on every record.
	excludedFeatureRecheck = 10 * time.Minute
	// excludedMaxEntries bounds the memo. Past it a new key's sightings are
	// dropped, and the next sweep logs how many.
	excludedMaxEntries = 10000
	// excludedEntryTTL is how long a key with nothing left to send is kept
	// after its last sighting, so a relist or a reconnect that replays the
	// same Event inside the API server's default Event TTL is not counted
	// twice.
	excludedEntryTTL = time.Hour
	// excludedPostTimeout bounds each call to the daemon, so one slow call
	// cannot hold the cluster's sweep for long.
	excludedPostTimeout = 2 * time.Second
	// excludedSweepBudget bounds the calls one sweep makes, so a memo full of
	// keys after a restart is sent over several sweeps instead of holding the
	// sweep goroutine. A key not reached keeps its sightings for the next.
	excludedSweepBudget = 5 * time.Second
	// excludedFinalFlushTimeout bounds the flush made when a cluster stops or
	// the process shuts down, which sends every key's pending sightings.
	excludedFinalFlushTimeout = 5 * time.Second
	// excludedFailureBackoff is how long the poster refuses at once after a
	// failed call, so a daemon that hangs costs one timeout a minute rather
	// than one per excluded event. The refused sightings are kept.
	excludedFailureBackoff = time.Minute
	// daemonFeatureAlertExcluded is what the daemon's /healthz lists when it
	// serves excludedEventsPath (session_kv_server ALERT_EXCLUDED_FEATURE).
	daemonFeatureAlertExcluded = "alert-excluded"
)

// errExcludedUnsupported is what the poster returns when the daemon does not
// serve excluded events: the sightings are dropped, not kept for later.
var errExcludedUnsupported = errors.New("the daemon does not take excluded events")

// errExcludedBackoff is what the poster returns inside excludedFailureBackoff
// after a failed call: the sightings are kept for the next record.
var errExcludedBackoff = errors.New("backing off after a failed call to the daemon")

// excludedKey is a memo key: the event's own dedup key and its container,
// since kubelet records one Event per container and the replay check reads one
// stamp sequence per key; and, for an event whose involved object has no UID,
// the object's namespace, kind and name too, so two nameless objects are not
// counted as one (the daemon folds such rows the same way). The daemon still
// writes one row per object and reason.
type excludedKey struct {
	EventKey
	Container             string
	Namespace, Kind, Name string
}

func excludedKeyFor(ev TriageEvent) excludedKey {
	key := excludedKey{EventKey: ev.Key, Container: ev.Container}
	if ev.Key.UID == "" {
		key.Namespace, key.Kind, key.Name = ev.Namespace, ev.KindOfObject, ev.Name
	}
	return key
}

// excludedRecord is the body of POST /v1/excluded-events: the event fields
// the daemon's ledger row keeps, and how many sightings it stands for.
type excludedRecord struct {
	Kind         string `json:"kind"`
	Reason       string `json:"reason"`
	Namespace    string `json:"namespace"`
	KindOfObject string `json:"kind_of_object"`
	Name         string `json:"name"`
	UID          string `json:"uid"`
	Message      string `json:"message"`
	Cluster      string `json:"cluster"`
	Type         string `json:"type"`
	Occurrences  int    `json:"occurrences"`
}

func excludedRecordFor(ev TriageEvent, occurrences int) excludedRecord {
	return excludedRecord{
		Kind:         injectKindEvent,
		Reason:       ev.Key.Reason,
		Namespace:    ev.Namespace,
		KindOfObject: ev.KindOfObject,
		Name:         ev.Name,
		UID:          ev.Key.UID,
		Message:      ev.Message,
		Cluster:      ev.Cluster,
		Type:         ev.Type,
		Occurrences:  occurrences,
	}
}

type excludedEntry struct {
	last     TriageEvent
	lastSeen time.Time
	notedAt  time.Time
	pending  int
	sentAt   time.Time
}

// excludedRecorder counts the events alert-exclude rules hold back and sends
// them to the daemon for the daily recap. It is its own memo, not the dedup
// cache: an entry there would deduplicate a real incident on the same key, and
// it is persisted across a pod roll that may have removed the rule.
//
// Note only counts; every call to the daemon is made from Sweep, off the
// cluster's delivery path, so a watcher restart that replays a broad rule's
// events cannot hold real alerts behind them.
type excludedRecorder struct {
	post func(context.Context, excludedRecord) error
	now  func() time.Time

	mu      sync.Mutex
	entries map[excludedKey]*excludedEntry
	dropped int
}

func newExcludedRecorder(post func(context.Context, excludedRecord) error) *excludedRecorder {
	return &excludedRecorder{post: post, entries: map[excludedKey]*excludedEntry{}}
}

func (r *excludedRecorder) clock() time.Time {
	if r.now != nil {
		return r.now()
	}
	return time.Now()
}

// Note counts one sighting of an excluded event for the next Sweep to send.
// A sighting whose LastSeen has not moved is a replay of one already counted,
// and is skipped. The memo is in memory only, so a watcher restart counts the
// events still inside the API server's Event TTL once more.
func (r *excludedRecorder) Note(ev TriageEvent) {
	now := r.clock()
	r.mu.Lock()
	defer r.mu.Unlock()
	key := excludedKeyFor(ev)
	e := r.entries[key]
	if e != nil && !ev.LastSeen.After(e.lastSeen) {
		return
	}
	if e == nil {
		if len(r.entries) >= excludedMaxEntries {
			r.dropped++
			return
		}
		e = &excludedEntry{}
		r.entries[key] = e
	}
	e.last, e.lastSeen, e.notedAt = ev, ev.LastSeen, now
	e.pending++
}

// Sweep sends each key's sightings since its last record: at once for a key
// never sent, and otherwise once excludedFlushInterval has passed since the
// last, within excludedSweepBudget. It forgets a key with nothing left to send
// once excludedEntryTTL has passed since its last sighting.
func (r *excludedRecorder) Sweep(ctx context.Context) {
	r.flush(ctx, false, excludedSweepBudget)
}

// Flush sends every key's pending sightings, due or not, until ctx ends: the
// last chance when the cluster's dispatcher stops or the process shuts down,
// since the memo is in memory only.
func (r *excludedRecorder) Flush(ctx context.Context) {
	r.flush(ctx, true, 0)
}

// flush sends the keys that are due (every key with sightings when all is
// set), stopping once budget has passed (no limit at 0) or ctx ends. A key
// it does not reach keeps its sightings and its place.
func (r *excludedRecorder) flush(ctx context.Context, all bool, budget time.Duration) {
	start := r.clock()
	type due struct {
		key excludedKey
	}
	var keys []due
	r.mu.Lock()
	dropped := r.dropped
	r.dropped = 0
	for key, e := range r.entries {
		if e.pending == 0 {
			if !all && start.Sub(e.notedAt) >= excludedEntryTTL && start.Sub(e.sentAt) >= excludedFlushInterval {
				delete(r.entries, key)
			}
			continue
		}
		if !all && !e.sentAt.IsZero() && start.Sub(e.sentAt) < excludedFlushInterval {
			continue
		}
		keys = append(keys, due{key})
	}
	r.mu.Unlock()
	if dropped > 0 {
		log.Printf("k8s-event-watcher: dropped %d excluded sighting(s) of new keys: %d keys are already held", dropped, excludedMaxEntries)
	}
	for _, k := range keys {
		if ctx.Err() != nil || (budget > 0 && r.clock().Sub(start) >= budget) {
			return
		}
		r.mu.Lock()
		e := r.entries[k.key]
		if e == nil || e.pending == 0 {
			r.mu.Unlock()
			continue
		}
		rec := excludedRecordFor(e.last, e.pending)
		e.pending, e.sentAt = 0, r.clock()
		r.mu.Unlock()
		r.send(ctx, k.key, rec)
	}
}

// send posts rec, and on a failure puts its sightings back on the key so the
// next record carries them. A daemon without the route drops them.
func (r *excludedRecorder) send(ctx context.Context, key excludedKey, rec excludedRecord) {
	err := r.post(ctx, rec)
	if err == nil || errors.Is(err, errExcludedUnsupported) {
		return
	}
	r.mu.Lock()
	if e := r.entries[key]; e != nil {
		e.pending += rec.Occurrences
	}
	r.mu.Unlock()
	if errors.Is(err, errExcludedBackoff) {
		return
	}
	log.Printf("k8s-event-watcher: could not record %d excluded sighting(s) of %s %s/%s: %v; kept for the next record",
		rec.Occurrences, rec.Reason, rec.Namespace, rec.Name, err)
}

// excludedPoster posts records through inj, only while the daemon's /healthz
// lists daemonFeatureAlertExcluded, asked again every excludedFeatureRecheck.
// Each call is bounded by excludedPostTimeout, and a failed one makes it
// refuse at once for excludedFailureBackoff. No lock is held across a call.
type excludedPoster struct {
	inj *injector
	now func() time.Time

	mu           sync.Mutex
	checkedAt    time.Time
	supported    bool
	backoffUntil time.Time
}

func (p *excludedPoster) clock() time.Time {
	if p.now != nil {
		return p.now()
	}
	return time.Now()
}

func (p *excludedPoster) Post(ctx context.Context, rec excludedRecord) error {
	now := p.clock()
	p.mu.Lock()
	if now.Before(p.backoffUntil) {
		p.mu.Unlock()
		return errExcludedBackoff
	}
	recheck := p.checkedAt.IsZero() || now.Sub(p.checkedAt) >= excludedFeatureRecheck
	supported := p.supported
	p.mu.Unlock()

	if recheck {
		callCtx, cancel := context.WithTimeout(ctx, excludedPostTimeout)
		features, err := p.inj.Features(callCtx)
		cancel()
		if err != nil {
			p.fail(ctx, now)
			return err
		}
		supported = hasFeature(features, daemonFeatureAlertExcluded)
		p.mu.Lock()
		p.supported, p.checkedAt = supported, now
		p.mu.Unlock()
	}
	if !supported {
		return errExcludedUnsupported
	}
	callCtx, cancel := context.WithTimeout(ctx, excludedPostTimeout)
	defer cancel()
	if err := p.inj.PostExcluded(callCtx, rec); err != nil {
		p.fail(ctx, now)
		return err
	}
	return nil
}

// fail arms the back-off, unless the caller's context had ended: a stop's
// cancel says nothing about the daemon, and a back-off armed by it would
// refuse the final Flush the stop makes next.
func (p *excludedPoster) fail(ctx context.Context, now time.Time) {
	if ctx.Err() != nil {
		return
	}
	p.mu.Lock()
	p.backoffUntil = now.Add(excludedFailureBackoff)
	p.mu.Unlock()
}
