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
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"strings"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	dynamicfake "k8s.io/client-go/dynamic/fake"
)

// stubGetter is the objectGetter the join is driven with. Narrowing
// dynamic.Interface to one method is what makes this three lines instead of a
// fake cluster.
type stubGetter struct {
	obj  *unstructured.Unstructured
	err  error
	refs []ResourceRef
	// block, when set, is waited on before returning, so a test can prove the
	// lookup context is bounded.
	block <-chan struct{}
}

func (g *stubGetter) Get(ctx context.Context, ref ResourceRef) (*unstructured.Unstructured, error) {
	g.refs = append(g.refs, ref)
	// Checked first, and whether or not block is set, because a stub that
	// consults ctx only while blocking cannot tell an expired lookup context
	// from a live one. Dropping the timeout line from newJoiner leaves every
	// joiner at a zero timeout, which is an already-expired deadline and fails
	// every real GET with "context deadline exceeded" -- and without this the
	// stub would hand back the object regardless and the whole suite would stay
	// green. The deadline the join sets is only as load-bearing as this line.
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if g.block != nil {
		select {
		case <-g.block:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return g.obj, g.err
}

// joinRecord is a record that reaches the lookup: named object, non-delete
// verb, and the joiner's own cluster.
func joinRecord() AuditRecord {
	return AuditRecord{
		Principal: "ada@example.com",
		Cluster:   "prod-a",
		Project:   "example-project",
		Location:  "us-central1",
		Verb:      "patch",
		Timestamp: time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC),
		Resource: ResourceRef{
			Group:     "apps",
			Version:   "v1",
			Namespace: "prod",
			Resource:  "deployments",
			Name:      "api",
		},
	}
}

// joinCluster is the identity joinRecord's cluster fields spell out, so a
// joiner built with it treats that record as local.
func joinCluster() clusterIdentity {
	return clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-a"}
}

// joinSet is the one-cluster routing table most of these tests want: joinRecord
// resolves to getter and nothing else does.
//
// A nil getter builds an empty set rather than an entry pointing at nil,
// because that is what the production path produces -- buildClusterSet skips a
// nil direct getter instead of registering it -- and an entry holding nil would
// be found by the lookup and then panic on Get, testing a state the binary
// cannot reach.
func joinSet(getter objectGetter) map[clusterIdentity]objectGetter {
	if getter == nil {
		return nil
	}
	return map[clusterIdentity]objectGetter{joinCluster(): getter}
}

// notFound is the error a dynamic client returns for a missing object.
func notFound() error {
	return apierrors.NewNotFound(schema.GroupResource{Group: "apps", Resource: "deployments"}, "api")
}

// unservedPathFromMux is the error client-go builds when the request never
// reaches an API group's handler and the mux answers in plain text, which is
// what an unserved group and a retired CRD version do. rest.Request synthesises
// it with isUnexpectedResponse set, so it carries a cause -- and, like every
// other refusal of the path, no name.
func unservedPathFromMux() error {
	return apierrors.NewGenericServerResponse(
		404, "get", schema.GroupResource{}, "", "404 page not found", 0, true,
	)
}

// unservedPathFromGroupHandler is the same refusal from the other direction: an
// unserved version or resource of a group the cluster does serve, answered with
// a real Status and therefore no cause. Both shapes were read off a GKE control
// plane; the empty name is the only thing they have in common, which is why
// pathNotServed reads that and not the cause.
func unservedPathFromGroupHandler() error {
	return apierrors.NewGenericServerResponse(
		404, "get", schema.GroupResource{}, "", "", 0, false,
	)
}

func TestJoinOutcomes(t *testing.T) {
	obj := managedFieldsObject(entry("kubectl-edit", "Update", "", `{"f:spec":{"f:replicas":{}}}`, nil))

	for _, tc := range []struct {
		name   string
		getter objectGetter
		mutate func(*AuditRecord)
		want   joinOutcome
		// wantLookup says whether the getter should have been called at all.
		wantLookup bool
	}{
		{
			name:       "a successful lookup enriches",
			getter:     &stubGetter{obj: obj},
			want:       joinEnriched,
			wantLookup: true,
		},
		{
			name:   "a delete has no object left to fetch",
			getter: &stubGetter{obj: obj},
			mutate: func(r *AuditRecord) { r.Verb = deleteVerb },
			want:   joinNoObject,
		},
		{
			name:   "a create with no assigned name cannot be addressed",
			getter: &stubGetter{obj: obj},
			mutate: func(r *AuditRecord) { r.Resource.Name = "" },
			want:   joinNoObject,
		},
		{
			name:   "a record from a differently named cluster is not looked up here",
			getter: &stubGetter{obj: obj},
			mutate: func(r *AuditRecord) { r.Cluster = "prod-b" },
			want:   joinUnreachable,
		},
		{
			// The case a name-only guard gets wrong. A GKE cluster name is
			// unique within a project and location, so "prod-a" in two regions
			// is ordinary -- and the lookup would succeed, returning a real
			// object of that name whose ownership belongs to a different
			// cluster entirely. Nothing downstream could tell.
			name:   "a same-named cluster in another location is a different cluster",
			getter: &stubGetter{obj: obj},
			mutate: func(r *AuditRecord) { r.Location = "europe-west1" },
			want:   joinUnreachable,
		},
		{
			name:   "a same-named cluster in another project is a different cluster",
			getter: &stubGetter{obj: obj},
			mutate: func(r *AuditRecord) { r.Project = "other-project" },
			want:   joinUnreachable,
		},
		{
			// An incompletely labelled record is refused rather than assumed
			// local: the alternative is enriching it from whatever object of
			// that name this cluster happens to hold.
			name:   "a record with no location cannot be placed",
			getter: &stubGetter{obj: obj},
			mutate: func(r *AuditRecord) { r.Location = "" },
			want:   joinUnreachable,
		},
		{
			name:   "an empty cluster set means no join",
			getter: nil,
			want:   joinUnreachable,
		},
		{
			name:       "NotFound means the object has since been removed",
			getter:     &stubGetter{err: notFound()},
			want:       joinGone,
			wantLookup: true,
		},
		{
			name:       "a NotFound from a path the cluster does not serve is a failed lookup, not a removal",
			getter:     &stubGetter{err: unservedPathFromMux()},
			want:       joinFailed,
			wantLookup: true,
		},
		{
			name:       "and so is the same refusal answered by the group's own handler",
			getter:     &stubGetter{err: unservedPathFromGroupHandler()},
			want:       joinFailed,
			wantLookup: true,
		},
		{
			name:       "any other error is a failed lookup",
			getter:     &stubGetter{err: errors.New("forbidden")},
			want:       joinFailed,
			wantLookup: true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			record := joinRecord()
			if tc.mutate != nil {
				tc.mutate(&record)
			}

			var forwarded []DriftEvent
			j := newJoiner(joinSet(tc.getter), nil, func(_ context.Context, e DriftEvent) {
				forwarded = append(forwarded, e)
			})
			j.Handle(context.Background(), record)

			if len(forwarded) != 1 {
				t.Fatalf("forwarded %d events, want 1 -- the join fails open and forwards every outcome", len(forwarded))
			}
			if got := forwarded[0].Outcome; got != tc.want {
				t.Errorf("Outcome = %q, want %q", got, tc.want)
			}
			if forwarded[0].Record.Principal != record.Principal {
				t.Errorf("Record.Principal = %q, want the record forwarded unchanged", forwarded[0].Record.Principal)
			}

			if stub, ok := tc.getter.(*stubGetter); ok {
				if got := len(stub.refs) > 0; got != tc.wantLookup {
					t.Errorf("lookup performed = %v, want %v", got, tc.wantLookup)
				}
			}
		})
	}
}

