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
// (handleDelegateRequest): the door's own-list rule and the target's list for
// r's backend, and for a profile the tier r would run at against the card's
// tiers. It stops at DelegateTargetsMax, so an install with more profiles than
// that shows the first ones by name. Resolving a target (a pinned profile's
// cluster, an ambiguous name) is the delegation's step, not the list's.
func (g *Gateway) delegateTargets(r TaskRequester, profiles profileLister) lib.DelegateTargets {
	allowed := func(target string) bool {
		return !g.doorUnlisted(target, r.Backend) && g.targetAllows(target, r.Backend, r.Subject)
	}
	out := lib.DelegateTargets{Targets: []lib.DelegateTarget{}}
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
		if len(out.Targets) == lib.DelegateTargetsMax {
			break
		}
		if c.Name == targetPlatform || !lib.ValidSubjectToken(c.Name) || !allowed(c.Name) {
			continue
		}
		if len(c.Tiers) > 0 && !slices.Contains(c.Tiers, string(tier)) {
			continue
		}
		out.Targets = append(out.Targets, lib.DelegateTarget{Name: c.Name, Description: lib.DelegateTargetDescription(c.Description)})
	}
	return out
}
