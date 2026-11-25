package gateway

import (
	"cmp"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The install the scope clusters tests run under: a folder whose members the
// publisher lists, one explicit project, the management project, a glob
// exclusion, and one excluded cluster.
const (
	scTestScope = `{"present":true,"projects":["explicit-a"],"folders":["123"],` +
		`"exclude":{"projects":["legacy-*"],"clusters":[{"projectId":"member-b","location":"us-east1","clusterName":"dropped"}]}}`
	scTestManagement = "mgmt-proj"
	scTestWidth      = 3
	scTestConfigMap  = "kubeagents-resolved-scope"
)

// scTestDocument is the resolved scope the publisher would write for
// scTestScope at now: member-a and member-b listed, member-c a member no
// probe has listed, member-denied a row its probe can't read, legacy-old a
// listed project the exclusion drops. withClusters false is the document with
// its `clusters` map left out, as the publisher writes it when the map would
// pass its budget.
func scTestDocument(t *testing.T, decl *scopeDeclaration, now time.Time, withClusters bool) string {
	t.Helper()
	stamp := now.UTC().Format(time.RFC3339)
	older := now.Add(-20 * time.Minute).UTC().Format(time.RFC3339)
	row := func(id, outcome string) map[string]any {
		return map[string]any{"id": id, "via": []string{"folders/123"}, "outcome": outcome, "confirmedAt": stamp}
	}
	doc := map[string]any{
		"v": 1, "resolvedAt": stamp, "memberHash": memberHash(decl), "state": "ok", "excludedProjects": []string{},
		"projects": []any{row("member-a", "ok"), row("member-b", "ok"), row("member-c", "ok"), row("member-denied", "denied"), row("legacy-old", "ok")},
	}
	if withClusters {
		doc["clusters"] = map[string]any{
			"member-a":      map[string]any{"at": stamp, "clusters": [][]string{{"us-central1", "a2"}, {"us-central1", "a1"}, {"europe-west1", "a3"}}},
			"member-b":      map[string]any{"at": older, "clusters": [][]string{{"us-east1", "dropped"}, {"us-east1", "keep"}}},
			"member-denied": map[string]any{"at": stamp, "clusters": [][]string{{"us-east1", "y"}}},
			"legacy-old":    map[string]any{"at": stamp, "clusters": [][]string{{"us-east1", "x"}}},
		}
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// startScopeClustersRig is the trigger rig under scTestScope, reading the
// resolved scope from a ConfigMap that holds scTestDocument, through the
// gateway's own ConfigMap reader.
func startScopeClustersRig(t *testing.T, withClusters bool) *triggerRig {
	t.Helper()
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, []byte(scTestScope), 0o600); err != nil {
		t.Fatal(err)
	}
	tr := startTriggerRig(t, func(c *Config) {
		c.ScopeFile = scope
		c.ManagementProject = scTestManagement
		c.DelegationWidthMax = scTestWidth
	})
	decl := scopeFrom(t, scTestScope)
	client := fake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Namespace: "kubeagents-system", Name: scTestConfigMap},
		Data:       map[string]string{resolvedScopeDataKey: scTestDocument(t, decl, time.Now(), withClusters)},
	})
	tr.g.SetResolvedScope(client, "kubeagents-system", scTestConfigMap)
	return tr
}

// scopeClustersOn is the scopeClusters part on a submission, failing the
// test when there is none.
func scopeClustersOn(t *testing.T, env *lib.Envelope) lib.ScopeClusters {
	t.Helper()
	got, ok, err := lib.ScopeClustersOf(messageParts(t, env))
	if !ok || err != nil {
		t.Fatalf("scopeClusters part: found=%v err=%v", ok, err)
	}
	return got
}

