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
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-logr/logr/funcr"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
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
	logf "sigs.k8s.io/controller-runtime/pkg/log"
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
	usageTestA2AIP     = "10.0.0.12"
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
			Containers: []corev1.Container{
				{Name: platformAgentContainerName, Ports: []corev1.ContainerPort{{Name: chatMetricsPortName, ContainerPort: chatMetricsPort}}},
				{Name: "fluent-bit"},
			},
		},
		Status: corev1.PodStatus{
			Phase: corev1.PodRunning, PodIP: ip,
			// The agent container has passed its readiness probe: Hermes is
			// up, and with it the chat listener the poller reads.
			ContainerStatuses: []corev1.ContainerStatus{{Name: platformAgentContainerName, Ready: true}},
		},
	}
}

// usageA2AGatewayPod is a running A2A gateway pod, the one a next-mode
// install runs, with its metrics port on the gateway container.
func usageA2AGatewayPod(name, uid, ip string, created time.Time) *corev1.Pod {
	agent := usageTestAgent(created)
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{
			Name: name, Namespace: usageTestNamespace, UID: types.UID(uid),
			CreationTimestamp: metav1.NewTime(created),
			Labels:            a2aGatewayPodSelector(agent),
		},
		Spec: corev1.PodSpec{
			Containers: []corev1.Container{{Name: a2aGatewayContainerName, Ports: []corev1.ContainerPort{{Name: a2aGatewayMetricsPortName, ContainerPort: a2aGatewayMetricsPort}}}},
		},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: ip},
	}
}

func a2aAddr() string { return usageTestA2AIP + ":9096" }

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
	blocking map[string]bool
	calls    []string
	// hook, when set, is called at the start of every Scrape with the address
	// being scraped, before the blocking wait: a seam for a test that must act
	// (cancel a parent context, say) while a scrape is in flight.
	hook func(addr string)
}

func (s *stubUsageSource) Scrape(ctx context.Context, addr string, counters []string) (usageReading, error) {
	s.mu.Lock()
	s.calls = append(s.calls, addr)
	block := s.blocking[addr]
	err := s.errs[addr]
	reading, ok := s.readings[addr]
	hook := s.hook
	s.mu.Unlock()
	if hook != nil {
		hook(addr)
	}
	// A blocking address models a listener that accepts the dial and never
	// answers: the real source holds here until its own deadline, so the stub
	// holds until the context the poll bounds it with is done.
	if block {
		<-ctx.Done()
		return usageReading{}, ctx.Err()
	}
	if err != nil {
		return usageReading{}, err
	}
	if !ok {
		// The chat listener is on every gateway pod and answers zero until a
		// message arrives, so a test that said nothing about it gets that; the
		// watcher's and the broker's listeners stay "not configured", a refused
		// connection, as before. fail() still models a down chat listener.
		if slices.Equal(counters, usageChatCounters) || slices.Equal(counters, usageA2AChatCounters) {
			return usageReading{Samples: map[string]int64{usageCounterChatMessagesInbound: 0, usageCounterSessionsChat: 0}}, nil
		}
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindConnect}
	}
	// set's sample is the pod group's first counter; setSample names the rest.
	out := usageReading{Samples: map[string]int64{}, StartTime: reading.StartTime, Clusters: reading.Clusters}
	for i, counter := range counters {
		value, named := reading.Samples[counter]
		if !named && i == 0 {
			value = reading.Samples[stubPrimarySample]
		}
		out.Samples[counter] = value
	}
	return out, nil
}

// stubPrimarySample keys the sample set gives, handed out as the first counter
// a scrape asks for.
const stubPrimarySample = ""

func (s *stubUsageSource) set(addr string, sample int64, start *float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.errs, addr)
	reading := s.readings[addr]
	if reading.Samples == nil {
		reading.Samples = map[string]int64{}
	}
	reading.Samples[stubPrimarySample] = sample
	reading.StartTime = start
	s.readings[addr] = reading
}

// setSample gives addr's reading a named counter's sample beside set's.
func (s *stubUsageSource) setSample(addr, counter string, sample int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	reading := s.readings[addr]
	if reading.Samples == nil {
		reading.Samples = map[string]int64{}
	}
	reading.Samples[counter] = sample
	s.readings[addr] = reading
}

// podEntry is the document's entry for the pod uid, where the pod feeds one
// counter; a pod feeding several is indexed by usagePodEntryKey.
func podEntry(doc *usageDocument, uid string) *usagePodEntry {
	var found *usagePodEntry
	for _, entry := range doc.Pods {
		if entry.PodUID == uid {
			if found != nil {
				panic("podEntry on a pod with several counters: " + uid)
			}
			found = entry
		}
	}
	return found
}

// setClusters gives addr's reading the watcher's cluster gauge, which set
// leaves in place; a reading with none models a broker's body.
func (s *stubUsageSource) setClusters(addr string, registered, monitored int64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	reading := s.readings[addr]
	reading.Clusters = &usageClusterGauges{Registered: registered, Monitored: monitored}
	s.readings[addr] = reading
}

func (s *stubUsageSource) fail(addr string, kind string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.errs[addr] = &usageScrapeError{Kind: kind}
}

func (s *stubUsageSource) block(addr string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.blocking[addr] = true
}

func (s *stubUsageSource) unblock(addr string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.blocking, addr)
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

// gaugeValue reads a gauge field as a test compares it: -1 for absent.
func gaugeValue(v *int64) int64 {
	if v == nil {
		return -1
	}
	return *v
}

// expireNewerFieldsRecordForTest ages every newer-field pruning record of the
// CR past the reprobe interval, as the clock would.
func (p *UsageCounterPoller) expireNewerFieldsRecordForTest(agent *agentv1alpha1.PlatformAgent) {
	p.r.prunedNewerFieldsMu.Lock()
	defer p.r.prunedNewerFieldsMu.Unlock()
	for field := range p.r.prunedNewerFields[usagePollKey(agent)] {
		p.r.prunedNewerFields[usagePollKey(agent)][field] = p.now().Add(-2 * usageStatusReprobeInterval)
	}
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
	// cmUpdateErr, when set, is what the Update interceptor returns for a
	// ConfigMap write, standing in for an API server that rejects it.
	cmUpdateErr error
	// cmCreateErr, when set, is what the Create interceptor returns for a
	// ConfigMap write, standing in for an API server that refuses it -- a
	// count/configmaps quota at its cap, or an admission policy that denies it.
	cmCreateErr error
	// pruning makes the fake behave like a served CRD without status.usage:
	// the echo of a status patch comes back with the field empty.
	pruning bool
	// pruningGauges makes the fake behave like a served CRD at a previous
	// schema: status.usage with its first-schema counters, without the fields
	// added later (the cluster gauges, the merged counter, the chat counter, the
	// blocked counter). pruningMerged is the CRD from the release after that:
	// the gauges served, the merged counter not; pruningChat the next: the
	// gauges and the merged counter served, the chat counter not; pruningBlocked
	// the one from the release before this: everything but the blocked counter
	// served. Each strips its own field alone, so a test can tell which record
	// held a patch back.
	pruningGauges     bool
	pruningMerged     bool
	pruningChat       bool
	pruningBlocked    bool
	pruningFailures   bool
	pruningHuman      bool
	pruningErrored    bool
	pruningBackground bool
	pruningFeedback   bool
	pruningClosed     bool
}

func newUsageHarness(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) *usageHarness {
	t.Helper()
	h := &usageHarness{t: t, stub: &stubUsageSource{readings: map[string]usageReading{}, errs: map[string]error{}, blocking: map[string]bool{}}}
	funcs := interceptor.Funcs{
		SubResourcePatch: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			h.patches++
			err := c.SubResource(subResourceName).Patch(ctx, obj, patch, opts...)
			// A pruning CRD drops the fields from what is stored as well as from
			// the echo, so the store is stripped with a second patch the poller
			// never sees, and the echo the poller reads is stripped the same way.
			pa, isAgent := obj.(*agentv1alpha1.PlatformAgent)
			if err == nil && isAgent && (h.pruning || h.pruningGauges || h.pruningMerged || h.pruningChat || h.pruningBlocked || h.pruningFailures || h.pruningHuman || h.pruningErrored || h.pruningBackground || h.pruningFeedback || h.pruningClosed) {
				strip := `{"status":{"usage":{"clustersRegistered":null,"clustersMonitored":null,"remediationsMergedTotal":null,"chatMessagesInboundTotal":null,"toolExecutionsBlockedTotal":null,"brokerFailuresTotal":null,"lastHumanActiveTime":null,"chatTurnsErroredTotal":null,"backgroundTasksGaveUpTotal":null,"negativeFeedbackTotal":null,"remediationsClosedUnmergedTotal":null}}}`
				if h.pruningMerged {
					strip = `{"status":{"usage":{"remediationsMergedTotal":null}}}`
				}
				if h.pruningChat {
					strip = `{"status":{"usage":{"chatMessagesInboundTotal":null}}}`
				}
				if h.pruningBlocked {
					strip = `{"status":{"usage":{"toolExecutionsBlockedTotal":null}}}`
				}
				if h.pruningFailures {
					strip = `{"status":{"usage":{"brokerFailuresTotal":null}}}`
				}
				if h.pruningHuman {
					strip = `{"status":{"usage":{"lastHumanActiveTime":null}}}`
				}
				if h.pruningErrored {
					strip = `{"status":{"usage":{"chatTurnsErroredTotal":null}}}`
				}
				if h.pruningBackground {
					strip = `{"status":{"usage":{"backgroundTasksGaveUpTotal":null}}}`
				}
				if h.pruningFeedback {
					strip = `{"status":{"usage":{"negativeFeedbackTotal":null}}}`
				}
				if h.pruningClosed {
					strip = `{"status":{"usage":{"remediationsClosedUnmergedTotal":null}}}`
				}
				if h.pruning {
					strip = `{"status":{"usage":null}}`
				}
				stored := pa.DeepCopy()
				err = c.SubResource(subResourceName).Patch(ctx, stored, client.RawPatch(types.MergePatchType, []byte(strip)))
			}
			if isAgent && h.pruning {
				pa.Status.Usage = agentv1alpha1.AgentUsageStatus{}
			}
			if isAgent && h.pruningGauges {
				pa.Status.Usage.ClustersRegistered = nil
				pa.Status.Usage.ClustersMonitored = nil
			}
			if isAgent && (h.pruningGauges || h.pruningMerged) {
				pa.Status.Usage.RemediationsMergedTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningChat) {
				pa.Status.Usage.ChatMessagesInboundTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningBlocked) {
				pa.Status.Usage.ToolExecutionsBlockedTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningFailures) {
				pa.Status.Usage.BrokerFailuresTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningHuman) {
				pa.Status.Usage.LastHumanActiveTime = nil
			}
			if isAgent && (h.pruningGauges || h.pruningErrored) {
				pa.Status.Usage.ChatTurnsErroredTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningBackground) {
				pa.Status.Usage.BackgroundTasksGaveUpTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningFeedback) {
				pa.Status.Usage.NegativeFeedbackTotal = 0
			}
			if isAgent && (h.pruningGauges || h.pruningClosed) {
				pa.Status.Usage.RemediationsClosedUnmergedTotal = 0
			}
			return err
		},
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			if _, ok := obj.(*corev1.ConfigMap); ok {
				h.cmWrites++
				if h.cmCreateErr != nil {
					return h.cmCreateErr
				}
			}
			return c.Create(ctx, obj, opts...)
		},
		Update: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
			if _, ok := obj.(*corev1.ConfigMap); ok {
				h.cmWrites++
				if h.cmUpdateErr != nil {
					return h.cmUpdateErr
				}
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
		r:          h.r,
		source:     h.stub,
		now:        func() time.Time { return h.clock },
		pollBudget: func() time.Duration { return usageCountersPollInterval },
		streaks:    map[usageStreakKey]*usageScrapeStreak{},
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

// markUsageConfigMapImmutable sets Immutable on the counters ConfigMap the poller
// created, so a later live read sees it. The branch keys on the object's field,
// not on the 422; the fake client does not enforce immutability, so a test still
// sets cmUpdateErr to make the Update fail as the API server would.
func markUsageConfigMapImmutable(t *testing.T, h *usageHarness, cmName string) {
	t.Helper()
	cm := &corev1.ConfigMap{}
	key := client.ObjectKey{Namespace: usageTestNamespace, Name: cmName}
	if err := h.cl.Get(context.Background(), key, cm); err != nil {
		t.Fatalf("reading the created ConfigMap to mark it immutable: %v", err)
	}
	cm.Immutable = ptr.To(true)
	if err := h.cl.Update(context.Background(), cm); err != nil {
		t.Fatalf("marking the ConfigMap immutable: %v", err)
	}
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
	if _, err := h.p.writeDocument(context.Background(), agent, existing, doc); err != nil {
		h.t.Fatalf("writing the document: %v", err)
	}
	h.cmWrites = 0
}

func gatewayAddr() string { return usageTestGatewayIP + ":9095" }
func chatAddr() string    { return usageTestGatewayIP + ":9097" }
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
	// Four targets: gw-a's watcher and chat listeners, gw-noport's chat listener
	// (only its sidecar lost its port), and the broker. Pending, and terminating,
	// are never targets.
	if len(targets) != 4 {
		t.Fatalf("targets = %+v, want gw-a twice, gw-noport's chat listener and the broker", targets)
	}
	addrs := map[string]string{}
	watcherTargets := 0
	for _, target := range targets {
		if target.port == eventWatcherMetricsPortName {
			watcherTargets++
		}
		for _, counter := range target.counters {
			if target.uid == "gw-a" || target.uid == "broker-b" {
				addrs[counter] = target.addr
			}
		}
	}
	if watcherTargets != 1 {
		t.Errorf("%d watcher targets, want gw-a alone: %+v", watcherTargets, targets)
	}
	if addrs[usageCounterEventsIngested] != gatewayAddr() || addrs[usageCounterChatMessagesInbound] != chatAddr() || addrs[usageCounterToolExecutions] != brokerAddr() || addrs[usageCounterRemediationsProposed] != brokerAddr() {
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
	addrs := map[string]string{}
	for _, target := range targets {
		addrs[target.port] = target.addr
	}
	if addrs[eventWatcherMetricsPortName] != "[fd00::10]:9095" || addrs[chatMetricsPortName] != "[fd00::10]:9097" {
		t.Fatalf("targets = %+v, want [fd00::10]:9095 and [fd00::10]:9097", targets)
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
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4, usageCounterRemediationsProposed: 0, usageCounterRemediationsMerged: 0, usageCounterChatMessagesInbound: 0, usageCounterSessionsTriage: 0, usageCounterSessionsChat: 0, usageCounterToolExecutionsBlocked: 0, usageCounterBrokerFailures: 0, usageCounterChatTurnsErrored: 0, usageCounterBackgroundTasksGaveUp: 0, usageCounterNegativeFeedback: 0, usageCounterRemediationsClosedUnmerged: 0},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			usagePodEntryKey("gw-a", usageCounterEventsIngested):                 {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterEventsIngested, Sample: 4, Marker: moved},
			usagePodEntryKey("gw-a", usageCounterSessionsTriage):                 {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterSessionsTriage, Sample: 0, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterToolExecutions):             {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterToolExecutions, Sample: 10, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterRemediationsProposed):       {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterRemediationsProposed, Sample: 0, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterRemediationsMerged):         {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterRemediationsMerged, Sample: 0, Marker: moved},
			usagePodEntryKey("gw-a", usageCounterChatMessagesInbound):            {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterChatMessagesInbound, Sample: 0, Marker: moved},
			usagePodEntryKey("gw-a", usageCounterSessionsChat):                   {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterSessionsChat, Sample: 0, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterToolExecutionsBlocked):      {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterToolExecutionsBlocked, Sample: 0, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterBrokerFailures):             {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterBrokerFailures, Sample: 0, Marker: moved},
			usagePodEntryKey("gw-a", usageCounterChatTurnsErrored):               {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterChatTurnsErrored, Sample: 0, Marker: moved},
			usagePodEntryKey("gw-a", usageCounterBackgroundTasksGaveUp):          {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterBackgroundTasksGaveUp, Sample: 0, Marker: moved},
			usagePodEntryKey("gw-a", usageCounterNegativeFeedback):               {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterNegativeFeedback, Sample: 0, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterRemediationsClosedUnmerged): {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterRemediationsClosedUnmerged, Sample: 0, Marker: moved},
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
			Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4, usageCounterRemediationsProposed: 0, usageCounterRemediationsMerged: 0, usageCounterChatMessagesInbound: 0, usageCounterToolExecutionsBlocked: 0},
			Pods: map[string]*usagePodEntry{
				usagePodEntryKey("broker-b", usageCounterToolExecutions): {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterToolExecutions, Sample: 10, Marker: stamp},
			},
		}
	}
	cases := []struct {
		name   string
		mutate func(*usageDocument)
	}{
		{"another CR's UID", func(d *usageDocument) { d.AgentUID = "predecessor" }},
		{"a total above the float64-exact ceiling", func(d *usageDocument) { d.Totals[usageCounterToolExecutions] = usageTotalCeiling + 1 }},
		{"a total below the status", func(d *usageDocument) { d.Totals[usageCounterToolExecutions] = 1 }},
		{"a first-recorded time in the future", func(d *usageDocument) { d.FirstRecorded = metav1.NewTime(usageClock(59)) }},
		{"a last-moved time in the future", func(d *usageDocument) { d.LastMoved = ptr.To(metav1.NewTime(usageClock(59))) }},
		{"a last-human-moved time in the future", func(d *usageDocument) { d.LastHumanMoved = ptr.To(metav1.NewTime(usageClock(59))) }},
		{"a first-recorded time before the CR", func(d *usageDocument) { d.FirstRecorded = metav1.NewTime(created.Add(-time.Minute)) }},
		{"a negative sample", func(d *usageDocument) { podEntry(d, "broker-b").Sample = -1 }},
		{"an unknown version", func(d *usageDocument) { d.Version = usageDocumentVersion + 1 }},
		{"an unfolded name that is no counter", func(d *usageDocument) { d.Unfolded = []string{"bogus"} }},
		{"an unfolded counter with a total", func(d *usageDocument) { d.Unfolded = []string{usageCounterToolExecutions} }},
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
			if got.Pods[usagePodEntryKey("broker-b", usageCounterToolExecutions)] == nil || got.Pods[usagePodEntryKey("broker-b", usageCounterToolExecutions)].Sample != 500 || got.Pods[usagePodEntryKey("gw-a", usageCounterEventsIngested)] == nil || got.Pods[usagePodEntryKey("gw-a", usageCounterEventsIngested)].Sample != 300 {
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
	doc := &usageDocument{Version: usageDocumentVersion, AgentUID: usageTestAgentUID, FirstRecorded: stamp, Totals: map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 0, usageCounterRemediationsProposed: 0}, Pods: map[string]*usagePodEntry{}}
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

// sessionsTotal is a first-schema field, so under a served CRD that prunes
// status.usage whole a poll in which sessions alone move writes the probe
// once, records the pruning in the record the Ready writer shares, and keeps
// the ConfigMap current; a CRD at a previous schema, which serves the first
// schema, lands it beside the first three counters.
func TestUsagePoller_SessionsAloneRecordThePrunedStatus(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruning = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setSample(gatewayAddr(), usageCounterSessionsTriage, 10)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	h.stub.setSample(gatewayAddr(), usageCounterSessionsTriage, 12)
	h.poll(10)
	if h.patches != 1 {
		t.Fatalf("%d patches after sessions alone moved, want 1 (the probe)", h.patches)
	}
	if !h.r.usageStatusPruned(agent) {
		t.Fatal("the echo came back without sessionsTotal and no pruning was recorded")
	}
	h.stub.setSample(gatewayAddr(), usageCounterSessionsTriage, 13)
	h.poll(15)
	if h.patches != 1 {
		t.Fatalf("%d patches while the record is fresh, want 1", h.patches)
	}
	if doc := h.document(); usageSessionsTotal(doc.Totals) != 3 {
		t.Fatalf("the ConfigMap fell behind under the pruning CRD: %v", doc.Totals)
	}
	// A CRD at a previous schema serves sessionsTotal with the first three
	// counters: the field lands while the newer fields are pruned.
	h.r.prunedUsageStatus.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-2*usageStatusReprobeInterval))
	h.pruning = false
	h.pruningGauges = true
	h.poll(20)
	if status := h.status(); status.SessionsTotal != 3 {
		t.Fatalf("status under a previous-schema CRD: %+v, want sessionsTotal 3", status)
	}
	if h.r.usageStatusPruned(agent) {
		t.Error("the echo carried sessionsTotal and the record was not cleared")
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
// untouched, costs one log line, and records a Warning event from the second
// failed poll onward, re-recorded every poll with a byte-identical message so
// the event recorder folds the repeats into one live Event rather than one the
// API server's hourly retention would drop; a pod the first poll could not
// scrape, created before the document, is recorded on its next scrape and adds
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
	var first string
	select {
	case first = <-h.recorder.Events:
		if !strings.Contains(first, usageScrapeFailingReason) || !strings.Contains(first, "agent-gateway-aaa") {
			t.Fatalf("event = %q, want %s naming the pod", first, usageScrapeFailingReason)
		}
	default:
		t.Fatal("no event after two failed polls")
	}
	h.poll(15)
	select {
	case ev := <-h.recorder.Events:
		if ev != first {
			t.Fatalf("the third poll's event = %q, want it byte-identical to the second's %q so the recorder folds them", ev, first)
		}
	default:
		t.Fatal("no event after a third failed poll; a standing failure must re-record so its cause outlives the event TTL")
	}
	if doc := h.document(); doc.Pods[usagePodEntryKey("gw-a", usageCounterEventsIngested)] != nil {
		t.Fatalf("an unreadable pod was recorded: %+v", doc.Pods[usagePodEntryKey("gw-a", usageCounterEventsIngested)])
	}

	// Back, created before the document: recorded, adds nothing; then counted.
	h.stub.set(gatewayAddr(), 400, ptr.To(1.0))
	h.poll(20)
	if doc := h.document(); doc.Pods[usagePodEntryKey("gw-a", usageCounterEventsIngested)] == nil || doc.Pods[usagePodEntryKey("gw-a", usageCounterEventsIngested)].Sample != 400 || doc.Totals[usageCounterEventsIngested] != 0 {
		t.Fatalf("after recovery: %+v", doc)
	}
	h.stub.set(gatewayAddr(), 403, ptr.To(1.0))
	h.poll(25)
	if status := h.status(); status.EventsIngestedTotal != 3 {
		t.Fatalf("after the first counted poll: %+v", status)
	}
}

