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
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// refusingURL returns a loopback URL nothing listens on, and the listener's
// address so a test can bind it later.
func refusingURL(t *testing.T) (string, string) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	ln.Close()
	return "http://" + addr, addr
}

// The Hermes API answers 401 to a caller without its key. That still proves
// the gateway is listening, which is all the wait asks.
func TestWaitForGateway_AnyHTTPAnswerIsReady(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.RequestURI()
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	if !waitForGateway(context.Background(), srv.URL, time.Second, 10*time.Millisecond) {
		t.Fatal("waitForGateway = false for a gateway answering 401; want true")
	}
	if gotPath != gatewayProbePath {
		t.Errorf("probe path = %q; want %q", gotPath, gatewayProbePath)
	}
}

func TestWaitForGateway_WaitsUntilTheGatewayBinds(t *testing.T) {
	url, addr := refusingURL(t)
	go func() {
		time.Sleep(100 * time.Millisecond)
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			return
		}
		_ = http.Serve(ln, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	}()

	if !waitForGateway(context.Background(), url, 5*time.Second, 10*time.Millisecond) {
		t.Fatal("waitForGateway = false for a gateway that binds after 100ms; want true")
	}
}

func TestWaitForGateway_GivesUpAtTheLimit(t *testing.T) {
	url, _ := refusingURL(t)
	start := time.Now()
	if waitForGateway(context.Background(), url, 50*time.Millisecond, 10*time.Millisecond) {
		t.Fatal("waitForGateway = true with nothing listening; want false")
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("waitForGateway took %s past a 50ms limit", elapsed)
	}
}

func TestWaitForGateway_StopsOnShutdown(t *testing.T) {
	url, _ := refusingURL(t)
	ctx, cancel := context.WithCancel(context.Background())
	time.AfterFunc(50*time.Millisecond, cancel)
	start := time.Now()
	if waitForGateway(ctx, url, time.Hour, 10*time.Millisecond) {
		t.Fatal("waitForGateway = true after shutdown; want false")
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("waitForGateway took %s to stop after its context ended", elapsed)
	}
}

func TestValidate_GatewayFlags(t *testing.T) {
	base := func() flags {
		return flags{
			daemonURL:   "http://localhost:8699",
			tokenEnv:    "TOKEN",
			mode:        "per-incident",
			owner:       "watcher",
			dedupWindow: 1,
			clusterName: "c",
			gatewayURL:  "http://127.0.0.1:8642",
			gatewayWait: time.Minute,
		}
	}

	if f := base(); f.validate() != nil {
		t.Fatalf("unexpected error: %v", f.validate())
	}

	f := base()
	f.gatewayWait = 0
	if err := f.validate(); err == nil || !strings.Contains(err.Error(), "--gateway-wait") {
		t.Errorf("zero --gateway-wait: got %v; want a --gateway-wait error", err)
	}

	f = base()
	f.gatewayURL += "/"
	if err := f.validate(); err == nil || !strings.Contains(err.Error(), "--gateway-url") {
		t.Errorf("trailing slash: got %v; want a --gateway-url error", err)
	}
}

func TestParseFlags_GatewayDefaults(t *testing.T) {
	f, err := parseFlags(nil)
	if err != nil {
		t.Fatalf("parseFlags: %v", err)
	}
	if f.gatewayURL != "" {
		t.Errorf("--gateway-url default = %q; want empty (no wait)", f.gatewayURL)
	}
	if f.gatewayWait != 15*time.Minute {
		t.Errorf("--gateway-wait default = %s; want 15m", f.gatewayWait)
	}
}
