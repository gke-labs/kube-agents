package controller

import (
	"context"
	"fmt"
	"sync"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// fakeScheduleGateway is the gateway as the reconciler sees it: the same
// idempotency key returns the same task, as the gateway's KV record does.
type fakeScheduleGateway struct {
	mu    sync.Mutex
	err   error
	tasks map[string]string
	sends []string
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

type fakeScheduleTracker struct{ state ScheduleTaskState }

func (t *fakeScheduleTracker) State(context.Context, *agentv1alpha1.AgentSchedule, string) (ScheduleTaskState, error) {
	return t.state, nil
}

type scheduleFixture struct {
	r       *AgentScheduleReconciler
	c       client.Client
	gw      *fakeScheduleGateway
	tracker *fakeScheduleTracker
	clock   time.Time
	key     types.NamespacedName
}

// created is every fixture schedule's creation time: an hourly schedule's
// first slot is 01:00.
var scheduleCreated = time.Date(2026, 11, 11, 0, 30, 0, 0, time.UTC)

func newScheduleFixture(t *testing.T, mutate func(*agentv1alpha1.AgentSchedule), profileTiers ...string) *scheduleFixture {
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
	c := fake.NewClientBuilder().WithScheme(setupScheme()).
		WithObjects(&profile, sched).WithStatusSubresource(&agentv1alpha1.AgentSchedule{}).Build()
	f := &scheduleFixture{c: c, gw: &fakeScheduleGateway{}, tracker: &fakeScheduleTracker{state: ScheduleTaskState{Found: true}},
		key: client.ObjectKeyFromObject(sched)}
	f.r = &AgentScheduleReconciler{Client: c, Scheme: c.Scheme(), Submitter: f.gw, Tracker: f.tracker, Now: func() time.Time { return f.clock }}
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
	f.at(t, hourAt(1, 30))
	if s := f.get(t); s.Status.SkipCounts.MissedWindow != 0 || s.Status.ActiveTaskID != "" {
		t.Fatalf("the abandoned slot was counted again or run late: %+v", s.Status)
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
