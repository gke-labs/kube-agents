package controller

import (
	"context"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func withMembers(agent *agentv1alpha1.PlatformAgent, members ...agentv1alpha1.TierMember) *agentv1alpha1.PlatformAgent {
	agent.Spec.Tiers = &agentv1alpha1.PlatformAgentTiersSpec{Membership: members}
	return agent
}

// spec.tiers.membership reaches the gateway as one backend:user=tier list,
// and an install without it renders no variable, as before (#3659).
func TestGatewayRendersTheTierMembership(t *testing.T) {
	envOf := func(agent *agentv1alpha1.PlatformAgent) map[string]string {
		out := map[string]string{}
		for _, e := range buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env {
			out[e.Name] = e.Value
		}
		return out
	}
	if _, ok := envOf(a2aTestAgent())[a2aTierMembershipEnvVar]; ok {
		t.Fatal("membership rendered with no spec.tiers")
	}
	if _, ok := envOf(withMembers(a2aTestAgent()))[a2aTierMembershipEnvVar]; ok {
		t.Fatal("membership rendered for an empty list")
	}
	agent := withMembers(a2aTestAgent(),
		agentv1alpha1.TierMember{Backend: "gchat", User: "users/1", Tier: "oncall"},
		agentv1alpha1.TierMember{Backend: "slack", User: "U0ABC", Tier: "read"})
	if got, want := envOf(agent)[a2aTierMembershipEnvVar], "gchat:users/1=oncall,slack:U0ABC=read"; got != want {
		t.Fatalf("%s = %q, want %q", a2aTierMembershipEnvVar, got, want)
	}
}

// Admission refuses a person mapped to an unattended tier (built-in or a
// Tier), to a compiled-in legacy tier, or listed twice (Google Chat ids
// case-blind). A tier nothing defines yet, read, and an interactive Tier
// pass.
func TestTierMembershipAdmission(t *testing.T) {
	ns := a2aTestAgent().Namespace
	tiers := []*agentv1alpha1.Tier{
		{ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: ns}, Spec: agentv1alpha1.TierSpec{Mode: tierModeInteractive}},
		{ObjectMeta: metav1.ObjectMeta{Name: "nightly", Namespace: ns}, Spec: agentv1alpha1.TierSpec{Mode: tierModeUnattended}},
		{ObjectMeta: metav1.ObjectMeta{Name: "nightly-elsewhere", Namespace: "other"}, Spec: agentv1alpha1.TierSpec{Mode: tierModeUnattended}},
	}
	b := fake.NewClientBuilder().WithScheme(setupScheme())
	for _, tier := range tiers {
		b = b.WithObjects(tier)
	}
	c := b.Build()
	m := func(backend, user, tier string) agentv1alpha1.TierMember {
		return agentv1alpha1.TierMember{Backend: backend, User: user, Tier: tier}
	}
	for _, tc := range []struct {
		name    string
		members []agentv1alpha1.TierMember
		want    string // a fragment of the one error; "" for none
	}{
		{"read, an interactive Tier, and a tier nothing defines yet", []agentv1alpha1.TierMember{m("gchat", "a", "read"), m("slack", "U1", "oncall"), m("discord", "1", "ghost")}, ""},
		{"an unattended Tier", []agentv1alpha1.TierMember{m("slack", "U1", "nightly")}, "unattended-mode tier"},
		{"the built-in unattended tier", []agentv1alpha1.TierMember{m("slack", "U1", "unattended")}, "unattended-mode tier"},
		{"an unattended Tier in another namespace isn't this install's", []agentv1alpha1.TierMember{m("slack", "U1", "nightly-elsewhere")}, ""},
		{"a legacy tier", []agentv1alpha1.TierMember{m("gchat", "a", "developer-team")}, "compiled-in tier"},
		{"a Google Chat person twice, in two cases", []agentv1alpha1.TierMember{m("gchat", "Alice@x.com", "read"), m("gchat", "alice@x.com", "oncall")}, "Duplicate value"},
		{"one Slack id in two cases is two people", []agentv1alpha1.TierMember{m("slack", "U1", "read"), m("slack", "u1", "oncall")}, ""},
		{"one id on two backends is two people", []agentv1alpha1.TierMember{m("slack", "1", "read"), m("discord", "1", "oncall")}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			errs, err := ValidateTierMembership(context.Background(), c, withMembers(a2aTestAgent(), tc.members...), field.NewPath("spec", "tiers", "membership"))
			if err != nil {
				t.Fatal(err)
			}
			switch {
			case tc.want == "" && len(errs) != 0:
				t.Fatalf("refused: %v", errs)
			case tc.want != "" && (len(errs) != 1 || !strings.Contains(errs.ToAggregate().Error(), tc.want)):
				t.Fatalf("errs = %v, want one containing %q", errs, tc.want)
			}
		})
	}
}
