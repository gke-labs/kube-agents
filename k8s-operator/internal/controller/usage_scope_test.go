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
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The test agent's host triple is p/l/c (usageTestAgent). These are the
// neighbours the watcher registers beside it when the reconcile has adopted
// every cluster in project p.
var (
	scopeTestHost       = usageClusterRef{Name: "c", Project: "p", Location: "l"}
	scopeTestNeighbourA = usageClusterRef{Name: "analytics-dev", Project: "p", Location: "europe-west4"}
	scopeTestNeighbourB = usageClusterRef{Name: "batch-east", Project: "p", Location: "us-east1-b"}
)

// setClusterRefs gives addr's watcher reading the named cluster_up series,
// every one monitored, as the scrape would read them.
func (s *stubUsageSource) setClusterRefs(addr string, refs ...usageClusterRef) {
	s.mu.Lock()
	defer s.mu.Unlock()
	reading := s.readings[addr]
	reading.Clusters = &usageClusterGauges{Registered: int64(len(refs)), Monitored: int64(len(refs)), Refs: append([]usageClusterRef(nil), refs...)}
	s.readings[addr] = reading
}

func scopeHarness(t *testing.T, agent *agentv1alpha1.PlatformAgent, refs ...usageClusterRef) *usageHarness {
	t.Helper()
	h := dataVolumeHarness(t, agent)
	h.stub.setClusterRefs(gatewayAddr(), refs...)
	return h
}

func scopeCondition(t *testing.T, h *usageHarness) *metav1.Condition {
	t.Helper()
	return meta.FindStatusCondition(h.agent().Status.Conditions, scopeUndeclaredCondition)
}

// With no scope declared and the watcher registering clusters beyond the host,
// the condition is True and names them; once only the host is registered it is
// removed, as the reading no longer calls for it.
func TestScopeUndeclared_NamesTheClustersBeyondTheHost(t *testing.T) {
	h := scopeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)), scopeTestHost, scopeTestNeighbourB, scopeTestNeighbourA)
	h.poll(5)
	cond := scopeCondition(t, h)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != scopeUndeclaredReason {
		t.Fatalf("condition = %+v, want True/%s", cond, scopeUndeclaredReason)
	}
	for _, want := range []string{
		"2 cluster(s) beyond the host cluster c",
		"analytics-dev (p/europe-west4), batch-east (p/us-east1-b)",
		"management project p",
		scopeUndeclaredHint,
	} {
		if !strings.Contains(cond.Message, want) {
			t.Errorf("message %q lacks %q", cond.Message, want)
		}
	}
	if strings.Contains(cond.Message, "c (p/l)") {
		t.Errorf("message %q lists the host cluster as a neighbour", cond.Message)
	}
	if cond.ObservedGeneration != h.agent().Generation {
		t.Errorf("observedGeneration = %d, want %d", cond.ObservedGeneration, h.agent().Generation)
	}
	if meta.FindStatusCondition(h.agent().Status.Conditions, "Ready") != nil {
		t.Fatal("the poller wrote Ready")
	}

	h.stub.setClusterRefs(gatewayAddr(), scopeTestHost)
	h.poll(10)
	if again := scopeCondition(t, h); again != nil {
		t.Fatalf("condition after the neighbours left = %+v, want it removed", again)
	}
}

// A neighbour that appears while the condition holds rewrites the message, so
// the names on the CR are the clusters registered now; an unchanged set writes
// nothing.
func TestScopeUndeclared_MessageFollowsTheSet(t *testing.T) {
	h := scopeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)), scopeTestHost, scopeTestNeighbourA)
	h.poll(5)
	first := scopeCondition(t, h)
	if first == nil || strings.Contains(first.Message, "batch-east") {
		t.Fatalf("first condition = %+v", first)
	}
	before := h.patches
	h.poll(10)
	if h.patches != before {
		t.Fatalf("an unchanged set wrote %d patch(es)", h.patches-before)
	}
	h.stub.setClusterRefs(gatewayAddr(), scopeTestHost, scopeTestNeighbourA, scopeTestNeighbourB)
	h.poll(15)
	second := scopeCondition(t, h)
	if second == nil || !strings.Contains(second.Message, "batch-east (p/us-east1-b)") || !strings.Contains(second.Message, "2 cluster(s)") {
		t.Fatalf("condition after a neighbour joined = %+v", second)
	}
	if !second.LastTransitionTime.Equal(&first.LastTransitionTime) {
		t.Errorf("a message rewrite moved lastTransitionTime from %v to %v", first.LastTransitionTime, second.LastTransitionTime)
	}
}

