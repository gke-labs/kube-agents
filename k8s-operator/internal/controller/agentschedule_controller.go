/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"errors"
	"fmt"
	"os"
	"slices"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/robfig/cron/v3"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// AgentSchedulesEnvVar turns the AgentSchedule reconciler on. Off by default
// until the profile dispatcher runs by default and the trigger ingress lands
// (gke-labs/kube-agents#5617): with it off, an AgentSchedule is stored and
// nothing fires.
const AgentSchedulesEnvVar = "A2A_AGENT_SCHEDULES"

// AgentSchedulesEnabled reports whether the operator runs schedules.
func AgentSchedulesEnabled() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(AgentSchedulesEnvVar)), "true")
}

// The Accepted condition's reasons.
const (
	agentScheduleReasonValid             = "ScheduleValid"
	agentScheduleReasonInvalidSchedule   = "InvalidSchedule"
	agentScheduleReasonProfileNotFound   = "ProfileNotFound"
	agentScheduleReasonProfileDisallowed = "ProfileTierDisallowed"
)

const (
	// agentScheduleDefaultStartingDeadline and the active deadline are the
	// CRD defaults, used when a stored object predates the defaulting.
	agentScheduleDefaultStartingDeadline = 900 * time.Second
	agentScheduleDefaultActiveDeadline   = 3600 * time.Second
	// agentScheduleLostGrace is how long past its active deadline a run may
	// go without a terminal before it is recorded Lost (design section 2).
	agentScheduleLostGrace = 5 * time.Minute
	// agentScheduleActivePoll is how often a run in flight is checked for
	// its terminal while no event says it ended.
	agentScheduleActivePoll = 30 * time.Second
	// agentScheduleRetry is the back-off for a slot the gateway did not take.
	agentScheduleRetry = 15 * time.Second
	// agentScheduleRecentRuns bounds status.recentRuns.
	agentScheduleRecentRuns = 10
	// agentScheduleMaxSlotWalk bounds the walk to the latest missed slot.
	agentScheduleMaxSlotWalk = 100000
)

// ErrScheduleGatewayUnavailable is a submission the gateway did not answer:
// the slot stays claimed and is retried with the same idempotency key.
var ErrScheduleGatewayUnavailable = errors.New("the gateway did not accept the submission")

// ErrScheduleRateLimited is the gateway refusing a run under the trigger's
// cap: the slot is counted and dropped, never retried.
var ErrScheduleRateLimited = errors.New("the gateway refused the submission under the trigger's rate cap")

// ScheduleSubmission is one run, as the reconciler hands it to the gateway.
type ScheduleSubmission struct {
	IdempotencyKey string
	TriggerName    string
	Origin         string
	SlotTime       time.Time
	Schedule       *agentv1alpha1.AgentSchedule
}

// ScheduleSubmitter submits a run and returns its task id. The same
// idempotency key returns the same task id, so a re-send after a crash never
// starts a second run. It returns ErrScheduleGatewayUnavailable or
// ErrScheduleRateLimited for the two outcomes the reconciler treats apart.
type ScheduleSubmitter interface {
	Submit(ctx context.Context, sub ScheduleSubmission) (string, error)
}

// ScheduleTaskState is what the bus says about a run in flight.
type ScheduleTaskState struct {
	// Found is false when no record of the task remains.
	Found bool
	// Terminal is true once the task ended.
	Terminal bool
	// Outcome is the run's outcome when Terminal (an AgentScheduleOutcome).
	Outcome string
	// CompletedAt is when it ended.
	CompletedAt time.Time
}

// ScheduleTaskTracker reports on a run in flight.
type ScheduleTaskTracker interface {
	State(ctx context.Context, sched *agentv1alpha1.AgentSchedule, taskID string) (ScheduleTaskState, error)
}

var (
	agentScheduleRunsTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "kubeagents_schedule_runs_total",
		Help: "Scheduled runs by outcome (submitted, succeeded, failed, cancelled, lost).",
	}, []string{"schedule", "outcome"})
	agentScheduleSkipsTotal = prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "kubeagents_schedule_skips_total",
		Help: "Scheduled slots that did not run, by reason.",
	}, []string{"schedule", "reason"})
)

