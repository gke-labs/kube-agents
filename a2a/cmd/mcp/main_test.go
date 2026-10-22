package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"
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
