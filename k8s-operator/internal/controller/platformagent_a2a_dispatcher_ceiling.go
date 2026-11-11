/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The dispatcher's identity-map entry names every profile durable it reads,
// so it grows with the profile count, and one entry can't be split across
// shards: past a shard's budget its ConfigMap is one the API server refuses.
// Until the callout derives those grants per connection
// (gke-labs/kube-agents#5381), the operator binds profiles oldest first only
// while the entry fits (refuseOverDispatcherCeiling), refuses the newer ones,
// and reports where the entry stands. Past the budget anyway, which only the
// draining profiles' grants can do, it runs no dispatcher rather than write a
// map it can't apply.
const (
	dispatcherCeilingCondition = "DispatcherCeiling"

	dispatcherCeilingReasonWithin  = "WithinBudget"
	dispatcherCeilingReasonNear    = "NearBudget"
	dispatcherCeilingReasonRefused = "ProfilesRefused"
	dispatcherCeilingReasonOver    = "OverBudget"

	// dispatcherCeilingIssue is the change that lifts the identity ceiling.
	dispatcherCeilingIssue = "gke-labs/kube-agents#5381"
	// dispatchConfigCeilingIssue is the change that lifts the dispatch
	// config's.
	dispatchConfigCeilingIssue = "gke-labs/kube-agents#5479"
)

// a2aDispatcherCeilingBytes is the most the dispatcher's entry may add to a
// shard: the shard budget less the envelope every shard carries.
const a2aDispatcherCeilingBytes = a2aAuthMapShardByteBudget - a2aAuthMapShardEnvelopeBytes

// a2aDispatcherWarnBytes is four fifths of it: past this the condition warns.
const a2aDispatcherWarnBytes = a2aDispatcherCeilingBytes * 4 / 5

// a2aDispatcherDrainingReserve is what the profile cut leaves for the
// grants of profiles that have left and still drain: two subjects and a stop
// subscription each, about 250 bytes, so room for some 130 at once. Past it
// the entry can still overflow, which the map's own check catches.
const a2aDispatcherDrainingReserve = 32 * 1024

// a2aDispatcherProfileBudget is what the live profiles' share may come to.
const a2aDispatcherProfileBudget = a2aDispatcherCeilingBytes - a2aDispatcherDrainingReserve

// a2aDispatchConfigLimitBytes is the most a ConfigMap's data may hold; the
// dispatch config is one key in one ConfigMap (gke-labs/kube-agents#5479).
const a2aDispatchConfigLimitBytes = 1 << 20

// a2aDispatchConfigEnvelopeBytes is left over for the ConfigMap's key and
// the slack between a profile's measured share and its place in the array.
const a2aDispatchConfigEnvelopeBytes = 4 * 1024

// a2aDispatchConfigProfileBudget is what the live profiles' entries may come
// to: the limit, less the envelope and the same reserve for the draining
// profiles' entries, which are smaller than their identity grants.
const a2aDispatchConfigProfileBudget = a2aDispatchConfigLimitBytes - a2aDispatchConfigEnvelopeBytes - a2aDispatcherDrainingReserve

// The two limits a profile can be cut at.
const (
	dispatcherCeilingLimitIdentity = "identity"
	dispatcherCeilingLimitConfig   = "config"
)

// dispatcherCeilingRemoved is the verdict that takes the condition away: the
// dispatcher switched off, or the A2A stack gone.
var dispatcherCeilingRemoved = &metav1.Condition{}

// a2aDispatcherMapEntry is the dispatcher's identity-map entry for these
// profiles, as renderA2AAuthMapDraining writes it.
func a2aDispatcherMapEntry(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, draining []a2aDispatchProfile) (entry a2aAuthMapIdentity, profileCount int) {
	names, durables := dispatcherGrantNames(agent, profiles, draining)
	id := dispatcherIdentity(agent, agent.Namespace, names, durables)
	return a2aAuthMapIdentity{
		ServiceAccount: id.serviceAccount,
		User:           id.user,
		Account:        id.account,
		Grants:         a2aAuthMapGrants{Publish: id.publish, Subscribe: id.subscribe},
	}, len(names)
}

