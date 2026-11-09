package gateway

import (
	"slices"
	"sort"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// platformTargetDescription is how a session's delegate tool describes the
// platform agent. It has no card on DIRECTORY, so the gateway words it.
const platformTargetDescription = "the platform agent, which can read and act across the fleet"

// profileLister is what delegateTargets reads the profiles from: every card
// the gateway holds, in name order. The profile directory serves it from the
// cards it keeps off DIRECTORY (profiledir.go), so building the list costs no
// bus read. A cache with its own refresh, or a per-requester view once the
// PlatformAgent's membership mapping lands, would slot in behind this.
type profileLister interface {
	sortedCards() []lib.AgentCard
}

// delegateAuthors is everyone a delegation from this turn will be checked
// for, as far as the list can know it when the turn is submitted: the
// session's author set as it will stand once this turn's requester joins it,
// unknown if that would pass sessionAuthorCap. The set is recorded after the
// submission is built, and a pod about to be spawned is seeded only then
// with the authors of the earlier turns its primer replays (ensureSessionPod).
// So for an incarnation whose set isn't started (a fresh or rehydrated pod),
// it is every requester and steer author still on the conversation's record,
// a superset of what the primer keeps: the list may leave out a profile the
// delegation would allow, never offer one it would refuse for these people.
func delegateAuthors(rec *SessionRecord, r TaskRequester) ([]TaskRequester, bool) {
	var out []TaskRequester
	unknown := false
	add := func(a TaskRequester) {
		if !slices.Contains(out, a) {
			out = append(out, a)
		}
	}
	if authors, u, _ := rec.sessionAuthorsOf(); rec.BusSession != "" && rec.SessionAuthorsFor == rec.BusSession {
		unknown = u
		for _, a := range authors {
			add(a)
		}
	} else {
		for _, ref := range rec.Tasks {
			if ref.Requester == nil {
				continue
			}
			add(*ref.Requester)
			for _, a := range ref.SteerAuthors {
				add(a)
			}
			unknown = unknown || ref.SteerAuthorsOverflow
		}
	}
	add(r)
	return out, unknown || len(out) > sessionAuthorCap
}

// sortedCards is every card the directory holds, in name order.
func (d *profileDirectory) sortedCards() []lib.AgentCard {
	if d == nil {
		return nil
	}
	d.mu.RLock()
	defer d.mu.RUnlock()
	out := make([]lib.AgentCard, 0, len(d.cards))
	for _, c := range d.cards {
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// delegateTargets is the agents r may delegate to, as a session turn's
// delegateTargets part carries them: platform first, then each profile in
// name order. It applies the checks a delegation meets when it arrives
// (handleDelegateRequest): the door's own-list rule and the target's list,
// for r and for everyone else whose text reached the session (authors), and
// for a profile the tier r would run at against the card's tiers. With that
// set incomplete (authorsUnknown) the delegation refuses every target, so the
// list is empty. Steers that arrive during the turn, and a pinned profile
// whose own cluster is out of scope, are checked only when a delegation
// arrives; the list is a hint, and those cost a refused delegation. It stops at DelegateTargetsMax, or before the part would pass
// DelegateTargetsMaxBytes, and says so (Truncated), so an install with more
// profiles than that shows the first ones by name. Resolving a target (a
// pinned profile's cluster, an ambiguous name) is the delegation's step, not
// the list's.
func (g *Gateway) delegateTargets(r TaskRequester, authors []TaskRequester, authorsUnknown bool, profiles profileLister) lib.DelegateTargets {
	out := lib.DelegateTargets{Targets: []lib.DelegateTarget{}}
	if authorsUnknown {
		return out
	}
	everyone := append([]TaskRequester{r}, authors...)
	allowed := func(target string) bool {
		for _, a := range everyone {
			if g.authorRefusal(target, a, ruleDelegationSessionAuthor) != "" {
				return false
			}
		}
		return true
	}
	if allowed(targetPlatform) {
		out.Targets = append(out.Targets, lib.DelegateTarget{Name: targetPlatform, Description: platformTargetDescription})
	}
	tier, err := requesterTier(r)
	if err != nil {
		// No attested identity reaches no profile (profileRouteFor's caller
		// refuses it the same way).
		return out
	}
	for _, c := range profiles.sortedCards() {
		if c.Name == targetPlatform || !lib.ValidSubjectToken(c.Name) || !allowed(c.Name) {
			continue
		}
		if len(c.Tiers) > 0 && !slices.Contains(c.Tiers, string(tier)) {
			continue
		}
		next := lib.DelegateTarget{Name: c.Name, Description: lib.DelegateTargetDescription(c.Description), Parameters: c.Parameters}
		if !fits(lib.DelegateTargets{}, next) {
			// A card whose parameters break the part's rules isn't offered;
			// the delegation would refuse its parameters anyway.
			continue
		}
		if len(out.Targets) == lib.DelegateTargetsMax || !fits(out, next) {
			out.Truncated = true
			break
		}
		out.Targets = append(out.Targets, next)
	}
	return out
}

// fits reports whether t with next added is still a valid part: inside the
// byte bound, and with next's parameters well formed (a card the operator
// wrote is held to the part's rules rather than trusted).
func fits(t lib.DelegateTargets, next lib.DelegateTarget) bool {
	t.Targets = append(slices.Clone(t.Targets), next)
	// Measured as it would go out if this is the last that fits.
	t.Truncated = true
	_, err := lib.DelegateTargetsPart(t)
	return err == nil
}
