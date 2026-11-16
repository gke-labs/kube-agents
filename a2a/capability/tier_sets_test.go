package capability

import (
	"strings"
	"testing"
)

// The lattice tierVerbs replaced (#3659), kept here as the oracle. Every
// decision the rank made must come out the same under the sets, for the three
// tiers that existed, so the change to explicit sets alters no capability in
// flight.
var (
	retiredTierRank = map[Tier]int{
		TierDeveloperTeam: 0,
		TierClusterAdmin:  1,
		TierPlatform:      2,
	}
	retiredVerbTier = map[Verb]Tier{
		VerbTaskExecute:        TierDeveloperTeam,
		VerbTaskDelegate:       TierDeveloperTeam,
		VerbTopicPublishShared: TierClusterAdmin,
		VerbClusterMutate:      TierClusterAdmin,
		VerbFleetRead:          TierPlatform,
	}
)

// retiredPermits is the old Permits's tier decision: "" when the rank allowed
// the verb, otherwise the substring of the rule that refused it, in the order
// the old code checked.
func retiredPermits(t Tier, v Verb) string {
	need, ok := retiredVerbTier[v]
	if !ok {
		return "verb is not in the table"
	}
	have, ok := retiredTierRank[t]
	if !ok {
		return "capability carries a tier that is not a known tier"
	}
	if have < retiredTierRank[need] {
		return "does not permit the verb"
	}
	return ""
}

func retiredNarrows(parent, child Tier) string {
	pr, ok := retiredTierRank[parent]
	if !ok {
		return "parent carries a tier that is not a known tier"
	}
	cr, ok := retiredTierRank[child]
	if !ok {
		return "child carries a tier that is not a known tier"
	}
	if cr > pr {
		return "child widens the tier"
	}
	return ""
}

// sameDecision reports whether err is the decision want describes: nil for
// "", otherwise a refusal whose text contains want.
func sameDecision(err error, want string) bool {
	if want == "" {
		return err == nil
	}
	return err != nil && strings.Contains(err.Error(), want)
}

// The tiers and verbs the comparison walks: every one the old tables named,
// plus an unknown of each, which both versions must refuse.
var (
	oracleTiers = []Tier{TierDeveloperTeam, TierClusterAdmin, TierPlatform, Tier("root"), Tier("")}
	oracleVerbs = []Verb{
		VerbTaskExecute, VerbTaskDelegate, VerbTopicPublishShared, VerbClusterMutate, VerbFleetRead,
		Verb("cluster.read"), Verb(""),
	}
)

func TestTierSetsMatchTheRetiredRank(t *testing.T) {
	const scope = Scope("project/P/cluster/C")
	for _, tier := range oracleTiers {
		for _, verb := range oracleVerbs {
			want := retiredPermits(tier, verb)
			err := Permits(Entry{Tier: tier, Scope: scope, Delegate: "d"}, verb, scope)
			if !sameDecision(err, want) {
				t.Errorf("Permits(tier %q, verb %q) = %v, the retired rank said %q", tier, verb, err, want)
			}
		}
	}
	for _, parent := range oracleTiers {
		for _, child := range oracleTiers {
			want := retiredNarrows(parent, child)
			err := Narrows(Entry{Tier: parent, Scope: scope}, Entry{Tier: child, Scope: scope})
			if !sameDecision(err, want) {
				t.Errorf("Narrows(parent %q, child %q) = %v, the retired rank said %q", parent, child, err, want)
			}
		}
	}
}

// Every verb the retired table knew is still in some tier's set, and every verb
// added since is named here on purpose: a verb added to the table without
// being listed fails, so a widening never slips in unnoticed.
var addedSinceTheRank = map[Verb]string{
	VerbProfileRun: "#3659: the dispatcher's question to the verifier",
}

func TestTierSetsKeepTheVocabulary(t *testing.T) {
	have := map[Verb]bool{}
	for _, v := range Verbs() {
		have[v] = true
		_, retired := retiredVerbTier[v]
		_, added := addedSinceTheRank[v]
		if !retired && !added {
			t.Errorf("Verbs() names %q, which neither the retired table nor addedSinceTheRank lists", v)
		}
	}
	for v := range retiredVerbTier {
		if !have[v] {
			t.Errorf("verb %q from the retired table is in no tier's set", v)
		}
	}
	for v := range addedSinceTheRank {
		if !have[v] {
			t.Errorf("addedSinceTheRank lists %q, but no tier's set names it", v)
		}
	}
}

// A tier is only a tier if the table names it: an entry carrying an unknown
// one is refused by Validate, not given an empty set that would pass the
// subset check against any parent.
func TestAnUnknownTierIsRefusedNotEmpty(t *testing.T) {
	err := Entry{Tier: Tier("root"), Scope: "project/P", Delegate: "d"}.Validate()
	if err == nil || !strings.Contains(err.Error(), "is not a known tier") {
		t.Fatalf("Validate with an unknown tier = %v, want a refusal naming an unknown tier", err)
	}
	// The empty set is the dangerous one: it is a subset of everything.
	if !(verbSet{}).subsetOf(tierVerbs[TierDeveloperTeam]) {
		t.Fatal("the empty set should be a subset of any set; the guard above is what stops it being used")
	}
	if err := Narrows(Entry{Tier: TierDeveloperTeam, Scope: "project/P"}, Entry{Tier: Tier("root"), Scope: "project/P"}); err == nil {
		t.Fatal("Narrows let a child with an unknown tier through under a developer-team parent")
	}
}

// The three original tiers must keep nesting whatever verbs are added later.
// 09's worked example delegates platform → cluster-admin → developer-team,
// and every chain in flight does the same: if a new verb went into
// developer-team's set but not cluster-admin's, each such hop would start
// refusing as "child widens the tier", with no other test noticing. This one
// is independent of the vocabulary, so it outlives the oracle above.
func TestTheOriginalTiersStillNest(t *testing.T) {
	chain := []Tier{TierDeveloperTeam, TierClusterAdmin, TierPlatform}
	for i := 0; i+1 < len(chain); i++ {
		inner, outer := chain[i], chain[i+1]
		if !tierVerbs[inner].subsetOf(tierVerbs[outer]) {
			t.Errorf("%s's verb set is no longer inside %s's: every %s → %s delegation would be refused", inner, outer, outer, inner)
		}
	}
}

// No tier may have an empty set: it would be a subset of every other and so
// narrow under any parent.
func TestTierSetsAreNonEmpty(t *testing.T) {
	for tier, set := range tierVerbs {
		if len(set) == 0 {
			t.Errorf("tier %q has an empty verb set", tier)
		}
	}
}
