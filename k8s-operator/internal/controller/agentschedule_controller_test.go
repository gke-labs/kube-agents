package controller

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeScheduleGateway is the gateway as the reconciler sees it: the same
// idempotency key returns the same task, as the gateway's KV record does.
type fakeScheduleGateway struct {
	mu      sync.Mutex
	err     error
	tasks   map[string]string
	sends   []string
	origins []string
	// onSubmit, when set, runs at each submission, before it is answered.
	onSubmit func(ScheduleSubmission)
}

func (g *fakeScheduleGateway) Submit(_ context.Context, sub ScheduleSubmission) (string, error) {
	if g.onSubmit != nil {
		g.onSubmit(sub)
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	g.sends = append(g.sends, sub.IdempotencyKey)
	g.origins = append(g.origins, sub.Origin)
	if g.err != nil {
		return "", g.err
	}
	if g.tasks == nil {
		g.tasks = map[string]string{}
	}
	if id, ok := g.tasks[sub.IdempotencyKey]; ok {
		return id, nil
	}
	id := fmt.Sprintf("task-%d", len(g.tasks)+1)
	g.tasks[sub.IdempotencyKey] = id
	return id, nil
}

func (g *fakeScheduleGateway) distinctRuns() int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return len(g.tasks)
}

type fakeScheduleTracker struct {
	state ScheduleTaskState
	calls int
}

func (t *fakeScheduleTracker) State(context.Context, *agentv1alpha1.AgentSchedule, string) (ScheduleTaskState, error) {
	t.calls++
	return t.state, nil
}

// fakeScheduleStopper is the gateway's stop: it records the tasks it was
// asked to stop and answers result, or err.
type fakeScheduleStopper struct {
	result string
	err    error
	stops  []string
}

func (s *fakeScheduleStopper) Stop(_ context.Context, _ *agentv1alpha1.AgentSchedule, taskID string) (string, error) {
	s.stops = append(s.stops, taskID)
	return s.result, s.err
}

type scheduleFixture struct {
	r       *AgentScheduleReconciler
	c       client.Client
	gw      *fakeScheduleGateway
	tracker *fakeScheduleTracker
	stopper *fakeScheduleStopper
	clock   time.Time
	key     types.NamespacedName
}

// created is every fixture schedule's creation time: an hourly schedule's
// first slot is 01:00.
var scheduleCreated = time.Date(2026, 11, 11, 0, 30, 0, 0, time.UTC)

// scheduleAgent is the fixture's install: the PlatformAgent in the
// schedules' namespace.
func scheduleAgent() *agentv1alpha1.PlatformAgent {
	return &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: "kubeagents-system", UID: "agent-uid"}}
}

func newScheduleFixture(t *testing.T, mutate func(*agentv1alpha1.AgentSchedule), profileTiers ...string) *scheduleFixture {
	t.Helper()
	return newScheduleFixtureWith(t, mutate, true, profileTiers...)
}

func newScheduleFixtureWith(t *testing.T, mutate func(*agentv1alpha1.AgentSchedule), withAgent bool, profileTiers ...string) *scheduleFixture {
	t.Helper()
	if len(profileTiers) == 0 {
		profileTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	}
	profile := testAgentProfile("kubeagents-system", "fleet-audit", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = profileTiers
	})
	sched := &agentv1alpha1.AgentSchedule{
		ObjectMeta: metav1.ObjectMeta{
			Name: "compliance-audit", Namespace: "kubeagents-system", Generation: 1,
			CreationTimestamp: metav1.NewTime(scheduleCreated),
			Labels:            map[string]string{agentv1alpha1.AgentScheduleOriginLabel: agentv1alpha1.AgentScheduleOriginBuiltin},
			// Built-in by default: the install's PlatformAgent controls it.
			// User schedules don't fire yet (UserSchedulesDeferred).
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent",
				Name: "platform-agent", UID: "agent-uid", Controller: ptr.To(true),
			}},
		},
		Spec: agentv1alpha1.AgentScheduleSpec{
			Schedule:                "0 * * * *",
			ProfileRef:              agentv1alpha1.AgentScheduleProfileRef{Name: "fleet-audit"},
			Prompt:                  "Run the compliance audit.",
			StartingDeadlineSeconds: ptr.To[int64](900),
			ActiveDeadlineSeconds:   ptr.To[int64](3600),
		},
	}
	if mutate != nil {
		mutate(sched)
	}
	objs := []client.Object{&profile, sched}
	if withAgent {
		objs = append(objs, scheduleAgent())
	}
	c := fake.NewClientBuilder().WithScheme(setupScheme()).
		WithObjects(objs...).WithStatusSubresource(&agentv1alpha1.AgentSchedule{}).Build()
	f := &scheduleFixture{c: c, gw: &fakeScheduleGateway{}, tracker: &fakeScheduleTracker{state: ScheduleTaskState{Found: true}},
		stopper: &fakeScheduleStopper{result: triggerStopStopped}, key: client.ObjectKeyFromObject(sched)}
	// The fixture's schedule is one the PlatformAgent renders, so a labelled,
	// controlled one runs as built-in.
	builtin := sched.Name
	f.r = &AgentScheduleReconciler{Client: c, Scheme: c.Scheme(), Submitter: f.gw, Tracker: f.tracker, Stopper: f.stopper, Now: func() time.Time { return f.clock },
		BuiltinNames: func(*agentv1alpha1.PlatformAgent) []string { return []string{builtin} }}
	return f
}

func (f *scheduleFixture) at(t *testing.T, when time.Time) ctrl.Result {
	t.Helper()
	f.clock = when
	res, err := f.r.Reconcile(context.Background(), ctrl.Request{NamespacedName: f.key})
	if err != nil {
		t.Fatalf("Reconcile at %s: %v", when.Format(time.RFC3339), err)
	}
	return res
}

func (f *scheduleFixture) get(t *testing.T) *agentv1alpha1.AgentSchedule {
	t.Helper()
	var s agentv1alpha1.AgentSchedule
	if err := f.c.Get(context.Background(), f.key, &s); err != nil {
		t.Fatal(err)
	}
	return &s
}

func hourAt(h, m int) time.Time { return time.Date(2026, 11, 11, h, m, 0, 0, time.UTC) }

// A due slot is claimed, submitted with its idempotency key, and recorded;
// the schedule moves to its next slot.
func TestAgentScheduleFiresADueSlotOnce(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	if n := len(f.gw.sends); n != 0 {
		t.Fatalf("submitted %d runs before the first slot", n)
	}
	if got := f.get(t).Status.NextScheduleTime.UTC(); !got.Equal(hourAt(1, 0)) {
		t.Fatalf("next = %s, want 01:00", got)
	}
	f.at(t, hourAt(1, 0).Add(10*time.Second))
	s := f.get(t)
	if want := fmt.Sprintf("schedule/kubeagents-system/compliance-audit/%d", hourAt(1, 0).Unix()); len(f.gw.sends) != 1 || f.gw.sends[0] != want {
		t.Fatalf("sends = %v, want one with key %s", f.gw.sends, want)
	}
	if s.Status.ActiveTaskID != "task-1" || !s.Status.LastScheduleTime.UTC().Equal(hourAt(1, 0)) ||
		!s.Status.NextScheduleTime.UTC().Equal(hourAt(2, 0)) || s.Status.PendingSlot != nil {
		t.Fatalf("status after the run = %+v", s.Status)
	}
	if c := meta.FindStatusCondition(s.Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("Accepted = %+v", c)
	}
}

