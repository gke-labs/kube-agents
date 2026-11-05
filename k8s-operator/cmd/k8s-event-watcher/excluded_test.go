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
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// fakeExcludedPost records what the recorder sends, and fails while err is set.
type fakeExcludedPost struct {
	mu   sync.Mutex
	sent []excludedRecord
	err  error
}

func (f *fakeExcludedPost) post(_ context.Context, rec excludedRecord) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return f.err
	}
	f.sent = append(f.sent, rec)
	return nil
}

func (f *fakeExcludedPost) occurrences() []int {
	f.mu.Lock()
	defer f.mu.Unlock()
	var out []int
	for _, r := range f.sent {
		out = append(out, r.Occurrences)
	}
	return out
}

func excludedSighting(uid string, at time.Time) TriageEvent {
	return TriageEvent{
		Key:          EventKey{UID: uid, Reason: "BackOff"},
		Cluster:      "seeded-a",
		Namespace:    "payments",
		KindOfObject: "Pod",
		Name:         "payments-api-7d9f-abcde",
		Message:      "Back-off restarting failed container app",
		Type:         "Warning",
		LastSeen:     at,
	}
}

func newTestExcludedRecorder(now *time.Time) (*excludedRecorder, *fakeExcludedPost) {
	fake := &fakeExcludedPost{}
	r := newExcludedRecorder(fake.post)
	r.now = func() time.Time { return *now }
	return r, fake
}

func TestExcludedRecorderSendsNothingFromNote(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)

	r.Note(excludedSighting("pod-1", now))

	if got := fake.occurrences(); len(got) != 0 {
		t.Fatalf("Note sent %v; every send belongs to Sweep, off the delivery path", got)
	}
}

func TestExcludedRecorderSendsANewKeyOnTheNextSweep(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)

	r.Note(excludedSighting("pod-1", now))
	r.Sweep(context.Background())

	if got := fake.occurrences(); len(got) != 1 || got[0] != 1 {
		t.Fatalf("sent %v; want one record of 1 sighting", got)
	}
	rec := fake.sent[0]
	if rec.Kind != injectKindEvent || rec.Cluster != "seeded-a" || rec.Namespace != "payments" ||
		rec.UID != "pod-1" || rec.Reason != "BackOff" || rec.KindOfObject != "Pod" || rec.Type != "Warning" {
		t.Errorf("record = %+v", rec)
	}
}

func TestExcludedRecorderBatchesSightingsInsideTheInterval(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)
	ctx := context.Background()

	r.Note(excludedSighting("pod-1", now))
	r.Sweep(ctx)
	for i := 1; i <= 3; i++ {
		now = now.Add(time.Minute)
		r.Note(excludedSighting("pod-1", now))
	}
	r.Sweep(ctx)
	if got := fake.occurrences(); len(got) != 1 {
		t.Fatalf("a sweep inside the interval sent %v", got)
	}
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)
	if got := fake.occurrences(); len(got) != 2 || got[1] != 3 {
		t.Fatalf("sent %v; want the 3 batched sightings in a second record", got)
	}
}

// A replay of a sighting already counted (an informer relist, or a
// reconnect) has the same LastSeen and is not counted again.
func TestExcludedRecorderSkipsAReplay(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)
	ctx := context.Background()

	seen := now
	r.Note(excludedSighting("pod-1", seen))
	r.Note(excludedSighting("pod-1", seen))
	r.Sweep(ctx)
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)

	if got := fake.occurrences(); len(got) != 1 || got[0] != 1 {
		t.Fatalf("sent %v; a replay was counted", got)
	}
}

func TestExcludedRecorderKeepsTheSightingsOfAFailedSend(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)
	ctx := context.Background()

	fake.err = errors.New("daemon unavailable")
	r.Note(excludedSighting("pod-1", now))
	r.Sweep(ctx)
	now = now.Add(time.Minute)
	r.Note(excludedSighting("pod-1", now))

	fake.err = nil
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)
	if got := fake.occurrences(); len(got) != 1 || got[0] != 2 {
		t.Fatalf("sent %v; want one record carrying both sightings", got)
	}
}

