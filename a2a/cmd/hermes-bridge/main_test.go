package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
)

// A missing NATS_URL is the one failure the bridge reports with a usage exit,
// and it is decided before anything is dialed, so a test can reach it.
func TestRealMainMissingNATSURLIsUsage(t *testing.T) {
	t.Setenv("NATS_URL", "")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	err := realMain(context.Background(), log)
	if !errors.Is(err, errUsage) {
		t.Fatalf("realMain with empty NATS_URL returned %v, want errUsage", err)
	}
}

func TestRunMapsUsageErrorToExitUsage(t *testing.T) {
	t.Setenv("NATS_URL", "")
	if got := run(); got != exitUsage {
		t.Errorf("run() = %d, want %d", got, exitUsage)
	}
}

func TestEnvOr(t *testing.T) {
	const key = "HERMES_BRIDGE_TEST_ENVOR"
	t.Setenv(key, "")
	if got := envOr(key, "fallback"); got != "fallback" {
		t.Errorf("envOr on empty = %q, want %q", got, "fallback")
	}
	t.Setenv(key, "set")
	if got := envOr(key, "fallback"); got != "set" {
		t.Errorf("envOr on set = %q, want %q", got, "set")
	}
}

func TestEnvInt(t *testing.T) {
	const key = "HERMES_BRIDGE_TEST_ENVINT"
	quiet := slog.New(slog.NewJSONHandler(io.Discard, nil))

	t.Setenv(key, "")
	if got := envInt(quiet, key, 7); got != 7 {
		t.Errorf("envInt on empty = %d, want 7", got)
	}
	t.Setenv(key, "42")
	if got := envInt(quiet, key, 7); got != 42 {
		t.Errorf("envInt on 42 = %d, want 42", got)
	}

	// A non-integer falls back to the default and says so once, naming the
	// key and the value it refused, so a typo in a manifest is visible.
	var buf bytes.Buffer
	loud := slog.New(slog.NewJSONHandler(&buf, nil))
	t.Setenv(key, "ten")
	if got := envInt(loud, key, 7); got != 7 {
		t.Errorf("envInt on non-integer = %d, want the default 7", got)
	}
	out := buf.String()
	if n := strings.Count(out, "\n"); n != 1 {
		t.Errorf("envInt on non-integer logged %d records, want 1:\n%s", n, out)
	}
	for _, want := range []string{`"level":"ERROR"`, `"key":"` + key + `"`, `"value":"ten"`, `"default":7`} {
		if !strings.Contains(out, want) {
			t.Errorf("envInt log record lacks %s:\n%s", want, out)
		}
	}
}
