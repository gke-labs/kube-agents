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
	"maps"
	"slices"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The ForgeConnected condition, from the broker's report of the forges the
// install's managed repositories live on, which the usage poller reads off the
// stateful broker's metrics listener. The broker's readings sweep attempts
// every managed repository on a timer, the GitOps repository among them, and
// reports each provider's repositories by how the attempt left the connection.
// Without the condition a credential that expired, or a GitHub App that lost
// its installation, shows only when a task fails. The condition does not touch
// Ready, since the agent still answers.
const (
	forgeConnectedCondition = "ForgeConnected"

	// forgeReasonRepositoriesAnswer is the True reason: every managed
	// repository of every provider answered the last sweep.
	forgeReasonRepositoriesAnswer = "RepositoriesAnswer"
	// The False reasons, one a connection state: the worst state any
	// repository is in, in vcsConnectionStates' order.
	forgeReasonUnauthorized    = "Unauthorized"
	forgeReasonNotFound        = "NotFound"
	forgeReasonUnreachable     = "Unreachable"
	forgeReasonIdentityUnknown = "IdentityUnknown"
	forgeReasonRateLimited     = "RateLimited"
	forgeReasonFailed          = "Failed"
	// forgeReasonSweepStale is the Unknown reason: no provider is known to
	// fail, and at least one was last attempted over an hour ago, so the
	// broker serves its age alone.
	forgeReasonSweepStale = "SweepStale"

	// forgeProviderOther is the provider the broker reports a managed
	// repository under when it names no forge the image ships; a provider
	// outside the operator's registry reads as this too.
	forgeProviderOther = "other"
	// forgeHostsMax bounds the declared hosts a message quotes for one
	// provider, and forgeLabelMax each of them: they are text from the spec.
	forgeHostsMax = 3
	forgeLabelMax = 128

	// The message's text. A forge is named "provider (host, host, 2 more)";
	// a failing one reads "1 of 3 managed repositories unauthorized", or with
	// several states "3 of 4 managed repositories do not answer (2
	// unauthorized, 1 unreachable)".
	forgeLabelFormat         = "%s (%s)"
	forgeMoreHostsFormat     = "%d more"
	forgeOneRepository       = "1 managed repository"
	forgeRepositoriesFormat  = "%d managed repositories"
	forgeStateCountFormat    = "%d %s"
	forgeOneStateFormat      = "%d of %s %s"
	forgeSeveralStatesFormat = "%d of %s do not answer (%s)"
	// forgeNoKnownStateClause is for a provider the broker says is not
	// connected while naming no failing state the operator reads.
	forgeNoKnownStateClause = "a managed repository does not answer"
	forgeFailingFormat      = "%s: %s"
	forgeAnsweringFormat    = "%s: %s %s"
	forgeAnswerVerb         = "answer"
	forgeAnswersVerb        = "answers"
	forgeStaleClause        = ": last attempted over an hour ago. "
	forgeProviderSeparator  = "; "
	forgeListSeparator      = ", "
	forgeSentenceEnd        = "."
	forgeSentenceBreak      = ". "

	forgeNotConnectedHint = "The agent cannot read those repositories, so its proposals and their outcomes are not seen. Check the forge's credential, and for a GitHub App its installation on the repository; the broker's log names each repository under \"proposal readings\"."
	forgeStaleHint        = "The broker's readings sweep has not attempted it since; a repository whose credential cannot name its account is not attempted again until the broker restarts."
)

// forgeReasons is the False reason for each connection state.
var forgeReasons = map[string]string{
	vcsConnectionUnauthorized: forgeReasonUnauthorized,
	vcsConnectionNotFound:     forgeReasonNotFound,
	vcsConnectionUnreachable:  forgeReasonUnreachable,
	vcsConnectionIdentity:     forgeReasonIdentityUnknown,
	vcsConnectionRateLimited:  forgeReasonRateLimited,
	vcsConnectionOther:        forgeReasonFailed,
}

// forgeStateWords is each failing connection state as a message says it.
var forgeStateWords = map[string]string{
	vcsConnectionUnauthorized: "unauthorized",
	vcsConnectionNotFound:     "not found",
	vcsConnectionUnreachable:  "unreachable",
	vcsConnectionIdentity:     "with a credential that cannot name its account",
	vcsConnectionRateLimited:  "rate limited",
	vcsConnectionOther:        "failing",
}

