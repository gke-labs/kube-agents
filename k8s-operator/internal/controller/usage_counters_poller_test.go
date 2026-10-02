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
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/client-go/util/workqueue"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	usageTestNamespace = "usage-test"
	usageTestAgentName = "agent"
	usageTestAgentUID  = "agent-uid-1"
	usageTestGatewayIP = "10.0.0.10"
	usageTestBrokerIP  = "10.0.0.20"
	usageTestGatewayB  = "10.0.0.11"
)

func usageClock(minute int) time.Time {
	return time.Date(2026, 10, 2, 9, minute, 0, 0, time.UTC)
}

func usageTestAgent(created time.Time) *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{
			Name:              usageTestAgentName,
			Namespace:         usageTestNamespace,
			UID:               types.UID(usageTestAgentUID),
			CreationTimestamp: metav1.NewTime(created),
		},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "p", Location: "l", ClusterName: "c"},
		},
	}
}

// usageGatewayPod is a running gateway pod with the watcher's port on the
// native sidecar, an initContainers entry, among containers that carry no
// port.
func usageGatewayPod(name, uid, ip string, created time.Time) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: usageTestNamespace, UID: types.UID(uid),
			CreationTimestamp: metav1.NewTime(created),
			Labels:            map[string]string{"app": usageTestAgentName + "-gateway"},
		},
		Spec: corev1.PodSpec{
			InitContainers: []corev1.Container{
				{Name: "plugin-staging"},
				{Name: "agent-api-auth", Ports: []corev1.ContainerPort{
					{Name: "proxy-api", ContainerPort: 8643},
					{Name: eventWatcherMetricsPortName, ContainerPort: eventWatcherMetricsPort},
				}},
			},
			Containers: []corev1.Container{{Name: "agent"}, {Name: "fluent-bit"}},
		},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: ip},
	}
}

func usageBrokerPod(name, uid, ip string, created time.Time) *corev1.Pod {
	agent := usageTestAgent(created)
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: usageTestNamespace, UID: types.UID(uid),
			CreationTimestamp: metav1.NewTime(created),
			Labels:            credentialProxySelector(agent),
		},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{
				{Name: "envoy"},
				{Name: "envoy-credential-proxy", Ports: []corev1.ContainerPort{
					{Name: credentialProxyMetricsPortName, ContainerPort: credentialProxyMetricsPort},
				}},
			},
		},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: ip},
	}
}

// stubUsageSource answers scrapes from a table keyed by address.
type stubUsageSource struct {
	mu       sync.Mutex
	readings map[string]usageReading
	errs     map[string]error
	calls    []string
}

func (s *stubUsageSource) Scrape(_ context.Context, addr, _ string) (usageReading, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls = append(s.calls, addr)
	if err := s.errs[addr]; err != nil {
		return usageReading{}, err
	}
	reading, ok := s.readings[addr]
	if !ok {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindConnect}
	}
	return reading, nil
}

func (s *stubUsageSource) set(addr string, sample int64, start *float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.errs, addr)
	s.readings[addr] = usageReading{Sample: sample, StartTime: start}
}

func (s *stubUsageSource) fail(addr string, kind string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.errs[addr] = &usageScrapeError{Kind: kind}
}

func (s *stubUsageSource) scraped(addr string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, call := range s.calls {
		if call == addr {
			return true
		}
	}
	return false
}

// usageHarness is a poller over a fake client, with the writes it makes
// counted.
type usageHarness struct {
	t        *testing.T
	cl       client.Client
	r        *PlatformAgentReconciler
	p        *UsageCounterPoller
	stub     *stubUsageSource
	recorder *record.FakeRecorder
	clock    time.Time
	patches  int
	cmWrites int
	// pruning makes the fake behave like a served CRD without status.usage:
	// the echo of a status patch comes back with the field empty.
	pruning bool
}

