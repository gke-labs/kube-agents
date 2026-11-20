package controller

import (
	"context"
	"fmt"
	"slices"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// gchatMemberBackend is the one membership backend whose ids are case-blind,
// as the gateway matches them (a2a/gateway/allowlist.go requesterSubject).
const gchatMemberBackend = "gchat"

// ValidateTierMembership checks spec.tiers.membership at admission (#3659):
//   - a member's tier mustn't be unattended, the built-in or a Tier of that
//     mode: an unattended tier runs Jobs nobody steers, and a person's task
//     is a conversation;
//   - nor a compiled-in legacy tier, which no profile runs;
//   - a person is listed once, Google Chat ids compared case-blind as the
//     gateway compares them.
//
// A tier no Tier defines yet is allowed: the gateway refuses that member's
// profile tasks until one does, and never falls back to read. A Tier's mode
// can't change (the Tier CRD), so the mode read here stays true. The Tiers
// are read from the agent's namespace; with the Tier CRD absent, or a role
// that can't read them, there are none to check, and the gateway refuses
// such a member anyway.
//
// On an update (old non-nil) only the entries the edit adds or changes are
// checked, so a Tier created unattended after a member named it doesn't
// block every later edit to the PlatformAgent.
func ValidateTierMembership(ctx context.Context, c client.Reader, old, agent *agentv1alpha1.PlatformAgent, path *field.Path) (field.ErrorList, error) {
	if agent.Spec.Tiers == nil || len(agent.Spec.Tiers.Membership) == 0 {
		return nil, nil
	}
	unchanged := map[agentv1alpha1.TierMember]bool{}
	if old != nil && old.Spec.Tiers != nil {
		for _, m := range old.Spec.Tiers.Membership {
			unchanged[m] = true
		}
	}
	modes := map[string]string{agentv1alpha1.AgentProfileTierUnattended: tierModeUnattended}
	if c != nil {
		var list agentv1alpha1.TierList
		if err := c.List(ctx, &list, client.InNamespace(agent.Namespace)); err != nil && !isCRDNotInstalledError(err) && !apierrors.IsForbidden(err) {
			return nil, fmt.Errorf("list Tiers to check spec.tiers.membership: %w", err)
		}
		for _, t := range list.Items {
			if t.DeletionTimestamp == nil && tierSkipReason(t.Name, t.Spec.Mode) == "" {
				modes[t.Name] = t.Spec.Mode
			}
		}
	}
	var errs field.ErrorList
	// Every unchanged entry's person is seen first, so an added entry naming
	// one of them is a duplicate wherever it sits in the list, above the
	// existing entry as well as below it.
	seen := map[string]bool{}
	for _, m := range agent.Spec.Tiers.Membership {
		if unchanged[m] {
			seen[tierMemberKey(m)] = true
		}
	}
	for i, m := range agent.Spec.Tiers.Membership {
		if unchanged[m] {
			continue
		}
		p := path.Index(i)
		key := tierMemberKey(m)
		if seen[key] {
			errs = append(errs, field.Duplicate(p, m.Backend+":"+m.User))
		}
		seen[key] = true
		switch {
		case slices.Contains(legacyTierNames, m.Tier):
			errs = append(errs, field.Invalid(p.Child("tier"), m.Tier,
				"a compiled-in tier no profile runs; name read or an interactive Tier"))
		case modes[m.Tier] == tierModeUnattended:
			errs = append(errs, field.Invalid(p.Child("tier"), m.Tier,
				"an unattended-mode tier, which runs Jobs nobody steers; a person's tier must be interactive"))
		}
	}
	return errs, nil
}

// tierMemberKey is the person a member names, as the gateway matches turns:
// a Google Chat email without case.
func tierMemberKey(m agentv1alpha1.TierMember) string {
	key := m.Backend + ":" + strings.TrimSpace(m.User)
	if m.Backend == gchatMemberBackend {
		key = strings.ToLower(key)
	}
	return key
}