// A declared scope, any membership list with an entry, says the operator chose
// the fleet, so nothing is written whatever the watcher registers. Exclusions
// alone are not a declaration: the customer's shape, a present block with only
// exclude.projects, still warns about the neighbours it leaves in.
func TestScopeUndeclared_DeclaredScopeWritesNothing(t *testing.T) {
	for name, scope := range map[string]*agentv1alpha1.ScopeSpec{
		"projects":                     {Projects: []string{"other"}},
		"folders":                      {Folders: []string{"123"}},
		"organizations":                {Organizations: []string{"456"}},
		"sharedVpcHosts":               {SharedVpcHosts: []string{"net-host"}},
		"metricsScopes":                {MetricsScopes: []string{"obs-hub"}},
		"the management project named": {Projects: []string{"p"}},
	} {
		t.Run(name, func(t *testing.T) {
			agent := usageTestAgent(usageClock(0).Add(-time.Hour))
			agent.Spec.Scope = scope
			h := scopeHarness(t, agent, scopeTestHost, scopeTestNeighbourA)
			h.poll(5)
			if cond := scopeCondition(t, h); cond != nil {
				t.Fatalf("condition = %+v, want none under a declared scope", cond)
			}
		})
	}
	agent := usageTestAgent(usageClock(0).Add(-time.Hour))
	agent.Spec.Scope = &agentv1alpha1.ScopeSpec{Exclude: &agentv1alpha1.ScopeExcludeSpec{Projects: []string{"*-sandbox"}}, MaxProjects: ptr.To(int32(100))}
	h := scopeHarness(t, agent, scopeTestHost, scopeTestNeighbourA)
	h.poll(5)
	if cond := scopeCondition(t, h); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("condition under a block with only exclusions = %+v, want True", cond)
	}
}

// Only the host registered, under either of the labellings the watcher gives
// it (its profile's full triple, or the bare name of the directly reached
// cluster before its profile is absorbed), writes nothing.
func TestScopeUndeclared_HostOnlyWritesNothing(t *testing.T) {
	for name, refs := range map[string][]usageClusterRef{
		"full triple":  {scopeTestHost},
		"direct entry": {{Name: "c"}},
		"both":         {{Name: "c"}, scopeTestHost},
		"none":         {},
	} {
		t.Run(name, func(t *testing.T) {
			h := scopeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)), refs...)
			h.poll(5)
			if cond := scopeCondition(t, h); cond != nil {
				t.Fatalf("condition = %+v, want none", cond)
			}
			if h.agent().Status.Usage.ClustersRegistered == nil {
				t.Fatal("the gauges were not written")
			}
		})
	}
}

// Without the host's name, location or project on the CR the operator cannot
// tell a neighbour from the host, so it says nothing rather than naming the
// host as adopted.
func TestScopeUndeclared_UnknownHostWritesNothing(t *testing.T) {
	for name, edit := range map[string]func(h *agentv1alpha1.HarnessSpec){
		"no cluster name":   func(h *agentv1alpha1.HarnessSpec) { h.ClusterName = "" },
		"no location":       func(h *agentv1alpha1.HarnessSpec) { h.Location = "" },
		"no project":        func(h *agentv1alpha1.HarnessSpec) { h.ProjectID = "" },
		"no harness at all": nil,
	} {
		t.Run(name, func(t *testing.T) {
			agent := usageTestAgent(usageClock(0).Add(-time.Hour))
			if edit == nil {
				agent.Spec.Harness = nil
			} else {
				edit(agent.Spec.Harness)
			}
			h := scopeHarness(t, agent, scopeTestHost, scopeTestNeighbourA)
			h.poll(5)
			if cond := scopeCondition(t, h); cond != nil {
				t.Fatalf("condition = %+v, want none", cond)
			}
		})
	}
}