func newUsageHarness(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) *usageHarness {
	t.Helper()
	h := &usageHarness{t: t, stub: &stubUsageSource{readings: map[string]usageReading{}, errs: map[string]error{}}}
	funcs := interceptor.Funcs{
		SubResourcePatch: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			h.patches++
			err := c.SubResource(subResourceName).Patch(ctx, obj, patch, opts...)
			if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && h.pruning {
				pa.Status.Usage = agentv1alpha1.AgentUsageStatus{}
			}
			return err
		},
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			if _, ok := obj.(*corev1.ConfigMap); ok {
				h.cmWrites++
			}
			return c.Create(ctx, obj, opts...)
		},
		Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
			if _, ok := obj.(*corev1.ConfigMap); ok {
				h.cmWrites++
			}
			return c.Update(ctx, obj, opts...)
		},
	}
	scheme := setupScheme()
	h.cl = fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append([]client.Object{agent}, objs...)...).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	h.recorder = record.NewFakeRecorder(16)
	h.r = &PlatformAgentReconciler{Client: h.cl, APIReader: h.cl, Scheme: scheme, Recorder: h.recorder}
	h.p = &UsageCounterPoller{
		r:       h.r,
		source:  h.stub,
		now:     func() time.Time { return h.clock },
		streaks: map[types.UID]*usageScrapeStreak{},
	}
	return h
}

func (h *usageHarness) poll(minute int) {
	h.t.Helper()
	h.clock = usageClock(minute)
	h.p.pollOnce(context.Background())
}

func (h *usageHarness) document() *usageDocument {
	h.t.Helper()
	cm := &corev1.ConfigMap{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName + usageCountersConfigMapSuffix}, cm); err != nil {
		h.t.Fatalf("reading the ConfigMap: %v", err)
	}
	var doc usageDocument
	if err := json.Unmarshal([]byte(cm.Data[usageCountersDocumentKey]), &doc); err != nil {
		h.t.Fatalf("parsing the document: %v", err)
	}
	return &doc
}

func (h *usageHarness) configMap() *corev1.ConfigMap {
	h.t.Helper()
	cm := &corev1.ConfigMap{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName + usageCountersConfigMapSuffix}, cm); err != nil {
		h.t.Fatalf("reading the ConfigMap: %v", err)
	}
	return cm
}

func (h *usageHarness) status() agentv1alpha1.AgentUsageStatus {
	h.t.Helper()
	agent := &agentv1alpha1.PlatformAgent{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName}, agent); err != nil {
		h.t.Fatalf("reading the agent: %v", err)
	}
	return agent.Status.Usage
}

func (h *usageHarness) writeDocument(doc *usageDocument) {
	h.t.Helper()
	agent := &agentv1alpha1.PlatformAgent{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName}, agent); err != nil {
		h.t.Fatalf("reading the agent: %v", err)
	}
	cm := &corev1.ConfigMap{}
	err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName + usageCountersConfigMapSuffix}, cm)
	var existing *corev1.ConfigMap
	if err == nil {
		existing = cm
	}
	if err := h.p.writeDocument(context.Background(), agent, existing, doc); err != nil {
		h.t.Fatalf("writing the document: %v", err)
	}
	h.cmWrites = 0
}

func gatewayAddr() string { return usageTestGatewayIP + ":9095" }
func brokerAddr() string  { return usageTestBrokerIP + ":8766" }

func usageDefaultObjects(created time.Time) []client.Object {
	return []client.Object{
		usageGatewayPod("agent-gateway-aaa", "gw-a", usageTestGatewayIP, created),
		usageBrokerPod("agent-credential-proxy-bbb", "broker-b", usageTestBrokerIP, created),
	}
}

