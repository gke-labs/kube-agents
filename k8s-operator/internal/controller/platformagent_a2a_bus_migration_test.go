package controller

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	"github.com/gke-labs/kube-agents/k8s-operator/internal/busmigrate"
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
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(j), j); err != nil {
		if err := m.cl.Create(context.Background(), j); err != nil {
			m.t.Fatal(err)
		}
	}
	j.Status.Conditions = []batchv1.JobCondition{{Type: cond, Status: corev1.ConditionTrue}}
	if err := m.cl.Status().Update(context.Background(), j); err != nil {
		m.t.Fatal(err)
	}
}

// finishPod ends one of the run's pods as the kubelet would report it, with
// its termination message, and reason (DeadlineExceeded for a pod stopped at
// activeDeadlineSeconds).
func (m *migrationRig) finishPod(name string, phase corev1.PodPhase, message, reason string) {
	m.t.Helper()
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, name, "3-1"), Namespace: m.agent.Namespace}}
	if err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(pod), pod); err != nil {
		if err := m.cl.Create(context.Background(), pod); err != nil {
			m.t.Fatal(err)
		}
	}
	pod.Status.Phase, pod.Status.Reason = phase, reason
	pod.Status.ContainerStatuses = []corev1.ContainerStatus{{Name: "bus-" + name, State: corev1.ContainerState{Terminated: &corev1.ContainerStateTerminated{Message: message}}}}
	if err := m.cl.Status().Update(context.Background(), pod); err != nil {
		m.t.Fatal(err)
	}
}

// validDigests is a hashing pod's report for every provisioned stream.
func validDigests() string {
	d := map[string]busmigrate.StagedDigest{}
	for i, n := range a2aProvisionedStreams {
		d[n] = busmigrate.StagedDigest{Data: fmt.Sprintf("%064x", i+1), Meta: fmt.Sprintf("%064x", i+100)}
	}
	b, _ := json.Marshal(d)
	return string(b)
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
	recorder := record.NewFakeRecorder(16)
	m.r.Recorder = recorder
	m.rollOut(1)
	m.pass(busMigrationSnapshot, 1)
	m.finishPod("snapshot", corev1.PodSucceeded, "", "")
	m.pass(busMigrationHashing, 1)
	m.finishPod("hash", corev1.PodSucceeded, validDigests(), "")
	m.pass(busMigrationConverting, 3)

	// The run Secret holds the hashing pod's digests and the operator's
	// policies, at one replica, owned by the CR.
	secret := &corev1.Secret{}
	if err := m.cl.Get(context.Background(), client.ObjectKey{Namespace: m.agent.Namespace, Name: busMigrationRunSecret(m.agent, "3-1")}, secret); err != nil {
		t.Fatalf("no run Secret after hashing: %v", err)
	}
	var run busmigrate.RunFile
	if err := json.Unmarshal(secret.Data[busMigrationRunKey], &run); err != nil {
		t.Fatal(err)
	}
	wantDigests, _ := parseBusDigests(validDigests())
	if !reflect.DeepEqual(run.Digests, wantDigests) || !reflect.DeepEqual(run.Policies, a2aStreamPolicies(1)) || !ownedByAgent(secret.ObjectMeta, m.agent) {
		t.Errorf("run Secret %+v, owners %v", run, secret.OwnerReferences)
	}

	m.rollOut(3)
	m.pass(busMigrationRestoring, 3)
	m.finishPod("restore", corev1.PodSucceeded, `{"warnings":["stream TOPICS-STATE: subject x is missing; a fresh install has it"]}`, "")
	m.pass(busMigrationReleasing, 3)
	if w := m.agent.Status.BusMigration.Warnings; len(w) != 1 || !strings.Contains(w[0], "subject x is missing") {
		t.Errorf("status warnings %v", w)
	}
	select {
	case e := <-recorder.Events:
		if !strings.Contains(e, "Warning BusMigrationDrift") || !strings.Contains(e, "subject x is missing") {
			t.Errorf("event %q", e)
		}
	default:
		t.Error("no Warning Event for the restore's warnings")
	}
	m.pass(busMigrationDone, 3)
	select {
	case e := <-recorder.Events:
		t.Errorf("a second event for the same warnings: %q", e)
	default:
	}

	late := newMigrationRig(t, busMigrationGrow, busMigrationConverting, 3, time.Now().Add(-time.Minute))
	late.pass(busMigrationRollingBack, 1)
}

// A hashing pod whose report is not exactly the provisioned streams' digest
// pairs writes no run Secret, and the run turns back naming why.
func TestARefusedDigestReportTurnsTheRunBack(t *testing.T) {
	m := newMigrationRig(t, busMigrationGrow, busMigrationHashing, 1, time.Now().Add(time.Hour))
	m.finishPod("hash", corev1.PodSucceeded, `{"TASKS":{"data":"x","meta":"y"}}`, "")
	if _, err := m.r.resolveA2ABusReplicas(context.Background(), m.agent); err != nil {
		t.Fatal(err)
	}
	run := m.agent.Status.BusMigration
	if !run.Abandoning || !strings.Contains(run.Cause, "the hashing pod's report was refused") {
		t.Errorf("phase %s abandoning %v cause %q", run.Phase, run.Abandoning, run.Cause)
	}
	if err := m.cl.Get(context.Background(), client.ObjectKey{Namespace: m.agent.Namespace, Name: busMigrationRunSecret(m.agent, "3-1")}, &corev1.Secret{}); err == nil {
		t.Error("a run Secret from a refused report")
	}
}

