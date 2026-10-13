package controller

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/go-logr/logr/funcr"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
)

func TestResolveActiveInterfaces(t *testing.T) {
	on, off := ptr.To(true), ptr.To(false)
	cases := []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		want  []string
	}{
		{"nil agent still has the dashboard on by default", nil, []string{"dashboard"}},
		{"an empty spec has the dashboard on by default", &agentv1alpha1.PlatformAgent{}, []string{"dashboard"}},
		{
			"dashboard switched off and nothing else",
			&agentv1alpha1.PlatformAgent{Spec: agentv1alpha1.PlatformAgentSpec{
				Harness: &agentv1alpha1.HarnessSpec{Hermes: &agentv1alpha1.HermesSpec{DashboardEnabled: off}},
			}},
			nil,
		},
		{
			"a present integration block is not an enabled one",
			&agentv1alpha1.PlatformAgent{Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
					GoogleChat: &agentv1alpha1.GoogleChatSpec{},
					Slack:      &agentv1alpha1.SlackSpec{Enabled: off},
					Teams:      &agentv1alpha1.TeamsSpec{},
				},
			}},
			[]string{"dashboard"},
		},
		{
			"every channel on, sorted",
			&agentv1alpha1.PlatformAgent{Spec: agentv1alpha1.PlatformAgentSpec{
				Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{
					Teams:      &agentv1alpha1.TeamsSpec{Enabled: on},
					Slack:      &agentv1alpha1.SlackSpec{Enabled: on},
					GoogleChat: &agentv1alpha1.GoogleChatSpec{Enabled: on},
				},
			}},
			[]string{"dashboard", "googlechat", "slack", "teams"},
		},
		{
			"chat without the dashboard",
			&agentv1alpha1.PlatformAgent{Spec: agentv1alpha1.PlatformAgentSpec{
				Harness:     &agentv1alpha1.HarnessSpec{Hermes: &agentv1alpha1.HermesSpec{DashboardEnabled: off}},
				Integration: &agentv1alpha1.PlatformAgentIntegrationSpec{Slack: &agentv1alpha1.SlackSpec{Enabled: on}},
			}},
			[]string{"slack"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveActiveInterfaces(tc.agent)
			if !slices.Equal(got, tc.want) {
				t.Errorf("resolveActiveInterfaces() = %v, want %v", got, tc.want)
			}
		})
	}
}

// The counters are declared but unwritten, so a status that has only the
// interfaces must not advertise five zeros; and a counter that is set must
// appear under the name the schema gives it.
func TestAgentUsageStatusSerialisesOnlyWhatIsSet(t *testing.T) {
	only := agentv1alpha1.AgentUsageStatus{ActiveInterfaces: []string{"dashboard"}}
	got, err := json.Marshal(only)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != `{"activeInterfaces":["dashboard"]}` {
		t.Errorf("serialised %s, want only activeInterfaces", got)
	}

	now := metav1.Now()
	full := agentv1alpha1.AgentUsageStatus{
		SessionsTotal: 3, EventsIngestedTotal: 4, ToolExecutionsTotal: 5,
		RemediationsProposedTotal: 6, RemediationsAppliedTotal: 7,
		ClustersRegistered: ptr.To(int64(8)), ClustersMonitored: ptr.To(int64(9)),
		ActiveInterfaces: []string{"googlechat"}, LastActiveTime: &now,
	}
	got, err = json.Marshal(full)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(got, &decoded); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{
		"sessionsTotal", "eventsIngestedTotal", "toolExecutionsTotal",
		"remediationsProposedTotal", "remediationsAppliedTotal", "clustersRegistered", "clustersMonitored",
		"activeInterfaces", "lastActiveTime",
	} {
		if _, ok := decoded[key]; !ok {
			t.Errorf("serialised form lacks %q: %s", key, got)
		}
	}
	if len(decoded) != 9 {
		t.Errorf("serialised form has %d keys, want 9: %s", len(decoded), got)
	}
}