// The claim is in the API before the gateway sees the submission: a crash
// at the gateway call leaves the slot claimed, so the next leader re-sends it.
func TestAgentScheduleTheClaimIsStoredBeforeTheSubmission(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	var stored *agentv1alpha1.AgentSchedulePendingSlot
	f.gw.onSubmit = func(sub ScheduleSubmission) {
		var s agentv1alpha1.AgentSchedule
		if err := f.c.Get(context.Background(), f.key, &s); err != nil {
			t.Error(err)
			return
		}
		stored = s.Status.PendingSlot
		if stored == nil || stored.IdempotencyKey != sub.IdempotencyKey {
			t.Errorf("at submission the stored claim is %+v, want key %s", stored, sub.IdempotencyKey)
		}
	}
	f.at(t, hourAt(1, 0))
	if stored == nil {
		t.Fatal("no claim was stored before the submission")
	}
}

// A restart after the claim (and perhaps after the gateway took it) re-sends
// the same key, so the run is neither doubled nor dropped.
func TestAgentScheduleARestartAfterTheClaimResendsTheSameKey(t *testing.T) {
	slot := hourAt(1, 0)
	key := fmt.Sprintf("schedule/kubeagents-system/compliance-audit/%d", slot.Unix())
	f := newScheduleFixture(t, nil)
	// The gateway took it before the crash.
	if _, err := f.gw.Submit(context.Background(), ScheduleSubmission{IdempotencyKey: key}); err != nil {
		t.Fatal(err)
	}
	s := f.get(t)
	s.Status.NextScheduleTime = ptrTime(slot)
	s.Status.PendingSlot = &agentv1alpha1.AgentSchedulePendingSlot{SlotTime: metav1.NewTime(slot), IdempotencyKey: key, ClaimedAt: metav1.NewTime(slot)}
	if err := f.c.Status().Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, slot.Add(time.Minute))
	s = f.get(t)
	if f.gw.distinctRuns() != 1 || s.Status.ActiveTaskID != "task-1" || s.Status.PendingSlot != nil {
		t.Fatalf("runs = %d, status = %+v; want the one run adopted", f.gw.distinctRuns(), s.Status)
	}
}

// A gateway that stays down holds the claim and retries until the starting
// deadline, then counts the slot once as gatewayUnavailable, not again as a
// missed window.
func TestAgentScheduleAGatewayDownUntilTheDeadlineCountsOnce(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.gw.err = fmt.Errorf("dial: %w", ErrScheduleGatewayUnavailable)
	f.at(t, hourAt(0, 45))
	res := f.at(t, hourAt(1, 1))
	if s := f.get(t); s.Status.PendingSlot == nil || res.RequeueAfter != agentScheduleRetry {
		t.Fatalf("pending = %+v, requeue = %s; want the claim held and a retry", s.Status.PendingSlot, res.RequeueAfter)
	}
	f.at(t, hourAt(1, 20))
	s := f.get(t)
	if s.Status.PendingSlot != nil || s.Status.SkipCounts.GatewayUnavailable != 1 || s.Status.SkipCounts.MissedWindow != 0 ||
		!s.Status.NextScheduleTime.UTC().Equal(hourAt(2, 0)) {
		t.Fatalf("status = %+v", s.Status)
	}
	f.gw.err = nil
	f.at(t, hourAt(2, 0))
	if s := f.get(t); s.Status.SkipCounts.MissedWindow != 0 || s.Status.ActiveTaskID == "" ||
		!s.Status.LastScheduleTime.UTC().Equal(hourAt(2, 0)) {
		t.Fatalf("after the gateway came back: %+v; want the 02:00 slot run and nothing counted again", s.Status)
	}
}

// A slot that passes while suspended is counted and not run later.
func TestAgentScheduleSuspendedSlotsAreCountedNotRun(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Spec.Suspend = true })
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 5))
	s := f.get(t)
	if len(f.gw.sends) != 0 || s.Status.SkipCounts.Suspended != 1 || !s.Status.NextScheduleTime.UTC().Equal(hourAt(2, 0)) {
		t.Fatalf("sends = %v, status = %+v", f.gw.sends, s.Status)
	}
	s.Spec.Suspend = false
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(1, 30))
	if len(f.gw.sends) != 0 {
		t.Fatalf("unsuspending ran the stale slot: %v", f.gw.sends)
	}
	f.at(t, hourAt(2, 0))
	if s := f.get(t); len(f.gw.sends) != 1 || !s.Status.LastScheduleTime.UTC().Equal(hourAt(2, 0)) {
		t.Fatalf("sends = %v, status = %+v; want the first slot after unsuspending run", f.gw.sends, s.Status)
	}
}

// An outage past the starting deadline counts one missed window and runs the
// latest slot still inside it.
func TestAgentScheduleAnOutageRunsTheLatestSlotInsideTheDeadline(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(4, 10))
	s := f.get(t)
	if s.Status.SkipCounts.MissedWindow != 1 || len(f.gw.sends) != 1 || !s.Status.LastScheduleTime.UTC().Equal(hourAt(4, 0)) {
		t.Fatalf("sends = %v, status = %+v; want one missed window and the 04:00 slot run", f.gw.sends, s.Status)
	}
}

// An outage that ends past the latest slot's deadline runs nothing.
func TestAgentScheduleAnOutagePastEveryDeadlineRunsNothing(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(4, 30))
	s := f.get(t)
	if s.Status.SkipCounts.MissedWindow != 1 || len(f.gw.sends) != 0 || !s.Status.NextScheduleTime.UTC().Equal(hourAt(5, 0)) {
		t.Fatalf("sends = %v, status = %+v", f.gw.sends, s.Status)
	}
}

// Forbid: a slot that comes due while the last run is live is skipped.
func TestAgentScheduleASlotDuringALiveRunIsSkipped(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.at(t, hourAt(2, 0))
	s := f.get(t)
	if f.gw.distinctRuns() != 1 || s.Status.SkipCounts.AlreadyRunning != 1 || s.Status.ActiveTaskID != "task-1" {
		t.Fatalf("runs = %d, status = %+v", f.gw.distinctRuns(), s.Status)
	}
}

