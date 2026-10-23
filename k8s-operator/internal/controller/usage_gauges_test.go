package controller

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func gaugeTestAgent() *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{}
	agent.Name = "pa"
	agent.Namespace = "ns"
	agent.Status.Phase = "Ready"
	agent.Status.Usage = agentv1alpha1.AgentUsageStatus{
		ToolExecutionsTotal:      5,
		ChatMessagesInboundTotal: 2,
		NegativeFeedbackTotal:    1,
		ClustersRegistered:       ptr.To(int64(3)),
		ClustersMonitored:        ptr.To(int64(1)),
		LastActiveTime:           &metav1.Time{Time: usageClock(10)},
	}
	return agent
}

// Every counter and gauge of status.usage lands as one sample of the usage
// family, labelled by the field's JSON name; the times land as unix seconds in
// the time family; an absent pointer or time has no sample.
func TestUsageGauges_PublishEveryFieldOfTheStatus(t *testing.T) {
	reg := prometheus.NewRegistry()
	g := newUsageGauges(reg)
	agent := gaugeTestAgent()
	g.publishUsage(agent)
	for field, want := range map[string]float64{"toolExecutionsTotal": 5, "chatMessagesInboundTotal": 2, "negativeFeedbackTotal": 1, "eventsIngestedTotal": 0, "clustersRegistered": 3, "clustersMonitored": 1} {
		if got := testutil.ToFloat64(g.usage.WithLabelValues("ns", "pa", field)); got != want {
			t.Errorf("%s = %v, want %v", field, got, want)
		}
	}
	if got := testutil.ToFloat64(g.times.WithLabelValues("ns", "pa", "lastActiveTime")); got != float64(usageClock(10).Unix()) {
		t.Errorf("lastActiveTime = %v, want %v", got, usageClock(10).Unix())
	}
	if n := testutil.CollectAndCount(g.times, usageTimeGaugeName); n != 1 {
		t.Errorf("%d time samples, want 1: the absent human time has none", n)
	}
	if n := testutil.CollectAndCount(g.usage, usageGaugeName); n != len(usageExportFields) {
		t.Errorf("%d usage samples, want one per field (%d)", n, len(usageExportFields))
	}
}

func TestUsageGauges_AnAbsentGaugeOrTimeLosesItsSample(t *testing.T) {
	reg := prometheus.NewRegistry()
	g := newUsageGauges(reg)
	agent := gaugeTestAgent()
	g.publishUsage(agent)
	agent.Status.Usage.ClustersRegistered = nil
	agent.Status.Usage.ClustersMonitored = nil
	agent.Status.Usage.LastActiveTime = nil
	g.publishUsage(agent)
	if n := testutil.CollectAndCount(g.usage, usageGaugeName); n != len(usageExportFields)-2 {
		t.Errorf("%d usage samples after the gauges went absent, want %d", n, len(usageExportFields)-2)
	}
	if n := testutil.CollectAndCount(g.times, usageTimeGaugeName); n != 0 {
		t.Errorf("%d time samples after the time went absent, want 0", n)
	}
}

func TestUsageGauges_PhaseIsOneHotOverTheVocabulary(t *testing.T) {
	reg := prometheus.NewRegistry()
	g := newUsageGauges(reg)
	agent := gaugeTestAgent()
	g.publishPhase(agent)
	for _, phase := range usagePhases {
		want := 0.0
		if phase == "Ready" {
			want = 1
		}
		if got := testutil.ToFloat64(g.phase.WithLabelValues("ns", "pa", phase)); got != want {
			t.Errorf("phase %s = %v, want %v", phase, got, want)
		}
	}
	agent.Status.Phase = "Degraded"
	g.publishPhase(agent)
	if testutil.ToFloat64(g.phase.WithLabelValues("ns", "pa", "Ready")) != 0 || testutil.ToFloat64(g.phase.WithLabelValues("ns", "pa", "Degraded")) != 1 {
		t.Error("the phase did not move from Ready to Degraded")
	}
	agent.Status.Phase = "Elsewhere"
	g.publishPhase(agent)
	if n := testutil.CollectAndCount(g.phase, usagePhaseGaugeName); n != len(usagePhases) {
		t.Errorf("%d phase samples after an unknown phase, want %d: the vocabulary is closed", n, len(usagePhases))
	}
	if testutil.ToFloat64(g.phase.WithLabelValues("ns", "pa", "Degraded")) != 0 {
		t.Error("an unknown phase left Degraded at 1")
	}
}

