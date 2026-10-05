package gateway

import (
	"testing"
)

func TestTargetAllowsTable(t *testing.T) {
	cfg := &Config{TargetAllowedUsers: map[string]map[string][]string{
		"platform": {
			gchatBackend: {"Alice@Example.com"},
			slackBackend: {"U0ABC"},
		},
	}}
	g := &Gateway{cfg: cfg, targetAllowed: buildTargetAllowed(cfg)}
	for _, tc := range []struct {
		target, backend, author string
		want                    bool
	}{
		{"platform", gchatBackend, "alice@example.com", true}, // email, case-insensitive
		{"platform", gchatBackend, "ALICE@EXAMPLE.COM", true},
		{"platform", gchatBackend, "bob@example.com", false},
		{"platform", slackBackend, "U0ABC", true},
		{"platform", slackBackend, "u0abc", false}, // member id, exact
		{"platform", discordBackend, "1001", true}, // no list for the backend: ingress is the gate
		{"platform", injectBackend, "devops-bench", true},
		{"other", gchatBackend, "bob@example.com", true}, // no list for the target
	} {
		if got := g.targetAllows(tc.target, tc.backend, tc.author); got != tc.want {
			t.Errorf("targetAllows(%q,%q,%q) = %v, want %v", tc.target, tc.backend, tc.author, got, tc.want)
		}
	}
}

func TestAnEmptyListIsNoList(t *testing.T) {
	cfg := &Config{TargetAllowedUsers: map[string]map[string][]string{"platform": {gchatBackend: {" ", ""}}}}
	g := &Gateway{cfg: cfg, targetAllowed: buildTargetAllowed(cfg)}
	if !g.targetAllows("platform", gchatBackend, "anyone@example.com") {
		t.Fatal("a list of blanks refused a requester; blanks must read as no list")
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
	t.Setenv(EnvTargetAllowedUsersGchat, "")
	t.Setenv(EnvTargetAllowedUsersSlack, "")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if lists := cfg.TargetAllowedUsers["platform"]; len(lists) != 0 {
		t.Fatalf("empty env parsed as lists: %v", lists)
	}
}
