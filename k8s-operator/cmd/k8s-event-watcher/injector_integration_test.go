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

// The cross-language half of the injector <-> session_kv_server seam test.
//
// Every other test in this package runs against a same-language fake of the
// daemon; this one runs against whatever SESSION_KV_INTEGRATION_URL points at,
// which tests/integration/test_seam_injector_kv.py sets to a real
// session_kv_server on a loopback port. Skipped without the variable, so a
// bare `go test ./...` is unchanged.
//
// The Python side owns the fixture; this side owns the client semantics: a
// created session id round-trips, the double-JSON envelope delivers the
// payload fields, a spent quota reads back as injectStatusSuppressed, and a
// bad bearer is an error rather than a delivery.

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

func liveInjector(t *testing.T, token string) *injector {
	t.Helper()
	url := os.Getenv("SESSION_KV_INTEGRATION_URL")
	if url == "" {
		t.Skip("SESSION_KV_INTEGRATION_URL is not set; the Python seam test drives this")
	}
	if token == "" {
		token = os.Getenv("SESSION_KV_INTEGRATION_TOKEN")
	}
	inj, err := newInjector(injectorConfig{
		daemonURL:      strings.TrimSuffix(url, "/"),
		bearerToken:    token,
		assertedCaller: "k8s-event-watcher-integration",
	})
	if err != nil {
		t.Fatalf("newInjector: %v", err)
	}
	return inj
}

func liveContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	t.Cleanup(cancel)
	return ctx
}

func testPayload(reason string) InjectPayload {
	return InjectPayload{
		Kind:         injectKindEvent,
		Reason:       reason,
		Namespace:    "payments",
		KindOfObject: "Pod",
		Name:         "payments-api-6cfdb6b98b-zwv24",
		UID:          "uid-1",
		Message:      "back-off restarting failed container",
		Count:        7,
		FirstSeen:    time.Now().Add(-time.Hour),
	}
}

// TestLiveKVCreateAndInject: session create -> inject -> "injected", the
// documented happy path, against the real daemon.
func TestLiveKVCreateAndInject(t *testing.T) {
	inj := liveInjector(t, "")
	ctx := liveContext(t)

	sessionID, err := inj.CreateSession(ctx)
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	if !strings.HasPrefix(sessionID, "k8s-evt-") {
		t.Errorf("sessionID %q does not carry the daemon's k8s-evt- prefix", sessionID)
	}

	status, err := inj.Inject(ctx, sessionID, testPayload("CrashLoopBackOff"))
	if err != nil {
		t.Fatalf("Inject: %v", err)
	}
	if status != "injected" {
		t.Errorf("expected status injected, got %q", status)
	}
}

// TestLiveKVQuotaSuppression: the daemon's 200-with-suppressed contract. The
// Python side starts this daemon with ALERT_DAILY_LIMIT_CRITICAL=1, so the
// second Critical inject must read back as the suppressed status the
// dispatcher keys its dedup rollback on.
func TestLiveKVQuotaSuppression(t *testing.T) {
	if os.Getenv("SESSION_KV_INTEGRATION_QUOTA") != "1" {
		t.Skip("quota fixture not configured for this run")
	}
	inj := liveInjector(t, "")
	ctx := liveContext(t)

	first, err := inj.CreateSession(ctx)
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	status, err := inj.Inject(ctx, first, testPayload("CrashLoopBackOff"))
	if err != nil {
		t.Fatalf("first Inject: %v", err)
	}
	if status != "injected" {
		t.Fatalf("first inject expected injected, got %q", status)
	}

	second, err := inj.CreateSession(ctx)
	if err != nil {
		t.Fatalf("CreateSession: %v", err)
	}
	status, err = inj.Inject(ctx, second, testPayload("CrashLoopBackOff"))
	if err != nil {
		t.Fatalf("second Inject: %v", err)
	}
	if status != injectStatusSuppressed {
		t.Errorf("expected %q on the spent quota, got %q", injectStatusSuppressed, status)
	}
}

// TestLiveKVBadBearer: an invalid key must surface as an error from both
// endpoints, never as a quiet success.
func TestLiveKVBadBearer(t *testing.T) {
	inj := liveInjector(t, "not-the-key")
	ctx := liveContext(t)

	if _, err := inj.CreateSession(ctx); err == nil {
		t.Error("CreateSession with a bad bearer must fail")
	} else if !strings.Contains(err.Error(), "401") {
		t.Errorf("expected a 401 in the error, got: %v", err)
	}

	if _, err := inj.Inject(ctx, "k8s-evt-nonexistent", testPayload("Evicted")); err == nil {
		t.Error("Inject with a bad bearer must fail")
	} else if !strings.Contains(err.Error(), "401") {
		t.Errorf("expected a 401 in the error, got: %v", err)
	}
}
