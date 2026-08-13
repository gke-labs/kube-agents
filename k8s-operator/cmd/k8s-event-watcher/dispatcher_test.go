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
	"testing"
	"time"
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

	filter := newFilter(newFilterConfig(nil, nil, nil, 3))
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
				filter:   newFilter(newFilterConfig(nil, nil, nil, 3)),
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
		filter:   newFilter(newFilterConfig(nil, nil, nil, 3)),
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
		filter:   newFilter(newFilterConfig(nil, nil, nil, 3)),
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
