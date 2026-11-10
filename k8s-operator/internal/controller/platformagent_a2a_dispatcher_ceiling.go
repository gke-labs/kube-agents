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
	"fmt"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The dispatcher's identity-map entry names every profile durable it reads,
// so it grows with the profile count, and one entry can't be split across
// shards: past a shard's budget its ConfigMap is one the API server refuses.
// Until the callout derives those grants per connection
// (gke-labs/kube-agents#5381), the operator reports the entry's size against
// the budget, warns near it, and past it runs no dispatcher rather than write
// a map it can't apply.
const (
	dispatcherCeilingCondition = "DispatcherCeiling"

	dispatcherCeilingReasonWithin = "WithinBudget"
	dispatcherCeilingReasonNear   = "NearBudget"
	dispatcherCeilingReasonOver   = "OverBudget"

	// dispatcherCeilingIssue is the change that lifts the ceiling.
	dispatcherCeilingIssue = "gke-labs/kube-agents#5381"
)

// a2aDispatcherCeilingBytes is the most the dispatcher's entry may add to a
// shard: the shard budget less the envelope every shard carries.
const a2aDispatcherCeilingBytes = a2aAuthMapShardByteBudget - a2aAuthMapShardEnvelopeBytes

// a2aDispatcherWarnBytes is four fifths of it: past this the condition warns.
const a2aDispatcherWarnBytes = a2aDispatcherCeilingBytes * 4 / 5

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
	switch {
	case size > a2aDispatcherCeilingBytes:
		over = true
		condition.Status = metav1.ConditionFalse
		condition.Reason = dispatcherCeilingReasonOver
		condition.Message = fmt.Sprintf("the profile dispatcher's identity entry for %d profiles is %d bytes, over the %d bytes one identity-map shard can carry, "+
			"so the operator runs no dispatcher and profile tasks aren't dispatched; remove profiles, or switch the dispatcher off "+
			"(operator.profileDispatcher.enabled: false). Lifting the ceiling is %s", n, size, a2aDispatcherCeilingBytes, dispatcherCeilingIssue)
	case size >= a2aDispatcherWarnBytes:
		condition.Status = metav1.ConditionTrue
		condition.Reason = dispatcherCeilingReasonNear
		condition.Message = fmt.Sprintf("the profile dispatcher's identity entry for %d profiles is %d bytes, %d%% of the %d bytes one identity-map shard can carry; "+
			"past it the operator stops running the dispatcher (about 700 profiles at two tiers). Lifting the ceiling is %s", n, size, pct, a2aDispatcherCeilingBytes, dispatcherCeilingIssue)
	default:
		condition.Status = metav1.ConditionTrue
		condition.Reason = dispatcherCeilingReasonWithin
		condition.Message = fmt.Sprintf("the profile dispatcher's identity entry for %d profiles is %d bytes, %d%% of the %d bytes one identity-map shard can carry",
			n, size, pct, a2aDispatcherCeilingBytes)
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