// A run whose task vanished is recorded Lost, and the due slot runs.
func TestAgentScheduleAVanishedRunIsLostAndDoesNotWedge(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.tracker.state = ScheduleTaskState{Found: false}
	f.at(t, hourAt(2, 0))
	s := f.get(t)
	if s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeLost || s.Status.ActiveTaskID != "task-2" {
		t.Fatalf("status = %+v; want the first run Lost and the 02:00 slot running", s.Status)
	}
}

// A run past its active deadline plus grace, with no terminal, is Lost.
func TestAgentScheduleARunPastItsDeadlineIsLost(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 6 * * *" })
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(6, 0))
	f.at(t, hourAt(7, 4))
	if s := f.get(t); s.Status.ActiveTaskID == "" {
		t.Fatal("the run was given up inside its deadline plus grace")
	}
	f.at(t, hourAt(7, 6))
	if s := f.get(t); s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeLost || s.Status.ActiveTaskID != "" {
		t.Fatalf("status = %+v", s.Status)
	}
}

// A terminal is recorded, on the status and in the recent-runs ring.
func TestAgentScheduleRecordsATerminal(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeSucceeded, CompletedAt: hourAt(1, 20)}
	f.at(t, hourAt(1, 21))
	s := f.get(t)
	if s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeSucceeded || s.Status.ActiveTaskID != "" ||
		len(s.Status.RecentRuns) != 1 || s.Status.RecentRuns[0].Outcome != agentv1alpha1.AgentScheduleOutcomeSucceeded {
		t.Fatalf("status = %+v", s.Status)
	}
}

// A terminal's reason is recorded on the run and as the last reason; a reason
// off the CRD's shape is dropped rather than failing the status write; a Lost
// run carries none.
func TestAgentScheduleRecordsARunsReason(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeFailed, CompletedAt: hourAt(1, 20), Reason: "deadline-exceeded"}
	f.at(t, hourAt(1, 21))
	s := f.get(t)
	if s.Status.LastReason != "deadline-exceeded" || len(s.Status.RecentRuns) != 1 || s.Status.RecentRuns[0].Reason != "deadline-exceeded" {
		t.Fatalf("status = %+v; want the run and the last reason deadline-exceeded", s.Status)
	}
	// The next run is Lost: the last reason is cleared, not carried over.
	f.tracker.state = ScheduleTaskState{Found: true}
	f.at(t, hourAt(2, 0))
	f.tracker.state = ScheduleTaskState{Found: false}
	f.at(t, hourAt(3, 0))
	s = f.get(t)
	if s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeLost || s.Status.LastReason != "" {
		t.Fatalf("status = %+v; want a Lost run with no reason", s.Status)
	}
	// A reason off the CRD's shape is dropped (knownReason). The fixture's
	// client doesn't validate the CRD pattern; TestRunReasonShapeIsTheCRDsPattern
	// pins that knownReason is that pattern.
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeFailed, CompletedAt: hourAt(3, 20), Reason: "not a token; with detail"}
	f.at(t, hourAt(3, 21))
	s = f.get(t)
	if s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeFailed || s.Status.LastReason != "" || s.Status.RecentRuns[0].Reason != "" {
		t.Fatalf("status = %+v; want an off-shape reason dropped", s.Status)
	}
}

func TestKnownReason(t *testing.T) {
	for in, want := range map[string]string{
		"write-refused":          "write-refused",
		"error_during_execution": "error_during_execution",
		"":                       "",
		"has space":              "",
		"reason: x":              "",
		strings.Repeat("a", 65):  "",
	} {
		if got := knownReason(in); got != want {
			t.Errorf("knownReason(%q) = %q, want %q", in, got, want)
		}
	}
}

// run-now starts a run with its own key and leaves the cadence alone; a
// second token while that run is live adopts it.
func TestAgentScheduleRunNowStartsOrAdoptsARun(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	s := f.get(t)
	s.Annotations = map[string]string{agentv1alpha1.AgentScheduleRunNowAnnotation: "t1"}
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(0, 50))
	s = f.get(t)
	if len(f.gw.sends) != 1 || f.gw.sends[0] != "run-now/kubeagents-system/compliance-audit/t1" ||
		s.Status.LastHandledRunNow != "t1" || !s.Status.NextScheduleTime.UTC().Equal(hourAt(1, 0)) || s.Status.LastScheduleTime != nil {
		t.Fatalf("sends = %v, status = %+v", f.gw.sends, s.Status)
	}
	s.Annotations[agentv1alpha1.AgentScheduleRunNowAnnotation] = "t2"
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(0, 55))
	if s = f.get(t); len(f.gw.sends) != 1 || s.Status.LastHandledRunNow != "t2" || s.Status.ActiveTaskID != "task-1" {
		t.Fatalf("a run-now during a live run started a second one: sends = %v, status = %+v", f.gw.sends, s.Status)
	}
}

// annotate sets the schedule's stop token.
func (f *scheduleFixture) annotateStop(t *testing.T, token string) {
	t.Helper()
	s := f.get(t)
	if s.Annotations == nil {
		s.Annotations = map[string]string{}
	}
	s.Annotations[agentv1alpha1.AgentScheduleStopAnnotation] = token
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
}

// A new stop token ends the run in flight through the gateway, once (#6052):
// the token is recorded as handled, the same token again stops nothing, and
// the run's canceled terminal lands as Cancelled.
func TestAgentScheduleAStopTokenEndsTheRunInFlightOnce(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	active := f.get(t).Status.ActiveTaskID
	if active == "" {
		t.Fatal("no run in flight")
	}
	f.annotateStop(t, "s1")
	f.at(t, hourAt(1, 2))
	s := f.get(t)
	if len(f.stopper.stops) != 1 || f.stopper.stops[0] != active || s.Status.LastHandledStop != "s1" {
		t.Fatalf("stops = %v, lastHandledStop = %q", f.stopper.stops, s.Status.LastHandledStop)
	}
	f.at(t, hourAt(1, 3))
	if len(f.stopper.stops) != 1 {
		t.Fatalf("a handled token stopped again: %v", f.stopper.stops)
	}
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: scheduleOutcomeFor(a2aStateCanceled), CompletedAt: hourAt(1, 4)}
	f.at(t, hourAt(1, 5))
	s = f.get(t)
	if s.Status.ActiveTaskID != "" || len(s.Status.RecentRuns) != 1 || s.Status.RecentRuns[0].Outcome != agentv1alpha1.AgentScheduleOutcomeCancelled {
		t.Fatalf("after the canceled terminal: %+v", s.Status)
	}
}