// The first poll records every pod and adds nothing, writing the ConfigMap
// with a non-controller owner reference to the CR and patching no status; the
// second poll adds the deltas, writes the ConfigMap and patches the status
// once, with lastActiveTime the poll's time.
func TestUsagePoller_FirstPollRecordsThenCountsFromTheBaseline(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
	h.stub.set(brokerAddr(), 70, ptr.To(200.0))

	h.poll(5)
	if h.cmWrites != 1 || h.patches != 0 {
		t.Fatalf("first poll: %d ConfigMap writes, %d status patches; want 1 and 0", h.cmWrites, h.patches)
	}
	cm := h.configMap()
	if len(cm.OwnerReferences) != 1 || cm.OwnerReferences[0].UID != types.UID(usageTestAgentUID) || cm.OwnerReferences[0].Controller != nil && *cm.OwnerReferences[0].Controller {
		t.Fatalf("owner references: %+v, want one non-controller reference to the CR", cm.OwnerReferences)
	}
	if cm.Labels["app.kubernetes.io/instance"] == "" && cm.Labels["kubeagents.x-k8s.io/agent-name"] == "" {
		t.Errorf("the ConfigMap carries no instance labels: %v", cm.Labels)
	}
	doc := h.document()
	if doc.Totals[usageCounterEventsIngested] != 0 || doc.Totals[usageCounterToolExecutions] != 0 || doc.AgentUID != usageTestAgentUID {
		t.Fatalf("first document: %+v", doc)
	}
	if !doc.FirstRecorded.Time.Equal(usageClock(5)) {
		t.Errorf("firstRecorded = %v, want %v", doc.FirstRecorded, usageClock(5))
	}
	if status := h.status(); status.ToolExecutionsTotal != 0 || status.LastActiveTime != nil {
		t.Fatalf("the first poll wrote the status: %+v", status)
	}

	h.stub.set(gatewayAddr(), 512, ptr.To(100.0))
	h.stub.set(brokerAddr(), 73, ptr.To(200.0))
	h.poll(10)
	if h.cmWrites != 2 || h.patches != 1 {
		t.Fatalf("second poll: %d ConfigMap writes, %d status patches; want 2 and 1", h.cmWrites, h.patches)
	}
	status := h.status()
	if status.EventsIngestedTotal != 12 || status.ToolExecutionsTotal != 3 {
		t.Fatalf("status after the second poll: %+v, want 12 events and 3 tool executions", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) {
		t.Fatalf("lastActiveTime = %v, want %v", status.LastActiveTime, usageClock(10))
	}
	if status.ActiveInterfaces != nil {
		t.Errorf("the poller touched activeInterfaces: %v", status.ActiveInterfaces)
	}

	// A quiet poll writes nothing.
	h.poll(15)
	if h.cmWrites != 2 || h.patches != 1 {
		t.Fatalf("a quiet poll wrote: %d ConfigMap writes, %d status patches", h.cmWrites, h.patches)
	}
}

// The port is found by name on the listener's own container, the native
// sidecar among several containers; a pod without the port on that
// container, or not running, is not a target, and a CR's own init container
// that names the same port first is not the listener.
func TestUsagePoller_FindsThePortByNameOnTheSidecar(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	pending := usageGatewayPod("agent-gateway-pending", "gw-pending", "10.0.0.99", created)
	pending.Status.Phase = corev1.PodPending
	noPort := usageGatewayPod("agent-gateway-noport", "gw-noport", "10.0.0.98", created)
	noPort.Spec.InitContainers[1].Ports = nil
	terminating := usageGatewayPod("agent-gateway-terminating", "gw-terminating", "10.0.0.97", created)
	terminating.Finalizers = []string{"usage-test/hold"}
	objects := usageDefaultObjects(created)
	gateway := objects[0].(*corev1.Pod)
	// A CR author's init container, placed first as the render places them,
	// naming the watcher's port on another number.
	gateway.Spec.InitContainers = append([]corev1.Container{{
		Name:  "authors-own",
		Ports: []corev1.ContainerPort{{Name: eventWatcherMetricsPortName, ContainerPort: 9999}},
	}}, gateway.Spec.InitContainers...)
	h := newUsageHarness(t, usageTestAgent(created), append(objects, pending, noPort, terminating)...)
	// The finalizer keeps the pod around with its deletionTimestamp set.
	if err := h.cl.Delete(context.Background(), terminating); err != nil {
		t.Fatal(err)
	}
	targets, live, err := h.p.targets(context.Background(), usageTestAgent(created))
	if err != nil {
		t.Fatal(err)
	}
	if len(targets) != 2 {
		t.Fatalf("targets = %+v, want the gateway and the broker", targets)
	}
	addrs := map[string]string{}
	for _, target := range targets {
		addrs[target.counter] = target.addr
	}
	if addrs[usageCounterEventsIngested] != gatewayAddr() || addrs[usageCounterToolExecutions] != brokerAddr() {
		t.Errorf("addresses: %v", addrs)
	}
	if !live["gw-pending"] || !live["gw-noport"] || !live["gw-a"] || !live["broker-b"] {
		t.Errorf("live = %v, want every pod that exists", live)
	}
	if live["gw-terminating"] {
		t.Errorf("live = %v: a terminating pod, which is never read again, counts as live", live)
	}
}

// An IPv6 pod IP is joined so the URL parses.
func TestUsagePodPortAndAddress_IPv6(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	pod := usageGatewayPod("agent-gateway-v6", "gw-v6", "fd00::10", created)
	h := newUsageHarness(t, usageTestAgent(created), pod)
	targets, _, err := h.p.targets(context.Background(), usageTestAgent(created))
	if err != nil {
		t.Fatal(err)
	}
	if len(targets) != 1 || targets[0].addr != "[fd00::10]:9095" {
		t.Fatalf("targets = %+v, want [fd00::10]:9095", targets)
	}
}

// With the watcher switched off, the gateway pods are not read and no event
// is recorded; the broker still is.
func TestUsagePoller_DisabledWatcherScrapesNoGatewayPod(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)}
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 70, nil)
	h.poll(5)
	h.stub.set(brokerAddr(), 75, nil)
	h.poll(10)
	if h.stub.scraped(gatewayAddr()) {
		t.Error("the gateway listener was read with the watcher off")
	}
	if status := h.status(); status.ToolExecutionsTotal != 5 || status.EventsIngestedTotal != 0 {
		t.Errorf("status: %+v", status)
	}
	select {
	case ev := <-h.recorder.Events:
		t.Errorf("an event was recorded: %s", ev)
	default:
	}
}

