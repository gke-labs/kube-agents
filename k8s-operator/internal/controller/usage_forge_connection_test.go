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
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func forgeConditionOf(h *usageHarness) *metav1.Condition {
	return meta.FindStatusCondition(h.agent().Status.Conditions, forgeConnectedCondition)
}

func forgeHarness(t *testing.T, agent *agentv1alpha1.PlatformAgent) *usageHarness {
	t.Helper()
	if agent == nil {
		agent = usageTestAgent(usageClock(0).Add(-time.Hour))
	}
	return dataVolumeHarness(t, agent)
}

// githubAliasAgent declares the install's GitOps repository through the
// deprecated github alias, as most installs do.
func githubAliasAgent() *agentv1alpha1.PlatformAgent {
	agent := usageTestAgent(usageClock(0).Add(-time.Hour))
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		GitHub: &agentv1alpha1.GitHubSpec{Org: "acme", GitRepo: "acme/infra"},
	}
	return agent
}

func connectedForge(provider string, ok int64) usageForge {
	return usageForge{Provider: provider, Connected: true, HasState: true, Repositories: map[string]int64{vcsConnectionOk: ok}}
}

func failingForge(provider string, repositories map[string]int64) usageForge {
	return usageForge{Provider: provider, HasState: true, Repositories: repositories}
}

func staleForge(provider string) usageForge {
	return usageForge{Provider: provider}
}

// The broker's body, as it renders the three families beside the counter the
// poller knows the stateful broker by.
func forgeBody(series ...string) string {
	return strings.Join(append([]string{
		"# TYPE kubeagents_vcs_requests_total counter",
		`kubeagents_vcs_requests_total{verb="proposal-create",status="success",provider="github"} 2`,
		"# TYPE kubeagents_vcs_forge_connected gauge",
	}, append(series, "")...), "\n")
}

func scrapeForges(t *testing.T, body string, counters []string) (usageReading, error) {
	t.Helper()
	return newPodUsageSource().Scrape(context.Background(), serveBody(t, http.StatusOK, body), usageMetricsPath, counters, nil)
}

func TestPodUsageSource_ReadsTheBrokersForges(t *testing.T) {
	reading, err := scrapeForges(t, forgeBody(
		`kubeagents_vcs_forge_connected{provider="github"} 1`,
		`kubeagents_vcs_forge_connected{provider="gitlab"} 0`,
		"# TYPE kubeagents_vcs_forge_repositories gauge",
		`kubeagents_vcs_forge_repositories{provider="github",state="ok"} 2`,
		`kubeagents_vcs_forge_repositories{provider="github",state="unauthorized"} 0`,
		`kubeagents_vcs_forge_repositories{provider="gitlab",state="ok"} 1`,
		`kubeagents_vcs_forge_repositories{provider="gitlab",state="unauthorized"} 1`,
		`kubeagents_vcs_forge_repositories{provider="gitlab",state="unreachable"} 2`,
		`kubeagents_vcs_forge_repositories{provider="gitlab",state="a_newer_state"} 5`,
		"# TYPE kubeagents_vcs_forge_checked_age_seconds gauge",
		`kubeagents_vcs_forge_checked_age_seconds{provider="github"} 59.046`,
		`kubeagents_vcs_forge_checked_age_seconds{provider="gitlab"} 12.500`,
		`kubeagents_vcs_forge_checked_age_seconds{provider="gitea"} 4100.000`,
	), usageBrokerCounters)
	if err != nil {
		t.Fatal(err)
	}
	want := &usageForgeReading{Forges: []usageForge{
		{Provider: "gitea"},
		{Provider: "github", Connected: true, HasState: true, Repositories: map[string]int64{vcsConnectionOk: 2}},
		{Provider: "gitlab", HasState: true,
			Repositories: map[string]int64{vcsConnectionOk: 1, vcsConnectionUnauthorized: 1, vcsConnectionUnreachable: 2}},
	}}
	if !reflect.DeepEqual(reading.Forges, want) {
		t.Fatalf("forges = %+v, want %+v", reading.Forges, want)
	}
}

// A broker that names no provider (no managed repository, no sweep yet, or one
// that predates the series) is an empty reading, not a missing one; a body the
// poller does not know as the stateful broker's carries none.
func TestPodUsageSource_ABrokerWithNoForgeIsAnEmptyReadingAndAnotherBodyHasNone(t *testing.T) {
	reading, err := scrapeForges(t, forgeBody(), usageBrokerCounters)
	if err != nil {
		t.Fatal(err)
	}
	if reading.Forges == nil || len(reading.Forges.Forges) != 0 {
		t.Fatalf("forges = %+v, want an empty reading", reading.Forges)
	}
	body := forgeBody(`kubeagents_vcs_forge_connected{provider="github"} 1`)
	for name, counters := range map[string][]string{"the exec tier": usageExecTierCounters, "the chat listener": usageChatCounters} {
		reading, err := scrapeForges(t, body, counters)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		if reading.Forges != nil {
			t.Fatalf("%s: forges = %+v, want none", name, reading.Forges)
		}
	}
}

