package capability

// Verb is an action a broker is about to take on behalf of a request.
//
// The entry does not carry a list of these, deliberately: 09's shape is
// {tier, scope, delegate} and A4 owes 09 the action-list amendment along with
// the per-call lookup. So a verb is permitted by the *tier* it requires, and
// that mapping is code rather than data on the wire. The consequence worth
// stating plainly: adding a verb here widens what every existing capability at
// that tier already permits, retroactively and without a rewrite. That is the
// property the action list would remove, and it is A4's to remove.
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
)

// verbTier is the required tier for each verb. A verb absent from this table is
// refused: the table is the allowlist, and an unknown verb reaching a verifier
// means a caller and a verifier disagree about the vocabulary, which is not a
// condition to resolve in the permissive direction.
var verbTier = map[Verb]Tier{
	VerbTaskExecute:        TierDeveloperTeam,
	VerbTaskDelegate:       TierDeveloperTeam,
	VerbTopicPublishShared: TierClusterAdmin,
	VerbClusterMutate:      TierClusterAdmin,
	VerbFleetRead:          TierPlatform,
}

// Permits answers the question the verifier exists to answer: does this
// capability permit verb v on resource r. It is applied to the *leaf* of a
// resolved chain, after the chain itself has been walked — a leaf is never
// wider than its root, so checking the leaf checks the chain.
func Permits(e Entry, v Verb, r Scope) error {
	need, ok := verbTier[v]
	if !ok {
		return refuse("verb is not in the table")
	}
	have, ok := tierRank[e.Tier]
	if !ok {
		return refuse("capability carries a tier that is not in the lattice")
	}
	if have < tierRank[need] {
		return refuse("capability's tier is below the tier the verb requires")
	}
	if err := r.validate(); err != nil {
		return refuse("the resource is not a well-formed scope")
	}
	if !e.Scope.Contains(r) {
		return refuse("resource is outside the capability's scope")
	}
	return nil
}

// Verbs returns the verb vocabulary, sorted for the conformance suite. The
// suite holds this table to the documented one in both directions, the way
// profiles.TestFieldsMatchTheSpecTable holds the profile struct to its spec.
func Verbs() []Verb {
	out := make([]Verb, 0, len(verbTier))
	for v := range verbTier {
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