// usageForgesForPoll is the forge reading this poll projects: the stateful
// broker's, or nil when no such body was read this poll, which leaves the
// condition as it stands. The broker is one pod by design, so there is at most
// one.
func usageForgesForPoll(scraped []usageScrapedPod) *usageForgeReading {
	for _, s := range scraped {
		if s.Forges != nil {
			return s.Forges
		}
	}
	return nil
}

// forgeProviderName is the provider a reading names, held to the providers
// the operator knows: the label is a peer's text.
func forgeProviderName(provider string) string {
	if slices.Contains(agentv1alpha1.GitProviderNames(), provider) {
		return provider
	}
	return forgeProviderOther
}

// forgeHosts is the hosts the spec declares for each provider, as the broker
// is configured with them; the install's one GitHub host when the declaration
// hands the broker no list (a github.com-only install). The broker reports by
// provider alone, so a message names the hosts from here.
func forgeHosts(agent *agentv1alpha1.PlatformAgent) map[string][]string {
	hosts := map[string][]string{}
	for _, forge := range brokerForges(agent) {
		if forge.Host != "" && !slices.Contains(hosts[forge.Provider], forge.Host) {
			hosts[forge.Provider] = append(hosts[forge.Provider], forge.Host)
		}
	}
	if len(hosts) > 0 || agent.Spec.Integration == nil {
		return hosts
	}
	if resolved, err := agent.Spec.Integration.ResolveGit(); err == nil && resolved != nil {
		if host := resolved.GitHubHost(); host != "" {
			hosts[agentv1alpha1.GitProviderGitHub] = []string{host}
		}
	}
	return hosts
}

// forgeLabel is a provider with its declared hosts, as a message names it:
// "gitlab (gitlab.com, gitlab.example.com)", or the provider alone when the
// spec declares no host for it.
func forgeLabel(provider string, hosts map[string][]string) string {
	declared := hosts[provider]
	if len(declared) == 0 {
		return provider
	}
	shown := make([]string, 0, forgeHostsMax+1)
	for i, host := range declared {
		if i == forgeHostsMax {
			shown = append(shown, fmt.Sprintf(forgeMoreHostsFormat, len(declared)-forgeHostsMax))
			break
		}
		shown = append(shown, truncateForCondition(host, forgeLabelMax))
	}
	return fmt.Sprintf(forgeLabelFormat, provider, strings.Join(shown, forgeListSeparator))
}

func forgeRepositories(n int64) string {
	if n == 1 {
		return forgeOneRepository
	}
	return fmt.Sprintf(forgeRepositoriesFormat, n)
}

// forgeProviderSummary is one provider's state, merged over the reading's
// entries that name it: an entry a newer broker labels with a provider the
// operator does not know joins the "other" one.
type forgeProviderSummary struct {
	provider     string
	connected    bool
	hasState     bool
	repositories map[string]int64
}

func forgeSummaries(reading *usageForgeReading) []forgeProviderSummary {
	byProvider := map[string]*forgeProviderSummary{}
	for _, forge := range reading.Forges {
		name := forgeProviderName(forge.Provider)
		summary := byProvider[name]
		if summary == nil {
			summary = &forgeProviderSummary{provider: name, connected: true, repositories: map[string]int64{}}
			byProvider[name] = summary
		}
		if !forge.HasState {
			continue
		}
		summary.hasState = true
		summary.connected = summary.connected && forge.Connected
		for state, count := range forge.Repositories {
			summary.repositories[state] += count
		}
	}
	out := make([]forgeProviderSummary, 0, len(byProvider))
	for _, name := range slices.Sorted(maps.Keys(byProvider)) {
		out = append(out, *byProvider[name])
	}
	return out
}

// forgeFailing is the message clause for a provider that is not connected, and
// its worst state: "1 of 3 managed repositories unauthorized", with every
// failing state named when there are several.
func forgeFailing(summary forgeProviderSummary) (clause, worst string) {
	var total, failing int64
	for _, count := range summary.repositories {
		total += count
	}
	parts := []string{}
	for _, state := range vcsConnectionStates {
		count := summary.repositories[state]
		if state == vcsConnectionOk || count == 0 {
			continue
		}
		if worst == "" {
			worst = state
		}
		failing += count
		parts = append(parts, fmt.Sprintf(forgeStateCountFormat, count, forgeStateWords[state]))
	}
	if worst == "" {
		// The broker says not connected and names no failing state: a newer
		// broker's state the operator does not read.
		return forgeNoKnownStateClause, vcsConnectionOther
	}
	if len(parts) == 1 {
		return fmt.Sprintf(forgeOneStateFormat, failing, forgeRepositories(total), forgeStateWords[worst]), worst
	}
	return fmt.Sprintf(forgeSeveralStatesFormat, failing, forgeRepositories(total), strings.Join(parts, forgeListSeparator)), worst
}

