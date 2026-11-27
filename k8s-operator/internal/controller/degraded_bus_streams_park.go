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

// busStreamsDegradedEdit is what a parked pass does to Degraded for the
// poller-owned conditions (BusStreamsPresent and SlackConnected).
type busStreamsDegradedEdit int

const (
	// busStreamsDegradedKeep leaves Degraded as it stands.
	busStreamsDegradedKeep busStreamsDegradedEdit = iota
	// busStreamsDegradedSet writes the condition parkBusStreamsDegraded
	// returns: True/A2ABusStreamsMissing, True/SlackConnectionFailed, or a
	// spec refusal the poller reason outranked once that reason goes.
	busStreamsDegradedSet
	// busStreamsDegradedClear removes a Degraded/A2ABusStreamsMissing or
	// Degraded/SlackConnectionFailed.
	busStreamsDegradedClear
)

// parkBusStreamsDegraded is the Degraded edit a pass parked in
// updateStatusDegraded makes for the poller-owned conditions
// (BusStreamsPresent and SlackConnected), and the condition to write for
// busStreamsDegradedSet (#5707).
//
// Degraded's reason is otherwise ranked only by updateStatusReady, which a
// park returns before. Both pollers keep writing their conditions through a
// park (a failed provision Job or missing sandbox keys can each last days),
// and every write wakes a pass through the unfiltered PlatformAgent watch, so
// the parked pass is where Degraded follows them. It does what
// updateStatusReady would do for A2ABusStreamsMissing and
// SlackConnectionFailed and leaves every other reason alone:
//
//   - streams missing (or Slack disconnected while streams are present):
//     Degraded reads the highest active poller reason with its condition's
//     message, unless Degraded already holds a non-cleared reason
//     updateStatusReady ranks above it (degradedRanksAbove).
//   - neither poller condition active: a Degraded/A2ABusStreamsMissing or
//     Degraded/SlackConnectionFailed gives way to the spec refusal it
//     outranked, if one applies (specResourceDegraded), and is removed
//     otherwise, as updateStatusReady removes a Degraded no reason holds. Any
//     other reason stays; RBACIncomplete in particular is reportRBACSkew's to
//     clear.
func parkBusStreamsDegraded(agent *agentv1alpha1.PlatformAgent) (busStreamsDegradedEdit, *metav1.Condition) {
	degraded := meta.FindStatusCondition(agent.Status.Conditions, degradedConditionType)
	streams := busStreamsMissingCondition(agent)
	slackFailed := slackConnectionFailedCondition(agent)

	var wantReason, wantMsg string
	switch {
	case streams != nil:
		wantReason = conditionReasonA2ABusStreamsMissing
		wantMsg = streams.Message
	case slackFailed != nil:
		wantReason = conditionReasonSlackConnectionFailed
		wantMsg = slackFailed.Message
	}

	if wantReason == "" {
		if degraded == nil ||
			(degraded.Reason != conditionReasonA2ABusStreamsMissing && degraded.Reason != conditionReasonSlackConnectionFailed) {
			return busStreamsDegradedKeep, nil
		}
		// The poller reason goes. A spec refusal it outranked comes back, as
		// updateStatusReady would show it; it is the one lower reason a park
		// can read from the CR alone. A2AGatewayWaitingForTasks reads the
		// gateway's pods and Lease, so it is the unparked pass's to put back,
		// like the reasons ranked above the streams.
		if reason, msg := specResourceDegraded(agent); reason != "" {
			return busStreamsDegradedSet, &metav1.Condition{
				Type: degradedConditionType, Status: metav1.ConditionTrue,
				Reason: reason, Message: msg, ObservedGeneration: agent.Generation,
			}
		}
		return busStreamsDegradedClear, nil
	}
	if degraded != nil && degraded.Status == metav1.ConditionTrue {
		// An already-cleared A2ABusStreamsMissing does not outrank
		// SlackConnectionFailed when streams recover while Slack stays down.
		if degraded.Reason != conditionReasonA2ABusStreamsMissing && degradedRanksAbove(degraded.Reason, wantReason) {
			return busStreamsDegradedKeep, nil
		}
		// Current when the reason and message match, as updateStatusReady
		// compares them (degradedUnchanged), and when it carries this
		// generation: updateStatusReady stamps the generation on every write,
		// and a park that wrote Ready for a spec edit must not leave Degraded
		// a generation behind. One write brings both level, so this does not
		// write on every tick.
		if degraded.Reason == wantReason && degraded.Message == wantMsg &&
			degraded.ObservedGeneration == agent.Generation {
			return busStreamsDegradedKeep, nil
		}
	}
	return busStreamsDegradedSet, &metav1.Condition{
		Type:               degradedConditionType,
		Status:             metav1.ConditionTrue,
		Reason:             wantReason,
		Message:            wantMsg,
		ObservedGeneration: agent.Generation,
	}
}

// degradedRanksAbove reports whether updateStatusReady ranks candidate above
// target in degradedConditionReasons.
func degradedRanksAbove(candidate, target string) bool {
	at := slices.Index(degradedConditionReasons, target)
	return slices.Contains(degradedConditionReasons[:max(at, 0)], candidate)
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
// resource and exec checks run workloads at the operator's defaults, and
// InvalidKanbanAdmission keeps rendering the resolved kanban.admission block,
// so Ready and the phase keep what the workload says. A parked pass reads them
// too (parkBusStreamsDegraded): they are what Degraded falls back to when a
// poller-owned reason (A2ABusStreamsMissing or SlackConnectionFailed) goes.
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
	if refusal := kanbanAdmissionRefusal(agent); refusal != "" {
		return conditionReasonInvalidKanbanAdmission, fmt.Sprintf(kanbanAdmissionDegradedMsgFmt, refusal)
	}
	return "", ""
}