func TestUsageGauges_ForgetDropsEveryFamilyOfTheAgent(t *testing.T) {
	reg := prometheus.NewRegistry()
	g := newUsageGauges(reg)
	agent := gaugeTestAgent()
	other := gaugeTestAgent()
	other.Name = "other"
	g.publishUsage(agent)
	g.publishPhase(agent)
	g.publishUsage(other)
	g.publishPhase(other)
	g.forget("ns", "pa")
	if n := testutil.CollectAndCount(g.usage, usageGaugeName); n != len(usageExportFields) {
		t.Errorf("%d usage samples after forgetting one agent, want the other agent's %d", n, len(usageExportFields))
	}
	if n := testutil.CollectAndCount(g.times, usageTimeGaugeName); n != 1 {
		t.Errorf("%d time samples after forgetting one agent, want 1", n)
	}
	if n := testutil.CollectAndCount(g.phase, usagePhaseGaugeName); n != len(usagePhases) {
		t.Errorf("%d phase samples after forgetting one agent, want %d", n, len(usagePhases))
	}
}

// The exporter's field lists are a second copy of AgentUsageStatus: a counter
// or gauge added to the struct without a row here would be missing from the
// scrape with nothing failing.
func TestUsageGauges_CoverEveryNumericAndTimeFieldOfTheStatus(t *testing.T) {
	named := map[string]bool{}
	for _, f := range usageExportFields {
		named[f.name] = true
	}
	for _, f := range usageExportTimeFields {
		named[f.name] = true
	}
	typ := reflect.TypeOf(agentv1alpha1.AgentUsageStatus{})
	for i := 0; i < typ.NumField(); i++ {
		field := typ.Field(i)
		tag := strings.Split(field.Tag.Get("json"), ",")[0]
		if tag == "" || tag == "activeInterfaces" {
			continue
		}
		if !named[tag] {
			t.Errorf("status.usage.%s has no gauge row", tag)
		}
		delete(named, tag)
	}
	if len(named) != 0 {
		t.Errorf("gauge rows name fields the status does not have: %v", named)
	}
}

// A reconcile publishes the agent as it read it, before any writer runs, so
// an operator restart serves every agent's samples on its first pass over
// the fleet and a status write by any writer is published on the reconcile
// it re-enqueues. The finalizer path returns before every status writer.
func TestReconcile_PublishesTheAgentItRead(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "gauged", Namespace: "reconcile-publishes"},
		Status: agentv1alpha1.AgentStatus{
			Phase: "Degraded",
			Usage: agentv1alpha1.AgentUsageStatus{ChatTurnsErroredTotal: 4},
		},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if got := testutil.ToFloat64(usageMetrics.phase.WithLabelValues(agent.Namespace, agent.Name, "Degraded")); got != 1 {
		t.Errorf("phase Degraded = %v after a reconcile that wrote no status, want 1", got)
	}
	if got := testutil.ToFloat64(usageMetrics.usage.WithLabelValues(agent.Namespace, agent.Name, "chatTurnsErroredTotal")); got != 4 {
		t.Errorf("chatTurnsErroredTotal = %v after a reconcile that wrote no status, want 4", got)
	}

	// Gone: the next reconcile drops every sample of the agent. The first
	// reconcile added the finalizer, which the fake client honours.
	if err := cl.Get(context.Background(), req.NamespacedName, agent); err != nil {
		t.Fatalf("Get: %v", err)
	}
	agent.Finalizers = nil
	if err := cl.Update(context.Background(), agent); err != nil {
		t.Fatalf("Update: %v", err)
	}
	if err := cl.Delete(context.Background(), agent); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("Reconcile after delete: %v", err)
	}
	if n := samplesOfAgent(t, usageMetrics.phase, agent.Namespace); n != 0 {
		t.Errorf("%d phase samples after the agent was deleted, want 0", n)
	}
	if n := samplesOfAgent(t, usageMetrics.usage, agent.Namespace); n != 0 {
		t.Errorf("%d usage samples after the agent was deleted, want 0", n)
	}
}