func TestJoinForwardsTheLookupError(t *testing.T) {
	wantErr := errors.New("deployments.apps is forbidden")
	var got DriftEvent
	j := newJoiner(joinSet(&stubGetter{err: wantErr}), nil, func(_ context.Context, e DriftEvent) { got = e })
	j.Handle(context.Background(), joinRecord())

	if !errors.Is(got.LookupError, wantErr) {
		t.Errorf("LookupError = %v, want %v -- a failed join has to say why", got.LookupError, wantErr)
	}
}

func TestJoinPassesTheAuditResourceStraightThrough(t *testing.T) {
	// T1 kept the audit log's plural resource rather than converting to a Kind
	// so that no RESTMapper is needed here. If that ever changes, this is the
	// test that says what depended on it.
	stub := &stubGetter{obj: &unstructured.Unstructured{}}
	j := newJoiner(joinSet(stub), nil, func(context.Context, DriftEvent) {})
	record := joinRecord()
	j.Handle(context.Background(), record)

	if len(stub.refs) != 1 {
		t.Fatalf("getter called %d times, want 1", len(stub.refs))
	}
	if stub.refs[0] != record.Resource {
		t.Errorf("getter received %+v, want %+v", stub.refs[0], record.Resource)
	}
}

func TestJoinCountsEveryOutcome(t *testing.T) {
	j := newJoiner(joinSet(&stubGetter{err: notFound()}), nil, func(context.Context, DriftEvent) {})

	j.Handle(context.Background(), joinRecord()) // gone

	elsewhere := joinRecord()
	elsewhere.Cluster = "prod-b"
	j.Handle(context.Background(), elsewhere) // unreachable

	removed := joinRecord()
	removed.Verb = deleteVerb
	j.Handle(context.Background(), removed) // no_object
	j.Handle(context.Background(), removed) // no_object

	counts := j.Counts()
	if counts.Gone != 1 || counts.Unreachable != 1 || counts.NoObject != 2 {
		t.Errorf("counts = %+v, want gone=1 unreachable=1 no_object=2", counts)
	}
	if counts.Enriched != 0 || counts.Failed != 0 {
		t.Errorf("counts = %+v, want enriched and failed at zero", counts)
	}
}

