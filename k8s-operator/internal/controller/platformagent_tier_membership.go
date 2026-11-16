package controller

import (
	"context"
	"fmt"
	"slices"
	"strings"

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
// are read from the agent's namespace; with the Tier CRD absent there are
// none to check.
func ValidateTierMembership(ctx context.Context, c client.Reader, agent *agentv1alpha1.PlatformAgent, path *field.Path) (field.ErrorList, error) {
	if agent.Spec.Tiers == nil || len(agent.Spec.Tiers.Membership) == 0 {
		return nil, nil
	}
	modes := map[string]string{agentv1alpha1.AgentProfileTierUnattended: tierModeUnattended}
	if c != nil {
		var list agentv1alpha1.TierList
		if err := c.List(ctx, &list, client.InNamespace(agent.Namespace)); err != nil && !isCRDNotInstalledError(err) {
			return nil, fmt.Errorf("list Tiers to check spec.tiers.membership: %w", err)
		}
		for _, t := range list.Items {
			if t.DeletionTimestamp == nil && tierSkipReason(t.Name, t.Spec.Mode) == "" {
				modes[t.Name] = t.Spec.Mode
			}
		}
	}
	var errs field.ErrorList
	seen := map[string]bool{}
	for i, m := range agent.Spec.Tiers.Membership {
		p := path.Index(i)
		switch {
		case slices.Contains(legacyTierNames, m.Tier):
			errs = append(errs, field.Invalid(p.Child("tier"), m.Tier,
				"a compiled-in tier no profile runs; name read or an interactive Tier"))
		case modes[m.Tier] == tierModeUnattended:
			errs = append(errs, field.Invalid(p.Child("tier"), m.Tier,
				"an unattended-mode tier, which runs Jobs nobody steers; a person's tier must be interactive"))
		}
		key := m.Backend + ":" + strings.TrimSpace(m.User)
		if m.Backend == gchatMemberBackend {
			key = strings.ToLower(key)
		}
		if seen[key] {
			errs = append(errs, field.Duplicate(p, m.Backend+":"+m.User))
		}
		seen[key] = true
	}
	return errs, nil
}
