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

package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/dynamic"
)

const (
	// joinRequestTimeout bounds one live-object lookup. Ten seconds is far above
	// a healthy GKE control plane's read latency, so a lookup that reaches it has
	// stopped answering rather than being slow.
	//
	// This bounds one lookup and not the batch, which is the limit that actually
	// matters: the pull loop settles a batch only after every record in it has
	// been handled, so enough slow lookups still hold the batch past the Pub/Sub
	// ack deadline and get the whole thing redelivered. The subscriber's
	// joinBudget (--batch-join-budget) is what bounds that, and it is the number
	// to reason about against the deadline -- not this one multiplied by the
	// batch size.
	joinRequestTimeout = 10 * time.Second

	// managedFieldsTimeGranularity is the precision a managedFields timestamp
	// survives at. metav1.Time marshals with time.RFC3339, which has no
	// fractional part, so every entry the API server returns is floored to the
	// whole second -- while an audit record's timestamp comes from Cloud Logging
	// with nanoseconds intact. reconciledBy puts both on this grid before
	// comparing them, so its rule is "a later second" and not "a later instant":
	// the second the change landed in is the one an entry cannot claim from,
	// because an entry sharing it may be the change.
	managedFieldsTimeGranularity = time.Second

	// deleteVerb is the audit verb for a call that removed the object. There is
	// nothing to fetch afterwards, so the join is skipped rather than attempted
	// and reported as a miss.
	deleteVerb = "delete"

	// ownerListSeparator separates rendered fieldOwner claims in a log line.
	ownerListSeparator = " "

	// noOwnersLabel stands in for an object whose managedFields is empty, which
	// is not the same as an object we failed to read. It happens on clusters
	// where nothing has ever used Server-Side Apply against the object.
	noOwnersLabel = "none"

	// maxUnreachableClusters bounds how many distinct cluster identities the
	// shutdown report will name, for the same reason classify.go bounds the
	// unattributed principal list: the key comes from the record, so the number
	// of distinct values is set by what arrives rather than by anything this
	// process controls, and an unbounded map keyed on it grows with the traffic.
	// A project with more than this many clusters has a fleet-wide onboarding
	// gap rather than a list of stragglers to read, so the names stop being the
	// useful part well before the cap.
	maxUnreachableClusters = 128

	// unreachableOverflowLabel collects every cluster past the cap, so the count
	// stays exact even where the names stop.
	unreachableOverflowLabel = "(other clusters; name list capped)"

	// unreachableListSeparator separates rendered cluster entries in the
	// shutdown line, matching unattributedListSeparator, which renders the other
	// name-and-count list that line's neighbour prints.
	unreachableListSeparator = ", "

	// injectHeldOutOfScopeMarker is appended to the DRIFT line of a record the
	// inject will hold because its cluster is outside the install's scope
	// (DriftEvent.OutOfScope). On the DRIFT line rather than only on the hold
	// line that follows it, because the DRIFT line is the one an operator
	// greps for: a record that was seen and not escalated has to be readable
	// as such from that line alone, or every unreachable DRIFT line in a run
	// reads as a card that was sent.
	injectHeldOutOfScopeMarker = "inject=held_out_of_scope"
)

// joinOutcome says what happened when the detector tried to enrich a record
// with the live object's field ownership. Every outcome except joinEnriched is
// still forwarded: by this point T2 has established that a person successfully
// changed declarative state, and that is worth reporting even unenriched.
//
// This is the opposite of T2's disposition, deliberately. The tier filter fails
// closed, dropping anything it cannot prove is a human change, because a false
// report costs an operator's attention. The join fails open, because the thing
// it adds is detail: dropping a confirmed human change on a lookup error would
// discard the finding to protect the annotation on it.
type joinOutcome string