func TestJoinCountsStringAlwaysNamesEveryOutcome(t *testing.T) {
	// A zero that is absent reads as a category that did not apply; a zero that
	// is printed reads as one that did not happen.
	got := joinCounts{}.String()
	for _, outcome := range []joinOutcome{joinEnriched, joinNoObject, joinGone, joinUnreachable, joinFailed} {
		if !strings.Contains(got, string(outcome)+"=") {
			t.Errorf("joinCounts.String() = %q, want it to name %q", got, outcome)
		}
	}
}

func TestJoinBoundsTheLookup(t *testing.T) {
	// A control plane that has stopped answering must not hold the batch past
	// the Pub/Sub ack deadline, so the lookup carries its own timeout rather
	// than inheriting only the pull loop's context.
	// The default is asserted separately from the bound. Overriding j.timeout
	// below is what makes the rest of this test finish in milliseconds, but it
	// also means the rest of it would pass against a joiner whose default was
	// never set -- so the value the running binary actually uses is checked
	// here, where newJoiner is the only place it comes from.
	if def := newJoiner(joinSet(nil), nil, nil).timeout; def != joinRequestTimeout {
		t.Errorf("newJoiner timeout = %s, want %s", def, joinRequestTimeout)
	}

	blocked := make(chan struct{})
	defer close(blocked)

	var got DriftEvent
	j := newJoiner(joinSet(&stubGetter{block: blocked}), nil, func(_ context.Context, e DriftEvent) { got = e })
	j.timeout = time.Millisecond

	done := make(chan struct{})
	go func() {
		defer close(done)
		j.Handle(context.Background(), joinRecord())
	}()

	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Handle did not return: the lookup is not bounded by joiner.timeout")
	}
	if got.Outcome != joinFailed {
		t.Errorf("Outcome = %q, want %q for a lookup that timed out", got.Outcome, joinFailed)
	}
}

