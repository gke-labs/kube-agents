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
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
)

const (
	testReleaseNamespace = "kubeagents-system"
	testReleaseName      = "kube-agents"
	helmUserAgent        = "terraform-provider-helm_v3.3.0_x5"
	goClientUserAgent    = "Go-http-client/2.0"
	kubectlUserAgent     = "kubectl/v1.35.6 (darwin/arm64) kubernetes/09fd156"
	otherCluster         = "prod-b"
)

func testOwnRelease() ownRelease {
	return ownRelease{
		release: helmRelease{Namespace: testReleaseNamespace, Name: testReleaseName},
		cluster: joinCluster(),
	}
}

// releaseObject is a live object carrying the annotations Helm stamps for the
// named release.
func releaseObject(namespace, name string) *stubGetter {
	obj := &unstructured.Unstructured{Object: map[string]any{}}
	obj.SetAnnotations(map[string]string{
		helmReleaseNamespaceAnnotation: namespace,
		helmReleaseNameAnnotation:      name,
	})
	return &stubGetter{obj: obj}
}

// plainObject is a live object with no release annotations.
func plainObject() *stubGetter {
	return &stubGetter{obj: &unstructured.Unstructured{Object: map[string]any{}}}
}

// unreadable is the lookup a kind the join's ServiceAccount has no read on
// gets: RBAC, admission policies, most custom resources.
func unreadable() *stubGetter {
	return &stubGetter{err: errors.New("forbidden")}
}

// namespacedRecord is a live write by Helm's Go client to an object in
// namespace.
func namespacedRecord(namespace string) AuditRecord {
	record := joinRecord()
	record.UserAgent = helmUserAgent
	record.Resource.Namespace = namespace
	record.Resource.Name = "kube-agents-controller-manager"
	return record
}

// clusterRecord is a live write by Helm's Go client to a cluster-scoped object.
func clusterRecord(group, resource, name string) AuditRecord {
	record := joinRecord()
	record.UserAgent = goClientUserAgent
	record.Resource = ResourceRef{Group: group, Version: "v1", Resource: resource, Name: name}
	return record
}

// crdRecord is a live kubectl write to a CRD, as the installer applies them.
func crdRecord(name string) AuditRecord {
	record := clusterRecord(crdGroup, crdResource, name)
	record.UserAgent = kubectlUserAgent
	return record
}

// with returns record after change has edited it.
func with(record AuditRecord, change func(*AuditRecord)) AuditRecord {
	change(&record)
	return record
}

// handleOwnRelease drives one record through a joiner that has the digest
// route and the test release configured.
func handleOwnRelease(t *testing.T, getter objectGetter, record AuditRecord) (DriftEvent, *joiner) {
	t.Helper()
	var got DriftEvent
	j := newJoiner(joinSet(getter), nil, nil, nil, func(_ context.Context, e DriftEvent) { got = e })
	j.skipBacklog = true
	j.own = testOwnRelease()
	j.Handle(context.Background(), record)
	return got, j
}