// A standing failure re-records the Warning on every poll from the second on,
// each with a byte-identical message. That identity is what lets the event
// recorder fold them into a single Event whose count rises and whose
// LastTimestamp stays fresh, so the cause outlives the API server's one-hour
// Event retention -- rather than the symptom, a frozen lastActiveTime, standing
// alone the morning after a NetworkPolicy started blocking the scrape.
func TestUsagePoller_AStandingFailureReRecordsAStableWarning(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)

	var msgs []string
	for minute := 5; minute <= 25; minute += 5 {
		h.poll(minute)
		select {
		case ev := <-h.recorder.Events:
			msgs = append(msgs, ev)
		default:
			msgs = append(msgs, "")
		}
	}
	// Poll 1: a log line only, no event. Polls 2..5: an event each, identical.
	if msgs[0] != "" {
		t.Fatalf("an event after one failed poll: %q", msgs[0])
	}
	for i := 1; i < len(msgs); i++ {
		if msgs[i] == "" {
			t.Fatalf("no event after failed poll %d; a standing failure must re-record every poll", i+1)
		}
		if msgs[i] != msgs[1] {
			t.Fatalf("poll %d event = %q, want it byte-identical to poll 2's %q so the recorder folds them", i+1, msgs[i], msgs[1])
		}
	}
}

// Several listeners failing in one poll share a single Warning on the CR, not
// one each. The event recorder keys its spam filter on source+involvedObject
// (the CR), so a Warning per failing pod would drain that bucket and drop the
// causes behind it -- the other pods this poll, and anything the CR records
// before the bucket refills a poll later. The poll records one event naming
// every failing pod instead.
func TestUsagePoller_FailingListenersShareOneWarningPerPoll(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.stub.fail(brokerAddr(), usageScrapeKindConnect)
	h.poll(5)  // first failure of each: a log line only, no event
	h.poll(10) // second: past the streak, the Warning

	var events []string
drain:
	for {
		select {
		case ev := <-h.recorder.Events:
			events = append(events, ev)
		default:
			break drain
		}
	}
	if len(events) != 1 {
		t.Fatalf("got %d Warning(s) for one poll with two failing listeners, want exactly 1: %q", len(events), events)
	}
	for _, pod := range []string{"agent-gateway-aaa", "agent-credential-proxy-bbb"} {
		if !strings.Contains(events[0], pod) {
			t.Errorf("the shared Warning does not name %s: %q", pod, events[0])
		}
	}
	if !strings.Contains(events[0], usageScrapeFailingReason) {
		t.Errorf("the shared Warning is not a %s event: %q", usageScrapeFailingReason, events[0])
	}
}

// A poll is bounded by its budget: a listener that accepts the dial and never
// answers cannot freeze the poll, and the CRs behind it, past the interval. The
// poll returns when its context deadline fires, and the abandonment is quiet: it
// records no failure streak for the pod it abandoned, writes no ConfigMap, and --
// because it was cut on the first CR's hung listener before it reached the second
// -- completes no sweep and so prunes no streak, leaving one that stands behind
// the unreached CR to be read next interval. Without the deadline the blocking
// scrape never returns and the 10s guard below fails the test.
func TestUsagePoller_APollIsBoundedByItsBudget(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	a := usageTestAgent(created) // usage-test/agent sorts first; its gateway hangs
	b := usageTestAgent(created)
	b.Name, b.UID = "agent-zzz", "agent-uid-2" // usage-test/agent-zzz sorts last, unreached this poll
	bGateway := usageGatewayPod("agent-zzz-gateway-aaa", "gw-b", usageTestGatewayB, created)
	bGateway.Labels = map[string]string{"app": "agent-zzz-gateway"}
	h := newUsageHarness(t, a, append(usageDefaultObjects(created), b, bGateway)...)
	const budget = 50 * time.Millisecond
	h.p.pollBudget = func() time.Duration { return budget }
	h.stub.block(gatewayAddr())
	h.stub.set(brokerAddr(), 10, nil)
	// A streak the budget-cut poll must leave alone: its UID is in no CR this poll
	// reaches, so a poll that swept every CR would prune it. This poll is cut on
	// A's hung listener before it reaches B, so the sweep does not complete and
	// nothing is pruned -- stepping the cursor past the budget-eater marks A swept,
	// but B is still outstanding.
	const behindUID = types.UID("behind-the-hung-listener")
	h.p.streaks[usageStreakKey{uid: behindUID, port: eventWatcherMetricsPortName}] = &usageScrapeStreak{count: 1}

	done := make(chan struct{})
	go func() {
		h.poll(5)
		close(done)
	}()
	const guard = 10 * time.Second
	select {
	case <-done:
	case <-time.After(guard):
		t.Fatalf("pollOnce did not return within %s of a listener that never answers; its budget does not bound it", guard)
	}
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a poll cut short by its budget recorded an event: %q", ev)
	default:
	}
	if h.cmWrites != 0 {
		t.Errorf("a poll cut short by its budget wrote the ConfigMap %d time(s), want 0", h.cmWrites)
	}
	gotCount := -1
	if s := h.p.streaks[usageStreakKey{uid: behindUID, port: eventWatcherMetricsPortName}]; s != nil {
		gotCount = s.count
	}
	if len(h.p.streaks) != 1 || gotCount != 1 {
		t.Errorf("after a budget-cut poll: %d streak(s), the pre-seeded one at count %d; want 1 streak at count 1 (the abandoned scrape added a streak, or the cut-short poll pruned the one behind it)", len(h.p.streaks), gotCount)
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
	if doc := h.document(); !doc.Pods[usagePodEntryKey("gw-b", usageCounterEventsIngested)].Marker.Time.Equal(usageClock(10)) {
		t.Fatalf("the reset replica did not take the sibling's marker: %+v", doc.Pods[usagePodEntryKey("gw-b", usageCounterEventsIngested)])
	}
}