func TestReconciledBy(t *testing.T) {
	changedAt := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)
	before := changedAt.Add(-time.Hour)
	after := changedAt.Add(time.Minute)

	for _, tc := range []struct {
		name        string
		managers    map[string]bool
		owners      []fieldOwner
		wantClaim   bool
		wantManager string
	}{
		{
			name:        "a configured manager writing after the change is a reconcile",
			managers:    map[string]bool{"argocd-controller": true},
			owners:      []fieldOwner{{Manager: "argocd-controller", UpdatedAt: after}},
			wantClaim:   true,
			wantManager: "argocd-controller",
		},
		{
			name:     "a write in the change's own second does not count, because it may be the change",
			managers: map[string]bool{"argocd-controller": true},
			owners:   []fieldOwner{{Manager: "argocd-controller", UpdatedAt: changedAt}},
			// Manager is self-declared, so a person applying with
			// --field-manager=argocd-controller produces exactly this entry.
			wantClaim: false,
		},
		{
			name:      "a configured manager that last wrote before the change is not a reconcile",
			managers:  map[string]bool{"argocd-controller": true},
			owners:    []fieldOwner{{Manager: "argocd-controller", UpdatedAt: before}},
			wantClaim: false,
		},
		{
			name:      "an unconfigured detector makes no claim at all",
			managers:  nil,
			owners:    []fieldOwner{{Manager: "argocd-controller", UpdatedAt: after}},
			wantClaim: false,
		},
		{
			name:      "a manager that is not the configured one does not count",
			managers:  map[string]bool{"argocd-controller": true},
			owners:    []fieldOwner{{Manager: "kubectl-edit", UpdatedAt: after}},
			wantClaim: false,
		},
		{
			name:     "a configured manager with no recorded time is skipped, not guessed at",
			managers: map[string]bool{"argocd-controller": true},
			owners:   []fieldOwner{{Manager: "argocd-controller"}},
			// Guessing "after" hides real drift; guessing "before" invents a
			// reconcile that never happened.
			wantClaim: false,
		},
		{
			name:     "matching is case-sensitive, because a manager name is not a DNS name",
			managers: map[string]bool{"argocd-controller": true},
			owners:   []fieldOwner{{Manager: "ArgoCD-Controller", UpdatedAt: after}},
			// Two clients really can differ only in case.
			wantClaim: false,
		},
		{
			name:     "the claim names the manager behind it, not the first owner",
			managers: map[string]bool{"flux": true},
			owners: []fieldOwner{
				{Manager: "kubectl-edit", UpdatedAt: after},
				{Manager: "flux", UpdatedAt: after},
			},
			wantClaim:   true,
			wantManager: "flux",
		},
		{
			name:     "a configured manager's status write is not the reconcile",
			managers: map[string]bool{"kustomize-controller": true},
			owners:   []fieldOwner{{Manager: "kustomize-controller", Subresource: "status", UpdatedAt: after}},
			// The systematic case, not an edge one: a GitOps controller writes
			// .status on its own custom resources every loop, under the same
			// manager name, so without the subresource skip a change to spec is
			// marked reconciled within seconds of being made.
			wantClaim: false,
		},
		{
			name:     "a status entry does not hide the same manager's write to the object",
			managers: map[string]bool{"kustomize-controller": true},
			owners: []fieldOwner{
				{Manager: "kustomize-controller", Subresource: "status", UpdatedAt: after},
				{Manager: "kustomize-controller", UpdatedAt: after},
			},
			// Skipping the status entry costs no real claim, because an apply to
			// the object is its own fieldOwner. Pinned so the skip cannot be
			// widened into "this manager is ignored".
			wantClaim:   true,
			wantManager: "kustomize-controller",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			j := newJoiner(joinSet(nil), tc.managers, func(context.Context, DriftEvent) {})
			claim, manager := j.reconciledBy(tc.owners, changedAt)

			if claim != tc.wantClaim {
				t.Errorf("Reconciled = %v, want %v", claim, tc.wantClaim)
			}
			if manager != tc.wantManager {
				t.Errorf("ReconciledBy = %q, want %q", manager, tc.wantManager)
			}
			if !claim && manager != "" {
				t.Errorf("ReconciledBy = %q with no claim, want empty", manager)
			}
		})
	}
}

func TestReconciledByIgnoresAStatusWriteAnsweringASpecChange(t *testing.T) {
	// The whole scenario end to end, because the table above states the rule and
	// this states why it matters. A person runs `flux suspend kustomization
	// apps`, which patches spec.suspend -- exactly the out-of-band change worth
	// paging on. The controller's next loop writes .status on the same object,
	// seconds later, under the manager name --gitops-managers configures.
	//
	// Reported as reconciled, that change reaches the card carrying a claim
	// that is false. Reconciled does not suppress the inject -- every surviving
	// record is escalated -- it sets the "Possibly already reverted" line, which
	// tells the reader the person's edit may have been written over and there
	// may be nothing left to do. Here the edit stands untouched, so that line
	// would send them looking for a revert that never happened.
	const manager = "kustomize-controller"
	suspendedAt := time.Date(2026, 9, 17, 12, 0, 0, 0, time.UTC)
	statusWrittenAt := suspendedAt.Add(4 * time.Second)

	j := newJoiner(joinSet(nil), map[string]bool{manager: true}, func(context.Context, DriftEvent) {})

	claim, claimed := j.reconciledBy([]fieldOwner{
		{Manager: "flux-cli", UpdatedAt: suspendedAt, Paths: []string{"spec.suspend"}},
		{Manager: manager, Subresource: "status", UpdatedAt: statusWrittenAt, Paths: []string{"status.conditions"}},
	}, suspendedAt)

	if claim {
		t.Errorf("Reconciled = true by %q, want no claim: the controller wrote status, and spec.suspend still stands", claimed)
	}
}

func TestReconciledByDoesNotInventAClaimFromTwoMissingTimes(t *testing.T) {
	// Neither side has a time. The strict comparison declines this on its own --
	// the zero time is not after itself -- and the guard above it is what makes
	// that a decision rather than an accident of how two absent values happen to
	// sort. Pinned here so a future comparison that reads the two zeroes as a
	// match cannot report a reconcile built entirely out of values the detector
	// does not have.
	//
	// A record with no timestamp does not require a malformed payload: the
	// field is a plain time.Time, so an absent or null `timestamp` decodes to
	// the zero value without error. A genuinely malformed one fails
	// json.Unmarshal and is nacked before it reaches here.
	j := newJoiner(joinSet(nil), map[string]bool{"argocd-controller": true}, func(context.Context, DriftEvent) {})

	claim, manager := j.reconciledBy([]fieldOwner{{Manager: "argocd-controller"}}, time.Time{})
	if claim {
		t.Errorf("Reconciled = true by %q, want no claim when neither the change nor the manager has a time", manager)
	}
}

