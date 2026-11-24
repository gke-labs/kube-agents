package controller

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestAProfilesHarnessTypeReachesItsWorkersOnlyWhenSet: spec.harness.type
// goes to the warm pool and the Job template as A2A_HARNESS when the profile
// sets it, with A2A_HARNESS_UNISOLATED rendered empty beside it, and a profile that doesn't gets no A2A_HARNESS at all (the worker
// reads unset as Claude Code), so its pods are the ones it had before.
func TestAProfilesHarnessTypeReachesItsWorkersOnlyWhenSet(t *testing.T) {
	agent := a2aTestAgent()
	for _, c := range []struct {
		name    string
		harness string
		want    string
		set     bool
	}{
		{"absent", "", "", false},
		{"claude-code", agentv1alpha1.AgentProfileHarnessClaudeCode, "claude-code", true},
	} {
		t.Run(c.name, func(t *testing.T) {
			p := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.Type = c.harness })
			pool := buildProfilePool(agent, &p, "read", agentProfileServiceAccountName(&p, "read"), true)
			job := buildProfileJobTemplateAt(agent, &p, "read", true, "")
			for kind, spec := range map[string]corev1.PodSpec{"pool": pool.Spec.Template.Spec, "job": job.Spec} {
				var got string
				var set, unisolatedSet bool
				var unisolated string
				for _, e := range spec.Containers[0].Env {
					switch e.Name {
					case "A2A_HARNESS":
						got, set = e.Value, true
					case "A2A_HARNESS_UNISOLATED":
						unisolated, unisolatedSet = e.Value, true
					}
				}
				if set != c.set || got != c.want {
					t.Errorf("%s: A2A_HARNESS set=%v %q, want set=%v %q", kind, set, got, c.set, c.want)
				}
				// With a type, the isolation waiver is rendered empty so the
				// image's ENV can't set it; without one, nothing is added.
				if unisolatedSet != c.set || unisolated != "" {
					t.Errorf("%s: A2A_HARNESS_UNISOLATED set=%v %q, want set=%v and empty", kind, unisolatedSet, unisolated, c.set)
				}
			}
		})
	}
}

// TestAProfileWhoseHarnessTheOperatorDoesntRenderIsRefused: a type the
// CRD's enum would refuse, admitted by an older, newer or edited CRD, is
// refused whole with HarnessNotSupported, never rendered as Claude Code.
// Absent and claude-code render.
func TestAProfileWhoseHarnessTheOperatorDoesntRenderIsRefused(t *testing.T) {
	agent := a2aTestAgent()
	for _, c := range []struct {
		harness string
		refused bool
	}{
		{"", false},
		{"claude-code", false},
		{"antigravity", true},
		{"Claude-Code", true},
		{" claude-code", true},
	} {
		p := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) { p.Spec.Harness.Type = c.harness })
		r := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{p})["auditor"]
		if (r.refused != nil) != c.refused {
			t.Errorf("type %q: refused %v, want refused=%v", c.harness, r.refused, c.refused)
			continue
		}
		if c.refused && (r.reason != reasonAgentProfileHarnessNotSupported || !strings.Contains(r.refused.Error(), "spec.harness.type")) {
			t.Errorf("type %q: reason %q, %v; want %s naming spec.harness.type", c.harness, r.reason, r.refused, reasonAgentProfileHarnessNotSupported)
		}
	}
}

// TestTheHarnessTypeIsNotOnTheCard: the type is pod configuration only. It
// is not on the profile's card, and the profile's bus identities and their
// permissions are the same with it, so routing and permissions can't come
// to depend on it.
func TestTheHarnessTypeIsNotOnTheCard(t *testing.T) {
	agent := a2aTestAgent()
	plain := testAgentProfile(agent.Namespace, "auditor")
	typed := testAgentProfile(agent.Namespace, "auditor", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Harness.Type = agentv1alpha1.AgentProfileHarnessClaudeCode
	})
	a, err := json.Marshal(desiredAgentCard(&plain))
	if err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(desiredAgentCard(&typed))
	if err != nil {
		t.Fatal(err)
	}
	if string(a) != string(b) {
		t.Errorf("the card changed with the harness type:\n%s\n%s", a, b)
	}
	// Nor in the profile's bus identities and their permissions.
	pr := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{plain})["auditor"]
	tr := resolveAgentProfileIdentities(agent, []agentv1alpha1.AgentProfile{typed})["auditor"]
	if !reflect.DeepEqual(pr, tr) {
		t.Errorf("the bus identities changed with the harness type:\n%+v\n%+v", pr, tr)
	}
}