// The broker pod's counters come from one body. Each has its own baseline
// entry under the pod; the two this test moves advance together and survive
// the pod's restart, and the proposals move lastActiveTime as a brokered action
// does. The merged counter's test beside it walks the same phases.
func TestUsagePoller_ProposalsCountBesideToolExecutions(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 70, ptr.To(200.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 2)
	h.poll(5)
	if status := h.status(); status.RemediationsProposedTotal != 0 || status.LastActiveTime != nil {
		t.Fatalf("the first poll wrote a counter: %+v", status)
	}
	doc := h.document()
	if len(doc.Pods) != 13 {
		t.Fatalf("the document holds %d entries, want 13 (gateway events, triage sessions, chat, chat sessions, turns errored and cards given up and thumbs-downs; broker tools, refusals, proposals, merged, failures and closes): %+v", len(doc.Pods), doc.Pods)
	}

	// A proposal opened and no command run: the proposals counter moves alone.
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 3)
	h.poll(10)
	status := h.status()
	if status.RemediationsProposedTotal != 1 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("after one proposal: %+v, want 1 proposal and 0 tool executions", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) {
		t.Fatalf("a proposal did not move lastActiveTime: %v", status.LastActiveTime)
	}

	// Both move.
	h.stub.set(brokerAddr(), 75, ptr.To(200.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 4)
	h.poll(15)
	if status := h.status(); status.RemediationsProposedTotal != 2 || status.ToolExecutionsTotal != 5 {
		t.Fatalf("after both moved: %+v, want 2 proposals and 5 tool executions", status)
	}

	// The broker pod restarts in place: a later start time, both counters from
	// zero, both whole samples added, nothing lost.
	h.stub.set(brokerAddr(), 1, ptr.To(300.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 1)
	h.poll(20)
	if status := h.status(); status.RemediationsProposedTotal != 3 || status.ToolExecutionsTotal != 6 {
		t.Fatalf("after the restart: %+v, want 3 proposals and 6 tool executions", status)
	}
}

// The gateway pod carries two listeners, the watcher's and the chat plugin's,
// read as two targets of one pod: the chat counter lands in its own entry,
// summed across replicas, and moves lastActiveTime; a failing chat listener
// has a streak of its own and does not count as the watcher failing.
// Above one replica the gateway container runs leader_elect.py, which starts
// Hermes on the lease holder alone and labels that pod: the standby runs no
// gateway, nothing listens on its chat port, and reading it would put a
// standing Warning on a healthy install. It stays live, keeps its entry, and
// is not read; the watcher's sidecar runs on both and is read on both.
func TestUsagePoller_ChatListenerIsReadFromTheLeaderAlone(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))}}
	uncapUsagePollReplicas(t)
	a := usageGatewayPod("agent-gateway-aaa", "gw-a", usageTestGatewayIP, created)
	a.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	objs := append([]client.Object{a, b}, usageDefaultObjects(created)[1:]...)
	h := newUsageHarness(t, agent, objs...)
	bChat := usageTestGatewayB + ":9097"
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(usageTestGatewayB+":9095", 0, nil)
	h.stub.set(chatAddr(), 10, ptr.To(1.0))
	h.stub.fail(bChat, usageScrapeKindRefused)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	h.poll(10)
	h.poll(15)
	if h.stub.scraped(bChat) {
		t.Fatal("the standby's chat port was read")
	}
	if !h.stub.scraped(usageTestGatewayB + ":9095") {
		t.Fatal("the standby's watcher was not read")
	}
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a Warning on a healthy install: %q", ev)
	default:
	}
	// Leadership moves: the old leader's port is no longer read, the new
	// leader's is. The new leader is a pod older than the document with no
	// entry for the counter, but its Hermes started after the document was
	// first recorded, which its body says, so nothing of its count has been
	// seen and its first sample adds whole: the messages it took between the
	// lease and the first poll are counted. The total keeps both pods'.
	h.stub.set(chatAddr(), 14, ptr.To(1.0))
	h.poll(20)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("after 4 messages on the leader: %d, want 4", got)
	}
	a.Labels = map[string]string{"app": usageTestAgentName + "-gateway"}
	b.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	if err := h.cl.Update(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	if err := h.cl.Update(context.Background(), b); err != nil {
		t.Fatal(err)
	}
	h.stub.set(bChat, 3, ptr.To(float64(usageClock(22).Unix())))
	h.stub.fail(chatAddr(), usageScrapeKindRefused)
	h.poll(25)
	if got := h.status().ChatMessagesInboundTotal; got != 7 {
		t.Fatalf("after leadership moved and 3 messages on the new leader, whose process is younger than the document: %d, want 7", got)
	}
	h.stub.set(bChat, 6, ptr.To(float64(usageClock(22).Unix())))
	h.poll(30)
	if got := h.status().ChatMessagesInboundTotal; got != 10 {
		t.Fatalf("after 3 more messages on the new leader: %d, want 10", got)
	}
	if !h.stub.scraped(chatAddr()) {
		t.Fatal("the old leader was never read")
	}
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a Warning after leadership moved: %q", ev)
	default:
	}
}

// The chat listener binds minutes after the pod is Running, once the
// entrypoint has scaffolded the profiles and Hermes is up: until the agent
// container is Ready the port is not read, so a cold start that is going well
// spends none of the streak, and no Warning is recorded however long it takes.
func TestUsagePoller_ChatListenerIsNotReadBeforeTheAgentContainerIsReady(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	a := usageGatewayPod("agent-gateway-aaa", "gw-a", usageTestGatewayIP, created)
	a.Status.ContainerStatuses = []corev1.ContainerStatus{{Name: platformAgentContainerName, Ready: false}}
	h := newUsageHarness(t, usageTestAgent(created), append([]client.Object{a}, usageDefaultObjects(created)[1:]...)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.fail(chatAddr(), usageScrapeKindRefused)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	h.poll(10)
	h.poll(15)
	if h.stub.scraped(chatAddr()) {
		t.Fatal("the chat port was read before the agent container was ready")
	}
	if !h.stub.scraped(gatewayAddr()) {
		t.Fatal("the watcher was not read")
	}
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a Warning during a cold start: %q", ev)
	default:
	}
	a.Status.ContainerStatuses[0].Ready = true
	if err := h.cl.Status().Update(context.Background(), a); err != nil {
		t.Fatal(err)
	}
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.poll(20)
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	h.poll(25)
	if got := h.status().ChatMessagesInboundTotal; got != 2 {
		t.Fatalf("after the container became ready and 2 messages: %d, want 2", got)
	}
}

// A listener the poll decides not to read has its streak closed: a leader
// read once during its startup (one failed read), then unlabelled as
// leadership moved, must not carry that count to the next time it leads, or
// the next cold start's one failed read would be the second and a Warning.
func TestUsagePoller_AnUnreadListenersStreakIsClosed(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))}}
	uncapUsagePollReplicas(t)
	a := usageGatewayPod("agent-gateway-aaa", "gw-a", usageTestGatewayIP, created)
	a.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, agent, append([]client.Object{a, b}, usageDefaultObjects(created)[1:]...)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(usageTestGatewayB+":9095", 0, nil)
	h.stub.fail(chatAddr(), usageScrapeKindRefused)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5) // the leader's one failed read: streak 1
	// Leadership moves to b before the next poll; a stays live, unread.
	a.Labels = map[string]string{"app": usageTestAgentName + "-gateway"}
	b.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	for _, pod := range []*corev1.Pod{a, b} {
		if err := h.cl.Update(context.Background(), pod); err != nil {
			t.Fatal(err)
		}
	}
	h.stub.set(usageTestGatewayB+":9097", 1, ptr.To(2.0))
	h.poll(10)
	// Leadership returns to a, whose fresh Hermes is again read once before
	// it binds: without the close this is the second failure and a Warning.
	a.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	b.Labels = map[string]string{"app": usageTestAgentName + "-gateway"}
	for _, pod := range []*corev1.Pod{a, b} {
		if err := h.cl.Update(context.Background(), pod); err != nil {
			t.Fatal(err)
		}
	}
	h.poll(15)
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a stale streak made one failed read a Warning: %q", ev)
	default:
	}
	// The same listener failing on the next poll too is a real streak.
	h.poll(20)
	select {
	case <-h.recorder.Events:
	default:
		t.Fatal("two consecutive failed reads of the leader recorded no Warning")
	}
}

// The young-process rule needs the body to say so: a never-read pod older
// than the document whose body carries no start time, or a start before the
// document, is baselined as before.
func TestUsagePoller_AnOldProcessOnANeverReadPodIsBaselined(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))}}
	uncapUsagePollReplicas(t)
	a := usageGatewayPod("agent-gateway-aaa", "gw-a", usageTestGatewayIP, created)
	a.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, agent, append([]client.Object{a, b}, usageDefaultObjects(created)[1:]...)...)
	bChat := usageTestGatewayB + ":9097"
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(usageTestGatewayB+":9095", 0, nil)
	h.stub.set(chatAddr(), 10, ptr.To(1.0))
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	a.Labels = map[string]string{"app": usageTestAgentName + "-gateway"}
	b.Labels[gatewayLeaderLabel] = gatewayLeaderLabelValue
	for _, pod := range []*corev1.Pod{a, b} {
		if err := h.cl.Update(context.Background(), pod); err != nil {
			t.Fatal(err)
		}
	}
	h.stub.set(bChat, 3, nil) // no start time in the body
	h.stub.fail(chatAddr(), usageScrapeKindRefused)
	h.poll(10)
	if got := h.status().ChatMessagesInboundTotal; got != 0 {
		t.Fatalf("a body without a start time added its whole sample: %d, want 0", got)
	}
}

// On a next-mode install the A2A gateway owns Chat ingress and counts the
// messages it accepts; the poller reads its pod as a fourth target into the
// same chat counter, with an entry and a streak of its own, beside the
// plugin's entry on the gateway pod (which counts nothing there: a turn the
// bridge hands Hermes' API server never reaches the dispatch hook).
func TestUsagePoller_ChatMessagesCountFromTheA2AGatewayToo(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	a2a := usageA2AGatewayPod("agent-a2a-gateway-ccc", "a2a-c", usageTestA2AIP, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), a2a)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(a2aAddr(), 10, ptr.To(1.0))
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	if doc := h.document(); doc.Pods[usagePodEntryKey("a2a-c", usageCounterChatMessagesInbound)] == nil {
		t.Fatalf("the A2A gateway's chat entry was not recorded: %v", doc.Pods)
	}
	h.stub.set(chatAddr(), 2, ptr.To(1.0))
	h.stub.set(a2aAddr(), 14, ptr.To(1.0))
	h.poll(10)
	if got := h.status().ChatMessagesInboundTotal; got != 5 {
		t.Fatalf("after 1 API-port message and 4 A2A gateway messages: %d, want 5", got)
	}
	// The A2A gateway's listener failing is its own streak and Warning, and
	// the plugin's reading keeps landing.
	h.stub.fail(a2aAddr(), usageScrapeKindRefused)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.poll(15)
	h.poll(20)
	if got := h.status().ChatMessagesInboundTotal; got != 6 {
		t.Fatalf("with the A2A gateway unreadable: %d, want 6", got)
	}
	select {
	case ev := <-h.recorder.Events:
		if !strings.Contains(ev, "agent-a2a-gateway-ccc") || !strings.Contains(ev, "chatMessagesInboundTotal") {
			t.Fatalf("event = %q, want it to name the A2A gateway pod and the chat field", ev)
		}
	default:
		t.Fatal("no Warning after two failed reads of the A2A gateway")
	}
}

// status.usage.sessionsTotal is the sum of two document counters: the
// watcher's triage sessions, read from the gateway pods' watcher listener, and
// the chat sessions, read from the leader's chat listener and from the A2A
// gateway on a next install. Either half moves lastActiveTime, and a failing
// chat listener's Warning names the status field and not a document counter.
func TestUsagePoller_SessionsTotalIsTheTriageSessionsPlusTheChatSessions(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	a2a := usageA2AGatewayPod("agent-a2a-gateway-ccc", "a2a-c", usageTestA2AIP, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), a2a)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setSample(gatewayAddr(), usageCounterSessionsTriage, 10)
	h.stub.set(chatAddr(), 0, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterSessionsChat, 4)
	h.stub.set(a2aAddr(), 0, ptr.To(1.0))
	h.stub.setSample(a2aAddr(), usageCounterSessionsChat, 20)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	if status := h.status(); status.SessionsTotal != 0 {
		t.Fatalf("first poll wrote a counter: %+v", status)
	}
	doc := h.document()
	for _, key := range []string{usagePodEntryKey("gw-a", usageCounterSessionsTriage), usagePodEntryKey("gw-a", usageCounterSessionsChat), usagePodEntryKey("a2a-c", usageCounterSessionsChat)} {
		if doc.Pods[key] == nil {
			t.Fatalf("entry %s was not recorded: %v", key, doc.Pods)
		}
	}
	h.stub.setSample(gatewayAddr(), usageCounterSessionsTriage, 12)
	h.stub.setSample(chatAddr(), usageCounterSessionsChat, 5)
	h.stub.setSample(a2aAddr(), usageCounterSessionsChat, 23)
	h.poll(10)
	status := h.status()
	if status.SessionsTotal != 6 || status.ChatMessagesInboundTotal != 0 || status.EventsIngestedTotal != 0 {
		t.Fatalf("after 2 triage, 1 chat and 3 A2A sessions: %+v, want sessionsTotal 6 and nothing else moved", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) {
		t.Fatalf("a session did not move lastActiveTime: %v", status.LastActiveTime)
	}
	// The triage half alone moves the field too.
	h.stub.setSample(gatewayAddr(), usageCounterSessionsTriage, 13)
	h.poll(15)
	if got := h.status().SessionsTotal; got != 7 {
		t.Fatalf("after one more triage session: %d, want 7", got)
	}
	// A failing chat listener names the status field in its Warning, not the
	// document's name for the half it feeds.
	h.stub.fail(chatAddr(), usageScrapeKindRefused)
	h.poll(20)
	h.poll(25)
	select {
	case ev := <-h.recorder.Events:
		if !strings.Contains(ev, "status.usage.sessionsTotal") || strings.Contains(ev, usageCounterSessionsChat) {
			t.Fatalf("the Warning does not name the status field, or names a document counter: %s", ev)
		}
	default:
		t.Fatal("no Warning after two failed reads of the chat listener")
	}
}

// The status value of sessionsTotal is one number for two document counters:
// it seeds the chat half, and the read-back bound holds the pair's sum, not
// each half, to it.
func TestUsagePoller_TheSessionsStatusSeedsTheChatHalfAndFloorsTheSum(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Status.Usage.SessionsTotal = 8
	seed := usageStatusSeed(agent, usageClock(1))
	if seed.Totals[usageCounterSessionsChat] != 8 || seed.Totals[usageCounterSessionsTriage] != 0 {
		t.Fatalf("seed = %v, want the status in the chat half and zero in the triage half", seed.Totals)
	}
	stamp := metav1.NewTime(usageClock(1))
	cm := &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{OwnerReferences: []metav1.OwnerReference{{UID: agent.UID}}}}
	doc := func(triage, chat int64) *usageDocument {
		totals := usageZeroTotals()
		totals[usageCounterSessionsTriage] = triage
		totals[usageCounterSessionsChat] = chat
		return &usageDocument{Version: usageDocumentVersion, AgentUID: string(agent.UID), FirstRecorded: stamp, Totals: totals, Pods: map[string]*usagePodEntry{}}
	}
	if reason := usageDocumentInvalid(doc(5, 3), cm, agent, usageClock(2)); reason != "" {
		t.Fatalf("a pair summing to the status was refused: %s", reason)
	}
	if reason := usageDocumentInvalid(doc(5, 2), cm, agent, usageClock(2)); reason == "" {
		t.Fatal("a pair summing below the status was accepted")
	}
}

func TestUsagePoller_ChatMessagesCountFromTheGatewaysSecondListener(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), b)...)
	bChat := usageTestGatewayB + ":9097"
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 1, 1)
	h.stub.set(usageTestGatewayB+":9095", 0, nil)
	h.stub.setClusters(usageTestGatewayB+":9095", 1, 1)
	h.stub.set(chatAddr(), 10, ptr.To(1.0))
	h.stub.set(bChat, 20, ptr.To(1.0))
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	if status := h.status(); status.ChatMessagesInboundTotal != 0 {
		t.Fatalf("first poll wrote a counter: %+v", status)
	}
	if doc := h.document(); doc.Pods[usagePodEntryKey("gw-a", usageCounterChatMessagesInbound)] == nil || doc.Pods[usagePodEntryKey("gw-b", usageCounterChatMessagesInbound)] == nil {
		t.Fatalf("the chat entries were not recorded: %v", doc.Pods)
	}
	// At one replica every gateway pod is read, and leadership moves between
	// pods above it, so each pod's own count is summed: the deltas sum.
	h.stub.set(chatAddr(), 13, ptr.To(1.0))
	h.stub.set(bChat, 22, ptr.To(1.0))
	h.poll(10)
	status := h.status()
	if status.ChatMessagesInboundTotal != 5 || status.EventsIngestedTotal != 0 {
		t.Fatalf("after messages on both replicas: %+v, want 5 inbound and 0 events", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) {
		t.Fatalf("a chat message did not move lastActiveTime: %v", status.LastActiveTime)
	}
	// The chat listener of one replica fails for the streak: the watcher's
	// reading is unaffected, the gauges hold, and the Warning names the chat
	// field, not the watcher's.
	h.stub.fail(bChat, usageScrapeKindRefused)
	h.poll(15)
	h.poll(20)
	if status := h.status(); status.ClustersRegistered == nil || *status.ClustersRegistered != 1 {
		t.Fatalf("a failing chat listener cleared the gauges: %+v", status)
	}
	select {
	case ev := <-h.recorder.Events:
		if !strings.Contains(ev, "status.usage.chatMessagesInboundTotal") || strings.Contains(ev, "eventsIngestedTotal") {
			t.Fatalf("the Warning names the wrong field: %s", ev)
		}
	default:
		t.Fatal("no Warning was recorded for the failing chat listener")
	}
}

