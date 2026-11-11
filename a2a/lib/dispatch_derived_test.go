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

// For one release the dispatcher reads both shapes: an entry with no
// durables, and one that still lists them. A listed set must be exactly the
// derived one; a mismatch refuses the config rather than either side
// being silently preferred.
func TestListedDurablesMustEqualTheDerivedOnes(t *testing.T) {
	without := validDispatchConfig()
	without.Profiles = append([]DispatchProfile(nil), without.Profiles...)
	without.Profiles[0].Durables = nil
	if err := without.Validate(); err != nil {
		t.Errorf("a live profile with no listed durables: %v", err)
	}
	if err := validDispatchConfig().Validate(); err != nil {
		t.Errorf("a profile listing exactly the derived durables: %v", err)
	}
	for name, edit := range map[string]func(*DispatchProfile){
		"one missing": func(p *DispatchProfile) { p.Durables = p.Durables[:1] },
		"one extra": func(p *DispatchProfile) {
			p.Durables = append(p.Durables, DispatchDurable{Tier: "unattended", Kind: SubmitKindTop, Name: ProfileDurableName(p.Name, "unattended", SubmitKindTop)})
		},
		"a draining profile listing one": func(p *DispatchProfile) {
			p.DrainingUntil = time.Date(2026, 11, 12, 0, 0, 0, 0, time.UTC)
		},
	} {
		c := validDispatchConfig()
		c.Profiles = append([]DispatchProfile(nil), c.Profiles...)
		edit(&c.Profiles[0])
		if err := c.Validate(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	draining := validDispatchConfig()
	draining.Profiles = append([]DispatchProfile(nil), draining.Profiles...)
	draining.Profiles[0].Durables = nil
	draining.Profiles[0].DrainingUntil = time.Date(2026, 11, 12, 0, 0, 0, 0, time.UTC)
	if err := draining.Validate(); err != nil {
		t.Errorf("a draining profile with no durables: %v", err)
	}
}

// A Job template reference may carry only its hash: the name is the
// profile's own param's, which the dispatcher refuses to look past anyway.
func TestAJobTemplateRefMayCarryOnlyItsHash(t *testing.T) {
	c := validDispatchConfig()
	c.Profiles = append([]DispatchProfile(nil), c.Profiles...)
	c.Profiles[0].JobTemplateRef = &DispatchJobTemplateRef{Hash: "abc123"}
	if err := c.Validate(); err != nil {
		t.Fatalf("a hash-only ref: %v", err)
	}
	if got := c.Profiles[0].JobTemplateRef.NameFor("tse"); got != DispatchJobTemplateName("tse") {
		t.Errorf("a hash-only ref names %q; want %q", got, DispatchJobTemplateName("tse"))
	}
	named := &DispatchJobTemplateRef{Name: "kube-agents-dispatch-job-profile-tse", Hash: "abc123"}
	if got := named.NameFor("tse"); got != named.Name {
		t.Errorf("a named ref names %q; want %q", got, named.Name)
	}
	c.Profiles[0].JobTemplateRef = &DispatchJobTemplateRef{}
	if err := c.Validate(); err == nil {
		t.Error("a ref with no hash was accepted")
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