// The Ready writer populates the interfaces, and only a change in them costs a
// write: the second pass over an unchanged spec makes none, a spec that enables a
// channel makes exactly one, and the counters ride through untouched.
func TestUpdateStatusReadyWritesActiveInterfacesOnlyWhenTheyChange(t *testing.T) {
	agent := observedGenerationAgent(1)
	agent.Status.Usage.SessionsTotal = 42 // as if a producer had written it
	counter := &statusWriteCounter{}
	r := observedGenerationReconciler(agent, counter)

	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Fatalf("first pass made %d status writes, want 1", counter.writes)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(stored.Status.Usage.ActiveInterfaces, []string{"dashboard"}) {
		t.Errorf("status.usage.activeInterfaces = %v, want [dashboard]", stored.Status.Usage.ActiveInterfaces)
	}
	if stored.Status.Usage.SessionsTotal != 42 {
		t.Errorf("the Ready writer clobbered sessionsTotal: got %d, want 42", stored.Status.Usage.SessionsTotal)
	}

	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Fatalf("an unchanged pass made a status write (%d total); that is the hot loop", counter.writes)
	}

	// Stored, not only edited in memory: a status write hands back the server's
	// copy of the object, spec included, as it does in a real reconcile.
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
	}
	if err := r.Update(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Fatalf("enabling a channel made %d writes in total, want 2", counter.writes)
	}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(stored.Status.Usage.ActiveInterfaces, []string{"dashboard", "slack"}) {
		t.Errorf("status.usage.activeInterfaces = %v, want [dashboard slack]", stored.Status.Usage.ActiveInterfaces)
	}

	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Fatalf("a second unchanged pass made a status write (%d total)", counter.writes)
	}
}

// TestAPrunedUsageStatusDoesNotWriteEveryPass is the operator-ahead-of-its-CRD
// case for status.usage, the skew TestAPrunedObservedGenerationDoesNotWriteEveryPass
// covers for observedGeneration: a served CRD without the field prunes it on
// every write, it reads back absent, and a gate keyed on it alone would write
// every pass. The writer notices the pruning from the echo and stops gating on
// the field; once the CRD carries it, the next write for any other reason lands
// it and the gate resumes.
func TestAPrunedUsageStatusDoesNotWriteEveryPass(t *testing.T) {
	agent := observedGenerationAgent(1)
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	persist := funcs.SubResourceUpdate
	pruning := true
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && pruning {
			pa.Status.Usage = agentv1alpha1.AgentUsageStatus{}
		}
		return persist(ctx, cl, subResourceName, obj, opts...)
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	settleReady(t, r, agent)
	if len(agent.Status.Usage.ActiveInterfaces) != 0 {
		t.Fatalf("the fixture did not prune status.usage (got %v); the test is not exercising the skew", agent.Status.Usage.ActiveInterfaces)
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Errorf("%d status writes across three unchanged passes under a pruning CRD, want 1: this is the write-every-pass loop", counter.writes)
	}

	// The skew persists past the interval: one probe write, recorded again,
	// then quiet again.
	key := client.ObjectKeyFromObject(agent)
	r.prunedUsageStatus.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("%d writes after the record expired under a still-pruning CRD, want 2: one probe, then quiet", counter.writes)
	}

	// The CRD is applied. While the record is fresh nothing is written for the
	// field alone; once it expires the next pass writes once and lands it.
	pruning = false
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("%d writes after the CRD was applied with a fresh record, want 2: the field alone must not force a write yet", counter.writes)
	}
	r.prunedUsageStatus.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	settleReady(t, r, agent)
	if counter.writes != 3 || !slices.Equal(agent.Status.Usage.ActiveInterfaces, []string{"dashboard"}) {
		t.Fatalf("after the record expired under the applied CRD: %d writes, activeInterfaces=%v; want 3 and [dashboard]", counter.writes, agent.Status.Usage.ActiveInterfaces)
	}
	if r.usageStatusPruned(agent) {
		t.Error("the echo carried the field and the record was not cleared")
	}

	// And the gate is back on the field: a spec change that moves it costs one
	// write, an unchanged pass none.
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
	}
	if err := r.Update(context.Background(), agent); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 4 || !slices.Equal(agent.Status.Usage.ActiveInterfaces, []string{"dashboard", "slack"}) {
		t.Errorf("after enabling slack: %d writes, activeInterfaces=%v; want 4 and [dashboard slack]", counter.writes, agent.Status.Usage.ActiveInterfaces)
	}
}

