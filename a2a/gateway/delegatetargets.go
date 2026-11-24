package gateway

import (
	"slices"
	"sort"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// platformTargetDescription is how a session's delegate tool describes the
// platform agent. It has no card on DIRECTORY, so the gateway words it.
const platformTargetDescription = "the platform agent, which can read and act across the fleet"

// profileLister is what delegateTargets reads the profiles from: every card
// the gateway holds, in name order. The profile directory serves it from the
// cards it keeps off DIRECTORY (profiledir.go), so building the list costs no
// bus read. A cache with its own refresh, or a per-requester view by tier
// (tierOf), would slot in behind this.
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
// approximate reports that branch, where an unknown set may be the
// superset's overflow rather than the pod's (the primer drops turns to fit),
// so the caller sends no list rather than an empty one.
func delegateAuthors(rec *SessionRecord, r TaskRequester) (authors []TaskRequester, unknown, approximate bool) {
	var out []TaskRequester
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
		approximate = true
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
	return out, unknown || len(out) > sessionAuthorCap, approximate
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
	out := lib.DelegateTargets{Targets: []lib.DelegateTarget{}, WidthMax: g.advertisedWidth()}
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
	tier, err := g.tierFor(everyone, false) // an unknown author list returned above
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
		next := lib.DelegateTarget{Name: c.Name, Description: lib.DelegateTargetDescription(c.Description), Parameters: offeredParameters(c)}
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

// advertisedWidth is the batch width a delegateTargets part offers: the
// setting the delegation refuses a wider batch under (DelegationWidthMax, in
// handleDelegateRequest), for a session parent and a profile parent alike.
// It rides the part only when it offers a batch, so at one the part is the
// shape a pod built before batches reads.
func (g *Gateway) advertisedWidth() int {
	if g.cfg != nil && g.cfg.DelegationWidthMax > 1 {
		return g.cfg.DelegationWidthMax
	}
	return 0
}

// advertisedRounds is the delegation rounds a profile parent's
// delegateTargets part offers: the setting its requests are refused past
// (DelegationRoundsMax, under delegation.rounds in handleDelegateRequest). It
// rides the part only when over one, so at one the part is the shape a worker
// built before rounds reads. A session parent's part never carries it: the
// rule is a profile parent's.
func (g *Gateway) advertisedRounds() int {
	if g.cfg != nil && g.cfg.DelegationRoundsMax > 1 {
		return g.cfg.DelegationRoundsMax
	}
	return 0
}

// probeParameterValue fills a required parameter in the probe
// profileDelegateTargets checks a profile with: the parent's worker supplies
// the real value, and the gateway reads no value but a target's.
const probeParameterValue = "probe"

// probeTargetToken names every part of the cluster in the probe of a profile
// whose cluster the parent's worker chooses. An underscore is in no GCP
// project, location or cluster name, so the probe never matches a pin.
const probeTargetToken = "probe_target"

// profileDelegateTargets is the delegateTargets part a delegating profile
// parent's submission carries (#6789): every profile the delegation would
// accept from parent, in name order, the batch width it would accept, and
// the delegation rounds (advertisedRounds).
// It is not a second copy of the rules: the parent-wide refusals
// (profileParentRefusal, the depth cap) and each profile's
// (delegateEntryCheck) are the delegation's own, run on a probe entry
// addressed to the profile. Platform is never listed: a profile parent's
// platform child is refused. A parent below a scheduled run is offered only
// profiles that run at read and list unattended, as delegateEntryCheck holds
// its children to (#6744). A parent the delegation refuses outright, such as
// one at the depth cap, gets an empty list, which its tool reads as nobody.
//
// The probe gives the profile what a delegation that can be accepted would:
// each required parameter, and a target. Under a parent scoped to a cluster
// that is the parent's own cluster, for a profile not pinned elsewhere, since
// a child outside it is refused; so the list never offers a profile that
// can't run there. Under a parent with the install's scope, a profile that
// takes a target gets one the worker will choose, and only that cluster's
// own checks (in scope, pinned profiles' tiers, the profile it yields to) are
// left to the delegation (profileRouteWith). The parent's chain and the
// requester's lists are checked as they stand at submission; a steer during
// the task is checked only when the delegation arrives.
func (g *Gateway) profileDelegateTargets(rec *SessionRecord, parent TaskRef) lib.DelegateTargets {
	out := lib.DelegateTargets{Targets: []lib.DelegateTarget{}, WidthMax: g.advertisedWidth(), RoundsMax: g.advertisedRounds()}
	if parent.Requester == nil || parent.Depth >= g.cfg.DelegationDepthMax {
		return out
	}
	if rule, _ := profileParentRefusal(parent); rule != "" {
		return out
	}
	parentCluster, clusterScoped := scopeCluster(capability.Scope(parent.Scope))
	for _, c := range g.profiles.sortedCards() {
		if c.Name == targetPlatform || !lib.ValidSubjectToken(c.Name) {
			continue
		}
		params := map[string]string{}
		for _, p := range c.Parameters {
			if p.Required {
				params[p.Name] = probeParameterValue
			}
		}
		delete(params, paramProject)
		delete(params, paramLocation)
		delete(params, paramCluster)
		scopeChecked := true
		switch {
		case c.ClusterRef != nil:
			// Its target is its pin, which the delegation checks as given.
		case clusterScoped:
			params[paramProject], params[paramLocation], params[paramCluster] = parentCluster.Project, parentCluster.Location, parentCluster.Cluster
		case takesTarget(c):
			params[paramProject], params[paramLocation], params[paramCluster] = probeTargetToken, probeTargetToken, probeTargetToken
			scopeChecked = false
		}
		if len(params) == 0 {
			params = nil
		}
		if _, no := g.delegateEntryCheck(rec, parent, true, lib.DelegateTask{Addressee: c.Name, Parameters: params}, scopeChecked); no != nil {
			continue
		}
		next := lib.DelegateTarget{Name: c.Name, Description: lib.DelegateTargetDescription(c.Description), Parameters: offeredParameters(c)}
		if !fits(lib.DelegateTargets{}, next) {
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

// profileTargetsParts is the delegateTargets part a profile task submitted
// on its own carries (#6789), parent being the entry its submitter records
// for it: one part when parent's profile's card says it delegates
// (profileDelegates), none otherwise, so a non-delegating profile's
// submission is unchanged. A part that can't be built is left off and
// logged; the worker's delegate tool then falls back to platform. Both a
// task started from a turn (startTaskWith) and a scheduled run
// (publishTriggerRun) read it, so they carry the same list.
func (g *Gateway) profileTargetsParts(rec *SessionRecord, parent TaskRef, taskID string) []lib.Part {
	if !g.profileDelegates(parent.Addressee) {
		return nil
	}
	part, err := lib.DelegateTargetsPart(g.profileDelegateTargets(rec, parent))
	if err != nil {
		g.log.Warn("delegate targets not attached to a profile task; its delegate tool falls back to platform", "taskId", taskID, "err", err)
		return nil
	}
	return []lib.Part{part}
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

// offeredParameters is what a card asks a delegation for. A profile pinned to a
// cluster takes its target from the pin (resolveProfile), so its project,
// location and cluster aren't offered: listing them as required would invite
// a partial or guessed target the gateway refuses.
func offeredParameters(c lib.AgentCard) []lib.ProfileParameter {
	if c.ClusterRef == nil {
		return c.Parameters
	}
	out := make([]lib.ProfileParameter, 0, len(c.Parameters))
	for _, p := range c.Parameters {
		switch p.Name {
		case paramProject, paramLocation, paramCluster:
			continue
		}
		out = append(out, p)
	}
	return out
}