// TestAScheduledRunsScopeClustersAreWhatAClusterChildIsAcceptedFor: a
// scheduled run of a delegating profile carries one page of the clusters the
// resolved scope lists (#6789), and the page is the delegation's own scope
// check: each cluster on it is accepted as a `cluster` child, the excluded
// cluster and the projects the scope refuses are absent, the explicit and
// management projects and the unlisted member are named as listed
// elsewhere, and the count, order and truncation are right.
func TestAScheduledRunsScopeClustersAreWhatAClusterChildIsAcceptedFor(t *testing.T) {
	t.Parallel()
	page := []lib.ScopeCluster{
		{Project: "member-a", Location: "europe-west1", Cluster: "a3"},
		{Project: "member-a", Location: "us-central1", Cluster: "a1"},
		{Project: "member-a", Location: "us-central1", Cluster: "a2"},
	}
	offPage := lib.ScopeCluster{Project: "member-b", Location: "us-east1", Cluster: "keep"}
	refusedOnScope := []lib.ScopeCluster{
		{Project: "member-b", Location: "us-east1", Cluster: "dropped"},
		{Project: "member-denied", Location: "us-east1", Cluster: "y"},
		{Project: "legacy-old", Location: "us-east1", Cluster: "x"},
	}

	t.Run("page", func(t *testing.T) {
		t.Parallel()
		tr := startScopeClustersRig(t, true)
		origin, _ := startScheduledParent(t, tr)
		got := scopeClustersOn(t, &origin)
		want := lib.ScopeClusters{Count: 4, Clusters: page, Truncated: true,
			UnlistedProjects: []string{"explicit-a", "member-c", scTestManagement}, UnlistedCount: 3,
			OldestListing: got.OldestListing}
		if !reflect.DeepEqual(got, want) {
			t.Fatalf("the scheduled run's scope clusters =\n%+v\nwant\n%+v", got, want)
		}
		// member-b's listing is the older one, twenty minutes back.
		if at, err := time.Parse(time.RFC3339, got.OldestListing); err != nil || time.Since(at) < 19*time.Minute {
			t.Fatalf("oldest listing = %q, want member-b's, about twenty minutes old", got.OldestListing)
		}
		// The page rides beside the delegateTargets list, whose width it is.
		if targets := targetsOn(t, &origin); targets.WidthMax != scTestWidth || len(got.Clusters) != scTestWidth {
			t.Fatalf("width %d, page %d, want both %d", targets.WidthMax, len(got.Clusters), scTestWidth)
		}
	})

	type delegation struct {
		c      lib.ScopeCluster
		minted bool
	}
	var cases []delegation
	for _, c := range append(slices.Clone(page), offPage) {
		cases = append(cases, delegation{c, true})
	}
	for _, c := range refusedOnScope {
		cases = append(cases, delegation{c, false})
	}
	for _, tc := range cases {
		t.Run(tc.c.Project+"-"+tc.c.Cluster, func(t *testing.T) {
			t.Parallel()
			tr := startScopeClustersRig(t, true)
			origin, parent := startScheduledParent(t, tr)
			ctx := context.Background()
			if err := parent.PublishArtifact(ctx, delegateArtifactWithParameters(t, "cluster", "look",
				targetParams(tc.c.Project, tc.c.Location, tc.c.Cluster))); err != nil {
				t.Fatal(err)
			}
			key := "trigger:schedule/kubeagents-system/compliance-audit"
			var end string
			var children int
			waitFor(t, "the delegation's outcome", func() bool {
				rec, _ := tr.g.reg.Get(ctx, key)
				if rec == nil {
					return false
				}
				ref, ok := rec.TaskRefFor(origin.TaskID)
				end, children = ref.DelegationEnd, len(ref.Children)
				return ok && (children > 0 || end != "")
			})
			if minted := children > 0; minted != tc.minted {
				t.Fatalf("delegating to cluster on %+v: minted %v (end %q), want %v", tc.c, minted, end, tc.minted)
			}
		})
	}
}

// TestScopeClustersWithoutTheResolvedScopesMapAreUnknown: a document with no
// `clusters` map (the publisher dropped it for size, or never wrote one)
// gives no count to trust, so the page says unknown and truncated, and every
// project in scope reads as listed elsewhere.
func TestScopeClustersWithoutTheResolvedScopesMapAreUnknown(t *testing.T) {
	t.Parallel()
	tr := startScopeClustersRig(t, false)
	origin, _ := startScheduledParent(t, tr)
	got := scopeClustersOn(t, &origin)
	want := lib.ScopeClusters{Clusters: []lib.ScopeCluster{}, Truncated: true, Unknown: true,
		UnlistedProjects: []string{"explicit-a", "member-a", "member-b", "member-c", scTestManagement}, UnlistedCount: 5}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("scope clusters without the map =\n%+v\nwant\n%+v", got, want)
	}
}

// scGateway is a gateway that reads doc as its resolved scope, with the
// widest page.
func scGateway(doc *resolvedScope) *Gateway {
	return &Gateway{cfg: &Config{Namespace: "kubeagents-system", ManagementProject: scTestManagement,
		DelegationWidthMax: lib.ScopeClustersPageMax, DelegationDepthMax: 3},
		log: slog.New(slog.NewTextHandler(io.Discard, nil)), resolvedScope: &fakeResolvedSource{doc: doc}}
}