func init() {
	metrics.Registry.MustRegister(agentScheduleRunsTotal, agentScheduleSkipsTotal)
}

// AgentScheduleReconciler fires AgentSchedules: when a slot comes due it
// claims it in status, submits an Unattended task through the gateway, and
// records the run (docs/designs/scheduling-on-profiles.md, section 2). It runs
// only on the elected operator leader, as every controller does.
type AgentScheduleReconciler struct {
	client.Client
	Scheme    *runtime.Scheme
	Submitter ScheduleSubmitter
	Tracker   ScheduleTaskTracker
	// Now is the clock; nil means time.Now.
	Now func() time.Time
}

func (r *AgentScheduleReconciler) now() time.Time {
	if r.Now != nil {
		return r.Now().UTC()
	}
	return time.Now().UTC()
}

// parseAgentSchedule reads a schedule as the CronJob controller does: five
// standard fields, no seconds, in UTC.
func parseAgentSchedule(expr string) (cron.Schedule, error) {
	if strings.Contains(expr, "TZ=") {
		return nil, fmt.Errorf("a schedule is read in UTC and takes no time zone prefix")
	}
	return cron.ParseStandard(expr)
}

func durationOr(seconds *int64, def time.Duration) time.Duration {
	if seconds == nil || *seconds <= 0 {
		return def
	}
	return time.Duration(*seconds) * time.Second
}

// AgentScheduleTriggerName is a schedule's trigger name, as the gateway
// verifies it and the run's grant carries it.
func AgentScheduleTriggerName(sched *agentv1alpha1.AgentSchedule) string {
	return "schedule/" + sched.Name
}

func agentScheduleOrigin(sched *agentv1alpha1.AgentSchedule) string {
	if sched.Labels[agentv1alpha1.AgentScheduleOriginLabel] == agentv1alpha1.AgentScheduleOriginBuiltin {
		return agentv1alpha1.AgentScheduleOriginBuiltin
	}
	return agentv1alpha1.AgentScheduleOriginUser
}

func slotKey(sched *agentv1alpha1.AgentSchedule, slot time.Time) string {
	return fmt.Sprintf("schedule/%s/%s/%d", sched.Namespace, sched.Name, slot.Unix())
}