// With nothing running, a stop token is handled at once and asks the gateway
// nothing. A claim still in flight isn't dropped (the gateway may already have
// taken it): the token waits until the claim resolves into a run, which the
// next pass stops.
func TestAgentScheduleAStopWaitsForAClaimInFlight(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.annotateStop(t, "s0")
	f.at(t, hourAt(0, 45))
	if s := f.get(t); s.Status.LastHandledStop != "s0" || len(f.stopper.stops) != 0 {
		t.Fatalf("with nothing running: lastHandledStop = %q, stops = %v", s.Status.LastHandledStop, f.stopper.stops)
	}
	f.gw.err = fmt.Errorf("dial: %w", ErrScheduleGatewayUnavailable)
	f.at(t, hourAt(1, 0))
	if f.get(t).Status.PendingSlot == nil {
		t.Fatal("no claim held")
	}
	f.annotateStop(t, "s1")
	f.gw.err = nil
	f.at(t, hourAt(1, 1))
	s := f.get(t)
	if s.Status.ActiveTaskID == "" || s.Status.LastHandledStop != "s0" || len(f.stopper.stops) != 0 {
		t.Fatalf("the claim in flight: status = %+v, stops = %v", s.Status, f.stopper.stops)
	}
	f.at(t, hourAt(1, 2))
	s = f.get(t)
	if s.Status.LastHandledStop != "s1" || len(f.stopper.stops) != 1 || f.stopper.stops[0] != s.Status.ActiveTaskID {
		t.Fatalf("the run the claim became: lastHandledStop = %q, stops = %v", s.Status.LastHandledStop, f.stopper.stops)
	}
}

// A run-now asked for after a stop isn't an adoption of the run being
// stopped: it waits for that run to settle, then starts a new one.
func TestAgentScheduleARunNowAfterAStopStartsANewRun(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	first := f.get(t).Status.ActiveTaskID
	f.annotateStop(t, "s1")
	f.at(t, hourAt(1, 1))
	s := f.get(t)
	s.Annotations[agentv1alpha1.AgentScheduleRunNowAnnotation] = "r1"
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(1, 2))
	if s = f.get(t); s.Status.LastHandledRunNow != "" || len(f.gw.sends) != 1 {
		t.Fatalf("the run-now adopted the stopping run: lastHandledRunNow = %q, sends = %v", s.Status.LastHandledRunNow, f.gw.sends)
	}
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: scheduleOutcomeFor(a2aStateCanceled), CompletedAt: hourAt(1, 3)}
	f.at(t, hourAt(1, 4))
	s = f.get(t)
	if s.Status.LastHandledRunNow != "r1" || len(f.gw.sends) != 2 || s.Status.ActiveTaskID == first || s.Status.StoppedTaskID != "" {
		t.Fatalf("after the stopped run settled: status = %+v, sends = %v", s.Status, f.gw.sends)
	}
}

// A stop the gateway refuses for good is handled, not retried.
func TestAgentScheduleARefusedStopIsNotRetried(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.stopper.err = fmt.Errorf("%w (invalid): the run predates the record of its trigger", errTriggerStopRefused)
	f.annotateStop(t, "s1")
	f.at(t, hourAt(1, 1))
	f.at(t, hourAt(1, 2))
	if s := f.get(t); s.Status.LastHandledStop != "s1" || len(f.stopper.stops) != 1 {
		t.Fatalf("lastHandledStop = %q, stops = %v", s.Status.LastHandledStop, f.stopper.stops)
	}
}

// An API server serving a CRD from before lastHandledStop prunes it. The
// stop isn't sent then: a token that can't be recorded would read as new on
// every pass and stop every later run.
func TestAgentScheduleAStopIsNotSentWhenTheTokenCantBeRecorded(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.r.Client = interceptor.NewClient(f.c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, sub string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if s, ok := obj.(*agentv1alpha1.AgentSchedule); ok {
				s.Status.LastHandledStop, s.Status.StoppedTaskID = "", ""
			}
			return c.SubResource(sub).Update(ctx, obj, opts...)
		},
	})
	f.annotateStop(t, "s1")
	f.at(t, hourAt(1, 1))
	f.at(t, hourAt(1, 2))
	if len(f.stopper.stops) != 0 {
		t.Fatalf("a stop was sent with its token pruned: %v", f.stopper.stops)
	}
}

// A gateway that doesn't answer the stop (an older one, say) leaves the token
// unhandled and comes back soon; the next pass that's answered handles it.
func TestAgentScheduleAnUnansweredStopIsTriedAgain(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.stopper.err = fmt.Errorf("no answer: %w", ErrScheduleGatewayUnavailable)
	f.annotateStop(t, "s1")
	res := f.at(t, hourAt(1, 1))
	if s := f.get(t); s.Status.LastHandledStop != "" || res.RequeueAfter > agentScheduleRetry {
		t.Fatalf("an unanswered stop: lastHandledStop = %q, requeue = %s", s.Status.LastHandledStop, res.RequeueAfter)
	}
	f.stopper.err = nil
	f.at(t, hourAt(1, 2))
	if s := f.get(t); s.Status.LastHandledStop != "s1" || len(f.stopper.stops) != 2 {
		t.Fatalf("the retry: lastHandledStop = %q, stops = %v", s.Status.LastHandledStop, f.stopper.stops)
	}
}

// A refusal under the trigger's cap counts the slot and does not retry it.
func TestAgentScheduleARateLimitedSlotIsCountedNotRetried(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.gw.err = ErrScheduleRateLimited
	f.at(t, hourAt(0, 45))
	res := f.at(t, hourAt(1, 0))
	s := f.get(t)
	if s.Status.SkipCounts.RateLimited != 1 || s.Status.PendingSlot != nil || res.RequeueAfter == agentScheduleRetry {
		t.Fatalf("status = %+v, requeue = %s", s.Status, res.RequeueAfter)
	}
}

// A run that ends with no final delivery yet is asked about once on the pass
// that settles it, not a second time for its delivery on the same pass: that
// answer was just read. The next pass asks for the delivery.
func TestAgentScheduleASettlingRunIsAskedAboutOncePerPass(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeSucceeded, CompletedAt: hourAt(1, 5)}
	f.tracker.calls = 0
	f.at(t, hourAt(1, 6))
	if s := f.get(t); s.Status.ActiveTaskID != "" || f.tracker.calls != 1 {
		t.Fatalf("settling pass: active = %q, tracker calls = %d, want 1", s.Status.ActiveTaskID, f.tracker.calls)
	}
	f.at(t, hourAt(1, 7))
	if f.tracker.calls != 2 {
		t.Fatalf("the next pass: tracker calls = %d, want 2 (its delivery)", f.tracker.calls)
	}
}