const (
	// joinEnriched is a successful lookup: Owners carries the live object's
	// managedFields.
	joinEnriched joinOutcome = "enriched"

	// joinNoObject is a record there is no object to fetch for -- a delete, or
	// a create whose name the API server had not assigned when it was audited.
	joinNoObject joinOutcome = "no_object"

	// joinGone is a lookup for which the cluster served the path and answered
	// NotFound. The object existed when the call was audited and does not now,
	// so something removed it in between. Still forwarded: the audited change
	// happened, and an object that has since been deleted is a stronger signal
	// than a quiet one, not a weaker.
	//
	// "Served the path" is the load-bearing half -- pathNotServed says why a 404
	// is not on its own enough to conclude this.
	joinGone joinOutcome = "gone"

	// joinUnreachable is a record for a cluster this process has no client for.
	//
	// The fan-in shrank this rather than removing it, and what is left is the
	// useful part: a cluster in the project with no Cluster Agent profile and no
	// credential flag naming it -- one not onboarded, or one whose profile was
	// skipped at discovery. joiner.UnreachableClusters and OutOfScopeClusters
	// name them, one list per disposition, because after the fan-in the count
	// alone no longer says which.
	//
	// Still forwarded, and whether it is then injected is not decided here:
	// DriftEvent.OutOfScope carries that, set when no readable Cluster Agent
	// profile names the cluster, so a detector stays loud about a cluster it
	// was meant to reach and quiet about the rest of the project.
	joinUnreachable joinOutcome = "unreachable"

	// joinFailed is any other lookup error: RBAC, a network fault, a timeout,
	// an API group the cluster does not serve.
	joinFailed joinOutcome = "failed"
)

// joinCounts tallies outcomes for the shutdown report, so a run that enriched
// nothing says why rather than just reporting drift lines with no ownership.
type joinCounts struct {
	Enriched    int
	NoObject    int
	Gone        int
	Unreachable int
	Failed      int

	// OutOfScope is the subset of Unreachable that was marked for the inject
	// to hold -- see DriftEvent.OutOfScope. Not a sixth outcome: the record's
	// Outcome stays joinUnreachable, and this count exists because a run that
	// held every unreachable record and one that forwarded them all print the
	// same Unreachable.
	OutOfScope int
}

// String renders the tally for the shutdown line, always printing every
// outcome. A zero that is absent reads as a category that did not apply; a zero
// that is printed reads as one that did not happen, and those differ.
func (c joinCounts) String() string {
	return fmt.Sprintf("enriched=%d no_object=%d gone=%d unreachable=%d out_of_scope=%d failed=%d",
		c.Enriched, c.NoObject, c.Gone, c.Unreachable, c.OutOfScope, c.Failed)
}

// DriftEvent is an audit record plus what the live object says about it. It is
// the value the inject flattens into a gitops-drift payload.
type DriftEvent struct {
	// Record is the audited change, unchanged from what T2 forwarded.
	Record AuditRecord

	// Outcome says whether Owners was populated, and if not, why not.
	Outcome joinOutcome

	// Owners is the live object's field ownership, empty unless Outcome is
	// joinEnriched.
	Owners []fieldOwner

	// Reconciled reports that a configured GitOps manager wrote to the object in
	// a later second than the audited change -- so whatever the person did has
	// since been overwritten by the GitOps controller and there may be nothing
	// left to revert.
	//
	// False when --gitops-managers is unset, because with nothing configured
	// the detector cannot tell a GitOps controller from any other client and
	// must not guess. False therefore means "not shown to be reconciled", never
	// "shown not to be": the field is a positive claim only.
	Reconciled bool

	// ReconciledBy names the manager behind that claim, empty when Reconciled
	// is false.
	ReconciledBy string

	// LookupError is the error behind a joinFailed outcome, nil otherwise.
	LookupError error

	// OutOfScope is set by the joiner on a joinUnreachable record whose
	// cluster the install's scope does not name: the profiles directory was
	// read and no readable Cluster Agent profile in it carries this identity
	// (scopeIndex says what "readable" covers and how a drop is logged). The
	// record is logged and not escalated: driftInjectHandler.Handle writes
	// the DRIFT line with injectHeldOutOfScopeMarker, counts the hold, and
	// sends nothing.
	//
	// The clusters this describes are the ones in the project the reconcile
	// never onboarded -- excluded by spec.scope, or simply not profiled. The
	// subscription is project-wide, so their records arrive regardless, and
	// each one injected is a card that says a change happened on a cluster
	// nobody profiled, with no owners and no lookup error, spending a unit of
	// the fleet-wide daily drift budget that the profiled clusters then do
	// not have. Past the ceiling the daemon suppresses silently, so a channel
	// gone quiet reads the same as one with nothing to report.
	//
	// Not set for an unreachable record from a cluster a profile does name.
	// Those are inside the scope and the join still could not read them --
	// every profile skipped at discovery for a 403, or a profile the
	// reconcile wrote after discovery ran -- and they are forwarded and
	// injected exactly as before this field existed, because a thin card is
	// the symptom an operator acts on and a silent hold would turn the
	// README's named IAM gap into a detector that looks healthy and reports
	// nothing. Not set either when the scope is unknown: no --profiles-dir,
	// or a directory that cannot be read (scopeIndex has both), and not for
	// a record whose identity is incomplete, which no scope could name. The
	// size of the join is not the test, because a deployed detector always
	// joins its own cluster and so always has one; what separates a scope
	// boundary from a misconfiguration is whether a profile names the cluster.
	OutOfScope bool
}

