package main

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// unreachableNATSURL is a loopback port nothing listens on. lib.Connect
// dials once with no retry-on-failed-connect, so a refused port fails the
// dial immediately rather than waiting out a timeout.
const unreachableNATSURL = "nats://127.0.0.1:1"

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

// Every failure after the environment is read is the failure exit, not the
// usage exit and not a clean zero: a bridge that cannot reach the bus must
// show as Error in the pod, not Completed. The dial is the first such
// failure a test can reach without a bus.
func TestRunMapsDialFailureToExitFailure(t *testing.T) {
	t.Setenv("NATS_URL", unreachableNATSURL)
	t.Setenv("NATS_USER", "")
	t.Setenv(apiServerKeyEnv, "loopback-key")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	err := realMain(context.Background(), log)
	if !errors.Is(err, nats.ErrNoServers) {
		t.Fatalf("realMain against %s returned %v, want a wrapped nats.ErrNoServers", unreachableNATSURL, err)
	}
	if errors.Is(err, errUsage) {
		t.Fatalf("realMain dial failure %v satisfies errUsage; it must not", err)
	}
	if got := run(); got != exitFailure {
		t.Errorf("run() = %d, want %d", got, exitFailure)
	}
}

// The API executor needs the pod's key before the server honours the session
// headers. Named explicitly with no key, it fails at start, before the bus is
// dialled, rather than once per task. Left unset with no key, the daemon runs
// the subprocess executor instead, so a sidecar declared before the API
// executor existed keeps working on an image bump.
func TestRunAPIExecutorWithoutKey(t *testing.T) {
	t.Setenv("NATS_URL", unreachableNATSURL)
	t.Setenv("NATS_USER", "")
	t.Setenv(apiServerKeyEnv, "")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	t.Setenv(executorEnv, "api")
	err := realMain(context.Background(), log)
	if err == nil || errors.Is(err, nats.ErrNoServers) || !strings.Contains(err.Error(), "APIKey") {
		t.Fatalf("realMain with %s=api and no %s returned %v, want the missing-key refusal before any dial", executorEnv, apiServerKeyEnv, err)
	}
	for _, executor := range []string{"cli", ""} {
		t.Setenv(executorEnv, executor)
		if err := realMain(context.Background(), log); !errors.Is(err, nats.ErrNoServers) {
			t.Fatalf("realMain with %s=%q and no key returned %v, want the dial failure", executorEnv, executor, err)
		}
	}
}

func TestBridgeExecutorDefault(t *testing.T) {
	var logs bytes.Buffer
	log := slog.New(slog.NewJSONHandler(&logs, nil))
	for _, tc := range []struct{ executor, key, want string }{
		{"", "loopback-key", "api"},
		{"", "", "cli"},
		{"", "  ", "cli"},
		{"cli", "loopback-key", "cli"},
		{"api", "", "api"},
	} {
		t.Setenv(executorEnv, tc.executor)
		t.Setenv(apiServerKeyEnv, tc.key)
		logs.Reset()
		if got := bridgeExecutor(log); got != tc.want {
			t.Errorf("bridgeExecutor(%s=%q, key=%q) = %q, want %q", executorEnv, tc.executor, tc.key, got, tc.want)
		}
		if warned := strings.Contains(logs.String(), `"level":"WARN"`); warned != (tc.executor == "" && tc.want == "cli") {
			t.Errorf("bridgeExecutor(%s=%q, key=%q) warned=%v; want a warning only on the keyless fallback", executorEnv, tc.executor, tc.key, warned)
		}
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

// The environment's "off" is the Config's empty string; anything else is an
// address, passed through for net.Listen to judge.
func TestActivityListenMapsOffToClosed(t *testing.T) {
	if got := activityListen(activityListenOff); got != "" {
		t.Errorf("activityListen(off) = %q, want empty", got)
	}
	if got := activityListen("127.0.0.1:9"); got != "127.0.0.1:9" {
		t.Errorf("activityListen(addr) = %q, want the address back", got)
	}
}

// In the environment 0 seconds is the heartbeat off; the Config's own zero is
// its default, so the daemon has to say off in the Config's word (negative).
func TestProgressIntervalMapsZeroToOff(t *testing.T) {
	quiet := slog.New(slog.NewJSONHandler(io.Discard, nil))
	if got := progressInterval(quiet, 0); got >= 0 {
		t.Errorf("progressInterval(0) = %v, want negative (off)", got)
	}
	if got := progressInterval(quiet, 30); got != 30*time.Second {
		t.Errorf("progressInterval(30) = %v, want 30s", got)
	}
}

// A count of seconds the duration cannot hold would wrap negative, which the
// Config reads as off; it is refused like a non-integer instead, loudly and
// with the default in its place. The largest count that fits is accepted
// quietly.
func TestProgressIntervalRefusesAnOverRangeValue(t *testing.T) {
	over := maxDurationSeconds + 1
	if int64(int(over)) != over {
		t.Skip("int cannot hold an over-range second count on this platform")
	}
	var buf bytes.Buffer
	loud := slog.New(slog.NewJSONHandler(&buf, nil))
	if got := progressInterval(loud, int(over)); got != time.Duration(defaultProgressIntervalSeconds)*time.Second {
		t.Errorf("progressInterval(over-range) = %v, want the default %ds", got, defaultProgressIntervalSeconds)
	}
	out := buf.String()
	if n := strings.Count(out, "\n"); n != 1 {
		t.Errorf("progressInterval on over-range logged %d records, want 1:\n%s", n, out)
	}
	for _, want := range []string{`"level":"ERROR"`, `"key":"BRIDGE_PROGRESS_INTERVAL_SECONDS"`, `"value":` + strconv.FormatInt(over, 10), `"default":` + strconv.Itoa(defaultProgressIntervalSeconds)} {
		if !strings.Contains(out, want) {
			t.Errorf("progressInterval log record lacks %s:\n%s", want, out)
		}
	}

	buf.Reset()
	if got := progressInterval(loud, int(maxDurationSeconds)); got != time.Duration(maxDurationSeconds)*time.Second || got <= 0 {
		t.Errorf("progressInterval(largest) = %v, want it accepted", got)
	}
	if buf.Len() != 0 {
		t.Errorf("the largest count that fits was logged about:\n%s", buf.String())
	}
}