// a2aDispatcherProfileShares is the dispatcher entry's size with no profiles,
// and the bytes each of these profiles adds to it: its durables' grants and its
// supervisor and stop subjects, as dispatcherIdentity writes them for it. The
// entry's arrays never empty, so a profile's lines add exactly, and the entry
// for any set of them is base plus their shares.
func a2aDispatcherProfileShares(agent *agentv1alpha1.PlatformAgent, profiles []*agentv1alpha1.AgentProfile) (base int, shares []int, err error) {
	measure := func(names, durables []string) (int, error) {
		id := dispatcherIdentity(agent, agent.Namespace, names, durables)
		return a2aAuthMapEntryBytes(a2aAuthMapIdentity{
			ServiceAccount: id.serviceAccount, User: id.user, Account: id.account,
			Grants: a2aAuthMapGrants{Publish: id.publish, Subscribe: id.subscribe},
		})
	}
	if base, err = measure(nil, nil); err != nil {
		return 0, nil, err
	}
	for _, p := range profiles {
		var durables []string
		for _, tier := range p.AllowedTierList() {
			for _, kind := range a2aSubmitKinds {
				durables = append(durables, profileDurableName(p.Name, tier, kind))
			}
		}
		n, err := measure([]string{p.Name}, durables)
		if err != nil {
			return 0, nil, err
		}
		shares = append(shares, n-base)
	}
	return base, shares, nil
}

// a2aDispatchConfigShares is the dispatch config's size with no profiles,
// and the bytes each of these profiles' entries adds to it, as
// buildA2ADispatchConfigMapWith writes it (compact JSON). A profile that allows
// unattended under Job mode carries its Job template; measured both with and
// without image volumes, it is charged the larger, since which one the
// cluster takes isn't known here. Each share is measured alone and charged
// one more byte for the comma it takes in the array: exact for every entry
// after the first, one over for the first, never under.
func a2aDispatchConfigShares(agent *agentv1alpha1.PlatformAgent, profiles []*agentv1alpha1.AgentProfile) (base int, shares []int, err error) {
	jobMode := dispatcherJobPolicyRendered()
	header := dispatchConfigHeader(agent)
	header.Version = strings.Repeat("0", 16)
	if jobMode {
		header.JobLabels = dispatchJobLabels(agent)
	}
	measure := func(entries []a2aDispatchProfile) (int, error) {
		cfg := header
		cfg.Profiles = entries
		body, err := json.Marshal(cfg)
		return len(body), err
	}
	if base, err = measure([]a2aDispatchProfile{}); err != nil {
		return 0, nil, err
	}
	for _, p := range profiles {
		var tmpl *corev1.PodTemplateSpec
		if jobMode && slices.Contains(p.AllowedTierList(), agentv1alpha1.AgentProfileTierUnattended) {
			with, without := buildProfileJobTemplate(agent, p, true), buildProfileJobTemplate(agent, p, false)
			tmpl = with
			if a, b := jsonLen(with), jsonLen(without); b > a {
				tmpl = without
			}
		}
		n, err := measure([]a2aDispatchProfile{dispatchProfileEntry(agent, p, tmpl)})
		if err != nil {
			return 0, nil, err
		}
		shares = append(shares, n-base+1)
	}
	return base, shares, nil
}

func jsonLen(v any) int {
	b, _ := json.Marshal(v)
	return len(b)
}

// dispatcherCeilingCut is how many of bound, oldest first, fit under both the
// dispatcher's identity budget and the dispatch config's, and which limit
// stopped it ("" when all fit). Once one doesn't fit, every newer one is cut
// too, even a smaller one that would: a later profile never jumps an earlier.
func dispatcherCeilingCut(agent *agentv1alpha1.PlatformAgent, bound []*agentv1alpha1.AgentProfile) (fit int, limit string, err error) {
	idBase, idShares, err := a2aDispatcherProfileShares(agent, bound)
	if err != nil {
		return 0, "", err
	}
	cfgBase, cfgShares, err := a2aDispatchConfigShares(agent, bound)
	if err != nil {
		return 0, "", err
	}
	idUsed, cfgUsed := idBase, cfgBase
	for i := range bound {
		if idUsed+idShares[i] > a2aDispatcherProfileBudget {
			return i, dispatcherCeilingLimitIdentity, nil
		}
		if cfgUsed+cfgShares[i] > a2aDispatchConfigProfileBudget {
			return i, dispatcherCeilingLimitConfig, nil
		}
		idUsed += idShares[i]
		cfgUsed += cfgShares[i]
	}
	return len(bound), "", nil
}

