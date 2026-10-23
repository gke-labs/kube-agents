package main

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func file(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "f")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestConfigFromEnv(t *testing.T) {
	token, pass := file(t, "tok\n"), file(t, "pw\n")
	s, err := configFromEnv(env(map[string]string{
		envBusURL:       "tls://platform-agent-a2a-nats.kubeagents-system.svc:4222",
		envTokenFile:    token,
		envPasswordFile: pass,
		envAgent:        "platform-agent",
		envNamespace:    "kubeagents-system",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if s.listen != defaultListen || s.tokenFile != token {
		t.Errorf("listen %q token %q", s.listen, s.tokenFile)
	}
	if s.bus.User != defaultBusUser || s.bus.Password != "pw" {
		t.Errorf("bus user %q password %q, want the console user and the trimmed file", s.bus.User, s.bus.Password)
	}
	if s.bus.Deployment.Agent != "platform-agent" || s.bus.Deployment.Namespace != "kubeagents-system" {
		t.Errorf("deployment = %+v", s.bus.Deployment)
	}
}

// Each of these is a pod that must not start: there is no default bus, no
// running without a token, and no running without the bus credential.
func TestConfigFromEnvRefusesToStartWithoutWhatItNeeds(t *testing.T) {
	token, pass := file(t, "tok"), file(t, "pw")
	base := map[string]string{envBusURL: "tls://bus:4222", envTokenFile: token, envPasswordFile: pass}
	for name, override := range map[string]map[string]string{
		"no bus URL":          {envBusURL: ""},
		"a plaintext bus URL": {envBusURL: "nats://bus:4222"},
		"a missing token":     {envTokenFile: filepath.Join(t.TempDir(), "absent")},
		"an empty token":      {envTokenFile: file(t, " \n")},
		"a missing password":  {envPasswordFile: filepath.Join(t.TempDir(), "absent")},
		"an empty password":   {envPasswordFile: file(t, "\n")},
	} {
		t.Run(name, func(t *testing.T) {
			m := map[string]string{}
			for k, v := range base {
				m[k] = v
			}
			for k, v := range override {
				m[k] = v
			}
			if _, err := configFromEnv(env(m)); err == nil {
				t.Errorf("started with %s", name)
			}
		})
	}
}

// The first connect is retried until it succeeds, not given up on: on a fresh
// install the bus comes up after this pod.
func TestTheFirstDialIsRetried(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	calls := 0
	got, err := dialWithRetry(context.Background(), log, time.Millisecond, func(context.Context) (string, error) {
		if calls++; calls < 3 {
			return "", errors.New("connection refused")
		}
		return "bus", nil
	})
	if err != nil || got != "bus" || calls != 3 {
		t.Fatalf("got %q, %v after %d calls; want the bus on the third", got, err, calls)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, err := dialWithRetry(ctx, log, time.Millisecond, func(context.Context) (string, error) {
		return "", errors.New("connection refused")
	}); !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("a dial that never succeeds ended with %v, want the context's end", err)
	}
}

// A dial the bus refused for good (the credential, or a certificate that does
// not verify) is not retried as "not reachable yet": it fails at once.
func TestARefusedDialIsNotRetried(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	for name, refusal := range map[string]error{
		"authorization": fmt.Errorf("connect: %w", nats.ErrAuthorization),
		"unknown CA":    fmt.Errorf("connect: %w", x509.UnknownAuthorityError{}),
		"wrong name":    fmt.Errorf("connect: %w", x509.HostnameError{Host: "127.0.0.1"}),
	} {
		calls := 0
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		_, err := dialWithRetry(ctx, log, time.Millisecond, func(context.Context) (string, error) {
			calls++
			return "", refusal
		})
		cancel()
		if calls != 1 || err == nil || errors.Is(err, context.DeadlineExceeded) {
			t.Errorf("%s: %d calls, err %v; want one call and the refusal", name, calls, err)
		}
	}
}

// Stopped while still waiting for an unreachable bus, the server exits
// cleanly: a rollout or drain during an outage is not a failed container.
func TestAStopDuringTheFirstDialIsClean(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	ctx, cancel := context.WithTimeout(context.Background(), 300*time.Millisecond)
	defer cancel()
	err := realMain(ctx, log, env(map[string]string{
		envListen:       "127.0.0.1:0",
		envTokenFile:    file(t, "a-token"),
		envBusURL:       "tls://127.0.0.1:1",
		envPasswordFile: file(t, "pw"),
	}))
	if err != nil {
		t.Errorf("a stop during the first dial returned %v, want a clean stop", err)
	}
}

// A stop ends the requests in flight rather than waiting them out: a handler
// that runs until its request's context is done is ended by the stop, and
// serve returns cleanly well inside shutdownGrace.
func TestAStopEndsTheRequestsInFlight(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	ln, err := net.Listen(networkTCP, "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	entered := make(chan struct{})
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(entered)
		<-r.Context().Done()
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() { served <- serve(ctx, log, ln, h) }()
	go func() {
		if resp, err := http.Get("http://" + ln.Addr().String() + "/"); err == nil {
			resp.Body.Close()
		}
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("the request never reached the handler")
	}
	start := time.Now()
	cancel()
	select {
	case err := <-served:
		if err != nil {
			t.Errorf("serve returned %v on a stop, want a clean one", err)
		}
		if elapsed := time.Since(start); elapsed > shutdownGrace/4 {
			t.Errorf("the stop took %s with a request in flight", elapsed)
		}
	case <-time.After(shutdownGrace + 5*time.Second):
		t.Fatal("serve did not return after the stop")
	}
}