// The merged-proposals counter lands beside the proposals counter from the
// same broker body, and moves lastActiveTime.
func TestUsagePoller_MergedProposalsCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, ptr.To(200.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 1)
	h.poll(5)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 3)
	h.poll(10)
	status := h.status()
	if status.RemediationsMergedTotal != 2 || status.RemediationsProposedTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("after two merges: %+v, want 2 merged and nothing else", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) {
		t.Fatalf("a merge did not move lastActiveTime: %v", status.LastActiveTime)
	}
}

// The broker's refused outcomes, blocked, busy and abandoned, count into the
// blocked counter and nowhere else, and a refusal is activity.
func TestUsagePoller_BlockedToolExecutionsCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, ptr.To(200.0))
	h.stub.setSample(brokerAddr(), usageCounterToolExecutionsBlocked, 1)
	h.poll(5)
	h.stub.setSample(brokerAddr(), usageCounterToolExecutionsBlocked, 4)
	h.poll(10)
	status := h.status()
	if status.ToolExecutionsBlockedTotal != 3 || status.ToolExecutionsTotal != 0 || status.RemediationsProposedTotal != 0 {
		t.Fatalf("after three refusals: %+v, want 3 blocked and nothing else", status)
	}
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) {
		t.Fatalf("a refusal did not move lastActiveTime: %v", status.LastActiveTime)
	}
	doc := h.document()
	if doc.Totals[usageCounterToolExecutionsBlocked] != 3 {
		t.Fatalf("document totals = %v, want 3 blocked", doc.Totals)
	}
}

// A served CRD from the release before this one serves the chat counter and
// prunes the blocked counter: its absence is recorded against it alone, the
// others keep landing, and a poll with it alone behind does not re-patch.
func TestUsagePoller_AChatServingCRDWithoutTheBlockedCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningBlocked = true
	var mu sync.Mutex
	var lines []string
	ctx := logf.IntoContext(context.Background(), funcr.New(func(_, args string) {
		mu.Lock()
		lines = append(lines, args)
		mu.Unlock()
	}, funcr.Options{}))
	pollAt := func(minute int) {
		h.clock = usageClock(minute)
		h.p.pollOnce(ctx)
	}
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(brokerAddr(), usageCounterToolExecutionsBlocked, 1)
	pollAt(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(brokerAddr(), usageCounterToolExecutionsBlocked, 2)
	pollAt(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.ToolExecutionsBlockedTotal != 0 || status.ClustersRegistered == nil {
		t.Fatalf("after a message and a refusal: %+v, want 2 messages, no blocked counter, the gauges", status)
	}
	if h.p.newerFieldPruned(agent, usageNewerFieldGauges) || h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageCounterToolExecutionsBlocked) {
		t.Fatal("the blocked counter's absence was not recorded against the blocked counter alone")
	}
	patches := h.patches
	pollAt(12)
	if h.patches != patches {
		t.Fatalf("a poll with the blocked counter alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	pollAt(14)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("a message waited on the blocked counter's record: %d, want 4", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the blocked counter's absence was recorded as the counters' pruning")
	}
	mu.Lock()
	defer mu.Unlock()
	named := 0
	for _, l := range lines {
		if strings.Contains(l, "does not serve a status.usage field") {
			named++
			if !strings.Contains(l, `"field"="toolExecutionsBlockedTotal"`) {
				t.Fatalf("the log named the wrong field: %s", l)
			}
		}
	}
	if named != 1 {
		t.Fatalf("%d pruning log lines, want 1 naming toolExecutionsBlockedTotal", named)
	}
}

// The broker's failure family counts into brokerFailuresTotal and nowhere
// else, and a failure alone is not activity: lastActiveTime stays.
func TestUsagePoller_BrokerFailuresCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, ptr.To(200.0))
	h.stub.setSample(brokerAddr(), usageCounterBrokerFailures, 1)
	h.poll(5)
	h.stub.setSample(brokerAddr(), usageCounterBrokerFailures, 4)
	h.poll(10)
	status := h.status()
	if status.BrokerFailuresTotal != 3 || status.ToolExecutionsTotal != 0 || status.RemediationsProposedTotal != 0 {
		t.Fatalf("after three failures: %+v, want 3 failures and nothing else", status)
	}
	if status.LastActiveTime != nil {
		t.Fatalf("a failure alone moved lastActiveTime: %v", status.LastActiveTime)
	}
	if doc := h.document(); doc.Totals[usageCounterBrokerFailures] != 3 {
		t.Fatalf("document totals = %v, want 3 failures", doc.Totals)
	}
}

// A served CRD from the release before this one prunes brokerFailuresTotal
// alone: its absence is recorded against it, the others keep landing, and a
// poll with it alone behind does not re-patch while the record is fresh.
func TestUsagePoller_AChatServingCRDWithoutTheFailuresCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningFailures = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(brokerAddr(), usageCounterBrokerFailures, 1)
	h.poll(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(brokerAddr(), usageCounterBrokerFailures, 2)
	h.poll(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.BrokerFailuresTotal != 0 {
		t.Fatalf("after a message and a failure: %+v, want 2 messages and no failures counter", status)
	}
	if h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageCounterBrokerFailures) {
		t.Fatal("the failures counter's absence was not recorded against it alone")
	}
	patches := h.patches
	h.poll(12)
	if h.patches != patches {
		t.Fatal("a poll with the failures counter alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	h.poll(14)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("a message waited on the failures counter's record: %d, want 4", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the failures counter's absence was recorded as the counters' pruning")
	}
}

// lastHumanActiveTime follows the counters a person moves and nothing else:
// a brokered command moves lastActiveTime alone, a chat message moves both.
func TestUsagePoller_LastHumanActiveTimeFollowsHumanCountersAlone(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, ptr.To(200.0))
	h.poll(5)
	h.stub.set(brokerAddr(), 14, ptr.To(200.0))
	h.poll(10)
	status := h.status()
	if status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(10)) || status.LastHumanActiveTime != nil {
		t.Fatalf("after a command: lastActiveTime=%v lastHumanActiveTime=%v, want 10 and absent", status.LastActiveTime, status.LastHumanActiveTime)
	}
	h.stub.set(chatAddr(), 2, ptr.To(1.0))
	h.poll(15)
	status = h.status()
	if status.LastHumanActiveTime == nil || !status.LastHumanActiveTime.Time.Equal(usageClock(15)) || !status.LastActiveTime.Time.Equal(usageClock(15)) {
		t.Fatalf("after a message: lastActiveTime=%v lastHumanActiveTime=%v, want both at 15", status.LastActiveTime, status.LastHumanActiveTime)
	}
	h.stub.set(brokerAddr(), 20, ptr.To(200.0))
	h.poll(20)
	status = h.status()
	if !status.LastHumanActiveTime.Time.Equal(usageClock(15)) || !status.LastActiveTime.Time.Equal(usageClock(20)) {
		t.Fatalf("after another command: lastActiveTime=%v lastHumanActiveTime=%v, want 20 and 15", status.LastActiveTime, status.LastHumanActiveTime)
	}
	if doc := h.document(); doc.LastHumanMoved == nil || !doc.LastHumanMoved.Time.Equal(usageClock(15)) {
		t.Fatalf("document lastHumanMoved = %v, want 15", doc.LastHumanMoved)
	}
}

// A served CRD from the release before this one prunes lastHumanActiveTime
// alone: its absence is recorded against it, the counters keep landing, and a
// poll with it alone behind does not re-patch while the record is fresh.
func TestUsagePoller_AChatServingCRDWithoutLastHumanActiveTimeKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningHuman = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.poll(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.poll(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.LastHumanActiveTime != nil || status.LastActiveTime == nil {
		t.Fatalf("after a message: %+v, want 2 messages, lastActiveTime, no lastHumanActiveTime", status)
	}
	if h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageNewerFieldLastHumanActive) {
		t.Fatal("the time's absence was not recorded against lastHumanActiveTime alone")
	}
	patches := h.patches
	h.poll(12)
	if h.patches != patches {
		t.Fatal("a poll with lastHumanActiveTime alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(brokerAddr(), 11, nil)
	h.poll(14)
	if got := h.status().ToolExecutionsTotal; got != 1 {
		t.Fatalf("a command waited on the time's record: %d, want 1", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the time's absence was recorded as the counters' pruning")
	}
}

// A lastHumanActiveTime the operator never writes -- in the future, before
// the CR existed, after lastActiveTime, or without a lastActiveTime at all --
// is not seeded into a fresh document, as a future lastActiveTime is not.
func TestUsagePoller_AnOutOfBoundsHumanTimeIsNotSeeded(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	last := metav1.NewTime(usageClock(2))
	cases := []struct {
		name       string
		active     *metav1.Time
		human      metav1.Time
		wantSeeded bool
	}{
		{"in the future", &last, metav1.NewTime(usageClock(5).Add(time.Hour)), false},
		{"before the CR", &last, metav1.NewTime(created.Add(-time.Minute)), false},
		{"after lastActiveTime", &last, metav1.NewTime(usageClock(3)), false},
		{"without a lastActiveTime", nil, metav1.NewTime(usageClock(2)), false},
		{"inside the bounds", &last, metav1.NewTime(usageClock(1)), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			agent := usageTestAgent(created)
			agent.Status.Usage.LastActiveTime = tc.active
			human := tc.human
			agent.Status.Usage.LastHumanActiveTime = &human
			h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
			h.stub.set(gatewayAddr(), 0, nil)
			h.stub.set(brokerAddr(), 0, nil)
			h.poll(5)
			doc := h.document()
			if seeded := doc.LastHumanMoved != nil; seeded != tc.wantSeeded {
				t.Fatalf("seeded=%v, want %v: %+v", seeded, tc.wantSeeded, doc.LastHumanMoved)
			}
		})
	}
}

// A moved time outside the CR's life is refused as a first-recorded time is:
// a zero (Go renders a zero metav1.Time as null, so only a hand-written
// document carries one, which is why it is planted in the raw JSON here; admitted,
// it would patch the status field as null and the echo would record a pruning
// CRD), a time before the CR was created, or a human time after the last
// move of any kind, which one stamp setting both cannot produce.
func TestUsagePoller_ReseedsADocumentWithATimeOutsideTheCRsLife(t *testing.T) {
	cases := []struct{ name, field, value string }{
		{"a zero lastMoved", "lastMoved", "0001-01-01T00:00:00Z"},
		{"a zero lastHumanMoved", "lastHumanMoved", "0001-01-01T00:00:00Z"},
		{"a lastMoved before the CR", "lastMoved", "1970-01-01T00:00:01Z"},
		{"a lastHumanMoved before the CR", "lastHumanMoved", "1970-01-01T00:00:01Z"},
		{"a lastHumanMoved after lastMoved", "lastHumanMoved", usageClock(9).UTC().Format(time.RFC3339)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			field := tc.field
			created := usageClock(0).Add(-time.Hour)
			h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
			h.stub.set(gatewayAddr(), 0, nil)
			h.stub.set(brokerAddr(), 10, nil)
			h.poll(3)
			// A command, so the document carries a lastMoved of minute 5.
			h.stub.set(brokerAddr(), 11, nil)
			h.poll(5)
			cm := &corev1.ConfigMap{}
			key := client.ObjectKey{Namespace: usageTestNamespace, Name: usageTestAgentName + usageCountersConfigMapSuffix}
			if err := h.cl.Get(context.Background(), key, cm); err != nil {
				t.Fatal(err)
			}
			var raw map[string]any
			if err := json.Unmarshal([]byte(cm.Data[usageCountersDocumentKey]), &raw); err != nil {
				t.Fatal(err)
			}
			raw[field] = tc.value
			b, err := json.Marshal(raw)
			if err != nil {
				t.Fatal(err)
			}
			cm.Data[usageCountersDocumentKey] = string(b)
			if err := h.cl.Update(context.Background(), cm); err != nil {
				t.Fatal(err)
			}
			h.poll(10)
			doc := h.document()
			if !doc.FirstRecorded.Time.Equal(usageClock(10)) {
				t.Fatalf("a document with %s was not re-seeded: firstRecorded=%v", tc.name, doc.FirstRecorded)
			}
			if doc.LastHumanMoved != nil || (doc.LastMoved != nil && !doc.LastMoved.Time.Equal(usageClock(5))) {
				t.Fatalf("the planted time survived the re-seed: lastMoved=%v lastHumanMoved=%v", doc.LastMoved, doc.LastHumanMoved)
			}
		})
	}
}

// Failed turns count into chatTurnsErroredTotal from the chat listener and
// nowhere else, and a failed turn alone is not activity.
func TestUsagePoller_ChatTurnsErroredCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, nil)
	h.stub.set(chatAddr(), 0, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterChatTurnsErrored, 1)
	h.poll(5)
	h.stub.setSample(chatAddr(), usageCounterChatTurnsErrored, 4)
	h.poll(10)
	status := h.status()
	if status.ChatTurnsErroredTotal != 3 || status.ChatMessagesInboundTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("after three failed turns: %+v, want 3 and nothing else", status)
	}
	if status.LastActiveTime != nil {
		t.Fatalf("a failed turn alone moved lastActiveTime: %v", status.LastActiveTime)
	}
	if doc := h.document(); doc.Totals[usageCounterChatTurnsErrored] != 3 {
		t.Fatalf("document totals = %v, want 3", doc.Totals)
	}
}

// A served CRD from the release before this one prunes chatTurnsErroredTotal
// alone: its absence is recorded against it, the others keep landing, and a
// poll with it alone behind does not re-patch while the record is fresh.
func TestUsagePoller_AChatServingCRDWithoutTheErroredCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningErrored = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(chatAddr(), usageCounterChatTurnsErrored, 1)
	h.poll(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterChatTurnsErrored, 2)
	h.poll(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.ChatTurnsErroredTotal != 0 {
		t.Fatalf("after a message and a failed turn: %+v, want 2 messages and no errored counter", status)
	}
	if h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageCounterChatTurnsErrored) {
		t.Fatal("the errored counter's absence was not recorded against it alone")
	}
	patches := h.patches
	h.poll(12)
	if h.patches != patches {
		t.Fatal("a poll with the errored counter alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	h.poll(14)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("a message waited on the errored counter's record: %d, want 4", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the errored counter's absence was recorded as the counters' pruning")
	}
}