// A status left behind the ConfigMap, the crash between the two writes staged
// by hand, is repaired by the next poll without the totals moving and with
// lastActiveTime the time the ConfigMap recorded, not the repair's.
func TestUsagePoller_RepairsAStatusBehindTheConfigMap(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	moved := metav1.NewTime(usageClock(3))
	h.writeDocument(&usageDocument{
		Version:       usageDocumentVersion,
		AgentUID:      usageTestAgentUID,
		FirstRecorded: metav1.NewTime(usageClock(1)),
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			"gw-a":     {Name: "agent-gateway-aaa", Counter: usageCounterEventsIngested, Sample: 4, Marker: moved},
			"broker-b": {Name: "agent-credential-proxy-bbb", Counter: usageCounterToolExecutions, Sample: 10, Marker: moved},
		},
	})
	h.stub.set(gatewayAddr(), 4, nil)
	h.stub.set(brokerAddr(), 10, nil)
	h.poll(10)
	if h.patches != 1 || h.cmWrites != 0 {
		t.Fatalf("repair: %d patches, %d ConfigMap writes; want 1 and 0", h.patches, h.cmWrites)
	}
	status := h.status()
	if status.ToolExecutionsTotal != 10 || status.EventsIngestedTotal != 4 {
		t.Fatalf("status after the repair: %+v", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(3)) {
		t.Fatalf("lastActiveTime = %v, want the ConfigMap's %v", status.LastActiveTime, usageClock(3))
	}
}

// A ConfigMap whose recorded CR UID is not the CR's, or whose values fail the
// read-back bounds, is treated as absent: re-seeded from the status with this
// poll's time as the first-recorded time, every pod recorded and nothing
// added.
func TestUsagePoller_ReseedsAnInvalidDocument(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	stamp := metav1.NewTime(usageClock(1))
	valid := func() *usageDocument {
		return &usageDocument{
			Version:       usageDocumentVersion,
			AgentUID:      usageTestAgentUID,
			FirstRecorded: stamp,
			Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4},
			Pods: map[string]*usagePodEntry{
				"broker-b": {Name: "agent-credential-proxy-bbb", Counter: usageCounterToolExecutions, Sample: 10, Marker: stamp},
			},
		}
	}
	cases := []struct {
		name   string
		mutate func(*usageDocument)
	}{
		{"another CR's UID", func(d *usageDocument) { d.AgentUID = "predecessor" }},
		{"a total above the int64 headroom", func(d *usageDocument) { d.Totals[usageCounterToolExecutions] = usageTotalCeiling + 1 }},
		{"a total below the status", func(d *usageDocument) { d.Totals[usageCounterToolExecutions] = 1 }},
		{"a first-recorded time in the future", func(d *usageDocument) { d.FirstRecorded = metav1.NewTime(usageClock(59)) }},
		{"a first-recorded time before the CR", func(d *usageDocument) { d.FirstRecorded = metav1.NewTime(created.Add(-time.Minute)) }},
		{"a negative sample", func(d *usageDocument) { d.Pods["broker-b"].Sample = -1 }},
		{"an unknown version", func(d *usageDocument) { d.Version = usageDocumentVersion + 1 }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := usageTestAgent(created)
			agent.Status.Usage.ToolExecutionsTotal = 7
			h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
			doc := valid()
			tc.mutate(doc)
			h.writeDocument(doc)
			h.stub.set(brokerAddr(), 500, nil)
			h.stub.set(gatewayAddr(), 300, nil)
			h.poll(10)
			got := h.document()
			if !got.FirstRecorded.Time.Equal(usageClock(10)) {
				t.Errorf("firstRecorded = %v, want the re-seeding poll's %v", got.FirstRecorded, usageClock(10))
			}
			if got.Totals[usageCounterToolExecutions] != 7 || got.Totals[usageCounterEventsIngested] != 0 {
				t.Errorf("totals = %v, want seeded from the status (7, 0)", got.Totals)
			}
			if got.Pods["broker-b"] == nil || got.Pods["broker-b"].Sample != 500 || got.Pods["gw-a"] == nil || got.Pods["gw-a"].Sample != 300 {
				t.Errorf("pods = %+v, want both recorded at their samples", got.Pods)
			}
			if h.patches != 0 {
				t.Errorf("a re-seed patched the status %d times", h.patches)
			}
		})
	}
}