func runNowKey(sched *agentv1alpha1.AgentSchedule, token string) string {
	return fmt.Sprintf("run-now/%s/%s/%s", sched.Namespace, sched.Name, token)
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentschedules,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentschedules/status,verbs=get;update;patch

func (r *AgentScheduleReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var sched agentv1alpha1.AgentSchedule
	if err := r.Get(ctx, req.NamespacedName, &sched); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	cronSched, accepted, err := r.accept(ctx, &sched)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !accepted {
		// Nothing fires; a profile or spec change requeues it.
		return ctrl.Result{}, r.writeStatus(ctx, &sched)
	}
	return r.fire(ctx, &sched, cronSched)
}

// accept sets the Accepted condition: the schedule parses, and its profile
// exists and allows the unattended tier. The gateway checks the tier again at
// every submission, since a profile can change after this ran.
func (r *AgentScheduleReconciler) accept(ctx context.Context, sched *agentv1alpha1.AgentSchedule) (cron.Schedule, bool, error) {
	cond := metav1.Condition{Type: agentv1alpha1.AgentScheduleConditionAccepted, ObservedGeneration: sched.Generation}
	sched.Status.ObservedGeneration = sched.Generation
	cronSched, perr := parseAgentSchedule(sched.Spec.Schedule)
	if perr != nil {
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonInvalidSchedule
		cond.Message = fmt.Sprintf("schedule %q does not parse: %v", sched.Spec.Schedule, perr)
		meta.SetStatusCondition(&sched.Status.Conditions, cond)
		return nil, false, nil
	}
	var profile agentv1alpha1.AgentProfile
	err := r.Get(ctx, types.NamespacedName{Namespace: sched.Namespace, Name: sched.Spec.ProfileRef.Name}, &profile)
	switch {
	case apierrors.IsNotFound(err):
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonProfileNotFound
		cond.Message = fmt.Sprintf("AgentProfile %q does not exist in this namespace", sched.Spec.ProfileRef.Name)
	case err != nil:
		return nil, false, err
	case !slices.Contains(profile.AllowedTierList(), agentv1alpha1.AgentProfileTierUnattended):
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonProfileDisallowed
		cond.Message = fmt.Sprintf("AgentProfile %q does not allow the unattended tier", profile.Name)
	default:
		cond.Status, cond.Reason = metav1.ConditionTrue, agentScheduleReasonValid
		cond.Message = "the schedule parses and its profile allows the unattended tier"
	}
	meta.SetStatusCondition(&sched.Status.Conditions, cond)
	return cronSched, cond.Status == metav1.ConditionTrue, nil
}

// fire is the slot logic. Each claim is written to status before its
// submission, so a restart at any point re-sends the same idempotency key.
func (r *AgentScheduleReconciler) fire(ctx context.Context, sched *agentv1alpha1.AgentSchedule, cronSched cron.Schedule) (ctrl.Result, error) {
	st := &sched.Status
	now := r.now()
	startingDeadline := durationOr(sched.Spec.StartingDeadlineSeconds, agentScheduleDefaultStartingDeadline)

	// 1. A run in flight: did it end, or vanish, or outlive its deadline?
	if st.ActiveTaskID != "" {
		if err := r.trackActive(ctx, sched, now); err != nil {
			return ctrl.Result{}, err
		}
	}

	// 2. A claimed slot the gateway has not taken yet (a retry, or a
	// restart between the claim and the status write after it).
	if st.PendingSlot != nil {
		retry, err := r.submitPending(ctx, sched, cronSched, now, startingDeadline)
		if err != nil {
			return ctrl.Result{}, err
		}
		if retry {
			return ctrl.Result{RequeueAfter: agentScheduleRetry}, r.writeStatus(ctx, sched)
		}
	}

	// 3. Run now: a token not handled yet starts a run, or adopts the one
	// in flight. It leaves the cadence alone.
	if token := sched.Annotations[agentv1alpha1.AgentScheduleRunNowAnnotation]; token != "" && token != st.LastHandledRunNow && st.PendingSlot == nil {
		if st.ActiveTaskID != "" {
			st.LastHandledRunNow = token
		} else {
			st.PendingSlot = &agentv1alpha1.AgentSchedulePendingSlot{
				SlotTime: metav1.NewTime(now), IdempotencyKey: runNowKey(sched, token), RunNow: token, ClaimedAt: metav1.NewTime(now),
			}
			if err := r.writeStatus(ctx, sched); err != nil {
				return ctrl.Result{}, err
			}
			retry, err := r.submitPending(ctx, sched, cronSched, now, startingDeadline)
			if err != nil {
				return ctrl.Result{}, err
			}
			if retry {
				return ctrl.Result{RequeueAfter: agentScheduleRetry}, r.writeStatus(ctx, sched)
			}
		}
	}

	// 4. The cadence.
	if st.NextScheduleTime == nil {
		from := sched.CreationTimestamp.UTC()
		if st.LastScheduleTime != nil && st.LastScheduleTime.After(from) {
			from = st.LastScheduleTime.UTC()
		}
		st.NextScheduleTime = ptrTime(cronSched.Next(from))
	}
	if next := st.NextScheduleTime.UTC(); !now.Before(next) {
		switch {
		case sched.Spec.Suspend:
			r.skip(sched, "suspended", &st.SkipCounts.Suspended)
			st.NextScheduleTime = ptrTime(cronSched.Next(now))
		case st.ActiveTaskID != "" || st.PendingSlot != nil:
			r.skip(sched, "already_running", &st.SkipCounts.AlreadyRunning)
			st.NextScheduleTime = ptrTime(cronSched.Next(now))
		default:
			slot := next
			if now.Sub(next) > startingDeadline {
				// Missed while the operator or the gateway was down: one
				// count, and the latest slot still inside the deadline
				// runs, if there is one.
				r.skip(sched, "missed_window", &st.SkipCounts.MissedWindow)
				latest := latestSlot(cronSched, next, now)
				if now.Sub(latest) > startingDeadline {
					st.NextScheduleTime = ptrTime(cronSched.Next(now))
					break
				}
				slot = latest
			}
			st.PendingSlot = &agentv1alpha1.AgentSchedulePendingSlot{
				SlotTime: metav1.NewTime(slot), IdempotencyKey: slotKey(sched, slot), ClaimedAt: metav1.NewTime(now),
			}
			if err := r.writeStatus(ctx, sched); err != nil {
				return ctrl.Result{}, err
			}
			retry, err := r.submitPending(ctx, sched, cronSched, now, startingDeadline)
			if err != nil {
				return ctrl.Result{}, err
			}
			if retry {
				return ctrl.Result{RequeueAfter: agentScheduleRetry}, r.writeStatus(ctx, sched)
			}
		}
	}

	if err := r.writeStatus(ctx, sched); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: r.requeueAfter(sched, now)}, nil
}