// samplesOfAgent counts a family's samples in one namespace; the process-wide
// exporter also carries what other tests in the package published.
func samplesOfAgent(t *testing.T, vec *prometheus.GaugeVec, namespace string) int {
	t.Helper()
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(vec)
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	n := 0
	for _, f := range families {
		for _, m := range f.GetMetric() {
			for _, l := range m.GetLabel() {
				if l.GetName() == usageGaugeNamespaceLabel && l.GetValue() == namespace {
					n++
				}
			}
		}
	}
	return n
}

// The phase vocabulary is the writers' own constants. The guard is on the
// assignment forms the package writes a phase through, a Status.Phase field,
// newPhase or phase, in any file, the AgentPlugin writer included since it
// shares degradedPhase: a new phase has to be a constant, and every
// PlatformAgent writer constant is in usagePhases. A literal returned or
// compared elsewhere is outside it.
func TestUsageGauges_PhasesAreConstantsTheVocabularyCarries(t *testing.T) {
	for _, p := range []string{provisioningPhase, readyPhase, degradedPhase} {
		if !slices.Contains(usagePhases, p) {
			t.Errorf("usagePhases lacks the writer constant %q", p)
		}
	}
	literal := regexp.MustCompile(`(\.Phase|\bnewPhase|\bphase)\s*:?=\s*"[A-Za-z]+"`)
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range literal.FindAllString(string(src), -1) {
			t.Errorf("%s assigns a phase from a literal (%s); use a phase constant so usagePhases carries it", f, m)
		}
	}
}

// While the served CRD prunes status.usage, the CR as read carries zeros that
// mean unknown, not idle: the usage and time families are dropped for the agent
// and the phase stays. The hold is a pruned record of any age (the fresh test
// is the writers' reprobe gate, not this one) or a usage block that reads
// entirely empty, which is what a pruned status looks like before the first
// write of this process records it, and what a new agent looks like before its
// first poll.
func TestReconcile_HoldsTheUsageFamiliesBackWhileTheServedCRDPrunesThem(t *testing.T) {
	cases := []struct {
		name   string
		record func(r *PlatformAgentReconciler, key types.NamespacedName)
	}{
		{"a fresh pruned record", func(r *PlatformAgentReconciler, key types.NamespacedName) { r.prunedUsageStatus.Store(key, time.Now()) }},
		{"a pruned record older than the reprobe interval", func(r *PlatformAgentReconciler, key types.NamespacedName) {
			r.prunedUsageStatus.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
		}},
		{"no record yet and an empty usage block", func(*PlatformAgentReconciler, types.NamespacedName) {}},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := &agentv1alpha1.PlatformAgent{
				ObjectMeta: metav1.ObjectMeta{Name: "pruned", Namespace: fmt.Sprintf("reconcile-pruned-%d", i)},
				Status:     agentv1alpha1.AgentStatus{Phase: degradedPhase},
			}
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
			r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
			// A series from before the skew.
			usageMetrics.usage.WithLabelValues(agent.Namespace, agent.Name, "chatTurnsErroredTotal").Set(4)
			tc.record(r, req.NamespacedName)
			if _, err := r.Reconcile(context.Background(), req); err != nil {
				t.Fatalf("Reconcile: %v", err)
			}
			if n := samplesOfAgent(t, usageMetrics.usage, agent.Namespace); n != 0 {
				t.Errorf("%d usage samples, want 0: a zero here would read as idle", n)
			}
			if got := testutil.ToFloat64(usageMetrics.phase.WithLabelValues(agent.Namespace, agent.Name, degradedPhase)); got != 1 {
				t.Errorf("phase Degraded = %v, want 1: the phase is not pruned", got)
			}
		})
	}
}