func TestReconciledByDoesNotClaimAgainstAChangeWithNoTimestamp(t *testing.T) {
	// The mirror of the test above, and the one the zero-time guard is really
	// for. Where two missing times merely fail to order, a missing changedAt
	// against a manager with a real time orders the wrong way round: the zero
	// time sorts before every real one, so any configured manager that has ever
	// written the object lands strictly after it, and the detector
	// reports its most recent write as the reconcile for a change it cannot
	// place in time at all.
	//
	// Every other case in this file gives changedAt a real value, which is why
	// the accidental ordering that covers a zero UpdatedAt looked like it
	// covered both.
	wroteLongBefore := time.Date(2020, 1, 1, 0, 0, 0, 0, time.UTC)

	j := newJoiner(joinSet(nil), map[string]bool{"argocd-controller": true}, func(context.Context, DriftEvent) {})

	claim, manager := j.reconciledBy(
		[]fieldOwner{{Manager: "argocd-controller", UpdatedAt: wroteLongBefore}},
		time.Time{},
	)
	if claim {
		t.Errorf("Reconciled = true by %q, want no claim: the change has no timestamp, so nothing can be shown to follow it", manager)
	}
}

func TestReconciledByDeclinesAWriteInTheChangesOwnSecond(t *testing.T) {
	// An entry sharing the change's second may *be* the change. Manager is
	// self-declared, so `kubectl apply --server-side
	// --field-manager=argocd-controller` produces exactly this fieldOwner, and a
	// claim here would report the audited write as its own reconcile.
	//
	// Written with a sub-second changedAt because that is the only way to see
	// the comparison at all: every other timestamp in this file lands on a
	// second boundary, where truncation is a no-op.
	changedAt := time.Date(2026, 9, 16, 12, 0, 0, 600_000_000, time.UTC)
	sameSecond := changedAt.Truncate(time.Second) // what the API server returns

	j := newJoiner(joinSet(nil), map[string]bool{"argocd-controller": true}, func(context.Context, DriftEvent) {})

	if claim, manager := j.reconciledBy([]fieldOwner{{Manager: "argocd-controller", UpdatedAt: sameSecond}}, changedAt); claim {
		t.Errorf("Reconciled = true by %q, want no claim: the write in the change's own second may be the change", manager)
	}
}

func TestReconciledByClaimsAWriteFromTheNextSecond(t *testing.T) {
	// The ordinary reconcile, and the reason the audit side is floored. Without
	// the truncation this comparison would turn on how much of the change's
	// second had already elapsed: a reconcile stored as 12:00:01 against a
	// change at 12:00:00.6 is 400ms later, but against a change recorded at
	// 12:00:01.4 -- the same second on the API server's grid -- a raw comparison
	// would read it as earlier.
	changedAt := time.Date(2026, 9, 16, 12, 0, 0, 600_000_000, time.UTC)
	reconciledAt := changedAt.Truncate(time.Second).Add(time.Second)

	j := newJoiner(joinSet(nil), map[string]bool{"argocd-controller": true}, func(context.Context, DriftEvent) {})

	claim, manager := j.reconciledBy([]fieldOwner{{Manager: "argocd-controller", UpdatedAt: reconciledAt}}, changedAt)
	if !claim {
		t.Error("Reconciled = false, want true: a GitOps write in the second after the change is the reconcile")
	}
	if manager != "argocd-controller" {
		t.Errorf("ReconciledBy = %q, want argocd-controller", manager)
	}
}

func TestReconciledByFloorsTheManagerSideToo(t *testing.T) {
	// A managedFields time arrives whole-second today, so flooring the audit
	// side alone happens to give the same answers -- an integer second after
	// floor(changedAt) is after changedAt as well. This is the case where the
	// two spellings part company: a sub-second UpdatedAt inside the change's own
	// second, which flooring only the audit side reads as a reconcile and
	// reopens the self-claim with. Pinned so the rule stays "a later second"
	// rather than resting on how the value reached us.
	changedAt := time.Date(2026, 9, 16, 12, 0, 0, 200_000_000, time.UTC)
	sameSecond := changedAt.Add(700 * time.Millisecond)

	j := newJoiner(joinSet(nil), map[string]bool{"argocd-controller": true}, func(context.Context, DriftEvent) {})

	if claim, manager := j.reconciledBy([]fieldOwner{{Manager: "argocd-controller", UpdatedAt: sameSecond}}, changedAt); claim {
		t.Errorf("Reconciled = true by %q, want no claim: %v and %v are the same second", manager, sameSecond, changedAt)
	}
}

