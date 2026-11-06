// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"context"
	"fmt"
	"regexp"
	"sort"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The ScopeUndeclared condition: the clusters the Cluster Agent reconcile adopted
// without anyone asking for them. With no membership list on spec.scope the
// reconcile lists the management project and gives every GKE cluster in it a
// Cluster Agent (docs/designs/multi-project-scope.md §7), so a single-cluster
// install in a shared project quietly watches its neighbours. Nothing on the CR
// said so until this condition, which the usage poller writes from the watcher's
// cluster_up series, the same reading status.usage.clustersRegistered counts: each
// series is one cluster the watcher built a client for, labelled with its
// profile's triple. It is True only while the scope is undeclared and a cluster
// other than spec.harness's is registered; otherwise it is removed, as the data
// volume condition is without a reading. It does not touch Ready. The reconcile
// cannot write it itself: the agent's ServiceAccount can get the PlatformAgent
// and not patch it, by design.
const (
	scopeUndeclaredCondition = "ScopeUndeclared"
	scopeUndeclaredReason    = "ClustersAdoptedFromProject"
	// scopeUndeclaredNamesShown bounds the names the message lists; the rest
	// are counted. Enough for the single-cluster install this exists for, and
	// short of the 32 KiB a condition message is capped at under a 200-cluster
	// project.
	scopeUndeclaredNamesShown = 8
	// scopeUndeclaredInvalidName stands in for a label value that is not shaped
	// like a GKE identifier: the labels are a pod listener's text, and the CR
	// is not where it gets printed as written.
	scopeUndeclaredInvalidName = "?"
	scopeUndeclaredHint        = "Declare the scope on spec.scope: name the management project in spec.scope.projects to keep them, or list each one under spec.scope.exclude.clusters to leave it unmanaged."
)

// scopeUndeclaredNamePattern is the shape a cluster name, project ID or location
// takes (the CRD's ScopeClusterRef patterns); a label value outside it prints as
// scopeUndeclaredInvalidName.
var scopeUndeclaredNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// scopeDeclared reports whether spec.scope names a project, folder, organisation
// or selector. Exclusions are not a declaration: a block whose membership lists
// are all empty, the installer's own render without SCOPE_* values, still
// adopts the management project's other clusters.
func scopeDeclared(agent *agentv1alpha1.PlatformAgent) bool {
	s := agent.Spec.Scope
	if s == nil {
		return false
	}
	return len(s.Projects)+len(s.Folders)+len(s.Organizations)+len(s.SharedVpcHosts)+len(s.MetricsScopes) > 0
}

// usageClusterRefsForPoll is the registered clusters this poll projects, and
// whether it projects a reading at all, on the data volume reading's terms
// (usageDataVolumeForPoll): known false while the watcher is on and no gateway
// replica has been read for less than the event streak; known true with nil
// when the watcher is off or down. Every replica builds the same fleet and one
// mid-startup reports fewer, so the reading is the union, each triple once,
// sorted so an unchanged fleet reads the same on every poll.
func usageClusterRefsForPoll(agent *agentv1alpha1.PlatformAgent, scraped []usageScrapedPod, gatewayRead, watcherDown bool) (refs []usageClusterRef, known bool) {
	if !eventWatcherEnabled(agent) || watcherDown {
		return nil, true
	}
	if !gatewayRead {
		return nil, false
	}
	seen := map[usageClusterRef]bool{}
	for _, s := range scraped {
		if s.Clusters == nil {
			continue
		}
		for _, ref := range s.Clusters.Refs {
			if !seen[ref] {
				seen[ref] = true
				refs = append(refs, ref)
			}
		}
	}
	sort.Slice(refs, func(i, j int) bool {
		a, b := refs[i], refs[j]
		return a.Project < b.Project || (a.Project == b.Project && (a.Location < b.Location || (a.Location == b.Location && a.Name < b.Name)))
	})
	return refs, true
}

// scopeNeighbours is every registered cluster in the management project that is
// not the host: spec.harness's cluster by name, and by location where the series
// carries one (the directly reached cluster is labelled by name alone, with no
// project, until its profile is absorbed). The project filter is what makes the
// attribution true: project-wide adoption produces management-project clusters
// only, and a profile made by hand for a cluster in another project, which the
// scope prune keeps, is not this condition's to report. nil when the CR does not
// name its host, or names its project by number: the watcher labels the series
// with the project ID, so the host could not be told from a neighbour.
func scopeNeighbours(agent *agentv1alpha1.PlatformAgent, refs []usageClusterRef) []usageClusterRef {
	if !scopeHostKnown(agent) {
		return nil
	}
	h := agent.Spec.Harness
	var out []usageClusterRef
	for _, ref := range refs {
		// The bare direct entry is skipped here too: it is the host, under no project.
		if ref.Project != h.ProjectID {
			continue
		}
		host := ref.Name == h.ClusterName && (ref.Location == "" || ref.Location == h.Location)
		if !host {
			out = append(out, ref)
		}
	}
	return out
}

// scopeHostKnown reports whether spec.harness names the host cluster and its
// project by ID, the two things a series must match to read as the host.
func scopeHostKnown(agent *agentv1alpha1.PlatformAgent) bool {
	h := agent.Spec.Harness
	return h != nil && h.ClusterName != "" && h.ProjectID != "" && !isProjectNumber(h.ProjectID)
}

