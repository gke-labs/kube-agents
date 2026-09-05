package gateway

import (
	"testing"
	"time"
)

// setBaseEnv pins the required env plus empty values for every optional
// knob these tests exercise, so a developer's exported variables cannot
// leak in (envOr treats empty as unset).
func setBaseEnv(t *testing.T) {
	t.Helper()
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")
	t.Setenv("NATS_PASSWORD", "pw")
	t.Setenv("DISCORD_TOKEN", "x")
	t.Setenv("SESSION_KV_SALT", "")
	t.Setenv("A2A_ATTRIBUTION_SALT", "")
	t.Setenv("A2A_TASK_DEADLINE_SECONDS", "")
	t.Setenv("A2A_ASK_TTL", "")
	t.Setenv("A2A_OWNER_DEPLOYMENT", "")
	t.Setenv("A2A_MAX_SESSIONS", "")
	t.Setenv("A2A_IDLE_TTL", "")
}

// TestFromEnvSaltPrecedence: the salt is SESSION_KV_SALT, the one the
// install provisions (settled 8/31) — it wins over the playground override,
// which wins over the derived fallback. Deriving from the bus password is
// the recorded deviation: it breaks the cross-surface join and hands a
// de-anonymization key to whoever holds that credential.
func TestFromEnvSaltPrecedence(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if string(cfg.AttributionSalt) == "pw" || len(cfg.AttributionSalt) != 32 {
		t.Fatalf("derived fallback should be a 32-byte digest, not the password: %d bytes", len(cfg.AttributionSalt))
	}

	t.Setenv("A2A_ATTRIBUTION_SALT", "legacy-salt")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if string(cfg.AttributionSalt) != "legacy-salt" {
		t.Fatalf("A2A_ATTRIBUTION_SALT not honored: %q", cfg.AttributionSalt)
	}

	t.Setenv("SESSION_KV_SALT", "install-salt")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if string(cfg.AttributionSalt) != "install-salt" {
		t.Fatalf("SESSION_KV_SALT must win over every fallback: %q", cfg.AttributionSalt)
	}

	// The shipped redactor does .strip() on this env; a Secret made from a
	// file with a trailing newline must hash the same on both surfaces.
	t.Setenv("SESSION_KV_SALT", "\ninstall-salt \n")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if string(cfg.AttributionSalt) != "install-salt" {
		t.Fatalf("SESSION_KV_SALT not trimmed to match the redactor: %q", cfg.AttributionSalt)
	}

	// No salt of any kind and an empty password: the derived fallback would
	// be a public constant — refuse at boot.
	t.Setenv("SESSION_KV_SALT", "")
	t.Setenv("A2A_ATTRIBUTION_SALT", "")
	t.Setenv("NATS_PASSWORD", "")
	if _, err := FromEnv(); err == nil {
		t.Fatal("empty password with no salt accepted")
	}
}

// TestFromEnvTaskDeadline: the env contract shared with the worker adapter
// (A2A_TASK_DEADLINE_SECONDS, integer seconds) — absent means the adapter's
// own 1800s default, and a value the deadline cannot honestly enforce
// refuses at boot.
func TestFromEnvTaskDeadline(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.TaskDeadline != 30*time.Minute {
		t.Fatalf("default TaskDeadline = %v, want 30m", cfg.TaskDeadline)
	}

	t.Setenv("A2A_TASK_DEADLINE_SECONDS", "900")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.TaskDeadline != 15*time.Minute {
		t.Fatalf("TaskDeadline = %v, want 15m", cfg.TaskDeadline)
	}

	for _, bad := range []string{"59", "0", "-1", "30m"} {
		t.Setenv("A2A_TASK_DEADLINE_SECONDS", bad)
		if _, err := FromEnv(); err == nil {
			t.Fatalf("A2A_TASK_DEADLINE_SECONDS=%q accepted", bad)
		}
	}
}

// TestFromEnvAskTTL: the independent bound on the KV ask copy — absent
// means 24h (under the stream's 72h retention, above any legitimate task),
// and a sub-minute value refuses at boot.
func TestFromEnvAskTTL(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AskTTL != 24*time.Hour {
		t.Fatalf("default AskTTL = %v, want 24h", cfg.AskTTL)
	}

	t.Setenv("A2A_ASK_TTL", "2h")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AskTTL != 2*time.Hour {
		t.Fatalf("AskTTL = %v, want 2h", cfg.AskTTL)
	}

	for _, bad := range []string{"30s", "junk"} {
		t.Setenv("A2A_ASK_TTL", bad)
		if _, err := FromEnv(); err == nil {
			t.Fatalf("A2A_ASK_TTL=%q accepted", bad)
		}
	}
}

// TestFromEnvOwnerDeployment: the owner passes through; empty stays empty
// (playground spawns unowned pods, the documented fallback).
func TestFromEnvOwnerDeployment(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.OwnerDeployment != "" {
		t.Fatalf("OwnerDeployment = %q, want empty", cfg.OwnerDeployment)
	}

	t.Setenv("A2A_OWNER_DEPLOYMENT", "agent-a2a-gateway")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.OwnerDeployment != "agent-a2a-gateway" {
		t.Fatalf("OwnerDeployment = %q", cfg.OwnerDeployment)
	}
}
