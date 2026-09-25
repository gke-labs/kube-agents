package controller

import (
	"encoding/json"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func scopeTestAgent(scope *agentv1alpha1.ScopeSpec) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "pa", Namespace: "ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{},
			Scope:   scope,
		},
	}
}

func TestRenderScopeJSONRendersAnEmptyDeclarationForNoScope(t *testing.T) {
	// The file is rendered on every install so the reconcile can tell "nothing
	// declared" from "no render reached this pod" (a rollback). A CR with no scope
	// block renders the empty declaration with present=false; the ways of declaring
	// an empty block render the same bytes as each other, with present=true, so an
	// operator who empties the lists is telling the reconcile to drop the projects
	// and an operator (or an older webhook) that removes the block is not.
	absent := renderScopeJSON(scopeTestAgent(nil))
	if !strings.Contains(absent, `"present": false`) || !strings.Contains(absent, `"projects": []`) || !strings.Contains(absent, `"clusters": []`) {
		t.Fatalf("no-scope render is not an absent empty declaration: %q", absent)
	}
	if got, ok := buildConfigMapData(scopeTestAgent(nil), nil)[scopeConfigKey]; !ok || got != absent {
		t.Fatalf("ConfigMap must always carry %s, with the absent declaration when the CR has no scope", scopeConfigKey)
	}
	want := renderScopeJSON(scopeTestAgent(&agentv1alpha1.ScopeSpec{}))
	if !strings.Contains(want, `"present": true`) {
		t.Fatalf("an empty scope block must render present=true: %q", want)
	}
	for name, scope := range map[string]*agentv1alpha1.ScopeSpec{
		"empty lists":  {Projects: []string{}, Exclude: &agentv1alpha1.ScopeExcludeSpec{}},
		"empty nested": {Exclude: &agentv1alpha1.ScopeExcludeSpec{Projects: []string{}, Clusters: []agentv1alpha1.ScopeClusterRef{}}},
	} {
		t.Run(name, func(t *testing.T) {
			if got := renderScopeJSON(scopeTestAgent(scope)); got != want {
				t.Fatalf("%s renders differently from an empty block:\n%s\nvs\n%s", name, got, want)
			}
			if got, ok := buildConfigMapData(scopeTestAgent(scope), nil)[scopeConfigKey]; !ok || got != want {
				t.Fatalf("%s: ConfigMap must always carry %s with the empty declaration", name, scopeConfigKey)
			}
		})
	}
}

func TestRenderScopeJSONIsSortedAndDeterministic(t *testing.T) {
	scope := &agentv1alpha1.ScopeSpec{
		Projects: []string{"zeta-prod", "alpha-prod"},
		Exclude: &agentv1alpha1.ScopeExcludeSpec{
			Projects: []string{"*-sandbox", "alpha-scratch"},
			Clusters: []agentv1alpha1.ScopeClusterRef{
				{ProjectID: "zeta-prod", Location: "us-central1", ClusterName: "b"},
				{ProjectID: "alpha-prod", Location: "us-east1", ClusterName: "z"},
				{ProjectID: "alpha-prod", Location: "us-central1", ClusterName: "a"},
			},
		},
	}
	first := renderScopeJSON(scopeTestAgent(scope))
	// The same declaration in a different order renders the same bytes: the
	// ConfigMap hash rolls the pod, so declaration order must not.
	reordered := scope.DeepCopy()
	reordered.Projects = []string{"alpha-prod", "zeta-prod"}
	reordered.Exclude.Clusters[0], reordered.Exclude.Clusters[2] = reordered.Exclude.Clusters[2], reordered.Exclude.Clusters[0]
	if second := renderScopeJSON(scopeTestAgent(reordered)); second != first {
		t.Fatalf("render depends on declaration order:\n%s\nvs\n%s", first, second)
	}

	var decl struct {
		Projects []string `json:"projects"`
		Exclude  struct {
			Projects []string `json:"projects"`
			Clusters []struct {
				ProjectID   string `json:"projectId"`
				Location    string `json:"location"`
				ClusterName string `json:"clusterName"`
			} `json:"clusters"`
		} `json:"exclude"`
	}
	if err := json.Unmarshal([]byte(first), &decl); err != nil {
		t.Fatalf("render is not JSON: %v\n%s", err, first)
	}
	if got := strings.Join(decl.Projects, ","); got != "alpha-prod,zeta-prod" {
		t.Errorf("projects not sorted: %s", got)
	}
	if got := strings.Join(decl.Exclude.Projects, ","); got != "*-sandbox,alpha-scratch" {
		t.Errorf("exclude.projects not sorted: %s", got)
	}
	if len(decl.Exclude.Clusters) != 3 || decl.Exclude.Clusters[0].ClusterName != "a" ||
		decl.Exclude.Clusters[1].ClusterName != "z" || decl.Exclude.Clusters[2].ClusterName != "b" {
		t.Errorf("exclude.clusters not sorted by (project, location, name): %+v", decl.Exclude.Clusters)
	}
	if !strings.HasSuffix(first, "\n") {
		t.Errorf("render should end with a newline for a clean file")
	}
}