func TestPodUsageSource_AForgeBodyPastItsBoundsFailsTheScrape(t *testing.T) {
	many := []string{}
	for i := range usageForgeProviderCeiling + 1 {
		many = append(many, fmt.Sprintf(`kubeagents_vcs_forge_connected{provider="p%d"} 1`, i))
	}
	for name, series := range map[string][]string{
		"more providers than the ceiling": many,
		"a negative sample":               {`kubeagents_vcs_forge_connected{provider="github"} -1`},
		"an age past a duration": {
			"# TYPE kubeagents_vcs_forge_checked_age_seconds gauge",
			`kubeagents_vcs_forge_checked_age_seconds{provider="github"} 1e30`,
		},
	} {
		_, err := scrapeForges(t, forgeBody(series...), usageBrokerCounters)
		var scrapeErr *usageScrapeError
		if !errors.As(err, &scrapeErr) || scrapeErr.Kind != usageScrapeKindSample {
			t.Errorf("%s: err = %v, want a sample error", name, err)
		}
	}
}

func TestForgeCondition_EachStateHasItsReason(t *testing.T) {
	agent := githubAliasAgent()
	for state, reason := range map[string]string{
		vcsConnectionUnauthorized: forgeReasonUnauthorized,
		vcsConnectionNotFound:     forgeReasonNotFound,
		vcsConnectionUnreachable:  forgeReasonUnreachable,
		vcsConnectionIdentity:     forgeReasonIdentityUnknown,
		vcsConnectionRateLimited:  forgeReasonRateLimited,
		vcsConnectionOther:        forgeReasonFailed,
	} {
		cond := forgeCondition(agent, &usageForgeReading{Forges: []usageForge{
			failingForge("github", map[string]int64{vcsConnectionOk: 2, state: 1}),
		}})
		if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != reason {
			t.Errorf("%s: condition %+v, want False with reason %s", state, cond, reason)
			continue
		}
		if !strings.HasPrefix(cond.Message, "github (github.com): 1 of 3 managed repositories "+forgeStateWords[state]+". ") {
			t.Errorf("%s: message %q", state, cond.Message)
		}
	}
	if len(forgeReasons) != len(vcsConnectionStates)-1 || len(forgeStateWords) != len(vcsConnectionStates)-1 {
		t.Errorf("every failing state needs a reason and a word: %d reasons, %d words, %d states",
			len(forgeReasons), len(forgeStateWords), len(vcsConnectionStates))
	}
}

func TestForgeCondition_TheWorstStateAcrossProvidersIsTheReason(t *testing.T) {
	cond := forgeCondition(gitlabAgent("token"), &usageForgeReading{Forges: []usageForge{
		failingForge("github", map[string]int64{vcsConnectionRateLimited: 1}),
		failingForge("gitlab", map[string]int64{vcsConnectionOk: 1, vcsConnectionUnreachable: 1, vcsConnectionUnauthorized: 2}),
	}})
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != forgeReasonUnauthorized {
		t.Fatalf("condition %+v, want False with reason %s", cond, forgeReasonUnauthorized)
	}
	want := "github (github.com): 1 of 1 managed repository rate limited; " +
		"gitlab (gitlab.com): 3 of 4 managed repositories do not answer (2 unauthorized, 1 unreachable). "
	if !strings.HasPrefix(cond.Message, want) {
		t.Fatalf("message %q, want it to start %q", cond.Message, want)
	}
	if strings.Contains(cond.Message, "infra") || strings.Contains(cond.Message, "platform/tools") {
		t.Fatalf("message names a repository: %q", cond.Message)
	}
}

func TestForgeCondition_ConnectedNamesEachProviderAndItsHosts(t *testing.T) {
	agent := gitlabAgent("token")
	agent.Generation = 7
	cond := forgeCondition(agent, &usageForgeReading{Forges: []usageForge{
		connectedForge("github", 1), connectedForge("gitlab", 3),
	}})
	if cond == nil || cond.ObservedGeneration != agent.Generation {
		t.Fatalf("condition %+v, want observedGeneration %d", cond, agent.Generation)
	}
	want := "github (github.com): 1 managed repository answers; gitlab (gitlab.com): 3 managed repositories answer."
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != forgeReasonRepositoriesAnswer || cond.Message != want {
		t.Fatalf("condition %+v, want True %q", cond, want)
	}
}

