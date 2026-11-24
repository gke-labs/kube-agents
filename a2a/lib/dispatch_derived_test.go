package lib

import (
	"encoding/json"
	"os"
	"slices"
	"testing"
	"time"
)

func durableNames(ds []DispatchDurable) []string {
	var out []string
	for _, d := range ds {
		out = append(out, d.Name)
	}
	slices.Sort(out)
	return out
}

// The durables are derived from the profile's live and draining tiers, one
// per (tier, kind), so the operator no longer has to write them (#5479).
func TestTheDurablesAreDerivedFromTheTiers(t *testing.T) {
	until := time.Date(2026, 11, 12, 0, 0, 0, 0, time.UTC)
	p := DispatchProfile{Name: "tse", Tiers: []string{"read", "unattended"},
		DrainingTiers: []DrainingTier{{Tier: "ops", Mode: "interactive", Until: until}}}
	want := []string{
		"profile-tse-ops-child", "profile-tse-ops-top",
		"profile-tse-read-child", "profile-tse-read-top",
		"profile-tse-unattended-child", "profile-tse-unattended-top",
	}
	if got := durableNames(p.DerivedDurables()); !slices.Equal(got, want) {
		t.Errorf("derived %v, want %v", got, want)
	}
	for _, d := range p.DerivedDurables() {
		if d.Name != ProfileDurableName(p.Name, d.Tier, d.Kind) {
			t.Errorf("durable %+v is not named for its tier and kind", d)
		}
	}
	draining := DispatchProfile{Name: "old", Tiers: []string{"read"}, DrainingUntil: until}
	if got := draining.DerivedDurables(); len(got) != 0 {
		t.Errorf("a draining profile derives %v; want none", got)
	}
}

// The durables the operator creates (its fixture, written by the operator's
// TestTheDispatchDurablesFixture) are the ones this package derives: the
// operator can't import lib, so this is what holds the two derivations equal.
func TestTheOperatorsDurablesAreTheDerivedOnes(t *testing.T) {
	raw, err := os.ReadFile("testdata/rendered-dispatch-durables.json")
	if err != nil {
		t.Fatalf("%v\nRegenerate with: go test ./internal/controller/ -run 'Fixture' -update (in k8s-operator)", err)
	}
	var entries []struct {
		Name          string    `json:"name"`
		Tiers         []string  `json:"tiers"`
		DrainingTiers []string  `json:"drainingTiers"`
		DrainingUntil time.Time `json:"drainingUntil"`
		Durables      []string  `json:"durables"`
	}
	if err := json.Unmarshal(raw, &entries); err != nil {
		t.Fatal(err)
	}
	if len(entries) < 4 {
		t.Fatalf("the fixture has %d profiles; want the four cases", len(entries))
	}
	sawDrainingTier, sawDraining := false, false
	for _, e := range entries {
		p := DispatchProfile{Name: e.Name, Tiers: e.Tiers, DrainingUntil: e.DrainingUntil}
		for _, tier := range e.DrainingTiers {
			p.DrainingTiers = append(p.DrainingTiers, DrainingTier{Tier: tier, Until: time.Unix(1, 0)})
			sawDrainingTier = true
		}
		sawDraining = sawDraining || p.Draining()
		want := append([]string{}, e.Durables...)
		slices.Sort(want)
		if got := durableNames(p.DerivedDurables()); !slices.Equal(got, want) && !(len(got) == 0 && len(want) == 0) {
			t.Errorf("profile %s: derived %v, the operator creates %v", e.Name, got, want)
		}
	}
	if !sawDrainingTier || !sawDraining {
		t.Errorf("the fixture covers a draining tier %v and a draining profile %v; want both", sawDrainingTier, sawDraining)
	}
}