// A ConfigMap from a predecessor CR of the same name, found by its owner
// reference rather than the document, is overwritten and re-owned.
func TestUsagePoller_ReownsAPredecessorsConfigMap(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	stamp := metav1.NewTime(usageClock(1))
	doc := &usageDocument{Version: usageDocumentVersion, AgentUID: usageTestAgentUID, FirstRecorded: stamp, Totals: map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 0}, Pods: map[string]*usagePodEntry{}}
	raw, _ := json.Marshal(doc)
	predecessor := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name: usageTestAgentName + usageCountersConfigMapSuffix, Namespace: usageTestNamespace,
			OwnerReferences: []metav1.OwnerReference{{APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent", Name: usageTestAgentName, UID: "predecessor-uid"}},
		},
		Data: map[string]string{usageCountersDocumentKey: string(raw)},
	}
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), predecessor)...)
	h.stub.set(brokerAddr(), 500, nil)
	h.poll(10)
	cm := h.configMap()
	if len(cm.OwnerReferences) != 1 || cm.OwnerReferences[0].UID != types.UID(usageTestAgentUID) {
		t.Fatalf("owner references after re-owning: %+v", cm.OwnerReferences)
	}
	if got := h.document(); got.Totals[usageCounterToolExecutions] != 0 || !got.FirstRecorded.Time.Equal(usageClock(10)) {
		t.Fatalf("the predecessor's totals were inherited: %+v", got)
	}
}

// Under a served CRD that prunes status.usage, the poller writes the status
// once per usageStatusReprobeInterval, shares the pruning record with the
// Ready writer, and keeps the ConfigMap current throughout.
func TestUsagePoller_SharesThePrunedRecordWithTheReadyWriter(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruning = true
	h.stub.set(brokerAddr(), 10, nil)
	h.poll(5)
	h.stub.set(brokerAddr(), 15, nil)
	h.poll(10)
	if h.patches != 1 {
		t.Fatalf("%d patches after the first movement, want 1 (the probe)", h.patches)
	}
	if !h.r.usageStatusPruned(agent) {
		t.Fatal("the echo came back without the counters and no pruning was recorded")
	}
	h.stub.set(brokerAddr(), 20, nil)
	h.poll(15)
	if h.patches != 1 {
		t.Fatalf("%d patches while the record is fresh, want 1", h.patches)
	}
	if doc := h.document(); doc.Totals[usageCounterToolExecutions] != 10 {
		t.Fatalf("the ConfigMap fell behind under the pruning CRD: %v", doc.Totals)
	}
	// The record expires: the next poll probes again, and with the CRD now
	// serving the field the patch lands everything accumulated since.
	h.r.prunedUsageStatus.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-2*usageStatusReprobeInterval))
	h.pruning = false
	h.poll(20)
	if h.patches != 2 {
		t.Fatalf("%d patches after the record expired, want 2", h.patches)
	}
	if status := h.status(); status.ToolExecutionsTotal != 10 || status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(15)) {
		t.Fatalf("status after the CRD landed: %+v, want 10 and lastActiveTime %v", status, usageClock(15))
	}
	if h.r.usageStatusPruned(agent) {
		t.Error("the echo carried the counters and the record was not cleared")
	}
}