// trackActive settles a run in flight that ended, vanished or outlived its
// deadline plus grace, so a lost task never wedges the schedule.
func (r *AgentScheduleReconciler) trackActive(ctx context.Context, sched *agentv1alpha1.AgentSchedule, now time.Time) error {
	st := &sched.Status
	state, err := r.Tracker.State(ctx, sched, st.ActiveTaskID)
	if err != nil {
		return err
	}
	switch {
	case state.Found && state.Terminal:
		r.settle(sched, state.Outcome, state.CompletedAt)
	case !state.Found:
		r.settle(sched, agentv1alpha1.AgentScheduleOutcomeLost, now)
	case st.ActiveTaskStartedAt != nil &&
		now.Sub(st.ActiveTaskStartedAt.UTC()) > durationOr(sched.Spec.ActiveDeadlineSeconds, agentScheduleDefaultActiveDeadline)+agentScheduleLostGrace:
		r.settle(sched, agentv1alpha1.AgentScheduleOutcomeLost, now)
	}
	return nil
}

// settle records a run's end and clears it from flight.
func (r *AgentScheduleReconciler) settle(sched *agentv1alpha1.AgentSchedule, outcome string, at time.Time) {
	st := &sched.Status
	if at.IsZero() {
		at = r.now()
	}
	done := metav1.NewTime(at)
	st.LastCompletionTime = &done
	st.LastOutcome = outcome
	for i := range st.RecentRuns {
		if st.RecentRuns[i].TaskID == st.ActiveTaskID {
			st.RecentRuns[i].CompletionTime = &done
			st.RecentRuns[i].Outcome = outcome
		}
	}
	agentScheduleRunsTotal.WithLabelValues(sched.Name, strings.ToLower(outcome)).Inc()
	st.ActiveTaskID = ""
	st.ActiveTaskStartedAt = nil
}

// submitPending sends the claimed slot. It reports retry when the gateway
// did not take it and the deadline still allows another try.
func (r *AgentScheduleReconciler) submitPending(ctx context.Context, sched *agentv1alpha1.AgentSchedule, cronSched cron.Schedule,
	now time.Time, startingDeadline time.Duration) (bool, error) {
	st := &sched.Status
	p := st.PendingSlot
	taskID, err := r.Submitter.Submit(ctx, ScheduleSubmission{
		IdempotencyKey: p.IdempotencyKey,
		TriggerName:    AgentScheduleTriggerName(sched),
		Origin:         agentScheduleOrigin(sched),
		SlotTime:       p.SlotTime.UTC(),
		Schedule:       sched,
	})
	switch {
	case err == nil:
		started := metav1.NewTime(now)
		st.ActiveTaskID, st.ActiveTaskStartedAt, st.LastTaskID = taskID, &started, taskID
		st.RecentRuns = append([]agentv1alpha1.AgentScheduleRun{{TaskID: taskID, SlotTime: p.SlotTime}}, st.RecentRuns...)
		if len(st.RecentRuns) > agentScheduleRecentRuns {
			st.RecentRuns = st.RecentRuns[:agentScheduleRecentRuns]
		}
		if p.RunNow != "" {
			st.LastHandledRunNow = p.RunNow
		} else {
			slot := p.SlotTime
			st.LastScheduleTime = &slot
			st.NextScheduleTime = ptrTime(cronSched.Next(now))
		}
		agentScheduleRunsTotal.WithLabelValues(sched.Name, "submitted").Inc()
		st.PendingSlot = nil
		return false, nil
	case errors.Is(err, ErrScheduleRateLimited):
		r.skip(sched, "rate_limited", &st.SkipCounts.RateLimited)
		r.dropPending(sched, cronSched, now)
		return false, nil
	case errors.Is(err, ErrScheduleGatewayUnavailable):
		claimed := p.SlotTime.UTC()
		if p.RunNow != "" {
			claimed = p.ClaimedAt.UTC()
		}
		if now.Sub(claimed) > startingDeadline {
			// Counted once here, so the next pass doesn't count the same
			// slot again as a missed window.
			r.skip(sched, "gateway_unavailable", &st.SkipCounts.GatewayUnavailable)
			r.dropPending(sched, cronSched, now)
			return false, nil
		}
		return true, nil
	default:
		return false, err
	}
}