// A refusal of the submission itself (here tier_not_allowed) drops the slot
// at once, without retrying it inside the starting deadline or counting it as
// an outage, and says so on the Submitted condition. The next slot tries
// again, and a run the gateway takes sets the condition back to True.
func TestAgentScheduleARefusedSubmissionIsShownNotRetried(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.gw.err = fmt.Errorf("%w (tier_not_allowed): the profile does not allow unattended", errTriggerRefused)
	f.at(t, hourAt(0, 45))
	res := f.at(t, hourAt(1, 0))
	s := f.get(t)
	if len(f.gw.sends) != 1 || s.Status.PendingSlot != nil || res.RequeueAfter == agentScheduleRetry {
		t.Fatalf("sends = %v, status = %+v, requeue = %s", f.gw.sends, s.Status, res.RequeueAfter)
	}
	if s.Status.SkipCounts.GatewayUnavailable != 0 || !s.Status.NextScheduleTime.UTC().Equal(hourAt(2, 0)) {
		t.Fatalf("a refusal was counted as an outage or held the slot: %+v", s.Status)
	}
	c := meta.FindStatusCondition(s.Status.Conditions, agentv1alpha1.AgentScheduleConditionSubmitted)
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != agentScheduleReasonGatewayRefused || !strings.Contains(c.Message, "tier_not_allowed") {
		t.Fatalf("Submitted = %+v", c)
	}
	f.at(t, hourAt(1, 10))
	if len(f.gw.sends) != 1 {
		t.Fatalf("the refused slot was sent again: %v", f.gw.sends)
	}
	f.gw.err = nil
	f.at(t, hourAt(2, 0))
	s = f.get(t)
	c = meta.FindStatusCondition(s.Status.Conditions, agentv1alpha1.AgentScheduleConditionSubmitted)
	if len(f.gw.sends) != 2 || s.Status.ActiveTaskID == "" || c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("the next slot: sends = %v, status = %+v", f.gw.sends, s.Status)
	}
}

// A name the run's trigger can't carry (a dot, or past 63 characters) reads
// Accepted=False and fires nothing, rather than being refused at every slot.
func TestAgentScheduleANameTheTriggerCantCarryIsNotAccepted(t *testing.T) {
	for _, name := range []string{"team.a.nightly", strings.Repeat("a", 64)} {
		f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Name = name })
		f.at(t, hourAt(0, 45))
		f.at(t, hourAt(1, 0))
		if len(f.gw.sends) != 0 {
			t.Fatalf("%s: submitted %v", name, f.gw.sends)
		}
		c := meta.FindStatusCondition(f.get(t).Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted)
		if c == nil || c.Status != metav1.ConditionFalse || c.Reason != agentScheduleReasonInvalidName {
			t.Fatalf("%s: Accepted = %+v", name, c)
		}
	}
}

// Nothing fires for a schedule that doesn't parse, or whose profile is
// missing or doesn't allow the unattended tier.
func TestAgentScheduleNotAcceptedFiresNothing(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.AgentSchedule)
		tiers  []string
		reason string
	}{
		{"bad cron", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "61 * * * *" }, nil, agentScheduleReasonInvalidSchedule},
		{"time zone", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "CRON_TZ=Asia/Tokyo 0 * * * *" }, nil, agentScheduleReasonInvalidSchedule},
		{"never matches", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 0 30 2 *" }, nil, agentScheduleReasonInvalidSchedule},
		{"no profile", func(s *agentv1alpha1.AgentSchedule) { s.Spec.ProfileRef.Name = "missing" }, nil, agentScheduleReasonProfileNotFound},
		{"read only", nil, []string{agentv1alpha1.AgentProfileTierRead}, agentScheduleReasonProfileDisallowed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newScheduleFixture(t, tc.mutate, tc.tiers...)
			f.at(t, hourAt(1, 0))
			f.at(t, hourAt(2, 0))
			s := f.get(t)
			c := meta.FindStatusCondition(s.Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted)
			if len(f.gw.sends) != 0 || c == nil || c.Status != metav1.ConditionFalse || c.Reason != tc.reason {
				t.Fatalf("sends = %v, Accepted = %+v; want none and reason %s", f.gw.sends, c, tc.reason)
			}
		})
	}
}

// A schedule outside an install's namespace never fires.
func TestAgentScheduleWithNoPlatformAgentFiresNothing(t *testing.T) {
	f := newScheduleFixtureWith(t, nil, false)
	f.at(t, hourAt(1, 0))
	f.at(t, hourAt(2, 0))
	c := meta.FindStatusCondition(f.get(t).Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted)
	if len(f.gw.sends) != 0 || c == nil || c.Reason != agentScheduleReasonNoPlatformAgent {
		t.Fatalf("sends = %v, Accepted = %+v", f.gw.sends, c)
	}
}

// A schedule is builtin only when the install's PlatformAgent controls it
// and its origin label reads builtin: a broker kept to origin: user by the
// admission policy can add the controller reference but not the label, and a
// hand-written schedule can set the label but has no controller reference.
// Anything else is a user schedule, and user schedules are deferred to Track 7
// (#5624, decided B on #5635): it reads Accepted=False and never submits.
func TestAgentScheduleOriginNeedsTheControllerAndTheLabel(t *testing.T) {
	for _, tc := range []struct {
		name       string
		controlled bool
		label      string
		rendered   bool
		builtin    bool
		reason     string
	}{
		{"labelled builtin, not controlled", false, agentv1alpha1.AgentScheduleOriginBuiltin, true, false, agentScheduleReasonBuiltinNotFromOperator},
		{"controlled, labelled user", true, agentv1alpha1.AgentScheduleOriginUser, true, false, agentScheduleReasonUserDeferred},
		{"controlled, unlabelled", true, "", true, false, agentScheduleReasonUserDeferred},
		{"controlled and labelled builtin, not a schedule the install renders", true, agentv1alpha1.AgentScheduleOriginBuiltin, false, false, agentScheduleReasonBuiltinNotFromOperator},
		{"controlled by the install, labelled builtin, rendered", true, agentv1alpha1.AgentScheduleOriginBuiltin, true, true, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) {
				if tc.label == "" {
					delete(s.Labels, agentv1alpha1.AgentScheduleOriginLabel)
				} else {
					s.Labels[agentv1alpha1.AgentScheduleOriginLabel] = tc.label
				}
				if !tc.controlled {
					s.OwnerReferences = nil
				}
			})
			if !tc.rendered {
				f.r.BuiltinNames = func(*agentv1alpha1.PlatformAgent) []string { return nil }
			}
			f.at(t, hourAt(0, 45))
			f.at(t, hourAt(1, 0))
			if tc.builtin {
				if len(f.gw.origins) != 1 || f.gw.origins[0] != agentv1alpha1.AgentScheduleOriginBuiltin {
					t.Fatalf("origins = %v, want [builtin]", f.gw.origins)
				}
				return
			}
			if len(f.gw.origins) != 0 {
				t.Fatalf("submitted %v; a user schedule must not fire yet", f.gw.origins)
			}
			got := f.get(t)
			cond := meta.FindStatusCondition(got.Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted)
			if cond == nil || cond.Status != metav1.ConditionFalse || cond.Reason != tc.reason {
				t.Fatalf("Accepted = %+v, want False/%s", cond, tc.reason)
			}
		})
	}
}

// The operator renders no built-in schedules yet, so with the rendered set
// every builtin-labelled schedule reads BuiltinNotFromOperator and fires
// nothing, however it was written.
func TestAgentScheduleNoScheduleIsBuiltinUntilTheOperatorRendersOne(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.r.BuiltinNames = nil
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	cond := meta.FindStatusCondition(f.get(t).Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted)
	if len(f.gw.sends) != 0 || cond == nil || cond.Reason != agentScheduleReasonBuiltinNotFromOperator {
		t.Fatalf("sends = %v, Accepted = %+v", f.gw.sends, cond)
	}
}