// The rule, one row per case: which writes are marked as the install's own,
// and as which kind, and which are left as any other write is.
func TestOwnReleaseRule(t *testing.T) {
	kubectl := func(r *AuditRecord) { r.UserAgent = kubectlUserAgent }
	deleted := func(r *AuditRecord) { r.Verb = deleteVerb }
	collectionDeleted := func(r *AuditRecord) { r.Verb = "deletecollection"; r.Resource.Name = "" }
	elsewhere := func(r *AuditRecord) { r.Cluster = otherCluster }
	operatorRole := "kube-agents-kubeagents-system-operator-role"

	tests := []struct {
		name   string
		getter *stubGetter
		record AuditRecord
		want   ownReleaseKind
	}{
		// The install's two namespaces.
		{"an object in the release namespace", releaseObject(testReleaseNamespace, testReleaseName), namespacedRecord(testReleaseNamespace), ownReleaseObject},
		{"an object in the release namespace the join cannot read", unreadable(), namespacedRecord(testReleaseNamespace), ownReleaseObject},
		{"an object in the profiles namespace", unreadable(), namespacedRecord(testReleaseNamespace + profilesNamespaceSuffix), ownReleaseObject},
		{"Helm's record of the release", unreadable(),
			with(namespacedRecord(testReleaseNamespace), func(r *AuditRecord) {
				r.Resource = ResourceRef{Version: "v1", Namespace: testReleaseNamespace, Resource: "secrets", Name: "sh.helm.release.v1.kube-agents.v12"}
			}), ownReleaseObject},
		{"a kubectl write in the release namespace", releaseObject(testReleaseNamespace, testReleaseName), with(namespacedRecord(testReleaseNamespace), kubectl), ownReleaseNone},
		{"a delete in the release namespace", releaseObject(testReleaseNamespace, testReleaseName), with(namespacedRecord(testReleaseNamespace), deleted), ownReleaseNone},
		{"a delete of a collection in the release namespace", unreadable(), with(namespacedRecord(testReleaseNamespace), collectionDeleted), ownReleaseNone},
		{"a write to a subresource in the release namespace", releaseObject(testReleaseNamespace, testReleaseName),
			with(namespacedRecord(testReleaseNamespace), func(r *AuditRecord) { r.Resource.Subresource = "status" }), ownReleaseNone},
		{"the release namespace of another cluster", unreadable(), with(namespacedRecord(testReleaseNamespace), elsewhere), ownReleaseNone},
		{"a namespace that only starts like the release namespace", unreadable(), namespacedRecord(testReleaseNamespace + "-staging"), ownReleaseNone},
		{"a namespace that only starts like the profiles namespace", unreadable(), namespacedRecord(testReleaseNamespace + profilesNamespaceSuffix + "-old"), ownReleaseNone},

		// Another namespace: only an object that names the release.
		{"an object elsewhere that carries the release annotations", releaseObject(testReleaseNamespace, testReleaseName), namespacedRecord("shop"), ownReleaseObject},
		{"an object of another release", releaseObject("shop", "checkout"), namespacedRecord("shop"), ownReleaseNone},
		{"a release of the same name in another namespace", releaseObject("shop", testReleaseName), namespacedRecord("shop"), ownReleaseNone},
		{"an object elsewhere with no release annotations", plainObject(), namespacedRecord("shop"), ownReleaseNone},
		{"an object elsewhere the join cannot read", unreadable(), namespacedRecord("shop"), ownReleaseNone},
		{"a kubectl write to an object elsewhere that carries the release annotations", releaseObject(testReleaseNamespace, testReleaseName), with(namespacedRecord("shop"), kubectl), ownReleaseNone},

		// Cluster-scoped RBAC and admission objects, by name.
		{"a cluster role named after the release", unreadable(), clusterRecord(rbacGroup, "clusterroles", operatorRole), ownReleaseObject},
		{"an admission policy named after the release", unreadable(), clusterRecord(admissionGroup, "validatingadmissionpolicies", "kube-agents-agent-readonly"), ownReleaseObject},
		{"a kubectl write to a cluster role named after the release", unreadable(), with(clusterRecord(rbacGroup, "clusterroles", operatorRole), kubectl), ownReleaseNone},
		{"a delete of a cluster role named after the release", unreadable(), with(clusterRecord(rbacGroup, "clusterroles", operatorRole), deleted), ownReleaseNone},
		{"a cluster role named after the release on another cluster", unreadable(), with(clusterRecord(rbacGroup, "clusterroles", operatorRole), elsewhere), ownReleaseNone},
		{"a cluster role not named after the release", unreadable(), clusterRecord(rbacGroup, "clusterroles", "cluster-admin-for-ada"), ownReleaseNone},
		{"a cluster role whose name only starts like the release name", unreadable(), clusterRecord(rbacGroup, "clusterroles", "kube-agentsx-role"), ownReleaseNone},
		{"a cluster-scoped object of another group named after the release", unreadable(), clusterRecord("storage.k8s.io", "storageclasses", "kube-agents-fast"), ownReleaseNone},

		// Any other cluster-scoped object: only one that names the release.
		{"a namespace that carries the release annotations", releaseObject(testReleaseNamespace, testReleaseName), clusterRecord("", "namespaces", "kubeagents-system-profiles"), ownReleaseObject},
		{"a namespace named after the release with no annotations", plainObject(), clusterRecord("", "namespaces", "kube-agents-scratch"), ownReleaseNone},

		// The product's CRDs, which the installer applies with kubectl.
		{"a kubectl write to a kube-agents CRD", unreadable(), crdRecord("platformagents.kubeagents.x-k8s.io"), ownReleaseCRD},
		{"a late kubectl write to a kube-agents CRD", unreadable(), with(crdRecord("tiers.kubeagents.x-k8s.io"), func(r *AuditRecord) { r.Backlog = true }), ownReleaseCRD},
		{"a delete of a kube-agents CRD", plainObject(), with(crdRecord("platformagents.kubeagents.x-k8s.io"), deleted), ownReleaseNone},
		{"a kube-agents CRD on another cluster", plainObject(), with(crdRecord("platformagents.kubeagents.x-k8s.io"), elsewhere), ownReleaseNone},
		{"a CRD of another group", plainObject(), crdRecord("widgets.example.com"), ownReleaseNone},
		{"a CRD whose group only ends like the product's", plainObject(), crdRecord("widgets.notkubeagents.x-k8s.io"), ownReleaseNone},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, j := handleOwnRelease(t, tt.getter, tt.record)
			if got.OwnRelease != tt.want || got.Digest != (tt.want != ownReleaseNone) {
				t.Errorf("own_release=%q digest=%t, want %q and the digest route only with a kind", got.OwnRelease, got.Digest, tt.want)
			}
			want := 0
			if tt.want != ownReleaseNone {
				want = 1
			}
			if c := j.Counts(); c.OwnRelease != want || !strings.Contains(c.String(), "own_release=") {
				t.Errorf("Counts() = %q, want own_release=%d", c.String(), want)
			}
		})
	}
}

