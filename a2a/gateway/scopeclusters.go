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
// Count is only what is known: Unknown, and Truncated. OldestListing is the
// oldest `at` among the projects the counted clusters came from.
//
// A parent scoped to one cluster is accepted for that cluster only, so its
// page is that cluster when it is in scope and nothing otherwise, with no map
// read. A parent the delegation refuses outright gets an empty page.
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
	return buildScopeClusters(scopeClustersInput{
		decl: decl, doc: doc, armed: g.resolvedScope != nil, now: time.Now(),
		parentScope: capability.Scope(parent.Scope), pageMax: g.scopeClustersPageMax(),
		verdict: g.scopeVerdict, holds: g.parentScopeHolds, management: g.cfg.ManagementProject,
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

// scopeClustersInput is what buildScopeClusters reads, so the scale test can
// time the build on its own.
type scopeClustersInput struct {
	decl        *scopeDeclaration
	doc         *resolvedScope
	armed       bool
	now         time.Time
	parentScope capability.Scope
	pageMax     int
	management  string
	verdict     func(decl *scopeDeclaration, doc *resolvedScope, armed bool, now time.Time, t clusterTarget) error
	holds       func(parent, child capability.Scope) bool
}

// scopeProbeToken names the location and cluster of the probe that asks
// whether a project is in scope at all: an underscore is in no GCP location
// or cluster name, so no exclude.clusters entry matches it.
const scopeProbeToken = "probe_target"

func buildScopeClusters(in scopeClustersInput) lib.ScopeClusters {
	out := lib.ScopeClusters{Clusters: []lib.ScopeCluster{}}
	if pc, ok := scopeCluster(in.parentScope); ok {
		if validClusterTarget(pc) && in.verdict(in.decl, in.doc, in.armed, in.now, pc) == nil && in.holds(in.parentScope, pc.scope()) {
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
			v = in.verdict(in.decl, in.doc, in.armed, in.now, clusterTarget{Project: p, Location: scopeProbeToken, Cluster: scopeProbeToken}) == nil
			verdicts[p] = v
		}
		return v
	}

	listing, known := resolvedClustersOf(in.doc)
	projects := make([]string, 0, len(listing))
	for p := range listing {
		projects = append(projects, p)
	}
	sort.Strings(projects)
	var oldest time.Time
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
		if at, err := time.Parse(time.RFC3339, entry.At); err == nil && (oldest.IsZero() || at.Before(oldest)) {
			oldest = at
		}
	}
	if !oldest.IsZero() {
		out.OldestListing = oldest.UTC().Format(time.RFC3339)
	}
	out.Unknown = !known
	out.Truncated = out.Unknown || out.Count > len(out.Clusters)

	// The projects in scope whose clusters the map doesn't carry: the
	// explicit ones and the management project, which the publisher never
	// probes, and a member row no probe has listed yet. Without a map,
	// every project in scope is one.
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
	probe := clusterTarget{Location: scopeProbeToken, Cluster: scopeProbeToken}
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