// A restore pod that fails rolls back, and the cause carries its report's
// error; a pod stopped at its deadline names the deadline; warnings the
// rollback repeats are recorded once.
func TestAFailedRestoreRollsBackNamingItsError(t *testing.T) {
	m := newMigrationRig(t, busMigrationGrow, busMigrationRestoring, 3, time.Now().Add(time.Hour))
	m.finishPod("restore", corev1.PodFailed, `{"error":"stream TASKS: config is outside the operator's policy: subjects: \"a2a.>\" is not one of [a2a.tasks.>]"}`, "")
	m.pass(busMigrationRollingBack, 1)
	if c := m.agent.Status.BusMigration.Cause; !strings.Contains(c, "the Restoring phase failed: stream TASKS: config is outside the operator's policy") {
		t.Errorf("cause %q", c)
	}

	// Killed with no report and no deadline (evicted): the phase failed,
	// with no word about a report it never wrote.
	evicted := newMigrationRig(t, busMigrationGrow, busMigrationRestoring, 3, time.Now().Add(time.Hour))
	evicted.finishPod("restore", corev1.PodFailed, "", "Evicted")
	evicted.pass(busMigrationRollingBack, 1)
	if c := evicted.agent.Status.BusMigration.Cause; c != "the Restoring phase failed" {
		t.Errorf("evicted restore cause %q", c)
	}

	// Killed at its deadline: whatever it reported, or nothing, the cause
	// is the deadline.
	for _, msg := range []string{"", `{"error":"context canceled"}`} {
		late := newMigrationRig(t, busMigrationGrow, busMigrationRollbackRestoring, 1, time.Now().Add(time.Hour))
		late.agent.Status.BusMigration.Abandoning = true
		late.finishPod("rollback", corev1.PodFailed, msg, "DeadlineExceeded")
		if _, err := late.r.resolveA2ABusReplicas(context.Background(), late.agent); err != nil {
			t.Fatal(err)
		}
		if c := late.agent.Status.BusMigration.Cause; !strings.Contains(c, "the RollbackRestoring phase was still running at the run's deadline") {
			t.Errorf("rollback stopped at its deadline (report %q): cause %q", msg, c)
		}
	}

	// The rollback restores the same snapshot, so it reports the same
	// warnings: they are recorded once, with one Event.
	twice := newMigrationRig(t, busMigrationGrow, busMigrationRestoring, 3, time.Now().Add(time.Hour))
	recorder := record.NewFakeRecorder(16)
	twice.r.Recorder = recorder
	const warned = `{"warnings":["stream TASKS: max_consumers is -1; a fresh install has 110"],"error":"stream KV_cap: finishing: boom"}`
	twice.finishPod("restore", corev1.PodFailed, warned, "")
	twice.pass(busMigrationRollingBack, 1)
	twice.rollOut(1)
	twice.pass(busMigrationRollbackRestoring, 1)
	twice.finishPod("rollback", corev1.PodSucceeded, `{"warnings":["stream TASKS: max_consumers is -1; a fresh install has 110"]}`, "")
	twice.pass(busMigrationReleasing, 1)
	if n := len(recorder.Events); n != 1 {
		t.Errorf("%d Events for one set of warnings, want 1", n)
	}
	if w := twice.agent.Status.BusMigration.Warnings; len(w) != 1 {
		t.Errorf("warnings %v", w)
	}
}

