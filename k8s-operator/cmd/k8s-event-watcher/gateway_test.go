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
	"os"
	"path/filepath"
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
	if f.gatewayWait != 20*time.Minute {
		t.Errorf("--gateway-wait default = %s; want 20m", f.gatewayWait)
	}
}

// realMain must not build its watch set, and so start no informer, until the
// gateway wait ends. A kubeconfig that cannot be parsed fails buildWatchSet,
// so the time that error takes to arrive is the time the wait held it.
func TestRealMain_HoldsTheWatchSetUntilTheGatewayWaitEnds(t *testing.T) {
	logs := captureLog(t)
	t.Setenv("WATCHER_TEST_TOKEN", "token")
	badKubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(badKubeconfig, []byte("not: [a kubeconfig"), 0o600); err != nil {
		t.Fatal(err)
	}
	url, _ := refusingURL(t)
	const wait = 300 * time.Millisecond

	start := time.Now()
	err := realMain([]string{
		"--daemon-url", "http://127.0.0.1:1", "--token-env", "WATCHER_TEST_TOKEN", "--owner", "watcher",
		"--kubeconfig", badKubeconfig, "--cluster-name", "x",
		"--gateway-url", url, "--gateway-wait", wait.String(),
	})
	if err == nil || !strings.Contains(err.Error(), "kubeconfig") {
		t.Fatalf("want realMain to stop on the kubeconfig after the wait, got err=%v", err)
	}
	if elapsed := time.Since(start); elapsed < wait {
		t.Errorf("buildWatchSet ran after %s, before the %s gateway wait ended", elapsed, wait)
	}
	if got := logs.String(); !strings.Contains(got, "did not answer within") {
		t.Errorf("log does not carry the gateway wait's ALERT line:\n%s", got)
	}
}

// The entrypoint is what turns the wait on in a deployed pod; without the
// flag the watcher keeps the old behaviour and the boot window is open again.
func TestStartServicesPassesTheGatewayURL(t *testing.T) {
	script, err := os.ReadFile(filepath.Join("..", "..", "..", "deploy", "shared", "start-services.sh"))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(script), `--gateway-url="${AGENT_GATEWAY_URL}"`) {
		t.Error(`start-services.sh does not pass --gateway-url="${AGENT_GATEWAY_URL}" to k8s-event-watcher`)
	}
	if !strings.Contains(string(script), `readonly AGENT_GATEWAY_URL="http://127.0.0.1:8642"`) {
		t.Error("start-services.sh does not set AGENT_GATEWAY_URL to the gateway's loopback address")
	}
}
