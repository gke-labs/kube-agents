package capability

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

// The scope a profile grant is minted with, and a target inside and outside it.
const (
	profileGrantScope = Scope("project/P")
	targetInScope     = Scope("project/P/location/L/cluster/C")
	targetOutOfScope  = Scope("project/Q/location/L/cluster/C")
	installScope      = Scope("namespace/kubeagents-system")
)

func profileGrant(tier Tier, profile string) Entry {
	return Entry{Tier: tier, Scope: profileGrantScope, Delegate: podA, Profile: profile}
}

func TestPermitsProfile(t *testing.T) {
	read := profileGrant(TierRead, "tse")
	for _, tc := range []struct {
		name    string
		e       Entry
		profile string
		tier    Tier
		target  Scope
		refuse  string // "" = allowed
	}{
		{"the grant's own profile, tier and an in-scope target", read, "tse", TierRead, targetInScope, ""},
		{"an unattended grant runs its profile", profileGrant(TierUnattended, "triage"), "triage", TierUnattended, "project/P", ""},
		{"a target outside the grant's scope", read, "tse", TierRead, targetOutOfScope, "outside the capability's scope"},
		{"an empty target", read, "tse", TierRead, "", "not a well-formed scope"},
		{"a tier above the grant's", read, "tse", TierUnattended, targetInScope, "not the grant's tier"},
		{"an unknown tier", read, "tse", Tier("admin"), targetInScope, "not a known tier"},
		{"another profile", read, "cluster-agent", TierRead, targetInScope, "minted for another profile"},
		{"no profile in the request", read, "", TierRead, targetInScope, "not a DNS label"},
		{"a profile that is not a DNS label", read, "TSE.*", TierRead, targetInScope, "not a DNS label"},
		{"a grant minted for no profile (Validate refuses such an entry; this is the backstop)", profileGrant(TierRead, ""), "tse", TierRead, targetInScope, "not minted for a profile"},
		{"a tier whose set lacks profile.run", Entry{Tier: TierPlatform, Scope: profileGrantScope, Delegate: podA, Profile: "tse"}, "tse", TierPlatform, targetInScope, "does not permit the verb"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := PermitsProfile(tc.e, tc.profile, tc.tier, tc.target)
			if tc.refuse == "" {
				if err != nil {
					t.Fatalf("refused: %v", err)
				}
				return
			}
			mustRefuse(t, err, tc.refuse)
		})
	}
}

// The new tiers hold no write verb and do not nest with the original three:
// a grant at one of them never narrows to, or from, a developer-team chain.
func TestTheProfileTiers(t *testing.T) {
	for _, tier := range []Tier{TierRead, TierUnattended} {
		for _, v := range []Verb{VerbClusterMutate, VerbTopicPublishShared, VerbFleetRead, VerbTaskDelegate} {
			if tierVerbs[tier].has(v) {
				t.Errorf("tier %q holds %q; profile tiers propose, they do not write or delegate", tier, v)
			}
		}
	}
	if tierVerbs[TierUnattended].has(VerbTaskExecute) {
		t.Error("unattended holds task.execute; it only starts profile tasks")
	}
	if err := Narrows(Entry{Tier: TierPlatform, Scope: "project/P"}, Entry{Tier: TierRead, Scope: "project/P"}); err == nil {
		t.Error("a platform parent narrowed to read, but read holds profile.run and platform does not")
	}
	// Unattended's set is inside read's, but a human's read grant must never
	// become a trigger's unattended one: profile tiers narrow only to
	// themselves.
	err := Narrows(Entry{Tier: TierRead, Scope: "project/P", Profile: "tse"}, Entry{Tier: TierUnattended, Scope: "project/P", Profile: "tse"})
	mustRefuse(t, err, "a profile tier narrows only to itself")
	if err := Narrows(Entry{Tier: TierRead, Scope: "project/P", Profile: "tse"}, Entry{Tier: TierRead, Scope: targetInScope, Profile: "tse"}); err != nil {
		t.Errorf("read → read with a narrower scope should narrow: %v", err)
	}
}

func TestAProfileOnlyNarrows(t *testing.T) {
	parent := Entry{Tier: TierRead, Scope: "project/P", Profile: "tse"}
	for _, tc := range []struct {
		name  string
		child Entry
		ok    bool
	}{
		{"keeps the profile", Entry{Tier: TierRead, Scope: "project/P", Profile: "tse"}, true},
		{"drops it", Entry{Tier: TierRead, Scope: "project/P"}, false},
		{"changes it", Entry{Tier: TierRead, Scope: "project/P", Profile: "other"}, false},
	} {
		err := Narrows(parent, tc.child)
		if (err == nil) != tc.ok {
			t.Errorf("%s: Narrows = %v, want ok=%v", tc.name, err, tc.ok)
		}
		if !tc.ok {
			mustRefuse(t, err, "changes or drops the parent's profile")
		}
	}
	// A parent with no profile cannot hand one down: the first hop would be
	// choosing which profile its chain may run.
	mustRefuse(t, Narrows(Entry{Tier: TierRead, Scope: "project/P"}, parent), "changes or drops the parent's profile")
}