// A provider whose check is over an hour old serves its age alone. With no
// provider known to fail the condition is Unknown; a failing one wins.
func TestForgeCondition_AStaleProviderIsUnknownUnlessAnotherFails(t *testing.T) {
	agent := gitlabAgent("token")
	cond := forgeCondition(agent, &usageForgeReading{Forges: []usageForge{connectedForge("github", 1), staleForge("gitlab")}})
	if cond == nil || cond.Status != metav1.ConditionUnknown || cond.Reason != forgeReasonSweepStale ||
		!strings.HasPrefix(cond.Message, "gitlab (gitlab.com): last attempted over an hour ago. ") {
		t.Fatalf("condition %+v, want Unknown SweepStale naming gitlab", cond)
	}
	cond = forgeCondition(agent, &usageForgeReading{Forges: []usageForge{
		failingForge("github", map[string]int64{vcsConnectionUnreachable: 1}), staleForge("gitlab"),
	}})
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != forgeReasonUnreachable {
		t.Fatalf("condition %+v, want False Unreachable", cond)
	}
}

// The provider label is a peer's text: one the operator does not know reads as
// other, and two such are one provider. A provider the spec declares no host
// for is named alone.
func TestForgeCondition_AnUnknownProviderIsOtherAndHasNoHost(t *testing.T) {
	cond := forgeCondition(githubAliasAgent(), &usageForgeReading{Forges: []usageForge{
		failingForge(`evil"; DROP`, map[string]int64{vcsConnectionOther: 1}),
		failingForge("another-unknown", map[string]int64{vcsConnectionOther: 2}),
	}})
	if cond == nil || cond.Reason != forgeReasonFailed || !strings.HasPrefix(cond.Message, "other: 3 of 3 managed repositories failing. ") {
		t.Fatalf("condition %+v, want other with 3 failing", cond)
	}
	if strings.Contains(cond.Message, "evil") || strings.Contains(cond.Message, "another-unknown") {
		t.Fatalf("message carries the label's text: %q", cond.Message)
	}
}

// A broker that says not connected and names no failing state the operator
// reads (a newer broker's state) is still not connected.
func TestForgeCondition_NotConnectedWithNoKnownStateIsFailed(t *testing.T) {
	cond := forgeCondition(githubAliasAgent(), &usageForgeReading{Forges: []usageForge{
		failingForge("github", map[string]int64{vcsConnectionOk: 1}),
	}})
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != forgeReasonFailed ||
		!strings.HasPrefix(cond.Message, "github (github.com): a managed repository does not answer. ") {
		t.Fatalf("condition %+v, want False Failed", cond)
	}
}

func TestForgeHosts_ComeFromTheSpecAndAreBounded(t *testing.T) {
	if got, want := forgeHosts(githubAliasAgent()), map[string][]string{"github": {"github.com"}}; !reflect.DeepEqual(got, want) {
		t.Errorf("the github alias: hosts %v, want %v", got, want)
	}
	if got := forgeHosts(usageTestAgent(usageClock(0))); len(got) != 0 {
		t.Errorf("no integration: hosts %v, want none", got)
	}
	got := forgeHosts(gitlabAgent("token"))
	if !reflect.DeepEqual(got, map[string][]string{"github": {"github.com"}, "gitlab": {"gitlab.com"}}) {
		t.Errorf("a gitlab forge: hosts %v", got)
	}
	long := strings.Repeat("h", 300) + ".example.com"
	label := forgeLabel("gitlab", map[string][]string{"gitlab": {"a.example.com", long, "c.example.com", "d.example.com", "e.example.com"}})
	if want := "gitlab (a.example.com, " + truncateForCondition(long, forgeLabelMax) + ", c.example.com, 2 more)"; label != want {
		t.Errorf("label %q, want %q: %d hosts, each cut to %d characters", label, want, forgeHostsMax, forgeLabelMax)
	}
	if cut := truncateForCondition(long, forgeLabelMax); len(cut) >= len(long) {
		t.Errorf("a %d-character host was not cut: %d characters", len(long), len(cut))
	}
	if label := forgeLabel("gitea", nil); label != "gitea" {
		t.Errorf("label %q, want the provider alone", label)
	}
}

