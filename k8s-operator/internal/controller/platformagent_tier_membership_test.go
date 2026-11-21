package controller

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

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
		{"an Entra person twice, in two cases", []agentv1alpha1.TierMember{m("a2a-entra", "1A2B3C4D-5E6F-4A7B-8C9D-0E1F2A3B4C5D", "read"), m("a2a-entra", "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d", "oncall")}, "Duplicate value"},
		{"one Slack id in two cases is two people", []agentv1alpha1.TierMember{m("slack", "U1", "read"), m("slack", "u1", "oncall")}, ""},
		{"one id on two backends is two people", []agentv1alpha1.TierMember{m("slack", "1", "read"), m("discord", "1", "oncall")}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			errs, err := ValidateTierMembership(context.Background(), c, nil, withMembers(a2aTestAgent(), tc.members...), field.NewPath("spec", "tiers", "membership"))
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

// On an update only what the edit adds or changes is checked: a member whose
// Tier became unattended after it was listed doesn't block an unrelated
// edit, but listing someone new at it does, and so does a new repeat.
func TestTierMembershipAdmissionOnUpdateChecksTheEdit(t *testing.T) {
	ns := a2aTestAgent().Namespace
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(
		&agentv1alpha1.Tier{ObjectMeta: metav1.ObjectMeta{Name: "nightly", Namespace: ns}, Spec: agentv1alpha1.TierSpec{Mode: tierModeUnattended}}).Build()
	stale := agentv1alpha1.TierMember{Backend: "slack", User: "U1", Tier: "nightly"}
	alice := agentv1alpha1.TierMember{Backend: "gchat", User: "alice@x.com", Tier: "read"}
	old := withMembers(a2aTestAgent(), stale, alice)
	path := field.NewPath("spec", "tiers", "membership")
	for _, tc := range []struct {
		name    string
		members []agentv1alpha1.TierMember
		refused bool
	}{
		{"the stale entry left as it was", []agentv1alpha1.TierMember{stale, {Backend: "slack", User: "U2", Tier: "read"}}, false},
		{"someone new at the unattended tier", []agentv1alpha1.TierMember{stale, {Backend: "slack", User: "U2", Tier: "nightly"}}, true},
		{"a new repeat of a listed person", []agentv1alpha1.TierMember{stale, {Backend: "slack", User: "U1", Tier: "read"}}, true},
		// The walk sees the unchanged entries first, so a repeat added above
		// the person it repeats is caught too (#6082 review).
		{"a new repeat above a listed person", []agentv1alpha1.TierMember{{Backend: "slack", User: "U1", Tier: "read"}, stale}, true},
		{"a Google Chat repeat in another case, above", []agentv1alpha1.TierMember{stale, {Backend: "gchat", User: "Alice@x.com", Tier: "oncall"}, alice}, true},
		{"the listed people left as they were", []agentv1alpha1.TierMember{alice, stale}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			errs, err := ValidateTierMembership(context.Background(), c, old, withMembers(a2aTestAgent(), tc.members...), path)
			if err != nil {
				t.Fatal(err)
			}
			if (len(errs) > 0) != tc.refused {
				t.Fatalf("errs = %v, refused want %v", errs, tc.refused)
			}
		})
	}
}

// A person listed twice, which a cluster without the webhook admits, renders
// once, so the gateway, which refuses a repeat at boot, still starts.
func TestTheMembershipRenderDropsARepeat(t *testing.T) {
	agent := withMembers(a2aTestAgent(),
		agentv1alpha1.TierMember{Backend: "gchat", User: "Alice@x.com", Tier: "oncall"},
		agentv1alpha1.TierMember{Backend: "gchat", User: "alice@x.com", Tier: "read"})
	env := a2aTierMembershipEnv(agent)
	if len(env) != 1 || env[0].Value != "gchat:Alice@x.com=oncall" {
		t.Fatalf("env = %+v", env)
	}
}

// The largest list the schema admits (MaxItems 200, user 128, tier 63, the
// longest backend) renders well under the kernel's limit on one environment
// string (MAX_ARG_STRLEN, 128 KiB): past it the gateway can't start.
func TestTheLargestMembershipFitsOneEnvString(t *testing.T) {
	const maxArgStrlen = 128 * 1024
	members := make([]agentv1alpha1.TierMember, 200)
	for i := range members {
		members[i] = agentv1alpha1.TierMember{Backend: "a2a-google", User: fmt.Sprintf("%03d", i) + strings.Repeat("u", 125), Tier: strings.Repeat("t", 63)}
	}
	env := a2aTierMembershipEnv(withMembers(a2aTestAgent(), members...))
	if n := len(env[0].Value); n >= maxArgStrlen/2 {
		t.Errorf("the largest admitted list renders %d bytes; want well under %d", n, maxArgStrlen)
	}
}

// A role that can't read Tiers checks what it can (repeats, legacy names)
// and lets the rest through: the gateway refuses an unattended member anyway.
func TestTierMembershipAdmissionWithoutTierAccess(t *testing.T) {
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithInterceptorFuncs(interceptor.Funcs{
		List: func(context.Context, client.WithWatch, client.ObjectList, ...client.ListOption) error {
			return apierrors.NewForbidden(schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "tiers"}, "", errors.New("no"))
		},
	}).Build()
	path := field.NewPath("spec", "tiers", "membership")
	errs, err := ValidateTierMembership(context.Background(), c, nil, withMembers(a2aTestAgent(), agentv1alpha1.TierMember{Backend: "slack", User: "U1", Tier: "oncall"}), path)
	if err != nil || len(errs) != 0 {
		t.Fatalf("errs = %v, err = %v", errs, err)
	}
	errs, _ = ValidateTierMembership(context.Background(), c, nil, withMembers(a2aTestAgent(), agentv1alpha1.TierMember{Backend: "slack", User: "U1", Tier: "developer-team"}), path)
	if len(errs) != 1 {
		t.Fatalf("a legacy tier passed without Tier access: %v", errs)
	}
}