func TestTheHashingReportIsReadStrictly(t *testing.T) {
	good, refused := parseBusDigests(validDigests())
	if refused != "" || len(good) != len(a2aProvisionedStreams) {
		t.Fatalf("a valid report refused: %s", refused)
	}
	var pairs map[string]busmigrate.StagedDigest
	_ = json.Unmarshal([]byte(validDigests()), &pairs)
	edit := func(f func(map[string]busmigrate.StagedDigest)) string {
		c := map[string]busmigrate.StagedDigest{}
		for k, v := range pairs {
			c[k] = v
		}
		f(c)
		b, _ := json.Marshal(c)
		return string(b)
	}
	for name, msg := range map[string]string{
		"not JSON":       "TASKS deadbeef",
		"a stream short": edit(func(c map[string]busmigrate.StagedDigest) { delete(c, "TASKS") }),
		"an extra":       edit(func(c map[string]busmigrate.StagedDigest) { c["OTHER"] = c["TASKS"] }),
		"a renamed":      edit(func(c map[string]busmigrate.StagedDigest) { c["OTHER"] = c["TASKS"]; delete(c, "TASKS") }),
		"short hex": edit(func(c map[string]busmigrate.StagedDigest) {
			c["TASKS"] = busmigrate.StagedDigest{Data: "abc", Meta: c["TASKS"].Meta}
		}),
		"uppercase hex": edit(func(c map[string]busmigrate.StagedDigest) {
			d := c["KV_cap"]
			d.Meta = strings.ToUpper(d.Meta)
			c["KV_cap"] = d
		}),
		"no meta": edit(func(c map[string]busmigrate.StagedDigest) {
			c["TASKS"] = busmigrate.StagedDigest{Data: c["TASKS"].Data}
		}),
	} {
		if _, refused := parseBusDigests(msg); refused == "" {
			t.Errorf("%s: accepted", name)
		}
	}
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

// A migration step's bus principal has exactly the JetStream API verbs its
// leg sends, on the provisioned streams by name, subscribes to its own inbox
// only, and is in the identity map only while its step runs.
func TestAMigrationPrincipalHasExactlyItsStepsGrants(t *testing.T) {
	type grant struct {
		leg    busMigrationLeg
		verbs  []string
		extra  []string // per stream, after the verbs; %s is the stream
		banned []string
	}
	shrinkBanned := []string{"SNAPSHOT", "RESTORE", "DELETE", "PURGE", "CREATE", "CONSUMER", "MSG.DELETE"}
	stream := grant{busMigrationGrow, []string{"INFO", "SNAPSHOT", "RESTORE", "UPDATE", "DELETE"},
		[]string{"$JS.SNAPSHOT.ACK.%s.>", "$JS.SNAPSHOT.RESTORE.%s.>"}, []string{"PURGE", "CREATE", "CONSUMER", "MSG.DELETE", "LEADER.STEPDOWN"}}
	want := map[busMigrationPhase]grant{
		busMigrationPinning:           {busMigrationShrink, []string{"INFO", "LEADER.STEPDOWN", "UPDATE"}, nil, shrinkBanned},
		busMigrationUnpinning:         {busMigrationShrink, []string{"INFO", "UPDATE"}, nil, shrinkBanned},
		busMigrationSnapshot:          stream,
		busMigrationRestoring:         stream,
		busMigrationRollbackRestoring: stream,
	}
	streamUsers := map[string]bool{}
	for phase, g := range want {
		agent := a2aTestAgent()
		agent.Spec.Mode = ptr.To("next")
		agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(g.leg), RunID: "5-9", Phase: string(phase)}
		ids := busMigrationIdentities(agent)
		if len(ids) != 1 {
			t.Fatalf("%s: %d principals, want 1", phase, len(ids))
		}
		id := ids[0]
		var expected []string
		for _, s := range a2aProvisionedStreams {
			for _, v := range g.verbs {
				expected = append(expected, "$JS.API.STREAM."+v+"."+s)
			}
			for _, e := range g.extra {
				expected = append(expected, fmt.Sprintf(e, s))
			}
		}
		if !slices.Equal(id.publish, expected) {
			t.Errorf("%s publish:\n got %v\nwant %v", phase, id.publish, expected)
		}
		for _, p := range id.publish {
			if strings.HasSuffix(p, ".*") || strings.Contains(p, "STREAM.>") || p == "$JS.API.>" || strings.HasPrefix(p, "$JS.API.STREAM.*") ||
				p == "$JS.SNAPSHOT.RESTORE.>" || p == "$JS.SNAPSHOT.ACK.>" || p == "$JS.SNAPSHOT.>" {
				t.Errorf("%s: wildcard over stream names: %s", phase, p)
			}
			for _, banned := range g.banned {
				if strings.Contains(p, "."+banned+".") {
					t.Errorf("%s grants %s: %s", phase, banned, p)
				}
			}
		}
		if !slices.Equal(id.subscribe, []string{"_INBOX." + id.user + ".>"}) {
			t.Errorf("%s subscribe %v, want its own inbox only", phase, id.subscribe)
		}
		if g.leg == busMigrationGrow {
			streamUsers[id.user+" "+id.serviceAccount] = true
		}
		// The callout refuses a whole map whose user is not one lowercase
		// DNS-1123 label (it becomes the inbox prefix), so a long run ID
		// must still fit; the ServiceAccount and pod names must be one too.
		long := "9999999-1999999999"
		for _, name := range []string{id.user, busMigrationPrincipal(busMigrationStreamIdentity, long), busMigrationWorkloadName(agent, "rollback", long), busMigrationRunSecret(agent, long)} {
			if errs := validation.IsDNS1123Label(name); len(errs) != 0 {
				t.Errorf("%q is not a DNS-1123 label: %v", name, errs)
			}
		}
		if id.auth != a2aAuthCallout || !strings.Contains(id.serviceAccount, "5-9") {
			t.Errorf("%s: auth %v by %q, want the callout and a ServiceAccount named for the run", phase, id.auth, id.serviceAccount)
		}
		found := false
		for _, all := range a2aIdentities(agent) {
			found = found || all.user == id.user
		}
		if !found {
			t.Errorf("%s: the principal is not in the identity map", phase)
		}
	}
	// The snapshot, restore and rollback pods hold one identity.
	if len(streamUsers) != 1 {
		t.Errorf("the one to three leg's phases hold %d identities, want one: %v", len(streamUsers), streamUsers)
	}
	// Outside those phases, the hashing pod's included, and after the run,
	// no migration principal.
	for _, phase := range []busMigrationPhase{busMigrationShrinking, busMigrationFreezing, busMigrationHashing, busMigrationConverting,
		busMigrationRollingBack, busMigrationReleasing, busMigrationDone, busMigrationAbandoned, busMigrationFailed} {
		agent := a2aTestAgent()
		agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "5-9", Phase: string(phase)}
		if ids := busMigrationIdentities(agent); len(ids) != 0 {
			t.Errorf("%s: %d migration principals, want none", phase, len(ids))
		}
	}
}

// workload reads back the pod spec of a phase's Job or pod.
func (m *migrationRig) workload(step string) (corev1.PodSpec, metav1.ObjectMeta, bool) {
	m.t.Helper()
	key := client.ObjectKey{Namespace: m.agent.Namespace, Name: busMigrationWorkloadName(m.agent, step, "3-1")}
	if step != "pin" && step != "unpin" {
		pod := &corev1.Pod{}
		if err := m.cl.Get(context.Background(), key, pod); err != nil {
			return corev1.PodSpec{}, metav1.ObjectMeta{}, false
		}
		return pod.Spec, pod.ObjectMeta, true
	}
	j := &batchv1.Job{}
	if err := m.cl.Get(context.Background(), key, j); err != nil {
		return corev1.PodSpec{}, metav1.ObjectMeta{}, false
	}
	return j.Spec.Template.Spec, j.ObjectMeta, true
}

