package controller

import (
	"context"
	"fmt"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// storedAgent reads the PlatformAgent back from the fake cluster.
func storedAgent(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.PlatformAgent {
	t.Helper()
	a := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), a); err != nil {
		t.Fatal(err)
	}
	return a
}

// #6298: the A2A stack runs on a render copy of the CR, and what it resolves
// into that copy's status reaches the status write. Before, nothing carried
// it back, so the bus's resolved count was never written.
func TestAReconcileWritesTheBusCountItResolves(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	r.busStreamState = emptyBusStreams
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(context.Background(), req); err != nil && !isPlatformAgentConflict(err) {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	if got := storedAgent(t, cl, agent).Status.BusReplicas; got != int32(agentv1alpha1.DefaultBusReplicas) {
		t.Errorf("stored status.busReplicas %d after three passes, want the %d the pass resolved", got, agentv1alpha1.DefaultBusReplicas)
	}
}

// A migration's phase change made in a pass is written: a three to one run
// in Pinning past its deadline turns back, and the stored run says so.
func TestAReconcileSavesAMigrationsPhaseChange(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	agent := a2aTestAgent()
	agent.Generation = 3
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(1)}}
	agent.Status.BusReplicas = 3
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationShrink), RunID: "3-1", Phase: string(busMigrationPinning),
		StartedAt: metav1.NewTime(time.Now().Add(-3 * time.Hour)), Deadline: metav1.NewTime(time.Now().Add(-time.Hour)), ObservedGeneration: 3}
	sts := buildA2ANATSStatefulSet(agent, "h")
	r, cl, req := a2aGateTestReconciler(t, agent, sts)
	r.busStreamState = emptyBusStreams
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(context.Background(), req); err != nil && !isPlatformAgentConflict(err) {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	run := storedAgent(t, cl, agent).Status.BusMigration
	if run == nil || busMigrationPhase(run.Phase) == busMigrationPinning || run.Cause == "" {
		t.Errorf("stored run after three passes past its deadline: %+v; want it moved on from Pinning, with a cause", run)
	}
}

// A one to three run started through Reconcile is stored, and every later
// pass keeps its one run ID.
func TestAOneToThreeStartedThroughReconcileKeepsOneRunID(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	agent := a2aTestAgent()
	agent.Generation = 4
	agent.Status.BusReplicas = 1
	sts := buildA2ANATSStatefulSet(agent, "h")
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(3)}}
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	r, cl, req := a2aGateTestReconciler(t, agent, sts)
	r.busStreamState = emptyBusStreams
	ids := map[string]bool{}
	for i := 0; i < 4; i++ {
		// No conflict either: the pass's status writes go against the version
		// the run's own write made.
		if _, err := r.Reconcile(context.Background(), req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
		if run := storedAgent(t, cl, agent).Status.BusMigration; run != nil {
			ids[run.RunID] = true
		}
	}
	if len(ids) != 1 {
		t.Errorf("stored run IDs over four passes: %v, want exactly one", ids)
	}
}

// A failed pass writes none of the A2A stack's half-done record: a give-up
// marked in the pass and followed by a cleanup that failed stays unmarked in
// the stored CR, so the next pass gives up again and redoes the cleanup.
// Carried back with the rest, a deferred status write would have stored the
// mark and the cleanup would never have run again.
func TestAFailedPassStoresNoHalfDoneGiveUp(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	agent := a2aTestAgent()
	agent.Generation = 3
	agent.Annotations = map[string]string{a2aBusMigrationAbandonAnnotation: "3-1"}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(3)}}
	agent.Status.BusReplicas = 1
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "3-1", Phase: string(busMigrationFailed),
		FailedIn: string(busMigrationRestoring), Cause: "a restore that failed",
		StartedAt: metav1.NewTime(time.Now().Add(-time.Hour)), Deadline: metav1.NewTime(time.Now().Add(time.Hour)), ObservedGeneration: 3}
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	sts := buildA2ANATSStatefulSet(agent, "h")
	failing := 0
	funcs := assigningUIDsOnCreate(fakeServerSideApplyInterceptors())
	funcs.List = func(ctx context.Context, c client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
		o := &client.ListOptions{}
		o.ApplyOptions(opts)
		if _, ok := list.(*corev1.PodList); ok && o.LabelSelector != nil && strings.Contains(o.LabelSelector.String(), a2aComponentLabel+"="+busMigrationComponent) {
			failing++
			return fmt.Errorf("the API server is unavailable")
		}
		return c.List(ctx, list, opts...)
	}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).
		WithObjects(agent, sandboxKeysSecret(agent), discordBotSecret(agent), sts).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(funcs).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), busStreamState: emptyBusStreams}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	var err error
	for i := 0; i < 3 && err == nil; i++ {
		_, err = r.Reconcile(context.Background(), req)
	}
	if err == nil {
		t.Fatalf("no pass failed (cleanup lists %d, stored run %+v)", failing, storedAgent(t, cl, agent).Status.BusMigration)
	}
	if failing == 0 {
		t.Fatal("the pass never reached the cleanup, so this proves nothing")
	}
	if run := storedAgent(t, cl, agent).Status.BusMigration; run == nil || run.GivenUp {
		t.Errorf("stored run after a failed cleanup: %+v; want it still not given up, for the next pass to redo", run)
	}
}

// Recording PROFILE_SUBMITS' max age writes that one field and keeps the
// pass's other in-memory status, resolved and not yet written: before, the
// write's answer replaced the whole status with the stored one.
func TestRecordingTheSubmitMaxAgeKeepsThePassesStatus(t *testing.T) {
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), agent); err != nil {
		t.Fatal(err)
	}
	agent.Status.BusReplicas = 3 // resolved this pass, not yet written
	r.recordProfileSubmitMaxAge(context.Background(), agent, ptr.To[int64](3600))
	if agent.Status.BusReplicas != 3 {
		t.Errorf("status.busReplicas %d after recording the max age, want the pass's 3 kept", agent.Status.BusReplicas)
	}
	stored := storedAgent(t, cl, agent)
	if got := stored.Status.ProfileSubmitMaxAgeSeconds; got == nil || *got != 3600 {
		t.Errorf("stored max age %v, want 3600", got)
	}
	if agent.ResourceVersion != stored.ResourceVersion {
		t.Errorf("the pass holds resourceVersion %s, the CR is at %s", agent.ResourceVersion, stored.ResourceVersion)
	}
}
