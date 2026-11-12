// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"fmt"
	"slices"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// busStreamsDegradedEdit is what a parked pass does to Degraded for the bus
// streams (parkBusStreamsDegraded).
type busStreamsDegradedEdit int

const (
	// busStreamsDegradedKeep leaves Degraded as it stands.
	busStreamsDegradedKeep busStreamsDegradedEdit = iota
	// busStreamsDegradedSet writes the condition parkBusStreamsDegraded
	// returns: True/A2ABusStreamsMissing, or a spec refusal the streams'
	// reason outranked once that reason goes.
	busStreamsDegradedSet
	// busStreamsDegradedClear removes a Degraded/A2ABusStreamsMissing.
	busStreamsDegradedClear
)

// parkBusStreamsDegraded is the Degraded edit a pass parked in
// updateStatusDegraded makes for the bus streams, and the condition to write
// for busStreamsDegradedSet (#5707).
//
// Degraded's reason is otherwise ranked only by updateStatusReady, which a
// park returns before. The bus-stream poller keeps writing BusStreamsPresent
// through a park (a failed provision Job or missing sandbox keys can each
// last days), and every write wakes a pass through the unfiltered
// PlatformAgent watch, so the parked pass is where Degraded follows it. It
// does what updateStatusReady would do for this one reason and leaves every
// other reason alone:
//
//   - streams missing: Degraded reads A2ABusStreamsMissing with the poller's
//     message, unless it holds a reason updateStatusReady ranks above the
//     streams (degradedRanksAboveBusStreams). The park did not evaluate that
//     one, so it stays, as on any park before this.
//   - streams not missing: a Degraded/A2ABusStreamsMissing gives way to the
//     spec refusal it outranked, if one applies (specResourceDegraded), and is
//     removed otherwise, as updateStatusReady removes a Degraded no reason
//     holds. Any other reason stays; RBACIncomplete in particular is
//     reportRBACSkew's to clear.
//
// busStreamsMissingCondition is nil off the A2A stack, so a today install or
// an unrecognised mode gets no streams reason.
func parkBusStreamsDegraded(agent *agentv1alpha1.PlatformAgent) (busStreamsDegradedEdit, *metav1.Condition) {
	degraded := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	streams := busStreamsMissingCondition(agent)
	if streams == nil {
		if degraded == nil || degraded.Reason != conditionReasonA2ABusStreamsMissing {
			return busStreamsDegradedKeep, nil
		}
		// The streams' reason goes. A spec refusal it outranked comes back,
		// as updateStatusReady would show it; it is the one lower reason a
		// park can read, the rest being the unparked pass's.
		if reason, msg := specResourceDegraded(agent); reason != "" {
			return busStreamsDegradedSet, &metav1.Condition{
				Type: degradedConditionType, Status: metav1.ConditionTrue,
				Reason: reason, Message: msg, ObservedGeneration: agent.Generation,
			}
		}
		return busStreamsDegradedClear, nil
	}
	if degraded != nil && degraded.Status == metav1.ConditionTrue {
		if degradedRanksAboveBusStreams(degraded.Reason) {
			return busStreamsDegradedKeep, nil
		}
		// Compared as updateStatusReady compares it (degradedUnchanged), so
		// the two writers agree on what is current and neither rewrites the
		// other's condition.
		if degraded.Reason == conditionReasonA2ABusStreamsMissing && degraded.Message == streams.Message {
			return busStreamsDegradedKeep, nil
		}
	}
	return busStreamsDegradedSet, &metav1.Condition{
		Type:               degradedConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             conditionReasonA2ABusStreamsMissing,
		Message:            streams.Message,
		ObservedGeneration: agent.Generation,
	}
}

// degradedRanksAboveBusStreams reports whether updateStatusReady ranks a
// Degraded reason above A2ABusStreamsMissing. degradedConditionReasons lists
// the reasons in that ranking, so the ones before the streams reason are the
// ones above it.
func degradedRanksAboveBusStreams(reason string) bool {
	at := slices.Index(degradedConditionReasons, conditionReasonA2ABusStreamsMissing)
	return slices.Contains(degradedConditionReasons[:max(at, 0)], reason)
}

// applyParkBusStreamsDegraded makes the edit parkBusStreamsDegraded chose.
func applyParkBusStreamsDegraded(agent *agentv1alpha1.PlatformAgent, edit busStreamsDegradedEdit, cond *metav1.Condition, now metav1.Time) {
	switch edit {
	case busStreamsDegradedSet:
		c := *cond
		c.LastTransitionTime = now
		meta.SetStatusCondition(&agent.Status.Conditions, c)
	case busStreamsDegradedClear:
		meta.RemoveStatusCondition(&agent.Status.Conditions, degradedConditionType)
	}
}

// specResourceDegraded is the first of the spec's resource and exec-tier
// refusals, in updateStatusReady's ranking, as a Degraded reason and message;
// "" when none applies. They are read from the spec on every pass rather than
// carried from the reconcile that applied the defaults, so the condition
// clears on the pass the override is corrected. Each is Degraded only: the
// workload runs at the operator's defaults, so Ready and the phase keep what
// the workload says. A parked pass reads them too (parkBusStreamsDegraded):
// they are what Degraded falls back to when the bus streams' reason goes.
func specResourceDegraded(agent *agentv1alpha1.PlatformAgent) (string, string) {
	if refusal, _ := credentialProxyResourcesRefusal(agent); refusal != "" {
		return conditionReasonInvalidCredentialProxyResources, fmt.Sprintf(invalidCredentialProxyResourcesMsgFmt, refusal)
	}
	if refusal, _ := agentAPIAuthResourcesRefusal(agent); refusal != "" {
		return conditionReasonInvalidAgentAPIAuthResources, fmt.Sprintf(invalidAgentAPIAuthResourcesMsgFmt, refusal)
	}
	if refusal, _ := shellSandboxResourcesRefusal(agent); refusal != "" {
		return conditionReasonInvalidShellSandboxResources, fmt.Sprintf(invalidShellSandboxResourcesMsgFmt, refusal)
	}
	// The exec tier is not rendered and every command goes to the stateful
	// broker.
	if refusal := ValidateCredentialProxyExec(agent, nil); len(refusal) > 0 {
		return conditionReasonInvalidCredentialProxyExec, fmt.Sprintf(invalidExecTierReplicasMsgFmt, refusal[0].Detail)
	}
	return "", ""
}
