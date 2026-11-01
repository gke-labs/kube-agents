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

	answer, err := inj.InjectDetailed(context.Background(), sessionID, payload)
	if err != nil {
		t.Fatalf("Inject call failed: %v", err)
	}
	// The handler above answers 200 with no body, which is what a daemon
	// predating the status field looks like. It must read as delivered, not
	// as suppressed.
	if answer.Status == injectStatusSuppressed {
		t.Errorf("got status %q for a bodyless 200, want it treated as delivered", answer.Status)
	}
}

// TestInjectorInjectReportsSuppressedStatus covers the two cases the HTTP
// status code cannot express: the daemon accepted the inject and then dropped
// the alert against its daily ceiling, or held it back as informational.
// Inject must surface the body's word for which, because the caller rolls back
// its dedup entry for one of them and keeps it for the other.
func TestInjectorInjectReportsSuppressedStatus(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want string
	}{
		{name: "suppressed", body: `{"status":"suppressed","severity":"Warning","suppressed_today":"3"}`, want: injectStatusSuppressed},
		{name: "filtered", body: `{"status":"filtered"}`, want: injectStatusFiltered},
		{name: "injected", body: `{"status":"injected"}`, want: "injected"},
		{name: "empty body", body: "", want: ""},
		{name: "unparseable body", body: "not json", want: ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(http.StatusOK)
				_, _ = w.Write([]byte(tc.body))
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

			answer, err := inj.InjectDetailed(context.Background(), "active-session", InjectPayload{Reason: "BackOff"})
			if err != nil {
				t.Fatalf("Inject call failed: %v", err)
			}
			if answer.Status != tc.want {
				t.Errorf("got status %q, want %q", answer.Status, tc.want)
			}
		})
	}
}

// breakerServer answers POST /sessions with whatever status and Retry-After
// the test has set, and counts the calls.
type breakerServer struct {
	status     int
	retryAfter string
	calls      int
}

func newBreakerInjector(t *testing.T) (*injector, *breakerServer, *time.Time) {
	t.Helper()
	bs := &breakerServer{status: http.StatusCreated}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/sessions" {
			t.Errorf("unexpected path %s", r.URL.Path)
		}
		bs.calls++
		if bs.retryAfter != "" {
			w.Header().Set(retryAfterHeader, bs.retryAfter)
		}
		w.WriteHeader(bs.status)
		if bs.status == http.StatusCreated {
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "sess-1"})
		}
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
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	inj.breaker.now = func() time.Time { return now }
	return inj, bs, &now
}

// create POSTs once with the server answering status and ignores the error:
// the breaker's view is what these tests read.
func create(t *testing.T, inj *injector, bs *breakerServer, status int) {
	t.Helper()
	bs.status = status
	_, _ = inj.CreateSession(context.Background())
}

func held(t *testing.T, inj *injector) bool {
	t.Helper()
	_, h := inj.SessionCreatesSuppressedUntil()
	return h
}

// TestSessionBreakerOpensOnTheThirdConsecutiveFailure pins the threshold:
// two 503s leave creates flowing, the third holds them for the fixed
// cooldown, and the first call past the deadline lets them through again.
func TestSessionBreakerOpensOnTheThirdConsecutiveFailure(t *testing.T) {
	inj, bs, now := newBreakerInjector(t)
	for i := 0; i < sessionBreakerThreshold-1; i++ {
		create(t, inj, bs, http.StatusServiceUnavailable)
		if held(t, inj) {
			t.Fatalf("breaker open after %d failures; want it open only at %d", i+1, sessionBreakerThreshold)
		}
	}
	create(t, inj, bs, http.StatusServiceUnavailable)
	until, h := inj.SessionCreatesSuppressedUntil()
	if !h {
		t.Fatal("breaker closed after the third 503; want it open")
	}
	if want := now.Add(sessionBreakerCooldown); !until.Equal(want) {
		t.Errorf("held until %s; want %s", until, want)
	}
	*now = now.Add(sessionBreakerCooldown - time.Second)
	if !held(t, inj) {
		t.Fatal("breaker closed a second before the deadline")
	}
	*now = now.Add(2 * time.Second)
	if held(t, inj) {
		t.Fatal("breaker still open a second past the deadline")
	}
	// Closed means the count started over: one more failure does not
	// re-open it.
	create(t, inj, bs, http.StatusServiceUnavailable)
	if held(t, inj) {
		t.Fatal("one failure after the cooldown re-opened the breaker; want the count reset")
	}
}

