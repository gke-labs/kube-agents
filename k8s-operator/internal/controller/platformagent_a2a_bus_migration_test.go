package controller

import (
	"context"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// walkBusMigration runs a migration from its first phase, failing the phase
// named failAt (and, when failAgain is set, the first phase of the way back
// too), and returns every phase it passed through and the last step.
func walkBusMigration(leg busMigrationLeg, failAt busMigrationPhase, failAgain bool) ([]busMigrationPhase, busMigrationStep) {
	phase := busMigrationPinning
	if leg == busMigrationGrow {
		phase = busMigrationFreezing
	}
	step := busMigrationStep{next: phase}
	seen := []busMigrationPhase{phase}
	failedOnce := false
	for i := 0; i < 20 && !busMigrationTerminal(step.next); i++ {
		outcome := busMigrationSucceeded
		if step.next == failAt && !failedOnce {
			outcome, failedOnce = busMigrationErred, true
		} else if failedOnce && failAgain && step.abandoning {
			outcome, failAgain = busMigrationErred, false
		}
		step = nextBusMigrationPhase(leg, step.next, step.abandoning, outcome)
		seen = append(seen, step.next)
	}
	return seen, step
}

// Every failure point ends bounded: back where it started with the data
// (Abandoned, not frozen), or, only when the way back itself fails, at Failed
// with the manual step named; frozen then only for one to three, whose
// rollback is what failed.
func TestEveryBusMigrationFailureIsBounded(t *testing.T) {
	for _, tc := range []struct {
		leg        busMigrationLeg
		failAt     busMigrationPhase
		failAgain  bool
		want       busMigrationPhase
		wantFrozen bool
	}{
		{busMigrationShrink, "", false, busMigrationDone, false},
		{busMigrationShrink, busMigrationPinning, false, busMigrationAbandoned, false},
		{busMigrationShrink, busMigrationPinning, true, busMigrationFailed, false},
		{busMigrationShrink, busMigrationShrinking, false, busMigrationFailed, false},
		{busMigrationGrow, "", false, busMigrationDone, false},
		{busMigrationGrow, busMigrationFreezing, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationSnapshot, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationHashing, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationConverting, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationRestoring, false, busMigrationAbandoned, false},
		{busMigrationGrow, busMigrationRestoring, true, busMigrationFailed, true},
		{busMigrationGrow, busMigrationConverting, true, busMigrationFailed, true},
	} {
		seen, last := walkBusMigration(tc.leg, tc.failAt, tc.failAgain)
		if last.next != tc.want {
			t.Errorf("%s failing at %q (again: %v): ended %s via %v, want %s", tc.leg, tc.failAt, tc.failAgain, last.next, seen, tc.want)
			continue
		}
		if got := busMigrationFrozen(tc.leg, last.next); got != tc.wantFrozen {
			t.Errorf("%s failing at %q (again: %v): left frozen = %v, want %v", tc.leg, tc.failAt, tc.failAgain, got, tc.wantFrozen)
		}
		if (last.next == busMigrationFailed) != (last.manual != "") {
			t.Errorf("%s failing at %q: Failed must name a manual step and nothing else may (manual %q)", tc.leg, tc.failAt, last.manual)
		}
	}
}

// One to three failing after the conversion rolls back through one server
// and the same snapshot, and only then lifts the freeze.
func TestAOneToThreeFailureAfterTheConversionRollsBack(t *testing.T) {
	seen, last := walkBusMigration(busMigrationGrow, busMigrationRestoring, false)
	want := []busMigrationPhase{busMigrationFreezing, busMigrationSnapshot, busMigrationHashing, busMigrationConverting,
		busMigrationRestoring, busMigrationRollingBack, busMigrationRollbackRestoring, busMigrationReleasing, busMigrationAbandoned}
	if len(seen) != len(want) {
		t.Fatalf("phases %v, want %v", seen, want)
	}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("phases %v, want %v", seen, want)
		}
	}
	if !last.abandoning {
		t.Error("a rolled-back run does not read as abandoned")
	}
}

// A pending pass changes nothing, and three to one never freezes.
func TestABusMigrationWaitsAndThreeToOneNeverFreezes(t *testing.T) {
	for _, phase := range []busMigrationPhase{busMigrationPinning, busMigrationShrinking, busMigrationUnpinning} {
		if busMigrationFrozen(busMigrationShrink, phase) {
			t.Errorf("three to one frozen in %s", phase)
		}
	}
	got := nextBusMigrationPhase(busMigrationGrow, busMigrationHashing, false, busMigrationPending)
	if got.next != busMigrationHashing || got.abandoning {
		t.Errorf("a pending Hashing pass moved to %+v", got)
	}
	for _, phase := range []busMigrationPhase{busMigrationSnapshot, busMigrationHashing, busMigrationConverting, busMigrationRestoring, busMigrationRollingBack, busMigrationReleasing} {
		if !busMigrationFrozen(busMigrationGrow, phase) {
			t.Errorf("one to three not frozen in %s", phase)
		}
	}
}

