package capability

// Verb is an action a broker is about to take on behalf of a request.
//
// The entry does not carry a list of these, deliberately: 09's shape is
// {tier, scope, delegate} and A4 owes 09 the action-list amendment along with
// the per-call lookup. So a verb is permitted by the *tier* the entry carries,
// through tierVerbs below, and that mapping is code rather than data on the
// wire. The consequence worth stating plainly: adding a verb to a tier's set
// widens what every existing capability at that tier already permits,
// retroactively and without a rewrite. That is the property the action list
// would remove, and it is A4's to remove. The same holds for narrowing:
// Narrows compares the sets, so changing a tier's set also changes which
// stored chains still resolve, not only what Permits allows. A tier must never
// have an empty set; that set is a subset of every other, so the tier would sit
// under any parent (TestTierSetsAreNonEmpty).
type Verb string

const (
	// VerbTaskExecute is an executor picking up a submitted task. The
	// narrowest thing any agent does, and the one the worker adapter gates.
	VerbTaskExecute Verb = "task.execute"

	// VerbTaskDelegate is a broker dispatching work onward to another
	// principal, which is also what writing a child entry is for.
	VerbTaskDelegate Verb = "task.delegate"

	// VerbTopicPublishShared is a publish onto the shared topic plane,
	// which every tier can read — so it crosses scopes by construction and
	// is not a developer-team action.
	VerbTopicPublishShared Verb = "topics.publish.shared"

	// VerbClusterMutate is any write against a cluster's control plane.
	VerbClusterMutate Verb = "cluster.mutate"

	// VerbFleetRead is a read that spans clusters.
	VerbFleetRead Verb = "fleet.read"

	// VerbProfileRun is the dispatcher starting a profile task: does this
	// grant permit running profile P at tier T for this target. It is the
	// one verb whose request carries a profile and a tier, and it is
	// answered by PermitsProfile, not Permits alone (#3659).
	VerbProfileRun Verb = "profile.run"
)

// tierVerbs is each tier's verb set: the tier permits exactly these verbs and
// nothing else. It replaced a ranked lattice (#3659) because the tiers to come
// are not a rank: one that may only propose is not "below" one that may read
// logs, it simply holds a different set. The three tiers here still nest,
// developer-team inside cluster-admin inside platform, so every decision the
// rank used to make comes out the same (TestTierSetsMatchTheRetiredRank pins
// each pair).
//
// The table is the allowlist twice over. A tier absent from it is not a tier:
// an entry carrying one is refused rather than given an empty set, because an
// empty set would still pass the subset check against any parent. And a verb in
// no tier's set is refused as unknown, because a caller and a verifier that
// disagree about the vocabulary is not a condition to resolve in the permissive
// direction.
var tierVerbs = map[Tier]verbSet{
	TierDeveloperTeam: newVerbSet(VerbTaskExecute, VerbTaskDelegate),
	TierClusterAdmin:  newVerbSet(VerbTaskExecute, VerbTaskDelegate, VerbTopicPublishShared, VerbClusterMutate),
	TierPlatform: newVerbSet(
		VerbTaskExecute, VerbTaskDelegate, VerbTopicPublishShared, VerbClusterMutate, VerbFleetRead,
	),
	// The profile tiers. Read runs interactive profile tasks; Unattended
	// is a trigger's principal and only ever starts profile tasks, so it
	// holds neither task.execute nor task.delegate. Neither holds a write
	// verb: in this round a profile proposes changes, it does not make
	// them. profile.run is in neither of the original three sets, so
	// their nesting is untouched.
	TierRead:       newVerbSet(VerbTaskExecute, VerbProfileRun),
	TierUnattended: newVerbSet(VerbProfileRun),
}

// verbSet is a tier's verbs. A map rather than a slice so membership is one
// lookup and a duplicate in the table cannot change a decision.
type verbSet map[Verb]struct{}

func newVerbSet(verbs ...Verb) verbSet {
	s := make(verbSet, len(verbs))
	for _, v := range verbs {
		s[v] = struct{}{}
	}
	return s
}

func (s verbSet) has(v Verb) bool {
	_, ok := s[v]
	return ok
}

// subsetOf reports whether every verb in s is also in other. It is what
// "narrower" means between two tiers: a child may carry a tier whose set is
// inside its parent's, and nothing else.
func (s verbSet) subsetOf(other verbSet) bool {
	for v := range s {
		if !other.has(v) {
			return false
		}
	}
	return true
}

// knownVerb reports whether any tier's set names v.
func knownVerb(v Verb) bool {
	for _, set := range tierVerbs {
		if set.has(v) {
			return true
		}
	}
	return false
}

// Permits answers the question the verifier exists to answer: does this
// capability permit verb v on resource r. It is applied to the *leaf* of a
// resolved chain, after the chain itself has been walked — a leaf is never
// wider than its root, so checking the leaf checks the chain.
func Permits(e Entry, v Verb, r Scope) error {
	if !knownVerb(v) {
		return refuse("verb is not in the table")
	}
	set, ok := tierVerbs[e.Tier]
	if !ok {
		return refuse("capability carries a tier that is not a known tier")
	}
	if !set.has(v) {
		return refuse("capability's tier does not permit the verb: its verb set does not include it")
	}
	if err := r.validate(); err != nil {
		return refuse("the resource is not a well-formed scope")
	}
	if !e.Scope.Contains(r) {
		return refuse("resource is outside the capability's scope")
	}
	return nil
}

// PermitsProfile answers profile.run: does grant e permit running profile at
// tier for target r. On top of Permits (the verb is in the tier's set and the
// target is in scope), the request's tier must be the grant's own tier and the
// request's profile the one the grant was minted for. The dispatcher never
// reads either out of the grant; it states what it means to run, and the
// verifier says yes or no.
func PermitsProfile(e Entry, profile string, tier Tier, r Scope) error {
	if err := Permits(e, VerbProfileRun, r); err != nil {
		return err
	}
	if _, ok := tierVerbs[tier]; !ok {
		return refuse("the request names a tier that is not a known tier")
	}
	if tier != e.Tier {
		return refuse("the request's tier is not the grant's tier")
	}
	if !validProfileName(profile) {
		return refuse("the request's profile is not a DNS label")
	}
	if e.Profile == "" {
		return refuse("the grant was not minted for a profile")
	}
	if profile != e.Profile {
		return refuse("the grant was minted for another profile")
	}
	return nil
}

// Verbs returns the verb vocabulary, every verb some tier's set names,
// sorted. TestTierSetsKeepTheVocabulary holds it to the verbs the retired
// rank knew.
func Verbs() []Verb {
	all := verbSet{}
	for _, set := range tierVerbs {
		for v := range set {
			all[v] = struct{}{}
		}
	}
	out := make([]Verb, 0, len(all))
	for v := range all {
		out = append(out, v)
	}
	sortVerbs(out)
	return out
}

func sortVerbs(v []Verb) {
	for i := 1; i < len(v); i++ {
		for j := i; j > 0 && v[j] < v[j-1]; j-- {
			v[j], v[j-1] = v[j-1], v[j]
		}
	}
}
