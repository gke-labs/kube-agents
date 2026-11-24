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

// A 10 GiB volume, in hundredths.
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

// The condition's message names the two lines that decide the status, where
// the current figures are and, when True, what to do. It holds no figure of
// its own, so it is the same at a later poll on a fuller volume, and it stays
// true there.
func TestDataVolume_MessageHoldsNoFigureAndStaysTrue(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(92))
	h.poll(5)
	cond := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition)
	want := "The agent's data volume reached 90% full and is not back under 85%. " +
		"The current figures are in status.storageStatus (capacityBytes and freeBytes, where the installed CRD has them) and in the kubeagents_data_volume_bytes metric. " +
		dataVolumeNearlyFullHint
	if cond == nil || cond.Message != want {
		t.Fatalf("True message = %q, want %q", cond.Message, want)
	}
	// An old transition time, so that a write that moved it would show.
	agent := h.agent()
	since := metav1.NewTime(usageClock(0).Add(-48 * time.Hour).Truncate(time.Second))
	meta.FindStatusCondition(agent.Status.Conditions, dataVolumeNearlyFullCondition).LastTransitionTime = since
	if err := h.cl.Status().Update(context.Background(), agent); err != nil {
		t.Fatalf("setting an old transition time: %v", err)
	}
	for _, figure := range []string{"92%", "9.2", "GiB"} {
		if strings.Contains(cond.Message, figure) {
			t.Fatalf("the True message holds the figure %q: %q", figure, cond.Message)
		}
	}
	// A later poll, fuller: the byte fields move, the condition's text does not.
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(97))
	h.poll(10)
	agent = h.agent()
	if again := meta.FindStatusCondition(agent.Status.Conditions, dataVolumeNearlyFullCondition); again.Message != cond.Message || !again.LastTransitionTime.Equal(&since) {
		t.Fatalf("a fuller volume rewrote the condition: %+v -> %+v", cond, again)
	}
	if agent.Status.StorageStatus.FreeBytes == nil || *agent.Status.StorageStatus.FreeBytes != freeAt(97) {
		t.Fatalf("status.storageStatus.freeBytes = %v, want the later reading %d: the message points there", agent.Status.StorageStatus.FreeBytes, freeAt(97))
	}
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(40))
	h.poll(15)
	cleared := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition)
	if cleared.Status != metav1.ConditionFalse || cleared.Message != "The agent's data volume is under 90% full. "+dataVolumeFiguresHint {
		t.Fatalf("False condition = %+v", cleared)
	}
}

// The message points at the byte fields, so a change of status carries the
// reading that caused it, also when free space moved by less than the write
// step since the fields were last written.
func TestDataVolume_AChangeOfStatusWritesTheReadingBesideIt(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	below := testVolume * 105 / 1000 // 89.5% used
	above := testVolume * 98 / 1000  // 90.2% used
	h.stub.setDataVolume(gatewayAddr(), testVolume, below)
	h.poll(5)
	if meta.IsStatusConditionTrue(h.agent().Status.Conditions, dataVolumeNearlyFullCondition) {
		t.Fatal("True at 89.5% used")
	}
	h.stub.setDataVolume(gatewayAddr(), testVolume, above)
	h.poll(10)
	agent := h.agent()
	if !meta.IsStatusConditionTrue(agent.Status.Conditions, dataVolumeNearlyFullCondition) {
		t.Fatal("not True at 90.2% used")
	}
	if got := ptr.Deref(agent.Status.StorageStatus.FreeBytes, -1); got != above {
		t.Fatalf("freeBytes = %d beside the True condition, want this poll's reading %d (under a hundredth from the last write)", got, above)
	}
}

// A condition that an earlier operator wrote, with the figures of its first
// write in the text, has the same status and reason as this operator's. The
// next poll writes this operator's text over it.
func TestDataVolume_AnEarlierOperatorsMessageIsRewritten(t *testing.T) {
	h := dataVolumeHarness(t, usageTestAgent(usageClock(0).Add(-time.Hour)))
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(92))
	h.poll(5)
	agent := h.agent()
	stale := "The agent's data volume is 91% full (9.1 of 10.0 GiB used). " + dataVolumeNearlyFullHint
	meta.FindStatusCondition(agent.Status.Conditions, dataVolumeNearlyFullCondition).Message = stale
	if err := h.cl.Status().Update(context.Background(), agent); err != nil {
		t.Fatalf("writing the earlier operator's condition: %v", err)
	}
	h.poll(10)
	cond := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition)
	if cond.Message == stale || strings.Contains(cond.Message, "GiB") {
		t.Fatalf("the earlier operator's message stayed: %q", cond.Message)
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
// interval, as it does for the newer status.usage fields, while the
// condition, which every schema carries, keeps landing inside the interval.
func TestDataVolume_APruningCRDIsProbedNotRewrittenEveryPoll(t *testing.T) {
	agent := usageTestAgent(usageClock(0).Add(-time.Hour))
	h := dataVolumeHarness(t, agent)
	h.pruningStorage = true
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(50))
	h.poll(5)
	after := h.patches
	if c := meta.FindStatusCondition(h.agent().Status.Conditions, dataVolumeNearlyFullCondition); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("the condition did not land under a pruning CRD: %+v", c)
	} else if !strings.Contains(c.Message, dataVolumeSeries) || !strings.Contains(c.Message, "where the installed CRD has them") {
		// The byte fields are not on this CR: the message must name another place for the figures.
		t.Fatalf("under a pruning CRD the message names only the byte fields: %q", c.Message)
	}
	// Inside the reprobe interval an unchanged reading does not rewrite the
	// pruned byte fields.
	h.poll(6)
	if h.patches != after {
		t.Fatalf("rewritten inside the reprobe interval: %d patches, want %d", h.patches, after)
	}
	// Still inside the interval the volume crosses 95%: one patch carries the
	// condition.
	h.stub.setDataVolume(gatewayAddr(), testVolume, freeAt(95))
	h.poll(7)
	got := h.agent()
	if h.patches != after+1 {
		t.Fatalf("%d patches after crossing 95%% inside the reprobe interval, want %d", h.patches, after+1)
	}
	if !meta.IsStatusConditionTrue(got.Status.Conditions, dataVolumeNearlyFullCondition) {
		t.Fatal("the condition stopped landing while the byte fields were pruned")
	}
	if s := got.Status.StorageStatus; s.CapacityBytes != nil || s.FreeBytes != nil {
		t.Fatalf("storageStatus %+v inside the reprobe interval, want the pruned byte fields left unwritten", s)
	}
	// Past the interval, with the CRD now serving the fields, one probe lands them.
	h.p.expireNewerFieldsRecordForTest(agent)
	h.pruningStorage = false
	h.poll(9)
	if s := h.agent().Status.StorageStatus; s.CapacityBytes == nil || *s.CapacityBytes != testVolume || s.FreeBytes == nil || *s.FreeBytes != freeAt(95) {
		t.Fatalf("after the record expired: storageStatus %+v, want capacity %d and free %d", s, testVolume, freeAt(95))
	}
}