// The ConfigMap's owner reference enqueues no reconcile: the controller Owns
// ConfigMaps through the handler that enqueues controller owners only, and
// the reference the poller writes is not one. The same handler enqueues a
// controller-owned ConfigMap, so the test discriminates.
func TestUsagePoller_ConfigMapOwnerReferenceEnqueuesNoReconcile(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 10, nil)
	h.poll(5)
	cm := h.configMap()

	mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{agentv1alpha1.GroupVersion})
	mapper.Add(agentv1alpha1.GroupVersion.WithKind("PlatformAgent"), meta.RESTScopeNamespace)
	owns := handler.EnqueueRequestForOwner(h.r.Scheme, mapper, &agentv1alpha1.PlatformAgent{}, handler.OnlyControllerOwner())
	queue := workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[reconcile.Request]())
	defer queue.ShutDown()
	owns.Create(context.Background(), event.CreateEvent{Object: cm}, queue)
	if queue.Len() != 0 {
		t.Fatalf("the poller's ConfigMap enqueued %d reconcile(s)", queue.Len())
	}
	controlled := cm.DeepCopy()
	controlled.OwnerReferences[0].Controller = ptr.To(true)
	owns.Create(context.Background(), event.CreateEvent{Object: controlled}, queue)
	if queue.Len() != 1 {
		t.Fatalf("a controller-owned ConfigMap enqueued %d reconcile(s); the handler under test is not the one Owns uses", queue.Len())
	}
}

// A listener that cannot be read leaves the pod's baseline and the totals
// untouched, costs one log line, and records one Warning event when the
// streak reaches two polls, not before; a pod the first poll could not scrape,
// created before the document, is recorded on its next scrape and adds
// nothing.
func TestUsagePoller_FailureStreaksAndLateRecording(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.poll(5)
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("an event after one failed poll: %s", ev)
	default:
	}
	h.poll(10)
	select {
	case ev := <-h.recorder.Events:
		if !strings.Contains(ev, usageScrapeFailingReason) || !strings.Contains(ev, "agent-gateway-aaa") {
			t.Fatalf("event = %q, want %s naming the pod", ev, usageScrapeFailingReason)
		}
	default:
		t.Fatal("no event after two failed polls")
	}
	h.poll(15)
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a second event for the same streak: %s", ev)
	default:
	}
	if doc := h.document(); doc.Pods["gw-a"] != nil {
		t.Fatalf("an unreadable pod was recorded: %+v", doc.Pods["gw-a"])
	}

	// Back, created before the document: recorded, adds nothing; then counted.
	h.stub.set(gatewayAddr(), 400, ptr.To(1.0))
	h.poll(20)
	if doc := h.document(); doc.Pods["gw-a"] == nil || doc.Pods["gw-a"].Sample != 400 || doc.Totals[usageCounterEventsIngested] != 0 {
		t.Fatalf("after recovery: %+v", doc)
	}
	h.stub.set(gatewayAddr(), 403, ptr.To(1.0))
	h.poll(25)
	if status := h.status(); status.EventsIngestedTotal != 3 {
		t.Fatalf("after the first counted poll: %+v", status)
	}
}

// Two gateway replicas on the poller end to end: the total takes the largest
// delta, and the replica reset against its sibling's marker persists through
// the ConfigMap.
func TestUsagePoller_TwoGatewayReplicas(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), b)...)
	bAddr := usageTestGatewayB + ":9095"
	h.stub.set(gatewayAddr(), 100, ptr.To(1.0))
	h.stub.set(bAddr, 100, ptr.To(1.0))
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	h.stub.set(gatewayAddr(), 110, ptr.To(1.0))
	h.stub.set(bAddr, 106, ptr.To(1.0))
	h.poll(10)
	if status := h.status(); status.EventsIngestedTotal != 10 {
		t.Fatalf("after both moved: %+v, want 10", status)
	}
	h.stub.set(bAddr, 110, ptr.To(1.0))
	h.poll(15)
	if status := h.status(); status.EventsIngestedTotal != 10 {
		t.Fatalf("the lagger's catch-up was counted: %+v", status)
	}
	if doc := h.document(); !doc.Pods["gw-b"].Marker.Time.Equal(usageClock(10)) {
		t.Fatalf("the reset replica did not take the sibling's marker: %+v", doc.Pods["gw-b"])
	}
}