// driftEventHandler consumes an enriched record. logDriftEvent is one; with
// --daemon-url set the handler is driftInjectHandler.Handle, which calls
// logDriftEvent first and then injects. The inject is layered over the log line
// rather than swapped for it, so the DRIFT lines an operator greps for are the
// same whether or not escalation is on.
type driftEventHandler func(context.Context, DriftEvent)

// objectGetter is the slice of dynamic.Interface the join uses. Narrowing it
// here is what lets the tests drive the join with a stub instead of standing up
// a fake cluster: dynamic.Interface reaches three interfaces deep before it
// gets to Get, and faking that is more code than the thing under test.
type objectGetter interface {
	Get(ctx context.Context, ref ResourceRef) (*unstructured.Unstructured, error)
}

// dynamicGetter adapts a real dynamic client to objectGetter.
type dynamicGetter struct {
	client dynamic.Interface
}

// Get fetches the live object an audit record names.
//
// ResourceRef already carries the group, version and plural resource, which is
// exactly how a dynamic client indexes -- T1 kept the audit log's plural rather
// than converting to a Kind precisely so that no RESTMapper is needed here.
func (g dynamicGetter) Get(ctx context.Context, ref ResourceRef) (*unstructured.Unstructured, error) {
	gvr := schema.GroupVersionResource{Group: ref.Group, Version: ref.Version, Resource: ref.Resource}

	// The subresource is deliberately not requested. A write to "status"
	// changes the parent object, whose managedFields carries the status claim
	// as its own entry, so fetching the parent gets both; asking for the
	// subresource would return a different body with no managedFields at all.
	if ref.Namespace != "" {
		return g.client.Resource(gvr).Namespace(ref.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
	}
	return g.client.Resource(gvr).Get(ctx, ref.Name, metav1.GetOptions{})
}

// clusterIdentity names one GKE cluster completely. All three parts are needed:
// a cluster name is unique within a project and location and nowhere wider, so
// two clusters called "prod" in different regions -- or in different projects --
// are ordinary, and matching on the name alone would read one's audit records
// against the other's objects.
//
// A struct rather than three string parameters because three adjacent strings
// are transposable without the compiler or go vet noticing, which is the hazard
// newFilterFromFlags exists to contain for the classifier's two.
type clusterIdentity struct {
	Project  string
	Location string
	Cluster  string
}

// String renders the identity for the startup line.
func (c clusterIdentity) String() string {
	return fmt.Sprintf("%s/%s/%s", c.Project, c.Location, c.Cluster)
}

// complete reports whether all three parts are present.
//
// An audit record missing any of them cannot be routed to a cluster, and is
// reported unreachable rather than matched. That is the safe direction: the
// alternative is treating an incompletely labelled record as belonging to
// whichever registered cluster happens to share the parts it does carry, and
// enriching it from that cluster's object of the same name.
//
// Keeping the partial identity out of the unreachable-cluster lists is a
// separate job, done by noteCluster: refusing the record here produces the
// same joinUnreachable outcome a missed lookup would, so this test cannot be
// what stops "proj//" being named there.
func (c clusterIdentity) complete() bool {
	return c.Project != "" && c.Location != "" && c.Cluster != ""
}

// recordIdentity reads the cluster an audit record was produced on.
//
// The three fields come from resource.labels on the Cloud Logging entry, which
// GKE sets from the control plane that served the call -- so this is the
// cluster's own account of itself, not an inference from the object.
func recordIdentity(record AuditRecord) clusterIdentity {
	return clusterIdentity{
		Project:  record.Project,
		Location: record.Location,
		Cluster:  record.Cluster,
	}
}

// joiner is T3's handler: for each record T2 forwards, it reads the live
// object's field ownership and passes both on.
//
// Not safe for concurrent use, for the same reason driftFilter is not: counts
// is a plain struct and Handle mutates it. Unlike driftFilter this one does
// per-record network I/O, so it is the obvious place to want a worker pool --
// which is exactly why the constraint is written down here rather than left to
// be rediscovered.
type joiner struct {
	// clusters routes a record to the client that can read its cluster, keyed
	// by the identity the record carries. A record whose cluster is absent is
	// joinUnreachable -- never served by another entry, because a same-named
	// object on the wrong cluster is a different object entirely, which is the
	// failure this map's key exists to prevent.
	//
	// Empty disables the join: every record naming a live object is forwarded
	// unreachable, which is the mode the detector runs in with no credential
	// flags and no --profiles-dir.
	clusters map[clusterIdentity]objectGetter

	// gitopsManagers are the field managers that are the GitOps controller.
	// Empty means unconfigured, and an unconfigured detector makes no
	// reconciliation claim at all.
	gitopsManagers map[string]bool

	// timeout bounds one lookup.
	timeout time.Duration

	next   driftEventHandler
	counts joinCounts

	// scope says whether a Cluster Agent profile names a cluster, for the
	// out-of-scope hold on an unreachable record. nil when the detector was
	// started without --profiles-dir, and then nothing is ever held.
	scope scopeIndex

	// injected reports whether the inject already sent a card for an insertId,
	// so a record redelivered after its cluster left the scope is not marked
	// held -- the inject would count it a duplicate and send nothing, and a
	// DRIFT line carrying the hold marker, a held count and a named cluster
	// would all say a card was withheld that was in fact sent. realMain always
	// wires the inject handler's hook, which answers false while the inject is
	// off; nil is the joiner built without one, in tests, and means never.
	injected func(insertID string) bool

	// unreachable counts records per cluster this process cannot read and did
	// not hold, and held counts the ones it did (DriftEvent.OutOfScope), so
	// the shutdown report can name each set with what became of its records.
	// Each capped at maxUnreachableClusters; past that, new clusters are
	// counted under unreachableOverflowLabel.
	unreachable map[string]int
	held        map[string]int
}

// newJoiner builds the handler. An empty or nil cluster set is legal and
// documented on the field: the detector supports running with no cluster access
// at all. A nil scope is legal for the same reason and documented on its field.
func newJoiner(clusters map[clusterIdentity]objectGetter, gitopsManagers map[string]bool, scope scopeIndex, injected func(insertID string) bool, next driftEventHandler) *joiner {
	return &joiner{
		clusters:       clusters,
		gitopsManagers: gitopsManagers,
		scope:          scope,
		injected:       injected,
		timeout:        joinRequestTimeout,
		next:           next,
		unreachable:    map[string]int{},
		held:           map[string]int{},
	}
}

// Clusters reports how many clusters the join can read, for the startup line.
func (j *joiner) Clusters() int { return len(j.clusters) }

// parseGitopsManagers splits the --gitops-managers flag into a set, reusing
// splitCSV so that this flag trims and drops empties exactly as T2's two
// principal lists do.
//
// Values are matched exactly and case-sensitively. A field manager is a free
// string the client chooses, not a DNS name, so there is no case-folding rule
// to appeal to here the way there is for a service-account domain -- and
// "argocd-controller" and "ArgoCD-Controller" really can be two clients.
func parseGitopsManagers(value string) map[string]bool {
	names := splitCSV(value)
	if len(names) == 0 {
		return nil
	}
	set := make(map[string]bool, len(names))
	for _, name := range names {
		set[name] = true
	}
	return set
}

// Handle enriches one record and forwards it.
func (j *joiner) Handle(ctx context.Context, record AuditRecord) {
	event := j.join(ctx, record)

	switch event.Outcome {
	case joinEnriched:
		j.counts.Enriched++
	case joinNoObject:
		j.counts.NoObject++
	case joinGone:
		j.counts.Gone++
	case joinUnreachable:
		j.counts.Unreachable++
		// Marked here and not in join, which classifies one record against
		// the routing table: whether a cluster the table lacks is outside the
		// install's scope is a question for the profiles directory, not the
		// table, and the field's doc comment has the argument for asking it
		// this way.
		identity := recordIdentity(record)
		if j.outOfScope(identity) && !j.alreadyInjected(record.InsertID) {
			event.OutOfScope = true
			j.counts.OutOfScope++
			j.noteCluster(j.held, identity)
		} else {
			j.noteCluster(j.unreachable, identity)
		}
	case joinFailed:
		j.counts.Failed++
	}

	j.next(ctx, event)
}

// alreadyInjected is the injected hook with nil meaning never.
func (j *joiner) alreadyInjected(insertID string) bool {
	return j.injected != nil && j.injected(insertID)
}

// outOfScope decides the hold for one unreachable record: true only when the
// scope is known and names no profile for the cluster. DriftEvent.OutOfScope
// lists the cases that answer false and why each must.
func (j *joiner) outOfScope(identity clusterIdentity) bool {
	if j.scope == nil || !identity.complete() {
		return false
	}
	profiled, known := j.scope.Profiled(identity)
	return known && !profiled
}

// noteCluster records one sighting of a cluster this process has no client
// for, in whichever of the two shutdown lists the record's disposition puts
// it, so UnreachableClusters or OutOfScopeClusters can name it.
//
// Keyed on the rendered identity rather than the struct because the overflow
// bucket is a label and not a cluster, and a map keyed on clusterIdentity has
// nowhere to put it.
//
// An incomplete identity is counted in Unreachable but not named here. The list
// is read as clusters to go and onboard, and a record whose resource.labels are
// missing a part renders as "proj//", which is not a cluster anyone can act on.
// The count still moves, so nothing is hidden -- a gap between the unreachable
// total and the sum of the named entries is how this shows up.
func (j *joiner) noteCluster(counts map[string]int, identity clusterIdentity) {
	if !identity.complete() {
		return
	}
	name := identity.String()
	// A cluster already being counted keeps counting past the cap; only a new
	// name folds into the overflow bucket, which is how classify.go bounds its
	// principal set without distorting the counts it already holds.
	if _, known := counts[name]; !known && len(counts) >= maxUnreachableClusters {
		name = unreachableOverflowLabel
	}
	counts[name]++
}

// UnreachableClusters renders "cluster=count" entries for the shutdown line,
// most frequent first: the clusters the join could not read whose records were
// forwarded without ownership, because a profile names them or the scope was
// never known.
//
// The count alone says how much the join missed; this says which cluster to go
// and look at, which is the difference between a number and an action. Empty
// when nothing was missed, so the caller can leave the clause off entirely.
func (j *joiner) UnreachableClusters() []string { return renderClusterCounts(j.unreachable) }

// OutOfScopeClusters is UnreachableClusters for the held set: the clusters no
// readable profile names, whose records were logged and not escalated. These
// are the ones to onboard, to exclude, or whose profile the scope's log named
// as not reading.
func (j *joiner) OutOfScopeClusters() []string { return renderClusterCounts(j.held) }

func renderClusterCounts(counts map[string]int) []string {
	names := make([]string, 0, len(counts))
	for name := range counts {
		names = append(names, name)
	}
	sort.Slice(names, func(a, b int) bool {
		if counts[names[a]] != counts[names[b]] {
			return counts[names[a]] > counts[names[b]]
		}
		return names[a] < names[b]
	})
	out := make([]string, 0, len(names))
	for _, name := range names {
		// %q for the same reason classify.go quotes a principal: the project,
		// location and cluster come out of the audit record rather than from
		// anything this process validated, so an unquoted one could forge what
		// reads as a separate log line.
		out = append(out, fmt.Sprintf("%q=%d", name, counts[name]))
	}
	return out
}

// join performs the lookup and classifies the result. Split from Handle so the
// outcome logic is testable without going through the counters.
func (j *joiner) join(ctx context.Context, record AuditRecord) DriftEvent {
	event := DriftEvent{Record: record}

	// No object to fetch, for either of two unrelated reasons. A delete removed
	// it, so the name parseResourceName found addresses nothing to look up --
	// that is the verb test, not the name test. A generateName create carries
	// no name at all, because the API server had not assigned one when the call
	// was audited, and that is the name test. Neither is a failure: the audited
	// change still stands on its own.
	//
	// This runs ahead of the credential check below, so a delete is counted
	// no_object whether or not the join has a cluster to read.
	if record.Verb == deleteVerb || record.Resource.Name == "" {
		event.Outcome = joinNoObject
		return event
	}

	// An incomplete identity is refused rather than left to miss on its own.
	// Every registered key is complete today -- the direct one is validated at
	// startup and ReadIdentity rejects a partial cluster_identity -- so this
	// changes no outcome now; it is here so that a record carrying only a
	// project can never be served by a cluster that happens to share the parts
	// it does carry.
	identity := recordIdentity(record)
	getter, ok := j.clusters[identity]
	if !ok || !identity.complete() {
		event.Outcome = joinUnreachable
		return event
	}

	lookupCtx, cancel := context.WithTimeout(ctx, j.timeout)
	defer cancel()

	obj, err := getter.Get(lookupCtx, record.Resource)
	switch {
	case apierrors.IsNotFound(err) && !pathNotServed(err):
		event.Outcome = joinGone
		return event
	case err != nil:
		event.Outcome = joinFailed
		event.LookupError = err
		return event
	}

	event.Outcome = joinEnriched
	event.Owners = ownership(obj)
	event.Reconciled, event.ReconciledBy = j.reconciledBy(event.Owners, record.Timestamp)
	return event
}

// pathNotServed reports whether a NotFound says the cluster does not serve the
// request path at all, rather than that the object is absent from it.
//
// Both arrive as a 404 and both satisfy apierrors.IsNotFound, which classifies
// on the status code alone. What separates them is whether the Status names an
// object. A genuine absence is built by apierrors.NewNotFound and carries the
// group, resource and name that were looked up. A refusal of the path comes
// back through NewGenericServerResponse with an empty GroupResource, no name,
// and the message "the server could not find the requested resource".
//
// The name is read rather than the CauseTypeUnexpectedServerResponse cause,
// because that cause is set only when client-go synthesises the error from a
// body that was not a Status, and the four ways a path can be unserved do not
// agree on that. Measured against a GKE control plane: an unserved API group
// and a CRD version whose `served` was turned off reach the mux and carry the
// cause; an unserved version of a served group and an unserved resource of a
// served group are answered by the group's own handler with a real Status and
// carry no causes. All four leave the name empty.
//
// This matters here and not in most clients because the join has no RESTMapper
// -- the group, version and resource come straight out of the audit record, so
// nothing has checked that the cluster still serves them. A CRD uninstalled, or
// a served version retired, between the audited write and the lookup reaches
// this point as a 404, and counting it gone would report that the object was
// deleted about an object that is standing under another version. Reported
// failed instead, with the error attached, which is the outcome that says the
// lookup did not answer the question. An unrecognised shape falls that way too:
// a Status that does not name what it could not find has not established that
// the object is gone.
func pathNotServed(err error) bool {
	var status apierrors.APIStatus
	if !errors.As(err, &status) {
		return false
	}
	details := status.Status().Details
	return details == nil || details.Name == ""
}

// reconciledBy reports whether a configured GitOps manager wrote to the object
// in a later second than the audited change.
//
// Both sides are put on the same grid first, because they are not recorded at
// the same precision. A managedFields time arrives floored to the second
// (managedFieldsTimeGranularity), an audit time arrives from Cloud Logging with
// nanoseconds, and comparing them raw would make a reconcile at 12:00:01.2 --
// stored as 12:00:01 -- sort before a change at 12:00:00.6 only by accident of
// how much of the second had elapsed. Flooring the audit side removes that.
//
// On that grid the comparison is strictly after, not at-or-after, and the loop
// below says why: an entry sharing the change's second may be the change. That
// costs the reconcile that lands inside the same second and buys the guarantee
// that a write can never answer itself.
//
// This is deliberately the weakest claim the data supports. It does not say the
// person's change was reverted -- the GitOps controller may have written an
// unrelated field, and managedFields keeps no history of the value that was
// there before. Deciding what actually happened is the agent's job; this flag
// exists so the agent is told when there is reason to look.
//
// Weak is not the same as cheap, though, and the loop below declines four
// things outright: a change with no time, a manager with no time, a claim made
// through a subresource, and a write in the audited change's own second. The
// last two are the ones that would otherwise fire constantly rather than
// rarely -- see the comments on them.
func (j *joiner) reconciledBy(owners []fieldOwner, changedAt time.Time) (bool, string) {
	// Redundant with the loop below -- a nil map returns false for every
	// lookup, so every owner would be skipped anyway -- and kept because it is
	// where the contract is legible. No test can tell the two apart, and a
	// mutation pass will say so.
	if len(j.gitopsManagers) == 0 {
		return false, ""
	}
	// A change with no recorded time cannot be placed against anything, so no
	// write can be shown to have followed it and the honest answer is no claim.
	//
	// This is the half the comparison gets backwards rather than merely misses.
	// The zero time sorts before every real one, so a zero changedAt puts every
	// configured manager that has ever touched the object strictly after it, and
	// the first one encountered is reported as having reconciled a change whose
	// time the detector does not know -- the "guess after, hide real drift"
	// outcome the rest of this function refuses to make, arriving through the
	// input rather than the logic.
	//
	// Reachable without anything being malformed. Timestamp is a plain
	// time.Time under `json:"timestamp"`, so a payload that omits the key, or
	// sends null, decodes to the zero time and no error -- parseAuditEntry has
	// no non-zero requirement to fail. A *malformed* timestamp is the case that
	// never gets here, because json.Unmarshal rejects it and the record is
	// nacked.
	if changedAt.IsZero() {
		return false, ""
	}
	for _, owner := range owners {
		if !j.gitopsManagers[owner.Manager] {
			continue
		}
		// A claim made through a subresource is not the reconcile. ownership()
		// keeps a subresource write as its own fieldOwner, and every GitOps
		// controller writes .status on its own custom resources -- a
		// Kustomization, a HelmRelease, an Application -- under the same manager
		// name, on every loop, seconds apart. Without this the systematic case
		// is the wrong one: `flux suspend kustomization apps` patches
		// spec.suspend, the controller's next status write lands a few seconds
		// after the audited change, and the event goes out Reconciled while the
		// person's change stands untouched. That is the reading the inject
		// carries as reconciled=true.
		//
		// Skipping the entry rather than requiring it to match the audited
		// change's own subresource, which looks sharper and is wrong for
		// `kubectl scale`: the audit record carries subresource "scale", but the
		// controller reconciles it by re-applying the object, whose entry has no
		// subresource at all, so matching the two would decline a real
		// reconcile. The cost is the reverse case -- a person patching /status
		// directly, answered by the controller's own status write -- where this
		// declines a claim that was true. Declining reports the drift, which is
		// the direction every other judgment in this function fails in.
		if owner.Subresource != "" {
			continue
		}
		// A manager with no recorded time cannot be placed relative to the
		// change either, and guessing in either direction is worse than not
		// claiming: guessing "after" hides real drift, guessing "before"
		// invents a reconcile that never happened.
		//
		// Redundant against the comparison below, now that a zero changedAt
		// returns above: a zero UpdatedAt sorts before any real changedAt, so
		// it reads as "before" and declines the claim anyway. Kept because the
		// two zero cases are one rule and splitting it across a guard and an
		// accident of ordering is how the second one came to be missing. No
		// test can tell this line from its absence, and a mutation pass will
		// say so.
		if owner.UpdatedAt.IsZero() {
			continue
		}
		// Strictly after, on the floored grid: a write in the *same* second as
		// the audited change cannot be shown to be a different write from it.
		//
		// Manager is self-declared -- ownership() copies whatever the client put
		// in --field-manager -- so a person running
		// `kubectl apply --server-side --field-manager=kustomize-controller`
		// produces one entry carrying the configured name, no subresource, and a
		// time floored into the audited change's own second. Under "at or after"
		// that entry satisfies the claim by construction, and the change goes out
		// Reconciled on the strength of being itself. It needs no intent to
		// happen: configure a manager name ordinary kubectl also emits, as an
		// older Argo CD install's kubectl-client-side-apply is, and every human
		// apply self-marks.
		//
		// The price is the reconcile that genuinely lands inside the same second,
		// which is now missed. That is the case the flooring was added for, and
		// giving it up buys the guarantee that the audited write can never be its
		// own answer -- a false claim suppresses the report, a missed one merely
		// leaves it noisy, and this function fails in the second direction
		// everywhere else. A reconcile that crosses the second boundary, which is
		// the ordinary one, still claims.
		//
		// Both sides floored, which is what makes the line read as "a later
		// second" rather than "a later instant". Either truncation alone gives
		// the same answers for the values that actually arrive here, so a
		// mutation pass will call each one redundant: the manager's side is
		// already whole-second, and a whole second later than floor(changedAt) is
		// later than changedAt too. The manager's side stops being redundant the
		// moment a sub-second UpdatedAt reaches here from anywhere -- floor the
		// audit side only, and 12:00:00.9 answers 12:00:00.2 and the self-claim
		// above is back -- which is the case the test pins. The audit side stays
		// because half a rule stated in code and half in a comment is how the
		// other half goes missing.
		if owner.UpdatedAt.Truncate(managedFieldsTimeGranularity).After(changedAt.Truncate(managedFieldsTimeGranularity)) {
			return true, owner.Manager
		}
	}
	return false, ""
}

// Counts reports the join tally so far.
func (j *joiner) Counts() joinCounts {
	return j.counts
}

// logDriftEvent is the terminal handler, replacing T2's logActionable. It stays
// the terminal handler with the inject on: driftInjectHandler.Handle calls it
// before sending, so a record whose inject fails is still on stdout.
func logDriftEvent(_ context.Context, event DriftEvent) {
	record := event.Record

	// The record half of the line is unchanged from T2's, deliberately: an
	// operator grepping for DRIFT lines across a version boundary should not
	// have to learn a second format, and everything T3 adds is appended rather
	// than interleaved.
	//
	// method= carries the fully qualified audit method, which is the only field
	// here that names the API group and version: Resource.String() renders
	// namespace/resource/name/subresource and drops both, so without it a DRIFT
	// line for "prod/widgets/foo" cannot be told from a same-named CRD in
	// another group. It is also the string to paste back into a
	// `gcloud logging read` filter to find the entry again.
	line := fmt.Sprintf("%s: DRIFT cluster=%s project=%s location=%s principal=%q method=%s verb=%s resource=%s user_agent=%q timestamp=%s insert_id=%s join=%s",
		commandName,
		record.Cluster,
		record.Project,
		record.Location,
		record.Principal,
		record.MethodName,
		record.Verb,
		record.Resource,
		record.UserAgent,
		record.Timestamp.Format(time.RFC3339),
		record.InsertID,
		event.Outcome,
	)

	if event.Outcome == joinEnriched {
		line += fmt.Sprintf(" owners=[%s]", renderOwners(event.Owners))
		if event.Reconciled {
			line += fmt.Sprintf(" reconciled_by=%q", event.ReconciledBy)
		}
	}
	if event.LookupError != nil {
		line += fmt.Sprintf(" lookup_error=%q", event.LookupError)
	}
	// Appended here, where the DRIFT line is built, rather than by the inject
	// handler that acts on the flag: the line is identical with the inject on
	// and off, and the marker says what the handler will do with the record,
	// which is the fact an operator reading only DRIFT lines needs.
	if event.OutOfScope {
		line += " " + injectHeldOutOfScopeMarker
	}

	log.Print(line)
}

// renderOwners formats the ownership list for a log line.
//
// Each claim is quoted, because neither half of one is constrained to be
// space-free: a field manager is validated only for length and printable
// characters, so "my tool" is legal, and a merge key renders the object's own
// field value, so spec.containers[name=my app].image is too. Unquoted and joined
// on a space, either would read as two claims to anything splitting the list.
// Quoting also matches the %q every other site uses for a caller-influenced
// name.
func renderOwners(owners []fieldOwner) string {
	if len(owners) == 0 {
		return noOwnersLabel
	}
	rendered := make([]string, 0, len(owners))
	for _, owner := range owners {
		rendered = append(rendered, fmt.Sprintf("%q", owner.String()))
	}
	return strings.Join(rendered, ownerListSeparator)
}
