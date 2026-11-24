package gateway

import (
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

// scBuildInput is buildScopeClusters' input for decl and doc under an
// install-scoped parent, with the gateway's own verdict.
func scBuildInput(decl *scopeDeclaration, doc *resolvedScope, now time.Time) scopeClustersInput {
	g := &Gateway{cfg: &Config{Namespace: "kubeagents-system", ManagementProject: scTestManagement},
		log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return scopeClustersInput{decl: decl, doc: doc, armed: true, now: now,
		parentScope: capability.NamespaceScope("kubeagents-system"), pageMax: lib.ScopeClustersPageMax,
		management: scTestManagement, verdict: g.scopeVerdict, holds: g.parentScopeHolds}
}

func scParseDoc(t *testing.T, raw string) *resolvedScope {
	t.Helper()
	var doc resolvedScope
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		t.Fatal(err)
	}
	return &doc
}

// TestScopeClustersUnknownCases: a too-large document, an unreadable one,
// and a map that doesn't parse all read as unknown; a malformed map never
// costs the admissions the rows decide.
func TestScopeClustersUnknownCases(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	good := scParseDoc(t, scTestDocument(t, decl, now, true))

	tooLarge := *good
	tooLarge.State = "too-large"
	tooLarge.Projects = nil
	malformed := *good
	malformed.Clusters = json.RawMessage(`{"member-a":{"at":"x","clusters":[["us-central1",5]]}}`)
	for name, doc := range map[string]*resolvedScope{"too large": &tooLarge, "unreadable": nil, "malformed map": &malformed} {
		got := buildScopeClusters(scBuildInput(decl, doc, now))
		if !got.Unknown || !got.Truncated || got.Count != 0 {
			t.Errorf("%s: %+v, want unknown, truncated, nothing counted", name, got)
		}
		if _, err := lib.ScopeClustersPart(got); err != nil {
			t.Errorf("%s: the part is refused: %v", name, err)
		}
	}
	g := &Gateway{cfg: &Config{}, log: slog.New(slog.NewTextHandler(io.Discard, nil)), resolvedScope: &fakeResolvedSource{doc: &malformed}}
	if err := g.checkScope(decl, clusterTarget{Project: "member-a", Location: "l", Cluster: "c"}); err != nil {
		t.Fatalf("a malformed clusters map refused a resolved member: %v", err)
	}
	// And a document read whole with a map of the wrong type still admits.
	odd := strings.Replace(scTestDocument(t, decl, now, false), `"v":1`, `"v":1,"clusters":5`, 1)
	if doc := scParseDoc(t, odd); checkResolvedScope(doc, decl, now, clusterTarget{Project: "member-a", Location: "l", Cluster: "c"}) != nil {
		t.Fatal("a clusters value of the wrong type cost a resolved member its admission")
	}
}

// TestScopeClustersUnderAClusterScopedParent: a parent scoped to one cluster
// is accepted for that cluster only, so its page is that cluster, or nothing
// when the cluster is out of scope.
func TestScopeClustersUnderAClusterScopedParent(t *testing.T) {
	t.Parallel()
	decl := scopeFrom(t, scTestScope)
	now := time.Now()
	doc := scParseDoc(t, scTestDocument(t, decl, now, true))
	in := scBuildInput(decl, doc, now)
	in.parentScope = clusterTarget{Project: "member-a", Location: "us-central1", Cluster: "a1"}.scope()
	got := buildScopeClusters(in)
	want := lib.ScopeClusters{Count: 1, Clusters: []lib.ScopeCluster{{Project: "member-a", Location: "us-central1", Cluster: "a1"}}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("cluster-scoped parent: %+v, want %+v", got, want)
	}
	in.parentScope = clusterTarget{Project: "member-b", Location: "us-east1", Cluster: "dropped"}.scope()
	if got := buildScopeClusters(in); got.Count != 0 || len(got.Clusters) != 0 || got.Truncated {
		t.Fatalf("a parent on an excluded cluster: %+v, want an empty page", got)
	}
}

// TestScopeClustersAtScale: thousands of clusters over hundreds of projects.
// The document parses and the page builds quickly, the page is capped at its
// size and in order, the count is every cluster, and the part stays inside
// its bounds.
func TestScopeClustersAtScale(t *testing.T) {
	t.Parallel()
	const projects, perProject = 500, 10
	explicit := make([]string, 0, 40)
	for i := range 40 {
		explicit = append(explicit, fmt.Sprintf("explicit-%03d", i))
	}
	declRaw, _ := json.Marshal(map[string]any{"present": true, "folders": []string{"123"}, "projects": explicit, "maxProjects": 1000})
	decl := scopeFrom(t, string(declRaw))
	now := time.Now()
	stamp := now.UTC().Format(time.RFC3339)
	rows := make([]any, 0, projects)
	clusters := map[string]any{}
	for p := range projects {
		id := fmt.Sprintf("member-%04d-%s", p, strings.Repeat("x", 12))
		rows = append(rows, map[string]any{"id": id, "via": []string{"folders/123"}, "outcome": "ok", "confirmedAt": stamp})
		pairs := make([][]string, 0, perProject)
		for c := range perProject {
			pairs = append(pairs, []string{"us-central1-" + string(rune('a'+c%3)), fmt.Sprintf("cluster-%02d-%s", c, strings.Repeat("y", 30))})
		}
		clusters[id] = map[string]any{"at": stamp, "clusters": pairs}
	}
	raw, _ := json.Marshal(map[string]any{"v": 1, "resolvedAt": stamp, "memberHash": memberHash(decl), "state": "ok",
		"excludedProjects": []string{}, "projects": rows, "clusters": clusters})

	start := time.Now()
	doc := scParseDoc(t, string(raw))
	got := buildScopeClusters(scBuildInput(decl, doc, now))
	part, err := lib.ScopeClustersPart(got)
	elapsed := time.Since(start)
	if err != nil {
		t.Fatalf("the part at scale is refused: %v", err)
	}
	if got.Count != projects*perProject || len(got.Clusters) != lib.ScopeClustersPageMax || !got.Truncated || got.Unknown {
		t.Fatalf("count %d page %d truncated %v unknown %v, want %d, %d, true, false",
			got.Count, len(got.Clusters), got.Truncated, got.Unknown, projects*perProject, lib.ScopeClustersPageMax)
	}
	if !slices.IsSortedFunc(got.Clusters, func(a, b lib.ScopeCluster) int {
		return strings.Compare(a.Project+"/"+a.Location+"/"+a.Cluster, b.Project+"/"+b.Location+"/"+b.Cluster)
	}) {
		t.Fatalf("the page is out of order: %+v", got.Clusters)
	}
	if got.UnlistedCount != len(explicit)+1 || len(got.UnlistedProjects) != lib.ScopeClustersUnlistedMax {
		t.Fatalf("unlisted %d named of %d, want %d of %d", len(got.UnlistedProjects), got.UnlistedCount, lib.ScopeClustersUnlistedMax, len(explicit)+1)
	}
	if len(part.Data) > lib.ScopeClustersMaxBytes+len(`{"scopeClusters":}`) {
		t.Fatalf("the part is %d bytes", len(part.Data))
	}
	// Generous, so a loaded machine passes: the build is milliseconds.
	if elapsed > 2*time.Second {
		t.Fatalf("parsing %d bytes and building the page took %v", len(raw), elapsed)
	}
	t.Logf("%d clusters, %d-byte document: parsed and built in %v", got.Count, len(raw), elapsed)
}