// forgeCondition is the condition a reading calls for. False when a provider
// is not connected, with the worst state as the reason; Unknown when none is
// known to fail and one was last attempted over an hour ago; True when every
// provider's repositories answered. nil for a reading that names no provider:
// the caller removes the condition, since there is no forge to report.
func forgeCondition(agent *agentv1alpha1.PlatformAgent, reading *usageForgeReading) *metav1.Condition {
	summaries := forgeSummaries(reading)
	if len(summaries) == 0 {
		return nil
	}
	hosts := forgeHosts(agent)
	cond := &metav1.Condition{Type: forgeConnectedCondition, ObservedGeneration: agent.Generation}
	var failing, stale, answering []string
	worst := ""
	for _, summary := range summaries {
		label := forgeLabel(summary.provider, hosts)
		switch {
		case !summary.hasState:
			stale = append(stale, label)
		case !summary.connected:
			clause, state := forgeFailing(summary)
			failing = append(failing, fmt.Sprintf(forgeFailingFormat, label, clause))
			if worst == "" || slices.Index(vcsConnectionStates, state) < slices.Index(vcsConnectionStates, worst) {
				worst = state
			}
		default:
			count := summary.repositories[vcsConnectionOk]
			verb := forgeAnswerVerb
			if count == 1 {
				verb = forgeAnswersVerb
			}
			answering = append(answering, fmt.Sprintf(forgeAnsweringFormat, label, forgeRepositories(count), verb))
		}
	}
	switch {
	case len(failing) > 0:
		cond.Status = metav1.ConditionFalse
		cond.Reason = forgeReasons[worst]
		cond.Message = strings.Join(failing, forgeProviderSeparator) + forgeSentenceBreak + forgeNotConnectedHint
	case len(stale) > 0:
		cond.Status = metav1.ConditionUnknown
		cond.Reason = forgeReasonSweepStale
		cond.Message = strings.Join(stale, forgeListSeparator) + forgeStaleClause + forgeStaleHint
	default:
		cond.Status = metav1.ConditionTrue
		cond.Reason = forgeReasonRepositoriesAnswer
		cond.Message = strings.Join(answering, forgeProviderSeparator) + forgeSentenceEnd
	}
	return cond
}

// projectForgeConnection writes the condition when its status, its reason or
// its message changed, as the kanban dispatcher's is written, and removes it
// when the broker reports no provider. The message holds counts of
// repositories and no time, so a forge that stays in one state is written
// once. The patch carries the resource version, because it replaces the
// conditions list whole; a conflict is left for the next poll.
func (p *UsageCounterPoller) projectForgeConnection(ctx context.Context, agent *agentv1alpha1.PlatformAgent, reading *usageForgeReading) error {
	if reading == nil {
		return nil
	}
	want := forgeCondition(agent, reading)
	existing := meta.FindStatusCondition(agent.Status.Conditions, forgeConnectedCondition)
	if want == nil && existing == nil {
		return nil
	}
	if want != nil && existing != nil &&
		existing.Status == want.Status && existing.Reason == want.Reason && existing.Message == want.Message {
		return nil
	}
	base := agent.DeepCopy()
	if want == nil {
		meta.RemoveStatusCondition(&agent.Status.Conditions, forgeConnectedCondition)
	} else {
		meta.SetStatusCondition(&agent.Status.Conditions, *want)
	}
	if err := p.r.Status().Patch(ctx, agent, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		if apierrors.IsConflict(err) {
			logf.FromContext(ctx).WithName(usagePollerLogName).Info("the CR changed under the forge connection condition write; it is retried next poll",
				"platformagent", client.ObjectKeyFromObject(agent).String())
			return nil
		}
		return fmt.Errorf("patching the %s condition: %w", forgeConnectedCondition, err)
	}
	return nil
}