func TestReconciledByDoesNotLetAnApplyReconcileItself(t *testing.T) {
	// The whole shape, as it arrives from a cluster: a person applies with the
	// configured manager's name, the API server records one entry under that
	// name, and nothing else has touched the object. Before the strictly-after
	// rule this reported Reconciled -- the reading T4 acts on to suppress the
	// inject -- for a change that stands untouched.
	const manager = "kustomize-controller"
	changedAt := time.Date(2026, 9, 17, 9, 30, 12, 250_000_000, time.UTC)

	j := newJoiner(joinSet(nil), map[string]bool{manager: true}, func(context.Context, DriftEvent) {})

	claim, claimed := j.reconciledBy([]fieldOwner{
		{Manager: manager, Operation: "Apply", UpdatedAt: changedAt.Truncate(time.Second), Paths: []string{"spec.replicas"}},
	}, changedAt)
	if claim {
		t.Errorf("Reconciled = true by %q, want no claim: the only write is the audited one wearing the manager's name", claimed)
	}
}

func TestReconciledByStillDeclinesAWriteFromAnEarlierSecond(t *testing.T) {
	// The other side of the truncation: flooring the audit timestamp must not
	// widen the window past the second the change landed in. A manager that last
	// wrote a second earlier reconciled something else.
	changedAt := time.Date(2026, 9, 16, 12, 0, 1, 600_000_000, time.UTC)
	earlier := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)

	j := newJoiner(joinSet(nil), map[string]bool{"argocd-controller": true}, func(context.Context, DriftEvent) {})

	if claim, manager := j.reconciledBy([]fieldOwner{{Manager: "argocd-controller", UpdatedAt: earlier}}, changedAt); claim {
		t.Errorf("Reconciled = true by %q, want no claim for a write from the previous second", manager)
	}
}

func TestJoinSetsReconciledFromTheLiveObject(t *testing.T) {
	changedAt := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)
	reconciledAt := changedAt.Add(time.Minute)
	obj := managedFieldsObject(
		entry("kubectl-edit", "Update", "", `{"f:spec":{"f:replicas":{}}}`, &changedAt),
		entry("argocd-controller", "Apply", "", `{"f:spec":{"f:replicas":{}}}`, &reconciledAt),
	)

	var got DriftEvent
	j := newJoiner(joinSet(&stubGetter{obj: obj}), parseGitopsManagers("argocd-controller"),
		func(_ context.Context, e DriftEvent) { got = e })

	record := joinRecord()
	record.Timestamp = changedAt
	j.Handle(context.Background(), record)

	if got.Outcome != joinEnriched {
		t.Fatalf("Outcome = %q, want %q", got.Outcome, joinEnriched)
	}
	if len(got.Owners) != 2 {
		t.Errorf("Owners = %v, want both managedFields entries", got.Owners)
	}
	if !got.Reconciled || got.ReconciledBy != "argocd-controller" {
		t.Errorf("Reconciled = %v by %q, want true by argocd-controller", got.Reconciled, got.ReconciledBy)
	}
}

func TestParseGitopsManagers(t *testing.T) {
	for _, tc := range []struct {
		name  string
		value string
		want  []string
	}{
		{name: "empty is unconfigured", value: "", want: nil},
		{name: "whitespace only is unconfigured", value: " , ", want: nil},
		{name: "entries are trimmed", value: "argocd-controller, flux ", want: []string{"argocd-controller", "flux"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := parseGitopsManagers(tc.value)
			if len(got) != len(tc.want) {
				t.Fatalf("parseGitopsManagers(%q) = %v, want %v", tc.value, got, tc.want)
			}
			for _, name := range tc.want {
				if !got[name] {
					t.Errorf("parseGitopsManagers(%q) did not contain %q", tc.value, name)
				}
			}
		})
	}
}

