package gateway

import (
	"os"
	"strings"
	"testing"
)

var allowlistTestPS = NewPseudonymizer([]byte("test-salt"))

// subjectOf is what a history entry stores for an author on backend.
func subjectOf(backend, authorID string) string {
	return requesterSubject(allowlistTestPS, backend, authorID)
}

func TestTargetAllowsTable(t *testing.T) {
	cfg := &Config{TargetAllowedUsers: map[string]map[string][]string{
		"platform": {
			gchatBackend: {"Alice@Example.com"},
			slackBackend: {"U0ABC"},
		},
	}}
	g := &Gateway{cfg: cfg, targetAllowed: buildTargetAllowed(cfg, allowlistTestPS)}
	for _, tc := range []struct {
		target, backend, author string
		want                    bool
	}{
		{"platform", gchatBackend, "alice@example.com", true}, // email, case-insensitive
		{"platform", gchatBackend, "ALICE@EXAMPLE.COM", true},
		{"platform", gchatBackend, " alice@example.com ", true}, // and trimmed
		{"platform", gchatBackend, "bob@example.com", false},
		{"platform", slackBackend, "U0ABC", true},
		{"platform", slackBackend, "u0abc", false}, // member id, exact
		{"platform", slackBackend, "", false},      // a blank subject is never a member
		{"platform", discordBackend, "1001", true}, // no list for the backend: ingress is the gate
		{"platform", injectBackend, "devops-bench", true},
		{"other", gchatBackend, "bob@example.com", true}, // no list for the target
	} {
		if got := g.targetAllows(tc.target, tc.backend, subjectOf(tc.backend, tc.author)); got != tc.want {
			t.Errorf("targetAllows(%q,%q,%q) = %v, want %v", tc.target, tc.backend, tc.author, got, tc.want)
		}
	}
}

// TestAnUnsetListIsNoList: no list for the (target, backend) pair leaves the
// ingress allowlist as the only gate.
func TestAnUnsetListIsNoList(t *testing.T) {
	cfg := &Config{TargetAllowedUsers: map[string]map[string][]string{"platform": {slackBackend: {"U0ABC"}}}}
	g := &Gateway{cfg: cfg, targetAllowed: buildTargetAllowed(cfg, allowlistTestPS)}
	if !g.targetAllows("platform", gchatBackend, subjectOf(gchatBackend, "anyone@example.com")) {
		t.Fatal("a backend with no list refused a requester; no list must read as all authenticated users")
	}
}

// TestABlankListIsNobody: a list that is present but blank after trimming
// admits nobody, the rule #2207 set for the Chat ingress list. The operator
// renders the var empty for a CR list of blanks so this reaches the gateway.
func TestABlankListIsNobody(t *testing.T) {
	cfg := &Config{TargetAllowedUsers: map[string]map[string][]string{"platform": {gchatBackend: {" ", ""}, slackBackend: nil}}}
	g := &Gateway{cfg: cfg, targetAllowed: buildTargetAllowed(cfg, allowlistTestPS)}
	if g.targetAllows("platform", gchatBackend, subjectOf(gchatBackend, "anyone@example.com")) {
		t.Fatal("a list of blanks admitted a requester; a present blank list must admit nobody")
	}
	if g.targetAllows("platform", slackBackend, subjectOf(slackBackend, "U0ABC")) {
		t.Fatal("an empty present list admitted a requester")
	}
}

// TestTargetAllowedHoldsNoPlaintext: the compiled lists compare against the
// pseudonym the KV stores, so they hold pseudonyms too, and the raw id is not
// a member even though it is the configured string.
func TestTargetAllowedHoldsNoPlaintext(t *testing.T) {
	cfg := &Config{TargetAllowedUsers: map[string]map[string][]string{
		"platform": {gchatBackend: {"Alice@Example.com"}, slackBackend: {"U0ABC"}},
	}}
	g := &Gateway{cfg: cfg, targetAllowed: buildTargetAllowed(cfg, allowlistTestPS)}
	for backend, set := range g.targetAllowed["platform"] {
		for entry := range set {
			if !strings.HasPrefix(entry, "hmac:") {
				t.Errorf("%s list holds %q, want only pseudonyms", backend, entry)
			}
		}
	}
	if g.targetAllows("platform", slackBackend, "U0ABC") {
		t.Error("a raw member id matched the hashed list; targetAllows must take the stored subject")
	}
}

func TestTargetAllowedUsersParseFromEnv(t *testing.T) {
	setBaseEnv(t) // the shared FromEnv environment from config_test.go
	t.Setenv(EnvTargetAllowedUsersGchat, " Alice@Example.com, bob@example.com ,")
	t.Setenv(EnvTargetAllowedUsersSlack, "U0ABC,,U0DEF")
	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	got := cfg.TargetAllowedUsers["platform"]
	if len(got[gchatBackend]) != 2 || got[gchatBackend][0] != "Alice@Example.com" {
		t.Fatalf("gchat list = %v", got[gchatBackend])
	}
	if len(got[slackBackend]) != 2 || got[slackBackend][1] != "U0DEF" {
		t.Fatalf("slack list = %v", got[slackBackend])
	}
	// Set but empty is a list that admits nobody: the operator renders it
	// for a CR list of blanks.
	t.Setenv(EnvTargetAllowedUsersGchat, " , ")
	t.Setenv(EnvTargetAllowedUsersSlack, "")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	lists, ok := cfg.TargetAllowedUsers["platform"]
	if !ok || len(lists) != 2 {
		t.Fatalf("set-but-empty env parsed as no lists: %v", lists)
	}
	for _, backend := range []string{gchatBackend, slackBackend} {
		if l, present := lists[backend]; !present || len(l) != 0 {
			t.Fatalf("%s list = %v (present=%v), want present and empty", backend, l, present)
		}
	}
	// Unset is no list.
	os.Unsetenv(EnvTargetAllowedUsersGchat)
	os.Unsetenv(EnvTargetAllowedUsersSlack)
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if lists := cfg.TargetAllowedUsers["platform"]; len(lists) != 0 {
		t.Fatalf("unset env parsed as lists: %v", lists)
	}
}
