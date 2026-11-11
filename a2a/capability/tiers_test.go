package capability

import (
	"context"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
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

func writeTiers(t *testing.T, path, body string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// The operator's file loads its tiers; a missing, unparseable or unknown-keyed
// file installs exactly the compiled-in table, never a wider one.
func TestTheTiersFileLoadsAndABadOneFallsBackToTheBuiltIns(t *testing.T) {
	restoreTiers(t)
	path := filepath.Join(t.TempDir(), "tiers.json")
	writeTiers(t, path, `{"tiers":[{"name":"read","mode":"interactive"},{"name":"oncall","mode":"interactive"}]}`)
	if _, err := LoadTierFile(path); err != nil || !IsProfileTier("oncall") {
		t.Fatalf("a good file: err %v, oncall loaded %v", err, IsProfileTier("oncall"))
	}
	for name, body := range map[string]string{
		"not json":    `{"tiers":[`,
		"unknown key": `{"tiers":[{"name":"x","mode":"interactive","verbs":["cluster.mutate"]}]}`,
		"wrong shape": `{"tiers":"oncall"}`,
	} {
		LoadTiers([]TierDefinition{{Name: "oncall", Mode: TierModeInteractive}})
		writeTiers(t, path, body)
		if _, err := LoadTierFile(path); err == nil {
			t.Errorf("%s: no error", name)
		}
		if IsProfileTier("oncall") || !reflect.DeepEqual(tiers().verbs, builtinTierTable().verbs) {
			t.Errorf("%s: the table is not the compiled-in one", name)
		}
	}
	LoadTiers([]TierDefinition{{Name: "oncall", Mode: TierModeInteractive}})
	if _, err := LoadTierFile(filepath.Join(t.TempDir(), "missing")); err == nil || IsProfileTier("oncall") {
		t.Error("a missing file kept a loaded tier")
	}
}

// The watch picks up an edit, as the kubelet's ConfigMap update makes one.
func TestTheTiersFileWatchPicksUpAnEdit(t *testing.T) {
	restoreTiers(t)
	path := filepath.Join(t.TempDir(), "tiers.json")
	writeTiers(t, path, `{"tiers":[]}`)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go WatchTierFile(ctx, path, 10*time.Millisecond, slog.New(slog.NewTextHandler(io.Discard, nil)))
	writeTiers(t, path, `{"tiers":[{"name":"nightly","mode":"unattended"}]}`)
	deadline := time.Now().Add(5 * time.Second)
	for !IsProfileTier("nightly") {
		if time.Now().After(deadline) {
			t.Fatal("the watch never loaded the edit")
		}
		time.Sleep(10 * time.Millisecond)
	}
	cancel()
}

func TestStartTierFileIsANoOpWhenUnset(t *testing.T) {
	restoreTiers(t)
	LoadTiers([]TierDefinition{{Name: "kept", Mode: TierModeInteractive}})
	StartTierFile(context.Background(), func(string) string { return "" }, nil)
	if !IsProfileTier("kept") {
		t.Error("an unset env changed the table")
	}
}
