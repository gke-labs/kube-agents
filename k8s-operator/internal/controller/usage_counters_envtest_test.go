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
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	usageEnvtestNamespace = "usage-env"
	usageEnvtestImage     = "example.com/kube-agents/test:0"
)

// usageEnvtestHarness is the poller against a real API server: the merge
// patch, the owner reference and the pruning are the server's, not a fake's.
type usageEnvtestHarness struct {
	t       *testing.T
	cl      client.Client
	agent   *agentv1alpha1.PlatformAgent
	r       *PlatformAgentReconciler
	p       *UsageCounterPoller
	stub    *stubUsageSource
	clock   time.Time
	patches int
}

func newUsageEnvtestHarness(t *testing.T, cfg *rest.Config, scheme *runtime.Scheme) *usageEnvtestHarness {
	t.Helper()
	direct, err := client.NewWithWatch(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("envtest client: %v", err)
	}
	h := &usageEnvtestHarness{t: t, stub: &stubUsageSource{readings: map[string]usageReading{}, errs: map[string]error{}}}
	h.cl = interceptor.NewClient(direct, interceptor.Funcs{
		SubResourcePatch: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			h.patches++
			return c.SubResource(subResourceName).Patch(ctx, obj, patch, opts...)
		},
	})
	ctx := context.Background()
	if err := h.cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: usageEnvtestNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	h.agent = &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: usageEnvtestNamespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
		},
	}
	if err := h.cl.Create(ctx, h.agent); err != nil {
		t.Fatalf("creating PlatformAgent: %v", err)
	}
	h.runningPod(usageGatewayPod("gateway", "", usageTestGatewayIP, time.Time{}))
	h.runningPod(usageBrokerPod("broker", "", usageTestBrokerIP, time.Time{}))

	h.r = &PlatformAgentReconciler{Client: h.cl, APIReader: h.cl, Scheme: scheme}
	h.p = &UsageCounterPoller{
		r:       h.r,
		source:  h.stub,
		now:     func() time.Time { return h.clock },
		streaks: map[types.UID]*usageScrapeStreak{},
	}
	return h
}

// runningPod creates pod and marks it Running at its IP through the status
// subresource, which is the kubelet's job on a real cluster.
func (h *usageEnvtestHarness) runningPod(pod *corev1.Pod) {
	h.t.Helper()
	pod.Namespace = usageEnvtestNamespace
	pod.UID = ""
	pod.CreationTimestamp = metav1.Time{}
	pod.Labels["app"] = envtestAgentName + "-gateway"
	if pod.Labels["kubeagents.x-k8s.io/component"] == "credential-proxy" {
		pod.Labels = credentialProxySelector(&agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName}})
	}
	for i := range pod.Spec.InitContainers {
		pod.Spec.InitContainers[i].Image = usageEnvtestImage
		pod.Spec.InitContainers[i].RestartPolicy = ptr.To(corev1.ContainerRestartPolicyAlways)
	}
	for i := range pod.Spec.Containers {
		pod.Spec.Containers[i].Image = usageEnvtestImage
	}
	status := pod.Status
	pod.Status = corev1.PodStatus{}
	ctx := context.Background()
	if err := h.cl.Create(ctx, pod); err != nil {
		h.t.Fatalf("creating pod %s: %v", pod.Name, err)
	}
	pod.Status = status
	pod.Status.PodIPs = []corev1.PodIP{{IP: status.PodIP}}
	if err := h.cl.Status().Update(ctx, pod); err != nil {
		h.t.Fatalf("marking pod %s running: %v", pod.Name, err)
	}
}

// at is minute minutes after the CR was created, which the real server
// stamped with the wall clock: the document's first-recorded time has to fall
// inside the CR's life, so the polls are placed after its creation rather than
// at a fixed date.
func (h *usageEnvtestHarness) at(minute int) time.Time {
	return h.agent.CreationTimestamp.Time.Truncate(time.Second).Add(time.Duration(minute) * time.Minute)
}

