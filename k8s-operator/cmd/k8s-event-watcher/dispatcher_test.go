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
		filter:    filter,
		dedup:     dedup,
		injector:  inj,
		metrics:   m,
		cluster:   "test-cluster",
		mode:      "per-incident",
		dryRun:    false,
	}

	ev := TriageEvent{
		Key:       EventKey{UID: "pod-1", Reason: "CrashLoopBackOff"},
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

	// 2. Dispatch same event again -> should not create session and should suppress injection
	disp.Dispatch(context.Background(), ev)
	if createCount != 1 {
		t.Errorf("expected session creation count to remain 1, got %d", createCount)
	}
	if injectCount != 1 {
		t.Errorf("expected injections count to remain 1, got %d", injectCount)
	}
}