// The condition follows the broker's report through the poller: set when a
// provider answers, False when a repository does not, True again when it does.
// Ready is never written, and a state that holds is written once.
func TestForgeConnection_ConditionFollowsTheBroker(t *testing.T) {
	h := forgeHarness(t, githubAliasAgent())
	h.poll(5)
	if cond := forgeConditionOf(h); cond != nil {
		t.Fatalf("before the broker serves the series: condition %+v, want none", cond)
	}
	h.stub.setForges(brokerAddr(), connectedForge("github", 1))
	h.poll(10)
	cond := forgeConditionOf(h)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != forgeReasonRepositoriesAnswer {
		t.Fatalf("a connected forge: condition %+v, want True", cond)
	}
	if cond.Message != "github (github.com): 1 managed repository answers." {
		t.Fatalf("message %q", cond.Message)
	}
	if cond.ObservedGeneration != h.agent().Generation {
		t.Fatalf("observedGeneration %d, want the CR's %d", cond.ObservedGeneration, h.agent().Generation)
	}
	// A state that holds is written once: the next poll, with nothing else
	// moving, patches nothing.
	version, patches := h.agent().ResourceVersion, h.patches
	h.poll(15)
	if h.patches != patches || h.agent().ResourceVersion != version {
		t.Fatalf("a state that holds was written again: %d patches after %d, resourceVersion %s after %s",
			h.patches, patches, h.agent().ResourceVersion, version)
	}
	h.stub.setForges(brokerAddr(), failingForge("github", map[string]int64{vcsConnectionOk: 1, vcsConnectionUnauthorized: 1}))
	h.poll(20)
	cond = forgeConditionOf(h)
	if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != forgeReasonUnauthorized {
		t.Fatalf("a failing repository: condition %+v, want False Unauthorized", cond)
	}
	h.stub.setForges(brokerAddr(), connectedForge("github", 1))
	h.poll(25)
	if cond = forgeConditionOf(h); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("after recovery: condition %+v, want True", cond)
	}
	if ready := meta.FindStatusCondition(h.agent().Status.Conditions, "Ready"); ready != nil {
		t.Fatalf("the poller wrote Ready: %+v", ready)
	}
}

// A poll that reads no broker body leaves the condition as it is: a broker
// that is down neither sets nor clears it.
func TestForgeConnection_AnUnreadBrokerLeavesTheCondition(t *testing.T) {
	h := forgeHarness(t, githubAliasAgent())
	h.stub.setForges(brokerAddr(), failingForge("github", map[string]int64{vcsConnectionUnreachable: 1}))
	h.poll(5)
	h.stub.fail(brokerAddr(), usageScrapeKindConnect)
	h.poll(10)
	if cond := forgeConditionOf(h); cond == nil || cond.Status != metav1.ConditionFalse {
		t.Fatalf("after an unread poll: condition %+v, want it still False", cond)
	}
}

// A broker that reports no provider has no forge to speak of: the condition
// goes, so an install whose last managed repository was removed does not keep
// a stale one.
func TestForgeConnection_NoProviderRemovesTheCondition(t *testing.T) {
	h := forgeHarness(t, githubAliasAgent())
	h.stub.setForges(brokerAddr(), connectedForge("github", 1))
	h.poll(5)
	if forgeConditionOf(h) == nil {
		t.Fatal("the condition was not set")
	}
	h.stub.setForges(brokerAddr())
	h.poll(10)
	if cond := forgeConditionOf(h); cond != nil {
		t.Fatalf("with no provider reported: condition %+v, want none", cond)
	}
	h.poll(15)
	if cond := forgeConditionOf(h); cond != nil {
		t.Fatalf("still no provider: condition %+v, want none", cond)
	}
}

// The patch replaces the conditions list whole, so it carries the resource
// version: a CR that changed since the poll read it refuses the write, which
// the next poll makes, and a condition another writer set meanwhile survives.
func TestForgeConnection_AWriteOverAChangedCRIsLeftForTheNextPoll(t *testing.T) {
	h := forgeHarness(t, githubAliasAgent())
	ctx := context.Background()
	read := h.agent()
	other := h.agent()
	meta.SetStatusCondition(&other.Status.Conditions, metav1.Condition{
		Type: "AnotherWriters", Status: metav1.ConditionTrue, Reason: "Set", Message: "set after the poll read the CR",
	})
	if err := h.cl.Status().Update(ctx, other); err != nil {
		t.Fatal(err)
	}
	reading := &usageForgeReading{Forges: []usageForge{connectedForge("github", 1)}}
	if err := h.p.projectForgeConnection(ctx, read, reading); err != nil {
		t.Fatalf("a conflict is not an error: %v", err)
	}
	if cond := forgeConditionOf(h); cond != nil {
		t.Fatalf("the write went over a changed CR: %+v", cond)
	}
	if meta.FindStatusCondition(h.agent().Status.Conditions, "AnotherWriters") == nil {
		t.Fatal("the other writer's condition was lost")
	}
	if err := h.p.projectForgeConnection(ctx, h.agent(), reading); err != nil {
		t.Fatal(err)
	}
	if cond := forgeConditionOf(h); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("the next write: condition %+v, want True", cond)
	}
	if meta.FindStatusCondition(h.agent().Status.Conditions, "AnotherWriters") == nil {
		t.Fatal("the other writer's condition was lost by the next write")
	}
}