// scBuild is the page for decl and doc under a parent at parentScope (the
// install's scope when empty).
func scBuild(decl *scopeDeclaration, doc *resolvedScope, now time.Time, parentScope capability.Scope) lib.ScopeClusters {
	if parentScope == "" {
		parentScope = capability.NamespaceScope("kubeagents-system")
	}
	return scGateway(doc).buildScopeClustersFor(decl, doc, parentScope, now)
}

func scParseDoc(t *testing.T, raw string) *resolvedScope {
	t.Helper()
	var doc resolvedScope
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		t.Fatal(err)
	}
	return &doc
}

// mustPart fails the test when s isn't a valid part.
func mustPart(t *testing.T, s lib.ScopeClusters) {
	t.Helper()
	if _, err := lib.ScopeClustersPart(s); err != nil {
		t.Fatalf("the part %+v is refused: %v", s, err)
	}
}

// TestScopeClustersUnknownCases: a too-large document, an unreadable one,
// and a `clusters` value that isn't a map all read as unknown, and a value
// of the wrong type never costs the admissions the rows decide.
func TestScopeClustersUnknownCases(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	good := scParseDoc(t, scTestDocument(t, decl, now, true))

	tooLarge := *good
	tooLarge.State = "too-large"
	tooLarge.Projects = nil
	notAMap := *good
	notAMap.Clusters = json.RawMessage(`[["us-central1","a1"]]`)
	for name, doc := range map[string]*resolvedScope{"too large": &tooLarge, "unreadable": nil, "not a map": &notAMap} {
		got := scBuild(decl, doc, now, "")
		if !got.Unknown || !got.Truncated || got.Count != 0 {
			t.Errorf("%s: %+v, want unknown, truncated, nothing counted", name, got)
		}
		mustPart(t, got)
	}
	g := scGateway(&notAMap)
	if err := g.checkScope(decl, clusterTarget{Project: "member-a", Location: "l", Cluster: "c"}); err != nil {
		t.Fatalf("a clusters value that isn't a map refused a resolved member: %v", err)
	}
	odd := strings.Replace(scTestDocument(t, decl, now, false), `"v":1`, `"v":1,"clusters":5`, 1)
	if doc := scParseDoc(t, odd); checkResolvedScope(doc, decl, now, clusterTarget{Project: "member-a", Location: "l", Cluster: "c"}) != nil {
		t.Fatal("a clusters value of the wrong type cost a resolved member its admission")
	}
}

// TestScopeClustersSkipsABadEntryAlone: an entry that isn't whole (an `at`
// that isn't the publisher's time, a non-string name, a pair of three, no
// clusters list) is left out on its own, as the publisher's _clean_clusters
// does: the rest of the map still counts, and the project reads as one no
// probe has listed.
func TestScopeClustersSkipsABadEntryAlone(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	stamp := now.UTC().Format(time.RFC3339)
	doc := scParseDoc(t, scTestDocument(t, decl, now, false))
	for _, id := range []string{"member-d", "member-e", "member-f", "member-g"} {
		doc.Projects = append(doc.Projects, resolvedRow{ID: id, Outcome: "ok", ConfirmedAt: stamp})
	}
	doc.Clusters = json.RawMessage(`{
		"member-a": {"at": "` + stamp + `", "clusters": [["us-central1", "a1"]]},
		"member-b": {"at": "yesterday", "clusters": [["us-east1", "keep"]]},
		"member-c": {"at": "` + stamp + `", "clusters": [["us-east1", 5]]},
		"member-d": {"at": "` + stamp + `", "clusters": [["us-east1", "c", "extra"]]},
		"member-e": {"at": "` + stamp + `"},
		"member-f": {"at": null, "clusters": []},
		"member-g": "not an entry"
	}`)
	got := scBuild(decl, doc, now, "")
	want := lib.ScopeClusters{Count: 1, Clusters: []lib.ScopeCluster{{Project: "member-a", Location: "us-central1", Cluster: "a1"}},
		UnlistedProjects: []string{"explicit-a", "member-b", "member-c", "member-d", "member-e", "member-f", "member-g", scTestManagement},
		UnlistedCount:    8, OldestListing: stamp}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("a map with bad entries =\n%+v\nwant\n%+v", got, want)
	}
	mustPart(t, got)
}

