package capability

import (
	"fmt"
	"slices"
	"sync/atomic"
)

// The tier table as data (#3659): an install defines profile tiers with the
// Tier custom resource, and the verifier, the gateway and the callout learn
// them through LoadTiers. What a tier may do still follows from code, not
// from the resource: a tier's mode picks one of two verb sets, so a resource
// can't name a verb, and the vocabulary stays closed.
//
// What stays compiled in, whatever is loaded:
//   - the three legacy tiers (platform, cluster-admin, developer-team), whose
//     sets nest and which no resource may redefine;
//   - the built-in profile tiers read and unattended, at their sets in
//     tierVerbs. A resource with one of their names changes nothing; the
//     chart's defaults name them only so an install can see them.
//
// So a loaded definition can only add a profile tier, at one of the two
// sets the built-ins already hold. A missing, empty or refused source leaves
// exactly the compiled-in table: it never falls back to anything wider.

// TierMode is how a profile tier runs, which picks its verb set.
type TierMode string

const (
	// TierModeInteractive is a person's tier: task.execute and profile.run,
	// read's set.
	TierModeInteractive TierMode = "interactive"
	// TierModeUnattended is a trigger's tier: profile.run alone,
	// unattended's set.
	TierModeUnattended TierMode = "unattended"
)

// modeVerbs is the verb set each mode gives, the built-in profile tiers'.
var modeVerbs = map[TierMode]Tier{
	TierModeInteractive: TierRead,
	TierModeUnattended:  TierUnattended,
}

// TierDefinition is one Tier resource as the table reads it.
type TierDefinition struct {
	Name string
	Mode TierMode
}

// tierTable is one consistent view of the tiers: each tier's verb set, and
// which tiers are profile tiers.
type tierTable struct {
	verbs   map[Tier]verbSet
	profile map[Tier]bool
}

var activeTiers atomic.Pointer[tierTable]

func init() { activeTiers.Store(builtinTierTable()) }

func builtinTierTable() *tierTable {
	t := &tierTable{verbs: make(map[Tier]verbSet, len(tierVerbs)), profile: make(map[Tier]bool, len(profileTiers))}
	for tier, set := range tierVerbs {
		t.verbs[tier] = set
	}
	for tier := range profileTiers {
		t.profile[tier] = true
	}
	return t
}

// tiers is the table in force. Readers take it once per decision, so one
// decision never sees two tables.
func tiers() *tierTable { return activeTiers.Load() }

// LoadTiers installs the compiled-in table plus defs, and returns why each
// definition it skipped was skipped. A skipped definition adds nothing; the
// rest still load. Calling it with nil restores the compiled-in table.
//
// A definition is skipped when its name isn't a DNS label, names a
// compiled-in tier, or repeats an earlier definition, or when its mode is
// neither interactive nor unattended.
func LoadTiers(defs []TierDefinition) []string {
	t := builtinTierTable()
	var skipped []string
	seen := map[string]bool{}
	for _, d := range defs {
		name := Tier(d.Name)
		switch {
		case !validProfileName(d.Name):
			skipped = append(skipped, fmt.Sprintf("tier %q: the name is not a DNS label", d.Name))
		case slices.Contains([]Tier{TierPlatform, TierClusterAdmin, TierDeveloperTeam}, name):
			skipped = append(skipped, fmt.Sprintf("tier %q: a legacy tier is compiled in and can't be redefined", d.Name))
		case profileTiers[name]:
			if modeVerbs[d.Mode] != name {
				skipped = append(skipped, fmt.Sprintf("tier %q: the built-in tier's set is compiled in; mode %q changes nothing", d.Name, d.Mode))
			}
		case seen[d.Name]:
			skipped = append(skipped, fmt.Sprintf("tier %q: defined twice; the first stands", d.Name))
		default:
			base, ok := modeVerbs[d.Mode]
			if !ok {
				skipped = append(skipped, fmt.Sprintf("tier %q: mode %q is not interactive or unattended", d.Name, d.Mode))
				break
			}
			t.verbs[name] = tierVerbs[base]
			t.profile[name] = true
		}
		seen[d.Name] = true
	}
	activeTiers.Store(t)
	return skipped
}

// LoadedTiers is the profile tiers in force, sorted: the built-ins and every
// loaded definition.
func LoadedTiers() []Tier {
	t := tiers()
	out := make([]Tier, 0, len(t.profile))
	for tier := range t.profile {
		out = append(out, tier)
	}
	slices.Sort(out)
	return out
}