// A registered cluster in another project is a profile somebody made by hand
// (or one a dropped declaration still carries), not one the management project
// adopted, so it is neither counted nor named; the management project's own
// neighbours still are.
func TestScopeUndeclared_IgnoresClustersInOtherProjects(t *testing.T) {
	foreign := usageClusterRef{Name: "payments-prod", Project: "payments", Location: "us-central1"}
	h := scopeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)), scopeTestHost, foreign)
	h.poll(5)
	if cond := scopeCondition(t, h); cond != nil {
		t.Fatalf("condition = %+v, want none for a foreign-project profile alone", cond)
	}
	h.stub.setClusterRefs(gatewayAddr(), scopeTestHost, foreign, scopeTestNeighbourA)
	h.poll(10)
	cond := scopeCondition(t, h)
	if cond == nil || !strings.Contains(cond.Message, "1 cluster(s)") || strings.Contains(cond.Message, "payments-prod") {
		t.Fatalf("condition = %+v, want the management project's neighbour alone", cond)
	}
}

// A harness that names its project by number, which nothing refuses, is matched
// through the host's own series, which carries the ID the reconcile resolved;
// until that series exists (the host still watched through its bare direct
// entry) the operator says nothing rather than naming the host as adopted.
func TestScopeUndeclared_ProjectNumberIsResolvedThroughTheHostsSeries(t *testing.T) {
	agent := usageTestAgent(usageClock(0).Add(-time.Hour))
	agent.Spec.Harness.ProjectID = "123456789012"
	h := scopeHarness(t, agent, usageClusterRef{Name: "c"}, scopeTestNeighbourA)
	h.poll(5)
	if cond := scopeCondition(t, h); cond != nil {
		t.Fatalf("condition before the host's series carries a project = %+v, want none", cond)
	}
	h.stub.setClusterRefs(gatewayAddr(), scopeTestHost, scopeTestNeighbourA)
	h.poll(10)
	cond := scopeCondition(t, h)
	if cond == nil || !strings.Contains(cond.Message, "1 cluster(s) beyond the host cluster c") || !strings.Contains(cond.Message, "analytics-dev") || strings.Contains(cond.Message, "c (p/l)") {
		t.Fatalf("condition = %+v, want the neighbour alone, named under the resolved project", cond)
	}
}

func TestScopeUndeclaredRemovalCause(t *testing.T) {
	agent := usageTestAgent(usageClock(0))
	if got := scopeUndeclaredRemovalCause(agent, nil); !strings.Contains(got, "no current reading") {
		t.Errorf("no reading: %q", got)
	}
	if got := scopeUndeclaredRemovalCause(agent, []usageClusterRef{scopeTestHost}); !strings.Contains(got, "no cluster beyond the host") {
		t.Errorf("host only: %q", got)
	}
	declared := usageTestAgent(usageClock(0))
	declared.Spec.Scope = &agentv1alpha1.ScopeSpec{Projects: []string{"p"}}
	if got := scopeUndeclaredRemovalCause(declared, []usageClusterRef{scopeTestHost, scopeTestNeighbourA}); !strings.Contains(got, "declares a membership") {
		t.Errorf("declared: %q", got)
	}
	numeric := usageTestAgent(usageClock(0))
	numeric.Spec.Harness.ProjectID = "123456789012"
	if got := scopeUndeclaredRemovalCause(numeric, []usageClusterRef{{Name: "c"}}); !strings.Contains(got, "not known") {
		t.Errorf("numeric project, host unresolved: %q", got)
	}
	if got := scopeUndeclaredRemovalCause(numeric, []usageClusterRef{scopeTestHost}); !strings.Contains(got, "no cluster beyond the host") {
		t.Errorf("numeric project, host resolved: %q", got)
	}
}

