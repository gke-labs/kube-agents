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
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The SlackConnected condition: whether the A2A gateway's Slack Socket Mode
// connection is up, read from kubeagents_a2a_gateway_slack_connected on the
// gateway's metrics listener by UsageCounterPoller. Kept independent of the
// gateway readiness probe and PlatformAgent Ready so an external Slack outage
// does not take down the console, A2A doors, or gateway PodDisruptionBudget;
// when False while Slack is armed, updateStatusReady and parked
// updateStatusDegraded project it onto Degraded=True (Reason:
// SlackConnectionFailed). Transitioning to False requires
// usageScrapeFailureEventStreak consecutive down polls so routine Socket Mode
// reconnects and startup handshakes do not flap Degraded; recovery to True is
// immediate on the first poll reading 1.
const (
	slackConnectedConditionType          = "SlackConnected"
	slackConnectedReasonConnected        = "SocketModeConnected"
	slackConnectedReasonFailed           = "ConnectionFailed"
	conditionReasonSlackConnectionFailed = "SlackConnectionFailed"
	slackConnectedUpMessage              = "The A2A gateway's Slack Socket Mode connection is up."
	slackConnectedFailedMessage          = "The A2A gateway cannot maintain a Slack Socket Mode connection (kubeagents_a2a_gateway_slack_connected is 0). Check the A2A gateway pod logs for auth.test or Socket Mode errors, and verify bot/app tokens, HTTP_PROXY/HTTPS_PROXY, and proxy egress NetworkPolicy."
)

// usageSlackConnectedSample reduces this poll's scraped A2A gateway pods to a
// single max-aggregated Slack reading: ptr.To(true) when any reporting pod
// reads 1, ptr.To(false) when at least one pod reports 0 and none reports 1,
// or nil when no pod exported the gauge.
func usageSlackConnectedSample(scraped []usageScrapedPod) *bool {
	found := false
	up := false
	for _, s := range scraped {
		if s.SlackConnected == nil {
			continue
		}
		found = true
		if *s.SlackConnected {
			up = true
		}
	}
	if !found {
		return nil
	}
	return ptr.To(up)
}

// noteSlackConnected updates the poller's consecutive miss and down streaks
// for agent from this poll's aggregated Slack gauge sample. Disabling Slack
// or reading 1 clears both streaks; reading nil increments misses and clears
// down; reading 0 clears misses and increments down.
func (p *UsageCounterPoller) noteSlackConnected(agent *agentv1alpha1.PlatformAgent, sample *bool) (misses, down int) {
	p.mu.Lock()
	defer p.mu.Unlock()
	key := usagePollKey(agent)
	if !a2aSlackArmed(agent) {
		delete(p.slackConnectedMisses, key)
		delete(p.slackConnectedDown, key)
		return 0, 0
	}
	if sample == nil {
		delete(p.slackConnectedDown, key)
		if p.slackConnectedMisses == nil {
			p.slackConnectedMisses = map[string]int{}
		}
		p.slackConnectedMisses[key]++
		return p.slackConnectedMisses[key], 0
	}
	delete(p.slackConnectedMisses, key)
	if *sample {
		delete(p.slackConnectedDown, key)
		return 0, 0
	}
	if p.slackConnectedDown == nil {
		p.slackConnectedDown = map[string]int{}
	}
	p.slackConnectedDown[key]++
	return 0, p.slackConnectedDown[key]
}

// usageSlackConnectedForPoll decides the SlackConnected status projection for
// this poll. known=false holds the previous SlackConnected condition across a
// single missed scrape, restart, or transient Socket Mode reconnect (a single
// poll reading 0); known=true with a nil reading removes it (Slack unarmed, or
// usageScrapeFailureEventStreak consecutive polls with no pod exporting the
// gauge); known=true with ptr.To(true) sets True immediately on the first poll
// reading 1; known=true with ptr.To(false) sets False once
// usageScrapeFailureEventStreak consecutive polls read 0.
func usageSlackConnectedForPoll(agent *agentv1alpha1.PlatformAgent, sample *bool, misses, down int) (connected *bool, known bool) {
	if !a2aSlackArmed(agent) {
		return nil, true
	}
	if sample == nil {
		return nil, misses >= usageScrapeFailureEventStreak
	}
	if *sample {
		return ptr.To(true), true
	}
	if down < usageScrapeFailureEventStreak {
		return nil, false
	}
	return ptr.To(false), true
}

// slackConnectedConditionFor returns the SlackConnected condition for a known
// reading, or nil when Slack is unarmed or the condition should be removed.
func slackConnectedConditionFor(agent *agentv1alpha1.PlatformAgent, connected *bool) *metav1.Condition {
	if !a2aSlackArmed(agent) || connected == nil {
		return nil
	}
	if *connected {
		return &metav1.Condition{
			Type:               slackConnectedConditionType,
			Status:             metav1.ConditionTrue,
			Reason:             slackConnectedReasonConnected,
			ObservedGeneration: agent.Generation,
			Message:            slackConnectedUpMessage,
		}
	}
	return &metav1.Condition{
		Type:               slackConnectedConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             slackConnectedReasonFailed,
		ObservedGeneration: agent.Generation,
		Message:            slackConnectedFailedMessage,
	}
}

// slackConnectionFailedCondition returns SlackConnected while Slack is armed
// and the condition reads False, for Degraded; nil otherwise.
func slackConnectionFailedCondition(agent *agentv1alpha1.PlatformAgent) *metav1.Condition {
	if !a2aSlackArmed(agent) {
		return nil
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, slackConnectedConditionType)
	if cond == nil || cond.Status != metav1.ConditionFalse {
		return nil
	}
	return cond
}

// projectSlackConnected writes this poll's SlackConnected condition to the CR
// using an optimistic-lock status patch, leaving conflicts to the next poll.
func (p *UsageCounterPoller) projectSlackConnected(ctx context.Context, agent *agentv1alpha1.PlatformAgent, connected *bool, known bool) error {
	if !known && a2aSlackArmed(agent) {
		return nil
	}
	log := logf.FromContext(ctx).WithName(usagePollerLogName).WithValues("platformagent", client.ObjectKeyFromObject(agent).String())
	base := agent.DeepCopy()
	want := slackConnectedConditionFor(agent, connected)
	existing := meta.FindStatusCondition(agent.Status.Conditions, slackConnectedConditionType)
	switch {
	case want == nil:
		if !meta.RemoveStatusCondition(&agent.Status.Conditions, slackConnectedConditionType) {
			return nil
		}
	case existing == nil || existing.Status != want.Status || existing.Reason != want.Reason || existing.Message != want.Message:
		meta.SetStatusCondition(&agent.Status.Conditions, *want)
	default:
		return nil
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		if apierrors.IsConflict(err) {
			log.Info("the CR changed under the SlackConnected condition write; it is retried next poll")
			return nil
		}
		return fmt.Errorf("patching the %s condition: %w", slackConnectedConditionType, err)
	}
	return nil
}
