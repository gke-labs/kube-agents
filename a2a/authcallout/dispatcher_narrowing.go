package authcallout

import (
	"fmt"
	"slices"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// The A2A profile dispatcher's grants, split across connections (#5381).
//
// The dispatcher's map entry narrows on NarrowingDispatcher. A connection
// that presents the dispatcher's token and no password is its control
// connection: it gets the entry's own grants, the fixed set that names no
// profile (the lease KV, offers, accepts, the capability verifier, its
// inbox). A connection that presents the token and a bucket password
// ("7/32", lib.DispatcherBucketCredential) is a bucket connection. Its
// grants are derived here from the profiles in the map that hash to that
// bucket, plus the bucket's draining profiles: their durables, supervisor
// publish and stop grants (lib.DispatcherGrants), and the bucket's own inbox.
//
// The password is not a credential. The token decides who connects, and the
// bucket only picks which slice of that principal's grants this connection
// holds. A password on any other entry, or a bucket spelling this callout
// doesn't accept, is refused. It never falls back to the control
// connection's grants.

// dispatcherBucketUser is the NATS user a bucket connection is issued: the
// entry's user, then -b<bucket>. It is a single label, so its inbox prefix
// _INBOX.<user>-b<n>.> is disjoint from the control connection's
// _INBOX.<user>.>.
func dispatcherBucketUser(user string, bucket int) string {
	return lib.DispatcherBucketUser(user, bucket)
}

// dispatcherBucketUsers is every bucket user a dispatcher entry can issue.
func dispatcherBucketUsers(user string) []string {
	out := make([]string, lib.DispatcherBuckets)
	for b := range out {
		out[b] = dispatcherBucketUser(user, b)
	}
	return out
}

// validateDispatcherEntry checks what only the dispatcher's entry carries.
func validateDispatcherEntry(id Identity) error {
	for _, u := range dispatcherBucketUsers(id.User) {
		if !lib.ValidSubjectToken(u) {
			return fmt.Errorf("user %q is too long for its bucket users: %q must be a single DNS-1123 label", id.User, u)
		}
	}
	seen := make(map[string]bool, len(id.Draining))
	for _, p := range id.Draining {
		if !lib.ValidSubjectToken(p) {
			return fmt.Errorf("user %q lists draining profile %q, which is not a single subject token", id.User, p)
		}
		if seen[p] {
			return fmt.Errorf("user %q lists draining profile %q twice", id.User, p)
		}
		seen[p] = true
	}
	return nil
}

// dispatcherBucketGrants derives one bucket connection's grants from m: the
// profile entries whose profile hashes to bucket, with every tier the map
// gives that profile, then the bucket's draining profiles that have no entry
// left. A profile entry with no tier (an operator before per-tier identities)
// has no durable name, so it contributes the profile's stop and supervisor
// grants but no durable. Refused profiles have no entry, so they get nothing,
// as the operator's render did.
func dispatcherBucketGrants(m *IdentityMap, id Identity, bucket int) Grants {
	var profiles []lib.DispatcherGrantProfile
	index := map[string]int{}
	for _, e := range m.Identities {
		if e.Narrowing != NarrowingProfile || lib.DispatcherBucket(e.Profile) != bucket {
			continue
		}
		i, ok := index[e.Profile]
		if !ok {
			i = len(profiles)
			index[e.Profile] = i
			profiles = append(profiles, lib.DispatcherGrantProfile{Name: e.Profile})
		}
		if e.Tier != "" && !slices.Contains(profiles[i].Tiers, e.Tier) {
			profiles[i].Tiers = append(profiles[i].Tiers, e.Tier)
		}
	}
	for _, p := range id.Draining {
		if _, live := index[p]; live || lib.DispatcherBucket(p) != bucket {
			continue
		}
		index[p] = len(profiles)
		profiles = append(profiles, lib.DispatcherGrantProfile{Name: p, Draining: true})
	}
	derived := lib.DispatcherGrants(profiles)
	inbox := "_INBOX." + dispatcherBucketUser(id.User, bucket) + ".>"
	return Grants{
		Publish:   append(derived.Publish, inbox),
		Subscribe: append(derived.Subscribe, inbox),
	}
}