// dropPending abandons the claimed slot: a run-now token is marked handled,
// and a cadence slot moves the schedule to its next slot.
func (r *AgentScheduleReconciler) dropPending(sched *agentv1alpha1.AgentSchedule, cronSched cron.Schedule, now time.Time) {
	st := &sched.Status
	if st.PendingSlot.RunNow != "" {
		st.LastHandledRunNow = st.PendingSlot.RunNow
	} else {
		st.NextScheduleTime = ptrTime(cronSched.Next(now))
	}
	st.PendingSlot = nil
}

func (r *AgentScheduleReconciler) skip(sched *agentv1alpha1.AgentSchedule, reason string, counter *int64) {
	*counter++
	agentScheduleSkipsTotal.WithLabelValues(sched.Name, reason).Inc()
}

// requeueAfter is the next time the schedule needs looking at: its next
// slot, or sooner while a run is in flight.
func (r *AgentScheduleReconciler) requeueAfter(sched *agentv1alpha1.AgentSchedule, now time.Time) time.Duration {
	d := time.Hour
	if sched.Status.NextScheduleTime != nil {
		if until := sched.Status.NextScheduleTime.UTC().Sub(now); until < d {
			d = until
		}
	}
	if sched.Status.ActiveTaskID != "" && agentScheduleActivePoll < d {
		d = agentScheduleActivePoll
	}
	if d < time.Second {
		d = time.Second
	}
	return d
}

func (r *AgentScheduleReconciler) writeStatus(ctx context.Context, sched *agentv1alpha1.AgentSchedule) error {
	return r.Status().Update(ctx, sched)
}

// latestSlot is the latest slot at or before now, walking from next.
func latestSlot(cronSched cron.Schedule, next, now time.Time) time.Time {
	latest := next
	for i := 0; i < agentScheduleMaxSlotWalk; i++ {
		after := cronSched.Next(latest)
		if after.After(now) {
			return latest
		}
		latest = after
	}
	return latest
}

func ptrTime(t time.Time) *metav1.Time {
	mt := metav1.NewTime(t.UTC())
	return &mt
}

// schedulesForProfile maps an AgentProfile event to the schedules that name
// it, so a tier change re-runs their Accepted check.
func (r *AgentScheduleReconciler) schedulesForProfile(ctx context.Context, obj client.Object) []reconcile.Request {
	var list agentv1alpha1.AgentScheduleList
	if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
		return nil
	}
	var reqs []reconcile.Request
	for _, s := range list.Items {
		if s.Spec.ProfileRef.Name == obj.GetName() {
			reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: s.Namespace, Name: s.Name}})
		}
	}
	return reqs
}

func (r *AgentScheduleReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.AgentSchedule{}).
		Watches(&agentv1alpha1.AgentProfile{}, handler.EnqueueRequestsFromMapFunc(r.schedulesForProfile)).
		Named("agentschedule").
		Complete(r)
}