// The rule needs a daemon that reads the marks and a configured release:
// without either the record goes the way it went before, and no revision is
// marked.
func TestOwnReleaseRuleNeedsADaemonThatReadsTheMarksAndARelease(t *testing.T) {
	named := testReleaseNamespace + "/" + testReleaseName
	tests := []struct {
		name    string
		routes  digestRoutes
		release string
		marked  bool
	}{
		{"both", digestRoutes{backlog: true, ownRelease: true}, named, true},
		// Such a daemon digests by count alone and words every row as late.
		{"a daemon with the digest route that does not read the marks", digestRoutes{backlog: true}, named, false},
		{"no digest route", digestRoutes{}, named, false},
		{"no release", digestRoutes{backlog: true, ownRelease: true}, "", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f, err := parseFlags([]string{
				"--project", "example-project",
				"--cluster-name", "prod-a",
				"--cluster-location", "us-central1",
				"--in-cluster",
				"--own-release", tt.release,
			})
			if err != nil {
				t.Fatalf("parseFlags returned error: %v", err)
			}
			for _, record := range []AuditRecord{namespacedRecord(testReleaseNamespace), crdRecord("tiers.kubeagents.x-k8s.io"), revisionRecord("12")} {
				var got DriftEvent
				filter, _ := newFilterFromFlags(f, buildClusterSet(plainObject(), directClusterIdentity(f), nil),
					func(_ context.Context, e DriftEvent) { got = e }, nil, tt.routes)
				record.StatusCode = statusCodeOK
				filter.Handle(context.Background(), record)
				if (got.OwnRelease != ownReleaseNone) != tt.marked || got.Digest != tt.marked {
					t.Errorf("%s: own_release=%q digest=%t, want marked=%t", record.Resource, got.OwnRelease, got.Digest, tt.marked)
				}
				if !tt.marked && got.ReleaseRevision != "" {
					t.Errorf("%s: release_revision=%q, want none", record.Resource, got.ReleaseRevision)
				}
			}
		})
	}
}