// The board's gave_up cards count into backgroundTasksGaveUpTotal from the chat
// listener and nowhere else, and a card giving up is not activity.
func TestUsagePoller_BackgroundTasksGaveUpCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, nil)
	h.stub.set(chatAddr(), 0, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterBackgroundTasksGaveUp, 1)
	h.poll(5)
	h.stub.setSample(chatAddr(), usageCounterBackgroundTasksGaveUp, 4)
	h.poll(10)
	status := h.status()
	if status.BackgroundTasksGaveUpTotal != 3 || status.ChatMessagesInboundTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("after three cards gave up: %+v, want 3 and nothing else", status)
	}
	if status.LastActiveTime != nil {
		t.Fatalf("a card giving up moved lastActiveTime: %v", status.LastActiveTime)
	}
	if doc := h.document(); doc.Totals[usageCounterBackgroundTasksGaveUp] != 3 {
		t.Fatalf("document totals = %v, want 3", doc.Totals)
	}
}

// A served CRD from the release before this one prunes backgroundTasksGaveUpTotal
// alone: its absence is recorded against it, the others keep landing, and a
// poll with it alone behind does not re-patch while the record is fresh.
func TestUsagePoller_AChatServingCRDWithoutTheBackgroundTasksCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningBackground = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(chatAddr(), usageCounterBackgroundTasksGaveUp, 1)
	h.poll(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterBackgroundTasksGaveUp, 2)
	h.poll(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.BackgroundTasksGaveUpTotal != 0 {
		t.Fatalf("after a message and a card: %+v, want 2 messages and no background-tasks counter", status)
	}
	if h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageCounterBackgroundTasksGaveUp) {
		t.Fatal("the background-tasks counter's absence was not recorded against it alone")
	}
	patches := h.patches
	h.poll(12)
	if h.patches != patches {
		t.Fatal("a poll with the background-tasks counter alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	h.poll(14)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("a message waited on the kanban counter's record: %d, want 4", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the background-tasks counter's absence was recorded as the counters' pruning")
	}
}

// Thumbs-downs count into negativeFeedbackTotal from the chat listener and
// nowhere else, and a thumbs-down alone is not activity.
func TestUsagePoller_NegativeFeedbackCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, nil)
	h.stub.set(chatAddr(), 0, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterNegativeFeedback, 1)
	h.poll(5)
	h.stub.setSample(chatAddr(), usageCounterNegativeFeedback, 4)
	h.poll(10)
	status := h.status()
	if status.NegativeFeedbackTotal != 3 || status.ChatMessagesInboundTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("after three thumbs-downs: %+v, want 3 and nothing else", status)
	}
	if status.LastActiveTime != nil {
		t.Fatalf("a thumbs-down alone moved lastActiveTime: %v", status.LastActiveTime)
	}
	if doc := h.document(); doc.Totals[usageCounterNegativeFeedback] != 3 {
		t.Fatalf("document totals = %v, want 3", doc.Totals)
	}
}

// A served CRD from the release before this one prunes negativeFeedbackTotal
// alone: its absence is recorded against it, the others keep landing, and a
// poll with it alone behind does not re-patch while the record is fresh.
func TestUsagePoller_AChatServingCRDWithoutTheFeedbackCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningFeedback = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(chatAddr(), usageCounterNegativeFeedback, 1)
	h.poll(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(chatAddr(), usageCounterNegativeFeedback, 2)
	h.poll(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.NegativeFeedbackTotal != 0 {
		t.Fatalf("after a message and a thumbs-down: %+v, want 2 messages and no feedback counter", status)
	}
	if h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageCounterNegativeFeedback) {
		t.Fatal("the feedback counter's absence was not recorded against it alone")
	}
	patches := h.patches
	h.poll(12)
	if h.patches != patches {
		t.Fatal("a poll with the feedback counter alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	h.poll(14)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("a message waited on the feedback counter's record: %d, want 4", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the feedback counter's absence was recorded as the counters' pruning")
	}
}

// The broker's closed-unmerged series counts into remediationsClosedUnmergedTotal
// and nowhere else, and a person closing a proposal is not activity: lastActiveTime stays.
func TestUsagePoller_RemediationsClosedUnmergedCount(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(brokerAddr(), 0, ptr.To(200.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsClosedUnmerged, 1)
	h.poll(5)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsClosedUnmerged, 4)
	h.poll(10)
	status := h.status()
	if status.RemediationsClosedUnmergedTotal != 3 || status.ToolExecutionsTotal != 0 || status.RemediationsProposedTotal != 0 {
		t.Fatalf("after three closes: %+v, want 3 closes and nothing else", status)
	}
	if status.LastActiveTime != nil {
		t.Fatalf("a close alone moved lastActiveTime: %v", status.LastActiveTime)
	}
	if doc := h.document(); doc.Totals[usageCounterRemediationsClosedUnmerged] != 3 {
		t.Fatalf("document totals = %v, want 3 closes", doc.Totals)
	}
}

// A served CRD from the release before this one prunes remediationsClosedUnmergedTotal
// alone: its absence is recorded against it, the others keep landing, and a
// poll with it alone behind does not re-patch while the record is fresh.
func TestUsagePoller_AChatServingCRDWithoutTheClosedCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningClosed = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsClosedUnmerged, 1)
	h.poll(5)
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsClosedUnmerged, 2)
	h.poll(10)
	if status := h.status(); status.ChatMessagesInboundTotal != 2 || status.RemediationsClosedUnmergedTotal != 0 {
		t.Fatalf("after a message and a close: %+v, want 2 messages and no closed counter", status)
	}
	if h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) || !h.p.newerFieldPruned(agent, usageCounterRemediationsClosedUnmerged) {
		t.Fatal("the closed counter's absence was not recorded against it alone")
	}
	patches := h.patches
	h.poll(12)
	if h.patches != patches {
		t.Fatal("a poll with the closed counter alone behind re-patched the status while its record was fresh")
	}
	h.stub.set(chatAddr(), 5, ptr.To(1.0))
	h.poll(14)
	if got := h.status().ChatMessagesInboundTotal; got != 4 {
		t.Fatalf("a message waited on the closed counter's record: %d, want 4", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the closed counter's absence was recorded as the counters' pruning")
	}
}

// A current-layout document that predates a counter is written back with the
// counter added even when the poll read nothing that changed it: the broker
// unread, the gateway level. Otherwise it would be re-migrated and re-logged
// every poll until the broker is read.
func TestUsagePoller_ADocumentWithoutANewerCounterIsWrittenWhenNothingElseMoved(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	moved := metav1.NewTime(usageClock(3))
	h.writeDocument(&usageDocument{
		Version:       usageDocumentVersion,
		AgentUID:      usageTestAgentUID,
		FirstRecorded: metav1.NewTime(usageClock(1)),
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4, usageCounterRemediationsProposed: 1},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			usagePodEntryKey("gw-a", usageCounterEventsIngested):           {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterEventsIngested, Sample: 40, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterToolExecutions):       {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterToolExecutions, Sample: 100, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterRemediationsProposed): {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterRemediationsProposed, Sample: 1, Marker: moved},
		},
	})
	h.cmWrites = 0
	h.stub.set(gatewayAddr(), 40, nil)
	h.stub.fail(brokerAddr(), usageScrapeKindRefused)
	h.poll(10)
	if h.cmWrites != 1 {
		t.Fatalf("%d ConfigMap writes on the migrating poll, want 1: the added counter was not written back", h.cmWrites)
	}
	doc := h.document()
	if _, ok := doc.Totals[usageCounterRemediationsMerged]; !ok || !slices.Contains(doc.Unfolded, usageCounterRemediationsMerged) {
		t.Fatalf("the written document lacks the counter or its unfolded mark: totals %v, unfolded %v", doc.Totals, doc.Unfolded)
	}
	h.poll(15)
	if h.cmWrites != 1 {
		t.Fatalf("%d ConfigMap writes after a quiet poll, want 1: the document was migrated again", h.cmWrites)
	}
}

// A current-layout document that predates a counter, as the previous release
// wrote it, gains the counter's total on read rather than being re-seeded.
func TestUsagePoller_ADocumentWithoutANewerCounterKeepsItsTotals(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	moved := metav1.NewTime(usageClock(3))
	h.writeDocument(&usageDocument{
		Version:       usageDocumentVersion,
		AgentUID:      usageTestAgentUID,
		FirstRecorded: metav1.NewTime(usageClock(1)),
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4, usageCounterRemediationsProposed: 1},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			usagePodEntryKey("gw-a", usageCounterEventsIngested):           {Name: "agent-gateway-aaa", PodUID: "gw-a", Counter: usageCounterEventsIngested, Sample: 40, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterToolExecutions):       {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterToolExecutions, Sample: 100, Marker: moved},
			usagePodEntryKey("broker-b", usageCounterRemediationsProposed): {Name: "agent-credential-proxy-bbb", PodUID: "broker-b", Counter: usageCounterRemediationsProposed, Sample: 1, Marker: moved},
		},
	})
	h.pruning = true
	h.stub.set(gatewayAddr(), 44, nil)
	h.stub.set(brokerAddr(), 100, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 1)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 5)
	h.poll(10)
	doc := h.document()
	if !doc.FirstRecorded.Time.Equal(usageClock(1)) {
		t.Fatalf("the document was re-seeded: firstRecorded %v", doc.FirstRecorded)
	}
	// The counter the document predates, with nothing in the status, takes the
	// broker's whole sample: no document and no status has seen any of it.
	if doc.Totals[usageCounterToolExecutions] != 10 || doc.Totals[usageCounterEventsIngested] != 8 || doc.Totals[usageCounterRemediationsProposed] != 1 || doc.Totals[usageCounterRemediationsMerged] != 5 {
		t.Fatalf("totals: %v, want 10, 8, 1 and 5", doc.Totals)
	}
	if e := doc.Pods[usagePodEntryKey("broker-b", usageCounterRemediationsMerged)]; e == nil || e.Sample != 5 {
		t.Fatalf("the new counter was not recorded at its sample: %+v", doc.Pods)
	}
}

// A version-1 document, entries keyed by pod UID and two totals, is migrated
// in place on the first poll: the totals survive, with the status pruned so a
// re-seed would have had nothing to start from; the interval's deltas add
// against the migrated baseline; the first-recorded time is kept; and the
// broker's new counter gets its entry.
func TestUsagePoller_MigratesAVersionOneDocument(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	moved := metav1.NewTime(usageClock(3))
	h.writeDocument(&usageDocument{
		Version:       usageDocumentVersionPodKeyed,
		AgentUID:      usageTestAgentUID,
		FirstRecorded: metav1.NewTime(usageClock(1)),
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			"gw-a":     {Name: "agent-gateway-aaa", Counter: usageCounterEventsIngested, Sample: 40, Marker: moved},
			"broker-b": {Name: "agent-credential-proxy-bbb", Counter: usageCounterToolExecutions, Sample: 100, Marker: moved},
		},
	})
	h.pruning = true
	h.stub.set(gatewayAddr(), 46, nil)
	h.stub.set(brokerAddr(), 103, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 2)
	h.poll(10)
	doc := h.document()
	if doc.Version != usageDocumentVersion || !doc.FirstRecorded.Time.Equal(usageClock(1)) {
		t.Fatalf("migrated document: version %d, firstRecorded %v; want %d and %v", doc.Version, doc.FirstRecorded, usageDocumentVersion, usageClock(1))
	}
	// The two kept totals take their deltas; the counter the layout lacked,
	// with nothing in the status, takes the broker's whole sample even though
	// the pod predates the document: no document and no status has seen any
	// of it.
	if doc.Totals[usageCounterToolExecutions] != 13 || doc.Totals[usageCounterEventsIngested] != 10 || doc.Totals[usageCounterRemediationsProposed] != 2 {
		t.Fatalf("totals after the migrating poll: %v, want 13, 10 and 2 (the deltas added to the kept totals, the whole sample to the new one)", doc.Totals)
	}
	for _, key := range []string{
		usagePodEntryKey("gw-a", usageCounterEventsIngested),
		usagePodEntryKey("broker-b", usageCounterToolExecutions),
		usagePodEntryKey("broker-b", usageCounterRemediationsProposed),
	} {
		if e := doc.Pods[key]; e == nil || e.PodUID == "" {
			t.Fatalf("entry %q after the migration: %+v", key, e)
		}
	}
	if doc.Pods[usagePodEntryKey("broker-b", usageCounterRemediationsProposed)].Sample != 2 {
		t.Fatalf("the new counter was not recorded at its sample: %+v", doc.Pods)
	}
}

// The migrating poll could not read the broker, so the migrated document is
// written at the current layout with the new counter still unseen. The
// unfolded set persists with it: the broker's first sample on the recovery
// poll still adds whole, and the set is cleared once a pod has been recorded
// for the counter, so a later pod older than the document is baselined.
func TestUsagePoller_MigratesAVersionOneDocument_BrokerFailsTheMigratingPoll(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	moved := metav1.NewTime(usageClock(3))
	h.writeDocument(&usageDocument{
		Version:       usageDocumentVersionPodKeyed,
		AgentUID:      usageTestAgentUID,
		FirstRecorded: metav1.NewTime(usageClock(1)),
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			"gw-a":     {Name: "agent-gateway-aaa", Counter: usageCounterEventsIngested, Sample: 40, Marker: moved},
			"broker-b": {Name: "agent-credential-proxy-bbb", Counter: usageCounterToolExecutions, Sample: 100, Marker: moved},
		},
	})
	h.stub.set(gatewayAddr(), 46, nil)
	h.stub.fail(brokerAddr(), usageScrapeKindRefused)
	h.poll(10)
	doc := h.document()
	if doc.Version != usageDocumentVersion || doc.Totals[usageCounterRemediationsProposed] != 0 {
		t.Fatalf("after the migrating poll: version %d, totals %v; want version %d and the new counter at 0", doc.Version, doc.Totals, usageDocumentVersion)
	}
	if !slices.Contains(doc.Unfolded, usageCounterRemediationsProposed) {
		t.Fatalf("the written document does not carry the unseen counter: unfolded %v", doc.Unfolded)
	}

	h.stub.set(brokerAddr(), 103, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 2)
	h.poll(15)
	doc = h.document()
	if doc.Totals[usageCounterRemediationsProposed] != 2 || doc.Totals[usageCounterToolExecutions] != 13 {
		t.Fatalf("totals after the recovery poll: %v, want remediationsProposedTotal 2 (the whole sample) and toolExecutionsTotal 13", doc.Totals)
	}
	if len(doc.Unfolded) != 0 {
		t.Fatalf("the unfolded set outlived the first recorded sample: %v", doc.Unfolded)
	}
	if got := h.status().RemediationsProposedTotal; got != 2 {
		t.Fatalf("status.usage.remediationsProposedTotal = %d, want 2", got)
	}
}