func TestAnEntrysProfileMustBeADNSLabel(t *testing.T) {
	for _, bad := range []string{"TSE", "tse.prod", "-tse", "tse-", "t*", strings.Repeat("a", 64)} {
		e := Entry{Tier: TierRead, Scope: "project/P", Delegate: podA, Profile: bad}
		mustRefuse(t, e.Validate(), "not a DNS label")
	}
	if err := (Entry{Tier: TierRead, Scope: "project/P", Delegate: podA, Profile: "tse-2"}).Validate(); err != nil {
		t.Errorf("a DNS-label profile should validate: %v", err)
	}
}

// Over the wire, through the walk: the dispatcher asks profile.run with
// CheckProfileRun and gets the verdict PermitsProfile gives.
func TestTheVerifierAnswersProfileRun(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), profileGrant(TierRead, "tse"))
	nc := serveVerifier(t, s)
	c := clientAs(t, nc, podA)
	ctx := context.Background()

	if err := c.CheckProfileRun(ctx, root, "tse", TierRead, targetInScope); err != nil {
		t.Fatalf("the grant's own profile at its tier for an in-scope target should be permitted: %v", err)
	}
	mustRefuse(t, c.CheckProfileRun(ctx, root, "cluster-agent", TierRead, targetInScope), "minted for another profile")
	mustRefuse(t, c.CheckProfileRun(ctx, root, "tse", TierUnattended, targetInScope), "not the grant's tier")
	mustRefuse(t, c.CheckProfileRun(ctx, root, "tse", TierRead, targetOutOfScope), "outside the capability's scope")
	mustRefuse(t, c.CheckProfileRun(ctx, root, "tse", TierRead, installScope), "outside the capability's scope")
	// Another principal asking about the same grant gets the walk's single answer.
	mustRefuse(t, clientAs(t, nc, podEvil).CheckProfileRun(ctx, root, "tse", TierRead, targetInScope), WalkRefused)
}

// A profile and tier on any other verb are refused rather than ignored, so a
// caller can never believe a field was checked when it was not.
func TestProfileFieldsOnAnotherVerbAreRefused(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), profileGrant(TierRead, "tse"))
	nc := serveVerifier(t, s)
	for _, req := range []Request{
		{Ref: root, Verb: VerbTaskExecute, Resource: targetInScope, Profile: "tse"},
		{Ref: root, Verb: VerbTaskExecute, Resource: targetInScope, Tier: TierRead},
	} {
		body, err := json.Marshal(req)
		if err != nil {
			t.Fatal(err)
		}
		got := rawAsk(t, nc, podA, body)
		if got.Allowed || !strings.Contains(got.Reason, "only read for profile.run") {
			t.Errorf("%+v: got %+v, want a refusal naming profile.run", req, got)
		}
	}
	// Control: the same grant permits task.execute when the fields are absent.
	if err := clientAs(t, nc, podA).Check(context.Background(), root, VerbTaskExecute, targetInScope); err != nil {
		t.Fatalf("task.execute without profile fields should be permitted at read: %v", err)
	}
}

// A profile tier always names its profile, and only profile tiers may. This is
// also what keeps A2A_AUTHORITY_TIER=read from booting a gateway that would
// mint profile-less read roots: its ceiling check runs Entry.Validate.
func TestAProfileIsSetExactlyAtAProfileTier(t *testing.T) {
	for _, tier := range []Tier{TierRead, TierUnattended} {
		mustRefuse(t, Entry{Tier: tier, Scope: "project/P", Delegate: podA}.Validate(), "names no profile")
	}
	for _, tier := range []Tier{TierDeveloperTeam, TierClusterAdmin, TierPlatform} {
		mustRefuse(t, Entry{Tier: tier, Scope: "project/P", Delegate: podA, Profile: "tse"}.Validate(), "not a profile tier")
		if err := (Entry{Tier: tier, Scope: "project/P", Delegate: podA}).Validate(); err != nil {
			t.Errorf("%s with no profile should validate: %v", tier, err)
		}
	}
}

// The scenario the adversarial pass ran end to end: a read root for "tse", and
// a hop that relabels it unattended. The hop is refused at the walk, so the
// verifier never gets as far as comparing tiers.
func TestAHopCannotRelabelReadAsUnattended(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), profileGrant(TierRead, "tse"))
	hop := s.write(t, hopKey(t, podA, 0), Entry{Tier: TierUnattended, Scope: profileGrantScope, Delegate: podB, Profile: "tse", Parent: &root})
	nc := serveVerifier(t, s)
	mustRefuse(t, clientAs(t, nc, podB).CheckProfileRun(context.Background(), hop, "tse", TierUnattended, targetInScope), WalkRefused)
}

// A hop under a profile-less read root cannot pick a profile, and the root
// itself does not validate.
func TestAHopCannotChooseAProfile(t *testing.T) {
	s := newStore()
	root := s.write(t, rootKey(t, gwTask), Entry{Tier: TierRead, Scope: profileGrantScope, Delegate: podA})
	hop := s.write(t, hopKey(t, podA, 0), Entry{Tier: TierRead, Scope: profileGrantScope, Delegate: podB, Profile: "anything", Parent: &root})
	nc := serveVerifier(t, s)
	mustRefuse(t, clientAs(t, nc, podB).CheckProfileRun(context.Background(), hop, "anything", TierRead, targetInScope), WalkRefused)
}
