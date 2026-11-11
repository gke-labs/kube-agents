package controller

import (
	"context"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// Which of a profile's allowedTiers the operator renders (#3659, Tier PR 2b-i).
//
// allowedTiers takes any short tier name; the CRD can't tell whether one is a
// tier. The operator decides, once per reconcile, from a catalog of the
// built-in tiers and the Tier resources in the install's namespace, applying
// the same skip rules the components apply when they load the tiers file
// (capability.LoadTiers), so the operator never renders a tier they refuse:
//   - a listed name with no tier is unknown (TierUnknown) and renders nothing;
//   - a profile may serve at most one unattended-mode tier, because Job mode
//     keeps one Job template per profile. With two listed, the incumbent (the
//     one already rendered) keeps serving and the newcomer is refused
//     (TierModeConflict); with none rendered before, neither renders.
//
// Every renderer downstream sees a copy of the profile whose allowedTiers is
// narrowed to its renderable tiers, carrying their modes, so identities,
// durables, pools, Job templates and the dispatch config follow without each
// learning the rules. A profile with no renderable tier is left out, which is
// how a removed profile is treated. A dropped tier is removed on the next pass,
// as read and unattended always were; draining it instead is 2b-ii's.

// The tier modes, as the Tier CRD and the capability library spell them.
const (
	tierModeInteractive = agentv1alpha1.TierModeInteractive
	tierModeUnattended  = agentv1alpha1.TierModeUnattended
)

// tierCatalog maps each usable tier to its mode.
type tierCatalog map[string]string

// legacyTierNames are capability's compiled-in non-profile tiers, which no
// Tier resource may redefine.
var legacyTierNames = []string{"platform", "cluster-admin", "developer-team"}

// tierNameRE is a DNS-1123 label, the capability library's rule for a tier
// name (validProfileName).
var tierNameRE = regexp.MustCompile(`^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$`)

func builtinTierCatalog() tierCatalog {
	return tierCatalog{
		agentv1alpha1.AgentProfileTierRead:       tierModeInteractive,
		agentv1alpha1.AgentProfileTierUnattended: tierModeUnattended,
	}
}

// tierSkipReason is why the components would skip a Tier resource when they
// load it ("" when they'd load it), the rules capability.LoadTiers applies.
func tierSkipReason(name, mode string) string {
	builtin := builtinTierCatalog()
	switch {
	case !tierNameRE.MatchString(name):
		return "the name is not a DNS label"
	case slices.Contains(legacyTierNames, name):
		return "it names a compiled-in legacy tier, which can't be redefined"
	case builtin[name] != "" && builtin[name] != mode:
		return fmt.Sprintf("it names the built-in tier %s, whose mode %s is compiled in", name, builtin[name])
	case mode != tierModeInteractive && mode != tierModeUnattended:
		return fmt.Sprintf("mode %q is not interactive or unattended", mode)
	}
	return ""
}

// loadTierCatalog is the built-in tiers plus the Tier resources in ns that the
// components would load. A role that can't read Tiers (unreadable) gets the
// built-ins alone, which fails closed for every custom tier. A list error is
// returned, so the reconcile retries instead of reading a passing failure as
// "no tiers" and removing what they render.
func loadTierCatalog(ctx context.Context, c client.Reader, ns string, unreadable bool) (tierCatalog, error) {
	cat := builtinTierCatalog()
	if unreadable {
		return cat, nil
	}
	var list agentv1alpha1.TierList
	if err := c.List(ctx, &list, client.InNamespace(ns)); err != nil {
		if isCRDNotInstalledError(err) {
			return cat, nil
		}
		return nil, fmt.Errorf("failed to list Tiers: %w", err)
	}
	for _, t := range list.Items {
		if t.DeletionTimestamp != nil || tierSkipReason(t.Name, t.Spec.Mode) != "" {
			continue
		}
		cat[t.Name] = t.Spec.Mode
	}
	return cat, nil
}

// tierVerdict is the operator's decision about one profile's tiers.
type tierVerdict struct {
	// renderable are the tiers rendered, in allowedTiers order.
	renderable []string
	// unknown are listed names with no usable tier; conflict are
	// unattended-mode tiers refused beside the one the profile serves.
	unknown, conflict []string
	// modes is each renderable tier's mode.
	modes map[string]string
}

// decideTiers decides p's tiers from cat, with rendered the tiers it was last
// rendered for (renderedTiersOf). rendered only picks the incumbent among the
// listed unattended-mode tiers: a tier in rendered that p doesn't list, or that
// cat doesn't hold, is never rendered.
func decideTiers(p *agentv1alpha1.AgentProfile, cat tierCatalog, rendered []string) tierVerdict {
	v := tierVerdict{modes: map[string]string{}}
	var known, unattended []string
	for _, tier := range p.AllowedTierList() {
		mode, ok := cat[tier]
		if !ok {
			v.unknown = append(v.unknown, tier)
			continue
		}
		known = append(known, tier)
		v.modes[tier] = mode
		if mode == tierModeUnattended {
			unattended = append(unattended, tier)
		}
	}
	if len(unattended) > 1 {
		var incumbent []string
		for _, tier := range unattended {
			if slices.Contains(rendered, tier) {
				incumbent = append(incumbent, tier)
			}
		}
		for _, tier := range unattended {
			if len(incumbent) != 1 || tier != incumbent[0] {
				v.conflict = append(v.conflict, tier)
			}
		}
	}
	for _, tier := range known {
		if !slices.Contains(v.conflict, tier) {
			v.renderable = append(v.renderable, tier)
		} else {
			delete(v.modes, tier)
		}
	}
	return v
}

// renderedTiersOf is the tiers p was last rendered for: its status's
// renderedTiers, or, when that is absent (a restore or a GitOps apply dropped
// status), derived once from the profile's own operator-created
// ServiceAccounts, so "status empty" is never read as "nothing rendered".
// Only ServiceAccounts carrying this profile's label and UID count, so a
// forged one confers nothing.
func renderedTiersOf(ctx context.Context, c client.Reader, p *agentv1alpha1.AgentProfile) ([]string, error) {
	if len(p.Status.RenderedTiers) > 0 {
		return p.Status.RenderedTiers, nil
	}
	if p.Spec.Identity.ServiceAccountName != "" {
		// A named ServiceAccount serves one tier: the one allowedTiers lists.
		return nil, nil
	}
	var sas corev1.ServiceAccountList
	if err := c.List(ctx, &sas, client.InNamespace(profileNamespaceOf(p)), client.MatchingLabels{agentProfileNameLabel: p.Name}); err != nil {
		if isCRDNotInstalledError(err) {
			return nil, nil
		}
		return nil, err
	}
	prefix := agentProfileServiceAccountPrefix + p.Name + "-"
	var out []string
	for i := range sas.Items {
		sa := &sas.Items[i]
		tier, ok := strings.CutPrefix(sa.Name, prefix)
		if !ok || !agentProfileOwnsServiceAccount(sa, p) || !allowedTierNameRE.MatchString(tier) {
			continue
		}
		out = append(out, tier)
	}
	sort.Strings(out)
	return out, nil
}

// allowedTierNameRE is the CRD's rule for a name allowedTiers may list.
var allowedTierNameRE = regexp.MustCompile(`^[a-z0-9]{1,10}$`)

// renderedTierModesAnnotation carries a narrowed copy's tier modes to the
// renderers. Set only on the in-memory copies renderableAgentProfiles makes,
// never written to the API server.
const renderedTierModesAnnotation = "kubeagents.x-k8s.io/internal-rendered-tier-modes"

// renderableAgentProfiles narrows each profile to its renderable tiers and
// leaves out a profile with none. It returns each profile's verdict, keyed
// by name, for its conditions.
func renderableAgentProfiles(ctx context.Context, c client.Reader, ns string, profiles []agentv1alpha1.AgentProfile, unreadable bool) ([]agentv1alpha1.AgentProfile, map[string]tierVerdict, error) {
	cat, err := loadTierCatalog(ctx, c, ns, unreadable)
	if err != nil {
		return nil, nil, err
	}
	verdicts := make(map[string]tierVerdict, len(profiles))
	var out []agentv1alpha1.AgentProfile
	for i := range profiles {
		p := &profiles[i]
		rendered, err := renderedTiersOf(ctx, c, p)
		if err != nil {
			return nil, nil, err
		}
		v := decideTiers(p, cat, rendered)
		verdicts[p.Name] = v
		if len(v.renderable) == 0 {
			continue
		}
		out = append(out, *narrowedCopy(p, v))
	}
	return out, verdicts, nil
}

// narrowedCopy is p with allowedTiers narrowed to v's renderable tiers and
// their modes carried for the renderers. p is not changed.
func narrowedCopy(p *agentv1alpha1.AgentProfile, v tierVerdict) *agentv1alpha1.AgentProfile {
	cp := p.DeepCopy()
	cp.Spec.AllowedTiers = append([]string(nil), v.renderable...)
	if cp.Annotations == nil {
		cp.Annotations = map[string]string{}
	}
	var pairs []string
	for _, tier := range v.renderable {
		pairs = append(pairs, tier+"="+v.modes[tier])
	}
	cp.Annotations[renderedTierModesAnnotation] = strings.Join(pairs, ",")
	return cp
}

// tierModeOf is a tier's mode on a profile the renderers were given: the
// built-in mode for read and unattended, which no Tier redefines, else from
// the narrowed copy's annotation. Only narrowedCopy's annotation is meant
// here; writeStatus strips a user-set one before judging the raw object.
func tierModeOf(p *agentv1alpha1.AgentProfile, tier string) string {
	if mode, ok := builtinTierCatalog()[tier]; ok {
		return mode
	}
	for _, pair := range strings.Split(p.Annotations[renderedTierModesAnnotation], ",") {
		if name, mode, ok := strings.Cut(pair, "="); ok && name == tier {
			return mode
		}
	}
	return ""
}

// unattendedTierOf is the profile's unattended-mode tier, "" for none: at most
// one, since decideTiers refuses a second.
func unattendedTierOf(p *agentv1alpha1.AgentProfile) string {
	for _, tier := range p.AllowedTierList() {
		if tierModeOf(p, tier) == tierModeUnattended {
			return tier
		}
	}
	return ""
}

// The TiersRendered condition's reasons.
const (
	reasonTiersRendered    = "AllTiersRendered"
	reasonTierUnknown      = "TierUnknown"
	reasonTierModeConflict = "TierModeConflict"
)

// reason is the TiersRendered reason: an unknown tier first, since it is the
// one an operator fixes by creating a Tier.
func (v tierVerdict) reason() string {
	switch {
	case len(v.unknown) > 0:
		return reasonTierUnknown
	case len(v.conflict) > 0:
		return reasonTierModeConflict
	}
	return reasonTiersRendered
}

// message says which tiers render and why the others don't.
func (v tierVerdict) message() string {
	var parts []string
	if len(v.renderable) > 0 {
		parts = append(parts, "rendered: "+strings.Join(v.renderable, ", "))
	} else {
		parts = append(parts, "no tier renders")
	}
	if len(v.unknown) > 0 {
		parts = append(parts, "no built-in tier or Tier resource in the namespace for: "+strings.Join(v.unknown, ", "))
	}
	if len(v.conflict) > 0 {
		serving := ""
		for _, tier := range v.renderable {
			if v.modes[tier] == tierModeUnattended {
				serving = tier
			}
		}
		if serving != "" {
			parts = append(parts, "a profile serves one unattended-mode tier, and these are refused beside "+serving+", the one it serves: "+
				strings.Join(v.conflict, ", "))
		} else {
			parts = append(parts, "a profile serves at most one unattended-mode tier, and none of these renders until all but one are removed: "+
				strings.Join(v.conflict, ", "))
		}
	}
	return strings.Join(parts, "; ")
}