// Editing the schedule moves the next slot to the new schedule's.
func TestAgentScheduleASpecEditMovesTheNextSlot(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 0 1 1 *" })
	f.at(t, hourAt(0, 45))
	s := f.get(t)
	s.Spec.Schedule = "*/5 * * * *"
	s.Generation = 2
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(0, 46))
	if got := f.get(t).Status.NextScheduleTime.UTC(); !got.Equal(hourAt(0, 50)) {
		t.Fatalf("next after the edit = %s, want 00:50", got)
	}
}

// An error the reconciler doesn't know holds the claim until the starting
// deadline, then counts the slot and moves on, rather than wedging.
func TestAgentScheduleAnUnknownSubmitErrorDoesNotWedge(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.gw.err = fmt.Errorf("profile tier re-check refused: 403")
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 1))
	if s := f.get(t); s.Status.PendingSlot == nil {
		t.Fatal("the claim was dropped inside the deadline")
	}
	f.at(t, hourAt(1, 20))
	s := f.get(t)
	if s.Status.PendingSlot != nil || s.Status.SkipCounts.GatewayUnavailable != 1 || !s.Status.NextScheduleTime.UTC().Equal(hourAt(2, 0)) {
		t.Fatalf("status = %+v", s.Status)
	}
}

type erringScheduleTracker struct{}

func (erringScheduleTracker) State(context.Context, *agentv1alpha1.AgentSchedule, string) (ScheduleTaskState, error) {
	return ScheduleTaskState{}, fmt.Errorf("bus unavailable")
}

// A tracker that keeps failing still lets the deadline settle the run.
func TestAgentScheduleATrackerErrorStillHonoursTheDeadline(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 6 * * *" })
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(6, 0))
	f.r.Tracker = erringScheduleTracker{}
	f.at(t, hourAt(6, 30))
	if s := f.get(t); s.Status.ActiveTaskID == "" {
		t.Fatal("a tracker error settled the run inside its deadline")
	}
	f.at(t, hourAt(7, 6))
	if s := f.get(t); s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeLost || s.Status.ActiveTaskID != "" {
		t.Fatalf("status = %+v; want Lost past the deadline", s.Status)
	}
}

// An outcome the status can't hold is recorded as Failed, and the status
// write goes through.
func TestAgentScheduleAnUnknownOutcomeIsFailed(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: "Canceled", CompletedAt: hourAt(1, 10)}
	f.at(t, hourAt(1, 11))
	if s := f.get(t); s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeFailed || s.Status.ActiveTaskID != "" {
		t.Fatalf("status = %+v", s.Status)
	}
}

// Unset, the run deadline is the profile's: a profile that allows two hours
// keeps a run alive past one, so Forbid holds.
func TestAgentScheduleTheRunDeadlineDefaultsToTheProfiles(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) {
		s.Spec.Schedule = "0 6 * * *"
		s.Spec.ActiveDeadlineSeconds = nil
	})
	var p agentv1alpha1.AgentProfile
	if err := f.c.Get(context.Background(), types.NamespacedName{Namespace: "kubeagents-system", Name: "fleet-audit"}, &p); err != nil {
		t.Fatal(err)
	}
	p.Spec.Lifecycle.ActiveDeadlineSeconds = 7200
	if err := f.c.Update(context.Background(), &p); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(6, 0))
	f.at(t, hourAt(7, 10))
	if s := f.get(t); s.Status.ActiveTaskID == "" {
		t.Fatal("the run was Lost at 70 minutes under a profile that allows two hours")
	}
	f.at(t, hourAt(8, 6))
	if s := f.get(t); s.Status.LastOutcome != agentv1alpha1.AgentScheduleOutcomeLost {
		t.Fatalf("status = %+v; want Lost past the profile's deadline plus grace", s.Status)
	}
}

// The recent-runs ring keeps the newest ten.
func TestAgentScheduleTheRecentRunsRingKeepsTen(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "*/5 * * * *" })
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeSucceeded}
	f.at(t, hourAt(0, 31))
	for m := 35; m <= 59+60; m += 5 {
		f.at(t, hourAt(0, 0).Add(time.Duration(m)*time.Minute))
		f.at(t, hourAt(0, 0).Add(time.Duration(m)*time.Minute+time.Minute))
	}
	s := f.get(t)
	if len(s.Status.RecentRuns) != agentScheduleRecentRuns || s.Status.RecentRuns[0].TaskID != s.Status.LastTaskID {
		t.Fatalf("ring = %d runs, newest %q, last task %q", len(s.Status.RecentRuns), s.Status.RecentRuns[0].TaskID, s.Status.LastTaskID)
	}
}

// The CRD's schema against a real API server: defaults land, and the CEL
// rule and enums refuse what the reconciler would otherwise have to.
func TestAgentScheduleSchemaEnvtest(t *testing.T) {
	cl, _ := startEnvtest(t)
	ctx := context.Background()
	ns := &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "kubeagents-system"}}
	if err := cl.Create(ctx, ns); err != nil {
		t.Fatal(err)
	}
	mk := func(name string, mutate func(*agentv1alpha1.AgentSchedule)) *agentv1alpha1.AgentSchedule {
		s := &agentv1alpha1.AgentSchedule{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
			Spec: agentv1alpha1.AgentScheduleSpec{
				Schedule:   "20 6 * * *",
				ProfileRef: agentv1alpha1.AgentScheduleProfileRef{Name: "fleet-audit"},
				Prompt:     "Run the compliance audit.",
			},
		}
		if mutate != nil {
			mutate(s)
		}
		return s
	}
	ok := mk("valid", nil)
	if err := cl.Create(ctx, ok); err != nil {
		t.Fatalf("a valid schedule was refused: %v", err)
	}
	if ok.Spec.ConcurrencyPolicy != agentv1alpha1.AgentScheduleConcurrencyForbid || ok.Spec.StartingDeadlineSeconds == nil ||
		*ok.Spec.StartingDeadlineSeconds != 900 || ok.Spec.Delivery.Mode != agentv1alpha1.AgentScheduleDeliveryChat ||
		ok.Spec.ActiveDeadlineSeconds != nil {
		t.Fatalf("defaults = %+v; want Forbid, 900, chat, and no active deadline (the profile's applies)", ok.Spec)
	}
	// A manifest that omits delivery, as kubectl sends it (the Go client
	// always sends delivery: {}), still defaults to the home channel.
	raw := &unstructured.Unstructured{Object: map[string]any{
		"apiVersion": agentv1alpha1.GroupVersion.String(), "kind": "AgentSchedule",
		"metadata": map[string]any{"name": "no-delivery", "namespace": "kubeagents-system"},
		"spec":     map[string]any{"schedule": "0 * * * *", "profileRef": map[string]any{"name": "fleet-audit"}, "prompt": "no-op"},
	}}
	if err := cl.Create(ctx, raw); err != nil {
		t.Fatalf("a schedule without delivery was refused: %v", err)
	}
	var noDelivery agentv1alpha1.AgentSchedule
	if err := cl.Get(ctx, types.NamespacedName{Namespace: "kubeagents-system", Name: "no-delivery"}, &noDelivery); err != nil {
		t.Fatal(err)
	}
	if noDelivery.Spec.Delivery.Mode != agentv1alpha1.AgentScheduleDeliveryChat {
		t.Errorf("delivery.mode with delivery omitted = %q, want chat", noDelivery.Spec.Delivery.Mode)
	}
	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.AgentSchedule)
	}{
		{"four-fields", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 * * *" }},
		{"time-zone", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "CRON_TZ=UTC 0 * * * *" }},
		{"seconds", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 0 * * * *" }},
		{"allow", func(s *agentv1alpha1.AgentSchedule) { s.Spec.ConcurrencyPolicy = "Allow" }},
		{"delivery", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Delivery.Mode = "slack" }},
		{"no-prompt", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Prompt = "" }},
		{"short-deadline", func(s *agentv1alpha1.AgentSchedule) { s.Spec.StartingDeadlineSeconds = ptr.To[int64](5) }},
	} {
		if err := cl.Create(ctx, mk(tc.name, tc.mutate)); err == nil || !apierrors.IsInvalid(err) {
			t.Errorf("%s: create = %v, want refused as invalid", tc.name, err)
		}
	}
}

