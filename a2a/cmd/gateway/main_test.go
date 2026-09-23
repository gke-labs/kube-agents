package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/gateway"
)

// startTestServerReadyTimeout bounds how long a test waits for the embedded
// nats-server to accept connections before failing loudly instead of hanging.
const startTestServerReadyTimeout = 10 * time.Second

// unreachableNATSURL is a loopback port nothing listens on. lib.Connect
// dials once with no retry-on-failed-connect, so a refused port fails the
// dial immediately rather than waiting out a timeout.
const unreachableNATSURL = "nats://127.0.0.1:1"

// realMain's first call is gateway.FromEnv, and every case below is refused
// there, so none of them dials NATS. Each case pins A2A_CHAT_DISPLAY_MODE to
// empty because FromEnv validates it before NATS_URL: a CI environment with
// a stray value there would otherwise change which error fires.
func TestRealMainRefusesBadConfigBeforeDialing(t *testing.T) {
	cases := []struct {
		name string
		env  map[string]string
		want string
	}{
		{
			name: "NATS_URL empty",
			env:  map[string]string{"NATS_URL": "", "DISCORD_TOKEN": "tok"},
			want: "NATS_URL",
		},
		{
			name: "both backends set",
			env: map[string]string{
				"NATS_URL":            "nats://127.0.0.1:1",
				"DISCORD_TOKEN":       "tok",
				"A2A_GCHAT_RELAY_URL": "http://relay",
			},
			want: "both DISCORD_TOKEN and A2A_GCHAT_RELAY_URL are set",
		},
		{
			name: "no backend set",
			env: map[string]string{
				"NATS_URL":            "nats://127.0.0.1:1",
				"DISCORD_TOKEN":       "",
				"A2A_GCHAT_RELAY_URL": "",
			},
			want: "no chat backend",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
			t.Setenv("NATS_URL", "")
			t.Setenv("DISCORD_TOKEN", "")
			t.Setenv("A2A_GCHAT_RELAY_URL", "")
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			log := slog.New(slog.NewJSONHandler(io.Discard, nil))
			err := realMain(context.Background(), log)
			if err == nil {
				t.Fatalf("realMain returned nil, want an error naming %q", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("realMain error %q, want it to name %q", err, tc.want)
			}
		})
	}
}

// The dial is the first thing after FromEnv that can fail, and its error
// has to come back out of realMain as the failure exit rather than a clean
// zero. SESSION_KV_SALT is set because FromEnv refuses an empty
// NATS_PASSWORD without a salt, which would stop the case at config.
func TestRealMainReturnsDialFailure(t *testing.T) {
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
	t.Setenv("NATS_URL", unreachableNATSURL)
	t.Setenv("NATS_USER", "")
	t.Setenv("NATS_PASSWORD", "")
	t.Setenv("SESSION_KV_SALT", "test-salt")
	t.Setenv("DISCORD_TOKEN", "tok")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	err := realMain(context.Background(), log)
	if !errors.Is(err, nats.ErrNoServers) {
		t.Fatalf("realMain against %s returned %v, want a wrapped nats.ErrNoServers", unreachableNATSURL, err)
	}
	if got := run(); got != exitFailure {
		t.Errorf("run() = %d, want %d", got, exitFailure)
	}
}

// A config error is the one path a test can reach without a bus, and run
// must report it as the failure exit rather than a clean zero.
func TestRunExitsNonZeroOnConfigError(t *testing.T) {
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
	t.Setenv("NATS_URL", "")
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	if got := run(); got != exitFailure {
		t.Errorf("run() = %d, want %d", got, exitFailure)
	}
}

// startTestServer starts an embedded, no-auth nats-server on a random port
// for buildAdapters tests that need a real bus but no gateway config.
func startTestServer(t *testing.T) *natsserver.Server {
	t.Helper()
	opts := &natsserver.Options{
		Host:     "127.0.0.1",
		Port:     -1,
		NoLog:    true,
		NoSigs:   true,
		StoreDir: t.TempDir(),
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(startTestServerReadyTimeout) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	return s
}

// fakePrimary is a gateway.Adapter stand-in for buildAdapters tests: it
// proves the primary backend is still reachable through the mux without
// standing up a real Discord or Google Chat adapter.
type fakePrimary struct{}

func newFakePrimary() *fakePrimary { return &fakePrimary{} }

func (f *fakePrimary) Run(ctx context.Context, handler func(gateway.InboundMessage)) error {
	<-ctx.Done()
	return ctx.Err()
}

func (f *fakePrimary) Post(conversation, text string) (string, error) {
	return "1", nil
}

func (f *fakePrimary) Edit(conversation, messageID, text string) error {
	return nil
}

func (f *fakePrimary) Roster(conversation string) ([]string, bool, error) {
	return nil, true, nil
}

func (f *fakePrimary) OpenDirect(userID string) (string, error) {
	return "", nil
}

// TestBuildAdaptersIncludesTheConsole proves buildAdapters wires both the
// configured chat backend and the console adapter behind one mux: a post to
// either backend's prefix must reach it (spec-chatops-gateway.md, "The
// console adapter").
func TestBuildAdaptersIncludesTheConsole(t *testing.T) {
	s := startTestServer(t)
	cfg := &gateway.Config{NATSURL: s.ClientURL(), DiscordToken: "x"}
	primary := newFakePrimary()
	m, err := buildAdapters(cfg, primary, nil, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := m.Post("console:tab-1", "hi"); err != nil {
		t.Errorf("console not wired: %v", err)
	}
	if _, err := m.Post("discord:g/c", "hi"); err != nil {
		t.Errorf("primary not wired: %v", err)
	}
}