// A usage block with any value in it is published, zeros and all: a counter at
// 0 beside one that moved is idle, not unknown.
func TestReconcile_PublishesAUsageBlockThatHoldsAnyValue(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "held", Namespace: "reconcile-held"},
		Status:     agentv1alpha1.AgentStatus{Phase: readyPhase, Usage: agentv1alpha1.AgentUsageStatus{ClustersRegistered: ptr.To[int64](0)}},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	if _, err := r.Reconcile(context.Background(), req); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	if n := samplesOfAgent(t, usageMetrics.usage, agent.Namespace); n != len(usageExportFields)-1 {
		t.Errorf("%d usage samples, want %d: a block with a gauge in it is published", n, len(usageExportFields)-1)
	}
}

// A served CRD at an intermediate schema serves the first-schema fields and
// prunes the newer ones, which the poller records per field. The publish reads
// those records and holds back the pruned fields' samples alone, so a block
// that is otherwise live publishes what the server serves and nothing it
// pruned as zero.
func TestReconcile_HoldsBackTheFieldsTheServedCRDPrunesAndPublishesTheRest(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "partial", Namespace: "reconcile-partial"},
		Status: agentv1alpha1.AgentStatus{Phase: readyPhase, Usage: agentv1alpha1.AgentUsageStatus{
			ActiveInterfaces:    []string{"dashboard"},
			ToolExecutionsTotal: 7,
		}},
	}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	p := NewUsageCounterPoller(r)
	ctx := context.Background()
	p.noteNewerFieldEcho(ctx, agent, usageCounterChatMessagesInbound, false)
	p.noteNewerFieldEcho(ctx, agent, usageNewerFieldGauges, false)
	p.noteNewerFieldEcho(ctx, agent, usageNewerFieldLastHumanActive, false)
	// Samples from before the records, as a restart's first pass leaves them.
	usageMetrics.usage.WithLabelValues(agent.Namespace, agent.Name, usageCounterChatMessagesInbound).Set(0)
	usageMetrics.usage.WithLabelValues(agent.Namespace, agent.Name, "clustersMonitored").Set(0)
	usageMetrics.times.WithLabelValues(agent.Namespace, agent.Name, usageNewerFieldLastHumanActive).Set(1)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	reg := prometheus.NewPedanticRegistry()
	reg.MustRegister(usageMetrics.usage, usageMetrics.times)
	families, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]float64{}
	for _, f := range families {
		for _, m := range f.GetMetric() {
			labels := map[string]string{}
			for _, l := range m.GetLabel() {
				labels[l.GetName()] = l.GetValue()
			}
			if labels[usageGaugeNamespaceLabel] == agent.Namespace {
				got[labels[usageGaugeFieldLabel]] = m.GetGauge().GetValue()
			}
		}
	}
	for _, held := range []string{usageCounterChatMessagesInbound, "clustersRegistered", "clustersMonitored", usageNewerFieldLastHumanActive} {
		if _, ok := got[held]; ok {
			t.Errorf("%s has a sample while the served CRD prunes it; a zero here reads as idle", held)
		}
	}
	if got[usageCounterToolExecutions] != 7 {
		t.Errorf("toolExecutionsTotal = %v, want 7: a field the server serves is published", got[usageCounterToolExecutions])
	}
	if _, ok := got[usageCounterBrokerFailures]; !ok {
		t.Errorf("brokerFailuresTotal has no sample; a field with no record is published, zero included")
	}
}
