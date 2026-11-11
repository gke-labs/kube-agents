package dispatch

import (
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A profile's own unattended-mode tier (a Tier resource, #3659) is on the
// unattended side for that profile only; the built-in stays unattended for all.
func TestAProfilesOwnUnattendedTierIsUnattendedForItAlone(t *testing.T) {
	c := DefaultConfig()
	nightly := lib.DispatchProfile{Name: "tse", Tiers: []string{"read", "nightly"}, UnattendedTier: "nightly"}
	other := lib.DispatchProfile{Name: "aud", Tiers: []string{"read", "nightly"}}
	for _, tc := range []struct {
		prof lib.DispatchProfile
		tier string
		want bool
	}{
		{nightly, "nightly", true},
		{nightly, "read", false},
		{nightly, "unattended", true},
		{other, "nightly", false},
		{other, "unattended", true},
	} {
		if got := c.unattendedFor(tc.prof, tc.tier); got != tc.want {
			t.Errorf("unattendedFor(%s, %s) = %v, want %v", tc.prof.Name, tc.tier, got, tc.want)
		}
	}
}

// A task at a tier its profile no longer lists may still have a Job: a custom
// unattended-mode tier dropped from the profile takes UnattendedTier with
// it, so its mode can't be told and the Job must still be deleted (#3659). A
// listed interactive tier never has one.
func TestATierDroppedFromItsProfileMayStillHaveAJob(t *testing.T) {
	c := DefaultConfig()
	dropped := lib.DispatchProfile{Name: "tse", Tiers: []string{"read"}}
	for _, tc := range []struct {
		tier string
		want bool
	}{
		{"nightly", true},
		{"read", false},
		{"unattended", true},
	} {
		if got := c.jobPossible(dropped, tc.tier); got != tc.want {
			t.Errorf("jobPossible(tse, %s) = %v, want %v", tc.tier, got, tc.want)
		}
	}
}