// TestScopeClustersListsAnExplicitProjectTheMapCarries: once the publisher
// lists the explicit and management projects too (the broker scope role's
// clusters-list widening), each is in the clusters map with no member row.
// Their clusters are listed like a member's, and neither is counted unlisted.
func TestScopeClustersListsAnExplicitProjectTheMapCarries(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	stamp := now.UTC().Format(time.RFC3339)
	doc := scParseDoc(t, scTestDocument(t, decl, now, false))
	doc.Clusters = json.RawMessage(`{
		"explicit-a": {"at": "` + stamp + `", "clusters": [["us-west1", "e1"]]},
		"` + scTestManagement + `": {"at": "` + stamp + `", "clusters": [["us-east4", "m1"]]},
		"member-a": {"at": "` + stamp + `", "clusters": [["us-central1", "a1"]]}
	}`)
	got := scBuild(decl, doc, now, "")
	want := lib.ScopeClusters{Count: 3, Clusters: []lib.ScopeCluster{
		{Project: "explicit-a", Location: "us-west1", Cluster: "e1"},
		{Project: "member-a", Location: "us-central1", Cluster: "a1"},
		{Project: scTestManagement, Location: "us-east4", Cluster: "m1"},
	}, UnlistedProjects: []string{"member-b", "member-c"}, UnlistedCount: 2, OldestListing: stamp}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("a map carrying the explicit and management projects =\n%+v\nwant\n%+v", got, want)
	}
	mustPart(t, got)
}

// TestScopeClustersAgeUnknownForAFutureListing: a listing stamped ahead of
// this clock gives no age, so the page leaves the oldest listing out rather
// than reporting the others' age as the page's.
func TestScopeClustersAgeUnknownForAFutureListing(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	doc := scParseDoc(t, scTestDocument(t, decl, now, false))
	future := now.Add(time.Hour).UTC().Format(time.RFC3339)
	past := now.Add(-time.Hour).UTC().Format(time.RFC3339)
	doc.Clusters = json.RawMessage(`{"member-a":{"at":"` + past + `","clusters":[["l","a"]]},"member-b":{"at":"` + future + `","clusters":[["l","b"]]}}`)
	got := scBuild(decl, doc, now, "")
	if got.Count != 2 || got.OldestListing != "" {
		t.Fatalf("a future listing: count %d oldest %q, want 2 and no age", got.Count, got.OldestListing)
	}
	doc.Clusters = json.RawMessage(`{"member-a":{"at":"` + past + `","clusters":[["l","a"]]}}`)
	if got := scBuild(decl, doc, now, ""); got.OldestListing != past {
		t.Fatalf("a past listing: oldest %q, want %q", got.OldestListing, past)
	}
}

// TestScopeClustersUnderAnUnboundedScope: a scope that declares no boundary
// admits any project not excluded, so the page is unknown and unbounded and
// names nothing as listed elsewhere, as though the rest were known.
func TestScopeClustersUnderAnUnboundedScope(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, `{"present":false,"exclude":{"clusters":[{"projectId":"member-b","location":"us-east1","clusterName":"dropped"}]}}`)
	now := time.Now()
	doc := scParseDoc(t, scTestDocument(t, scopeFrom(t, scTestScope), now, true))
	got := scBuild(decl, doc, now, "")
	if !got.Unbounded || !got.Unknown || !got.Truncated || len(got.UnlistedProjects) != 0 || got.UnlistedCount != 0 {
		t.Fatalf("an unbounded scope: %+v", got)
	}
	if slices.Contains(got.Clusters, lib.ScopeCluster{Project: "member-b", Location: "us-east1", Cluster: "dropped"}) {
		t.Fatalf("an excluded cluster is on an unbounded page: %+v", got.Clusters)
	}
	mustPart(t, got)
}

// TestScopeClustersUnderAClusterScopedParent: a parent scoped to one cluster
// is accepted for that cluster only, so its page is that cluster, or nothing
// when the cluster is out of scope, and it says the parent is scoped.
func TestScopeClustersUnderAClusterScopedParent(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	doc := scParseDoc(t, scTestDocument(t, decl, now, true))
	got := scBuild(decl, doc, now, clusterTarget{Project: "member-a", Location: "us-central1", Cluster: "a1"}.scope())
	want := lib.ScopeClusters{Count: 1, ParentScoped: true, Clusters: []lib.ScopeCluster{{Project: "member-a", Location: "us-central1", Cluster: "a1"}}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("cluster-scoped parent: %+v, want %+v", got, want)
	}
	mustPart(t, got)
	got = scBuild(decl, doc, now, clusterTarget{Project: "member-b", Location: "us-east1", Cluster: "dropped"}.scope())
	if !got.ParentScoped || got.Count != 0 || len(got.Clusters) != 0 || got.Truncated {
		t.Fatalf("a parent on an excluded cluster: %+v, want an empty scoped page", got)
	}
	mustPart(t, got)
}