func (h *usageEnvtestHarness) poll(minute int) {
	h.t.Helper()
	h.clock = h.at(minute)
	h.p.pollOnce(context.Background())
}

func (h *usageEnvtestHarness) status() agentv1alpha1.AgentUsageStatus {
	h.t.Helper()
	agent := &agentv1alpha1.PlatformAgent{}
	if err := h.cl.Get(context.Background(), client.ObjectKeyFromObject(h.agent), agent); err != nil {
		h.t.Fatalf("reading the agent: %v", err)
	}
	return agent.Status.Usage
}

func (h *usageEnvtestHarness) configMap() *corev1.ConfigMap {
	h.t.Helper()
	cm := &corev1.ConfigMap{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageEnvtestNamespace, Name: envtestAgentName + usageCountersConfigMapSuffix}, cm); err != nil {
		h.t.Fatalf("reading the ConfigMap: %v", err)
	}
	return cm
}

// TestUsagePollerOnAServedCRDEnvtest: a PlatformAgent served by this release's
// CRD receives one patch per poll in which the source moves and none in which
// it does not; the ConfigMap carries a non-controller owner reference the
// server accepted; and a status left behind the ConfigMap is repaired by the
// next poll without the totals moving and with lastActiveTime the time the
// ConfigMap recorded.
func TestUsagePollerOnAServedCRDEnvtest(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	h := newUsageEnvtestHarness(t, cfg, scheme)
	h.stub.set(gatewayAddr(), 100, ptr.To(1.0))
	h.stub.set(brokerAddr(), 50, ptr.To(2.0))

	h.poll(5)
	if h.patches != 0 {
		t.Fatalf("the first poll patched the status %d times", h.patches)
	}
	cm := h.configMap()
	if len(cm.OwnerReferences) != 1 || cm.OwnerReferences[0].UID != h.agent.UID || (cm.OwnerReferences[0].Controller != nil && *cm.OwnerReferences[0].Controller) {
		t.Fatalf("owner references: %+v", cm.OwnerReferences)
	}

	h.stub.set(gatewayAddr(), 104, ptr.To(1.0))
	h.stub.set(brokerAddr(), 57, ptr.To(2.0))
	h.poll(10)
	if h.patches != 1 {
		t.Fatalf("a poll in which the source moved patched %d times, want 1", h.patches)
	}
	status := h.status()
	if status.EventsIngestedTotal != 4 || status.ToolExecutionsTotal != 7 || status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(h.at(10)) {
		t.Fatalf("status after the move: %+v", status)
	}
	h.poll(15)
	if h.patches != 1 {
		t.Fatalf("a quiet poll patched the status (%d patches)", h.patches)
	}

	// The crash between the two writes, staged by hand: the status loses the
	// counters while the ConfigMap keeps them.
	ctx := context.Background()
	stale := &agentv1alpha1.PlatformAgent{}
	if err := h.cl.Get(ctx, client.ObjectKeyFromObject(h.agent), stale); err != nil {
		t.Fatal(err)
	}
	stale.Status.Usage = agentv1alpha1.AgentUsageStatus{}
	if err := h.cl.Status().Update(ctx, stale); err != nil {
		t.Fatalf("staging the stale status: %v", err)
	}
	if staged := h.status(); staged.ToolExecutionsTotal != 0 || staged.LastActiveTime != nil {
		t.Fatalf("the staging did not clear the status: %+v", staged)
	}
	h.poll(20)
	if h.patches != 2 {
		t.Fatalf("the repair poll patched %d times in total, want 2", h.patches)
	}
	repaired := h.status()
	if repaired.EventsIngestedTotal != 4 || repaired.ToolExecutionsTotal != 7 {
		t.Fatalf("the repair moved the totals: %+v", repaired)
	}
	if repaired.LastActiveTime == nil || !repaired.LastActiveTime.Time.Equal(h.at(10)) {
		t.Fatalf("lastActiveTime after the repair = %v, want the ConfigMap's %v", repaired.LastActiveTime, h.at(10))
	}
}

