package main

import (
	"os"
	"strings"
	"testing"
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