// The failure streaks are one map per process, pruned once per poll over
// every CR's pods: with two CRs each owning a failing listener, each CR gets
// its Warning event after two polls, and neither CR's poll resets the other's
// streak.
func TestUsagePoller_StreaksSurviveAnotherCRsPoll(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	first := usageTestAgent(created)
	second := usageTestAgent(created)
	second.Name, second.Namespace, second.UID = "second", "usage-test-2", "agent-uid-2"
	secondGateway := usageGatewayPod("second-gateway-aaa", "gw-second", "10.0.1.10", created)
	secondGateway.Namespace, secondGateway.Labels = second.Namespace, map[string]string{"app": second.Name + "-gateway"}
	h := newUsageHarness(t, first, append(usageDefaultObjects(created), second, secondGateway)...)
	h.stub.set(brokerAddr(), 1, nil)
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.stub.fail("10.0.1.10:9095", usageScrapeKindConnect)

	h.poll(5)
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("an event after one failed poll: %s", ev)
	default:
	}
	h.poll(10)
	events := map[string]bool{}
	for i := 0; i < 2; i++ {
		select {
		case ev := <-h.recorder.Events:
			events[ev] = true
		default:
		}
	}
	if len(events) != 2 {
		t.Fatalf("after two failed polls on two CRs: %d event(s), want one per CR: %v", len(events), events)
	}
	for _, pod := range []string{"agent-gateway-aaa", "second-gateway-aaa"} {
		found := false
		for ev := range events {
			if strings.Contains(ev, pod) {
				found = true
			}
		}
		if !found {
			t.Errorf("no event names pod %s: %v", pod, events)
		}
	}
}

// A status counter outside the document's bounds, which only a hand patch
// produces, is neither the seed nor the floor: the document is seeded from
// zero once and left alone, instead of being refused and re-seeded on every
// poll; the next move projects the real total over the absurd one.
func TestUsagePoller_AnAbsurdStatusTotalIsNeitherSeedNorFloor(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Status.Usage.ToolExecutionsTotal = usageTotalCeiling + 1
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.set(gatewayAddr(), 0, nil)
	h.poll(5)
	if doc := h.document(); doc.Totals[usageCounterToolExecutions] != 0 || !doc.FirstRecorded.Time.Equal(usageClock(5)) {
		t.Fatalf("seeded from the absurd status: %+v", doc)
	}
	h.poll(10)
	if h.cmWrites != 1 {
		t.Fatalf("%d ConfigMap writes across two quiet polls, want 1: the document was re-seeded", h.cmWrites)
	}
	h.stub.set(brokerAddr(), 15, nil)
	h.poll(15)
	if doc := h.document(); doc.Totals[usageCounterToolExecutions] != 5 || !doc.FirstRecorded.Time.Equal(usageClock(5)) {
		t.Fatalf("after the move: %+v, want 5 counted from the first document", doc)
	}
	if status := h.status(); status.ToolExecutionsTotal != 5 || status.LastActiveTime == nil {
		t.Fatalf("status after the move: %+v, want the real total projected over the absurd one", status)
	}
}

// A poll cut short by a cancelled context records no failure: no streak, no
// log line, no Warning event.
func TestUsagePoller_ACancelledPollRecordsNoFailure(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.stub.fail(brokerAddr(), usageScrapeKindConnect)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	h.clock = usageClock(5)
	if err := h.p.pollAgent(ctx, agent, map[string]bool{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("pollAgent on a cancelled context returned %v, want context.Canceled", err)
	}
	if len(h.p.streaks) != 0 {
		t.Fatalf("a cancelled poll recorded %d streak(s)", len(h.p.streaks))
	}
	h.p.pollAgent(ctx, agent, map[string]bool{}) //nolint:errcheck // the second cancelled poll must still record nothing
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a cancelled poll recorded an event: %s", ev)
	default:
	}
}

// A status lastActiveTime after the poll's clock, a hand patch or a
// predecessor leader's faster clock, is not seeded: the document is written
// once with no last-moved time and left alone, and the next move stamps its
// own poll.
func TestUsagePoller_AFutureStatusTimeIsNotSeeded(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	future := metav1.NewTime(usageClock(5).Add(time.Hour))
	agent.Status.Usage.LastActiveTime = &future
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.set(gatewayAddr(), 0, nil)
	h.poll(5)
	if doc := h.document(); doc.LastMoved != nil {
		t.Fatalf("the future time was seeded: %+v", doc)
	}
	h.poll(10)
	if h.cmWrites != 1 {
		t.Fatalf("%d ConfigMap writes across two quiet polls, want 1: the document was re-seeded", h.cmWrites)
	}
	h.stub.set(brokerAddr(), 15, nil)
	h.poll(15)
	status := h.status()
	if status.ToolExecutionsTotal != 5 || status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(15)) {
		t.Fatalf("status after the move: %+v, want 5 and lastActiveTime at the poll", status)
	}
}