// A version-1 document read under a status that already carries the newer
// counter -- this operator wrote it, was rolled back to one that re-seeded a
// version-1 document, and came back -- starts the missing total at that
// value, so the read-back floor does not refuse the migrated document and
// re-seed it; the broker's sample is baselined and adds nothing, since the
// status may already hold part of it.
func TestUsagePoller_MigratesAVersionOneDocumentUnderAStatusFloor(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Status.Usage.RemediationsProposedTotal = 5
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	moved := metav1.NewTime(usageClock(3))
	h.writeDocument(&usageDocument{
		Version:       usageDocumentVersionPodKeyed,
		AgentUID:      usageTestAgentUID,
		FirstRecorded: metav1.NewTime(usageClock(1)),
		Totals:        map[string]int64{usageCounterToolExecutions: 10, usageCounterEventsIngested: 4},
		LastMoved:     &moved,
		Pods: map[string]*usagePodEntry{
			"gw-a":     {Name: "agent-gateway-aaa", Counter: usageCounterEventsIngested, Sample: 40, Marker: moved},
			"broker-b": {Name: "agent-credential-proxy-bbb", Counter: usageCounterToolExecutions, Sample: 100, Marker: moved},
		},
	})
	h.stub.set(gatewayAddr(), 46, nil)
	h.stub.set(brokerAddr(), 103, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 7)
	h.poll(10)
	doc := h.document()
	if !doc.FirstRecorded.Time.Equal(usageClock(1)) {
		t.Fatalf("the migrated document was re-seeded: firstRecorded %v, want %v", doc.FirstRecorded, usageClock(1))
	}
	if doc.Totals[usageCounterToolExecutions] != 13 || doc.Totals[usageCounterEventsIngested] != 10 || doc.Totals[usageCounterRemediationsProposed] != 5 {
		t.Fatalf("totals after the migrating poll: %v, want 13, 10 and 5 (the status floor, the sample baselined)", doc.Totals)
	}
	if e := doc.Pods[usagePodEntryKey("broker-b", usageCounterRemediationsProposed)]; e == nil || e.Sample != 7 {
		t.Fatalf("the new counter was not baselined at its sample: %+v", e)
	}
	if got := h.status().RemediationsProposedTotal; got != 5 {
		t.Fatalf("status.usage.remediationsProposedTotal = %d, want 5", got)
	}
}

// A failing broker's Warning names each status field its counters project
// to, through the poll rather than the message builder alone.
func TestUsagePoller_WarningNamesEveryFieldOfAFailingBroker(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 10, nil)
	h.stub.fail(brokerAddr(), usageScrapeKindRefused)
	h.poll(5)
	h.poll(10)
	select {
	case ev := <-h.recorder.Events:
		want := "agent-credential-proxy-bbb (status.usage.toolExecutionsTotal, status.usage.remediationsProposedTotal, status.usage.remediationsMergedTotal, status.usage.toolExecutionsBlockedTotal, status.usage.brokerFailuresTotal, status.usage.remediationsClosedUnmergedTotal)"
		if !strings.Contains(ev, want) {
			t.Fatalf("event = %q, want it to name the broker's fields as %q", ev, want)
		}
	default:
		t.Fatal("no event after two failed polls")
	}
}

// The Warning names every status field a pod's counters project to, as paths.
func TestUsageScrapeFailureMessage_NamesEachStatusField(t *testing.T) {
	msg := usageScrapeFailureMessage([]usageScrapeFailure{{
		name: "agent-credential-proxy-bbb", counter: usageStatusFieldList(usageBrokerCounters), detail: "connection refused",
		err: &usageScrapeError{Kind: usageScrapeKindRefused},
	}})
	want := "pod agent-credential-proxy-bbb (status.usage.toolExecutionsTotal, status.usage.remediationsProposedTotal, status.usage.remediationsMergedTotal, status.usage.toolExecutionsBlockedTotal, status.usage.brokerFailuresTotal, status.usage.remediationsClosedUnmergedTotal): connection refused"
	if !strings.Contains(msg, want) {
		t.Fatalf("message %q does not name both fields as %q", msg, want)
	}
}

// The two cluster gauges are the latest poll's reading, not a fold: they are
// written on the first poll, fall when the watcher's reading falls, stay put
// when the listener cannot be read, and move lastActiveTime never.
func TestUsagePoller_ClusterGaugesFollowTheWatchersReading(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
	// The watcher starting: its fleet built, no informer synced yet. A zero
	// monitored is a reading and is written as one, not left absent.
	h.stub.setClusters(gatewayAddr(), 3, 0)
	h.stub.set(brokerAddr(), 70, ptr.To(200.0))

	h.poll(3)
	status := h.status()
	if h.patches != 1 || gaugeValue(status.ClustersRegistered) != 3 || gaugeValue(status.ClustersMonitored) != 0 {
		t.Fatalf("the starting watcher: %d patches, status %+v; want one patch writing 3/0", h.patches, status)
	}

	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.poll(5)
	status = h.status()
	if h.patches != 2 || gaugeValue(status.ClustersRegistered) != 3 || gaugeValue(status.ClustersMonitored) != 3 {
		t.Fatalf("first synced poll: %d patches, status %+v; want 3/3", h.patches, status)
	}
	if status.LastActiveTime != nil || status.EventsIngestedTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("the gauges moved a counter or lastActiveTime: %+v", status)
	}

	// A cluster's informer is held: monitored falls, registered does not.
	h.stub.setClusters(gatewayAddr(), 3, 2)
	h.poll(10)
	if status := h.status(); h.patches != 3 || gaugeValue(status.ClustersRegistered) != 3 || gaugeValue(status.ClustersMonitored) != 2 {
		t.Fatalf("after a fall: %d patches, status %+v; want 3/2", h.patches, status)
	}
	if status := h.status(); status.LastActiveTime != nil {
		t.Fatalf("a gauge change moved lastActiveTime: %v", status.LastActiveTime)
	}

	// Unchanged: no write.
	h.poll(15)
	if h.patches != 3 {
		t.Fatalf("an unchanged reading wrote the status: %d patches", h.patches)
	}

	// The listener cannot be read once: the fields keep their last reading.
	h.stub.fail(gatewayAddr(), usageScrapeKindRefused)
	h.poll(20)
	if status := h.status(); h.patches != 3 || gaugeValue(status.ClustersRegistered) != 3 || gaugeValue(status.ClustersMonitored) != 2 {
		t.Fatalf("a failed scrape changed the gauges: %d patches, status %+v", h.patches, status)
	}
	// A second poll in a row with no gateway reading: the operator has no
	// current reading, so both are cleared rather than held.
	h.poll(25)
	if status := h.status(); h.patches != 4 || status.ClustersRegistered != nil || status.ClustersMonitored != nil {
		t.Fatalf("a standing watcher failure left the gauges: %d patches, status %+v", h.patches, status)
	}

	// The watcher is back, with a cluster gone from its fleet.
	h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
	h.stub.setClusters(gatewayAddr(), 2, 2)
	h.poll(30)
	if status := h.status(); gaugeValue(status.ClustersRegistered) != 2 || gaugeValue(status.ClustersMonitored) != 2 {
		t.Fatalf("after the watcher came back: %+v, want 2/2", status)
	}
}

// Across gateway replicas the gauges take the largest reading: every replica's
// watcher builds the same fleet, so a replica mid-startup reports fewer, not
// others.
func TestUsagePoller_ClusterGaugesTakeTheLargestReplica(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), b)...)
	bAddr := usageTestGatewayB + ":9095"
	h.stub.set(gatewayAddr(), 100, ptr.To(1.0))
	h.stub.setClusters(gatewayAddr(), 4, 1)
	h.stub.set(bAddr, 100, ptr.To(1.0))
	h.stub.setClusters(bAddr, 4, 4)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	if status := h.status(); gaugeValue(status.ClustersRegistered) != 4 || gaugeValue(status.ClustersMonitored) != 4 {
		t.Fatalf("two replicas: %+v, want 4/4", status)
	}
}

// One replica read is a reading: a sibling failing for any number of polls
// neither clears the gauges nor lowers them, and a broker failing for the
// streak has nothing to do with them.
func TestUsagePoller_ClusterGaugesHoldWhileAnyReplicaIsRead(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	b := usageGatewayPod("agent-gateway-bbb", "gw-b", usageTestGatewayB, created)
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), b)...)
	bAddr := usageTestGatewayB + ":9095"
	h.stub.set(gatewayAddr(), 100, ptr.To(1.0))
	h.stub.setClusters(gatewayAddr(), 4, 4)
	h.stub.fail(bAddr, usageScrapeKindRefused)
	h.stub.fail(brokerAddr(), usageScrapeKindRefused)
	for _, minute := range []int{5, 10, 15} {
		h.poll(minute)
	}
	if status := h.status(); gaugeValue(status.ClustersRegistered) != 4 || gaugeValue(status.ClustersMonitored) != 4 {
		t.Fatalf("a failing sibling or broker touched the gauges: %+v, want 4/4", status)
	}
}

// A gateway pod that is live but never a target, Pending or without a pod IP,
// is the same absence of a reading as a failing listener: the gauges clear at
// the streak, and come back once a replica is read.
func TestUsagePoller_ClusterGaugesClearWhenNoGatewayIsReadable(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 100, ptr.To(1.0))
	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	if status := h.status(); gaugeValue(status.ClustersRegistered) != 3 {
		t.Fatalf("first poll: %+v", status)
	}
	// The gateway pod loses its IP (evicted and Pending again under the same
	// UID is the same shape): live, not a target, no scrape, no streak.
	pod := &corev1.Pod{}
	if err := h.cl.Get(context.Background(), client.ObjectKey{Namespace: usageTestNamespace, Name: "agent-gateway-aaa"}, pod); err != nil {
		t.Fatal(err)
	}
	pod.Status.PodIP = ""
	if err := h.cl.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	h.poll(10)
	if status := h.status(); gaugeValue(status.ClustersRegistered) != 3 {
		t.Fatalf("one poll without a readable gateway changed the gauges: %+v", status)
	}
	h.poll(15)
	if status := h.status(); status.ClustersRegistered != nil || status.ClustersMonitored != nil {
		t.Fatalf("two polls without a readable gateway left the gauges: %+v", status)
	}
	pod.Status.PodIP = usageTestGatewayIP
	if err := h.cl.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	h.poll(20)
	if status := h.status(); gaugeValue(status.ClustersRegistered) != 3 || gaugeValue(status.ClustersMonitored) != 3 {
		t.Fatalf("the reading did not come back: %+v", status)
	}
}

// With the watcher switched off there is no fleet being watched: the gauges
// are cleared rather than left at the last reading.
func TestUsagePoller_ClusterGaugesClearWhenTheWatcherIsOff(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)}
	agent.Status.Usage.ClustersRegistered = ptr.To(int64(3))
	agent.Status.Usage.ClustersMonitored = ptr.To(int64(3))
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.stub.set(brokerAddr(), 70, nil)
	h.poll(5)
	if status := h.status(); h.patches != 1 || status.ClustersRegistered != nil || status.ClustersMonitored != nil {
		t.Fatalf("watcher off: %d patches, status %+v; want one patch clearing both", h.patches, status)
	}
	h.poll(10)
	if h.patches != 1 {
		t.Fatalf("a second poll with the watcher off wrote again: %d patches", h.patches)
	}
}

// A served CRD without status.usage prunes a gauge-only patch too: the gauges
// get their own pruning record, so the poller does not re-patch them every
// poll, and the counters' shared record, which the Ready writer reads, is not
// touched by a probe that wrote no counter.
func TestUsagePoller_GaugeOnlyProbeUnderAPruningCRD(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruning = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.stub.set(brokerAddr(), 0, nil)
	h.poll(5)
	if h.patches != 1 {
		t.Fatalf("%d patches on the first poll, want 1 (the probe)", h.patches)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("a probe that wrote no counter recorded the counters' shared pruning record")
	}
	// A poll inside the reprobe interval (a sweep a budget cut spread out, say)
	// probes again neither the gauges nor, through them, the whole status.
	h.poll(8)
	if h.patches != 1 {
		t.Fatalf("%d patches while the gauge record is fresh, want 1", h.patches)
	}
	// The record expires and the CRD now serves the fields: the next poll lands them.
	h.p.expireNewerFieldsRecordForTest(agent)
	h.pruning = false
	h.poll(13)
	if status := h.status(); h.patches != 2 || gaugeValue(status.ClustersRegistered) != 3 || gaugeValue(status.ClustersMonitored) != 3 {
		t.Fatalf("after the record expired: %d patches, status %+v; want 2 and 3/3", h.patches, status)
	}
}

// A served CRD at a previous schema prunes the newer fields, the gauges and
// the merged counter, and keeps the first three (every CRD with status.usage
// has carried remediationsProposedTotal): those keep landing every poll they
// move, the shared record stays clear because the newer fields' absence is
// their own condition, and the merged counter moving alone does not re-patch
// every poll while that record is fresh.
// A served CRD from between the gauges and the merged counter serves the
// gauges and prunes the counter: the counter's absence stamps its own record,
// the gauges keep landing every poll they move, and the log names the one
// field that is pruned.
func TestUsagePoller_AGaugeServingCRDWithoutTheMergedCounterKeepsTheGaugesFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningMerged = true
	var mu sync.Mutex
	var lines []string
	ctx := logf.IntoContext(context.Background(), funcr.New(func(_, args string) {
		mu.Lock()
		lines = append(lines, args)
		mu.Unlock()
	}, funcr.Options{}))
	pollAt := func(minute int) {
		h.clock = usageClock(minute)
		h.p.pollOnce(ctx)
	}
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 1)
	pollAt(5)
	// A merge is counted: the patch carries the merged total, the CRD drops
	// it, and the counter's own record is stamped; the gauges came back.
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 2)
	pollAt(10)
	if status := h.status(); status.ClustersRegistered == nil || *status.ClustersRegistered != 3 || status.RemediationsMergedTotal != 0 {
		t.Fatalf("after the merge: %+v, want the gauges at 3 and no merged counter", status)
	}
	if h.p.newerFieldPruned(agent, usageNewerFieldGauges) || !h.p.newerFieldPruned(agent, usageCounterRemediationsMerged) {
		t.Fatal("the merged counter's absence was recorded against the gauges")
	}
	// The gauges move while the merged counter's record is fresh (inside the
	// reprobe interval, so the counter is not re-probed): they land.
	h.stub.setClusters(gatewayAddr(), 4, 4)
	pollAt(14)
	if status := h.status(); status.ClustersRegistered == nil || *status.ClustersRegistered != 4 {
		t.Fatalf("a gauge change waited on the merged counter's record: %+v", status)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the merged counter's absence was recorded as the counters' pruning")
	}
	mu.Lock()
	defer mu.Unlock()
	named := 0
	for _, l := range lines {
		if strings.Contains(l, "does not serve a status.usage field") {
			named++
			if !strings.Contains(l, `"field"="remediationsMergedTotal"`) || strings.Contains(l, "clustersRegistered") {
				t.Fatalf("the log named the wrong field: %s", l)
			}
		}
	}
	if named != 1 {
		t.Fatalf("%d pruning log lines, want 1 naming remediationsMergedTotal", named)
	}
}