// TestSessionBreakerCountsOnlyConsecutive429And5xx covers the statuses the
// count ignores: a 201 resets it, and a 404 (or any other non-busy answer)
// neither counts nor resets. 429 counts alongside the 5xx family.
func TestSessionBreakerCountsOnlyConsecutive429And5xx(t *testing.T) {
	t.Run("201 resets the count", func(t *testing.T) {
		inj, bs, _ := newBreakerInjector(t)
		create(t, inj, bs, http.StatusServiceUnavailable)
		create(t, inj, bs, http.StatusBadGateway)
		create(t, inj, bs, http.StatusCreated)
		create(t, inj, bs, http.StatusServiceUnavailable)
		create(t, inj, bs, http.StatusServiceUnavailable)
		if held(t, inj) {
			t.Fatal("breaker open after 503, 502, 201, 503, 503; want the 201 to have reset the count")
		}
	})
	t.Run("404 neither counts nor resets", func(t *testing.T) {
		inj, bs, _ := newBreakerInjector(t)
		for i := 0; i < sessionBreakerThreshold; i++ {
			create(t, inj, bs, http.StatusNotFound)
		}
		if held(t, inj) {
			t.Fatal("three 404s opened the breaker; want only 429 and 5xx to count")
		}
		create(t, inj, bs, http.StatusServiceUnavailable)
		create(t, inj, bs, http.StatusNotFound)
		create(t, inj, bs, http.StatusServiceUnavailable)
		if held(t, inj) {
			t.Fatal("breaker open at two 503s around a 404; want three")
		}
		create(t, inj, bs, http.StatusServiceUnavailable)
		if !held(t, inj) {
			t.Fatal("breaker closed after three 503s with a 404 between; want the 404 ignored")
		}
	})
	t.Run("429 counts", func(t *testing.T) {
		inj, bs, _ := newBreakerInjector(t)
		for i := 0; i < sessionBreakerThreshold; i++ {
			create(t, inj, bs, http.StatusTooManyRequests)
		}
		if !held(t, inj) {
			t.Fatal("three 429s left the breaker closed")
		}
	})
	t.Run("a transport error does not count", func(t *testing.T) {
		inj, bs, _ := newBreakerInjector(t)
		create(t, inj, bs, http.StatusServiceUnavailable)
		create(t, inj, bs, http.StatusServiceUnavailable)
		inj.cfg.daemonURL = "http://127.0.0.1:1"
		if _, err := inj.CreateSession(context.Background()); err == nil {
			t.Fatal("CreateSession against a closed port succeeded")
		}
		if held(t, inj) {
			t.Fatal("a refused connection opened the breaker; want only the daemon's answers to count")
		}
	})
}

// TestSessionBreakerHonoursRetryAfterUpToTheCap pins the cooldown arithmetic:
// the fixed 60s floor, a longer Retry-After in seconds or as an HTTP-date
// stretching it, and the 300s ceiling over anything larger.
func TestSessionBreakerHonoursRetryAfterUpToTheCap(t *testing.T) {
	for _, tc := range []struct {
		name       string
		retryAfter func(now time.Time) string
		want       time.Duration
	}{
		{name: "no header: the floor", retryAfter: func(time.Time) string { return "" }, want: 60 * time.Second},
		{name: "shorter than the floor: the floor", retryAfter: func(time.Time) string { return "10" }, want: 60 * time.Second},
		{name: "120 seconds", retryAfter: func(time.Time) string { return "120" }, want: 120 * time.Second},
		{name: "900 seconds: capped", retryAfter: func(time.Time) string { return "900" }, want: 300 * time.Second},
		{name: "HTTP-date 200s out", retryAfter: func(now time.Time) string { return now.Add(200 * time.Second).Format(http.TimeFormat) }, want: 200 * time.Second},
		{name: "HTTP-date in the past: the floor", retryAfter: func(now time.Time) string { return now.Add(-time.Minute).Format(http.TimeFormat) }, want: 60 * time.Second},
		{name: "garbage: the floor", retryAfter: func(time.Time) string { return "soon" }, want: 60 * time.Second},
	} {
		t.Run(tc.name, func(t *testing.T) {
			inj, bs, now := newBreakerInjector(t)
			bs.retryAfter = tc.retryAfter(*now)
			for i := 0; i < sessionBreakerThreshold; i++ {
				create(t, inj, bs, http.StatusServiceUnavailable)
			}
			until, h := inj.SessionCreatesSuppressedUntil()
			if !h {
				t.Fatal("breaker closed after three 503s")
			}
			if got := until.Sub(*now); got != tc.want {
				t.Errorf("cooldown %s; want %s", got, tc.want)
			}
			*now = now.Add(tc.want - time.Second)
			if !held(t, inj) {
				t.Errorf("breaker closed a second before its %s deadline", tc.want)
			}
			*now = now.Add(2 * time.Second)
			if held(t, inj) {
				t.Errorf("breaker open a second past its %s deadline", tc.want)
			}
		})
	}
}