func TestRenderScopeJSONRendersContainersSorted(t *testing.T) {
	// Folders and organisations are rendered under their own keys, sorted, and an
	// absent list renders as [] so the reader never sees null.
	scope := &agentv1alpha1.ScopeSpec{Folders: []string{"987654321098", "123456789012"}, Organizations: []string{"926317919369"}}
	got := renderScopeJSON(scopeTestAgent(scope))
	for _, want := range []string{
		"\"folders\": [\n    \"123456789012\",\n    \"987654321098\"\n  ]",
		"\"organizations\": [\n    \"926317919369\"\n  ]",
		"\"projects\": []",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("render lacks %q:\n%s", want, got)
		}
	}
	if none := renderScopeJSON(scopeTestAgent(&agentv1alpha1.ScopeSpec{})); !strings.Contains(none, "\"folders\": []") || !strings.Contains(none, "\"organizations\": []") {
		t.Errorf("an empty block must render empty container lists, got:\n%s", none)
	}
}

func TestRenderScopeJSONExcludeOnlyIsRendered(t *testing.T) {
	// An install migrating only its exclusions still needs them applied.
	scope := &agentv1alpha1.ScopeSpec{Exclude: &agentv1alpha1.ScopeExcludeSpec{
		Clusters: []agentv1alpha1.ScopeClusterRef{{ProjectID: "p", Location: "us-central1", ClusterName: "scratch"}},
	}}
	data := buildConfigMapData(scopeTestAgent(scope), nil)
	if _, ok := data[scopeConfigKey]; !ok {
		t.Fatalf("exclude-only scope must render %s", scopeConfigKey)
	}
	if !strings.Contains(data[scopeConfigKey], `"projects": []`) {
		t.Errorf("empty lists render as [] so the reader never sees null: %s", data[scopeConfigKey])
	}
}

func TestScopeFileReachesTheAgentWhetherOrNotScopeIsDeclared(t *testing.T) {
	agent := scopeTestAgent(nil)

	var vol *corev1.Volume
	for i := range buildDefaultVolumes(agent) {
		if v := &buildDefaultVolumes(agent)[i]; v.Name == scopeVolumeName {
			vol = v
		}
	}
	if vol == nil {
		t.Fatalf("no %s volume", scopeVolumeName)
	}
	cm := vol.VolumeSource.ConfigMap
	if cm == nil || cm.Name != agent.Name+"-config" {
		t.Fatalf("scope volume must project the config ConfigMap: %+v", vol.VolumeSource)
	}
	if cm.Optional == nil || !*cm.Optional {
		t.Errorf("scope volume must be optional: a ConfigMap from an older operator has no %s key", scopeConfigKey)
	}
	if len(cm.Items) != 1 || cm.Items[0].Key != scopeConfigKey || cm.Items[0].Path != scopeFileName {
		t.Errorf("scope volume must project exactly %s as %s: %+v", scopeConfigKey, scopeFileName, cm.Items)
	}

	mounted := false
	for _, m := range buildDefaultVolumeMounts("/opt/data") {
		if m.Name == scopeVolumeName {
			mounted = true
			if m.MountPath != scopeDir || !m.ReadOnly {
				t.Errorf("scope mount must be read-only at %s: %+v", scopeDir, m)
			}
		}
	}
	if !mounted {
		t.Errorf("agent container does not mount %s", scopeVolumeName)
	}
}
