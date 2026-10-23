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
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// A 10 GiB volume, the operator's default size, in hundredths.
const (
	testVolume  = int64(10) << 30
	testPercent = testVolume / 100
)

// freeAt is the free space that leaves the test volume usedPercent full.
func freeAt(usedPercent int64) int64 {
	return testVolume * (100 - usedPercent) / 100
}

func (h *usageHarness) agent() *agentv1alpha1.PlatformAgent {
	h.t.Helper()
	agent := &agentv1alpha1.PlatformAgent{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName}, agent); err != nil {
		h.t.Fatalf("reading the agent: %v", err)
	}
	return agent
}

func dataVolumeHarness(t *testing.T, agent *agentv1alpha1.PlatformAgent) *usageHarness {
	t.Helper()
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, ptr.To(100.0))
	h.stub.setClusters(gatewayAddr(), 1, 1)
	h.stub.set(brokerAddr(), 0, ptr.To(200.0))
	return h
}

// The byte fields and the condition follow the watcher's reading: False below
// the line, True at 90% used, held True between 85% and 90% so a volume at the
// line does not flap, and False again below 85%. Readiness is not touched.
func TestDataVolume_ConditionCrossesWithHysteresis(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	steps := []struct {
		usedPercent int64
		want        metav1.ConditionStatus
		wantReason  string
	}{
		{50, metav1.ConditionFalse, dataVolumeReasonHasSpace},
		{89, metav1.ConditionFalse, dataVolumeReasonHasSpace},
		{90, metav1.ConditionTrue, dataVolumeReasonNearlyFull},
		{87, metav1.ConditionTrue, dataVolumeReasonNearlyFull},
		{85, metav1.ConditionTrue, dataVolumeReasonNearlyFull},
		{84, metav1.ConditionFalse, dataVolumeReasonHasSpace},
		{87, metav1.ConditionFalse, dataVolumeReasonHasSpace},
		{100, metav1.ConditionTrue, dataVolumeReasonNearlyFull},
	}
	for i, step := range steps {
		free := freeAt(step.usedPercent)
		h.stub.setDataVolume(gatewayAddr(), testVolume, free)
		h.poll(5 * (i + 1))
		agent := h.agent()
		cond := meta.FindStatusCondition(agent.Status.Conditions, dataVolumeNearlyFullCondition)
		if cond == nil || cond.Status != step.want || cond.Reason != step.wantReason {
			t.Fatalf("step %d (%d%% used): condition %+v, want %s/%s", i, step.usedPercent, cond, step.want, step.wantReason)
		}
		if got := agent.Status.StorageStatus; got.CapacityBytes == nil || *got.CapacityBytes != testVolume || got.FreeBytes == nil || *got.FreeBytes != free {
			t.Fatalf("step %d: storageStatus %+v, want capacity %d free %d", i, got, testVolume, free)
		}
		if meta.FindStatusCondition(agent.Status.Conditions, "Ready") != nil {
			t.Fatalf("step %d: the poller wrote Ready", i)
		}
	}
}

// The condition's message names the figures and, when True, what to do. It is
// written when the status changes and left alone while it holds, so a volume
// filling slowly does not rewrite the condition on every poll.
func TestDataVolume_MessageIsWrittenOnTransition(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(92))
	h.poll(5)
	cond := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition)
	if cond == nil || !strings.Contains(cond.Message, "92% full (9.2 of 10.0 GiB used)") || !strings.Contains(cond.Message, dataVolumeNearlyFullHint) {
		t.Fatalf("True message = %q", cond.Message)
	}
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(95))
	h.poll(10)
	if again := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition); again.Message != cond.Message {
		t.Fatalf("a held True condition was rewritten: %q -> %q", cond.Message, again.Message)
	}
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(40))
	h.poll(15)
	cleared := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition)
	if cleared.Status != metav1.ConditionFalse || strings.Contains(cleared.Message, dataVolumeNearlyFullHint) {
		t.Fatalf("False condition = %+v", cleared)
	}
}

// Free space moving by less than a hundredth of the volume writes nothing, so
// a working agent does not wake the reconciler every poll; a hundredth or more
// rewrites the fields.
func TestDataVolume_SmallMovesDoNotWrite(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	h.stub.setDataVolume(gatewayAddr(), testVolume, testVolume/2)
	h.poll(5)
	before := h.patches
	h.stub.setDataVolume(gatewayAddr(), testVolume, testVolume/2-testPercent/2)
	h.poll(10)
	if h.patches != before {
		t.Fatalf("half a percent wrote the status: %d patches, want %d", h.patches, before)
	}
	if got := *h.agent().Status.StorageStatus.FreeBytes; got != testVolume/2 {
		t.Fatalf("freeBytes = %d, want the earlier %d", got, testVolume/2)
	}
	h.stub.setDataVolume(gatewayAddr(), testVolume, testVolume/2-testPercent)
	h.poll(15)
	if got := *h.agent().Status.StorageStatus.FreeBytes; got != testVolume/2-testPercent {
		t.Fatalf("a full percent did not write: freeBytes = %d", got)
	}
}

