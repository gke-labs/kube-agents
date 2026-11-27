package gateway

import (
	"slices"
	"sort"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// profileScopeClusters is the scopeClusters part a delegating profile
// parent's submission carries beside its delegateTargets (#6789): one page of
// the clusters a `cluster` child delegation from parent would be accepted
// for, as far as the install's scope decides it. The clusters come from the
// resolved scope document's `clusters` map, which the scope publisher writes
// for member projects; each is held to the delegation's own scope check
// (scopeVerdict: exclusions, the declaration, the resolved rows) and to the
// parent's scope (parentScopeHolds), so the page offers nothing the
// delegation refuses on scope. A profile's own rules (tiers, pins, lists)
// are the delegateTargets list's, and are left to the delegation.
//
// The page is the first min(DelegationWidthMax, ScopeClustersPageMax) in
// (project, location, cluster) order; Count is every cluster accepted.
// Projects in scope the map doesn't cover (an explicit project, the
// management project, a member no probe has listed) go in UnlistedProjects.
// With no map at all (no document, or one that dropped the map for size)
// Count is only what is known: Unknown, and Truncated. An install whose
// scope declares no boundary admits any project not excluded, so no list can
// be complete: Unknown and Unbounded, and no projects named as listed
// elsewhere. OldestListing is the oldest `at` among the projects the counted
// clusters came from; a stamp ahead of this clock (past the skew allowed)
// makes the age unknown, and OldestListing is left out.
//
// A parent scoped to one cluster is accepted for that cluster only, so its
// page is that cluster when it is in scope and nothing otherwise, with no map
// read, marked ParentScoped. A parent the delegation refuses outright, or an
// install whose scope file can't be read (every target refused), gets an
// empty page with a known count of zero.
func (g *Gateway) profileScopeClusters(parent TaskRef) lib.ScopeClusters {
	out := lib.ScopeClusters{Clusters: []lib.ScopeCluster{}}
	if parent.Requester == nil || parent.Depth >= g.cfg.DelegationDepthMax {
		return out
	}
	if rule, _ := profileParentRefusal(parent); rule != "" {
		return out
	}
	decl, err := loadScope(g.cfg.ScopeFile)
	if err != nil {
		// checkTargetScope refuses every target under a nil declaration, so
		// nothing is accepted and the empty page is the whole answer.
		return out
	}
	doc, lerr := g.loadResolvedScope()
	if lerr != nil {
		g.log.Warn("resolved scope unreadable; the scope clusters page lists none", "err", lerr)
	}
	return g.buildScopeClustersFor(decl, doc, capability.Scope(parent.Scope), time.Now())
}

// buildScopeClustersFor is the page for decl and doc under a parent at
// parentScope: the document indexed once, its listing from the source's
// cache, and the gateway's own verdict.
func (g *Gateway) buildScopeClustersFor(decl *scopeDeclaration, doc *resolvedScope, parentScope capability.Scope, now time.Time) lib.ScopeClusters {
	listing, known := g.resolvedClusters(doc)
	return buildScopeClusters(scopeClustersInput{
		decl: decl, doc: doc, listing: listing, known: known, now: now,
		parentScope: parentScope, pageMax: g.scopeClustersPageMax(),
		verdict: g.scopeVerdict(decl, newResolvedIndex(doc, decl), g.resolvedScope != nil, now),
		holds:   g.parentScopeHolds, management: g.cfg.ManagementProject,
	})
}

// scopeClustersPageMax is the page size: the delegation's width, so the page
// is one batch of targets, under the part's own cap.
func (g *Gateway) scopeClustersPageMax() int {
	width := 1
	if g.cfg != nil && g.cfg.DelegationWidthMax > 1 {
		width = g.cfg.DelegationWidthMax
	}
	return min(width, lib.ScopeClustersPageMax)
}

// scopeClustersInput is what buildScopeClusters reads: the declaration, the
// resolved document and its parsed listing, and the checks it applies.
type scopeClustersInput struct {
	decl        *scopeDeclaration
	doc         *resolvedScope
	listing     map[string]resolvedClusters
	known       bool
	now         time.Time
	parentScope capability.Scope
	pageMax     int
	management  string
	verdict     func(t clusterTarget) error
	holds       func(parent, child capability.Scope) bool
}

func buildScopeClusters(in scopeClustersInput) lib.ScopeClusters {
	out := lib.ScopeClusters{Clusters: []lib.ScopeCluster{}}
	if pc, ok := scopeCluster(in.parentScope); ok {
		out.ParentScoped = true
		if validClusterTarget(pc) && in.verdict(pc) == nil && in.holds(in.parentScope, pc.scope()) {
			out.Count = 1
			out.Clusters = append(out.Clusters, lib.ScopeCluster{Project: pc.Project, Location: pc.Location, Cluster: pc.Cluster})
		}
		return out
	}
	// A cluster exclude.clusters doesn't name is in scope exactly when its
	// project is: checkTargetScope decides the rest by project, and
	// checkResolvedScope only by project. So each project's verdict is taken
	// once, on a probe no exclude.clusters entry matches.
	verdicts := map[string]bool{}
	projectIn := func(p string) bool {
		v, seen := verdicts[p]
		if !seen {
			v = in.verdict(clusterTarget{Project: p, Location: probeTargetToken, Cluster: probeTargetToken}) == nil
			verdicts[p] = v
		}
		return v
	}

	listing := in.listing
	projects := make([]string, 0, len(listing))
	for p := range listing {
		projects = append(projects, p)
	}
	sort.Strings(projects)
	var oldest time.Time
	ageUnknown := false
	for _, p := range projects {
		if !lib.ValidSubjectToken(p) || !projectIn(p) {
			continue
		}
		entry := listing[p]
		targets := make([]clusterTarget, 0, len(entry.Clusters))
		for _, pair := range entry.Clusters {
			if len(pair) != 2 {
				continue
			}
			t := clusterTarget{Project: p, Location: pair[0], Cluster: pair[1]}
			if validClusterTarget(t) && !clusterExcluded(in.decl, t) && in.holds(in.parentScope, t.scope()) {
				targets = append(targets, t)
			}
		}
		sort.Slice(targets, func(i, j int) bool {
			if targets[i].Location != targets[j].Location {
				return targets[i].Location < targets[j].Location
			}
			return targets[i].Cluster < targets[j].Cluster
		})
		targets = slices.Compact(targets)
		if len(targets) == 0 {
			continue
		}
		out.Count += len(targets)
		for _, t := range targets {
			if len(out.Clusters) == in.pageMax {
				break
			}
			out.Clusters = append(out.Clusters, lib.ScopeCluster{Project: t.Project, Location: t.Location, Cluster: t.Cluster})
		}
		at, err := time.Parse(resolvedListingTimeFormat, entry.At)
		switch {
		case err != nil || at.After(in.now.Add(resolvedScopeClockSkew)):
			// No age to give: a stamp ahead of this clock would read as
			// fresher than any listing can be.
			ageUnknown = true
		case oldest.IsZero() || at.Before(oldest):
			oldest = at
		}
	}
	if !oldest.IsZero() && !ageUnknown {
		out.OldestListing = oldest.UTC().Format(time.RFC3339)
	}
	out.Unbounded = !in.decl.Present
	out.Unknown = !in.known || out.Unbounded
	out.Truncated = out.Unknown || out.Count > len(out.Clusters)
	if out.Unbounded {
		// Every project not excluded is in scope, so there is no set the
		// unlisted ones could be the rest of.
		return out
	}

	// The projects in scope whose clusters the map doesn't carry: a member
	// row no probe has listed yet and, from a publisher before #7107, the
	// explicit ones and the management project, which it never probed (from
	// #7107 it lists those too). Without a map, every project in scope is one.
	candidates := slices.Clone(in.decl.Projects)
	if in.management != "" {
		candidates = append(candidates, in.management)
	}
	if in.doc != nil {
		for _, r := range in.doc.Projects {
			candidates = append(candidates, r.ID)
		}
	}
	slices.Sort(candidates)
	candidates = slices.Compact(candidates)
	probe := clusterTarget{Location: probeTargetToken, Cluster: probeTargetToken}
	for _, p := range candidates {
		if _, listed := listing[p]; listed || !lib.ValidSubjectToken(p) || !projectIn(p) {
			continue
		}
		probe.Project = p
		if !in.holds(in.parentScope, probe.scope()) {
			continue
		}
		out.UnlistedCount++
		if len(out.UnlistedProjects) < lib.ScopeClustersUnlistedMax {
			out.UnlistedProjects = append(out.UnlistedProjects, p)
		}
	}
	return out
}

// clusterExcluded reports whether decl's exclude.clusters names t, the one
// part of checkTargetScope that reads a target's location and cluster.
func clusterExcluded(decl *scopeDeclaration, t clusterTarget) bool {
	if decl == nil {
		return false
	}
	for _, c := range decl.Exclude.Clusters {
		if c.ProjectID == t.Project && c.Location == t.Location && c.ClusterName == t.Cluster {
			return true
		}
	}
	return false
}

// validClusterTarget reports whether each part of t is a subject token, as
// the part's own bound and a delegation's parameters hold them.
func validClusterTarget(t clusterTarget) bool {
	return lib.ValidSubjectToken(t.Project) && lib.ValidSubjectToken(t.Location) && lib.ValidSubjectToken(t.Cluster)
}

// scopeClustersParts is the scopeClusters part for a delegating profile
// parent (profileTargetsParts), or none when it can't be built, logged.
func (g *Gateway) scopeClustersParts(parent TaskRef, taskID string) []lib.Part {
	part, err := lib.ScopeClustersPart(g.profileScopeClusters(parent))
	if err != nil {
		g.log.Warn("scope clusters not attached to a profile task", "taskId", taskID, "err", err)
		return nil
	}
	return []lib.Part{part}
}
