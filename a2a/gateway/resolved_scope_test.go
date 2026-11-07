package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

var resolvedNow = time.Date(2026, 11, 2, 14, 30, 0, 0, time.UTC)

func folderScope(t *testing.T) *scopeDeclaration {
	t.Helper()
	return scopeFrom(t, `{"present":true,"projects":["p0"],"folders":["123"],"exclude":{"projects":["legacy"]}}`)
}

func freshDoc(decl *scopeDeclaration, rows ...resolvedRow) *resolvedScope {
	return &resolvedScope{V: 1, ResolvedAt: resolvedNow.Add(-5 * time.Minute).Format(time.RFC3339),
		MemberHash: memberHash(decl), State: "ok", ExcludedProjects: []string{"team-x"}, Projects: rows}
}

func okRow(id string) resolvedRow {
	return resolvedRow{ID: id, Via: []string{"folders/123"}, Outcome: "ok",
		ConfirmedAt: resolvedNow.Add(-5 * time.Minute).Format(time.RFC3339)}
}

func TestMemberHash_MatchesTheSharedVectors(t *testing.T) {
	raw, err := os.ReadFile("testdata/resolved-scope-member-hash.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Vectors []struct {
			Scope json.RawMessage `json:"scope"`
			Hash  string          `json:"hash"`
		} `json:"vectors"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	if len(file.Vectors) == 0 {
		t.Fatal("no vectors")
	}
	for i, v := range file.Vectors {
		if got := memberHash(scopeFrom(t, string(v.Scope))); got != v.Hash {
			t.Errorf("vector %d: memberHash = %s, want %s", i, got, v.Hash)
		}
	}
}

func TestMemberHash_IgnoresExcludedClustersAndOrder(t *testing.T) {
	a := scopeFrom(t, `{"present":true,"folders":["2","1"],"exclude":{"projects":["b","a"]}}`)
	b := scopeFrom(t, `{"present":true,"folders":["1","2","2"],"exclude":{"projects":["a","b"],"clusters":[{"projectId":"p","location":"l","clusterName":"c"}]}}`)
	if memberHash(a) != memberHash(b) {
		t.Fatal("order, duplicates or exclude.clusters moved the hash")
	}
	c := scopeFrom(t, `{"present":true,"folders":["1","2"],"maxProjects":200,"exclude":{"projects":["a","b"]}}`)
	if memberHash(a) == memberHash(c) {
		t.Fatal("maxProjects did not move the hash")
	}
}

func TestResolvedScopeBound(t *testing.T) {
	for _, tc := range []struct {
		max  int
		want time.Duration
	}{{100, 40 * time.Minute}, {101, 50 * time.Minute}, {1000, 130 * time.Minute}} {
		if got := resolvedScopeBound(tc.max); got != tc.want {
			t.Errorf("bound(%d) = %v, want %v", tc.max, got, tc.want)
		}
	}
}

func TestCheckResolvedScope(t *testing.T) {
	decl := folderScope(t)
	target := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}
	stale := resolvedNow.Add(-time.Hour).Format(time.RFC3339)
	cases := []struct {
		name        string
		doc         func() *resolvedScope
		wantRefusal string
	}{
		{name: "a fresh ok row", doc: func() *resolvedScope { return freshDoc(decl, okRow("p1")) }},
		{name: "no document", doc: func() *resolvedScope { return nil }, wantRefusal: "the resolved scope is stale"},
		{name: "an unknown version", doc: func() *resolvedScope { d := freshDoc(decl, okRow("p1")); d.V = 2; return d },
			wantRefusal: "the resolved scope is stale"},
		{name: "a too-large document", doc: func() *resolvedScope { d := freshDoc(decl); d.State = "too-large"; return d },
			wantRefusal: "the resolved scope reads too-large"},
		{name: "an error document", doc: func() *resolvedScope { d := freshDoc(decl, okRow("p1")); d.State = "error"; return d },
			wantRefusal: "the resolved scope reads error"},
		{name: "a resolvedAt from the future", doc: func() *resolvedScope {
			d := freshDoc(decl, okRow("p1"))
			d.ResolvedAt = resolvedNow.Add(time.Hour).Format(time.RFC3339)
			return d
		}, wantRefusal: "the resolved scope is stale"},
		{name: "a resolvedAt within the clock skew", doc: func() *resolvedScope {
			d := freshDoc(decl, okRow("p1"))
			d.ResolvedAt = resolvedNow.Add(30 * time.Second).Format(time.RFC3339)
			return d
		}},
		{name: "a confirmedAt from the future", doc: func() *resolvedScope {
			r := okRow("p1")
			r.ConfirmedAt = resolvedNow.Add(time.Hour).Format(time.RFC3339)
			return freshDoc(decl, r)
		}, wantRefusal: "can't confirm p1/us-east1/prod is in this install's scope yet"},
		{name: "another declaration's document", doc: func() *resolvedScope { d := freshDoc(decl, okRow("p1")); d.MemberHash = "sha256:00"; return d },
			wantRefusal: "the resolved scope is stale"},
		{name: "a stale document", doc: func() *resolvedScope { d := freshDoc(decl, okRow("p1")); d.ResolvedAt = stale; return d },
			wantRefusal: "the resolved scope is stale"},
		{name: "an unparsable resolvedAt", doc: func() *resolvedScope { d := freshDoc(decl, okRow("p1")); d.ResolvedAt = "yesterday"; return d },
			wantRefusal: "the resolved scope is stale"},
		{name: "a project the publisher excluded", doc: func() *resolvedScope {
			d := freshDoc(decl, okRow("p1"))
			d.ExcludedProjects = []string{"p1"}
			return d
		}, wantRefusal: "excluded"},
		{name: "no row", doc: func() *resolvedScope { return freshDoc(decl, okRow("p9")) },
			wantRefusal: "can't confirm p1/us-east1/prod is in this install's scope yet"},
		{name: "a denied row", doc: func() *resolvedScope {
			r := okRow("p1")
			r.Outcome = "denied"
			return freshDoc(decl, r)
		}, wantRefusal: "its project can't be read: denied"},
		{name: "a row past the bound", doc: func() *resolvedScope {
			r := okRow("p1")
			r.ConfirmedAt = stale
			return freshDoc(decl, r)
		}, wantRefusal: "can't confirm p1/us-east1/prod is in this install's scope yet"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkResolvedScope(tc.doc(), decl, resolvedNow, target)
			if tc.wantRefusal == "" {
				if err != nil {
					t.Fatalf("err = %v, want admitted", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.wantRefusal) {
				t.Fatalf("err = %v, want a refusal containing %q", err, tc.wantRefusal)
			}
		})
	}
}

func TestCheckTargetScope_SelectorsNeedTheResolvedScope(t *testing.T) {
	target := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}
	for _, scope := range []string{
		`{"present":true,"folders":["123"]}`,
		`{"present":true,"organizations":["456"]}`,
		`{"present":true,"sharedVpcHosts":["host-a"]}`,
		`{"present":true,"metricsScopes":["scope-p"]}`,
	} {
		err := checkTargetScope(scopeFrom(t, scope), "", target)
		var unresolved errUnresolved
		if !errors.As(err, &unresolved) {
			t.Errorf("%s: err = %v, want errUnresolved", scope, err)
		}
		var r errRefused
		if !errors.As(err, &r) || !strings.Contains(r.notice, "can't confirm") {
			t.Errorf("%s: the refusal notice is %v, want can't confirm", scope, err)
		}
	}
	// A listed project, the management project and an exclusion keep their direct answers.
	if err := checkTargetScope(scopeFrom(t, `{"present":true,"sharedVpcHosts":["h"],"projects":["p1"]}`), "", target); err != nil {
		t.Errorf("a listed project: %v", err)
	}
	if err := checkTargetScope(scopeFrom(t, `{"present":true,"metricsScopes":["s"]}`), "p1", target); err != nil {
		t.Errorf("the management project: %v", err)
	}
	err := checkTargetScope(scopeFrom(t, `{"present":true,"folders":["1"],"exclude":{"projects":["p*"]}}`), "", target)
	var unresolved errUnresolved
	if err == nil || errors.As(err, &unresolved) || !strings.Contains(err.Error(), "excluded") {
		t.Errorf("an excluded project: %v, want a plain excluded refusal", err)
	}
}

func TestProfileRouteForConfirmsAFolderMemberAgainstTheResolvedScope(t *testing.T) {
	scopeFile := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(scopeFile, []byte(`{"present":true,"folders":["123"]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	decl := scopeFrom(t, `{"present":true,"folders":["123"]}`)
	now := time.Now().UTC().Format(time.RFC3339)
	src := &fakeResolvedSource{doc: &resolvedScope{V: 1, ResolvedAt: now, MemberHash: memberHash(decl), State: "ok",
		Projects: []resolvedRow{{ID: "p1", Outcome: "ok", ConfirmedAt: now}}}}
	g := &Gateway{cfg: &Config{ScopeFile: scopeFile, Namespace: "ns"}, log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		profiles: testDirectory(), resolvedScope: src}

	route, err := g.profileRouteFor("cluster", targetParams("p1", "us-east1", "dev"), "", "")
	if err != nil || route.Params["project"] != "p1" {
		t.Fatalf("a resolved folder member: route=%+v err=%v", route, err)
	}
	if _, err := g.profileRouteFor("cluster", targetParams("p2", "us-east1", "dev"), "", ""); err == nil ||
		!strings.Contains(err.Error(), "can't confirm p2/us-east1/dev is in this install's scope yet") {
		t.Fatalf("a project the resolved scope does not list: %v", err)
	}
}

type fakeResolvedSource struct {
	doc   *resolvedScope
	err   error
	calls int
}

func (f *fakeResolvedSource) load(context.Context) (*resolvedScope, error) {
	f.calls++
	return f.doc, f.err
}

func TestGatewayCheckScope(t *testing.T) {
	decl := folderScope(t)
	target := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	now := time.Now().UTC()
	doc := &resolvedScope{V: 1, ResolvedAt: now.Format(time.RFC3339), MemberHash: memberHash(decl), State: "ok",
		Projects: []resolvedRow{{ID: "p1", Outcome: "ok", ConfirmedAt: now.Format(time.RFC3339)}}}

	src := &fakeResolvedSource{doc: doc}
	g := &Gateway{cfg: &Config{}, log: logger, resolvedScope: src}
	if err := g.checkScope(decl, target); err != nil {
		t.Fatalf("a resolved member: %v", err)
	}
	if err := g.checkScope(decl, clusterTarget{Project: "p0", Location: "l", Cluster: "c"}); err != nil || src.calls != 1 {
		t.Fatalf("a listed project should not read the resolved scope: err=%v calls=%d", err, src.calls)
	}

	g.resolvedScope = &fakeResolvedSource{err: errors.New("forbidden")}
	if err := g.checkScope(decl, target); err == nil || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("an unreadable ConfigMap: %v, want a stale refusal", err)
	}

	g.resolvedScope = nil
	if err := g.checkScope(decl, target); err == nil || !strings.Contains(err.Error(), "can't confirm p1/us-east1/prod is in this install's scope yet") {
		t.Fatalf("no resolved scope configured: %v", err)
	}
}

