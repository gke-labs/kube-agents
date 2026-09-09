package gateway

import (
	"bytes"
	"crypto/hkdf"
	"crypto/sha256"
	"go/ast"
	"go/parser"
	"go/token"
	"strconv"
	"strings"
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
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	t.Setenv("A2A_GCHAT_TOKEN_PATH", "")
	t.Setenv("A2A_GCHAT_ALLOWED_USERS", "")
	t.Setenv("A2A_GCHAT_ALLOW_ALL_USERS", "")
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

// TestFromEnvDerivedSaltIsHKDF: with no salt provisioned and no override,
// the fallback expands the bus password through HKDF-SHA-256 under a fixed
// info string — not a bare digest of the credential (CodeQL
// go/weak-sensitive-data-hashing). The old derivation is pinned as a
// negative so reintroducing it fails here and not only in a scanner.
func TestFromEnvDerivedSaltIsHKDF(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("NATS_PASSWORD", "bus-password")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.AttributionSalt) != 32 {
		t.Fatalf("derived salt is %d bytes, want 32", len(cfg.AttributionSalt))
	}
	if string(cfg.AttributionSalt) == "bus-password" {
		t.Fatal("derived salt is the bus password verbatim")
	}

	// The info string is a wire constant: spelled out here rather than read
	// from the package, so editing it in config.go fails this test.
	want, err := hkdf.Key(sha256.New, []byte("bus-password"), nil, "a2a-attribution-salt", 32)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(cfg.AttributionSalt, want) {
		t.Fatalf("derived salt = %x, want HKDF-SHA-256(password, info=%q) = %x", cfg.AttributionSalt, "a2a-attribution-salt", want)
	}

	old := sha256.Sum256([]byte("a2a-attribution-salt:bus-password"))
	if bytes.Equal(cfg.AttributionSalt, old[:]) {
		t.Fatal("derived salt is the pre-HKDF sha256(\"a2a-attribution-salt:\"+password)")
	}

	// Deterministic for one password — every gateway replica reading the
	// same Secret must produce the same pseudonyms — and different for
	// another, so the salt is not a constant with the password decorating it.
	again, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(cfg.AttributionSalt, again.AttributionSalt) {
		t.Fatal("derived salt is not deterministic for one password")
	}
	t.Setenv("NATS_PASSWORD", "other-password")
	other, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(cfg.AttributionSalt, other.AttributionSalt) {
		t.Fatal("two passwords derived the same salt")
	}
}

// TestConfigSourceHashesOnlyThroughHKDF: config.go is the file that handles
// the bus password, so no bare hash may appear in it — the hash packages it
// imports may be reached only as the constructor argument to hkdf.Key. A
// behavioural test cannot tell a digest of the password from a digest of
// something harmless; this can, and it holds for sha256.Sum256, for the
// streaming h := sha256.New(); h.Write(password) spelling, for an aliased
// import, and for a switch to sha512.
//
// Two limits, stated rather than papered over: it is scoped to this one
// file, because registry.go hashes a session key legitimately, so moving the
// derivation into another file of the package escapes it — the required
// hkdf.Key call below turns that into a failure here rather than a silent
// pass. And a repo-wide ban on this pattern belongs in
// .github/semgrep-security-audit.yml, which this test does not replace.
func TestConfigSourceHashesOnlyThroughHKDF(t *testing.T) {
	const src = "config.go"
	file, err := parser.ParseFile(token.NewFileSet(), src, nil, 0)
	if err != nil {
		t.Fatal(err)
	}

	// Local names of the bare-hash packages and of crypto/hkdf, read from
	// the import block so an alias renames nothing out of the test's sight.
	hashPkgs := map[string]bool{}
	hkdfPkg := ""
	for _, imp := range file.Imports {
		path, err := strconv.Unquote(imp.Path.Value)
		if err != nil {
			t.Fatal(err)
		}
		name := path[strings.LastIndex(path, "/")+1:]
		if imp.Name != nil {
			name = imp.Name.Name
		}
		switch path {
		case "crypto/md5", "crypto/sha1", "crypto/sha256", "crypto/sha512":
			hashPkgs[name] = true
		case "crypto/hkdf":
			hkdfPkg = name
		}
	}
	if hkdfPkg == "" {
		t.Fatalf("%s does not import crypto/hkdf: the salt derivation moved or changed, and this guard no longer covers it", src)
	}

	// The one permitted reference: the hash constructor handed to hkdf.Key.
	permitted := map[ast.Expr]bool{}
	derivations := 0
	ast.Inspect(file, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		fn, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		pkg, ok := fn.X.(*ast.Ident)
		if !ok || pkg.Name != hkdfPkg || fn.Sel.Name != "Key" || len(call.Args) == 0 {
			return true
		}
		derivations++
		permitted[call.Args[0]] = true
		return true
	})
	if derivations == 0 {
		t.Fatalf("%s imports crypto/hkdf but calls no hkdf.Key: the salt derivation moved or changed", src)
	}

	ast.Inspect(file, func(n ast.Node) bool {
		sel, ok := n.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		pkg, ok := sel.X.(*ast.Ident)
		if !ok || !hashPkgs[pkg.Name] || permitted[sel] {
			return true
		}
		t.Errorf("%s reaches %s.%s outside hkdf.Key: the only secret in this file is NATS_PASSWORD, and a credential may not go through a bare hash", src, pkg.Name, sel.Sel.Name)
		return true
	})
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

// TestFromEnvGchatBackendSelection: setting the relay URL selects the gchat
// backend and carries the allowlist; the token path has a projected default.
func TestFromEnvGchatBackendSelection(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "http://relay.ns.svc:8081")
	t.Setenv("A2A_GCHAT_ALLOWED_USERS", "a@example.com, B@example.com ,")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Backend() != "gchat" {
		t.Fatalf("Backend() = %q, want gchat", cfg.Backend())
	}
	if cfg.GchatTokenPath != "/var/run/secrets/a2a-chat-relay/token" {
		t.Fatalf("token path default = %q", cfg.GchatTokenPath)
	}
	if len(cfg.GchatAllowedUsers) != 2 || cfg.GchatAllowedUsers[0] != "a@example.com" || cfg.GchatAllowedUsers[1] != "B@example.com" {
		t.Fatalf("allowed users = %v", cfg.GchatAllowedUsers)
	}
	if cfg.GchatAllowAllUsers {
		t.Fatal("allow-all must be off unless stated")
	}
	t.Setenv("A2A_GCHAT_ALLOW_ALL_USERS", "true")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.GchatAllowAllUsers {
		t.Fatal("A2A_GCHAT_ALLOW_ALL_USERS=true not honored")
	}
}

// TestFromEnvOneBackendPerProcess: two backends on one relay durable split
// event deliveries, and no backend is a misconfiguration, not a default.
func TestFromEnvOneBackendPerProcess(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("A2A_GCHAT_RELAY_URL", "http://relay.ns.svc:8081")
	if _, err := FromEnv(); err == nil {
		t.Fatal("DISCORD_TOKEN and A2A_GCHAT_RELAY_URL together must refuse")
	}
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	if _, err := FromEnv(); err == nil {
		t.Fatal("no backend at all must refuse")
	}
}