// A schedule edited while not accepted takes its new cadence once accepted;
// an edit to anything but the schedule keeps a due slot.
func TestAgentScheduleTheResetFollowsTheExpressionNotTheGeneration(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) { s.Spec.Schedule = "0 0 1 1 *" })
	f.at(t, hourAt(0, 45))
	var p agentv1alpha1.AgentProfile
	key := types.NamespacedName{Namespace: "kubeagents-system", Name: "fleet-audit"}
	if err := f.c.Get(context.Background(), key, &p); err != nil {
		t.Fatal(err)
	}
	p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead}
	if err := f.c.Update(context.Background(), &p); err != nil {
		t.Fatal(err)
	}
	s := f.get(t)
	s.Spec.Schedule, s.Generation = "*/5 * * * *", 2
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(0, 46)) // not accepted: the profile disallows unattended
	if err := f.c.Get(context.Background(), key, &p); err != nil {
		t.Fatal(err)
	}
	p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	if err := f.c.Update(context.Background(), &p); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(0, 47))
	if got := f.get(t).Status.NextScheduleTime.UTC(); !got.Equal(hourAt(0, 50)) {
		t.Fatalf("next after an edit made while not accepted = %s, want 00:50", got)
	}

	g := newScheduleFixture(t, nil)
	g.at(t, hourAt(0, 45))
	gs := g.get(t)
	gs.Spec.Prompt, gs.Generation = "A different prompt.", 2
	if err := g.c.Update(context.Background(), gs); err != nil {
		t.Fatal(err)
	}
	g.at(t, hourAt(1, 5))
	if st := g.get(t).Status; len(g.gw.sends) != 1 || !st.LastScheduleTime.UTC().Equal(hourAt(1, 0)) {
		t.Fatalf("a prompt edit dropped the due 01:00 slot: sends = %v, status = %+v", g.gw.sends, st)
	}
}

// A pass that changes nothing writes nothing.
func TestAgentScheduleAnUnchangedPassWritesNoStatus(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	before := f.get(t).ResourceVersion
	f.at(t, hourAt(0, 46))
	if after := f.get(t).ResourceVersion; after != before {
		t.Fatalf("an idle pass wrote status: resourceVersion %s -> %s", before, after)
	}
}

// How a run's report was delivered (Track 3): a final value on the terminal
// is recorded at once; posting is asked after on later passes, inside the
// window, without holding the next slot; post_failed is counted once; an
// unknown spelling and anything past the window leave the run as it was.
func TestAgentScheduleRecordsHowARunsReportWasDelivered(t *testing.T) {
	ended := func(delivery string) ScheduleTaskState {
		return ScheduleTaskState{Found: true, Terminal: true, Outcome: agentv1alpha1.AgentScheduleOutcomeSucceeded, CompletedAt: hourAt(1, 20), Delivery: delivery}
	}
	t.Run("final on the terminal", func(t *testing.T) {
		f := newScheduleFixture(t, nil)
		f.at(t, hourAt(0, 45))
		f.at(t, hourAt(1, 0))
		f.tracker.state = ended(agentv1alpha1.AgentScheduleReportPosted)
		f.at(t, hourAt(1, 21))
		if s := f.get(t); s.Status.RecentRuns[0].Delivery != agentv1alpha1.AgentScheduleReportPosted || s.Status.PostFailed != 0 {
			t.Fatalf("status = %+v", s.Status)
		}
	})
	t.Run("posting, then post_failed", func(t *testing.T) {
		f := newScheduleFixture(t, nil)
		f.at(t, hourAt(0, 45))
		f.at(t, hourAt(1, 0))
		f.tracker.state = ended(agentv1alpha1.AgentScheduleReportPosting)
		res := f.at(t, hourAt(1, 21))
		s := f.get(t)
		if s.Status.ActiveTaskID != "" || s.Status.RecentRuns[0].Delivery != "" {
			t.Fatalf("posting: the run is settled with no delivery yet; status = %+v", s.Status)
		}
		if res.RequeueAfter > agentScheduleActivePoll {
			t.Errorf("requeue %v while a delivery is awaited, want at most %v", res.RequeueAfter, agentScheduleActivePoll)
		}
		f.tracker.state = ended(agentv1alpha1.AgentScheduleReportPostFailed)
		f.at(t, hourAt(1, 23))
		f.at(t, hourAt(1, 24))
		if s := f.get(t); s.Status.RecentRuns[0].Delivery != agentv1alpha1.AgentScheduleReportPostFailed || s.Status.PostFailed != 1 {
			t.Fatalf("post_failed: want it recorded and counted once; status = %+v", s.Status)
		}
	})
	t.Run("post_unknown counts apart", func(t *testing.T) {
		f := newScheduleFixture(t, nil)
		f.at(t, hourAt(0, 45))
		f.at(t, hourAt(1, 0))
		f.tracker.state = ended(agentv1alpha1.AgentScheduleReportPostUnknown)
		f.at(t, hourAt(1, 21))
		f.at(t, hourAt(1, 22))
		if s := f.get(t); s.Status.RecentRuns[0].Delivery != agentv1alpha1.AgentScheduleReportPostUnknown || s.Status.PostUnknown != 1 || s.Status.PostFailed != 0 {
			t.Fatalf("status = %+v", s.Status)
		}
	})
	t.Run("unknown, then past the window", func(t *testing.T) {
		f := newScheduleFixture(t, nil)
		f.at(t, hourAt(0, 45))
		f.at(t, hourAt(1, 0))
		f.tracker.state = ended("bounced")
		f.at(t, hourAt(1, 21))
		if s := f.get(t); s.Status.RecentRuns[0].Delivery != "" {
			t.Fatalf("an unknown spelling was recorded: %+v", s.Status.RecentRuns[0])
		}
		f.tracker.state = ended(agentv1alpha1.AgentScheduleReportPosted)
		f.at(t, hourAt(1, 20).Add(agentScheduleDeliveryWindow+time.Minute))
		if s := f.get(t); s.Status.RecentRuns[0].Delivery != "" {
			t.Fatalf("a delivery was asked after past the window: %+v", s.Status.RecentRuns[0])
		}
	})
}