// TestScopeClustersUnderANarrowerParentScope: a parent whose scope is
// neither a cluster nor the install's holds only what its scope path
// contains, as the delegation's parentScopeHolds does: a project scope gets
// that project's clusters, and a scope that contains no cluster gets none.
func TestScopeClustersUnderANarrowerParentScope(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	doc := scParseDoc(t, scTestDocument(t, decl, now, true))
	got := scBuild(decl, doc, now, capability.Scope(scopeKindProject+"/member-a"))
	want := lib.ScopeClusters{Count: 3, Clusters: []lib.ScopeCluster{
		{Project: "member-a", Location: "europe-west1", Cluster: "a3"},
		{Project: "member-a", Location: "us-central1", Cluster: "a1"},
		{Project: "member-a", Location: "us-central1", Cluster: "a2"},
	}, OldestListing: got.OldestListing}
	if !reflect.DeepEqual(got, want) || got.OldestListing == "" {
		t.Fatalf("a project-scoped parent: %+v, want %+v", got, want)
	}
	got = scBuild(decl, doc, now, capability.NamespaceScope("another-namespace"))
	if got.Count != 0 || len(got.Clusters) != 0 || got.UnlistedCount != 0 || got.Unknown {
		t.Fatalf("a parent whose scope holds no cluster: %+v, want an empty known page", got)
	}
	mustPart(t, got)
}

// TestScopeClustersWithNoScopeFile: with no declaration every target is
// refused, so the page is empty with a known count of zero, not unknown.
func TestScopeClustersWithNoScopeFile(t *testing.T) {
	t.Parallel()
	g := scGateway(nil)
	g.cfg.ScopeFile = filepath.Join(t.TempDir(), "absent.json")
	requester := TaskRequester{Backend: consoleBackend, Subject: "hmac:00"}
	got := g.profileScopeClusters(TaskRef{Addressee: "chat", Tier: string(capability.TierRead), Scope: installScope, Requester: &requester})
	want := lib.ScopeClusters{Clusters: []lib.ScopeCluster{}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("no scope file: %+v, want %+v", got, want)
	}
	mustPart(t, got)
}

