package main

import (
	"bytes"
	"log/slog"
	"os"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The bus credential must not reach the harness. The worker NATS user is
// shared across every session pod and its grants cover the whole task plane,
// while the harness is a model-directed subprocess with a file-reading tool
// and /proc/self/environ readable at its own UID. Assert the refusal — that
// the values are absent — rather than that the filter exists.
func TestHarnessEnvWithholdsTheBusCredential(t *testing.T) {
	t.Setenv("NATS_PASSWORD", "s3cret-worker-password")
	t.Setenv("NATS_USER", "worker")
	t.Setenv("NATS_URL", "nats://platform-agent-a2a-nats.kubeagents-system.svc:4222")
	t.Setenv("TASK_ID", "task-abc")

	env := harnessEnv()

	for _, kv := range env {
		key, value, _ := strings.Cut(kv, "=")
		for _, withheld := range busCredentialEnv {
			if key == withheld {
				t.Errorf("%s reached the harness environment", key)
			}
		}
		if strings.Contains(value, "s3cret-worker-password") {
			t.Errorf("the bus password reached the harness as %s", key)
		}
	}

	// The filter is not a blanket drop: everything else the pod was given
	// still has to arrive, or the harness loses its task identity.
	var sawTask bool
	for _, kv := range env {
		if kv == "TASK_ID=task-abc" {
			sawTask = true
		}
	}
	if !sawTask {
		t.Error("TASK_ID did not survive the filter")
	}
}

// With no model auth configured the harness is pointed at the install's
// LiteLLM, which is the one destination the session fence permits besides the
// bus and DNS.
func TestHarnessEnvDefaultsToTheInstallLiteLLM(t *testing.T) {
	for _, key := range []string{"ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY"} {
		t.Setenv(key, "")
		_ = os.Unsetenv(key)
	}

	var base string
	for _, kv := range harnessEnv() {
		if key, value, _ := strings.Cut(kv, "="); key == "ANTHROPIC_BASE_URL" {
			base = value
		}
	}
	if base != "http://litellm" {
		t.Errorf("ANTHROPIC_BASE_URL = %q, want the in-namespace LiteLLM", base)
	}
}

// The default tool surface and the session fence have to agree: a tool that
// needs egress the policy denies does not fail, it hangs until the connect
// timeout, spending the task deadline on a black hole.
func TestDefaultToolSurfaceNeedsNoEgressTheFenceDenies(t *testing.T) {
	t.Setenv("A2A_ALLOWED_TOOLS", "")
	_ = os.Unsetenv("A2A_ALLOWED_TOOLS")
	t.Setenv("A2A_HARNESS_CMD", "")
	_ = os.Unsetenv("A2A_HARNESS_CMD")

	argv := harnessCommand()
	var allowed string
	for i, arg := range argv {
		if arg == "--allowedTools" && i+1 < len(argv) {
			allowed = argv[i+1]
		}
	}
	if allowed == "" {
		t.Fatalf("no --allowedTools in argv: %v", argv)
	}
	for _, networked := range []string{"WebFetch", "WebSearch", "Bash"} {
		if strings.Contains(allowed, networked) {
			t.Errorf("%s is in the default tool surface; the session fence permits only DNS, the bus and LiteLLM", networked)
		}
	}
}

// originSeq is the join between the two halves the origin-sequence fix already
// pins: the spawner renders lib.EnvOriginSeq (spawn_test.go) and the adapter
// honours Config.OriginSeq / Config.OriginSeqStated (adapter_origin_cap_test.go,
// session_adapter_test.go), but both of those set the Config fields directly.
// Nothing read the pod env into them. A regression that returned (0, true) for a
// valid sequence, or (n, false), would leave both suites green while every
// gateway-spawned worker fell back to the scan this fix exists to stop — the
// scan that hands back the oldest surviving steer and executes it as the
// request.
//
// The two return values answer different questions and the test keeps them
// apart: the uint64 is the sequence, the bool is only "did the spawner say
// anything at all". Everything except an unset variable is a spawner that spoke,
// including the ones that spoke uselessly.
func TestOriginSeqReadsThePodEnv(t *testing.T) {
	for _, tc := range []struct {
		name       string
		raw        string // "" with unset=true means the variable is absent
		unset      bool
		wantSeq    uint64
		wantStated bool
		wantWarn   bool
	}{
		{
			// The by-hand and dispatcher-spawned shapes, and any pod from a
			// spawner older than the variable. Not stated, so the adapter
			// falls back to the scan.
			name:       "absent is nobody told me",
			unset:      true,
			wantSeq:    0,
			wantStated: false,
		},
		{
			// A current spawner whose own publish returned no usable
			// sequence. Stated, so the adapter can tell it from an old
			// spawner, and no warning: this is the sentinel working.
			name:       "the unknown sentinel is stated, not a fault",
			raw:        lib.OriginSeqUnknown,
			wantSeq:    0,
			wantStated: true,
		},
		{
			name:       "a real sequence",
			raw:        "42",
			wantSeq:    42,
			wantStated: true,
		},
		{
			// uint64's ceiling, to prove the parse is not an int.
			name:       "the largest sequence uint64 holds",
			raw:        "18446744073709551615",
			wantSeq:    18446744073709551615,
			wantStated: true,
		},
		{
			// Unparseable is the sentinel rather than a boot failure: the
			// worst it costs is the scan an older spawner already gets,
			// whereas refusing to start turns a typo into a dead session.
			name:       "junk warns and degrades to the sentinel",
			raw:        "not-a-number",
			wantSeq:    0,
			wantStated: true,
			wantWarn:   true,
		},
		{
			// JetStream sequences start at 1, so a zero is a spawner that
			// read an empty PubAck, not a message at the head.
			name:       "zero warns and degrades to the sentinel",
			raw:        "0",
			wantSeq:    0,
			wantStated: true,
			wantWarn:   true,
		},
		{
			// ParseUint rejects the sign rather than wrapping it.
			name:       "negative warns and degrades to the sentinel",
			raw:        "-1",
			wantSeq:    0,
			wantStated: true,
			wantWarn:   true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(lib.EnvOriginSeq, tc.raw)
			if tc.unset {
				_ = os.Unsetenv(lib.EnvOriginSeq)
			}

			var logged bytes.Buffer
			log := slog.New(slog.NewJSONHandler(&logged, &slog.HandlerOptions{Level: slog.LevelWarn}))

			seq, stated := originSeq(log)

			if seq != tc.wantSeq {
				t.Errorf("sequence = %d, want %d for %s=%q", seq, tc.wantSeq, lib.EnvOriginSeq, tc.raw)
			}
			if stated != tc.wantStated {
				// Naming which way it is wrong, because the two
				// directions have opposite consequences: a false
				// negative sends a worker that was told the answer
				// back to the scan, a false positive makes a worker
				// that was told nothing act as if it were told 0.
				if tc.wantStated {
					t.Errorf("stated = false for %s=%q; the spawner did set the variable, so the adapter must not fall back to the scan", lib.EnvOriginSeq, tc.raw)
				} else {
					t.Errorf("stated = true with %s unset; nothing told this pod a sequence, so it has to take the scan", lib.EnvOriginSeq)
				}
			}

			warned := strings.Contains(logged.String(), "ignoring unusable origin sequence")
			if warned != tc.wantWarn {
				if tc.wantWarn {
					t.Errorf("no warning logged for %s=%q; an unusable value silently costs the worker its origin sequence, and the log line is the only place that says so. log: %q", lib.EnvOriginSeq, tc.raw, logged.String())
				} else {
					t.Errorf("warned on %s=%q, which is a usable value: %q", lib.EnvOriginSeq, tc.raw, logged.String())
				}
			}
			// The warning has to carry the value that caused it, or it
			// names no fault an operator can act on.
			if tc.wantWarn && !strings.Contains(logged.String(), tc.raw) {
				t.Errorf("warning does not quote the offending %s=%q: %q", lib.EnvOriginSeq, tc.raw, logged.String())
			}
		})
	}
}