// TestUsagePollerUnderACRDWithoutUsageEnvtest: under a served CRD without
// status.usage the poller writes the status once per
// usageStatusReprobeInterval, shares the pruning record with the Ready writer,
// and keeps the ConfigMap current throughout; when the CRD is applied the next
// probe lands everything accumulated since.
func TestUsagePollerUnderACRDWithoutUsageEnvtest(t *testing.T) {
	pruned, full := platformAgentCRDWithoutUsage(t)
	crdDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(crdDir, platformAgentCRDFile), pruned, 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, scheme := startEnvtestConfigWithCRDs(t, crdDir)
	h := newUsageEnvtestHarness(t, cfg, scheme)
	h.stub.set(brokerAddr(), 50, ptr.To(2.0))
	h.stub.set(gatewayAddr(), 0, ptr.To(1.0))
	h.poll(5)
	h.stub.set(brokerAddr(), 60, ptr.To(2.0))
	h.poll(10)
	if h.patches != 1 {
		t.Fatalf("the first movement under the pruning CRD patched %d times, want 1 (the probe)", h.patches)
	}
	if !h.r.usageStatusPruned(h.agent) {
		t.Fatal("the probe's echo came back without the counters and no record was kept")
	}
	// The Ready writer reads the same record.
	key := client.ObjectKeyFromObject(h.agent)
	if _, held := h.r.prunedUsageStatus.Load(key); !held {
		t.Fatal("the record is not in the map the Ready writer reads")
	}
	h.stub.set(brokerAddr(), 70, ptr.To(2.0))
	h.poll(15)
	if h.patches != 1 {
		t.Fatalf("a poll under a fresh record patched (%d patches)", h.patches)
	}
	cm := h.configMap()
	if cm.Data[usageCountersDocumentKey] == "" {
		t.Fatal("the ConfigMap is empty under the pruning CRD")
	}
	var doc usageDocument
	if err := decodeUsageDocument(cm, &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Totals[usageCounterToolExecutions] != 20 {
		t.Fatalf("the ConfigMap fell behind under the pruning CRD: %v", doc.Totals)
	}

	// Apply this release's CRD, expire the record, and the next poll lands
	// the totals accumulated since the operator was upgraded.
	live := &unstructured.Unstructured{}
	live.SetGroupVersionKind(schema.GroupVersionKind{Group: "apiextensions.k8s.io", Version: "v1", Kind: "CustomResourceDefinition"})
	ctx := context.Background()
	if err := h.cl.Get(ctx, client.ObjectKey{Name: platformAgentCRDName}, live); err != nil {
		t.Fatalf("reading the served CRD: %v", err)
	}
	live.Object["spec"] = full["spec"]
	if err := h.cl.Update(ctx, live); err != nil {
		t.Fatalf("applying the full CRD: %v", err)
	}
	deadline := time.Now().Add(crdSettleTimeout)
	for {
		h.r.prunedUsageStatus.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
		h.poll(20)
		if status := h.status(); status.ToolExecutionsTotal == 20 {
			if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(h.at(15)) {
				t.Fatalf("lastActiveTime after the CRD landed = %v, want the last move %v", status.LastActiveTime, h.at(15))
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the status never carried the totals after the CRD was applied: %+v", h.status())
		}
		time.Sleep(crdSettlePoll)
	}
	if h.r.usageStatusPruned(h.agent) {
		t.Error("the echo carried the counters and the record was not cleared")
	}
}

func decodeUsageDocument(cm *corev1.ConfigMap, doc *usageDocument) error {
	return json.Unmarshal([]byte(cm.Data[usageCountersDocumentKey]), doc)
}