// The bus renders the old count until the phase that converts it, the new
// one from there, and the old one again on a rollback.
func TestTheBusRendersTheCountItsPhaseCalls(t *testing.T) {
	for _, tc := range []struct {
		leg        busMigrationLeg
		phase      busMigrationPhase
		abandoning bool
		want       int
	}{
		{busMigrationShrink, busMigrationPinning, false, 3},
		{busMigrationShrink, busMigrationUnpinning, true, 3},
		{busMigrationShrink, busMigrationShrinking, false, 1},
		{busMigrationShrink, busMigrationDone, false, 1},
		{busMigrationShrink, busMigrationAbandoned, true, 3},
		{busMigrationShrink, busMigrationFailed, false, 1},
		{busMigrationShrink, busMigrationFailed, true, 3},
		{busMigrationGrow, busMigrationFreezing, false, 1},
		{busMigrationGrow, busMigrationSnapshot, false, 1},
		{busMigrationGrow, busMigrationHashing, false, 1},
		{busMigrationGrow, busMigrationConverting, false, 3},
		{busMigrationGrow, busMigrationRestoring, false, 3},
		{busMigrationGrow, busMigrationRollingBack, true, 1},
		{busMigrationGrow, busMigrationRollbackRestoring, true, 1},
		{busMigrationGrow, busMigrationReleasing, false, 3},
		{busMigrationGrow, busMigrationReleasing, true, 1},
		{busMigrationGrow, busMigrationDone, false, 3},
		{busMigrationGrow, busMigrationAbandoned, true, 1},
		{busMigrationGrow, busMigrationFailed, true, 1},
	} {
		if got := busMigrationReplicas(tc.leg, tc.phase, tc.abandoning); got != tc.want {
			t.Errorf("%s %s (abandoning %v) renders %d, want %d", tc.leg, tc.phase, tc.abandoning, got, tc.want)
		}
	}
}

// A Job's outcome is its Complete or Failed condition; still pending past
// the run's deadline counts as failed.
func TestAMigrationJobsOutcomeAndTheDeadline(t *testing.T) {
	job := func(cond batchv1.JobConditionType, status corev1.ConditionStatus) *batchv1.Job {
		j := &batchv1.Job{}
		if cond != "" {
			j.Status.Conditions = []batchv1.JobCondition{{Type: cond, Status: status}}
		}
		return j
	}
	for _, tc := range []struct {
		job  *batchv1.Job
		want busMigrationOutcome
	}{
		{job("", ""), busMigrationPending},
		{job(batchv1.JobComplete, corev1.ConditionTrue), busMigrationSucceeded},
		{job(batchv1.JobFailed, corev1.ConditionTrue), busMigrationErred},
		{job(batchv1.JobComplete, corev1.ConditionFalse), busMigrationPending},
	} {
		if got := jobOutcome(tc.job); got != tc.want {
			t.Errorf("jobOutcome(%+v) = %v, want %v", tc.job.Status.Conditions, got, tc.want)
		}
	}
	now := time.Now()
	if withDeadline(busMigrationPending, now.Add(-time.Second), now) != busMigrationErred {
		t.Error("a pending phase past the deadline is not failed")
	}
	if withDeadline(busMigrationPending, now.Add(time.Hour), now) != busMigrationPending {
		t.Error("a pending phase before the deadline was failed")
	}
	if withDeadline(busMigrationSucceeded, now.Add(-time.Second), now) != busMigrationSucceeded {
		t.Error("a finished phase was failed by the deadline")
	}
}

// migrationRig is a CR mid-migration on a fake cluster, with helpers to make
// the phase's work finish the way the cluster would report it.
type migrationRig struct {
	t     *testing.T
	agent *agentv1alpha1.PlatformAgent
	cl    client.Client
	r     *PlatformAgentReconciler
	sts   *appsv1.StatefulSet
}

func newMigrationRig(t *testing.T, leg busMigrationLeg, phase busMigrationPhase, running int32, deadline time.Time) *migrationRig {
	t.Helper()
	agent := a2aTestAgent()
	agent.Generation = 3
	asked := 1
	if leg == busMigrationGrow {
		asked = 3
	}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{BusReplicas: ptr.To(asked)}}
	agent.Status.BusReplicas = running
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(leg), RunID: "3-1", Phase: string(phase),
		StartedAt: metav1.Now(), Deadline: metav1.NewTime(deadline), ObservedGeneration: 3}
	sts := buildA2ANATSStatefulSet(agent, "h")
	sts.Spec.Replicas = ptr.To(running)
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, sts).WithStatusSubresource(&appsv1.StatefulSet{}, &batchv1.Job{}, &corev1.Pod{}).Build()
	return &migrationRig{t: t, agent: agent, cl: cl, r: &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(16)}, sts: sts}
}

