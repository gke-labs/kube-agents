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
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

func TestDispatcherDispatch_NewIncidentAndFollowUp(t *testing.T) {
	sessionID := "active-session-123"
	var createCount, injectCount int
	var lastInjectPayload InjectPayload

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST request, got %s", r.Method)
		}
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		if r.URL.Path == "/sessions/"+sessionID+"/inject" {
			injectCount++
			var req injectMessageRequest
			if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
				t.Fatalf("failed to decode body: %v", err)
			}
			if err := json.Unmarshal([]byte(req.Message), &lastInjectPayload); err != nil {
				t.Fatalf("failed to unmarshal message payload: %v", err)
			}
			w.WriteHeader(http.StatusOK)
			return
		}
		t.Errorf("unexpected endpoint %s", r.URL.Path)
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}

	filter := newFilter(newFilterConfig(nil, nil, nil, filterThresholds{}))
	dedup, err := newDedupCache(5*time.Minute, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}

	m := newMetrics()

	disp := &dispatcher{
		filter:   filter,
		dedup:    dedup,
		injector: inj,
		metrics:  m,
		mode:     "per-incident",
		dryRun:   false,
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "back-off restarting failed container",
		// At the crash-loop debounce threshold, so this exercises dispatch and
		// dedup rather than the leading-edge gate. Stated explicitly: leaving it
		// zero would also pass, but only via belowMinCount's fail-open branch,
		// which is a different behaviour than "a real crash loop got through".
		Count: 3,
	}

	// 1. Dispatch first event -> should create session and inject first event
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("expected 1 session creation, got %d", createCount)
	}
	if injectCount != 1 {
		t.Errorf("expected 1 injection, got %d", injectCount)
	}
	if lastInjectPayload.Kind != injectKindEvent {
		t.Errorf("expected first event kind to be %q, got %q", injectKindEvent, lastInjectPayload.Kind)
	}
	if lastInjectPayload.Cluster != "test-cluster" {
		t.Errorf("expected payload Cluster to come from ev.Cluster (%q), got %q", "test-cluster", lastInjectPayload.Cluster)
	}

	// 2. Dispatch same event again -> should not create session and should suppress injection
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("expected session creation count to remain 1, got %d", createCount)
	}
	if injectCount != 1 {
		t.Errorf("expected injections count to remain 1, got %d", injectCount)
	}
}

// TestDispatcherRollsBackDedupOnDeliveryFailure pins the invariant that makes a
// long dedup window safe: an entry must not outlive an alert that was never
// delivered. Observe writes the entry before either network call, and with a
// deployed window of 24h a steadily-failing workload never produces the quiet
// gap Case 2 needs, so an un-rolled-back entry means permanent silence for
// exactly the failure the window is tuned for.
func TestDispatcherRollsBackDedupOnDeliveryFailure(t *testing.T) {
	const sessionID = "session-1"

	for _, tc := range []struct {
		name            string
		failCreate      bool
		wantCreateCalls int
	}{
		// The daemon is not listening at all — a first-install pod whose
		// Session KV server has not come up yet.
		{name: "create session fails", failCreate: true, wantCreateCalls: 2},
		// The session is created but the payload cannot be delivered.
		{name: "inject fails", failCreate: false, wantCreateCalls: 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var createCount, injectCount int
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/sessions" {
					createCount++
					if tc.failCreate {
						w.WriteHeader(http.StatusServiceUnavailable)
						return
					}
					w.WriteHeader(http.StatusCreated)
					_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
					return
				}
				injectCount++
				w.WriteHeader(http.StatusInternalServerError)
			}))
			defer server.Close()

			inj, err := newInjector(injectorConfig{
				daemonURL:   server.URL,
				bearerToken: "mock-token",
				httpClient:  server.Client(),
			})
			if err != nil {
				t.Fatalf("failed to build injector: %v", err)
			}
			dedup, err := newDedupCache(24*time.Hour, "")
			if err != nil {
				t.Fatalf("failed to build cache: %v", err)
			}
			disp := &dispatcher{
				filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
				dedup:    dedup,
				injector: inj,
				metrics:  newMetrics(),
				mode:     "per-incident",
			}

			ev := TriageEvent{
				Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
				Cluster:   "test-cluster",
				Namespace: "default",
				Name:      "billing-service",
				LastSeen:  time.Now(),
				Message:   "back-off restarting failed container",
			}

			disp.Dispatch(context.Background(), ev)
			if dedup.Len() != 0 {
				t.Fatalf("a failed dispatch left %d dedup entries; want 0", dedup.Len())
			}

			// The kubelet's next repeat, well inside the window. It must be
			// treated as a new incident, not suppressed against the alert
			// that never went out.
			ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
			disp.Dispatch(context.Background(), ev)
			if createCount != tc.wantCreateCalls {
				t.Errorf("got %d create-session calls; want %d (the retry was suppressed)",
					createCount, tc.wantCreateCalls)
			}
		})
	}
}

