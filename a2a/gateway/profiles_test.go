package gateway

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func targetParams(project, location, cluster string) map[string]string {
	return map[string]string{"project": project, "location": location, "cluster": cluster}
}

var clusterParams = []lib.ProfileParameter{{Name: "project", Required: true}, {Name: "location", Required: true}, {Name: "cluster", Required: true}}

func testDirectory() *profileDirectory {
	d := newProfileDirectory()
	d.put("cluster", &lib.AgentCard{Name: "cluster", Parameters: clusterParams})
	d.put("prod-east", &lib.AgentCard{Name: "prod-east", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	d.put("chat", &lib.AgentCard{Name: "chat"})
	return d
}

func refusalOf(t *testing.T, err error) string {
	t.Helper()
	var r errRefused
	if !errors.As(err, &r) {
		t.Fatalf("err = %v, want a refusal", err)
	}
	return r.notice
}

func TestResolveProfile(t *testing.T) {
	d := testDirectory()
	cases := []struct {
		name, addressee string
		params          map[string]string
		wantProfile     string
		wantTarget      string
		wantRefusal     string
	}{
		{name: "generic with an unpinned target", addressee: "cluster", params: targetParams("p1", "us-east1", "dev"),
			wantProfile: "cluster", wantTarget: "p1/us-east1/dev"},
		{name: "generic with a pinned target goes to the pinned profile", addressee: "cluster", params: targetParams("p1", "us-east1", "prod"),
			wantProfile: "prod-east", wantTarget: "p1/us-east1/prod"},
		{name: "pinned with its own target", addressee: "prod-east", params: targetParams("p1", "us-east1", "prod"),
			wantProfile: "prod-east", wantTarget: "p1/us-east1/prod"},
		{name: "pinned with no target takes its own cluster", addressee: "prod-east",
			wantProfile: "prod-east", wantTarget: "p1/us-east1/prod"},
		{name: "pinned addressed for another cluster", addressee: "prod-east", params: targetParams("p1", "us-east1", "dev"),
			wantRefusal: "works only on cluster p1/us-east1/prod, not p1/us-east1/dev"},
		{name: "generic with no target", addressee: "cluster", wantRefusal: "needs a target cluster"},
		{name: "pinned with half a target", addressee: "prod-east", params: map[string]string{"cluster": "dev"},
			wantRefusal: "needs a target cluster"},
		{name: "generic with half a target", addressee: "cluster", params: map[string]string{"project": "p1"},
			wantRefusal: "needs a target cluster"},
		{name: "a profile that takes no target, given one", addressee: "chat", params: targetParams("p1", "us-east1", "dev"),
			wantRefusal: "doesn't take a target cluster"},
		{name: "a profile that takes no target", addressee: "chat", wantProfile: "chat"},
		{name: "unknown profile", addressee: "nope", wantRefusal: `no agent profile named "nope"`},
		{name: "a parameter value that would split the resource path", addressee: "cluster", params: targetParams("p1", "us-east1", "a/b"),
			wantRefusal: "parameters aren't valid"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			profile, target, err := resolveProfile(d, tc.addressee, tc.params)
			if tc.wantRefusal != "" {
				if got := refusalOf(t, err); !strings.Contains(got, tc.wantRefusal) {
					t.Fatalf("refusal = %q, want it to contain %q", got, tc.wantRefusal)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			if profile != tc.wantProfile {
				t.Fatalf("profile = %q, want %q", profile, tc.wantProfile)
			}
			got := ""
			if target != nil {
				got = target.String()
			}
			if got != tc.wantTarget {
				t.Fatalf("target = %q, want %q", got, tc.wantTarget)
			}
		})
	}
}

func TestResolveProfile_TwoProfilesPinnedToOneClusterIsRefused(t *testing.T) {
	d := testDirectory()
	d.put("prod-east-2", &lib.AgentCard{Name: "prod-east-2", Parameters: clusterParams,
		ClusterRef: &lib.ClusterRef{ProjectID: "p1", Location: "us-east1", Cluster: "prod"}})
	_, _, err := resolveProfile(d, "cluster", targetParams("p1", "us-east1", "prod"))
	if got := refusalOf(t, err); !strings.Contains(got, "more than one agent profile is pinned") {
		t.Fatalf("refusal = %q", got)
	}
}

func TestResolveProfile_ADeletedCardIsForgotten(t *testing.T) {
	d := testDirectory()
	d.put("prod-east", nil)
	profile, _, err := resolveProfile(d, "cluster", targetParams("p1", "us-east1", "prod"))
	if err != nil || profile != "cluster" {
		t.Fatalf("profile, err = %q, %v; want the generic profile once the pinned one is gone", profile, err)
	}
}

func TestTargetScopeIsTheVerifiersResourceForm(t *testing.T) {
	got := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}.scope()
	if got != "project/p1/location/us-east1/cluster/prod" {
		t.Fatalf("scope = %q", got)
	}
	if err := got.Validate(); err != nil {
		t.Fatal(err)
	}
}

func scopeFrom(t *testing.T, body string) *scopeDeclaration {
	t.Helper()
	path := filepath.Join(t.TempDir(), "scope.json")
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	d, err := loadScope(path)
	if err != nil {
		t.Fatal(err)
	}
	return d
}

func TestCheckTargetScope(t *testing.T) {
	target := clusterTarget{Project: "p1", Location: "us-east1", Cluster: "prod"}
	cases := []struct {
		name, scope, management string
		wantRefusal             string
	}{
		{name: "a listed project", scope: `{"present":true,"projects":["p1"]}`},
		{name: "a listed project a glob excludes", scope: `{"present":true,"projects":["p1"],"exclude":{"projects":["p*"]}}`,
			wantRefusal: "excluded"},
		{name: "a glob that doesn't match", scope: `{"present":true,"projects":["p1"],"exclude":{"projects":["q*","p1?"]}}`},
		{name: "a number entry never matches a project ID", scope: `{"present":true,"projects":["p1"],"exclude":{"projects":["123"]}}`},
		{name: "an entry that can't compile refuses", scope: `{"present":true,"projects":["p1"],"exclude":{"projects":["[z-a]*"]}}`,
			wantRefusal: "can't confirm"},
		{name: "the management project, unlisted", scope: `{"present":true,"projects":["p2"]}`, management: "p1"},
		{name: "the management project under a folder scope", scope: `{"present":true,"folders":["123"]}`, management: "p1"},
		{name: "an exclusion matching the management project is ignored",
			scope: `{"present":true,"projects":["p2"],"exclude":{"projects":["p*"]}}`, management: "p1"},
		{name: "the management project's cluster can still be excluded",
			scope:       `{"present":true,"exclude":{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"prod"}]}}`,
			management:  "p1",
			wantRefusal: "excluded"},
		{name: "another install's management project is not this one's", scope: `{"present":true,"projects":["p2"]}`, management: "p9",
			wantRefusal: "outside this install's scope"},
		{name: "an unlisted project", scope: `{"present":true,"projects":["p2"]}`, wantRefusal: "outside this install's scope"},
		{name: "an excluded cluster in a listed project",
			scope:       `{"present":true,"projects":["p1"],"exclude":{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"prod"}]}}`,
			wantRefusal: "excluded"},
		{name: "an excluded project", scope: `{"present":true,"projects":["p1"],"exclude":{"projects":["p1"]}}`, wantRefusal: "excluded"},
		{name: "a folder scope can't confirm an unlisted project", scope: `{"present":true,"folders":["123"]}`,
			wantRefusal: "can't confirm p1/us-east1/prod is in this install's scope yet"},
		{name: "an organization scope can't confirm either", scope: `{"present":true,"organizations":["456"]}`,
			wantRefusal: "can't confirm"},
		{name: "a folder scope that also lists the project", scope: `{"present":true,"folders":["123"],"projects":["p1"]}`},
		{name: "no declared scope draws no boundary", scope: `{"present":false}`},
		{name: "no declared scope still honours an exclusion",
			scope:       `{"present":false,"exclude":{"clusters":[{"projectId":"p1","location":"us-east1","clusterName":"prod"}]}}`,
			wantRefusal: "excluded"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkTargetScope(scopeFrom(t, tc.scope), tc.management, target)
			if tc.wantRefusal == "" {
				if err != nil {
					t.Fatalf("err = %v, want in scope", err)
				}
				return
			}
			if got := refusalOf(t, err); !strings.Contains(got, tc.wantRefusal) {
				t.Fatalf("refusal = %q, want it to contain %q", got, tc.wantRefusal)
			}
		})
	}
}

// The expected values are Python's fnmatch.fnmatchcase, which the reconcile's
// _excluded_by uses, so the gateway and the reconcile drop the same projects.
func TestProjectExcludedMatchesTheReconcile(t *testing.T) {
	cases := []struct {
		pattern, project string
		want             bool
	}{
		{"ops-*", "ops-a", true},
		{"ops-*", "ops-ab", true},
		{"ops-*", "ops", false},
		{"ops-*", "pay-prod", false},
		{"ops-*", "a1", false},
		{"ops-*", "b1", false},
		{"ops-*", "]1", false},
		{"ops-*", "x1", false},
		{"ops-*", "a-1", false},
		{"ops-*", "d-1", false},
		{"ops-*", "legacy", false},
		{"ops-*", "legacyx", false},
		{"ops-*", "[abc", false},
		{"ops-*", "sand7", false},
		{"ops-*", "sandx", false},
		{"ops-*", "axbyc", false},
		{"ops-*", "ab", false},
		{"ops-*", "x\\y", false},
		{"ops-*", "^p", false},
		{"ops-*", "p1", false},
		{"ops-*", "q1", false},
		{"ops-?", "ops-a", true},
		{"ops-?", "ops-ab", false},
		{"ops-?", "ops", false},
		{"ops-?", "pay-prod", false},
		{"ops-?", "a1", false},
		{"ops-?", "b1", false},
		{"ops-?", "]1", false},
		{"ops-?", "x1", false},
		{"ops-?", "a-1", false},
		{"ops-?", "d-1", false},
		{"ops-?", "legacy", false},
		{"ops-?", "legacyx", false},
		{"ops-?", "[abc", false},
		{"ops-?", "sand7", false},
		{"ops-?", "sandx", false},
		{"ops-?", "axbyc", false},
		{"ops-?", "ab", false},
		{"ops-?", "x\\y", false},
		{"ops-?", "^p", false},
		{"ops-?", "p1", false},
		{"ops-?", "q1", false},
		{"*-prod", "ops-a", false},
		{"*-prod", "ops-ab", false},
		{"*-prod", "ops", false},
		{"*-prod", "pay-prod", true},
		{"*-prod", "a1", false},
		{"*-prod", "b1", false},
		{"*-prod", "]1", false},
		{"*-prod", "x1", false},
		{"*-prod", "a-1", false},
		{"*-prod", "d-1", false},
		{"*-prod", "legacy", false},
		{"*-prod", "legacyx", false},
		{"*-prod", "[abc", false},
		{"*-prod", "sand7", false},
		{"*-prod", "sandx", false},
		{"*-prod", "axbyc", false},
		{"*-prod", "ab", false},
		{"*-prod", "x\\y", false},
		{"*-prod", "^p", false},
		{"*-prod", "p1", false},
		{"*-prod", "q1", false},
		{"[!a]*", "ops-a", true},
		{"[!a]*", "ops-ab", true},
		{"[!a]*", "ops", true},
		{"[!a]*", "pay-prod", true},
		{"[!a]*", "a1", false},
		{"[!a]*", "b1", true},
		{"[!a]*", "]1", true},
		{"[!a]*", "x1", true},
		{"[!a]*", "a-1", false},
		{"[!a]*", "d-1", true},
		{"[!a]*", "legacy", true},
		{"[!a]*", "legacyx", true},
		{"[!a]*", "[abc", true},
		{"[!a]*", "sand7", true},
		{"[!a]*", "sandx", true},
		{"[!a]*", "axbyc", false},
		{"[!a]*", "ab", false},
		{"[!a]*", "x\\y", true},
		{"[!a]*", "^p", true},
		{"[!a]*", "p1", true},
		{"[!a]*", "q1", true},
		{"[]x]*", "ops-a", false},
		{"[]x]*", "ops-ab", false},
		{"[]x]*", "ops", false},
		{"[]x]*", "pay-prod", false},
		{"[]x]*", "a1", false},
		{"[]x]*", "b1", false},
		{"[]x]*", "]1", true},
		{"[]x]*", "x1", true},
		{"[]x]*", "a-1", false},
		{"[]x]*", "d-1", false},
		{"[]x]*", "legacy", false},
		{"[]x]*", "legacyx", false},
		{"[]x]*", "[abc", false},
		{"[]x]*", "sand7", false},
		{"[]x]*", "sandx", false},
		{"[]x]*", "axbyc", false},
		{"[]x]*", "ab", false},
		{"[]x]*", "x\\y", true},
		{"[]x]*", "^p", false},
		{"[]x]*", "p1", false},
		{"[]x]*", "q1", false},
		{"[a-c]-1", "ops-a", false},
		{"[a-c]-1", "ops-ab", false},
		{"[a-c]-1", "ops", false},
		{"[a-c]-1", "pay-prod", false},
		{"[a-c]-1", "a1", false},
		{"[a-c]-1", "b1", false},
		{"[a-c]-1", "]1", false},
		{"[a-c]-1", "x1", false},
		{"[a-c]-1", "a-1", true},
		{"[a-c]-1", "d-1", false},
		{"[a-c]-1", "legacy", false},
		{"[a-c]-1", "legacyx", false},
		{"[a-c]-1", "[abc", false},
		{"[a-c]-1", "sand7", false},
		{"[a-c]-1", "sandx", false},
		{"[a-c]-1", "axbyc", false},
		{"[a-c]-1", "ab", false},
		{"[a-c]-1", "x\\y", false},
		{"[a-c]-1", "^p", false},
		{"[a-c]-1", "p1", false},
		{"[a-c]-1", "q1", false},
		{"legacy", "ops-a", false},
		{"legacy", "ops-ab", false},
		{"legacy", "ops", false},
		{"legacy", "pay-prod", false},
		{"legacy", "a1", false},
		{"legacy", "b1", false},
		{"legacy", "]1", false},
		{"legacy", "x1", false},
		{"legacy", "a-1", false},
		{"legacy", "d-1", false},
		{"legacy", "legacy", true},
		{"legacy", "legacyx", false},
		{"legacy", "[abc", false},
		{"legacy", "sand7", false},
		{"legacy", "sandx", false},
		{"legacy", "axbyc", false},
		{"legacy", "ab", false},
		{"legacy", "x\\y", false},
		{"legacy", "^p", false},
		{"legacy", "p1", false},
		{"legacy", "q1", false},
		{"[abc", "ops-a", false},
		{"[abc", "ops-ab", false},
		{"[abc", "ops", false},
		{"[abc", "pay-prod", false},
		{"[abc", "a1", false},
		{"[abc", "b1", false},
		{"[abc", "]1", false},
		{"[abc", "x1", false},
		{"[abc", "a-1", false},
		{"[abc", "d-1", false},
		{"[abc", "legacy", false},
		{"[abc", "legacyx", false},
		{"[abc", "[abc", true},
		{"[abc", "sand7", false},
		{"[abc", "sandx", false},
		{"[abc", "axbyc", false},
		{"[abc", "ab", false},
		{"[abc", "x\\y", false},
		{"[abc", "^p", false},
		{"[abc", "p1", false},
		{"[abc", "q1", false},
		{"*", "ops-a", true},
		{"*", "ops-ab", true},
		{"*", "ops", true},
		{"*", "pay-prod", true},
		{"*", "a1", true},
		{"*", "b1", true},
		{"*", "]1", true},
		{"*", "x1", true},
		{"*", "a-1", true},
		{"*", "d-1", true},
		{"*", "legacy", true},
		{"*", "legacyx", true},
		{"*", "[abc", true},
		{"*", "sand7", true},
		{"*", "sandx", true},
		{"*", "axbyc", true},
		{"*", "ab", true},
		{"*", "x\\y", true},
		{"*", "^p", true},
		{"*", "p1", true},
		{"*", "q1", true},
		{"sand[0-9]*", "ops-a", false},
		{"sand[0-9]*", "ops-ab", false},
		{"sand[0-9]*", "ops", false},
		{"sand[0-9]*", "pay-prod", false},
		{"sand[0-9]*", "a1", false},
		{"sand[0-9]*", "b1", false},
		{"sand[0-9]*", "]1", false},
		{"sand[0-9]*", "x1", false},
		{"sand[0-9]*", "a-1", false},
		{"sand[0-9]*", "d-1", false},
		{"sand[0-9]*", "legacy", false},
		{"sand[0-9]*", "legacyx", false},
		{"sand[0-9]*", "[abc", false},
		{"sand[0-9]*", "sand7", true},
		{"sand[0-9]*", "sandx", false},
		{"sand[0-9]*", "axbyc", false},
		{"sand[0-9]*", "ab", false},
		{"sand[0-9]*", "x\\y", false},
		{"sand[0-9]*", "^p", false},
		{"sand[0-9]*", "p1", false},
		{"sand[0-9]*", "q1", false},
		{"a*b*c", "ops-a", false},
		{"a*b*c", "ops-ab", false},
		{"a*b*c", "ops", false},
		{"a*b*c", "pay-prod", false},
		{"a*b*c", "a1", false},
		{"a*b*c", "b1", false},
		{"a*b*c", "]1", false},
		{"a*b*c", "x1", false},
		{"a*b*c", "a-1", false},
		{"a*b*c", "d-1", false},
		{"a*b*c", "legacy", false},
		{"a*b*c", "legacyx", false},
		{"a*b*c", "[abc", false},
		{"a*b*c", "sand7", false},
		{"a*b*c", "sandx", false},
		{"a*b*c", "axbyc", true},
		{"a*b*c", "ab", false},
		{"a*b*c", "x\\y", false},
		{"a*b*c", "^p", false},
		{"a*b*c", "p1", false},
		{"a*b*c", "q1", false},
		{"x[\\]y", "ops-a", false},
		{"x[\\]y", "ops-ab", false},
		{"x[\\]y", "ops", false},
		{"x[\\]y", "pay-prod", false},
		{"x[\\]y", "a1", false},
		{"x[\\]y", "b1", false},
		{"x[\\]y", "]1", false},
		{"x[\\]y", "x1", false},
		{"x[\\]y", "a-1", false},
		{"x[\\]y", "d-1", false},
		{"x[\\]y", "legacy", false},
		{"x[\\]y", "legacyx", false},
		{"x[\\]y", "[abc", false},
		{"x[\\]y", "sand7", false},
		{"x[\\]y", "sandx", false},
		{"x[\\]y", "axbyc", false},
		{"x[\\]y", "ab", false},
		{"x[\\]y", "x\\y", true},
		{"x[\\]y", "^p", false},
		{"x[\\]y", "p1", false},
		{"x[\\]y", "q1", false},
		{"^p", "ops-a", false},
		{"^p", "ops-ab", false},
		{"^p", "ops", false},
		{"^p", "pay-prod", false},
		{"^p", "a1", false},
		{"^p", "b1", false},
		{"^p", "]1", false},
		{"^p", "x1", false},
		{"^p", "a-1", false},
		{"^p", "d-1", false},
		{"^p", "legacy", false},
		{"^p", "legacyx", false},
		{"^p", "[abc", false},
		{"^p", "sand7", false},
		{"^p", "sandx", false},
		{"^p", "axbyc", false},
		{"^p", "ab", false},
		{"^p", "x\\y", false},
		{"^p", "^p", true},
		{"^p", "p1", false},
		{"^p", "q1", false},
		{"[^p]*", "ops-a", false},
		{"[^p]*", "ops-ab", false},
		{"[^p]*", "ops", false},
		{"[^p]*", "pay-prod", true},
		{"[^p]*", "a1", false},
		{"[^p]*", "b1", false},
		{"[^p]*", "]1", false},
		{"[^p]*", "x1", false},
		{"[^p]*", "a-1", false},
		{"[^p]*", "d-1", false},
		{"[^p]*", "legacy", false},
		{"[^p]*", "legacyx", false},
		{"[^p]*", "[abc", false},
		{"[^p]*", "sand7", false},
		{"[^p]*", "sandx", false},
		{"[^p]*", "axbyc", false},
		{"[^p]*", "ab", false},
		{"[^p]*", "x\\y", false},
		{"[^p]*", "^p", true},
		{"[^p]*", "p1", true},
		{"[^p]*", "q1", false},
	}
	for _, tc := range cases {
		got, err := projectExcluded(tc.project, []string{tc.pattern})
		if err != nil {
			t.Fatalf("projectExcluded(%q, %q): %v", tc.project, tc.pattern, err)
		}
		if got != tc.want {
			t.Errorf("projectExcluded(%q, [%q]) = %v, want %v", tc.project, tc.pattern, got, tc.want)
		}
	}
}

func TestCheckTargetScope_NoDeclarationRefusesEveryTarget(t *testing.T) {
	err := checkTargetScope(nil, "p1", clusterTarget{Project: "p1", Location: "l", Cluster: "c"})
	if got := refusalOf(t, err); !strings.Contains(got, "can't confirm") {
		t.Fatalf("refusal = %q", got)
	}
	if _, err := loadScope(filepath.Join(t.TempDir(), "absent.json")); !errors.Is(err, errNoScopeFile) {
		t.Fatalf("loadScope(absent) = %v, want errNoScopeFile", err)
	}
}
