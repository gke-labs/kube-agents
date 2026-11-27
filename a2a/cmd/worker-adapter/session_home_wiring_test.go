package main

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// runChildEnv makes this test binary run run() and exit with its code, so a
// test can see what run() does in each mode without a bus.
const runChildEnv = "WORKER_ADAPTER_TEST_RUN_CHILD"

// runChildSessionPersonaEnv points the child's core-agent session persona at
// a tree the test wrote.
const runChildSessionPersonaEnv = "A2A_TEST_SESSION_PERSONA_DIR"

// sessionHomeFailure is the start-up check's error, as run() logs it.
const sessionHomeFailure = "the session's shipped persona and skills"

func TestRunChild(t *testing.T) {
	if os.Getenv(runChildEnv) != "1" {
		t.Skip("helper for the wiring tests below")
	}
	os.Args = append([]string{os.Args[0]}, strings.Fields(os.Getenv(runChildEnv+"_ARGS"))...)
	// A test can put the core-agent session tree somewhere it wrote one.
	if dir := os.Getenv(runChildSessionPersonaEnv); dir != "" {
		coreAgentSessionPersonaDir = dir
	}
	os.Exit(run())
}

// changedSessionHome is a harness home whose shipped skill differs from its
// manifest: the check fails on it wherever it runs.
func changedSessionHome(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	files := map[string][]byte{lib.SessionHomePersonaFile: []byte("persona\n"), lib.SessionHomeSkillsDir + "/triage/SKILL.md": []byte("built\n")}
	for rel, data := range files {
		p := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(dir, persona.IntegrityManifestFile), persona.FormatIntegrityManifest(files), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, lib.SessionHomeSkillsDir, "triage", "SKILL.md"), []byte("changed\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return dir
}

// runChild runs run() in a child process with env on top of a minimal
// environment and returns its exit code and stderr.
func runChild(t *testing.T, args string, env ...string) (int, string) {
	t.Helper()
	cmd := exec.Command(os.Args[0], "-test.run=^TestRunChild$")
	cmd.Env = append([]string{runChildEnv + "=1", runChildEnv + "_ARGS=" + args, "PATH=" + os.Getenv("PATH"), "HOME=" + t.TempDir()}, env...)
	cmd.Dir = t.TempDir()
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	done := make(chan error, 1)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() { done <- cmd.Wait() }()
	select {
	case <-done:
	case <-time.After(60 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatalf("run() did not return; stderr: %s", stderr.String())
	}
	return cmd.ProcessState.ExitCode(), stderr.String()
}

// A session worker checks its shipped tree before anything else that
// needs the bus, and a difference exits 1 naming the file.
func TestRunChecksTheSessionHomeInSessionMode(t *testing.T) {
	home := changedSessionHome(t)
	code, stderr := runChild(t, "", "TASK_ID=task-wiring", "PROFILE=p", "NATS_URL=nats://127.0.0.1:1", claudeConfigDirEnv+"="+home, "A2A_WORKDIR="+t.TempDir())
	if code != 1 || !strings.Contains(stderr, sessionHomeFailure) || !strings.Contains(stderr, "triage/SKILL.md") {
		t.Errorf("session mode: exit %d, stderr %q; want 1 and the session-home check's error", code, stderr)
	}
}

// A profile worker and the two MCP subcommands don't run the session-home
// check: the profile worker checks its own persona instead (#6830), and the
// subcommands are stdio servers inside a harness, not task runs.
func TestRunSkipsTheSessionHomeCheckOutsideSessionMode(t *testing.T) {
	home := changedSessionHome(t)
	for _, tc := range []struct {
		name string
		args string
		env  []string
	}{
		{name: "profile worker", env: []string{lib.EnvProfileWorker + "=true", "PROFILE=p", "NATS_URL=nats://127.0.0.1:1", "A2A_WORKDIR=" + t.TempDir()}},
		{name: "mcp-relay usage", args: "mcp-relay"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, stderr := runChild(t, tc.args, append(tc.env, claudeConfigDirEnv+"="+home)...)
			if strings.Contains(stderr, sessionHomeFailure) {
				t.Errorf("%s ran the session-home check: %q", tc.name, stderr)
			}
		})
	}
}