func ownedByAgent(meta metav1.ObjectMeta, agent *agentv1alpha1.PlatformAgent) bool {
	for _, o := range meta.OwnerReferences {
		if o.Kind == "PlatformAgent" && o.Name == agent.Name && o.Controller != nil && *o.Controller {
			return true
		}
	}
	return false
}

// The pin Job is created when its phase runs: its own ServiceAccount (owned
// by the CR, bound to no Role), no API token, the bus token projected for the
// bus audience, connecting as the step's principal over TLS, bounded by the
// run's deadline, on the operator's image.
func TestAPhaseCreatesItsJobAndIdentity(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	m.pass(busMigrationPinning, 3)
	spec, meta, ok := m.workload("pin")
	if !ok {
		t.Fatal("no pin Job after a pass in Pinning")
	}
	name := busMigrationWorkloadName(m.agent, "pin", "3-1")
	if !ownedByAgent(meta, m.agent) {
		t.Errorf("pin Job owners %v", meta.OwnerReferences)
	}
	sa := &corev1.ServiceAccount{}
	if err := m.cl.Get(context.Background(), client.ObjectKey{Namespace: m.agent.Namespace, Name: name}, sa); err != nil {
		t.Fatalf("no ServiceAccount %s: %v", name, err)
	}
	if !ownedByAgent(sa.ObjectMeta, m.agent) {
		t.Errorf("ServiceAccount owners %v", sa.OwnerReferences)
	}
	rbs := &rbacv1.RoleBindingList{}
	if err := m.cl.List(context.Background(), rbs); err != nil {
		t.Fatal(err)
	}
	for _, rb := range rbs.Items {
		for _, s := range rb.Subjects {
			if s.Name == name {
				t.Errorf("RoleBinding %s binds the migration identity", rb.Name)
			}
		}
	}
	if spec.ServiceAccountName != name || spec.AutomountServiceAccountToken == nil || *spec.AutomountServiceAccountToken {
		t.Errorf("serviceAccount %q automount %v", spec.ServiceAccountName, spec.AutomountServiceAccountToken)
	}
	if spec.RestartPolicy != corev1.RestartPolicyNever {
		t.Errorf("restartPolicy %s", spec.RestartPolicy)
	}
	c := spec.Containers[0]
	if c.Image != "registry.example/operator:v1" {
		t.Errorf("image %q", c.Image)
	}
	if c.SecurityContext == nil || c.SecurityContext.ReadOnlyRootFilesystem == nil || !*c.SecurityContext.ReadOnlyRootFilesystem ||
		c.SecurityContext.AllowPrivilegeEscalation == nil || *c.SecurityContext.AllowPrivilegeEscalation {
		t.Errorf("container security %+v", c.SecurityContext)
	}
	args := strings.Join(c.Command, " ")
	for _, want := range []string{
		"/manager bus-migrate pin ",
		"--server " + a2aNATSTLSScheme + a2aNATSAddress(m.agent),
		"--ca-file " + a2aNATSCAFile(),
		"--user " + busMigrationPrincipal("pin", "3-1"),
		"--token-file " + a2aBusTokenPath + "/" + a2aBusTokenFile,
		"--streams " + strings.Join(a2aProvisionedStreams, ","),
		"--target " + a2aNATSName(m.agent) + "-0",
	} {
		if !strings.Contains(args, want) {
			t.Errorf("command %q lacks %q", args, want)
		}
	}
	var token bool
	for _, v := range spec.Volumes {
		if v.Projected == nil {
			continue
		}
		for _, src := range v.Projected.Sources {
			if src.ServiceAccountToken != nil {
				token = src.ServiceAccountToken.Audience == a2aBusTokenAudience
			}
		}
	}
	if !token {
		t.Error("no bus-audience token projected")
	}
	j := &batchv1.Job{}
	if err := m.cl.Get(context.Background(), client.ObjectKey{Namespace: m.agent.Namespace, Name: name}, j); err != nil {
		t.Fatal(err)
	}
	if d := j.Spec.ActiveDeadlineSeconds; d == nil || *d > 3300 || *d < 3200 {
		t.Errorf("activeDeadlineSeconds %v, want the hour left less the margin", d)
	}
}

// A transition creates the next phase's workload in the same pass: a failed
// pin starts the unpin Job, which raises the streams back to three.
func TestATransitionCreatesTheNextPhasesJob(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	m.finishJob("pin", batchv1.JobFailed)
	m.pass(busMigrationUnpinning, 3)
	spec, _, ok := m.workload("unpin")
	if !ok {
		t.Fatal("no unpin Job after the pin failed")
	}
	if args := strings.Join(spec.Containers[0].Command, " "); !strings.Contains(args, "bus-migrate unpin ") || !strings.Contains(args, "--replicas 3") ||
		!strings.Contains(args, "--user "+busMigrationPrincipal("unpin", "3-1")) {
		t.Errorf("unpin command %q", args)
	}
}

// Without OPERATOR_IMAGE nothing runs, and the phase waits out the deadline.
func TestNoOperatorImageRunsNothing(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "")
	m := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	m.pass(busMigrationPinning, 3)
	if _, _, ok := m.workload("pin"); ok {
		t.Error("a pin Job with no image")
	}
}