// A stop whose answer never made it into status (the stop failed and the
// pass's last status write failed too, or the operator died meanwhile) is
// sent again on the next pass: the token is recorded only once the gateway
// has answered, never before.
func TestAgentScheduleAStopLostBeforeItsAnswerIsSentAgain(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	inner := f.r.Client
	writes := 0
	f.r.Client = interceptor.NewClient(f.c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, sub string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			writes++
			if writes > 1 {
				return errors.New("the API server is unavailable")
			}
			return c.SubResource(sub).Update(ctx, obj, opts...)
		},
	})
	f.stopper.err = fmt.Errorf("no answer: %w", ErrScheduleGatewayUnavailable)
	f.annotateStop(t, "s1")
	f.clock = hourAt(1, 1)
	_, _ = f.r.Reconcile(context.Background(), ctrl.Request{NamespacedName: f.key})
	if s := f.get(t); s.Status.LastHandledStop == "s1" {
		t.Fatal("the token was persisted as handled before the gateway answered")
	}
	f.r.Client = inner
	f.stopper.err = nil
	f.at(t, hourAt(1, 2))
	if s := f.get(t); s.Status.LastHandledStop != "s1" || len(f.stopper.stops) != 2 {
		t.Fatalf("the lost stop: lastHandledStop = %q, stops = %v", s.Status.LastHandledStop, f.stopper.stops)
	}
}

// While a stop is retried after a transient failure, the run being stopped
// stays recorded (as a claimed slot does): a run-now set meanwhile isn't an
// adoption of it and starts a new run once it has ended, and a retry pass that
// changes nothing writes nothing.
func TestAgentScheduleARetriedStopKeepsTheRunItStops(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	active := f.get(t).Status.ActiveTaskID
	f.stopper.err = fmt.Errorf("no answer: %w", ErrScheduleGatewayUnavailable)
	s := f.get(t)
	s.Annotations = map[string]string{
		agentv1alpha1.AgentScheduleStopAnnotation:   "s1",
		agentv1alpha1.AgentScheduleRunNowAnnotation: "r1",
	}
	if err := f.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	f.at(t, hourAt(1, 1))
	s = f.get(t)
	if s.Status.StoppedTaskID != active || s.Status.LastHandledRunNow != "" || s.Status.LastHandledStop != "" {
		t.Fatalf("after a failed stop: stoppedTaskID = %q, lastHandledRunNow = %q, lastHandledStop = %q",
			s.Status.StoppedTaskID, s.Status.LastHandledRunNow, s.Status.LastHandledStop)
	}
	inner := f.r.Client
	writes := 0
	f.r.Client = interceptor.NewClient(f.c.(client.WithWatch), interceptor.Funcs{
		SubResourceUpdate: func(ctx context.Context, c client.Client, sub string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			writes++
			return c.SubResource(sub).Update(ctx, obj, opts...)
		},
	})
	f.at(t, hourAt(1, 2))
	if writes != 0 {
		t.Fatalf("a retry pass that changed nothing wrote status %d times", writes)
	}
	f.r.Client = inner
	f.stopper.err = nil
	f.at(t, hourAt(1, 3))
	f.tracker.state = ScheduleTaskState{Found: true, Terminal: true, Outcome: scheduleOutcomeFor(a2aStateCanceled), CompletedAt: hourAt(1, 4)}
	f.at(t, hourAt(1, 5))
	s = f.get(t)
	if s.Status.LastHandledStop != "s1" || s.Status.LastHandledRunNow != "r1" || s.Status.ActiveTaskID == active || len(f.gw.sends) != 2 {
		t.Fatalf("after the stop and the run's end: status = %+v, sends = %v", s.Status, f.gw.sends)
	}
}

// A stop the gateway refuses for good clears the run being stopped, so a
// run-now afterwards adopts the run still going, as it would with no stop.
func TestAgentScheduleARefusedStopClearsTheRunItStops(t *testing.T) {
	f := newScheduleFixture(t, nil)
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	f.stopper.err = fmt.Errorf("%w (invalid): the run predates the record of its trigger", errTriggerStopRefused)
	f.annotateStop(t, "s1")
	f.at(t, hourAt(1, 1))
	if s := f.get(t); s.Status.StoppedTaskID != "" || s.Status.LastHandledStop != "s1" {
		t.Fatalf("after a refused stop: stoppedTaskID = %q, lastHandledStop = %q", s.Status.StoppedTaskID, s.Status.LastHandledStop)
	}
}

// A controller reference of another kind that carries the PlatformAgent's uid
// doesn't make a schedule built-in: metav1.IsControlledBy matches uids alone,
// so the reconciler checks the kind too.
func TestAgentScheduleAControllerOfAnotherKindIsNotThePlatformAgent(t *testing.T) {
	f := newScheduleFixture(t, func(s *agentv1alpha1.AgentSchedule) {
		s.OwnerReferences = []metav1.OwnerReference{{
			APIVersion: "v1", Kind: "ConfigMap", Name: "not-an-agent", UID: "agent-uid", Controller: ptr.To(true),
		}}
	})
	f.at(t, hourAt(0, 45))
	f.at(t, hourAt(1, 0))
	cond := meta.FindStatusCondition(f.get(t).Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted)
	if len(f.gw.sends) != 0 || cond == nil || cond.Reason != agentScheduleReasonBuiltinNotFromOperator {
		t.Fatalf("sends = %v, Accepted = %+v", f.gw.sends, cond)
	}
}

// TestRunReasonShapeIsTheCRDsPattern: the reconciler drops what the CRD would
// refuse, so its shape and the generated CRD's pattern for both reason
// fields are the same expression.
func TestRunReasonShapeIsTheCRDsPattern(t *testing.T) {
	crd, err := os.ReadFile(filepath.Join("..", "..", "config", "crd", "bases", "kubeagents.x-k8s.io_agentschedules.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	want := "pattern: " + runReasonShape.String()
	if n := strings.Count(string(crd), want); n < 2 {
		t.Fatalf("the CRD has %d %q lines, want one each for reason and lastReason", n, want)
	}
}
