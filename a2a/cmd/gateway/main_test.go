package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"

	"github.com/nats-io/nats.go"
)

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
			// The refusal names what is armed, so an operator reading it
			// knows which variable to unset.
			want: "more than one backend is configured",
		},
		{
			name: "inject and a chat backend together",
			env: map[string]string{
				"NATS_URL":          "nats://127.0.0.1:1",
				"DISCORD_TOKEN":     "tok",
				"A2A_INJECT_LISTEN": ":8099",
			},
			want: "A2A_INJECT_LISTEN",
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
			t.Setenv("A2A_INJECT_LISTEN", "")
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