// liveObject builds an object the fake dynamic client's tracker will serve.
func liveObject(apiVersion, kind, namespace, name string) *unstructured.Unstructured {
	obj := &unstructured.Unstructured{}
	obj.SetAPIVersion(apiVersion)
	obj.SetKind(kind)
	if namespace != "" {
		obj.SetNamespace(namespace)
	}
	obj.SetName(name)
	obj.SetManagedFields([]metav1.ManagedFieldsEntry{
		entry("kubectl-edit", "Update", "", `{"f:spec":{"f:replicas":{}}}`, nil),
	})
	return obj
}

func TestDynamicGetterReachesTheObjectTheRecordNames(t *testing.T) {
	deployment := liveObject("apps/v1", "Deployment", "prod", "api")
	node := liveObject("v1", "Node", "", "gke-node-1")

	deploymentGVR := schema.GroupVersionResource{Group: "apps", Version: "v1", Resource: "deployments"}
	nodeGVR := schema.GroupVersionResource{Group: "", Version: "v1", Resource: "nodes"}

	client := dynamicfake.NewSimpleDynamicClientWithCustomListKinds(
		runtime.NewScheme(),
		map[schema.GroupVersionResource]string{
			deploymentGVR: "DeploymentList",
			nodeGVR:       "NodeList",
		},
		deployment, node,
	)
	getter := dynamicGetter{client: client}

	for _, tc := range []struct {
		name string
		ref  ResourceRef
		// wantSubresource is what the API call should ask for, which is never
		// the audited subresource: a write to "status" changes the parent
		// object, whose managedFields carries the status claim as its own
		// entry, and asking for the subresource returns a body with no
		// managedFields at all.
		wantSubresource string
	}{
		{
			name: "a namespaced object",
			ref:  ResourceRef{Group: "apps", Version: "v1", Namespace: "prod", Resource: "deployments", Name: "api"},
		},
		{
			name: "a cluster-scoped object takes the un-namespaced path",
			ref:  ResourceRef{Group: "", Version: "v1", Resource: "nodes", Name: "gke-node-1"},
		},
		{
			name: "a subresource write fetches the parent",
			ref:  ResourceRef{Group: "apps", Version: "v1", Namespace: "prod", Resource: "deployments", Name: "api", Subresource: "status"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			client.Fake.ClearActions()

			got, err := getter.Get(context.Background(), tc.ref)
			if err != nil {
				t.Fatalf("Get returned error: %v", err)
			}
			if got.GetName() != tc.ref.Name {
				t.Errorf("fetched %q, want %q", got.GetName(), tc.ref.Name)
			}
			if len(got.GetManagedFields()) == 0 {
				t.Error("fetched object carries no managedFields, which is the whole point of the lookup")
			}

			actions := client.Fake.Actions()
			if len(actions) != 1 {
				t.Fatalf("recorded %d actions, want 1", len(actions))
			}
			if got := actions[0].GetSubresource(); got != tc.wantSubresource {
				t.Errorf("requested subresource %q, want %q", got, tc.wantSubresource)
			}
			if got := actions[0].GetNamespace(); got != tc.ref.Namespace {
				t.Errorf("requested namespace %q, want %q", got, tc.ref.Namespace)
			}
			if got := actions[0].GetResource().Resource; got != tc.ref.Resource {
				t.Errorf("requested resource %q, want the audit log's plural %q", got, tc.ref.Resource)
			}
		})
	}
}

func TestDynamicGetterReportsNotFoundAsNotFound(t *testing.T) {
	// joinGone depends on apierrors.IsNotFound recognising what the client
	// returns; anything else would be counted as a failed lookup.
	client := dynamicfake.NewSimpleDynamicClientWithCustomListKinds(
		runtime.NewScheme(),
		map[schema.GroupVersionResource]string{
			{Group: "apps", Version: "v1", Resource: "deployments"}: "DeploymentList",
		},
	)

	_, err := dynamicGetter{client: client}.Get(context.Background(),
		ResourceRef{Group: "apps", Version: "v1", Namespace: "prod", Resource: "deployments", Name: "absent"})
	if !apierrors.IsNotFound(err) {
		t.Errorf("Get returned %v, want a NotFound the join can classify as gone", err)
	}
}

func TestPathNotServedSeparatesTheTwoKindsOf404(t *testing.T) {
	// Both are NotFound as far as the status code goes, which is why the join
	// cannot classify on apierrors.IsNotFound alone.
	for _, tc := range []struct {
		name string
		err  error
		want bool
	}{
		{name: "a missing object", err: notFound(), want: false},
		{name: "a path refused by the mux", err: unservedPathFromMux(), want: true},
		{name: "a path refused by the group's handler", err: unservedPathFromGroupHandler(), want: true},
		{name: "a wrapped unserved path", err: fmt.Errorf("lookup: %w", unservedPathFromMux()), want: true},
		{name: "not an API error at all", err: errors.New("dial tcp: connection refused"), want: false},
		{name: "no error", err: nil, want: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if !apierrors.IsNotFound(tc.err) && tc.want {
				t.Fatalf("test setup: %v is not even a NotFound, so the join would never consult pathNotServed", tc.err)
			}
			if got := pathNotServed(tc.err); got != tc.want {
				t.Errorf("pathNotServed(%v) = %v, want %v", tc.err, got, tc.want)
			}
		})
	}
}

