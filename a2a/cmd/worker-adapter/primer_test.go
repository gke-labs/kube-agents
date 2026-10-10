package main

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The primer file is read as written; unset or missing is a first turn and
// no error, so a pod from an older spawner still runs.
func TestReadPrimer(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	t.Setenv(lib.EnvPrimerFile, "")
	if got := readPrimer(log); got != "" {
		t.Errorf("unset: %q", got)
	}
	t.Setenv(lib.EnvPrimerFile, filepath.Join(t.TempDir(), "absent"))
	if got := readPrimer(log); got != "" {
		t.Errorf("missing file: %q", got)
	}
	path := filepath.Join(t.TempDir(), "primer")
	if err := os.WriteFile(path, []byte("User: hi\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(lib.EnvPrimerFile, path)
	if got := readPrimer(log); got != "User: hi\n" {
		t.Errorf("present: %q", got)
	}
}

// configFromEnv is where the worker's config is built; the primer has to
// make it into Config, or readPrimer is dead code.
func TestConfigFromEnvCarriesThePrimer(t *testing.T) {
	path := filepath.Join(t.TempDir(), "primer")
	if err := os.WriteFile(path, []byte("The user said: hi\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv(lib.EnvPrimerFile, path)
	t.Setenv("TASK_ID", "task-env-contract")
	t.Setenv("PROFILE", "chat")
	t.Setenv("NATS_URL", "nats://127.0.0.1:1")
	cfg, ok := configFromEnv(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if !ok {
		t.Fatal("configFromEnv refused the minimal env")
	}
	if cfg.Primer != "The user said: hi\n" {
		t.Errorf("cfg.Primer = %q", cfg.Primer)
	}
}