// A back-off refusal keeps the sightings for the next record, as a failure
// does.
func TestExcludedRecorderKeepsTheSightingsOfABackOff(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)
	ctx := context.Background()

	fake.err = errExcludedBackoff
	r.Note(excludedSighting("pod-1", now))
	r.Sweep(ctx)
	fake.err = nil
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)

	if got := fake.occurrences(); len(got) != 1 || got[0] != 1 {
		t.Fatalf("sent %v; want the backed-off sighting in the next record", got)
	}
}

func TestExcludedRecorderDropsWhatADaemonWithoutTheRouteCannotTake(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)
	ctx := context.Background()

	fake.err = errExcludedUnsupported
	r.Note(excludedSighting("pod-1", now))
	r.Sweep(ctx)
	fake.err = nil
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)

	if got := fake.occurrences(); len(got) != 0 {
		t.Fatalf("sent %v; sightings refused as unsupported were kept", got)
	}
}

func TestExcludedRecorderForgetsAnIdleKey(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)
	ctx := context.Background()

	seen := now
	r.Note(excludedSighting("pod-1", seen))
	r.Sweep(ctx)
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)
	// Inside the entry TTL a replay of the same sighting is still known.
	r.Note(excludedSighting("pod-1", seen))
	now = now.Add(excludedFlushInterval)
	r.Sweep(ctx)
	if got := fake.occurrences(); len(got) != 1 {
		t.Fatalf("sent %v; a replay inside the entry TTL was counted", got)
	}
	now = now.Add(excludedEntryTTL)
	r.Sweep(ctx)

	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.entries) != 0 {
		t.Errorf("an idle key with nothing to send is still held: %d entries", len(r.entries))
	}
}

func TestExcludedRecorderKeepsOneEntryPerKey(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)

	r.Note(excludedSighting("pod-1", now))
	other := excludedSighting("pod-1", now)
	other.Key.Reason = "Failed"
	r.Note(other)
	r.Note(excludedSighting("pod-2", now))
	r.Sweep(context.Background())

	if got := fake.occurrences(); len(got) != 3 {
		t.Fatalf("sent %v; want one record per key", got)
	}
}

func TestExcludedRecorderDropsNewKeysPastTheBound(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	r, fake := newTestExcludedRecorder(&now)

	for i := range excludedMaxEntries + 1 {
		r.Note(excludedSighting(fmt.Sprintf("pod-%d", i), now))
	}
	r.mu.Lock()
	held, dropped := len(r.entries), r.dropped
	r.mu.Unlock()
	if held != excludedMaxEntries || dropped != 1 {
		t.Fatalf("held %d keys and dropped %d; want %d and 1", held, dropped, excludedMaxEntries)
	}
	r.Sweep(context.Background())
	if got := len(fake.occurrences()); got != excludedMaxEntries {
		t.Errorf("sent %d records; want one per held key", got)
	}
}

// excludedDaemon is a fake daemon serving /healthz with features and
// POST /v1/excluded-events.
type excludedDaemon struct {
	mu       sync.Mutex
	features []string
	health   int
	posted   []excludedRecord
	auth     []string
}

func (d *excludedDaemon) start(t *testing.T) *injector {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		d.mu.Lock()
		defer d.mu.Unlock()
		switch r.URL.Path {
		case healthzPath:
			d.health++
			_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", injectFeaturesKey: d.features})
		case excludedEventsPath:
			var rec excludedRecord
			if err := json.NewDecoder(r.Body).Decode(&rec); err != nil {
				t.Errorf("decode excluded record: %v", err)
			}
			d.posted = append(d.posted, rec)
			d.auth = append(d.auth, r.Header.Get("Authorization"))
			_ = json.NewEncoder(w).Encode(map[string]string{"status": "excluded"})
		default:
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
	}))
	t.Cleanup(srv.Close)
	inj, err := newInjector(injectorConfig{daemonURL: srv.URL, bearerToken: "mock-token", httpClient: srv.Client()})
	if err != nil {
		t.Fatal(err)
	}
	return inj
}