// A served CRD from the release before this one serves the gauges and the
// merged counter and prunes the chat counter: the chat counter's absence
// stamps its own record, the merged counter keeps landing every poll it
// moves, and the log names the one field that is pruned.
func TestUsagePoller_AMergedServingCRDWithoutTheChatCounterKeepsTheOthersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningChat = true
	var mu sync.Mutex
	var lines []string
	ctx := logf.IntoContext(context.Background(), funcr.New(func(_, args string) {
		mu.Lock()
		lines = append(lines, args)
		mu.Unlock()
	}, funcr.Options{}))
	pollAt := func(minute int) {
		h.clock = usageClock(minute)
		h.p.pollOnce(ctx)
	}
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.stub.set(chatAddr(), 1, ptr.To(1.0))
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 1)
	pollAt(5)
	// A message and a merge are counted: the patch carries both, the CRD
	// drops the chat counter, and its record alone is stamped.
	h.stub.set(chatAddr(), 3, ptr.To(1.0))
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 2)
	pollAt(10)
	if status := h.status(); status.RemediationsMergedTotal != 1 || status.ChatMessagesInboundTotal != 0 || status.ClustersRegistered == nil {
		t.Fatalf("after a message and a merge: %+v, want 1 merged, no chat counter, the gauges", status)
	}
	if h.p.newerFieldPruned(agent, usageNewerFieldGauges) || h.p.newerFieldPruned(agent, usageCounterRemediationsMerged) || !h.p.newerFieldPruned(agent, usageCounterChatMessagesInbound) {
		t.Fatal("the chat counter's absence was not recorded against the chat counter alone")
	}
	// A quiet poll inside the interval: the chat counter alone is behind the
	// status, and its record holds the patch back.
	patches := h.patches
	pollAt(12)
	if h.patches != patches {
		t.Fatalf("a poll with the chat counter alone behind re-patched the status while its record was fresh")
	}
	// The merged counter moves while the chat record is fresh: it lands.
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 3)
	pollAt(14)
	if got := h.status().RemediationsMergedTotal; got != 2 {
		t.Fatalf("a merge waited on the chat counter's record: %d, want 2", got)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the chat counter's absence was recorded as the counters' pruning")
	}
	mu.Lock()
	defer mu.Unlock()
	named := 0
	for _, l := range lines {
		if strings.Contains(l, "does not serve a status.usage field") {
			named++
			if !strings.Contains(l, `"field"="chatMessagesInboundTotal"`) {
				t.Fatalf("the log named the wrong field: %s", l)
			}
		}
	}
	if named != 1 {
		t.Fatalf("%d pruning log lines, want 1 naming chatMessagesInboundTotal", named)
	}
}

func TestUsagePoller_APartialPruneKeepsTheCountersFlowing(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.pruningGauges = true
	h.stub.set(gatewayAddr(), 0, nil)
	h.stub.setClusters(gatewayAddr(), 3, 3)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 1)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 1)
	h.poll(5)
	h.stub.set(brokerAddr(), 15, nil)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsProposed, 2)
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 2)
	h.poll(10)
	h.stub.set(brokerAddr(), 20, nil)
	h.poll(15)
	if status := h.status(); status.ToolExecutionsTotal != 10 || status.RemediationsProposedTotal != 1 || status.ClustersRegistered != nil || status.RemediationsMergedTotal != 0 {
		t.Fatalf("status after three polls: %+v, want 10 tool executions, 1 proposal and no newer field", status)
	}
	if h.r.usageStatusPruned(agent) {
		t.Fatal("the newer fields' absence was recorded as the counters' pruning")
	}
	if h.patches != 3 {
		t.Fatalf("%d patches, want 3: the gauge probe on the first poll and one per counter movement", h.patches)
	}
	// Each counter write re-probed the newer fields and found them absent again, so
	// the record is still fresh, re-stamped rather than re-created.
	if !h.p.newerFieldPruned(agent, usageNewerFieldGauges) || !h.p.newerFieldPruned(agent, usageCounterRemediationsMerged) {
		t.Fatal("a newer field's record did not survive the counter writes that re-probed it")
	}
	// A merge alone, inside the reprobe interval: lastActiveTime, which the CRD
	// serves, moves, and that is the one patch; the quiet poll after it, with
	// the pruned counter still behind the document, writes nothing while the
	// record is fresh.
	h.stub.setSample(brokerAddr(), usageCounterRemediationsMerged, 3)
	h.poll(17)
	if status := h.status(); h.patches != 4 || status.LastActiveTime == nil || !status.LastActiveTime.Time.Equal(usageClock(17)) {
		t.Fatalf("%d patches after a merge moved lastActiveTime, status %+v; want 4 and the time", h.patches, status)
	}
	h.poll(19)
	if h.patches != 4 {
		t.Fatalf("%d patches on a quiet poll with a pruned counter behind, want 4", h.patches)
	}
}

// A completed sweep drops the gauge records of CRs it did not reach, the
// deleted ones, and keeps the records of the CRs it did.
func TestUsagePoller_ForgetsTheGaugeRecordsOfDepartedCRs(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	h.clock = usageClock(5)
	departed := usageTestAgent(created)
	departed.Name = "departed"
	h.p.noteNewerFieldEcho(context.Background(), agent, usageNewerFieldGauges, false)
	h.p.noteNewerFieldEcho(context.Background(), departed, usageNewerFieldGauges, false)
	h.p.forgetDepartedNewerFieldRecords(map[string]bool{usagePollKey(agent): true})
	if !h.p.newerFieldPruned(agent, usageNewerFieldGauges) {
		t.Error("a live CR's record was dropped by the sweep")
	}
	if h.p.newerFieldPruned(departed, usageNewerFieldGauges) {
		t.Error("a departed CR's record survived the sweep")
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

// A CR deleted between the poll's cached List and pollAgent's live read records
// no Warning: the standing scrape failure has nothing to attach to, and an Event
// written against the gone CR's name and UID would dangle in the namespace for
// the retention hour pointing at an object kubectl describe can no longer
// resolve. The NotFound returns before recordStandingFailures, as the
// cancelled-context guard does; a transient Get error below still records, so a
// standing cause outlives an API blip.
func TestUsagePoller_RecordsNoWarningForACRDeletedDuringThePoll(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)

	// The gateway listener is one failure past its first, so this poll's failure
	// crosses the streak and builds a standing failure -- the precondition for the
	// Warning the pre-fix code recorded against the deleted CR.
	h.p.streaks[usageStreakKey{uid: "gw-a", port: eventWatcherMetricsPortName}] = &usageScrapeStreak{count: 1}
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.stub.set(brokerAddr(), 10, nil)

	// Delete the CR after the harness seeded it: the pods stay, so targets still
	// builds the failing gateway target, but pollAgent's live Get now returns
	// NotFound -- the cached-List-then-deleted window the finding describes.
	if err := h.cl.Delete(context.Background(), agent); err != nil {
		t.Fatalf("deleting the CR mid-setup: %v", err)
	}

	h.clock = usageClock(5)
	if err := h.p.pollAgent(context.Background(), agent, map[string]bool{}); err != nil {
		t.Fatalf("pollAgent on a CR deleted mid-poll returned %v, want nil", err)
	}
	select {
	case ev := <-h.recorder.Events:
		t.Fatalf("a poll whose CR was deleted mid-poll recorded a Warning against the gone CR: %s", ev)
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
		"a refused connection":       {usageScrapeKindRefused, usageScrapeConnectGuidance},
		"a timeout":                  {usageScrapeKindTimeout, usageScrapeConnectGuidance},
		"a line past the bound":      {usageScrapeKindLine, usageScrapeResponseGuidance},
		"a malformed response":       {usageScrapeKindMalformed, usageScrapeResponseGuidance},
		"a status other than 200":    {usageScrapeKindStatus, usageScrapeResponseGuidance},
		"a timeout reading the body": {usageScrapeKindBodyTimeout, usageScrapeResponseGuidance},
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
	if doc.Pods[usagePodEntryKey("gw-old", usageCounterEventsIngested)] != nil {
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

// A rollout where the old replica is evicted rather than gracefully deleted:
// node pressure, preemption, or node loss leaves it in phase Failed with no
// deletion timestamp, the ReplicaSet replaces it without deleting it, and pod
// GC removes it only hours later. Like a terminating pod it is never read
// again, so it must leave the live set; otherwise its marker keeps resetting
// the new replica's advance and its entry never drops.
func TestUsagePoller_AnEvictedSiblingSuppressesNoAdvance(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	old := usageGatewayPod("agent-gateway-old", "gw-old", usageTestGatewayIP, created)
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
	// The old pod is evicted: phase Failed, no deletion timestamp. It is listed,
	// not read, and must no longer be live.
	ctx := context.Background()
	stored := &corev1.Pod{}
	if err := h.cl.Get(ctx, client.ObjectKeyFromObject(old), stored); err != nil {
		t.Fatal(err)
	}
	stored.Status.Phase = corev1.PodFailed
	if err := h.cl.Status().Update(ctx, stored); err != nil {
		t.Fatal(err)
	}
	h.stub.set(newAddr, 15, ptr.To(9.0))
	h.poll(15)
	if status := h.status(); status.EventsIngestedTotal != 18 {
		t.Fatalf("with the old pod evicted: %+v, want 18: the new replica's advance was reset against a pod that is never read again", status)
	}
	if doc := h.document(); doc.Pods[usagePodEntryKey("gw-old", usageCounterEventsIngested)] != nil {
		t.Fatalf("the evicted pod's entry was kept: %+v", doc.Pods)
	}
}

// An immutable counters ConfigMap -- hand-edited, or a foreign copy left in
// place -- makes every update fail with 422 Invalid, so without a signal the
// counters would freeze with nothing but a log line. The poller records a
// Warning on the CR naming the ConfigMap, and leaves the status untouched: the
// failed write returns before the status projection.
func TestUsagePoller_AnImmutableConfigMapRecordsAWarning(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
	h.stub.set(brokerAddr(), 70, ptr.To(200.0))
	h.poll(5) // Creates the ConfigMap and records the baseline.

	// Mark the created ConfigMap immutable so the live read sees it; the branch
	// keys on the object's Immutable field, not on the 422. The fake client does
	// not enforce immutability, so the Update is still forced to fail with
	// cmUpdateErr, as the API server would for an immutable object.
	cmName := usageTestAgentName + usageCountersConfigMapSuffix
	markUsageConfigMapImmutable(t, h, cmName)
	h.cmUpdateErr = apierrors.NewInvalid(schema.GroupKind{Kind: "ConfigMap"}, cmName, nil)

	h.stub.set(gatewayAddr(), 512, ptr.To(100.0))
	h.stub.set(brokerAddr(), 73, ptr.To(200.0))
	h.poll(10) // Would update the ConfigMap; the update is rejected.

	select {
	case ev := <-h.recorder.Events:
		if !strings.Contains(ev, "Warning") || !strings.Contains(ev, usageConfigMapImmutableReason) ||
			!strings.Contains(ev, cmName) || !strings.Contains(ev, "immutable") {
			t.Fatalf("event %q, want a Warning naming the immutable ConfigMap %s", ev, cmName)
		}
	default:
		t.Fatal("no Warning event was recorded for the immutable ConfigMap")
	}

	if status := h.status(); status.EventsIngestedTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("status.usage advanced despite the rejected write: %+v", status)
	}
}

// A write the namespace refuses for a standing reason other than immutability --
// a count/configmaps quota at its cap, or an admission policy that denies the
// write -- records a Warning on the CR naming the ConfigMap and leaves the status
// untouched, rather than freezing status.usage with only a log line. Every route
// the fault is raised on: a Create answered Forbidden (a quota) or Invalid (a
// validating policy), and an Update answered Forbidden (a policy denying updates)
// or Invalid where the object is not immutable (so it is a refused write, not the
// immutable instruction to delete it). The Update rows poll once to create the
// ConfigMap, then fail the next poll's write.
func TestUsagePoller_AWriteRefusedForAStandingReasonRecordsAWarning(t *testing.T) {
	cmName := usageTestAgentName + usageCountersConfigMapSuffix
	forbidden := func() error {
		return apierrors.NewForbidden(schema.GroupResource{Resource: "configmaps"}, cmName, errors.New("exceeded quota: count/configmaps"))
	}
	invalid := func() error { return apierrors.NewInvalid(schema.GroupKind{Kind: "ConfigMap"}, cmName, nil) }
	for _, tc := range []struct {
		name     string
		onCreate bool // inject on the first (Create) poll; otherwise create first, fail the Update
		err      func() error
	}{
		{"a Forbidden Create", true, forbidden},
		{"an Invalid Create", true, invalid},
		{"a Forbidden Update", false, forbidden},
		{"an Invalid Update of a mutable ConfigMap", false, invalid},
	} {
		t.Run(tc.name, func(t *testing.T) {
			created := usageClock(0).Add(-time.Hour)
			h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
			h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
			h.stub.set(brokerAddr(), 70, ptr.To(200.0))
			if tc.onCreate {
				h.cmCreateErr = tc.err()
				h.poll(5) // Would create the ConfigMap; the create is refused.
			} else {
				h.poll(5) // Creates the ConfigMap and records the baseline.
				h.cmUpdateErr = tc.err()
				h.stub.set(gatewayAddr(), 512, ptr.To(100.0)) // move the counters so the write runs
				h.stub.set(brokerAddr(), 73, ptr.To(200.0))
				h.poll(10) // Would update the ConfigMap; the update is refused.
			}

			select {
			case ev := <-h.recorder.Events:
				if !strings.Contains(ev, "Warning") || !strings.Contains(ev, usageConfigMapRefusedReason) ||
					!strings.Contains(ev, cmName) || !strings.Contains(ev, "refused") {
					t.Fatalf("event %q, want a Warning naming the refused ConfigMap %s", ev, cmName)
				}
			default:
				t.Fatal("no Warning event was recorded for the refused ConfigMap write")
			}

			if status := h.status(); status.EventsIngestedTotal != 0 || status.ToolExecutionsTotal != 0 {
				t.Fatalf("status.usage advanced despite the refused write: %+v", status)
			}
		})
	}
}

// A ConfigMap another writer parked under the counters name -- no operator
// instance label, no owner reference to the CR -- is not taken over: the poll
// leaves its data, labels and owner references untouched, records a Warning, and
// does not advance the status.
func TestUsagePoller_AForeignConfigMapIsLeftUntouched(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	cmName := usageTestAgentName + usageCountersConfigMapSuffix
	foreign := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      cmName,
			Namespace: usageTestNamespace,
			Labels:    map[string]string{"owner": "someone-else"},
		},
		Data: map[string]string{"their-key": "their-value"},
	}
	h := newUsageHarness(t, usageTestAgent(created), append(usageDefaultObjects(created), foreign)...)
	h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
	h.stub.set(brokerAddr(), 70, ptr.To(200.0))

	h.poll(5) // Would write the ConfigMap; the foreign one is refused instead.

	select {
	case ev := <-h.recorder.Events:
		if !strings.Contains(ev, "Warning") || !strings.Contains(ev, usageConfigMapForeignReason) || !strings.Contains(ev, cmName) {
			t.Fatalf("event %q, want a Warning naming the foreign ConfigMap %s", ev, cmName)
		}
	default:
		t.Fatal("no Warning event was recorded for the foreign ConfigMap")
	}

	if status := h.status(); status.EventsIngestedTotal != 0 || status.ToolExecutionsTotal != 0 {
		t.Fatalf("status.usage advanced despite the foreign ConfigMap: %+v", status)
	}

	cm := h.configMap()
	if len(cm.Data) != 1 || cm.Data["their-key"] != "their-value" {
		t.Errorf("the foreign ConfigMap's data was changed: %v", cm.Data)
	}
	if _, ok := cm.Data[usageCountersDocumentKey]; ok {
		t.Errorf("the counters document was written into the foreign ConfigMap: %v", cm.Data)
	}
	if len(cm.OwnerReferences) != 0 {
		t.Errorf("an owner reference was added to the foreign ConfigMap: %+v", cm.OwnerReferences)
	}
	if cm.Labels[labelInstance] != "" {
		t.Errorf("an instance label was added to the foreign ConfigMap: %v", cm.Labels)
	}
}

// countingReader is a client.Reader that counts Gets by object type, so a test
// can prove a read went through the uncached APIReader rather than the cached
// Client. The harness points both readers at one fake client, which hides a
// regression that routes either of the poller's live reads through the cache;
// setting a distinct APIReader makes the routing observable.
type countingReader struct {
	client.Reader
	mu        sync.Mutex
	agentGets int
	cmGets    int
}

func (c *countingReader) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	c.mu.Lock()
	switch obj.(type) {
	case *agentv1alpha1.PlatformAgent:
		c.agentGets++
	case *corev1.ConfigMap:
		c.cmGets++
	}
	c.mu.Unlock()
	return c.Reader.Get(ctx, key, obj, opts...)
}