const busMigrationMapFixturePath = "../../../a2a/authcallout/testdata/rendered-identity-map-migration.json"

// The map mid-snapshot, as the callout reads it (a2a/authcallout:
// migration_contract_test.go): the stream identity with its exact grants
// and the run's deadline as its notAfter. The deadline is far off so the
// callout's real-server test can still connect as it.
func TestRenderedBusMigrationFixture(t *testing.T) {
	agent := authMapTestAgent()
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationGrow), RunID: "3-1", Phase: string(busMigrationSnapshot),
		Deadline: metav1.NewTime(time.Date(2099, 1, 1, 0, 0, 0, 0, time.UTC))}
	cm, _, err := buildA2AAuthMapConfigMap(agent, nil)
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}
	rendered := cm.Data[a2aAuthMapKey]
	var doc a2aAuthMapDocument
	if err := json.Unmarshal([]byte(rendered), &doc); err != nil {
		t.Fatal(err)
	}
	var stamped int
	for _, id := range doc.Identities {
		switch {
		case id.User == busMigrationPrincipal(busMigrationStreamIdentity, "3-1") && id.NotAfter != nil && id.NotAfter.Equal(agent.Status.BusMigration.Deadline.Time):
			stamped++
		case id.NotAfter != nil:
			t.Errorf("%s carries notAfter %v; only the run's principal expires", id.User, id.NotAfter)
		}
	}
	if stamped != 1 {
		t.Errorf("the stream identity is not in the map with notAfter at the run's deadline:\n%s", rendered)
	}
	checkFixture(t, busMigrationMapFixturePath, rendered)
}

// notAfter is refused on a narrowed entry here as the callout refuses it
// there, so a render that put one there fails the reconcile rather than
// the callout's parse.
func TestNotAfterOnANarrowedEntryFailsTheRender(t *testing.T) {
	deadline := time.Date(2099, 1, 1, 0, 0, 0, 0, time.UTC)
	ids := []a2aAuthMapIdentity{{ServiceAccount: "system:serviceaccount:ns:s", User: "session", Account: a2aAccountApp, Narrowing: a2aNarrowingPod, NotAfter: &deadline}}
	if err := validateA2AAuthMapIdentities(ids); err == nil || !strings.Contains(err.Error(), "notAfter") {
		t.Errorf("validate error %v, want a refusal naming notAfter", err)
	}
}

// Why a run turned back is kept in Cause and named in the condition: a phase
// still running at the deadline, or a Job the API server stopped at its
// activeDeadlineSeconds, says the deadline; any other failure says the phase
// failed. The first cause is kept through the way back.
func TestATurnedBackRunNamesItsCause(t *testing.T) {
	late := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(-time.Minute))
	late.pass(busMigrationUnpinning, 3)
	run := late.agent.Status.BusMigration
	if want := "the Pinning phase was still running at the run's deadline, " + run.Deadline.UTC().Format(time.RFC3339); run.Cause != want {
		t.Errorf("late pin cause %q, want %q", run.Cause, want)
	}

	stopped := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	j := &batchv1.Job{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(stopped.agent, "pin", "3-1"), Namespace: stopped.agent.Namespace}}
	if err := stopped.cl.Create(context.Background(), j); err != nil {
		t.Fatal(err)
	}
	j.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue, Reason: batchv1.JobReasonDeadlineExceeded}}
	if err := stopped.cl.Status().Update(context.Background(), j); err != nil {
		t.Fatal(err)
	}
	stopped.pass(busMigrationUnpinning, 3)
	if c := stopped.agent.Status.BusMigration.Cause; !strings.Contains(c, "still running at the run's deadline") {
		t.Errorf("a Job stopped at its deadline gives cause %q", c)
	}

	failed := newMigrationRig(t, busMigrationShrink, busMigrationPinning, 3, time.Now().Add(time.Hour))
	failed.finishJob("pin", batchv1.JobFailed)
	failed.pass(busMigrationUnpinning, 3)
	if c := failed.agent.Status.BusMigration.Cause; c != "the Pinning phase failed" {
		t.Errorf("failed pin cause %q", c)
	}
	// The unpin fails too: the run is Failed, and the cause stays the pin's.
	failed.finishJob("unpin", batchv1.JobFailed)
	failed.pass(busMigrationFailed, 3)
	if c := failed.agent.Status.BusMigration.Cause; c != "the Pinning phase failed" {
		t.Errorf("after the unpin failed, cause %q; want the first failure kept", c)
	}
	if cond, ok := busMigratingCondition(failed.agent); !ok || !strings.Contains(cond.Message, "(the Pinning phase failed)") {
		t.Errorf("Failed condition %q does not name the cause", cond.Message)
	}

	done := newMigrationRig(t, busMigrationShrink, busMigrationAbandoned, 3, time.Now().Add(time.Hour))
	done.agent.Status.BusMigration.Cause = "the Pinning phase failed"
	if cond, ok := busMigratingCondition(done.agent); !ok || !strings.Contains(cond.Message, "(the Pinning phase failed)") {
		t.Errorf("Abandoned condition %q does not name the cause", cond.Message)
	}
}