// The handler takes its two routes from what the daemon advertises. The
// own-release one rests on the digest route: the marks travel on it.
func TestTheHandlerTakesTheOwnReleaseRouteFromTheDaemonsFeatures(t *testing.T) {
	tests := []struct {
		name     string
		features string
		want     digestRoutes
	}{
		{"both features", `"drift-backlog","drift-own-release"`, digestRoutes{backlog: true, ownRelease: true}},
		{"a daemon from before the marks", `"drift-backlog"`, digestRoutes{backlog: true}},
		{"the marks with no digest route", `"drift-own-release"`, digestRoutes{}},
		{"neither", ``, digestRoutes{}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			daemon, url := newFakeDaemon(t)
			daemon.mu.Lock()
			daemon.healthzBody = fmt.Sprintf(`{"status":"ok","inject_kinds":[%q],"features":[%s]}`, injectKindDrift, tt.features)
			daemon.mu.Unlock()
			inject, err := newDriftInjector(driftInjectorConfig{daemonURL: url, bearerToken: "token-value"})
			if err != nil {
				t.Fatalf("newDriftInjector returned error: %v", err)
			}
			if err := inject.VerifyKindSupported(context.Background(), injectKindDrift); err != nil {
				t.Fatalf("VerifyKindSupported: %v", err)
			}
			if got := newDriftInjectHandler(inject).routes(); got != tt.want {
				t.Errorf("routes = %+v, want %+v", got, tt.want)
			}
		})
	}
}

// A late Helm write to the release stays a backlog record, with no lookup and
// its publish time, and carries the kind too: the daemon needs it to tell an
// upgrade's late rows from any other late rows.
func TestOwnReleaseRuleMarksALateRecordAsBoth(t *testing.T) {
	logs := captureLog(t)
	var got DriftEvent
	getter := unreadable()
	j := newJoiner(joinSet(getter), nil, nil, nil, func(ctx context.Context, e DriftEvent) { got = e; logDriftEvent(ctx, e) })
	j.skipBacklog = true
	j.own = testOwnRelease()
	j.Handle(context.Background(), with(namespacedRecord(testReleaseNamespace), func(r *AuditRecord) { r.Backlog = true }))

	if !got.Digest || !got.late() || got.OwnRelease != ownReleaseObject || got.Outcome != joinSkippedBacklog || len(getter.refs) != 0 {
		t.Errorf("digest=%t late=%t own_release=%q outcome=%s lookups=%d, want a backlog record marked release with no lookup",
			got.Digest, got.late(), got.OwnRelease, got.Outcome, len(getter.refs))
	}
	payload := payloadForEvent(got)
	if !payload.Backlog || payload.OwnRelease != string(ownReleaseObject) {
		t.Errorf("payload backlog=%t own_release=%q, want both set", payload.Backlog, payload.OwnRelease)
	}
	out := logs.String()
	if !strings.Contains(out, backlogMarker) || !strings.Contains(out, ownReleaseField+"="+string(ownReleaseObject)) {
		t.Errorf("the DRIFT line should carry %q and %s=%s:\n%s", backlogMarker, ownReleaseField, ownReleaseObject, out)
	}
}

// revisionRecord is the create of the release record Secret for a revision,
// which Helm makes first in every upgrade and rollback.
func revisionRecord(revision string) AuditRecord {
	record := namespacedRecord(testReleaseNamespace)
	record.Verb = createVerb
	record.Resource = ResourceRef{Version: "v1", Namespace: testReleaseNamespace, Resource: secretsResource, Name: "sh.helm.release.v1.kube-agents.v" + revision}
	return record
}