func TestConfigMapResolvedScope_ReadsAndCaches(t *testing.T) {
	decl := folderScope(t)
	body, _ := json.Marshal(freshDoc(decl, okRow("p1")))
	client := fake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "pa-resolved-scope", Namespace: "ns"},
		Data:       map[string]string{resolvedScopeDataKey: string(body)},
	})
	src := newConfigMapResolvedScope(client, "ns", "pa-resolved-scope")
	clock := resolvedNow
	src.now = func() time.Time { return clock }
	doc, err := src.load(context.Background())
	if err != nil || doc == nil || len(doc.Projects) != 1 || doc.Projects[0].ID != "p1" {
		t.Fatalf("load = %+v, %v", doc, err)
	}

	// Within the cache window the ConfigMap is not read again.
	if err := client.CoreV1().ConfigMaps("ns").Delete(context.Background(), "pa-resolved-scope", metav1.DeleteOptions{}); err != nil {
		t.Fatal(err)
	}
	if doc, err := src.load(context.Background()); err != nil || doc == nil {
		t.Fatalf("cached load = %+v, %v", doc, err)
	}
	clock = clock.Add(resolvedScopeCacheTTL)
	if _, err := src.load(context.Background()); err == nil {
		t.Fatal("a read past the cache window should see the ConfigMap gone")
	}
}