// TestDispatcherRollsBackDedupOnQuotaSuppression covers the delivery failure
// that arrives as a success. The daemon answers 200 {"status":"suppressed"}
// when the day's ceiling for the event's severity is spent — no chat post, no
// agent turn. HTTP-wise that is indistinguishable from a delivered alert, so
// without reading the body the watcher would keep a dedup entry for an alert
// nobody received, and at a 24h window the ceiling would reset at 00:00 UTC
// long before the entry did.
func TestDispatcherRollsBackDedupOnQuotaSuppression(t *testing.T) {
	const sessionID = "session-1"
	var createCount, injectCount int

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		injectCount++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"suppressed","severity":"Warning","suppressed_today":"1"}`))
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:    dedup,
		injector: inj,
		metrics:  newMetrics(),
		mode:     "per-incident",
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "back-off restarting failed container",
	}

	disp.Dispatch(context.Background(), ev)
	if dedup.Len() != 0 {
		t.Fatalf("a quota-suppressed alert left %d dedup entries; want 0", dedup.Len())
	}

	ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
	disp.Dispatch(context.Background(), ev)
	if injectCount != 2 {
		t.Errorf("got %d inject calls; want 2 (the re-offer was suppressed locally)", injectCount)
	}
	if createCount != 2 {
		t.Errorf("got %d create-session calls; want 2", createCount)
	}
}

// TestDispatcherKeepsDedupEntryOnSuccess is the other half: a delivered alert
// must still suppress its repeats, or the rollback above would have traded
// silence for a flood.
func TestDispatcherKeepsDedupEntryOnSuccess(t *testing.T) {
	const sessionID = "session-1"
	var createCount int

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			createCount++
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to build injector: %v", err)
	}
	dedup, err := newDedupCache(24*time.Hour, "")
	if err != nil {
		t.Fatalf("failed to build cache: %v", err)
	}
	disp := &dispatcher{
		filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:    dedup,
		injector: inj,
		metrics:  newMetrics(),
		mode:     "per-incident",
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "billing-service",
		LastSeen:  time.Now(),
		Message:   "back-off restarting failed container",
	}

	disp.Dispatch(context.Background(), ev)
	if dedup.Len() != 1 {
		t.Fatalf("a delivered alert left %d dedup entries; want 1", dedup.Len())
	}

	ev.LastSeen = ev.LastSeen.Add(5 * time.Minute)
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("got %d create-session calls; want 1 (the repeat was not suppressed)", createCount)
	}
}

// newCountingDispatcher builds a dispatcher against a stub daemon, returning the
// dispatcher, its metrics, a pointer to the running inject count, and the payloads
// the daemon actually received. Zero thresholds take the shipped defaults, the same
// as on the command line.
//
// The payloads matter as much as the count: a debounce that fires the right number
// of times but hands the agent a message with no error text in it has not done its
// job, and a count-only assertion cannot tell the difference.
func newCountingDispatcher(t *testing.T, th filterThresholds) (*dispatcher, *metrics, *int, *[]InjectPayload) {
	t.Helper()
	sessionID := "sess-1"
	injectCount := 0
	var injected []InjectPayload
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: sessionID})
			return
		}
		injectCount++
		var req injectMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode inject request: %v", err)
		}
		var p InjectPayload
		if err := json.Unmarshal([]byte(req.Message), &p); err != nil {
			t.Errorf("unmarshal inject payload: %v", err)
		}
		injected = append(injected, p)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(server.Close)

	inj, err := newInjector(injectorConfig{
		daemonURL:   server.URL,
		bearerToken: "mock-token",
		httpClient:  server.Client(),
	})
	if err != nil {
		t.Fatalf("build injector: %v", err)
	}
	dedup, err := newDedupCache(5*time.Minute, "")
	if err != nil {
		t.Fatalf("build cache: %v", err)
	}
	m := newMetrics()
	return &dispatcher{
		filter:      newFilter(newFilterConfig(nil, nil, nil, th)),
		dedup:       dedup,
		pullClasses: newPullClassMemo(0, 0),
		injector:    inj,
		metrics:     m,
		mode:        "per-incident",
	}, m, &injectCount, &injected
}

// backoffEvent is the event kubelet actually emits while a container is
// restarting: Reason=BackOff, not Reason=CrashLoopBackOff.
func backoffEvent(count int) TriageEvent {
	return TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "BackOff"},
		Cluster:   "test-cluster",
		Namespace: "default",
		Name:      "api",
		Message:   "Back-off restarting failed container app in pod api-7d9f",
		LastSeen:  time.Now(),
		Count:     count,
	}
}

// TestDispatcherCrashLoopDebounce walks the sequence the gate exists for: a pod
// blips twice during a node scale-up and recovers. Under the shipped default of
// 3 that must produce no session at all — not a session plus a later "resolved".
//
// The dedup cache is asserted empty afterwards because a held event must not
// leave state behind: an entry created by the blip would make the *real* crash
// loop that follows an hour later look like a duplicate and suppress it.
func TestDispatcherCrashLoopDebounce(t *testing.T) {
	disp, m, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3})
	ctx := context.Background()

	disp.Dispatch(ctx, backoffEvent(1))
	disp.Dispatch(ctx, backoffEvent(2))

	if *injectCount != 0 {
		t.Errorf("transient crash loop fired %d injects; want 0", *injectCount)
	}
	if got := disp.dedup.Len(); got != 0 {
		t.Errorf("held events left %d dedup entries; want 0", got)
	}
	if got := testutil.ToFloat64(m.eventsFiltered.WithLabelValues("test-cluster", "", "", string(gateBackoffMinCount))); got != 2 {
		t.Errorf("events_filtered_total{gate=backoff_min_count} = %v; want 2", got)
	}

	// The same pod keeps crashing. At the threshold it has to get through.
	disp.Dispatch(ctx, backoffEvent(3))
	if *injectCount != 1 {
		t.Errorf("sustained crash loop fired %d injects; want 1", *injectCount)
	}
}

// TestDispatcherCrashLoopDebounceDisabled pins the escape hatch: --backoff-min-count=1
// is the pre-debounce behaviour, firing on the first event.
func TestDispatcherCrashLoopDebounceDisabled(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 1})

	disp.Dispatch(context.Background(), backoffEvent(1))

	if *injectCount != 1 {
		t.Errorf("with backoff-min-count=1, first event fired %d injects; want 1", *injectCount)
	}
}

// TestDispatcherImagePullNotDebounced guards the exemption from the *crash-loop*
// gate. Nothing established a cause for this pod, so the class is unknown and the
// event has to fire on #1 — the pre-classifier behaviour.
func TestDispatcherImagePullNotDebounced(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{backoffMinCount: 3, imagePullTransientMinCount: 3})

	ev := backoffEvent(1)
	ev.Message = `Back-off pulling image "example.com/app:nope"`
	disp.Dispatch(context.Background(), ev)

	if *injectCount != 1 {
		t.Errorf("image-pull back-off fired %d injects; want 1", *injectCount)
	}
}

// pullFailedEvent is the cause-bearing half of the kubelet pair. Reason=Failed
// is not in the shipped default --reason list, so the filter drops it — the
// point of dispatching it is the class the memo records on the way past.
func pullFailedEvent(msg string) TriageEvent {
	ev := backoffEvent(1)
	ev.Key.Reason = "Failed"
	ev.Message = msg
	return ev
}

// pullBackOffEvent is the causeless half kubelet emits ten seconds later.
func pullBackOffEvent(count int) TriageEvent {
	ev := backoffEvent(count)
	ev.Message = `Back-off pulling image "us-docker.pkg.dev/proj/repo/app:v1"`
	ev.Count = count
	return ev
}

// TestDispatcherRegistryThrottleDebounced is the drill the memo exists for. The
// registry throttles a pull; kubelet says so once, on an event the filter then
// drops, and every event after that is a back-off carrying no cause at all.
// Classifying each message where it stands would hold the 429 and fire on the
// causeless back-off, which is worse than not classifying — so this fails
// without the carry-forward, not merely without the gate.
func TestDispatcherRegistryThrottleDebounced(t *testing.T) {
	disp, m, injectCount, injected := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	ctx := context.Background()

	const throttle = `Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": failed to pull and unpack image: unexpected status from HEAD request: 429 Too Many Requests`
	disp.Dispatch(ctx, pullFailedEvent(throttle))
	disp.Dispatch(ctx, pullBackOffEvent(1))
	disp.Dispatch(ctx, pullBackOffEvent(2))

	if *injectCount != 0 {
		t.Errorf("throttled pull fired %d injects; want 0", *injectCount)
	}
	if got := disp.dedup.Len(); got != 0 {
		t.Errorf("held events left %d dedup entries; want 0", got)
	}
	if got := testutil.ToFloat64(m.eventsFiltered.WithLabelValues("test-cluster", "", "", string(gateImagePullTransient))); got != 2 {
		t.Errorf("events_filtered_total{gate=imagepull_transient_min_count} = %v; want 2", got)
	}

	// The registry has not let up. At the threshold it stops being transient.
	disp.Dispatch(ctx, pullBackOffEvent(3))
	if *injectCount != 1 {
		t.Fatalf("sustained throttle fired %d injects; want 1", *injectCount)
	}

	// Firing is only half of it. The event that won is a back-off with no error
	// text in it, and there is no follow-up inject, so without pull_cause the
	// agent is handed a sustained registry throttle and told nothing about it.
	got := (*injected)[0]
	if strings.Contains(got.Message, "429") {
		t.Fatalf("message unexpectedly carries the cause (%q) — this drill no longer covers the causeless case", got.Message)
	}
	if got.PullCause != throttle {
		t.Errorf("pull_cause = %q; want the 429 text the earlier event carried", got.PullCause)
	}
}