// dispatcherCeilingLimitPhrase says, in a message, what a cut at limit ran into.
func dispatcherCeilingLimitPhrase(limit string) string {
	if limit == dispatcherCeilingLimitConfig {
		return "before its dispatch config outgrows one ConfigMap"
	}
	return "before its bus identity outgrows one identity-map shard"
}

// refuseOverDispatcherCeiling refuses, in out, every bound profile past the
// point where the dispatcher's entry would outgrow its share of a shard.
// sorted is oldest first (name breaks ties), so the profiles already bound
// keep dispatch and the newest are the ones refused, and the same profiles
// always give the same cut. Once one doesn't fit, every newer one is refused
// too, even a smaller one that would: a later profile never jumps an earlier.
func refuseOverDispatcherCeiling(agent *agentv1alpha1.PlatformAgent, sorted []agentv1alpha1.AgentProfile, out map[string]agentProfileResolution) {
	var bound []*agentv1alpha1.AgentProfile
	for i := range sorted {
		if out[sorted[i].Name].refused == nil {
			bound = append(bound, &sorted[i])
		}
	}
	fit, limit, err := dispatcherCeilingCut(agent, bound)
	if err != nil {
		return // the map's and the apply's own checks refuse what can't be measured
	}
	for _, p := range bound[fit:] {
		out[p.Name] = agentProfileResolution{
			refused: fmt.Errorf("the profile dispatcher serves the %d oldest profiles %s, "+
				"and this one is newer, so it isn't bound; remove an older profile, or see %s for lifting the ceiling", fit, dispatcherCeilingLimitPhrase(limit), dispatcherCeilingIssueFor(limit)),
			reason: reasonAgentProfileDispatcherCeiling,
		}
	}
}

// dispatcherCeilingIssueFor is the change that lifts the limit a cut ran into.
func dispatcherCeilingIssueFor(limit string) string {
	if limit == dispatcherCeilingLimitConfig {
		return dispatchConfigCeilingIssue
	}
	return dispatcherCeilingIssue
}

// a2aDispatcherCeilingRefused is the profiles refuseOverDispatcherCeiling
// refuses, oldest first.
func a2aDispatcherCeilingRefused(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) []string {
	resolved := resolveAgentProfileIdentities(agent, profiles)
	var out []string
	for _, p := range sortedOldestFirst(profiles) {
		if resolved[p.Name].reason == reasonAgentProfileDispatcherCeiling {
			out = append(out, p.Name)
		}
	}
	return out
}

// dispatcherCeilingStanding is where the bound profiles stand against the
// cut: how many fit, which limit stopped the cut, and the dispatch config's
// size for the ones that fit.
func dispatcherCeilingStanding(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile) (fit int, limit string, configBytes int, err error) {
	resolved := resolveAgentProfileIdentities(agent, profiles)
	sorted := sortedOldestFirst(profiles)
	var bound []*agentv1alpha1.AgentProfile
	for i := range sorted {
		if r := resolved[sorted[i].Name]; r.refused == nil || r.reason == reasonAgentProfileDispatcherCeiling {
			bound = append(bound, &sorted[i])
		}
	}
	if fit, limit, err = dispatcherCeilingCut(agent, bound); err != nil {
		return 0, "", 0, err
	}
	base, shares, err := a2aDispatchConfigShares(agent, bound[:fit])
	if err != nil {
		return 0, "", 0, err
	}
	configBytes = base
	for _, n := range shares {
		configBytes += n
	}
	return fit, limit, configBytes, nil
}