// Nothing valid outlives the run's deadline, and the work stops first: a
// step's principal expires at the deadline, and its Job is stopped minutes
// before it, so a slow step fails on its own time limit, not on an
// authorization error halfway through a stream.
func TestAStepsJobStopsBeforeItsCredentialExpires(t *testing.T) {
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.UTC)
	agent := a2aTestAgent()
	agent.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Leg: string(busMigrationShrink), RunID: "3-1", Phase: string(busMigrationPinning),
		Deadline: metav1.NewTime(now.Add(time.Hour))}
	job := buildBusMigrationJob(agent, agent.Status.BusMigration, "pin", now)
	jobEnd := now.Add(time.Duration(*job.Spec.ActiveDeadlineSeconds) * time.Second)
	ids := busMigrationIdentities(agent)
	if len(ids) != 1 || ids[0].notAfter == nil {
		t.Fatalf("identities %+v", ids)
	}
	if !ids[0].notAfter.Equal(agent.Status.BusMigration.Deadline.Time) {
		t.Errorf("the principal expires at %v; want the run's deadline %v", ids[0].notAfter, agent.Status.BusMigration.Deadline.Time)
	}
	if gap := ids[0].notAfter.Sub(jobEnd); gap < 5*time.Minute {
		t.Errorf("the Job stops %v before its principal expires; want a margin of minutes", gap)
	}
}

// provisionCreateLines is every `stream add` / `kv add` in the rendered
// provision script, continuations joined, keyed by stream name, as flag maps.
func provisionCreateLines(t *testing.T) map[string]map[string]string {
	t.Helper()
	script := buildA2AProvisionJob(a2aTestAgent()).Spec.Template.Spec.Containers[0].Command[2]
	script = strings.ReplaceAll(script, "\\\n", " ")
	lines := map[string]map[string]string{}
	for _, line := range strings.Split(script, "\n") {
		_, add, ok := strings.Cut(line, "|| $NATS ")
		if !ok {
			continue
		}
		f := strings.Fields(add)
		if len(f) < 3 || f[1] != "add" || (f[0] != "stream" && f[0] != "kv") {
			continue
		}
		name := f[2]
		if f[0] == "kv" {
			name = "KV_" + name
		}
		flags := map[string]string{"kind": f[0]}
		for _, tok := range f[3:] {
			k, v, _ := strings.Cut(strings.TrimPrefix(tok, "--"), "=")
			flags[k] = strings.Trim(v, `'"`)
		}
		lines[name] = flags
	}
	if len(lines) == 0 {
		t.Fatal("no create lines found in the provision script; this test would pass vacuously")
	}
	return lines
}

// The table the restore checks is the provision script, flag for flag: the
// same streams, and every value the script passes on a policy field.
func TestTheStreamPoliciesAreTheProvisionScripts(t *testing.T) {
	lines := provisionCreateLines(t)
	policies := a2aStreamPolicies(1)
	var names []string
	for _, p := range policies {
		names = append(names, p.Name)
	}
	if !slices.Equal(names, a2aProvisionedStreams) {
		t.Errorf("policy streams %v, provisioned %v", names, a2aProvisionedStreams)
	}
	dur := func(s string) time.Duration {
		if s == "" {
			return 0
		}
		d, err := time.ParseDuration(s)
		if err != nil {
			t.Fatalf("duration %q: %v", s, err)
		}
		return d
	}
	num := func(s string, absent int64) int64 {
		if s == "" {
			return absent
		}
		n, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			t.Fatalf("number %q: %v", s, err)
		}
		return n
	}
	for _, p := range policies {
		f, ok := lines[p.Name]
		if !ok {
			t.Errorf("%s: no create line in the provision script", p.Name)
			continue
		}
		if f["replicas"] != "${STREAM_REPLICAS}" {
			t.Errorf("%s: replicas %q, want the run's STREAM_REPLICAS", p.Name, f["replicas"])
		}
		if !strings.EqualFold(f["storage"], p.Storage.String()) {
			t.Errorf("%s: storage %q, policy %s", p.Name, f["storage"], p.Storage)
		}
		var want busmigrate.StreamPolicy
		switch f["kind"] {
		case "stream":
			if f["retention"] != "limits" || f["discard"] != "old" {
				t.Errorf("%s: retention %q discard %q", p.Name, f["retention"], f["discard"])
			}
			_, direct := f["allow-direct"]
			want = busmigrate.StreamPolicy{Name: p.Name, Subjects: strings.Split(f["subjects"], ","), Retention: nats.LimitsPolicy, Storage: nats.FileStorage,
				MaxBytes: num(f["max-bytes"], -1), MaxAge: dur(f["max-age"]), Discard: nats.DiscardOld, MaxMsgsPerSubject: num(f["max-msgs-per-subject"], -1),
				AllowDirect: direct, Replicas: 1}
		case "kv":
			bucket := strings.TrimPrefix(p.Name, "KV_")
			want = busmigrate.StreamPolicy{Name: p.Name, Subjects: []string{"$KV." + bucket + ".>"}, Retention: nats.LimitsPolicy, Storage: nats.FileStorage,
				MaxBytes: num(f["max-bucket-size"], -1), MaxAge: dur(f["ttl"]), Discard: nats.DiscardNew, MaxMsgsPerSubject: num(f["history"], 1),
				AllowRollup: true, DenyDelete: true, AllowDirect: true, Replicas: 1}
		}
		if !reflect.DeepEqual(p, want) {
			t.Errorf("%s: policy\n %+v\nscript\n %+v", p.Name, p, want)
		}
	}
}