// pass runs the resolver and checks the phase and the count it renders.
func (m *migrationRig) pass(wantPhase busMigrationPhase, wantReplicas int32) {
	m.t.Helper()
	if _, err := m.r.resolveA2ABusReplicas(context.Background(), m.agent); err != nil {
		m.t.Fatal(err)
	}
	if got := busMigrationPhase(m.agent.Status.BusMigration.Phase); got != wantPhase {
		m.t.Fatalf("phase %s, want %s", got, wantPhase)
	}
	if m.agent.Status.BusReplicas != wantReplicas || int32(a2aNATSReplicas(m.agent)) != wantReplicas {
		m.t.Fatalf("in %s: status.busReplicas %d, rendered %d; want %d", wantPhase, m.agent.Status.BusReplicas, a2aNATSReplicas(m.agent), wantReplicas)
	}
}

func (m *migrationRig) finishJob(step string, cond batchv1.JobConditionType) {
	m.t.Helper()
	j := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, step, "3-1"), Namespace: m.agent.Namespace}}
	if err := m.cl.Create(context.Background(), j); err != nil {
		m.t.Fatal(err)
	}
	j.Status.Conditions = []batchv1.JobCondition{{Type: cond, Status: corev1.ConditionTrue}}
	if err := m.cl.Status().Update(context.Background(), j); err != nil {
		m.t.Fatal(err)
	}
}

func (m *migrationRig) rollOut(replicas int32) {
	m.t.Helper()
	sts := &appsv1.StatefulSet{}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(m.sts), sts); err != nil {
		m.t.Fatal(err)
	}
	sts.Spec.Replicas = ptr.To(replicas)
	if err := m.cl.Update(context.Background(), sts); err != nil {
		m.t.Fatal(err)
	}
	sts.Status = appsv1.StatefulSetStatus{ObservedGeneration: sts.Generation, Replicas: replicas, ReadyReplicas: replicas, UpdatedReplicas: replicas}
	if err := m.cl.Status().Update(context.Background(), sts); err != nil {
		m.t.Fatal(err)
	}
}

// Three to one through the pass: the pin Job's completion moves it to
// Shrinking, which renders one server, and that server rolled out is Done.
func TestAThreeToOneMigrationRunsThroughThePass(t *testing.T) {
	m := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	m.pass(busMigrationPinning, 3)
	m.finishJob("pin", batchv1.JobComplete)
	m.pass(busMigrationShrinking, 1)
	m.pass(busMigrationShrinking, 1)
	m.rollOut(1)
	m.pass(busMigrationDone, 1)
}

// A failed pin Job, or a pin still running at the deadline, unpins and keeps
// three servers.
func TestAFailedOrLatePinUnpins(t *testing.T) {
	m := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	m.finishJob("pin", batchv1.JobFailed)
	m.pass(busMigrationUnpinning, 3)
	late := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(-time.Minute))
	late.pass(busMigrationUnpinning, 3)
}

// One to three through the pass up to the restore: the freeze rolled out,
// the snapshot Job, the hashing pod, then three servers rendered and rolled
// out. A conversion still unfinished at the deadline rolls back to one.
func TestAOneToThreeMigrationRunsThroughThePass(t *testing.T) {
	m := newMigrationRig(t, busMigrationGrow, busMigrationFreezing, 1, time.Now().Add(time.Hour))
	m.rollOut(1)
	m.pass(busMigrationSnapshot, 1)
	m.finishJob("snapshot", batchv1.JobComplete)
	m.pass(busMigrationHashing, 1)
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, "hash", "3-1"), Namespace: m.agent.Namespace}}
	if err := m.cl.Create(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	pod.Status.Phase = corev1.PodSucceeded
	if err := m.cl.Status().Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	m.pass(busMigrationConverting, 3)
	m.rollOut(3)
	m.pass(busMigrationRestoring, 3)

	late := newMigrationRig(t, busMigrationGrow, busMigrationConverting, 3, time.Now().Add(-time.Minute))
	late.pass(busMigrationRollingBack, 1)
}