// Only this release's record Secret, written by a client other than kubectl
// on the install's cluster, marks an upgrade: its create the start, its update
// the end.
func TestReleaseRevisionMarksTheStartAndTheEndOfAnUpgrade(t *testing.T) {
	named := func(name string) AuditRecord {
		return with(revisionRecord("12"), func(r *AuditRecord) { r.Resource.Name = name })
	}
	verb := func(v string) func(*AuditRecord) { return func(r *AuditRecord) { r.Verb = v } }
	tests := []struct {
		name      string
		record    AuditRecord
		wantStart string
		wantEnd   string
	}{
		{"the create of the next revision", revisionRecord("12"), "12", ""},
		{"a late create of the next revision", with(revisionRecord("13"), func(r *AuditRecord) { r.Backlog = true }), "13", ""},
		{"an update of the record", with(revisionRecord("12"), verb(updateVerb)), "", "12"},
		{"a patch of the record", with(revisionRecord("12"), verb(patchVerb)), "", "12"},
		{"a delete of the record", with(revisionRecord("12"), verb(deleteVerb)), "", ""},
		{"a create with kubectl", with(revisionRecord("12"), func(r *AuditRecord) { r.UserAgent = kubectlUserAgent }), "", ""},
		{"an update with kubectl", with(revisionRecord("12"), func(r *AuditRecord) { r.Verb = updateVerb; r.UserAgent = kubectlUserAgent }), "", ""},
		{"a create on another cluster", with(revisionRecord("12"), func(r *AuditRecord) { r.Cluster = otherCluster }), "", ""},
		{"a create in another namespace", with(revisionRecord("12"), func(r *AuditRecord) { r.Resource.Namespace = "shop" }), "", ""},
		{"the record of another release", named("sh.helm.release.v1.checkout.v12"), "", ""},
		{"the record of a release whose name this one prefixes", named("sh.helm.release.v1.kube-agents.vault.v12"), "", ""},
		{"a name with no revision", named("sh.helm.release.v1.kube-agents.v"), "", ""},
		{"a name that only starts like a record", named("sh.helm.release.v1.kube-agents.v12-copy"), "", ""},
		{"a config map of that name", with(revisionRecord("12"), func(r *AuditRecord) { r.Resource.Resource = "configmaps" }), "", ""},
		{"any other create in the release namespace", with(namespacedRecord(testReleaseNamespace), verb(createVerb)), "", ""},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			logs := captureLog(t)
			var got DriftEvent
			j := newJoiner(joinSet(unreadable()), nil, nil, nil, func(ctx context.Context, e DriftEvent) { got = e; logDriftEvent(ctx, e) })
			j.skipBacklog = true
			j.own = testOwnRelease()
			j.Handle(context.Background(), tt.record)

			if got.ReleaseRevision != tt.wantStart || got.ReleaseRevisionEnd != tt.wantEnd {
				t.Errorf("release_revision=%q release_revision_end=%q, want %q and %q", got.ReleaseRevision, got.ReleaseRevisionEnd, tt.wantStart, tt.wantEnd)
			}
			payload := payloadForEvent(got)
			if payload.OwnReleaseRevision != tt.wantStart || payload.OwnReleaseRevisionEnd != tt.wantEnd {
				t.Errorf("payload own_release_revision=%q own_release_revision_end=%q, want %q and %q",
					payload.OwnReleaseRevision, payload.OwnReleaseRevisionEnd, tt.wantStart, tt.wantEnd)
			}
			out := logs.String()
			if strings.Contains(out, releaseRevisionField+"=") != (tt.wantStart != "") || strings.Contains(out, releaseRevisionEndField+"=") != (tt.wantEnd != "") {
				t.Errorf("the DRIFT line should carry the start mark only for %q and the end mark only for %q:\n%s", tt.wantStart, tt.wantEnd, out)
			}
		})
	}
}

func TestParseOwnRelease(t *testing.T) {
	got, err := parseOwnRelease("kubeagents-system/kube-agents")
	if err != nil || got != (helmRelease{Namespace: "kubeagents-system", Name: "kube-agents"}) {
		t.Errorf("parseOwnRelease = %+v, %v", got, err)
	}
	if got, err := parseOwnRelease(""); err != nil || got != (helmRelease{}) {
		t.Errorf("parseOwnRelease(\"\") = %+v, %v, want the zero release", got, err)
	}
	for _, bad := range []string{"kube-agents", "/kube-agents", "kubeagents-system/", "a/b/c"} {
		if _, err := parseOwnRelease(bad); err == nil {
			t.Errorf("parseOwnRelease(%q) returned no error", bad)
		}
	}
}