// The table's KV implications, and every value the script leaves to the
// CLI's defaults, are what a fresh install creates (busmigrate's
// fresh_streams.json, captured with the provision image's CLI against the
// bus image's server): each reference stream passes its policy's Check.
func TestTheStreamPoliciesMatchAFreshInstall(t *testing.T) {
	fresh, err := busmigrate.LoadFreshStreams()
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range a2aStreamPolicies(1) {
		ref, ok := fresh.Streams[p.Name]
		if !ok {
			t.Errorf("%s: not in the fresh-install reference; regenerate it", p.Name)
			continue
		}
		if d := p.Check(ref); len(d) != 0 {
			t.Errorf("%s: a fresh install differs from the policy: %v", p.Name, d)
		}
	}
}

// The snapshot pod writes the staging claim as the stream identity; the
// hashing pod reads it read-only and holds no bus credential, no token and
// no API access; the restore and rollback pods read it read-only as the
// stream identity, with the run Secret, ending at three and one replicas.
func TestTheSnapshotWritesTheStageAndTheOthersReadIt(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	stage := func(spec corev1.PodSpec) (claim *corev1.PersistentVolumeClaimVolumeSource, mount *corev1.VolumeMount) {
		for i := range spec.Volumes {
			if spec.Volumes[i].PersistentVolumeClaim != nil {
				claim = spec.Volumes[i].PersistentVolumeClaim
				for j := range spec.Containers[0].VolumeMounts {
					if spec.Containers[0].VolumeMounts[j].Name == spec.Volumes[i].Name {
						mount = &spec.Containers[0].VolumeMounts[j]
					}
				}
			}
		}
		return claim, mount
	}
	streamSA := busMigrationWorkloadName(a2aTestAgent(), busMigrationStreamIdentity, "3-1")
	m := newMigrationRig(t, busMigrationGrow, busMigrationSnapshot, 1, time.Now().Add(time.Hour))
	m.pass(busMigrationSnapshot, 1)
	snap, meta, ok := m.workload("snapshot")
	if !ok {
		t.Fatal("no snapshot pod")
	}
	claim, mount := stage(snap)
	if claim == nil || claim.ClaimName != busMigrationStageClaim(m.agent, "3-1") || claim.ReadOnly || mount == nil || mount.ReadOnly {
		t.Errorf("snapshot stage claim %+v mount %+v, want %s read-write", claim, mount, busMigrationStageClaim(m.agent, "3-1"))
	}
	if args := strings.Join(snap.Containers[0].Command, " "); !strings.Contains(args, "bus-migrate snapshot ") || !strings.Contains(args, "--dir "+busMigrationStageMount) ||
		!strings.Contains(args, "--user "+busMigrationPrincipal(busMigrationStreamIdentity, "3-1")) {
		t.Errorf("snapshot command %q", args)
	}
	if snap.ServiceAccountName != streamSA || !ownedByAgent(meta, m.agent) || snap.ActiveDeadlineSeconds == nil || *snap.ActiveDeadlineSeconds > 3300 {
		t.Errorf("snapshot pod SA %q owners %v deadline %v", snap.ServiceAccountName, meta.OwnerReferences, snap.ActiveDeadlineSeconds)
	}

	m.finishPod("snapshot", corev1.PodSucceeded, "", "")
	m.pass(busMigrationHashing, 1)
	hash, meta, ok := m.workload("hash")
	if !ok {
		t.Fatal("no hashing pod")
	}
	if !ownedByAgent(meta, m.agent) {
		t.Errorf("hashing pod owners %v", meta.OwnerReferences)
	}
	claim, mount = stage(hash)
	if claim == nil || !claim.ReadOnly || mount == nil || !mount.ReadOnly {
		t.Errorf("hashing stage claim %+v mount %+v, want read-only", claim, mount)
	}
	if len(hash.Volumes) != 1 || len(hash.Containers[0].Env) != 0 || *hash.AutomountServiceAccountToken || hash.ServiceAccountName == streamSA {
		t.Errorf("hashing pod carries more than the stage: SA %q volumes %v env %v", hash.ServiceAccountName, hash.Volumes, hash.Containers[0].Env)
	}
	if args := strings.Join(hash.Containers[0].Command, " "); !strings.HasPrefix(args, "/manager bus-hash --dir "+busMigrationStageMount) || strings.Contains(args, "--token-file") {
		t.Errorf("hash command %q", args)
	}

	for kind, replicas := range map[string]string{"restore": "3", "rollback": "1"} {
		phase, running := busMigrationRestoring, int32(3)
		if kind == "rollback" {
			phase, running = busMigrationRollbackRestoring, 1
		}
		r := newMigrationRig(t, busMigrationGrow, phase, running, time.Now().Add(time.Hour))
		r.agent.Status.BusMigration.Abandoning = kind == "rollback"
		r.pass(phase, running)
		spec, _, ok := r.workload(kind)
		if !ok {
			t.Fatalf("no %s pod", kind)
		}
		claim, mount := stage(spec)
		if claim == nil || !claim.ReadOnly || mount == nil || !mount.ReadOnly {
			t.Errorf("%s stage claim %+v mount %+v, want read-only", kind, claim, mount)
		}
		var secret *corev1.SecretVolumeSource
		for _, v := range spec.Volumes {
			if v.Secret != nil {
				secret = v.Secret
			}
		}
		if secret == nil || secret.SecretName != busMigrationRunSecret(r.agent, "3-1") || secret.DefaultMode == nil || *secret.DefaultMode != 0o440 {
			t.Errorf("%s run Secret volume %+v", kind, secret)
		}
		args := strings.Join(spec.Containers[0].Command, " ")
		if !strings.Contains(args, "bus-migrate restore ") || !strings.Contains(args, "--run-file "+busMigrationRunMount+"/"+busMigrationRunKey) ||
			!strings.Contains(args, "--replicas "+replicas) || spec.ServiceAccountName != streamSA {
			t.Errorf("%s command %q SA %q", kind, args, spec.ServiceAccountName)
		}
	}
}