func TestExcludedPosterSendsOnlyToADaemonThatAdvertisesTheRoute(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	ctx := context.Background()
	rec := excludedRecordFor(excludedSighting("pod-1", now), 2)

	d := &excludedDaemon{features: []string{"followup"}}
	p := &excludedPoster{inj: d.start(t), now: func() time.Time { return now }}
	if err := p.Post(ctx, rec); !errors.Is(err, errExcludedUnsupported) {
		t.Fatalf("Post to a daemon without the feature: err = %v; want errExcludedUnsupported", err)
	}

	d.mu.Lock()
	d.features = []string{"followup", daemonFeatureAlertExcluded}
	d.mu.Unlock()
	// The answer is trusted until excludedFeatureRecheck has passed.
	if err := p.Post(ctx, rec); !errors.Is(err, errExcludedUnsupported) {
		t.Fatalf("Post inside the recheck interval: err = %v; want the cached answer", err)
	}
	now = now.Add(excludedFeatureRecheck)
	if err := p.Post(ctx, rec); err != nil {
		t.Fatalf("Post after the recheck: %v", err)
	}

	d.mu.Lock()
	defer d.mu.Unlock()
	if d.health != 2 {
		t.Errorf("healthz asked %d times; want 2", d.health)
	}
	if len(d.posted) != 1 || d.posted[0].Occurrences != 2 || d.posted[0].UID != "pod-1" || d.auth[0] != "Bearer mock-token" {
		t.Errorf("posted %+v with auth %v", d.posted, d.auth)
	}
}

// After a failed call the poster refuses at once for the back-off, keeping
// the sightings, and asks again afterwards.
func TestExcludedPosterBacksOffAfterAFailure(t *testing.T) {
	now := time.Unix(1_700_000_000, 0)
	ctx := context.Background()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	t.Cleanup(srv.Close)
	inj, err := newInjector(injectorConfig{daemonURL: srv.URL, bearerToken: "mock-token", httpClient: srv.Client()})
	if err != nil {
		t.Fatal(err)
	}
	p := &excludedPoster{inj: inj, now: func() time.Time { return now }}
	rec := excludedRecordFor(excludedSighting("pod-1", now), 1)

	if err := p.Post(ctx, rec); err == nil || errors.Is(err, errExcludedBackoff) {
		t.Fatalf("first Post: err = %v; want the daemon's failure", err)
	}
	if err := p.Post(ctx, rec); !errors.Is(err, errExcludedBackoff) {
		t.Fatalf("Post inside the back-off: err = %v; want errExcludedBackoff", err)
	}
	now = now.Add(excludedFailureBackoff)
	if err := p.Post(ctx, rec); err == nil || errors.Is(err, errExcludedBackoff) {
		t.Fatalf("Post after the back-off: err = %v; want a new attempt", err)
	}
}

// The dispatcher hands an event the rules matched to the recorder, and no
// other gate's event.
func TestDispatcherRecordsOnlyAlertExcludedEvents(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3})
	cfg := disp.filter.cfg
	cfg.alertExclude = []alertExcludeRule{{Namespace: "payments"}}
	disp.filter = newFilter(cfg)
	fake := &fakeExcludedPost{}
	disp.excluded = newExcludedRecorder(fake.post)
	ctx := context.Background()

	excluded := backoffEvent(5)
	excluded.Namespace = "payments"
	disp.Dispatch(ctx, excluded)
	held := backoffEvent(1)
	held.Key.UID = "pod-2"
	disp.Dispatch(ctx, held)
	disp.Sweep(ctx)

	if got := fake.occurrences(); len(got) != 1 || fake.sent[0].Namespace != "payments" {
		t.Errorf("recorded %+v; want only the excluded event", fake.sent)
	}
	if *injectCount != 0 {
		t.Errorf("fired %d injects; want 0", *injectCount)
	}
}

func TestADryRunDispatcherRecordsNothing(t *testing.T) {
	f := &flags{dryRun: true, dedupWindow: time.Minute}
	disp := newDispatcher(f, newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})), nil, nil, newMetrics())
	if disp.excluded != nil {
		t.Error("a dry-run dispatcher has an excluded-event recorder")
	}
}