// The probe is scheduled, not left to the next event: while a record is held
// the steady-state requeue is the interval; without one, or once it has gone
// stale, the requeue is the caller's own ceiling.
func TestUsageStatusRequeueFollowsTheRecord(t *testing.T) {
	agent := observedGenerationAgent(1)
	r := observedGenerationReconciler(agent, &statusWriteCounter{})
	key := client.ObjectKeyFromObject(agent)

	if got := r.usageStatusRequeue(agent); got != secretEnvReprobeInterval {
		t.Errorf("requeue with no record = %s, want the caller's ceiling %s", got, secretEnvReprobeInterval)
	}
	r.prunedUsageStatus.Store(key, time.Now())
	if got := r.usageStatusRequeue(agent); got != usageStatusReprobeInterval {
		t.Errorf("requeue with a fresh record = %s, want %s", got, usageStatusReprobeInterval)
	}
	r.prunedUsageStatus.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	if got := r.usageStatusRequeue(agent); got != secretEnvReprobeInterval {
		t.Errorf("requeue with a stale record = %s, want the caller's ceiling: the record no longer gates, so the next pass probes on its own", got)
	}
	r.forgetUsageStatus(agent)
	if r.usageStatusPruned(agent) {
		t.Error("forgetUsageStatus left the record in place")
	}
}

// The skew is said once per record: a status write for another reason while
// the record is fresh re-records silently, and the probe after expiry says it
// again.
func TestAPrunedUsageStatusIsLoggedOncePerRecord(t *testing.T) {
	var lines []string
	logger := funcr.New(func(prefix, args string) { lines = append(lines, args) }, funcr.Options{})
	ctx := logf.IntoContext(context.Background(), logger)
	said := func() int {
		n := 0
		for _, l := range lines {
			if strings.Contains(l, "served CRD has no status.usage") {
				n++
			}
		}
		return n
	}

	agent := observedGenerationAgent(1)
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	persist := funcs.SubResourceUpdate
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
			pa.Status.Usage = agentv1alpha1.AgentUsageStatus{}
		}
		return persist(ctx, cl, subResourceName, obj, opts...)
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1)).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	settle := func() {
		t.Helper()
		if _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aProvisionState{}); err != nil {
			t.Fatalf("updateStatusReady: %v", err)
		}
	}

	settle()
	if said() != 1 || counter.writes != 1 {
		t.Fatalf("after the first pass: %d log lines, %d writes; want 1 and 1", said(), counter.writes)
	}
	// A write for another reason while the record is fresh: the generation
	// moved, so the gate writes, the echo is still pruned, and nothing is said.
	agent.Generation = 2
	if err := r.Update(ctx, agent); err != nil {
		t.Fatal(err)
	}
	settle()
	if counter.writes != 2 {
		t.Fatalf("the generation bump made %d writes in total, want 2", counter.writes)
	}
	if said() != 1 {
		t.Errorf("a write while the record was fresh logged again (%d lines), want 1", said())
	}
	// The probe after expiry finds the CRD still pruning and says so once more.
	r.prunedUsageStatus.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-2*usageStatusReprobeInterval))
	settle()
	if said() != 2 || counter.writes != 3 {
		t.Errorf("after the probe: %d log lines, %d writes; want 2 and 3", said(), counter.writes)
	}
}

// The cap at Reconcile's tail is what turns the record's expiry into a probe:
// a held record has to shorten the steady-state requeue to the interval, and
// no record has to leave the caller's ceiling alone. Read off Reconcile itself,
// the way the RBAC self-check's cap is, because the helper passing on its own
// says nothing about whether the tail still consults it.
func TestReconcileRequeuesAtTheIntervalWhileAUsageRecordIsHeld(t *testing.T) {
	scheme := setupScheme()
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"}}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		// The sandbox keys Secret keeps the agent out of Degraded, whose 30s
		// requeue would mask the interval under test.
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()

	// The finalizer, then the workload and the status, then a settled pass.
	for pass := 1; pass <= 2; pass++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", pass, err)
		}
	}
	settled, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("settled Reconcile: %v", err)
	}
	if settled.RequeueAfter != secretEnvReprobeInterval {
		t.Fatalf("settled RequeueAfter = %v, want the ceiling %v before any record is held", settled.RequeueAfter, secretEnvReprobeInterval)
	}

	r.prunedUsageStatus.Store(client.ObjectKeyFromObject(agent), time.Now())
	held, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile with a record held: %v", err)
	}
	if held.RequeueAfter != usageStatusReprobeInterval {
		t.Errorf("RequeueAfter = %v while a pruning record is held, want %v: nothing else schedules the probe", held.RequeueAfter, usageStatusReprobeInterval)
	}

	r.forgetUsageStatus(agent)
	released, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile with the record dropped: %v", err)
	}
	if released.RequeueAfter != secretEnvReprobeInterval {
		t.Errorf("RequeueAfter = %v with no record, want the ceiling %v", released.RequeueAfter, secretEnvReprobeInterval)
	}
}