// No reading clears the fields and removes the condition, on the gauges'
// terms: one missed poll holds them, the streak clears them; a watcher body
// with no volume series (an older watcher, or a failed statfs) clears them at
// once; and the watcher switched off clears them too.
func TestDataVolume_ClearsWithoutAReading(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	h.stub.setDataVolume(gatewayAddr(), testVolume, testPercent)
	h.poll(5)
	h.stub.fail(gatewayAddr(), usageScrapeKindRefused)
	h.poll(10)
	if agent := h.agent(); agent.Status.StorageStatus.FreeBytes == nil || !meta.IsStatusConditionTrue(agent.Status.Conditions, dataVolumeNearlyFullCondition) {
		t.Fatalf("one missed poll cleared the reading: %+v", agent.Status)
	}
	h.poll(15)
	assertNoDataVolume(t, h.agent(), "after the streak")

	h.stub.set(gatewayAddr(), 0, ptr.To(100.0))
	h.stub.setDataVolume(gatewayAddr(), testVolume, testPercent)
	h.poll(20)
	h.stub.mu.Lock()
	reading := h.stub.readings[gatewayAddr()]
	reading.DataVolume = nil
	h.stub.readings[gatewayAddr()] = reading
	h.stub.mu.Unlock()
	h.poll(25)
	assertNoDataVolume(t, h.agent(), "a watcher body without the series")
}

func TestDataVolume_ClearsWhenTheWatcherIsOff(t *testing.T) {
	agent := usageTestAgent(usageClock(0).Add(-time.Hour))
	agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)}
	agent.Status.StorageStatus = agentv1alpha1.StorageStatus{Bound: true, CapacityBytes: ptr.To(testVolume), FreeBytes: ptr.To(testPercent)}
	agent.Status.Conditions = []metav1.Condition{{Type: dataVolumeNearlyFullCondition, Status: metav1.ConditionTrue, Reason: dataVolumeReasonNearlyFull, LastTransitionTime: metav1.Now()}}
	h := dataVolumeHarness(t, agent)
	h.poll(5)
	got := h.agent()
	assertNoDataVolume(t, got, "watcher off")
	if !got.Status.StorageStatus.Bound {
		t.Fatal("clearing the reading cleared storageStatus.bound")
	}
}

// Two replicas share one volume, so the reading with less free space wins.
func TestDataVolume_TakesTheFullerReplica(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), b)...)
	bAddr := usageTestGatewayB + ":9095"
	h.stub.set(gatewayAddr(), 0, ptr.To(1.0))
	h.stub.set(bAddr, 0, ptr.To(1.0))
	h.stub.set(brokerAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 1, 1)
	h.stub.setClusters(bAddr, 1, 1)
	h.stub.setDataVolume(gatewayAddr(), testVolume, 50*testPercent)
	h.stub.setDataVolume(bAddr, testVolume, 5*testPercent)
	h.poll(5)
	if got := h.agent().Status.StorageStatus.FreeBytes; got == nil || *got != 5*testPercent {
		t.Fatalf("freeBytes = %v, want the fuller replica's %d", got, 5*testPercent)
	}
}

func assertNoDataVolume(t *testing.T, agent *agentv1alpha1.PlatformAgent, when string) {
	t.Helper()
	if s := agent.Status.StorageStatus; s.CapacityBytes != nil || s.FreeBytes != nil {
		t.Fatalf("%s: storageStatus %+v, want the byte fields cleared", when, s)
	}
	if c := meta.FindStatusCondition(agent.Status.Conditions, dataVolumeNearlyFullCondition); c != nil {
		t.Fatalf("%s: condition %+v, want it removed", when, c)
	}
}

// A served CRD from before this release prunes the byte fields. The poller
// records it from the patch's echo and probes again only after the reprobe
// interval, as it does for the newer status.usage fields, while
// the condition, which every schema carries, keeps landing.
func TestDataVolume_APruningCRDIsProbedNotRewrittenEveryPoll(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	h.pruningStorage = true
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(50))
	h.poll(5)
	after := h.patches
	if c := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("the condition did not land under a pruning CRD: %+v", c)
	}
	// Inside the reprobe interval nothing is written; past it, one probe.
	h.poll(7)
	if h.patches != after {
		t.Fatalf("the pruned byte fields were rewritten inside the reprobe interval: %d patches, want %d", h.patches, after)
	}
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(95))
	h.poll(15)
	if !meta.IsStatusConditionTrue(h.agent().Status.Conditions, dataVolumeNearlyFullCondition) {
		t.Fatal("the condition stopped landing while the byte fields were pruned")
	}
}
