package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/predicate"

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
// ends. An administrator ends a drain early with endTierDrainAnnotation
// (#5850).
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

// endingTiersAnnotation carries a narrowed copy's ending tiers (decideDrains,
// #5850) to the dispatch config, comma-separated. Set only on the in-memory
// copies renderableAgentProfiles makes, never written to the API server.
const endingTiersAnnotation = "kubeagents.x-k8s.io/internal-ending-tiers"

// endTierDrainAnnotation is the administrator's lever on an AgentProfile
// (#5850): "<tier>[,<tier>]" ends the drain of each listed tier the profile is
// draining. The dispatcher ends the tasks it holds there (canceled
// tier-ended) and the tier is torn down after endTierDrainGrace. Anyone who
// can update the AgentProfile can set it, the same people who can drop the
// tier or delete the profile. The operator consumes it: once the applied
// dispatch config marks a tier ending, that tier leaves the annotation, and a
// listed tier that is live or not draining is stripped with a Warning event,
// so a leftover never cuts a later drain.
const endTierDrainAnnotation = "kubeagents.x-k8s.io/end-tier-drain"

// endTierDrainGrace is how long after the lever a tier's drain ends at the
// latest: the dispatcher's refresh and its abandon, with room to spare. The
// PlatformAgent pass is requeued at the drain's end (earliestDrainEnd), so
// the teardown follows it closely.
const endTierDrainGrace = 2 * time.Minute

// endTierDrainRecheck is how soon the AgentProfile pass looks again while
// the lever is set, to consume it inside endTierDrainGrace.
const endTierDrainRecheck = 15 * time.Second

// endTierDrainRequested is the tier names p's endTierDrainAnnotation lists.
func endTierDrainRequested(p *agentv1alpha1.AgentProfile) map[string]bool {
	out := map[string]bool{}
	for _, tier := range strings.Split(p.Annotations[endTierDrainAnnotation], ",") {
		if tier = strings.TrimSpace(tier); tier != "" {
			out[tier] = true
		}
	}
	return out
}

// endingTiersOf is a narrowed copy's ending tiers, from endingTiersAnnotation.
func endingTiersOf(p *agentv1alpha1.AgentProfile) map[string]bool {
	out := map[string]bool{}
	for _, tier := range strings.Split(p.Annotations[endingTiersAnnotation], ",") {
		if tier != "" {
			out[tier] = true
		}
	}
	return out
}

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
	requested := endTierDrainRequested(p)
	prevEnding := map[string]bool{}
	for _, d := range prev.DrainingTiers {
		if d.Ending {
			prevEnding[d.Tier] = true
		}
	}
	v.appliedEnding = prevEnding
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
		// Ended by an administrator: this pass, or carried from the applied
		// config once the annotation is consumed. The drain then ends at
		// most endTierDrainGrace from now, long enough for the dispatcher
		// to end the tasks it holds before the teardown removes their pods.
		ending := requested[tier] || prevEnding[tier]
		if ending {
			if short := now.Add(endTierDrainGrace).Truncate(time.Second).UTC(); short.Before(until) {
				until = short
			}
			if v.ending == nil {
				v.ending = map[string]bool{}
			}
			v.ending[tier] = true
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

// consumeEndTierDrain keeps p's endTierDrainAnnotation to the tiers it may
// still act on: draining, and not yet marked ending in the applied dispatch
// config. A tier the applied config already marks ending is consumed (the
// config carries the end from here, decideDrains); a listed tier that is live,
// or that p isn't draining, is stripped with a Warning, so a leftover entry
// can never cut a drain that starts later. The patch carries p's
// resourceVersion, so an edit made since this pass read p is never lost; on a
// conflict the next pass tries again.
func (r *AgentProfileReconciler) consumeEndTierDrain(ctx context.Context, p *agentv1alpha1.AgentProfile, v tierVerdict) error {
	requested := endTierDrainRequested(p)
	if len(requested) == 0 {
		return nil
	}
	var keep, consumed, live, notDraining []string
	for _, tier := range endTierDrainSorted(requested) {
		_, draining := v.draining[tier]
		switch {
		case v.appliedEnding[tier]:
			consumed = append(consumed, tier)
		case draining:
			keep = append(keep, tier)
		case slices.Contains(v.renderable, tier):
			live = append(live, tier)
		default:
			notDraining = append(notDraining, tier)
		}
	}
	if len(keep) == len(requested) {
		return nil
	}
	base := p.DeepCopy()
	if len(keep) == 0 {
		delete(p.Annotations, endTierDrainAnnotation)
	} else {
		p.Annotations[endTierDrainAnnotation] = strings.Join(keep, ",")
	}
	if err := r.Patch(ctx, p, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		if apierrors.IsConflict(err) {
			*p = *base
			return nil // the next pass reads the newer object and tries again
		}
		return fmt.Errorf("consume the %s annotation: %w", endTierDrainAnnotation, err)
	}
	if r.Recorder != nil {
		if len(consumed) > 0 {
			r.Recorder.Eventf(p, corev1.EventTypeNormal, "TierDrainEnded",
				"ended the drain of tier(s) %s: the dispatcher ends their running tasks, and the tier goes within %s", strings.Join(consumed, ", "), endTierDrainGrace)
		}
		if len(live) > 0 {
			r.Recorder.Eventf(p, corev1.EventTypeWarning, "EndTierDrainIgnored",
				"%s lists tier(s) %s, which this profile runs (not draining); removed from the annotation, nothing ended", endTierDrainAnnotation, strings.Join(live, ", "))
		}
		if len(notDraining) > 0 {
			r.Recorder.Eventf(p, corev1.EventTypeWarning, "EndTierDrainIgnored",
				"%s lists tier(s) %s, which this profile isn't draining (or whose drain has already ended); removed from the annotation. It ends one tier's drain on a profile that still runs another tier; a profile left with no tier drains whole and isn't covered", endTierDrainAnnotation, strings.Join(notDraining, ", "))
		}
	}
	return nil
}

func endTierDrainSorted(m map[string]bool) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	slices.Sort(out)
	return out
}

// earliestDrainEnd is how long until the first draining tier among the
// rendered profiles ends, plus a second so the pass lands after it; zero when
// none drains.
func earliestDrainEnd(profiles []agentv1alpha1.AgentProfile, now time.Time) time.Duration {
	var soonest time.Duration
	for i := range profiles {
		for _, until := range drainingTiersOf(&profiles[i]) {
			d := until.Sub(now) + time.Second
			if d < time.Second {
				d = time.Second
			}
			if soonest == 0 || d < soonest {
				soonest = d
			}
		}
	}
	return soonest
}

// endTierDrainChanged passes an AgentProfile update whose endTierDrainAnnotation
// changed (#5850), which GenerationChangedPredicate doesn't.
func endTierDrainChanged() predicate.Predicate {
	return predicate.Funcs{
		UpdateFunc: func(e event.UpdateEvent) bool {
			if e.ObjectOld == nil || e.ObjectNew == nil {
				return false
			}
			return e.ObjectOld.GetAnnotations()[endTierDrainAnnotation] != e.ObjectNew.GetAnnotations()[endTierDrainAnnotation]
		},
		CreateFunc:  func(event.CreateEvent) bool { return false },
		DeleteFunc:  func(event.DeleteEvent) bool { return false },
		GenericFunc: func(event.GenericEvent) bool { return false },
	}
}