func TestLogDriftEventFormat(t *testing.T) {
	changedAt := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)

	for _, tc := range []struct {
		name    string
		event   DriftEvent
		want    []string
		notWant []string
	}{
		{
			name: "an enriched event carries its owners",
			event: DriftEvent{
				Record:  AuditRecord{Principal: "ada@example.com", Cluster: "prod-a", MethodName: "io.k8s.apps.v1.deployments.patch", Verb: "patch", Timestamp: changedAt},
				Outcome: joinEnriched,
				Owners:  []fieldOwner{{Manager: "kubectl-edit", Operation: "Update", Paths: []string{"spec.replicas"}}},
			},
			want: []string{
				`principal="ada@example.com"`,
				"method=io.k8s.apps.v1.deployments.patch",
				"join=enriched",
				`owners=["kubectl-edit(Update)=[spec.replicas]"]`,
			},
			notWant: []string{"reconciled_by="},
		},
		{
			name: "a reconciled event names the manager",
			event: DriftEvent{
				Record:       AuditRecord{Timestamp: changedAt},
				Outcome:      joinEnriched,
				Owners:       []fieldOwner{{Manager: "flux", Operation: "Apply"}},
				Reconciled:   true,
				ReconciledBy: "flux",
			},
			want: []string{`reconciled_by="flux"`},
		},
		{
			name: "an enriched object nothing has ever managed says so",
			event: DriftEvent{
				Record:  AuditRecord{Timestamp: changedAt},
				Outcome: joinEnriched,
			},
			want: []string{"owners=[" + noOwnersLabel + "]"},
		},
		{
			name: "a failed lookup carries its error and no owners field",
			event: DriftEvent{
				Record:      AuditRecord{Timestamp: changedAt},
				Outcome:     joinFailed,
				LookupError: errors.New("forbidden"),
			},
			want:    []string{"join=failed", `lookup_error="forbidden"`},
			notWant: []string{"owners="},
		},
		{
			name: "an unreachable record is still a DRIFT line",
			event: DriftEvent{
				Record:  AuditRecord{Cluster: "prod-b", Timestamp: changedAt},
				Outcome: joinUnreachable,
			},
			want:    []string{"DRIFT", "cluster=prod-b", "join=unreachable"},
			notWant: []string{"owners=", "lookup_error="},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			log.SetOutput(&buf)
			log.SetFlags(0)
			t.Cleanup(func() {
				log.SetOutput(io.Discard)
				log.SetFlags(log.LstdFlags)
			})

			logDriftEvent(context.Background(), tc.event)

			got := buf.String()
			for _, want := range tc.want {
				if !strings.Contains(got, want) {
					t.Errorf("log line %q, want it to contain %q", got, want)
				}
			}
			for _, notWant := range tc.notWant {
				if strings.Contains(got, notWant) {
					t.Errorf("log line %q, want it not to contain %q", got, notWant)
				}
			}
		})
	}
}

func TestRenderOwnersJoinsEveryClaim(t *testing.T) {
	got := renderOwners([]fieldOwner{
		{Manager: "kubectl-edit", Operation: "Update", Paths: []string{"spec.replicas"}},
		{Manager: "argocd-controller", Operation: "Apply", Paths: []string{"spec.template"}},
	})
	want := `"kubectl-edit(Update)=[spec.replicas]" "argocd-controller(Apply)=[spec.template]"`
	if got != want {
		t.Errorf("renderOwners = %q, want %q", got, want)
	}
}

// The separator is a space and neither half of a claim is constrained to be
// space-free -- a field manager is validated only for length and printable
// characters, and a merge key renders the object's own field value. Unquoted,
// this one claim reads as three.
func TestRenderOwnersQuotesAClaimContainingSpaces(t *testing.T) {
	got := renderOwners([]fieldOwner{
		{Manager: "my tool", Operation: "Update", Paths: []string{"spec.containers[name=my app].image"}},
	})
	want := `"my tool(Update)=[spec.containers[name=my app].image]"`
	if got != want {
		t.Errorf("renderOwners = %q, want %q", got, want)
	}
}
