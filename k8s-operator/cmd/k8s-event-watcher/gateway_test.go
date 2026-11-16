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
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// realMainStopAfter bounds a test that runs realMain until a log line.
const realMainStopAfter = 5 * time.Second

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

// writeKubeconfig writes a kubeconfig that parses and points at server.
func writeKubeconfig(t *testing.T, server string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "kubeconfig")
	cfg := "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n    server: " + server +
		"\ncontexts:\n- name: c\n  context:\n    cluster: c\n    user: u\ncurrent-context: c\nusers:\n- name: u\n  user:\n    token: t\n"
	if err := os.WriteFile(path, []byte(cfg), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// waitForLog polls logs until line appears or realMainStopAfter passes.
func waitForLog(logs *lockedBuffer, line string) {
	deadline := time.Now().Add(realMainStopAfter)
	for time.Now().Before(deadline) && !strings.Contains(logs.String(), line) {
		time.Sleep(10 * time.Millisecond)
	}
}

// sigtermWhenLogged stops a running realMain, which exits on SIGTERM, once
// line is logged or realMainStopAfter passes. The test registers for SIGTERM
// too, and keeps that registration until the signal has been sent, so the
// signal cannot kill the test binary after realMain has returned.
func sigtermWhenLogged(t *testing.T, logs *lockedBuffer, line string) {
	t.Helper()
	sigs := make(chan os.Signal, 1)
	signal.Notify(sigs, syscall.SIGTERM)
	sent := make(chan struct{})
	go func() {
		defer close(sent)
		waitForLog(logs, line)
		_ = syscall.Kill(os.Getpid(), syscall.SIGTERM)
	}()
	t.Cleanup(func() {
		<-sent
		signal.Stop(sigs)
	})
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
	logs := captureLog(t)
	url, _ := refusingURL(t)
	start := time.Now()
	if waitForGateway(context.Background(), url, 50*time.Millisecond, 10*time.Millisecond) {
		t.Fatal("waitForGateway = true with nothing listening; want false")
	}
	if elapsed := time.Since(start); elapsed > 2*time.Second {
		t.Errorf("waitForGateway took %s past a 50ms limit", elapsed)
	}
	// The ALERT carries the probe's error, so the reason the gateway did not
	// answer is in the log rather than left to the reader.
	if got := logs.String(); !strings.Contains(got, "last probe:") || !strings.Contains(got, "connection refused") {
		t.Errorf("ALERT line does not carry the probe error:\n%s", got)
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

	for _, bad := range []string{"localhost:8642", "127.0.0.1:8642", "ftp://127.0.0.1:8642", "http:8642"} {
		f = base()
		f.gatewayURL = bad
		if err := f.validate(); err == nil || !strings.Contains(err.Error(), "--gateway-url") {
			t.Errorf("--gateway-url=%q: got %v; want a --gateway-url error", bad, err)
		}
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
	if f.gatewayWait != 35*time.Minute {
		t.Errorf("--gateway-wait default = %s; want 35m", f.gatewayWait)
	}
}

// realMain must start no informer until the gateway wait ends. The informers
// are started per cluster after the wait, and each logs a "starting" line, so
// the time between the wait's ALERT and that line is the hold.
func TestRealMain_HoldsTheInformersUntilTheGatewayWaitEnds(t *testing.T) {
	logs := captureLog(t)
	t.Setenv("WATCHER_TEST_TOKEN", "token")
	url, _ := refusingURL(t)
	_, metricsAddr := refusingURL(t)
	const wait = time.Second

	sigtermWhenLogged(t, logs, "starting: preflight")
	scraped := make(chan string, 1)
	go func() {
		waitForLog(logs, "waiting up to")
		body := ""
		if resp, err := http.Get("http://" + metricsAddr + "/metrics"); err == nil {
			b, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			body = string(b)
		}
		scraped <- body
	}()

	err := realMain([]string{
		"--daemon-url", "http://127.0.0.1:1", "--token-env", "WATCHER_TEST_TOKEN", "--owner", "watcher",
		"--kubeconfig", writeKubeconfig(t, "http://127.0.0.1:1"), "--cluster-name", "x",
		"--gateway-url", url, "--gateway-wait", wait.String(), "--metrics-addr", metricsAddr,
	})
	if err != nil {
		t.Fatalf("realMain: %v", err)
	}
	// Registered at 0 during the wait: an absent series would read as no
	// watcher at all to an absent() alert and to status.usage.
	if body := <-scraped; !strings.Contains(body, `k8s_event_watcher_cluster_up{cluster="x",location="",project=""} 0`) {
		t.Errorf("cluster_up is not published at 0 during the gateway wait; /metrics:\n%s", body)
	}
	got := logs.String()
	alert := strings.Index(got, "did not answer within")
	start := strings.Index(got, "starting: preflight")
	if alert < 0 || start < 0 {
		t.Fatalf("log lacks the wait's ALERT line or the informer start:\n%s", got)
	}
	if start < alert {
		t.Errorf("an informer started before the gateway wait ended:\n%s", got)
	}
}

// A watch set that cannot be built must still exit at once, so the
// entrypoint's supervisor counts it as a failed start and raises its ALERT.
func TestRealMain_AnUnbuildableWatchSetDoesNotWaitForTheGateway(t *testing.T) {
	logs := captureLog(t)
	t.Setenv("WATCHER_TEST_TOKEN", "token")
	badKubeconfig := filepath.Join(t.TempDir(), "kubeconfig")
	if err := os.WriteFile(badKubeconfig, []byte("not: [a kubeconfig"), 0o600); err != nil {
		t.Fatal(err)
	}
	url, _ := refusingURL(t)
	const wait = 2 * time.Second

	start := time.Now()
	err := realMain([]string{
		"--daemon-url", "http://127.0.0.1:1", "--token-env", "WATCHER_TEST_TOKEN", "--owner", "watcher",
		"--kubeconfig", badKubeconfig, "--cluster-name", "x",
		"--gateway-url", url, "--gateway-wait", wait.String(),
	})
	if err == nil || !strings.Contains(err.Error(), "kubeconfig") {
		t.Fatalf("want realMain to stop on the kubeconfig, got err=%v", err)
	}
	if strings.Contains(logs.String(), "waiting up to") || time.Since(start) >= wait {
		t.Errorf("realMain waited for the gateway before reporting an unbuildable watch set:\n%s", logs.String())
	}
}

// --dry-run makes no daemon call, so there is no triage turn to wait for.
func TestRealMain_DryRunDoesNotWaitForTheGateway(t *testing.T) {
	logs := captureLog(t)
	url, _ := refusingURL(t)
	sigtermWhenLogged(t, logs, "starting: preflight")

	start := time.Now()
	if err := realMain([]string{
		"--dry-run", "--kubeconfig", writeKubeconfig(t, "http://127.0.0.1:1"), "--cluster-name", "x",
		"--gateway-url", url, "--gateway-wait", time.Hour.String(),
	}); err != nil {
		t.Fatalf("realMain: %v", err)
	}
	if strings.Contains(logs.String(), "waiting up to") {
		t.Errorf("--dry-run waited for the gateway:\n%s", logs.String())
	}
	if elapsed := time.Since(start); elapsed > 4*time.Second {
		t.Errorf("--dry-run took %s to start its informer", elapsed)
	}
}
