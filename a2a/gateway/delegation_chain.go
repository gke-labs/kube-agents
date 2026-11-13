package gateway

import (
	"fmt"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/capability"
)

// clusterScopeSegments is how many segments a cluster scope has:
// project/P/location/L/cluster/C (clusterTarget.scope).
const clusterScopeSegments = 6

// A profile parent's delegation rules (#5686; spec-profiles-dispatch.md,
// "Scale rules" and "Replacing kanban's plumbing" item 3; 09, "Profile
// tasks"). A profile task's child is minted a fresh root, not a hop, so the
// verifier never compares parent with child: the gateway's mint is where
// these hold. A child is never wider than its parent on tier (minted at the
// parent's, by profileRouteFor), scope or targets, and no profile appears
// twice on a chain except the chain head's one self-nest.

// childProfileChain is the chain a child of parent carries: the parent's
// chain and the parent's profile when the parent is a profile task, and
// none when a session turn delegated (the child heads its chain).
func childProfileChain(parent TaskRef, profileParent bool) []string {
	if !profileParent {
		return nil
	}
	return append(slices.Clone(parent.ProfileChain), parent.Addressee)
}

// profileParentRefusal is why a profile parent may not delegate at all, as
// a rule and a notice, or "" when it may. Checked once, before any child.
func profileParentRefusal(parent TaskRef) (rule, notice string) {
	// An unattended grant is bound to its trigger (09), and the child mint
	// carries no trigger: refused until the child's inheritance is designed.
	if capability.Tier(parent.Tier) == capability.TierUnattended {
		return ruleDelegationUnattendedParent, noticeUnattendedParent
	}
	// A profile task this gateway minted always has a scope, so an empty one
	// is a record from before #5686 or a path that didn't record it: its
	// chain is unknown too. Refused, never read as "no chain".
	if parent.Scope == "" {
		return ruleDelegationParentScopeUnknown, noticeParentScopeUnknown
	}
	return "", ""
}

// profileChildRefusal is why a profile parent may not start a child on
// route, as a rule and a notice, or "" when it may. route is the child's
// resolved route, so its Profile is the profile the address resolved to: a
// generic profile that yields to the one pinned to the target is checked as
// that profile, not as the name the parent wrote.
func (g *Gateway) profileChildRefusal(rec *SessionRecord, parent TaskRef, route *profileRoute) (rule, notice string) {
	chain := append(slices.Clone(parent.ProfileChain), parent.Addressee)
	headSelfNest := len(parent.ProfileChain) == 0 && route.Profile == parent.Addressee
	if slices.Contains(chain, route.Profile) && !headSelfNest {
		return ruleDelegationProfileCycle, fmt.Sprintf(noticeProfileCycle, route.Profile, strings.Join(chain, noticeChainSeparator))
	}
	if !g.parentScopeHolds(capability.Scope(parent.Scope), route.Scope) {
		parentCluster, isCluster := clusterForRoom(capability.Scope(parent.Scope))
		if !isCluster {
			// Neither a cluster nor this install's scope: nothing the room can
			// read as a scope, and nothing to delegate within.
			return ruleDelegationParentScope, noticeParentScopeStale
		}
		if route.Scope == capability.NamespaceScope(g.cfg.Namespace) {
			return ruleDelegationParentScope, fmt.Sprintf(noticeChildWiderThanParent, parentCluster)
		}
		childCluster, _ := clusterForRoom(route.Scope)
		return ruleDelegationParentScope, fmt.Sprintf(noticeOutsideParentScope, childCluster, parentCluster)
	}
	if !chainAgreesWithRecord(rec, parent) {
		return ruleDelegationParentChainMismatch, noticeParentChainMismatch
	}
	return "", ""
}

// parentScopeHolds reports whether a child at scope child lies inside a
// profile parent at scope parent. A parent with the install's scope (a task
// with no target) holds any child the install's own scope admits, which
// profileRouteFor has already checked: the install's namespace scope does
// not path-contain a cluster path, but the install's scope covers every
// cluster its scope file declares. Any other parent holds only what its
// scope path-contains, so a targetless child (the install's scope) under a
// cluster-scoped parent is refused.
func (g *Gateway) parentScopeHolds(parent, child capability.Scope) bool {
	if parent == capability.NamespaceScope(g.cfg.Namespace) {
		return true
	}
	return parent.Contains(child)
}

// chainAgreesWithRecord checks the carried chain against the ancestors the
// record still holds: walking up from parent through profile parents, each
// one present must be the profile the chain names at its place. A missing
// link (the bounded history evicted it) stops the walk; a contradicting one
// refuses. The walk stops at a session turn or a wake, where a chain starts
// afresh, so a profile task that ended before a wake isn't counted.
func chainAgreesWithRecord(rec *SessionRecord, parent TaskRef) bool {
	cur, i := parent, len(parent.ProfileChain)-1
	for cur.Role == taskRoleChild {
		up, ok := rec.TaskRefFor(cur.ParentTaskID)
		if !ok {
			return true
		}
		if up.Tier == "" || up.Role == taskRoleWake {
			// A session turn or a wake delegated cur: cur heads its chain.
			return i < 0
		}
		if i < 0 || parent.ProfileChain[i] != up.Addressee {
			return false
		}
		cur, i = up, i-1
	}
	// cur is a top submission, present on the record: it heads the chain,
	// so nothing may be left of the carried chain above it.
	return i < 0
}

// clusterForRoom is a cluster scope as the room reads it, "cluster
// project/location/cluster", the way the other target notices write one, and
// whether s is a cluster scope at all. A child's route scope is a cluster or
// the install's namespace scope, and the caller handles the latter first.
func clusterForRoom(s capability.Scope) (string, bool) {
	segs := strings.Split(string(s), "/")
	if len(segs) == clusterScopeSegments && segs[0] == scopeKindProject && segs[2] == scopeKindLocation && segs[4] == scopeKindCluster {
		return scopeKindCluster + " " + strings.Join([]string{segs[1], segs[3], segs[5]}, "/"), true
	}
	return "", false
}