func scopeUndeclaredLabel(value string) string {
	if scopeUndeclaredNamePattern.MatchString(value) {
		return value
	}
	return scopeUndeclaredInvalidName
}

// scopeUndeclaredMessage names the adopted clusters, the first
// scopeUndeclaredNamesShown by name and the rest by count, and says what to do.
func scopeUndeclaredMessage(agent *agentv1alpha1.PlatformAgent, neighbours []usageClusterRef) string {
	h := agent.Spec.Harness
	names := make([]string, 0, min(len(neighbours), scopeUndeclaredNamesShown))
	for _, ref := range neighbours[:min(len(neighbours), scopeUndeclaredNamesShown)] {
		names = append(names, fmt.Sprintf("%s (%s/%s)", scopeUndeclaredLabel(ref.Name), scopeUndeclaredLabel(ref.Project), scopeUndeclaredLabel(ref.Location)))
	}
	listed := strings.Join(names, ", ")
	if rest := len(neighbours) - len(names); rest > 0 {
		listed += fmt.Sprintf(", and %d more", rest)
	}
	return fmt.Sprintf("spec.scope names no project, folder, organization or selector, so the Cluster Agent reconcile adopts every GKE cluster in the management project %s. "+
		"%d cluster(s) beyond the host cluster %s have a Cluster Agent profile and are watched: %s. %s",
		h.ProjectID, len(neighbours), h.ClusterName, listed, scopeUndeclaredHint)
}

// scopeUndeclaredConditionFor is the condition the neighbours call for. nil means
// remove it: a declared scope, or no neighbour (a host-only reading, an install
// whose host the CR cannot tell apart, or no reading at all).
func scopeUndeclaredConditionFor(agent *agentv1alpha1.PlatformAgent, neighbours []usageClusterRef) *metav1.Condition {
	if scopeDeclared(agent) || len(neighbours) == 0 {
		return nil
	}
	return &metav1.Condition{
		Type:               scopeUndeclaredCondition,
		Status:             metav1.ConditionTrue,
		Reason:             scopeUndeclaredReason,
		ObservedGeneration: agent.Generation,
		Message:            scopeUndeclaredMessage(agent, neighbours),
	}
}

// scopeUndeclaredRemovalCause says why a condition the CR carries no longer
// applies, for the log line that records its removal.
func scopeUndeclaredRemovalCause(agent *agentv1alpha1.PlatformAgent, refs []usageClusterRef) string {
	switch {
	case scopeDeclared(agent):
		return "spec.scope declares a membership"
	case refs == nil:
		return "the operator has no current reading from the event watcher"
	case !scopeHostKnown(agent):
		return "spec.harness does not name the host cluster and its project by ID"
	default:
		return "the watcher registers no cluster beyond the host in the management project"
	}
}

// projectScope writes this poll's ScopeUndeclared condition to the CR: set when
// it is called for and differs from the one there (the message is rewritten when
// the set of adopted clusters changes, so the names on the CR are current),
// removed when it is not. One log line records each write, naming the clusters,
// so the adoption is in the operator's log as well as on the CR. The patch
// carries the resource version, as projectDataVolume's does; a conflict is left
// for the next poll.
func (p *UsageCounterPoller) projectScope(ctx context.Context, agent *agentv1alpha1.PlatformAgent, refs []usageClusterRef, known bool) error {
	if !known {
		return nil
	}
	log := logf.FromContext(ctx).WithName(usagePollerLogName).WithValues("platformagent", client.ObjectKeyFromObject(agent).String())
	base := agent.DeepCopy()
	neighbours := scopeNeighbours(agent, refs)
	want := scopeUndeclaredConditionFor(agent, neighbours)
	existing := meta.FindStatusCondition(agent.Status.Conditions, scopeUndeclaredCondition)
	switch {
	case want == nil:
		if !meta.RemoveStatusCondition(&agent.Status.Conditions, scopeUndeclaredCondition) {
			return nil
		}
		log.Info("the ScopeUndeclared condition is removed", "cause", scopeUndeclaredRemovalCause(agent, refs))
	case existing == nil || existing.Status != want.Status || existing.Reason != want.Reason || existing.Message != want.Message:
		meta.SetStatusCondition(&agent.Status.Conditions, *want)
		named := make([]string, 0, len(neighbours))
		for _, ref := range neighbours {
			named = append(named, scopeUndeclaredLabel(ref.Project)+"/"+scopeUndeclaredLabel(ref.Location)+"/"+scopeUndeclaredLabel(ref.Name))
		}
		log.Info("spec.scope names no project, folder, organization or selector and the Cluster Agent reconcile has adopted clusters beyond the host; the ScopeUndeclared condition names them",
			"clusters", len(neighbours), "names", strings.Join(named, ","))
	default:
		return nil
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		if apierrors.IsConflict(err) {
			log.Info("the CR changed under the scope condition write; it is retried next poll")
			return nil
		}
		return fmt.Errorf("patching the %s condition: %w", scopeUndeclaredCondition, err)
	}
	return nil
}