// The condition follows the reading on the cluster gauges' terms: held through
// one poll that reads no gateway replica, removed by the second, and removed
// when the watcher is off, so a held condition never outlives what the
// operator can see.
func TestScopeUndeclared_ClearsWhenTheReadingGoes(t *testing.T) {
	h := scopeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)), scopeTestHost, scopeTestNeighbourA)
	h.poll(5)
	if cond := scopeCondition(t, h); cond == nil {
		t.Fatal("no condition after the first poll")
	}
	h.stub.fail(gatewayAddr(), usageScrapeKindRefused)
	h.poll(10)
	if cond := scopeCondition(t, h); cond == nil {
		t.Fatal("one missed poll removed the condition")
	}
	h.poll(15)
	if cond := scopeCondition(t, h); cond != nil {
		t.Fatalf("condition after the streak = %+v, want it removed", cond)
	}

	agent := usageTestAgent(usageClock(0).Add(-time.Hour))
	agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)}
	agent.Status.Conditions = []metav1.Condition{{Type: scopeUndeclaredCondition, Status: metav1.ConditionTrue, Reason: scopeUndeclaredReason, LastTransitionTime: metav1.Now()}}
	off := scopeHarness(t, agent, scopeTestHost, scopeTestNeighbourA)
	off.poll(5)
	if cond := scopeCondition(t, off); cond != nil {
		t.Fatalf("condition with the watcher off = %+v, want it removed", cond)
	}
}

// Two replicas each build the same fleet; one mid-startup reports fewer. The
// union is what the condition names, each cluster once.
func TestScopeUndeclared_UnitesTheReplicas(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), b)...)
	bAddr := usageTestGatewayB + ":9095"
	h.stub.set(gatewayAddr(), 0, ptr.To(1.0))
	h.stub.set(bAddr, 0, ptr.To(1.0))
	h.stub.set(brokerAddr(), 0, nil)
	h.stub.setClusterRefs(gatewayAddr(), scopeTestHost, scopeTestNeighbourA)
	h.stub.setClusterRefs(bAddr, scopeTestHost, scopeTestNeighbourA, scopeTestNeighbourB)
	h.poll(5)
	cond := scopeCondition(t, h)
	if cond == nil || !strings.Contains(cond.Message, "2 cluster(s)") || strings.Count(cond.Message, "analytics-dev") != 1 {
		t.Fatalf("condition = %+v, want both neighbours once", cond)
	}
}

// The message is bounded and prints only names shaped like GKE identifiers:
// the labels are a listener's text, and a hostile or broken value must not
// reach the CR as written.
func TestScopeUndeclaredMessage_BoundsAndValidatesNames(t *testing.T) {
	agent := usageTestAgent(usageClock(0))
	var refs []usageClusterRef
	for i := 0; i < scopeUndeclaredNamesShown+3; i++ {
		refs = append(refs, usageClusterRef{Name: fmt.Sprintf("n-%02d", i), Project: "p", Location: "l"})
	}
	msg := scopeUndeclaredMessage(agent, refs)
	if !strings.Contains(msg, fmt.Sprintf("%d cluster(s)", len(refs))) || !strings.Contains(msg, "and 3 more") {
		t.Errorf("message %q does not bound the list at %d names", msg, scopeUndeclaredNamesShown)
	}
	if strings.Contains(msg, fmt.Sprintf("n-%02d", scopeUndeclaredNamesShown)) {
		t.Errorf("message %q names a cluster past the bound", msg)
	}
	hostile := scopeUndeclaredMessage(agent, []usageClusterRef{{Name: "x\ny; rm -rf /", Project: "P R", Location: "l"}})
	if strings.Contains(hostile, "rm -rf") || strings.Contains(hostile, "P R") || !strings.Contains(hostile, scopeUndeclaredInvalidName) {
		t.Errorf("hostile labels reached the message: %q", hostile)
	}
}
