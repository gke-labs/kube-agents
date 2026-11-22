package gateway

import (
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/capability"
)

func TestParseTierMembership(t *testing.T) {
	got, err := parseTierMembership(" gchat:a@x.com=oncall, slack:U2=read ,,a2a-google:a@x.com=oncall, a2a-entra:1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d=oncall")
	if err != nil || len(got) != 4 || got[0] != (TierMember{Backend: "gchat", User: "a@x.com", Tier: "oncall"}) || got[3].Backend != a2aEntraBackend {
		t.Fatalf("parse = %+v, %v", got, err)
	}
	for _, bad := range []string{"gchat:u", "gchat=read", "teams:u=read", "console:x=read", "a2a:svc=read", "gchat:u=on.call", "gchat:u=read,gchat:u=oncall", "gchat:Alice=read,gchat:alice=oncall", "gchat:=read", "a2a-entra:1A2B3C4D-5E6F-4A7B-8C9D-0E1F2A3B4C5D=read,a2a-entra:1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d=oncall"} {
		if _, err := parseTierMembership(bad); err == nil {
			t.Errorf("parse(%q) accepted a bad list", bad)
		}
	}
	if got, err := parseTierMembership(""); err != nil || len(got) != 0 {
		t.Errorf("an empty list: %+v, %v", got, err)
	}
}

// tierGateway is a gateway with the oncall and nightly tiers loaded, alice
// on gchat mapped to oncall, carol to nightly (unattended) and dave to a tier
// nothing defines; everyone else is read.
func tierGateway(t *testing.T) (*Gateway, TaskRequester, TaskRequester, TaskRequester, TaskRequester, TaskRequester) {
	t.Helper()
	t.Cleanup(func() { capability.LoadTiers(nil) })
	capability.LoadTiers([]capability.TierDefinition{{Name: "oncall", Mode: capability.TierModeInteractive}, {Name: "nightly", Mode: capability.TierModeUnattended}})
	ps := NewPseudonymizer([]byte("salt"))
	members := []TierMember{{"gchat", "alice", "oncall"}, {"gchat", "carol", "nightly"}, {"gchat", "dave", "ghost"}}
	g := &Gateway{cfg: &Config{AuthorityTier: capability.TierDeveloperTeam}, tierMembers: buildTierMembers(members, ps), log: slog.New(slog.DiscardHandler)}
	who := func(backend, id string) TaskRequester {
		return TaskRequester{Backend: backend, Subject: requesterSubject(ps, backend, id)}
	}
	return g, who("gchat", "alice"), who("gchat", "bob"), who("gchat", "carol"), who("gchat", "dave"), TaskRequester{Backend: consoleBackend, Subject: "console"}
}

func tierRefusal(t *testing.T, err error) string {
	t.Helper()
	var r errRefused
	if !errors.As(err, &r) {
		t.Fatalf("err %v, want a refusal notice", err)
	}
	return r.notice
}

// Tier 2c (#3659): an author's tier is the membership's, else read; a thread
// runs at its authors' common tier and is refused when they differ; an
// undefined or unattended tier is refused, never widened to read.
func TestTierForMapsAuthorsToOneTier(t *testing.T) {
	g, alice, bob, carol, dave, console := tierGateway(t)
	for name, tc := range map[string]struct {
		authors []TaskRequester
		want    capability.Tier
		refuse  string
	}{
		"unlisted is read":              {[]TaskRequester{bob}, capability.TierRead, ""},
		"listed runs at its tier":       {[]TaskRequester{alice}, "oncall", ""},
		"the console is read":           {[]TaskRequester{console}, capability.TierRead, ""},
		"same tier together":            {[]TaskRequester{bob, console}, capability.TierRead, ""},
		"different tiers are refused":   {[]TaskRequester{alice, bob}, "", "different tiers (oncall, read)"},
		"an unattended tier is refused": {[]TaskRequester{carol}, "", "tier nightly isn't defined"},
		"an undefined tier is refused":  {[]TaskRequester{dave}, "", "tier ghost isn't defined"},
		"no identity is refused":        {[]TaskRequester{{Backend: "gchat"}}, "", ""},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := g.tierFor(tc.authors, false)
			if tc.want != "" {
				if err != nil || got != tc.want {
					t.Fatalf("tierFor = %q, %v; want %q", got, err, tc.want)
				}
				return
			}
			notice := tierRefusal(t, err)
			if tc.refuse != "" && !strings.Contains(notice, tc.refuse) {
				t.Errorf("notice %q, want it to say %q", notice, tc.refuse)
			}
		})
	}
}

// The ceiling: a tier carrying a verb the install's AuthorityTier lacks,
// profile.run aside, is refused; the log names the verb, the room doesn't.
func TestTierForHoldsTheCeiling(t *testing.T) {
	g, alice, _, _, _, _ := tierGateway(t)
	if _, err := g.tierFor([]TaskRequester{alice}, false); err != nil {
		t.Fatalf("oncall under developer-team: %v", err)
	}
	var logs strings.Builder
	g.log = slog.New(slog.NewTextHandler(&logs, nil))
	g.cfg.AuthorityTier = capability.TierUnattended // holds profile.run alone
	notice := tierRefusal(t, func() error { _, err := g.tierFor([]TaskRequester{alice}, false); return err }())
	if want := fmt.Sprintf(noticeProfileTierCeiling, "oncall"); notice != want {
		t.Errorf("notice %q, want %q", notice, want)
	}
	if !strings.Contains(logs.String(), "task.execute") {
		t.Errorf("the log doesn't name the verb beyond the ceiling: %s", logs.String())
	}
}

// An author list that overflowed is refused once any membership exists, and
// passes when there is none (everyone is read then).
func TestTierForRefusesAnUnknownAuthorListWithMembership(t *testing.T) {
	g, _, bob, _, _, _ := tierGateway(t)
	if !strings.Contains(tierRefusal(t, func() error { _, err := g.tierFor([]TaskRequester{bob}, true); return err }()), "too many people") {
		t.Error("an overflowed author list with membership wasn't refused")
	}
	g.tierMembers = nil
	if tier, err := g.tierFor([]TaskRequester{bob}, true); err != nil || tier != capability.TierRead {
		t.Errorf("no membership: %q, %v; want read", tier, err)
	}
}

// A member is keyed as their turns are: a Google Chat email case-blind, an
// a2a-google email as the door's caller for it (a2aGoogleCaller), and an
// a2a-entra object id as the door's caller for it (a2aEntraCaller).
func TestTierMembersAreKeyedAsTheirTurns(t *testing.T) {
	ps := NewPseudonymizer([]byte("salt"))
	got := buildTierMembers([]TierMember{{gchatBackend, "Alice@X.com", "oncall"}, {a2aGoogleBackend, "bob@x.com", "oncall"}, {a2aEntraBackend, entraTestObjectID, "oncall"}}, ps)
	for _, r := range []TaskRequester{
		{Backend: gchatBackend, Subject: requesterSubject(ps, gchatBackend, "alice@x.com")},
		{Backend: a2aGoogleBackend, Subject: requesterSubject(ps, a2aGoogleBackend, a2aGoogleCaller("bob@x.com"))},
		{Backend: a2aEntraBackend, Subject: requesterSubject(ps, a2aEntraBackend, a2aEntraCaller(entraTestObjectID))},
	} {
		if got[r] != "oncall" {
			t.Errorf("%s's turn doesn't find its member: %v", r.Backend, got)
		}
	}
}