// The CR and its counters ConfigMap are read through the uncached APIReader, not
// the cached Client: a cached read of either, handed back as it stood before
// this poller's own last write, would have the next poll add the interval's
// deltas a second time.
func TestUsagePoller_ReadsTheCRAndConfigMapThroughTheAPIReader(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	reader := &countingReader{Reader: h.cl}
	h.r.APIReader = reader // distinct from Client, which stays the cached fake
	h.stub.set(gatewayAddr(), 500, ptr.To(100.0))
	h.stub.set(brokerAddr(), 70, ptr.To(200.0))

	h.poll(5)

	if reader.agentGets == 0 {
		t.Errorf("the CR was not read through the APIReader; a read routed through the cached Client would double-count the interval next poll")
	}
	if reader.cmGets == 0 {
		t.Errorf("the counters ConfigMap was not read through the APIReader; a cached read would double-count the interval next poll")
	}
}

// A scrape failure past its streak and a ConfigMap fault in the same poll share
// the CR's one Warning: both standing causes land in a single event -- the
// ConfigMap reason, which is what actually freezes the totals, with the scrape
// cause appended after "Also:" -- rather than two events draining the CR's one
// token bucket and starving whichever is recorded second.
func TestUsagePoller_AScrapeFailureAndAConfigMapFaultShareOneWarning(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	cmName := usageTestAgentName + usageCountersConfigMapSuffix
	// The gateway's listener is down from the first poll; the broker advances
	// each poll so the document changes and the write is attempted.
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.stub.set(brokerAddr(), 10, nil)
	h.poll(5) // Creates the ConfigMap; the gateway's first failure is a log line only.

	// Mark the ConfigMap immutable so the live read sees it, then fail the Update
	// as the API server would for an immutable object.
	markUsageConfigMapImmutable(t, h, cmName)
	h.cmUpdateErr = apierrors.NewInvalid(schema.GroupKind{Kind: "ConfigMap"}, cmName, nil)
	h.stub.set(brokerAddr(), 20, nil) // the broker moves, so the write runs and is rejected
	h.poll(10)                        // the gateway is now past its streak; both causes stand at once

	var events []string
drain:
	for {
		select {
		case ev := <-h.recorder.Events:
			events = append(events, ev)
		default:
			break drain
		}
	}
	if len(events) != 1 {
		t.Fatalf("got %d Warning(s) for a poll with both a scrape failure and a ConfigMap fault, want exactly 1: %q", len(events), events)
	}
	for _, want := range []string{usageConfigMapImmutableReason, cmName, "immutable", "agent-gateway-aaa", "Also:"} {
		if !strings.Contains(events[0], want) {
			t.Errorf("the shared Warning does not contain %q: %q", want, events[0])
		}
	}
}

// A budget cut leaves the cursor on the CR it stopped at, so that CR is polled
// first next interval and the CRs already finished are not re-read until the
// sweep wraps -- the bounded-progress guarantee a stable order with a cursor
// buys over List's per-call shuffle. Two CRs, the second's listener hung: the
// first poll finishes CR A and is cut on CR B, leaving the cursor on A; once B
// is unblocked, the next poll resumes at B before A.
func TestUsagePoller_ABudgetCutResumesAtTheCutCRNextPoll(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	a := usageTestAgent(created) // usage-test/agent
	b := usageTestAgent(created)
	b.Name, b.UID = "agent-zzz", "agent-uid-2" // usage-test/agent-zzz sorts after A
	bGateway := usageGatewayPod("agent-zzz-gateway-aaa", "gw-b", "10.0.2.10", created)
	bGateway.Labels = map[string]string{"app": "agent-zzz-gateway"}
	h := newUsageHarness(t, a, append(usageDefaultObjects(created), b, bGateway)...)
	const budget = 50 * time.Millisecond
	h.p.pollBudget = func() time.Duration { return budget }
	bAddr := "10.0.2.10:9095"
	h.stub.set(gatewayAddr(), 100, nil) // A's listeners answer fast
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.block(bAddr) // B's listener hangs, consuming the rest of the budget

	// Poll 1 blocks for the budget, so guard it as the bounded-poll test does.
	done := make(chan struct{})
	go func() { h.poll(5); close(done) }()
	const guard = 10 * time.Second
	select {
	case <-done:
	case <-time.After(guard):
		t.Fatalf("poll 1 did not return within %s; its budget does not bound it", guard)
	}

	const keyA, keyB = usageTestNamespace + "/agent", usageTestNamespace + "/agent-zzz"
	if h.p.cursor != keyA {
		t.Fatalf("after a cut on B, the cursor is %q, want %q (the last finished CR)", h.p.cursor, keyA)
	}
	if !h.p.sweptKeys[keyA] {
		t.Errorf("CR A finished but is not marked swept: %v", h.p.sweptKeys)
	}
	if h.p.sweptKeys[keyB] {
		t.Errorf("CR B was cut but is marked swept: %v", h.p.sweptKeys)
	}
	if !h.stub.scraped(bAddr) {
		t.Errorf("CR B's listener was never reached on poll 1: %v", h.stub.calls)
	}

	// Unblock B and poll again: the cursor sits on A, so B is polled first.
	h.stub.unblock(bAddr)
	h.stub.set(bAddr, 50, nil)
	h.stub.mu.Lock()
	h.stub.calls = nil
	h.stub.mu.Unlock()
	h.poll(10)
	h.stub.mu.Lock()
	first := ""
	if len(h.stub.calls) > 0 {
		first = h.stub.calls[0]
	}
	h.stub.mu.Unlock()
	if first != bAddr {
		t.Errorf("poll 2's first scrape was %q, want the cut CR's listener %q (resume after the cursor)", first, bAddr)
	}
	if len(h.p.sweptKeys) != 0 {
		t.Errorf("after a poll that reached every CR, the sweep did not reset: %v", h.p.sweptKeys)
	}
}

// A failure streak built on a CR swept early in a sweep survives the sweep
// completing on a later poll that does not revisit that CR. The streaks are
// pruned once a sweep -- a full pass, which a standing budget cut spreads over
// several polls -- has reached every CR, on the pods it saw across the whole
// sweep (sweepSeen), not the pods of the one poll that happened to complete it.
// Pruning on that last poll's partial view would reset an early CR's streak
// every sweep, so a standing scrape failure there would never cross the Warning
// threshold. Two CRs, A sorting first with a failing listener and Z last with a
// hung one: poll 1 sweeps A (streak 1) and is cut on Z; poll 2 resumes at Z, is
// cut on it at the head of the poll, and steps the cursor past the budget-eater
// -- completing the sweep without revisiting A; poll 3 wraps back to A, whose
// streak must still stand to reach the threshold on this second failure. The
// same completed sweep prunes a streak for a pod no current CR lists, keyed on
// sweepSeen (which still holds A's gw-a from poll 1) rather than the completing
// poll's partial view -- so the departed streak goes while A's stays.
func TestUsagePoller_AStreakSurvivesASweepThatCompletesWithoutRevisitingTheCR(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	a := usageTestAgent(created) // usage-test/agent sorts first
	z := usageTestAgent(created)
	z.Name, z.UID = "agent-zzz", "agent-uid-2" // usage-test/agent-zzz sorts last
	zGateway := usageGatewayPod("agent-zzz-gateway-aaa", "gw-z", usageTestGatewayB, created)
	zGateway.Labels = map[string]string{"app": "agent-zzz-gateway"}
	h := newUsageHarness(t, a, append(usageDefaultObjects(created), z, zGateway)...)
	const budget = 50 * time.Millisecond
	h.p.pollBudget = func() time.Duration { return budget }

	// A streak for a pod no current CR lists: the sweep that completes in poll 2
	// must prune it, on sweepSeen (holding A's gw-a from poll 1), while A's own
	// streak survives. A prune keyed on the completing poll's partial seen would
	// drop gw-a instead.
	h.p.streaks[usageStreakKey{uid: "departed", port: eventWatcherMetricsPortName}] = &usageScrapeStreak{count: 3}

	// A's gateway fails fast every poll, so its pod builds a streak, while A's
	// broker answers so A finishes and is swept. Z's gateway hangs, so every poll
	// that reaches Z is cut there.
	zAddr := usageTestGatewayB + ":9095"
	h.stub.fail(gatewayAddr(), usageScrapeKindConnect)
	h.stub.set(brokerAddr(), 10, nil)
	h.stub.block(zAddr)

	// Every poll is cut on Z's hang, so guard against a budget that does not
	// bound them.
	done := make(chan struct{})
	go func() {
		h.poll(5)  // sweeps A (streak 1), cut on Z
		h.poll(10) // resumes at Z, cut at the head, completes the sweep past Z
		h.poll(15) // wraps to A: its second failure, streak 2, one Warning
		close(done)
	}()
	const guard = 10 * time.Second
	select {
	case <-done:
	case <-time.After(guard):
		t.Fatalf("three budget-cut polls did not return within %s; the budget does not bound them", guard)
	}

	var events []string
drain:
	for {
		select {
		case ev := <-h.recorder.Events:
			events = append(events, ev)
		default:
			break drain
		}
	}
	if len(events) != 1 {
		t.Fatalf("got %d Warning(s), want exactly 1 once A's streak crosses the threshold on its second failure; "+
			"a streak pruned on the sweep-completing poll's partial view never crosses it: %q", len(events), events)
	}
	if !strings.Contains(events[0], "agent-gateway-aaa") || !strings.Contains(events[0], usageScrapeFailingReason) {
		t.Errorf("the Warning is not A's scrape-failing event: %q", events[0])
	}
	if h.p.streaks[usageStreakKey{uid: "departed", port: eventWatcherMetricsPortName}] != nil {
		t.Errorf("the departed pod's streak survived the completed sweep: forgetDepartedStreaks did not run, or not on sweepSeen")
	}
	if h.p.streaks[usageStreakKey{uid: "gw-a", port: eventWatcherMetricsPortName}] == nil {
		t.Errorf("A's streak was pruned: the completed sweep pruned on its partial view, not sweepSeen, which holds gw-a from poll 1")
	}
}

// When the poll's own budget cuts it short -- a listener that hangs past the
// interval -- it logs one line, so a hung listener freezing the CRs behind it is
// not silent. The parent context is live: this is the poll's own deadline.
func TestUsagePoller_LogsWhenItsOwnBudgetCutsThePoll(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	const budget = 50 * time.Millisecond
	h.p.pollBudget = func() time.Duration { return budget }
	h.stub.block(gatewayAddr())
	h.stub.set(brokerAddr(), 10, nil)

	var mu sync.Mutex
	var lines []string
	logger := funcr.New(func(_, args string) {
		mu.Lock()
		lines = append(lines, args)
		mu.Unlock()
	}, funcr.Options{})
	ctx := logf.IntoContext(context.Background(), logger)
	h.clock = usageClock(5)

	done := make(chan struct{})
	go func() { h.p.pollOnce(ctx); close(done) }()
	const guard = 10 * time.Second
	select {
	case <-done:
	case <-time.After(guard):
		t.Fatalf("pollOnce did not return within %s; its budget does not bound it", guard)
	}

	mu.Lock()
	defer mu.Unlock()
	found := false
	for _, l := range lines {
		if strings.Contains(l, "did not finish within its budget") {
			found = true
		}
	}
	if !found {
		t.Errorf("a budget-cut poll logged no budget line: %q", lines)
	}
}

// A poll cut short by a cancelled parent -- a shutdown or a leader change, not
// the poll's own budget -- logs no budget line: the next leader polls afresh,
// and a spurious "did not finish within its budget" on every leader change would
// cry wolf about a hung listener that is not there. The parent.Err() guard is
// what separates the two.
func TestUsagePoller_DoesNotLogABudgetCutWhenTheParentIsCancelled(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.p.pollBudget = func() time.Duration { return time.Minute } // far longer than the guard below
	h.stub.block(gatewayAddr())
	h.stub.set(brokerAddr(), 10, nil)

	var mu sync.Mutex
	var lines []string
	logger := funcr.New(func(_, args string) {
		mu.Lock()
		lines = append(lines, args)
		mu.Unlock()
	}, funcr.Options{})
	ctx, cancel := context.WithCancel(logf.IntoContext(context.Background(), logger))
	// Cancel the parent while a scrape is in flight, so the poll is cut by the
	// cancellation and not by its (far longer) budget.
	h.stub.hook = func(string) { cancel() }
	h.clock = usageClock(5)

	done := make(chan struct{})
	go func() { h.p.pollOnce(ctx); close(done) }()
	const guard = 10 * time.Second
	select {
	case <-done:
	case <-time.After(guard):
		t.Fatalf("pollOnce did not return within %s of its parent being cancelled", guard)
	}

	mu.Lock()
	defer mu.Unlock()
	for _, l := range lines {
		if strings.Contains(l, "did not finish within its budget") {
			t.Errorf("a poll cut by a cancelled parent logged a budget line: %q", l)
		}
	}
}

// uncapUsagePollReplicas makes targets read the requested replica count, so a
// test can still drive the leader rule above one replica. The operator caps
// the rendered gateway at one (supportedGatewayReplicas), so no install
// reaches this today; the rule stays for
// https://github.com/gke-labs/kube-agents/issues/3016.
func uncapUsagePollReplicas(t *testing.T) {
	t.Helper()
	prev := usagePollGatewayReplicas
	usagePollGatewayReplicas = requestedGatewayReplicas
	t.Cleanup(func() { usagePollGatewayReplicas = prev })
}

// With replicas above one requested, the operator still renders one gateway
// pod and runs no leader_elect.py on it, so that pod never carries the leader
// label. Its chat listener is read anyway: the leader rule reads the rendered
// count, not the requested one.
func TestUsagePoller_ARequestedHAInstallReadsItsOnePodsChatListener(t *testing.T) {
	created := usageClock(0).Add(-time.Hour)
	agent := usageTestAgent(created)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Availability: &agentv1alpha1.AvailabilitySpec{Replicas: ptr.To(int32(2))}}
	h := newUsageHarness(t, agent, usageDefaultObjects(created)...)
	targets, _, err := h.p.targets(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	for _, tg := range targets {
		if tg.addr == chatAddr() {
			return
		}
	}
	t.Fatalf("the one gateway pod's chat listener %s is not among the targets: %+v", chatAddr(), targets)
}
