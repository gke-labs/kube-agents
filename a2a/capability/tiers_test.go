package capability

import (
	"reflect"
	"strings"
	"testing"
)

func restoreTiers(t *testing.T) {
	t.Helper()
	t.Cleanup(func() { LoadTiers(nil) })
}

// The chart's defaults, read and unattended at their modes, load to exactly
// the compiled-in table: making tiers data changes no decision.
func TestTheDefaultTiersReproduceTheCompiledInTable(t *testing.T) {
	restoreTiers(t)
	before := builtinTierTable()
	if skipped := LoadTiers([]TierDefinition{{Name: "read", Mode: TierModeInteractive}, {Name: "unattended", Mode: TierModeUnattended}}); len(skipped) != 0 {
		t.Fatalf("the defaults were skipped: %v", skipped)
	}
	if !reflect.DeepEqual(tiers().verbs, before.verbs) || !reflect.DeepEqual(tiers().profile, before.profile) {
		t.Fatalf("the defaults changed the table:\n%v\nwant\n%v", tiers(), before)
	}
}

// A new tier gets its mode's set and is a profile tier: it must name a
// profile, permits what read or unattended permits, and narrows only to
// itself.
func TestALoadedTierTakesItsModesSet(t *testing.T) {
	restoreTiers(t)
	LoadTiers([]TierDefinition{{Name: "oncall", Mode: TierModeInteractive}, {Name: "nightly", Mode: TierModeUnattended}})
	root := Scope("project/p")
	oncall := Entry{Tier: "oncall", Scope: root, Delegate: "dispatcher", Profile: "tse"}
	nightly := Entry{Tier: "nightly", Scope: root, Delegate: "dispatcher", Profile: "tse"}
	for _, e := range []Entry{oncall, nightly} {
		if err := e.Validate(); err != nil {
			t.Fatalf("%s: %v", e.Tier, err)
		}
	}
	if err := Permits(oncall, VerbTaskExecute, root); err != nil {
		t.Errorf("an interactive tier refused task.execute: %v", err)
	}
	if err := Permits(nightly, VerbTaskExecute, root); err == nil {
		t.Error("an unattended tier permitted task.execute")
	}
	if err := PermitsProfile(nightly, "tse", "nightly", root); err != nil {
		t.Errorf("profile.run at the loaded tier: %v", err)
	}
	noProfile := oncall
	noProfile.Profile = ""
	if err := noProfile.Validate(); err == nil {
		t.Error("a loaded tier's entry with no profile validated")
	}
	asRead := oncall
	asRead.Tier = TierRead
	if err := Narrows(oncall, asRead); err == nil {
		t.Error("a loaded tier narrowed to read: a profile tier narrows only to itself")
	}
	if err := Narrows(asRead, oncall); err == nil {
		t.Error("read narrowed to a loaded tier")
	}
}

// Nothing a definition says can widen a compiled-in tier, add a verb, or
// leave anything wider than the compiled-in table behind.
func TestADefinitionCannotWidenTheCompiledInTiers(t *testing.T) {
	restoreTiers(t)
	before := builtinTierTable()
	skipped := LoadTiers([]TierDefinition{
		{Name: "unattended", Mode: TierModeInteractive}, // would widen unattended
		{Name: "read", Mode: TierModeUnattended},
		{Name: "platform", Mode: TierModeInteractive},
		{Name: "developer-team", Mode: TierModeUnattended},
		{Name: "Bad.Name", Mode: TierModeInteractive},
		{Name: "", Mode: TierModeInteractive},
		{Name: "admin", Mode: "write"},
		{Name: "twice", Mode: TierModeUnattended},
		{Name: "twice", Mode: TierModeInteractive},
	})
	if len(skipped) != 8 {
		t.Errorf("skipped %d, want 8: %v", len(skipped), skipped)
	}
	for tier, set := range before.verbs {
		if !reflect.DeepEqual(tiers().verbs[tier], set) {
			t.Errorf("compiled-in tier %s changed to %v", tier, tiers().verbs[tier])
		}
	}
	if !reflect.DeepEqual(tiers().verbs["twice"], tierVerbs[TierUnattended]) {
		t.Errorf("the first definition of twice should stand, at unattended's set: %v", tiers().verbs["twice"])
	}
	if _, ok := tiers().verbs["admin"]; ok {
		t.Error("a tier with an unknown mode loaded")
	}
	if got := strings.Join(skipped, "\n"); !strings.Contains(got, "compiled in") {
		t.Errorf("reasons don't say why: %s", got)
	}
	if !reflect.DeepEqual(Verbs(), func() []Verb { LoadTiers(nil); return Verbs() }()) {
		t.Error("loading tiers changed the verb vocabulary")
	}
}

// Loading nil, or an empty list, restores exactly the compiled-in table.
func TestNoSourceIsTheCompiledInTable(t *testing.T) {
	restoreTiers(t)
	LoadTiers([]TierDefinition{{Name: "extra", Mode: TierModeInteractive}})
	for _, defs := range [][]TierDefinition{nil, {}} {
		LoadTiers(defs)
		if !reflect.DeepEqual(tiers().verbs, builtinTierTable().verbs) {
			t.Fatalf("LoadTiers(%v) left %v", defs, tiers().verbs)
		}
	}
	if err := (Entry{Tier: "extra", Scope: "project/p", Delegate: "d", Profile: "x"}).Validate(); err == nil {
		t.Error("a tier from an earlier load survived a reload without it")
	}
}
