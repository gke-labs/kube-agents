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
)

func TestInjectorCreateSession(t *testing.T) {
	expectedSessionID := "test-session-12345"
	
	// Create mock HTTP server
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST request, got %s", r.Method)
		}
		if r.URL.Path != "/sessions" {
			t.Errorf("expected path /sessions, got %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer mock-token" {
			t.Errorf("expected Bearer token header, got %s", r.Header.Get("Authorization"))
		}
		if r.Header.Get("X-Asserted-Caller") != "test-owner" {
			t.Errorf("expected X-Asserted-Caller test-owner, got %s", r.Header.Get("X-Asserted-Caller"))
		}

		w.WriteHeader(http.StatusCreated)
		resp := createSessionResponse{
			SessionID: expectedSessionID,
			AppName:   "platform-agent",
		}
		_ = json.NewEncoder(w).Encode(resp)
	}))
	defer server.Close()

	inj, err := newInjector(injectorConfig{
		daemonURL:      server.URL,
		bearerToken:    "mock-token",
		assertedCaller: "test-owner",
		httpClient:     server.Client(),
	})
	if err != nil {
		t.Fatalf("failed to create injector: %v", err)
	}

	sid, err := inj.CreateSession(context.Background())
	if err != nil {
		t.Fatalf("failed CreateSession call: %v", err)
	}
	if sid != expectedSessionID {
		t.Errorf("got session ID %q, want %q", sid, expectedSessionID)
	}
}

func TestInjectorInject(t *testing.T) {
	sessionID := "active-session"
	payload := InjectPayload{
		Reason:    "FailedScheduling",
		Namespace: "default",
		Name:      "test-pod",
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST request, got %s", r.Method)
		}
		expectedPath := "/sessions/" + sessionID + "/inject"
		if r.URL.Path != expectedPath {
			t.Errorf("expected path %s, got %s", expectedPath, r.URL.Path)
		}

		var req injectMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("failed to decode inject request body: %v", err)
		}

		var parsedPayload InjectPayload
		if err := json.Unmarshal([]byte(req.Message), &parsedPayload); err != nil {
			t.Fatalf("failed to unmarshal message payload: %v", err)
		}

		if parsedPayload.Reason != payload.Reason || parsedPayload.Name != payload.Name {
			t.Errorf("incorrect payload; got %+v, want %+v", parsedPayload, payload)
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

	err = inj.Inject(context.Background(), sessionID, payload)
	if err != nil {
		t.Fatalf("Inject call failed: %v", err)
	}
}