// scScaleDocument is a resolved scope of rows member projects, each with
// perProject clusters in its listing when withClusters.
func scScaleDocument(t *testing.T, decl *scopeDeclaration, now time.Time, rows, perProject int, withClusters bool) string {
	t.Helper()
	stamp := now.UTC().Format(time.RFC3339)
	projects := make([]any, 0, rows)
	clusters := map[string]any{}
	for p := range rows {
		id := fmt.Sprintf("member-%05d-%s", p, strings.Repeat("x", 12))
		projects = append(projects, map[string]any{"id": id, "via": []string{"folders/123"}, "outcome": "ok", "confirmedAt": stamp})
		pairs := make([][]string, 0, perProject)
		for c := range perProject {
			pairs = append(pairs, []string{"us-central1-" + string(rune('a'+c%3)), fmt.Sprintf("cluster-%02d-%s", c, strings.Repeat("y", 30))})
		}
		clusters[id] = map[string]any{"at": stamp, "clusters": pairs}
	}
	doc := map[string]any{"v": 1, "resolvedAt": stamp, "memberHash": memberHash(decl), "state": "ok",
		"excludedProjects": []string{}, "projects": projects}
	if withClusters {
		doc["clusters"] = clusters
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return string(raw)
}

// TestScopeClustersAtScale: 5000 member rows, with a listing of two
// clusters each and without one. Each page builds in time linear in the
// rows (the document indexed once per build, its listing parsed once per
// read), inside a bound a tenfold regression would pass; the page is capped
// and in (project, location, cluster) order, the count is every cluster,
// and the part stays inside its bounds.
//
// It runs on its own, not in parallel with the suite's rigs, so the time
// bound measures the build rather than the suite's load.
func TestScopeClustersAtScale(t *testing.T) {
	const rows, perProject = 5000, 2
	// The bound on one build after the read. A build here takes about 5ms
	// with the map (3ms without); with the member hash and the row scan
	// per project it took about 650ms, and re-parsing the listing on each
	// build added about 9ms. A tenfold regression passes the bound.
	const buildBound = 40 * time.Millisecond
	explicit := make([]string, 0, 40)
	for i := range 40 {
		explicit = append(explicit, fmt.Sprintf("explicit-%03d", i))
	}
	declRaw, _ := json.Marshal(map[string]any{"present": true, "folders": []string{"123"}, "projects": explicit, "maxProjects": 10000})
	scope := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scope, declRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	decl := scopeFrom(t, string(declRaw))
	byTuple := func(a, b lib.ScopeCluster) int {
		return cmp.Or(strings.Compare(a.Project, b.Project), strings.Compare(a.Location, b.Location), strings.Compare(a.Cluster, b.Cluster))
	}

	for _, withClusters := range []bool{true, false} {
		now := time.Now()
		raw := scScaleDocument(t, decl, now, rows, perProject, withClusters)
		client := fake.NewSimpleClientset(&corev1.ConfigMap{
			ObjectMeta: metav1.ObjectMeta{Namespace: "kubeagents-system", Name: scTestConfigMap},
			Data:       map[string]string{resolvedScopeDataKey: raw},
		})
		g := scGateway(nil)
		g.cfg.ScopeFile = scope
		g.SetResolvedScope(client, "kubeagents-system", scTestConfigMap)
		requester := TaskRequester{Backend: consoleBackend, Subject: "hmac:00"}
		parent := TaskRef{Addressee: "chat", Tier: string(capability.TierRead), Scope: installScope, Requester: &requester}
		g.cfg.Namespace = chainTestNamespace

		got := g.profileScopeClusters(parent) // the read, the parse, and the first build
		var slowest time.Duration
		for range 5 {
			start := time.Now()
			again := g.profileScopeClusters(parent)
			slowest = max(slowest, time.Since(start))
			if !reflect.DeepEqual(again, got) {
				t.Fatalf("a second build differs: %+v", again)
			}
		}
		// The listing was parsed once for the document the source holds.
		cache := g.resolvedScope.(*configMapResolvedScope)
		doc, _ := cache.load(context.Background())
		first, _ := cache.clustersOf(doc)
		second, _ := cache.clustersOf(doc)
		if withClusters && (cache.listingDoc != doc || reflect.ValueOf(first).Pointer() != reflect.ValueOf(second).Pointer()) {
			t.Fatal("the parsed listing isn't kept for the document read")
		}
		part, err := lib.ScopeClustersPart(got)
		if err != nil {
			t.Fatalf("the part at scale is refused: %v", err)
		}
		if len(part.Data) > lib.ScopeClustersMaxBytes+len(`{"scopeClusters":}`) {
			t.Fatalf("the part is %d bytes", len(part.Data))
		}
		if withClusters {
			if got.Count != rows*perProject || len(got.Clusters) != lib.ScopeClustersPageMax || !got.Truncated || got.Unknown {
				t.Fatalf("count %d page %d truncated %v unknown %v, want %d, %d, true, false",
					got.Count, len(got.Clusters), got.Truncated, got.Unknown, rows*perProject, lib.ScopeClustersPageMax)
			}
			if !slices.IsSortedFunc(got.Clusters, byTuple) {
				t.Fatalf("the page is out of order: %+v", got.Clusters)
			}
			if got.UnlistedCount != len(explicit)+1 {
				t.Fatalf("unlisted %d, want %d", got.UnlistedCount, len(explicit)+1)
			}
		} else if !got.Unknown || got.Count != 0 || got.UnlistedCount != rows+len(explicit)+1 {
			t.Fatalf("without the map: unknown %v count %d unlisted %d, want unknown, 0, %d", got.Unknown, got.Count, got.UnlistedCount, rows+len(explicit)+1)
		}
		if len(got.UnlistedProjects) != lib.ScopeClustersUnlistedMax {
			t.Fatalf("%d unlisted named, want %d", len(got.UnlistedProjects), lib.ScopeClustersUnlistedMax)
		}
		if slowest > buildBound {
			t.Fatalf("with the map %v: a build over %d rows took %v, over %v", withClusters, rows, slowest, buildBound)
		}
		t.Logf("with the map %v: %d rows, %d-byte document, slowest build %v", withClusters, rows, len(raw), slowest)
	}
}
