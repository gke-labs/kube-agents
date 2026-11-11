package controller

import (
	"context"
	"encoding/json"
	"slices"
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A tier that leaves a profile drains instead of going at once (#3659, Tier
// PR 2b-ii). A tier leaves when allowedTiers drops it, when its Tier is
// deleted, or when it stops rendering (unknown, or refused beside another
// unattended-mode tier). While it drains, the profile keeps the tier's
// ServiceAccount, bus identity, warm pool and durables, so a task already
// running at it finishes; the dispatcher refuses what is queued at it
// tier-withdrawn and starts nothing new there; and the card, the dispatch
// config's live tiers and the Job template leave it out at once. It drains
// for a2aDrainingWindow, the longest deadline a task can carry plus a margin,
// as a profile that leaves the install does (#5074).
//
// The state is the applied dispatch config, which only the operator writes:
// a tier the applied config lists for a profile, live or draining, that this
// render no longer renders, drains. Both controllers read it, so the
// AgentProfile controller keeps a dropped tier's ServiceAccount and durables
// on the very pass that drops it, before the PlatformAgent controller has
// written the drain. Nothing a user writes (spec, status, annotations) can
// start or extend one, and the config itself only says which tiers left and
// when: a tier drains only while the profile has its own ServiceAccount for
// it, at the mode the catalog or that ServiceAccount gives (decideDrains).
// An end, once written to the config, is kept and clamped to the window. A
// tier the config still lists live starts its window on each pass, so a
// PlatformAgent controller that never manages to write the drain, or a hand
// edit moving the tier back to live, extends it; that only keeps what the
// profile already had, never grants a tier anew.
//
// Not a revoke: work running at a removed tier carries on until the drain
// ends. The emergency path is the stop and the breaker.
//
// Not drained: a profile left with no live tier, which leaves the install
// and drains as a removed profile does; and a profile on a named
// ServiceAccount (spec.identity), which serves exactly one tier, so it can't
// hold a second while one drains.

// drainingTiersAnnotation carries a narrowed copy's draining tiers and their
// ends (tier=unix seconds, comma-separated) to the renderers. Set only on the
// in-memory copies renderableAgentProfiles makes, never written to the API
// server.
const drainingTiersAnnotation = "kubeagents.x-k8s.io/internal-draining-tiers"

// appliedDispatchProfiles is the applied dispatch config's profiles by name.
// None applied (NotFound, or a dispatcher that is off and removed it) drains
// nothing; a body that doesn't decode drains nothing either, and the
// PlatformAgent controller records a Warning for it (dispatchDraining). Any
// other read error is returned, so the pass retries instead of dropping every
// drain.
func appliedDispatchProfiles(ctx context.Context, c client.Reader, agent *agentv1alpha1.PlatformAgent) (map[string]a2aDispatchProfile, error) {
	cm := &corev1.ConfigMap{}
	if err := c.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, cm); err != nil {
		if apierrors.IsNotFound(err) || isCRDNotInstalledError(err) {
			return nil, nil
		}
		return nil, err
	}
	var applied a2aDispatchConfig
	if err := json.Unmarshal([]byte(cm.Data[a2aDispatchConfigKey]), &applied); err != nil {
		return nil, nil
	}
	out := make(map[string]a2aDispatchProfile, len(applied.Profiles))
	for _, p := range applied.Profiles {
		if p.DrainingUntil.IsZero() {
			out[p.Name] = p
		}
	}
	return out, nil
}

// decideDrains adds to v the tiers p drains at now, from prev, the profile's
// entry in the applied dispatch config: each tier prev lists live that v
// doesn't render drains from now; each tier prev already drains keeps its
// end, clamped to the window, until that passes. A tier v renders again
// stops draining.
//
// prev only says which tiers left and when; nothing else is taken from it.
// A tier drains only if the profile still has its own operator-created
// ServiceAccount for it (owned, ownedTierServiceAccounts), which only a
// rendered tier gets, so an entry naming a tier this profile never rendered
// drains nothing. Its mode is the catalog's while its Tier exists (a Tier's
// mode can't change), else the one the ServiceAccount recorded when the
// operator made it; with neither, it doesn't drain and goes at once. A tier
// the components would skip (a legacy name, a built-in under another mode)
// never drains.
func decideDrains(p *agentv1alpha1.AgentProfile, v *tierVerdict, prev *a2aDispatchProfile, owned map[string]string, cat tierCatalog, now time.Time) {
	if len(v.renderable) == 0 || p.Spec.Identity.ServiceAccountName != "" {
		return
	}
	ceiling := now.Add(a2aDrainingWindow)
	add := func(tier string, until time.Time) {
		recorded, ours := owned[tier]
		if slices.Contains(v.renderable, tier) || !ours || !allowedTierNameRE.MatchString(tier) {
			return
		}
		mode, ok := cat[tier]
		if !ok {
			mode = recorded
		}
		if (mode != tierModeInteractive && mode != tierModeUnattended) || tierSkipReason(tier, mode) != "" {
			return
		}
		if _, dup := v.draining[tier]; dup {
			return
		}
		if until.IsZero() || until.After(ceiling) {
			until = ceiling.Truncate(time.Second).UTC()
		} else if !now.Before(until) {
			return
		}
		if v.draining == nil {
			v.draining = map[string]time.Time{}
		}
		v.draining[tier] = until
		v.modes[tier] = mode
	}
	for _, d := range prev.DrainingTiers {
		add(d.Tier, d.Until)
	}
	for _, tier := range prev.Tiers {
		add(tier, time.Time{})
	}
}

// withoutDraining is owned less the tiers prev already drains, so the
// status-lost fallback (renderedTiersOf) doesn't count a draining tier's
// ServiceAccount as an incumbent: a draining unattended tier listed again
// beside the live one would otherwise make both incumbents, and both conflict.
func withoutDraining(owned map[string]string, prev a2aDispatchProfile) map[string]string {
	if len(prev.DrainingTiers) == 0 {
		return owned
	}
	out := make(map[string]string, len(owned))
	for tier, mode := range owned {
		out[tier] = mode
	}
	for _, d := range prev.DrainingTiers {
		delete(out, d.Tier)
	}
	return out
}

// drainingTiers is v's draining tiers, sorted.
func (v tierVerdict) drainingTiers() []string {
	out := make([]string, 0, len(v.draining))
	for tier := range v.draining {
		out = append(out, tier)
	}
	slices.Sort(out)
	return out
}

// drainingTiersOf is a narrowed copy's draining tiers and their ends, from
// drainingTiersAnnotation; empty on any other profile.
func drainingTiersOf(p *agentv1alpha1.AgentProfile) map[string]time.Time {
	out := map[string]time.Time{}
	for _, pair := range strings.Split(p.Annotations[drainingTiersAnnotation], ",") {
		tier, unix, ok := strings.Cut(pair, "=")
		if !ok {
			continue
		}
		if sec, err := strconv.ParseInt(unix, 10, 64); err == nil {
			out[tier] = time.Unix(sec, 0).UTC()
		}
	}
	return out
}

// liveTiersOf is the tiers a profile takes new work at: allowedTiers less
// the draining ones. On a narrowed copy these are the renderable tiers.
func liveTiersOf(p *agentv1alpha1.AgentProfile) []string {
	drains := drainingTiersOf(p)
	var out []string
	for _, tier := range p.AllowedTierList() {
		if _, ok := drains[tier]; !ok {
			out = append(out, tier)
		}
	}
	return out
}