// No provision Job is created while a migration runs: on one to three it
// would create the streams empty ahead of the restore. After the run ends
// the Job runs as usual.
func TestAMigrationHoldsTheProvisionJob(t *testing.T) {
	agent := a2aTestAgent()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, discordBotSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}, &appsv1.StatefulSet{}, &appsv1.Deployment{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(32)}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "1-1", Phase: string(busMigrationRestoring),
		StartedAt: metav1.Now(), Deadline: metav1.NewTime(time.Now().Add(time.Hour))}
	agent.Status.BusReplicas = 3
	// The bus has rolled out, the callout serves: only the migration holds.
	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, sts); err != nil {
		t.Fatal(err)
	}
	sts.Spec.Replicas = ptr.To(int32(3))
	if err := cl.Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
	sts.Status = appsv1.StatefulSetStatus{ObservedGeneration: sts.Generation + 1, Replicas: 3, ReadyReplicas: 3, UpdatedReplicas: 3, CurrentReplicas: 3}
	if err := cl.Status().Update(ctx, sts); err != nil {
		t.Fatal(err)
	}
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatal(err)
	}
	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatal(err)
	}
	for _, j := range jobs.Items {
		if j.Name == buildA2AProvisionJob(agent).Name {
			t.Fatalf("the provision Job %s was created while a migration is restoring", j.Name)
		}
	}
	if !state.jobHeld {
		t.Error("the pass does not report the Job held")
	}
	done := a2aTestAgent()
	done.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Phase: string(busMigrationDone)}
	if busMigrationHoldsProvision(done) {
		t.Error("a finished migration still holds the provision Job")
	}
}

// The condition says what the run is doing: True while it runs (with the
// freeze named when writes are frozen), False when it ended without
// converting or stopped for a person (with the step), absent once Done.
func TestTheBusMigratingConditionFollowsTheRun(t *testing.T) {
	for _, tc := range []struct {
		leg        busMigrationLeg
		phase      busMigrationPhase
		abandoning bool
		message    string
		present    bool
		status     metav1.ConditionStatus
		reason     string
		contains   []string
	}{
		{busMigrationGrow, busMigrationSnapshot, false, "", true, metav1.ConditionTrue, "InProgress", []string{"from 1 NATS servers to 3", "Snapshotting", "writes are frozen"}},
		{busMigrationShrink, busMigrationPinning, false, "", true, metav1.ConditionTrue, "InProgress", []string{"from 3 NATS servers to 1", "Pinning"}},
		{busMigrationGrow, busMigrationRollingBack, true, "", true, metav1.ConditionTrue, "InProgress", []string{"undoing", "writes are frozen"}},
		{busMigrationGrow, busMigrationAbandoned, true, "", true, metav1.ConditionFalse, "Abandoned", []string{"runs 1 servers with its data"}},
		{busMigrationGrow, busMigrationFailed, true, "restore it by hand", true, metav1.ConditionFalse, "Failed", []string{"restore it by hand"}},
		{busMigrationGrow, busMigrationDone, false, "", false, "", "", nil},
	} {
		agent := a2aTestAgent()
		agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(tc.leg), Phase: string(tc.phase), Abandoning: tc.abandoning, Message: tc.message}
		cond, ok := busMigratingCondition(agent)
		if ok != tc.present {
			t.Errorf("%s %s: present %v, want %v", tc.leg, tc.phase, ok, tc.present)
			continue
		}
		if !ok {
			continue
		}
		if cond.Status != tc.status || cond.Reason != tc.reason {
			t.Errorf("%s %s: %s/%s, want %s/%s", tc.leg, tc.phase, cond.Status, cond.Reason, tc.status, tc.reason)
		}
		for _, s := range tc.contains {
			if !strings.Contains(cond.Message, s) {
				t.Errorf("%s %s: message %q lacks %q", tc.leg, tc.phase, cond.Message, s)
			}
		}
		if tc.leg == busMigrationShrink && strings.Contains(cond.Message, "frozen") {
			t.Errorf("three to one reports a freeze: %q", cond.Message)
		}
	}
}

// The status write carries the run and its condition, and drops the
// condition once the run converted the bus.
func TestTheStatusWriteCarriesTheMigration(t *testing.T) {
	ctx := context.Background()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(8)}
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "1-1", Phase: string(busMigrationHashing), ObservedGeneration: 1}
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{done: true}); err != nil {
		t.Fatal(err)
	}
	live := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
		t.Fatal(err)
	}
	if live.Status.BusMigration == nil || live.Status.BusMigration.Phase != string(busMigrationHashing) {
		t.Fatalf("status.busMigration after the write: %+v", live.Status.BusMigration)
	}
	if c := meta.FindStatusCondition(live.Status.Conditions, a2aBusMigratingConditionType); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("condition after the write: %+v", c)
	}
	agent = live
	agent.Status.BusMigration.Phase = string(busMigrationDone)
	if err := r.syncA2AConditions(ctx, agent, a2aProvisionState{done: true}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(agent), live); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(live.Status.Conditions, a2aBusMigratingConditionType); c != nil {
		t.Errorf("condition after a Done run: %+v, want it gone", c)
	}
	if live.Status.BusMigration == nil || live.Status.BusMigration.Phase != string(busMigrationDone) {
		t.Errorf("the Done run was not written: %+v", live.Status.BusMigration)
	}
}