// The staging claim is created with the snapshot pod: ReadWriteOncePod,
// owned by the CR, on the default StorageClass, and big enough for every
// provisioned stream full to its cap.
func TestTheSnapshotPhaseCreatesAOnePodStage(t *testing.T) {
	t.Setenv(operatorImageEnvVar, "registry.example/operator:v1")
	m := newMigrationRig(t, busMigrationGrow, busMigrationSnapshot, 1, time.Now().Add(time.Hour))
	m.pass(busMigrationSnapshot, 1)
	pvc := &corev1.PersistentVolumeClaim{}
	if err := m.cl.Get(context.Background(), client.ObjectKey{Namespace: m.agent.Namespace, Name: busMigrationStageClaim(m.agent, "3-1")}, pvc); err != nil {
		t.Fatalf("no staging claim: %v", err)
	}
	if !slices.Equal(pvc.Spec.AccessModes, []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOncePod}) {
		t.Errorf("access modes %v, want ReadWriteOncePod alone", pvc.Spec.AccessModes)
	}
	if pvc.Spec.StorageClassName != nil || !ownedByAgent(pvc.ObjectMeta, m.agent) {
		t.Errorf("storage class %v owners %v", pvc.Spec.StorageClassName, pvc.OwnerReferences)
	}
	var caps int64
	for _, p := range a2aStreamPolicies(1) {
		caps += p.MaxBytes
	}
	got := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if got.Value() < caps || got.Value() > caps*12/10 || got.Value()%(1<<30) != 0 {
		t.Errorf("stage request %s for %d bytes of caps; want at least the caps, within 20%%, in whole GiB", got.String(), caps)
	}
	if got.String() != "32Gi" {
		t.Errorf("stage request %s, want 32Gi for today's caps (28.9 GiB with 10%% headroom)", got.String())
	}
}

// When a run ends its ServiceAccounts go; the stage and run Secret go too,
// unless it Failed, when they stay for the manual step.
func TestARunsEndCleansUpWhatItMade(t *testing.T) {
	for _, tc := range []struct {
		name      string
		from      busMigrationPhase
		running   int32
		abandon   bool
		finish    func(m *migrationRig)
		end       busMigrationPhase
		keepStage bool
	}{
		{"done", busMigrationReleasing, 3, false, func(m *migrationRig) { m.rollOut(3) }, busMigrationDone, false},
		{"abandoned", busMigrationReleasing, 1, true, func(m *migrationRig) { m.rollOut(1) }, busMigrationAbandoned, false},
		{"failed", busMigrationRollbackRestoring, 1, true, func(m *migrationRig) { m.finishPod("rollback", corev1.PodFailed, `{"error":"boom"}`, "") }, busMigrationFailed, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := newMigrationRig(t, busMigrationGrow, tc.from, tc.running, time.Now().Add(time.Hour))
			m.agent.Status.BusMigration.Abandoning = tc.abandon
			ns := m.agent.Namespace
			made := []client.Object{
				&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, busMigrationStreamIdentity, "3-1"), Namespace: ns}},
				&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: busMigrationWorkloadName(m.agent, "hash", "3-1"), Namespace: ns}},
				&corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: busMigrationRunSecret(m.agent, "3-1"), Namespace: ns}},
				&corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: busMigrationStageClaim(m.agent, "3-1"), Namespace: ns}},
			}
			for _, o := range made {
				if err := m.cl.Create(context.Background(), o); err != nil {
					t.Fatal(err)
				}
			}
			tc.finish(m)
			if _, err := m.r.resolveA2ABusReplicas(context.Background(), m.agent); err != nil {
				t.Fatal(err)
			}
			if got := busMigrationPhase(m.agent.Status.BusMigration.Phase); got != tc.end {
				t.Fatalf("phase %s, want %s", got, tc.end)
			}
			for _, o := range made {
				err := m.cl.Get(context.Background(), client.ObjectKeyFromObject(o), o)
				_, isSA := o.(*corev1.ServiceAccount)
				keep := !isSA && tc.keepStage
				if keep && err != nil {
					t.Errorf("%T %s was removed from a Failed run", o, o.GetName())
				}
				if !keep && err == nil {
					t.Errorf("%T %s is still there after the run ended %s", o, o.GetName(), tc.end)
				}
			}
		})
	}
}

// The operator may not list or watch Secrets, so the run Secret is read
// uncached (a2aReader): here the cached client refuses any Secret read, and
// the hashing phase still writes the Secret and moves on.
func TestTheRunSecretIsReadUncached(t *testing.T) {
	m := newMigrationRig(t, busMigrationGrow, busMigrationHashing, 1, time.Now().Add(time.Hour))
	m.finishPod("hash", corev1.PodSucceeded, validDigests(), "")
	m.r.APIReader = m.cl
	m.r.Client = interceptor.NewClient(m.cl.(client.WithWatch), interceptor.Funcs{
		Get: func(ctx context.Context, c client.WithWatch, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
			if _, ok := obj.(*corev1.Secret); ok {
				return fmt.Errorf("secrets is forbidden: cannot list or watch")
			}
			return c.Get(ctx, key, obj, opts...)
		},
	})
	m.pass(busMigrationConverting, 3)
}