// a2aDispatcherCeiling measures the dispatcher's entry as a shard carries it
// and returns the condition that says where it stands, and whether it is
// past the ceiling, in which case the dispatcher isn't run. Only meaningful
// while the dispatcher is switched on.
func a2aDispatcherCeiling(agent *agentv1alpha1.PlatformAgent, profiles []agentv1alpha1.AgentProfile, draining []a2aDispatchProfile) (condition metav1.Condition, over bool, err error) {
	entry, n := a2aDispatcherMapEntry(agent, profiles, draining)
	size, err := a2aAuthMapEntryBytes(entry)
	if err != nil {
		return metav1.Condition{}, false, fmt.Errorf("measuring the dispatcher's identity entry: %w", err)
	}
	condition = metav1.Condition{Type: dispatcherCeilingCondition, ObservedGeneration: agent.Generation}
	pct := size * 100 / a2aDispatcherCeilingBytes
	refused := a2aDispatcherCeilingRefused(agent, profiles)
	fit, limit, configBytes, err := dispatcherCeilingStanding(agent, profiles)
	if err != nil {
		return metav1.Condition{}, false, fmt.Errorf("measuring the dispatch config: %w", err)
	}
	configPct := configBytes * 100 / a2aDispatchConfigLimitBytes
	switch {
	case size > a2aDispatcherCeilingBytes:
		over = true
		condition.Status = metav1.ConditionFalse
		condition.Reason = dispatcherCeilingReasonOver
		condition.Message = fmt.Sprintf("the profile dispatcher's identity entry for %d profiles, %d of them draining, is %d bytes, over the %d bytes one identity-map shard can carry, "+
			"so the operator runs no dispatcher, and profile tasks aren't dispatched, until those drains run out. Lifting the ceiling is %s",
			n, len(draining), size, a2aDispatcherCeilingBytes, dispatcherCeilingIssue)
	case len(refused) > 0:
		condition.Status = metav1.ConditionFalse
		condition.Reason = dispatcherCeilingReasonRefused
		condition.Message = fmt.Sprintf("the profile dispatcher serves %d profiles %s; "+
			"the %d newer ones aren't bound (the newest refused: %s), and the dispatcher serves the rest. Lifting the ceiling is %s",
			fit, dispatcherCeilingLimitPhrase(limit), len(refused), refused[len(refused)-1], dispatcherCeilingIssueFor(limit))
	case configPct >= 80 && configPct >= pct:
		condition.Status = metav1.ConditionTrue
		condition.Reason = dispatcherCeilingReasonNear
		condition.Message = fmt.Sprintf("the profile dispatcher's config for %d profiles is %d bytes, %d%% of the %d bytes one ConfigMap can hold; "+
			"past it the operator binds no newer profiles (about 200 profiles that allow unattended under Job mode). Lifting the ceiling is %s",
			fit, configBytes, configPct, a2aDispatchConfigLimitBytes, dispatchConfigCeilingIssue)
	case size >= a2aDispatcherWarnBytes:
		condition.Status = metav1.ConditionTrue
		condition.Reason = dispatcherCeilingReasonNear
		condition.Message = fmt.Sprintf("the profile dispatcher's identity entry for %d profiles is %d bytes, %d%% of the %d bytes one identity-map shard can carry; "+
			"past it the operator binds no newer profiles (about 650 profiles at two tiers). Lifting the ceiling is %s", n, size, pct, a2aDispatcherCeilingBytes, dispatcherCeilingIssue)
	default:
		condition.Status = metav1.ConditionTrue
		condition.Reason = dispatcherCeilingReasonWithin
		condition.Message = fmt.Sprintf("the profile dispatcher's identity entry for %d profiles is %d bytes, %d%% of the %d bytes one identity-map shard can carry; its config is %d bytes, %d%% of one ConfigMap",
			n, size, pct, a2aDispatcherCeilingBytes, configBytes, configPct)
	}
	return condition, over, nil
}

// syncDispatcherCeiling writes the DispatcherCeiling condition on the
// way out of Reconcile, as syncDispatcherJobMode does and for its reasons:
// nil leaves it as it was, dispatcherCeilingRemoved takes it away, anything
// else is its new value. A move into NearBudget or OverBudget is also a
// Warning event, once per transition.
func (r *PlatformAgentReconciler) syncDispatcherCeiling(ctx context.Context, agent *agentv1alpha1.PlatformAgent, verdict *metav1.Condition) error {
	switch verdict {
	case nil:
		return nil
	case dispatcherCeilingRemoved:
		if meta.RemoveStatusCondition(&agent.Status.Conditions, dispatcherCeilingCondition) {
			return r.Status().Update(ctx, agent)
		}
		return nil
	}
	condition := *verdict
	condition.LastTransitionTime = metav1.Now()
	// The reason read before the write: SetStatusCondition updates the
	// existing element in place, so a pointer to it would read the new one.
	priorReason := ""
	if prior := meta.FindStatusCondition(agent.Status.Conditions, dispatcherCeilingCondition); prior != nil {
		priorReason = prior.Reason
	}
	if !meta.SetStatusCondition(&agent.Status.Conditions, condition) {
		return nil
	}
	if condition.Reason != dispatcherCeilingReasonWithin && priorReason != condition.Reason {
		r.recordEvent(agent, "Warning", "Dispatcher"+condition.Reason, condition.Message)
	}
	return r.Status().Update(ctx, agent)
}
