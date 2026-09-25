package gateway

import (
	"bytes"
	"crypto/hkdf"
	"crypto/sha256"
	"fmt"
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
	t.Setenv("A2A_SESSION_TTL", "")
	t.Setenv("A2A_FIRST_EVENT_GRACE", "")
	t.Setenv("A2A_OWNER_DEPLOYMENT", "")
	t.Setenv("A2A_MAX_SESSIONS", "")
	t.Setenv("A2A_IDLE_TTL", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	t.Setenv("A2A_GCHAT_TOKEN_PATH", "")
	t.Setenv("A2A_GCHAT_ALLOWED_USERS", "")
	t.Setenv("A2A_GCHAT_ALLOW_ALL_USERS", "")
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
	t.Setenv("A2A_INJECT_LISTEN", "")
	t.Setenv("A2A_INJECT_TOKEN", "")
	t.Setenv("A2A_INJECT_PRINCIPAL_MAP", "")
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

// TestFromEnvSessionTTL: the bound on idle session records in session-state —
// absent means 7 days (well past TASKS 72h retention), and a sub-72h
// value or invalid format refuses at boot.
func TestFromEnvSessionTTL(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.SessionTTL != 7*24*time.Hour {
		t.Fatalf("default SessionTTL = %v, want 168h", cfg.SessionTTL)
	}

	t.Setenv("A2A_SESSION_TTL", "96h")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.SessionTTL != 96*time.Hour {
		t.Fatalf("SessionTTL = %v, want 96h", cfg.SessionTTL)
	}

	for _, bad := range []string{"71h", "48h", "30m", "junk"} {
		t.Setenv("A2A_SESSION_TTL", bad)
		if _, err := FromEnv(); err == nil {
			t.Fatalf("A2A_SESSION_TTL=%q accepted", bad)
		}
	}

	// Refusal when SessionTTL <= TaskDeadline
	t.Setenv("A2A_SESSION_TTL", "96h")
	t.Setenv("A2A_TASK_DEADLINE_SECONDS", fmt.Sprintf("%d", int((100*time.Hour).Seconds())))
	if _, err := FromEnv(); err == nil {
		t.Fatal("expected refusal when SessionTTL <= TaskDeadline, got nil")
	}
}

// TestFromEnvFirstEventGrace: the bound on a task with no events at all —
// absent means 10m (the pod deadline's pre-start budget), and a sub-minute
// value refuses at boot.
func TestFromEnvFirstEventGrace(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.FirstEventGrace != 10*time.Minute {
		t.Fatalf("default FirstEventGrace = %v, want 10m", cfg.FirstEventGrace)
	}

	t.Setenv("A2A_FIRST_EVENT_GRACE", "3m")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.FirstEventGrace != 3*time.Minute {
		t.Fatalf("FirstEventGrace = %v, want 3m", cfg.FirstEventGrace)
	}

	for _, bad := range []string{"30s", "junk"} {
		t.Setenv("A2A_FIRST_EVENT_GRACE", bad)
		if _, err := FromEnv(); err == nil {
			t.Fatalf("A2A_FIRST_EVENT_GRACE=%q accepted", bad)
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

// TestFromEnvInjectDoorArmsOnItsOwn: the door alone is enough for the gateway
// to start, which is what makes it the answer for an eval install (#1660) --
// a gateway with no ingress at all crash-loops, so a `mode: next` install
// never reads Ready and nothing can gate a rollout on it.
func TestFromEnvInjectDoorArmsOnItsOwn(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_INJECT_LISTEN", " :8099 ")
	t.Setenv("A2A_INJECT_TOKEN", " s3cret ")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.InjectArmed() {
		t.Fatal("the door is not armed")
	}
	// No REAL backend, and Backend() says so rather than naming the door:
	// which backend a gateway has and whether its door is open are two
	// questions, and collapsing them is what made the door exclusive.
	if cfg.Backend() != "" {
		t.Fatalf("Backend() = %q, want empty: the door is not a backend", cfg.Backend())
	}
	// Trimmed, because a value that is nothing but whitespace must read as
	// unset rather than as a door armed on an address net.Listen refuses.
	if cfg.InjectListen != ":8099" || cfg.InjectToken != "s3cret" {
		t.Fatalf("listen = %q token = %q, want both trimmed", cfg.InjectListen, cfg.InjectToken)
	}
	// Its own map, and never the chat backends'. An entry written for a
	// Discord sender must be unreachable from a door that takes its author
	// from a request body.
	if cfg.InjectPrincipalMapPath != defaultInjectPrincipalMapPath {
		t.Fatalf("InjectPrincipalMapPath = %q, want %q", cfg.InjectPrincipalMapPath, defaultInjectPrincipalMapPath)
	}
	if cfg.InjectPrincipalMapPath == cfg.PrincipalMapPath {
		t.Fatal("the door reads the chat backends' principal map")
	}

	t.Setenv("A2A_INJECT_LISTEN", "   ")
	if _, err := FromEnv(); err == nil {
		t.Fatal("a whitespace-only A2A_INJECT_LISTEN with no other ingress must refuse, not arm a door")
	}
}

// TestFromEnvRefusesADoorWithNoToken: there is no unauthenticated mode. The
// fence in front of the door does not govern the port-forward its caller
// uses, so a door without a token would widen who can drive the platform
// persona from the holders of the agent's API key to anyone with
// pods/portforward in the namespace.
func TestFromEnvRefusesADoorWithNoToken(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_INJECT_LISTEN", ":8099")
	t.Setenv("A2A_INJECT_TOKEN", "")
	_, err := FromEnv()
	if err == nil {
		t.Fatal("the door armed with no bearer token")
	}
	if !strings.Contains(err.Error(), "A2A_INJECT_TOKEN") {
		t.Fatalf("the refusal does not name the variable to set: %v", err)
	}
	// Whitespace is not a token either.
	t.Setenv("A2A_INJECT_TOKEN", "   ")
	if _, err := FromEnv(); err == nil {
		t.Fatal("whitespace passed as a bearer token")
	}
}

// TestFromEnvTheDoorSitsBesideARealBackend is the decision this guard
// encodes (bnaylor, 2026-09-17): the door is a side door, not a backend.
//
// The guard exists so that a two-backend misconfiguration cannot leave one
// backend silently unconsumed -- two processes on one Chat relay durable
// split its deliveries. A local HTTP door consumes nothing and competes for
// nothing, so counting it would buy no safety and would cost stage 2 the
// thing it needs: the eval door and the Chat relay on one install, so the
// two transports can be compared against it.
func TestFromEnvTheDoorSitsBesideARealBackend(t *testing.T) {
	for _, tc := range []struct {
		name    string
		env     map[string]string
		backend string
	}{
		{"beside discord", map[string]string{"DISCORD_TOKEN": "x"}, discordBackend},
		{"beside the chat relay", map[string]string{
			"DISCORD_TOKEN": "", "A2A_GCHAT_RELAY_URL": "http://relay.ns.svc:8081",
		}, gchatBackend},
	} {
		t.Run(tc.name, func(t *testing.T) {
			setBaseEnv(t)
			for key, value := range tc.env {
				t.Setenv(key, value)
			}
			t.Setenv("A2A_INJECT_LISTEN", ":8099")
			t.Setenv("A2A_INJECT_TOKEN", "s3cret")
			cfg, err := FromEnv()
			if err != nil {
				t.Fatalf("the door beside one real backend must be accepted: %v", err)
			}
			if cfg.Backend() != tc.backend {
				t.Fatalf("Backend() = %q, want %q: the door must not displace the real backend",
					cfg.Backend(), tc.backend)
			}
			if !cfg.InjectArmed() {
				t.Fatal("the door is not armed beside the backend")
			}
		})
	}
}

// TestFromEnvOneBackendPerProcess: two REAL backends on one relay durable
// split event deliveries, and no ingress at all is a misconfiguration, not a
// default. The check counts rather than enumerating pairs, so a fourth
// backend cannot be armed alongside one nobody thought of. The inject door is
// not in the count -- TestFromEnvTheDoorSitsBesideARealBackend is that half.
func TestFromEnvOneBackendPerProcess(t *testing.T) {
	setBaseEnv(t)
	for _, tc := range []struct {
		name string
		env  map[string]string
	}{
		{"discord and gchat", map[string]string{
			"DISCORD_TOKEN": "x", "A2A_GCHAT_RELAY_URL": "http://relay.ns.svc:8081",
		}},
		{"discord and gchat with the door open too", map[string]string{
			"DISCORD_TOKEN": "x", "A2A_GCHAT_RELAY_URL": "http://relay.ns.svc:8081",
			"A2A_INJECT_LISTEN": ":8099", "A2A_INJECT_TOKEN": "s3cret",
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			setBaseEnv(t)
			t.Setenv("DISCORD_TOKEN", "")
			for key, value := range tc.env {
				t.Setenv(key, value)
			}
			_, err := FromEnv()
			if err == nil {
				t.Fatal("two chat backends in one process must refuse")
			}
			// The message has to name what is armed, or an operator reading
			// it cannot tell which variable to unset. The door is not one of
			// them: naming it would send the reader to unset the one thing
			// that is allowed to be there.
			for key := range tc.env {
				if strings.HasPrefix(key, "A2A_INJECT_") {
					// Not in the list of what is armed. The message may go on
					// to say the door is allowed there -- that is the point
					// it should make -- but the list an operator reads to
					// decide what to unset must hold the two backends alone.
					armed, _, _ := strings.Cut(err.Error(), ")")
					if strings.Contains(armed, key) {
						t.Errorf("the refusal lists %s among what is armed, and it is not one of the "+
							"two backends: %v", key, err)
					}
					continue
				}
				if !strings.Contains(err.Error(), key) {
					t.Errorf("the refusal does not name %s: %v", key, err)
				}
			}
		})
	}

	setBaseEnv(t)
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	t.Setenv("A2A_INJECT_LISTEN", "")
	if _, err := FromEnv(); err == nil {
		t.Fatal("no backend at all must refuse")
	}
}

// A typo in the display mode must be refused, not silently rendered as
// debug (the relay branches on != default).
func TestFromEnvRefusesAnUnknownDisplayMode(t *testing.T) {
	setBaseEnv(t)
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "verbose")
	if _, err := FromEnv(); err == nil || !strings.Contains(err.Error(), "A2A_CHAT_DISPLAY_MODE") {
		t.Fatalf("FromEnv() error = %v; want a refusal naming A2A_CHAT_DISPLAY_MODE", err)
	}
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "default")
	if cfg, err := FromEnv(); err != nil || cfg.DisplayMode != "default" {
		t.Fatalf("FromEnv() = %+v, %v", cfg, err)
	}
}

// The advisory `…events` writer-class check has to be tightenable by config,
// because the instruction attached to it is "flip this one retention window
// after the split reaches an install" and a flip that needs a new image does
// not get made. The default is the loose one: a strict gateway started before
// the window elapses refuses supervisor terminals written before the split.
func TestFromEnvStrictEventsWriter(t *testing.T) {
	setBaseEnv(t)

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StrictEventsWriter {
		t.Fatal("StrictEventsWriter defaults on; pre-split supervisor terminals would be refused")
	}
	if SupervisorAgreement(cfg).StrictEventsWriter {
		t.Fatal("the default config produced a strict agreement policy")
	}

	t.Setenv("A2A_STRICT_EVENTS_WRITER", "true")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if !cfg.StrictEventsWriter {
		t.Fatal("A2A_STRICT_EVENTS_WRITER=true did not tighten the config")
	}
	if !SupervisorAgreement(cfg).StrictEventsWriter {
		t.Fatal("the tightened config did not reach the agreement policy")
	}
	// The supervisor name is not lost when the flag moves.
	if got := SupervisorAgreement(cfg).Supervisor; got != gatewayParty.Session {
		t.Fatalf("Supervisor = %q, want %q", got, gatewayParty.Session)
	}

	t.Setenv("A2A_STRICT_EVENTS_WRITER", "yes")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StrictEventsWriter {
		t.Fatal("a near-miss value tightened the check; the safe direction is loose")
	}
}