// The Warning event's last sentence follows the failure's class: a connection
// that never produced a response points at the policy and the listener's
// liveness, a response the poller refused points at what is serving the port.
func TestUsagePoller_EventGuidanceFollowsTheFailureClass(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	cases := map[string]struct {
		kind string
		want string
	}{
		"a refused connection":    {usageScrapeKindRefused, usageScrapeConnectGuidance},
		"a timeout":               {usageScrapeKindTimeout, usageScrapeConnectGuidance},
		"a line past the bound":   {usageScrapeKindLine, usageScrapeResponseGuidance},
		"a malformed response":    {usageScrapeKindMalformed, usageScrapeResponseGuidance},
		"a status other than 200": {usageScrapeKindStatus, usageScrapeResponseGuidance},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
			h.stub.set(brokerAddr(), 1, nil)
			h.stub.fail(gatewayAddr(), tc.kind)
			h.poll(5)
			h.poll(10)
			select {
			case ev := <-h.recorder.Events:
				if !strings.Contains(ev, tc.want) {
					t.Fatalf("event = %q, want it to end with %q", ev, tc.want)
				}
				other := usageScrapeResponseGuidance
				if tc.want == usageScrapeResponseGuidance {
					other = usageScrapeConnectGuidance
				}
				if strings.Contains(ev, other) {
					t.Fatalf("event = %q carries the other class's guidance too", ev)
				}
			default:
				t.Fatal("no event after two failed polls")
			}
		})
	}
}

// A rollout on an HA gateway: the new replica is read late, so it is recorded
// against the old pod's marker; when the old pod is terminating it is never
// read again, so it leaves the live set and the new replica's advance is
// counted rather than reset against a marker nothing will move again.
func TestUsagePoller_ATerminatingSiblingSuppressesNoAdvance(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	old := usageGatewayPod("agent-gateway-old", "gw-old", usageTestGatewayIP, created)
	old.Finalizers = []string{"usage-test/hold"}
	fresh := usageGatewayPod("agent-gateway-new", "gw-new", usageTestGatewayB, usageClock(3))
	broker := usageBrokerPod("agent-credential-proxy-bbb", "broker-b", usageTestBrokerIP, created)
	h := newUsageHarness(t, usageTestAgent(created), old, fresh, broker)
	newAddr := usageTestGatewayB + ":9095"
	h.stub.set(brokerAddr(), 0, nil)
	h.stub.set(gatewayAddr(), 100, ptr.To(1.0))
	h.stub.fail(newAddr, usageScrapeKindRefused)
	h.poll(0)
	h.stub.set(gatewayAddr(), 110, ptr.To(1.0))
	h.poll(5)
	// The new replica's first read: recorded against the old pod's marker.
	h.stub.set(gatewayAddr(), 115, ptr.To(1.0))
	h.stub.set(newAddr, 12, ptr.To(9.0))
	h.poll(10)
	if status := h.status(); status.EventsIngestedTotal != 15 {
		t.Fatalf("after the late read: %+v, want 15", status)
	}
	// The old pod is terminating: listed, not read, and no longer live.
	ctx := context.Background()
	if err := h.cl.Delete(ctx, old); err != nil {
		t.Fatal(err)
	}
	h.stub.set(newAddr, 15, ptr.To(9.0))
	h.poll(15)
	if status := h.status(); status.EventsIngestedTotal != 18 {
		t.Fatalf("with the old pod terminating: %+v, want 18: the new replica's advance was reset against a pod that is never read again", status)
	}
	doc := h.document()
	if doc.Pods["gw-old"] != nil {
		t.Fatalf("the terminating pod's entry was kept: %+v", doc.Pods)
	}
	// Gone for good: the new replica carries on alone.
	stored := &corev1.Pod{}
	if err := h.cl.Get(ctx, client.ObjectKeyFromObject(old), stored); err == nil {
		stored.Finalizers = nil
		if err := h.cl.Update(ctx, stored); err != nil {
			t.Fatal(err)
		}
	}
	h.stub.set(newAddr, 18, ptr.To(9.0))
	h.poll(20)
	if status := h.status(); status.EventsIngestedTotal != 21 {
		t.Fatalf("after the old pod left: %+v, want 21", status)
	}
}
