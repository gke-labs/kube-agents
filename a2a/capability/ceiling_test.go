package capability

import "testing"

// Tier 2c (#3659): a profile tier fits under the install's AuthorityTier when
// everything it carries besides profile.run is in the ceiling's set.
func TestOutsideCeilingLeavesProfileRunOut(t *testing.T) {
	if got := OutsideCeiling(TierRead, TierDeveloperTeam); len(got) != 0 {
		t.Errorf("read under developer-team: outside %v, want none (profile.run is left out)", got)
	}
	if got := OutsideCeiling(TierUnattended, TierDeveloperTeam); len(got) != 0 {
		t.Errorf("unattended under developer-team: outside %v, want none", got)
	}
	if got := OutsideCeiling(TierPlatform, TierDeveloperTeam); len(got) == 0 {
		t.Error("platform under developer-team: nothing outside, want its extra verbs")
	}
	if got := OutsideCeiling(Tier("nope"), TierDeveloperTeam); len(got) == 0 {
		t.Error("an unknown tier fit under the ceiling")
	}
}

func TestIsInteractiveProfileTier(t *testing.T) {
	t.Cleanup(func() { LoadTiers(nil) })
	LoadTiers([]TierDefinition{{Name: "oncall", Mode: TierModeInteractive}, {Name: "nightly", Mode: TierModeUnattended}})
	for tier, want := range map[Tier]bool{TierRead: true, "oncall": true, TierUnattended: false, "nightly": false, TierDeveloperTeam: false, "nope": false} {
		if got := IsInteractiveProfileTier(tier); got != want {
			t.Errorf("IsInteractiveProfileTier(%s) = %v, want %v", tier, got, want)
		}
	}
}