// --own-release reaches the joiner through one line of newFilterFromFlags,
// keyed on the cluster the direct flags name.
func TestNewFilterFromFlagsWiresTheOwnRelease(t *testing.T) {
	f, err := parseFlags([]string{
		"--project", "example-project",
		"--cluster-name", "prod-a",
		"--cluster-location", "us-central1",
		"--in-cluster",
		"--own-release", "kubeagents-system/kube-agents",
	})
	if err != nil {
		t.Fatalf("parseFlags returned error: %v", err)
	}
	var got DriftEvent
	filter, _ := newFilterFromFlags(f, buildClusterSet(unreadable(), directClusterIdentity(f), nil),
		func(_ context.Context, e DriftEvent) { got = e }, nil, digestRoutes{backlog: true, ownRelease: true})

	record := namespacedRecord(testReleaseNamespace)
	record.StatusCode = statusCodeOK
	filter.Handle(context.Background(), record)
	if got.OwnRelease != ownReleaseObject {
		t.Error("a write to the release --own-release names was not marked: the flag does not reach the joiner")
	}

	record.Cluster = otherCluster
	filter.Handle(context.Background(), record)
	if got.OwnRelease != ownReleaseNone {
		t.Error("a write on another cluster was marked: the rule is not keyed on the cluster the direct flags name")
	}
}

func TestRealMainRejectsABadOwnRelease(t *testing.T) {
	tests := []struct {
		name    string
		argv    []string
		wantErr string
	}{
		{
			name:    "not namespace/name",
			argv:    []string{"--project", "p", "--own-release", "kube-agents"},
			wantErr: "is not <namespace>/<name>",
		},
		{
			name:    "no direct credentials",
			argv:    []string{"--project", "p", "--own-release", "kubeagents-system/kube-agents"},
			wantErr: "--own-release was given without --in-cluster or --kubeconfig",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := realMain(tt.argv)
			if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
				t.Errorf("realMain(%v) = %v, want an error containing %q", tt.argv, err, tt.wantErr)
			}
		})
	}
}

// An own-release record goes to the digest route with no session, marked with
// its kind and the revision it starts, so the daemon can decide and word it.
func TestInjectPostsAnOwnReleaseRecordToTheDigestRoute(t *testing.T) {
	daemon, url := newFakeDaemon(t)
	handler := backlogHandlerAgainst(t, daemon, url)
	logs := captureLog(t)

	event := driftEvent("insert-1")
	event.Digest = true
	event.OwnRelease = ownReleaseObject
	event.ReleaseRevision = "12"
	handler.Handle(context.Background(), event)

	if sessions, injects := daemon.counts(); sessions != 0 || injects != 0 {
		t.Errorf("sessions=%d injects=%d, want 0 and 0 -- an own-release record opened a session", sessions, injects)
	}
	daemon.mu.Lock()
	payloads := daemon.backlogPayloads
	daemon.mu.Unlock()
	if len(payloads) != 1 {
		t.Fatalf("digest posts = %d, want 1", len(payloads))
	}
	if got := payloads[0]; got.OwnRelease != "release" || got.OwnReleaseRevision != "12" || got.Backlog || got.PublishTime != "" {
		t.Errorf("payload = %+v, want own_release=release, own_release_revision=12 and no backlog or publish_time", got)
	}
	out := logs.String()
	if !strings.Contains(out, "own_release=release") || !strings.Contains(out, "release_revision=12") || strings.Contains(out, backlogMarker) {
		t.Errorf("the DRIFT line should carry own_release=release and release_revision=12, and not %q:\n%s", backlogMarker, out)
	}
}
