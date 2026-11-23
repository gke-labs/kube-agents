package controller

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

var updateProfileWorkerGolden = flag.Bool("update-profile-worker-golden", false, "rewrite testdata/profile-worker-templates.json from the current render")

// profileWorkerGoldenCases are the profiles the golden renders: the test
// profile as is, without model or turns, and with a tools image.
var profileWorkerGoldenCases = []struct {
	Name   string
	Mutate func(*agentv1alpha1.AgentProfile)
}{
	{"default", nil},
	{"no model or turns", func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.Model, p.Spec.Harness.MaxTurns = "", 0 }},
	{"tools", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Tools = []agentv1alpha1.AgentProfileTool{{Name: "kubectl", Image: "registry.example/tools-kubectl:1"}}
	}},
}

type profileWorkerGolden struct {
	Name        string
	Pool        any
	JobTemplate any
}

func renderProfileWorkerGolden(t *testing.T) []profileWorkerGolden {
	t.Helper()
	agent := a2aTestAgent()
	var out []profileWorkerGolden
	for _, c := range profileWorkerGoldenCases {
		var mutate []func(*agentv1alpha1.AgentProfile)
		if c.Mutate != nil {
			mutate = append(mutate, c.Mutate)
		}
		p := testAgentProfile(agent.Namespace, "auditor", mutate...)
		out = append(out, profileWorkerGolden{
			Name:        c.Name,
			Pool:        buildProfilePool(agent, &p, "read", agentProfileServiceAccountName(&p, "read"), true),
			JobTemplate: buildProfileJobTemplateAt(agent, &p, "read", true, ""),
		})
	}
	return out
}

// TestAProfileWithoutAHarnessTypeRendersAsBefore: a profile's warm pool and
// Job template, as rendered from main before AgentProfile had a harness
// type (testdata/profile-worker-templates.json). A profile that doesn't set
// the field must render byte for byte the same, so upgrading the operator
// rolls no warm pool. Regenerate only for an intended change, with
// -update-profile-worker-golden.
func TestAProfileWithoutAHarnessTypeRendersAsBefore(t *testing.T) {
	got, err := json.MarshalIndent(renderProfileWorkerGolden(t), "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	path := filepath.Join("testdata", "profile-worker-templates.json")
	if *updateProfileWorkerGolden {
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Errorf("the profile worker render changed for a profile with no harness type; rerun with -update-profile-worker-golden only if that is intended")
	}
}
