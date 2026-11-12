package controller

import (
	"fmt"
	"strings"
	"testing"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// withDispatchBudget shrinks the dispatch config's byte budget for one test.
func withDispatchBudget(t *testing.T, bytes int) {
	t.Helper()
	old := dispatchConfigBudgetBytes
	dispatchConfigBudgetBytes = bytes
	t.Cleanup(func() { dispatchConfigBudgetBytes = old })
}

func budgetProfiles(agent *agentv1alpha1.PlatformAgent, n int) []agentv1alpha1.AgentProfile {
	base := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	out := make([]agentv1alpha1.AgentProfile, 0, n)
	for i := range n {
		out = append(out, testAgentProfile(agent.Namespace, fmt.Sprintf("p%03d", i), func(p *agentv1alpha1.AgentProfile) {
			p.CreationTimestamp = metav1.NewTime(base.Add(time.Duration(i) * time.Minute))
			p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
		}))
	}
	return out
}

// Past the byte budget the newest profiles are refused, each with a reason
// its IdentityReady condition carries, instead of the API server refusing the
// whole ConfigMap (#5479). The oldest keep dispatching.
func TestProfilesPastTheDispatchBudgetAreRefusedNewestFirst(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	profiles := budgetProfiles(agent, 40)
	// Room for the envelope and about ten entries.
	full, err := buildA2ADispatchConfigMap(agent, profiles[:10], nil)
	if err != nil {
		t.Fatal(err)
	}
	withDispatchBudget(t, len(full.Data[a2aDispatchConfigKey])+50)

	resolved := resolveAgentProfileIdentities(agent, profiles)
	var kept, refused []string
	for _, p := range profiles {
		r := resolved[p.Name]
		switch {
		case r.refused == nil:
			kept = append(kept, p.Name)
		case r.reason == reasonAgentProfileDispatchFull:
			refused = append(refused, p.Name)
			if !strings.Contains(r.refused.Error(), "dispatch config") {
				t.Errorf("%s: refusal %q doesn't name the dispatch config", p.Name, r.refused)
			}
		default:
			t.Errorf("%s refused for another reason: %v (%s)", p.Name, r.refused, r.reason)
		}
	}
	if len(kept) < 9 || len(kept) > 10 || len(refused) != len(profiles)-len(kept) {
		t.Fatalf("kept %d, refused %d; want the oldest ~10 kept and the rest refused", len(kept), len(refused))
	}
	for i, name := range kept {
		if name != profiles[i].Name {
			t.Fatalf("kept %v; want the oldest, in order", kept)
		}
	}
	cm, err := buildA2ADispatchConfigMap(agent, profiles, nil)
	if err != nil {
		t.Fatal(err)
	}
	if size := len(cm.Data[a2aDispatchConfigKey]); size > dispatchConfigBudgetBytes {
		t.Errorf("the config is %d bytes, over the %d-byte budget", size, dispatchConfigBudgetBytes)
	}
}

// Under the budget nothing is refused, and with no dispatcher the budget
// doesn't apply (there is no dispatch config to fill).
func TestTheDispatchBudgetRefusesNothingItDoesNotHaveTo(t *testing.T) {
	agent := a2aTestAgent()
	profiles := budgetProfiles(agent, 40)
	withDispatchBudget(t, 1000)
	for name, r := range resolveAgentProfileIdentities(agent, profiles) {
		if r.reason == reasonAgentProfileDispatchFull {
			t.Errorf("%s refused for the dispatch budget with no dispatcher", name)
		}
	}
	withDispatcher(t)
	withDispatchBudget(t, 900<<10)
	for name, r := range resolveAgentProfileIdentities(agent, profiles) {
		if r.refused != nil {
			t.Errorf("%s refused under a roomy budget: %v", name, r.refused)
		}
	}
}

// With the Job policy on, the estimate counts each unattended profile's
// template reference and the Job labels, so the config written never runs
// past the budget the estimate admitted against.
func TestTheDispatchBudgetNeverRunsShortWithJobMode(t *testing.T) {
	withDispatcher(t)
	t.Setenv(a2aDispatcherJobPolicyEnvVar, "true")
	agent := a2aTestAgent()
	profiles := budgetProfiles(agent, 40)
	tenTemplates := dispatchJobTemplates(agent, profiles[:10], false)
	ten, err := buildA2ADispatchConfigMapWith(agent, profiles[:10], tenTemplates, nil)
	if err != nil {
		t.Fatal(err)
	}
	withDispatchBudget(t, len(ten.Data[a2aDispatchConfigKey])+50)
	all := dispatchJobTemplates(agent, profiles, false)
	cm, err := buildA2ADispatchConfigMapWith(agent, profiles, all, nil)
	if err != nil {
		t.Fatal(err)
	}
	if size := len(cm.Data[a2aDispatchConfigKey]); size > dispatchConfigBudgetBytes {
		t.Fatalf("the config is %d bytes, over the %d-byte budget it was admitted against", size, dispatchConfigBudgetBytes)
	}
	kept := 0
	for _, r := range resolveAgentProfileIdentities(agent, profiles) {
		if r.refused == nil {
			kept++
		}
	}
	if kept < 9 || kept > 10 {
		t.Errorf("kept %d; want the ~10 that fit", kept)
	}
}
