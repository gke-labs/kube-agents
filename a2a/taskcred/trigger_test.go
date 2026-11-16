package taskcred

import (
	"strings"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
)

func proxyClaims(now time.Time) Claims {
	c := validClaims(now)
	c.Audience = AudienceCredentialProxy
	return c
}

func TestATriggerRoundTripsOnAProxyCredential(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	for _, trig := range []Trigger{
		{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "compliance-audit"},
		{Name: "schedule/compliance-audit", Origin: "builtin"},
		{Name: "stall-watch", Origin: "builtin"},
	} {
		c := proxyClaims(now)
		tr := trig
		c.Trigger = &tr
		got, err := Verify(mint(t, kp, c), []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew)
		if err != nil {
			t.Fatalf("%+v: refused: %v", trig, err)
		}
		if got.Trigger == nil || *got.Trigger != trig {
			t.Errorf("trigger = %+v, want %+v", got.Trigger, trig)
		}
	}
}

func TestACredentialWithoutATriggerHasNone(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	got, err := Verify(mint(t, kp, proxyClaims(now)), []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew)
	if err != nil || got.Trigger != nil {
		t.Errorf("trigger = %+v, err %v; want none", got.Trigger, err)
	}
}

func TestATriggerIsForTheCredentialProxyAlone(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	c := validClaims(now) // nats
	c.Trigger = &Trigger{Name: "schedule/compliance-audit", Origin: "builtin", Stream: "compliance-audit"}
	if _, err := Mint(kp, c); err == nil {
		t.Error("Mint put a trigger on a nats credential")
	}
	tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) {
		gc.Data["trigger"] = map[string]any{"name": "schedule/x", "origin": "builtin"}
	})
	if _, err := Verify(tok, []string{pub}, AudienceNATS, now, testLifetime, testSkew); err == nil {
		t.Error("Verify accepted a trigger on a nats credential")
	}
}

func TestMalformedTriggersAreRefused(t *testing.T) {
	kp, pub := signingKey(t)
	now := time.Now()
	long := strings.Repeat("a", 64)
	for name, trig := range map[string]map[string]any{
		"no name":                   {"origin": "builtin"},
		"an empty name":             {"name": "", "origin": "builtin"},
		"a dotted name":             {"name": "schedule/a.b", "origin": "builtin"},
		"a wildcard name":           {"name": "schedule/*", "origin": "builtin"},
		"three name segments":       {"name": "schedule/a/b", "origin": "builtin"},
		"an empty name segment":     {"name": "schedule/", "origin": "builtin"},
		"an upper-case name":        {"name": "Schedule/a", "origin": "builtin"},
		"an over-long name part":    {"name": "schedule/" + long, "origin": "builtin"},
		"no origin":                 {"name": "schedule/a"},
		"an unknown origin":         {"name": "schedule/a", "origin": "admin"},
		"a user origin":             {"name": "schedule/a", "origin": "user"},
		"another schedule's stream": {"name": "schedule/a", "origin": "builtin", "stream": "b"},
		"a stream on a bare source": {"name": "stall-watch", "origin": "builtin", "stream": "stall-watch"},
		"an upper-case origin":      {"name": "schedule/a", "origin": "Builtin"},
		"a dotted stream":           {"name": "schedule/a", "origin": "builtin", "stream": "a.b"},
		"a stream with a path":      {"name": "schedule/a", "origin": "builtin", "stream": "user/a"},
		"a doubled user prefix":     {"name": "schedule/x", "origin": "builtin", "stream": "user/user/x"},
		"an unknown trigger key":    {"name": "schedule/a", "origin": "builtin", "scope": "fleet"},
		"a non-string origin":       {"name": "schedule/a", "origin": 1},
		"a null name":               {"name": nil, "origin": "builtin"},
		"a case-folded key":         {"NAME": "schedule/a", "origin": "builtin"},
	} {
		tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) {
			gc.Audience = string(AudienceCredentialProxy)
			gc.Data["trigger"] = trig
		})
		if _, err := Verify(tok, []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew); err == nil {
			t.Errorf("%s: Verify accepted %v", name, trig)
		}
	}
	tok := handMint(t, kp, now, func(gc *jwt.GenericClaims) {
		gc.Audience = string(AudienceCredentialProxy)
		gc.Data["trigger"] = "schedule/a"
	})
	if _, err := Verify(tok, []string{pub}, AudienceCredentialProxy, now, testLifetime, testSkew); err == nil {
		t.Error("Verify accepted a trigger that is not an object")
	}
	for name, trig := range map[string]Trigger{
		"no name":         {Origin: "builtin"},
		"a bad origin":    {Name: "schedule/a", Origin: "root"},
		"a dotted stream": {Name: "schedule/a", Origin: "builtin", Stream: "a.b"},
	} {
		c := proxyClaims(now)
		tr := trig
		c.Trigger = &tr
		if _, err := Mint(kp, c); err == nil {
			t.Errorf("%s: Mint accepted %+v", name, trig)
		}
	}
}