// TestDispatcherThrottleFiresOnCauseBearingEvent runs the same incident with the
// deployed --reason list, which does include Failed, and with both counters
// climbing the way kubelet actually increments them.
//
// Measured on GKE v1.36.2-gke.2064000, the causeless events do outrun the
// cause-bearing one over a few minutes (12 versus 5), because "still backing off"
// is re-emitted on the pod-worker sync while the cause is re-emitted only when a
// retry fires. They overtake once the backoff interval exceeds the sync interval,
// which is after a threshold of 3 has already released — every live run opened
// with the cause. This pins that ordering so a threshold change has to confront
// it, and asserts pull_cause is omitted when the message already says it.
func TestDispatcherThrottleFiresOnCauseBearingEvent(t *testing.T) {
	disp, _, injectCount, injected := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	disp.filter = newFilter(newFilterConfig([]string{"Failed", "BackOff"}, nil, nil, filterThresholds{imagePullTransientMinCount: 3}))
	ctx := context.Background()

	const throttle = `Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": unexpected status from HEAD request: 429 Too Many Requests`
	cause := func(count int) TriageEvent {
		ev := pullFailedEvent(throttle)
		ev.Count = count
		return ev
	}

	// Attempt 1, then backoff; attempt 2, then backoff. The cause event reaches
	// the threshold on attempt 3, before the back-off counter gets there.
	disp.Dispatch(ctx, cause(1))
	disp.Dispatch(ctx, pullBackOffEvent(1))
	disp.Dispatch(ctx, cause(2))
	disp.Dispatch(ctx, pullBackOffEvent(2))
	if *injectCount != 0 {
		t.Fatalf("fired %d injects before the threshold; want 0", *injectCount)
	}

	disp.Dispatch(ctx, cause(3))
	if *injectCount != 1 {
		t.Fatalf("sustained throttle fired %d injects; want 1", *injectCount)
	}
	got := (*injected)[0]
	if got.Message != throttle {
		t.Errorf("message = %q; want the cause-bearing event to be the one injected", got.Message)
	}
	if got.PullCause != "" {
		t.Errorf("pull_cause = %q; want it omitted when message already carries the cause", got.PullCause)
	}
}

// TestDispatcherBadTagNotDebounced is the same shape with the opposite cause.
// A tag that does not exist is not going to start existing, so the carry-forward
// must not delay it.
func TestDispatcherBadTagNotDebounced(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	ctx := context.Background()

	disp.Dispatch(ctx, pullFailedEvent(`Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": manifest unknown`))
	disp.Dispatch(ctx, pullBackOffEvent(1))

	if *injectCount != 1 {
		t.Errorf("bad tag fired %d injects; want 1", *injectCount)
	}
}

// TestDispatcherPullClassIsPerObject pins the memo key. One pod being throttled
// must not delay a different pod's bad tag.
func TestDispatcherPullClassIsPerObject(t *testing.T) {
	disp, _, injectCount, _ := newCountingDispatcher(t, filterThresholds{imagePullTransientMinCount: 3})
	ctx := context.Background()

	disp.Dispatch(ctx, pullFailedEvent(`Failed to pull image "us-docker.pkg.dev/proj/repo/app:v1": 429 Too Many Requests`))

	other := pullBackOffEvent(1)
	other.Key.UID = "pod-2"
	disp.Dispatch(ctx, other)

	if *injectCount != 1 {
		t.Errorf("unrelated pod fired %d injects; want 1", *injectCount)
	}
}