func TestConfigMapResolvedScope_IgnoresThePublishersOwnFields(t *testing.T) {
	// numbers and carried are the publisher's carry-forward state; the gateway reads past them.
	decl := folderScope(t)
	body, _ := json.Marshal(freshDoc(decl, okRow("p1")))
	withState := strings.TrimSuffix(string(body), "}") +
		`,"numbers":{"111222333444":"p1"},"carried":[{"id":"222333444555","via":["metricsScopes/hub"],"outcome":"unreachable"}]}`
	client := fake.NewSimpleClientset(&corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: "pa-resolved-scope", Namespace: "ns"},
		Data:       map[string]string{resolvedScopeDataKey: withState},
	})
	doc, err := newConfigMapResolvedScope(client, "ns", "pa-resolved-scope").load(context.Background())
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if err := checkResolvedScope(doc, decl, resolvedNow, clusterTarget{Project: "p1", Location: "l", Cluster: "c"}); err != nil {
		t.Fatalf("a fresh ok row next to the publisher's own fields: %v", err)
	}
}

func TestConfigMapResolvedScope_EmptyObjectIsNoDocument(t *testing.T) {
	client := fake.NewSimpleClientset(&corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Name: "pa-resolved-scope", Namespace: "ns"}})
	_, err := newConfigMapResolvedScope(client, "ns", "pa-resolved-scope").load(context.Background())
	if !errors.Is(err, errNoResolvedScope) {
		t.Fatalf("err = %v, want errNoResolvedScope", err)
	}
}
