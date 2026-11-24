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
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
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
	agentScheduleReasonNoPlatformAgent   = "NoPlatformAgent"
	// agentScheduleReasonUserDeferred: a schedule the PlatformAgent didn't
	// render. User schedules are deferred to Track 7 (#5624; decided B on
	// #5635): which identity the gateway resolves a creator's tier from is
	// open, so none fires, and the gateway refuses origin user anyway.
	agentScheduleReasonUserDeferred = "UserSchedulesDeferred"
	// agentScheduleReasonBuiltinNotFromOperator: labelled builtin, but not
	// one of the built-in schedules the install's PlatformAgent renders (a
	// schedule someone else labelled, say, before the admission policy
	// refused that), so it isn't read as built-in.
	agentScheduleReasonBuiltinNotFromOperator = "BuiltinNotFromOperator"
	// agentScheduleReasonInvalidName: a name the run's trigger can't carry.
	// The trigger name is schedule/<name>, and the gateway holds <name> to a
	// dot-free DNS-1123 label, so any other name would be refused at every
	// slot.
	agentScheduleReasonInvalidName = "InvalidName"
	// agentScheduleReasonProfileScriptUnknown: spec.script names a helper
	// script the referenced profile's spec.scripts doesn't list. The
	// dispatcher would refuse the run, so none is submitted; adding the
	// script to the profile accepts the schedule (the profile watch
	// re-queues it).
	agentScheduleReasonProfileScriptUnknown = "ProfileScriptUnknown"
)

// The Submitted condition's reasons.
const (
	agentScheduleReasonSubmitted      = "RunSubmitted"
	agentScheduleReasonGatewayRefused = "GatewayRefused"
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
	// Delivery is how its report was delivered (an AgentScheduleReport*
	// value), or empty while the gateway has no record of it.
	Delivery string
}

// ScheduleTaskTracker reports on a run in flight.
type ScheduleTaskTracker interface {
	State(ctx context.Context, sched *agentv1alpha1.AgentSchedule, taskID string) (ScheduleTaskState, error)
}

// ScheduleTaskStopper stops a run in flight through the gateway, and returns
// the gateway's outcome: stopped, already_final or unknown_run.
type ScheduleTaskStopper interface {
	Stop(ctx context.Context, sched *agentv1alpha1.AgentSchedule, taskID string) (string, error)
}

// The skip reasons and run outcomes the metrics carry.
const (
	agentScheduleSkipSuspended          = "suspended"
	agentScheduleSkipAlreadyRunning     = "already_running"
	agentScheduleSkipMissedWindow       = "missed_window"
	agentScheduleSkipGatewayUnavailable = "gateway_unavailable"
	agentScheduleSkipRateLimited        = "rate_limited"
	agentScheduleSkipRefused            = "refused"
	agentScheduleRunSubmitted           = "submitted"
)

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

// schedulePass is one Reconcile's state: the metrics it counted, which go
// out only once the status write that records them succeeds (a conflict's
// retry would count them again), and the run deadline its profile sets.
type schedulePass struct {
	metrics        []prometheus.Counter
	activeDeadline time.Duration
	// stored is the status as last read or written, so an unchanged status
	// isn't written again.
	stored agentv1alpha1.AgentScheduleStatus
}

func (p *schedulePass) count(c prometheus.Counter) { p.metrics = append(p.metrics, c) }

// AgentScheduleReconciler fires AgentSchedules: when a slot comes due it
// claims it in status, submits an Unattended task through the gateway, and
// records the run (docs/designs/scheduling-on-profiles.md, section 2). It runs
// only on the elected operator leader, as every controller does.
type AgentScheduleReconciler struct {
	client.Client
	Scheme    *runtime.Scheme
	Submitter ScheduleSubmitter
	Tracker   ScheduleTaskTracker
	Stopper   ScheduleTaskStopper
	// Now is the clock; nil means time.Now.
	Now func() time.Time
	// BuiltinNames, when set, replaces the built-in set the PlatformAgent
	// renders (agentScheduleBuiltinNames). Tests set it.
	BuiltinNames func(*agentv1alpha1.PlatformAgent) []string
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

// agentScheduleBuiltinNames is the set of built-in schedules the install's
// PlatformAgent renders: the names of the table the PlatformAgent reconciler
// renders from (builtinSchedules, platformagent_builtin_schedules.go), so a
// schedule is read as built-in only when the operator would have written it.
//
// That renderer must own the whole object it renders: a schedule someone
// created under a built-in's name before the admission policy existed has to
// be overwritten in full (labels, references and spec) or deleted and
// created again, never adopted by name with its content kept.
func agentScheduleBuiltinNames(agent *agentv1alpha1.PlatformAgent) []string {
	if !AgentSchedulesEnabled() {
		return nil
	}
	return builtinScheduleNames(builtinSchedules(agent))
}

// builtinNames is the reconciler's built-in set: BuiltinNames when a test
// sets it, the rendered set otherwise.
func (r *AgentScheduleReconciler) builtinNames(agent *agentv1alpha1.PlatformAgent) []string {
	if r.BuiltinNames != nil {
		return r.BuiltinNames(agent)
	}
	return agentScheduleBuiltinNames(agent)
}

// agentScheduleOrigin is builtin only for a schedule the operator rendered:
// its controller is the install's PlatformAgent, its origin label reads
// builtin, and its name is one of the built-in schedules that PlatformAgent
// renders. The admission policy (config/admission/agentschedule-builtin-
// policy.yaml) keeps anyone but the operator from setting the label or the
// controller reference, and the name check covers a schedule written before
// the policy existed: whoever created it set both, and nothing it carries
// (its managed fields name a client-chosen manager) proves who. A builtin
// origin unlocks a built-in audit's report stream (design section 1); the
// gateway checks the stream against the trigger again.
func agentScheduleOrigin(sched *agentv1alpha1.AgentSchedule, agent *agentv1alpha1.PlatformAgent, builtins []string) string {
	if agent != nil && metav1.IsControlledBy(sched, agent) && controlledByAPlatformAgent(sched) &&
		sched.Labels[agentv1alpha1.AgentScheduleOriginLabel] == agentv1alpha1.AgentScheduleOriginBuiltin &&
		slices.Contains(builtins, sched.Name) {
		return agentv1alpha1.AgentScheduleOriginBuiltin
	}
	return agentv1alpha1.AgentScheduleOriginUser
}

// agentScheduleScriptProblem says why a schedule's prompt or script can't
// run, or "" when they can. Exactly one of them is set, and a script sets
// neither skills nor maxTurns, which the CRD's rules hold too; this is the
// same check for an object stored without them. A script runs only on a
// built-in schedule (agentScheduleOrigin): a helper script runs with no model
// in the loop, so only the operator may pick one, and the gateway, dispatcher
// and worker each check the script again. Whether the profile lists the
// script is accept()'s check, once it has read the profile.
func agentScheduleScriptProblem(sched *agentv1alpha1.AgentSchedule, origin string) string {
	hasPrompt, hasScript := sched.Spec.Prompt != "", sched.Spec.Script != ""
	switch {
	case hasPrompt == hasScript:
		return "exactly one of spec.prompt and spec.script must be set"
	case hasScript && (len(sched.Spec.Skills) > 0 || sched.Spec.MaxTurns != nil):
		return "spec.skills and spec.maxTurns apply to a model run; a script run takes neither"
	case hasScript && origin != agentv1alpha1.AgentScheduleOriginBuiltin:
		return fmt.Sprintf("spec.script names helper script %q, which only a built-in schedule the PlatformAgent renders may run", sched.Spec.Script)
	}
	return ""
}

// controlledByAPlatformAgent reports whether the schedule's controller
// reference names a PlatformAgent, not only its uid: metav1.IsControlledBy
// compares uids alone.
func controlledByAPlatformAgent(sched *agentv1alpha1.AgentSchedule) bool {
	ref := metav1.GetControllerOf(sched)
	return ref != nil && ref.Kind == "PlatformAgent" && ref.APIVersion == agentv1alpha1.GroupVersion.String()
}

func slotKey(sched *agentv1alpha1.AgentSchedule, slot time.Time) string {
	return fmt.Sprintf("schedule/%s/%s/%d", sched.Namespace, sched.Name, slot.Unix())
}

func runNowKey(sched *agentv1alpha1.AgentSchedule, token string) string {
	return fmt.Sprintf("run-now/%s/%s/%s", sched.Namespace, sched.Name, token)
}

// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentschedules,verbs=get;list;watch
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentschedules/status,verbs=get;update

func (r *AgentScheduleReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var sched agentv1alpha1.AgentSchedule
	if err := r.Get(ctx, req.NamespacedName, &sched); err != nil {
		if apierrors.IsNotFound(err) {
			// A deleted PlatformAgent's built-in schedules are collected
			// with it; the last one gone is the cue to close its bus
			// connection, which reconnects forever otherwise.
			return ctrl.Result{}, r.forgetIfNoAgent(ctx, req.Namespace)
		}
		return ctrl.Result{}, err
	}
	pass := &schedulePass{stored: *sched.Status.DeepCopy()}
	cronSched, agent, accepted, err := r.accept(ctx, &sched, pass)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !accepted {
		// Nothing fires; a profile or spec change requeues it.
		return ctrl.Result{}, r.writeStatus(ctx, &sched, pass)
	}
	if sched.Status.NextScheduleTime != nil && sched.Status.Schedule != sched.Spec.Schedule {
		// A new cron expression (an edit, a re-time through the
		// PlatformAgent's overrides): the next slot follows it, from now,
		// rather than waiting out the old one's. Keyed on the expression,
		// not the generation, so an edit to anything else (the prompt, say)
		// keeps a due slot, and an edit made while not accepted still
		// takes effect once it is.
		sched.Status.NextScheduleTime = ptrTime(cronSched.Next(r.now()))
	}
	sched.Status.Schedule = sched.Spec.Schedule
	return r.fire(ctx, &sched, cronSched, agent, pass)
}

// forgetIfNoAgent closes the submitter's bus connection for namespace when no
// PlatformAgent is left there.
func (r *AgentScheduleReconciler) forgetIfNoAgent(ctx context.Context, namespace string) error {
	var agents agentv1alpha1.PlatformAgentList
	if err := r.List(ctx, &agents, client.InNamespace(namespace)); err != nil {
		return err
	}
	if len(agents.Items) == 0 {
		r.forgetNamespace(namespace)
	}
	return nil
}

// forgetNamespace closes the submitter's bus connection for namespace, when
// the submitter holds one (the bus submitter does; a test's fake doesn't).
func (r *AgentScheduleReconciler) forgetNamespace(namespace string) {
	if f, ok := r.Submitter.(interface{ forget(namespace string) }); ok {
		f.forget(namespace)
	}
}

// accept sets the Accepted condition: the schedule parses, and its profile
// exists, allows the unattended tier, and lists the schedule's script if it
// names one. The gateway checks the tier again at every submission, and the
// dispatcher the script, since a profile can change after this ran.
func (r *AgentScheduleReconciler) accept(ctx context.Context, sched *agentv1alpha1.AgentSchedule, pass *schedulePass) (cron.Schedule, *agentv1alpha1.PlatformAgent, bool, error) {
	cond := metav1.Condition{Type: agentv1alpha1.AgentScheduleConditionAccepted, ObservedGeneration: sched.Generation}
	sched.Status.ObservedGeneration = sched.Generation
	cronSched, perr := parseAgentSchedule(sched.Spec.Schedule)
	if perr == nil && cronSched.Next(r.now()).IsZero() {
		// A schedule no date matches (the 30th of February): the parser's
		// Next gives up and returns the zero time, which would read as a
		// slot that is always due.
		perr = errors.New("no date matches it")
	}
	if perr != nil {
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonInvalidSchedule
		cond.Message = fmt.Sprintf("schedule %q does not parse: %v", sched.Spec.Schedule, perr)
		meta.SetStatusCondition(&sched.Status.Conditions, cond)
		return nil, nil, false, nil
	}
	if errs := validation.IsDNS1123Label(sched.Name); len(errs) > 0 {
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonInvalidName
		cond.Message = fmt.Sprintf("the name must be a dot-free DNS-1123 label, since the run's trigger is schedule/<name>: %s", strings.Join(errs, "; "))
		meta.SetStatusCondition(&sched.Status.Conditions, cond)
		return nil, nil, false, nil
	}
	// A schedule fires only in an install's namespace: one PlatformAgent per
	// cluster, whose gateway takes its runs. Elsewhere it would submit as the
	// install's trigger under a name nothing there owns.
	var agents agentv1alpha1.PlatformAgentList
	if err := r.List(ctx, &agents, client.InNamespace(sched.Namespace)); err != nil {
		return nil, nil, false, err
	}
	if len(agents.Items) == 0 {
		r.forgetNamespace(sched.Namespace)
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonNoPlatformAgent
		cond.Message = "no PlatformAgent in this namespace: schedules run only in an install's namespace"
		meta.SetStatusCondition(&sched.Status.Conditions, cond)
		return nil, nil, false, nil
	}
	agent := &agents.Items[0]
	origin := agentScheduleOrigin(sched, agent, r.builtinNames(agent))
	if msg := agentScheduleScriptProblem(sched, origin); msg != "" {
		cond.Status, cond.Reason, cond.Message = metav1.ConditionFalse, agentScheduleReasonInvalidSchedule, msg
		meta.SetStatusCondition(&sched.Status.Conditions, cond)
		return nil, nil, false, nil
	}
	if origin != agentv1alpha1.AgentScheduleOriginBuiltin {
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonUserDeferred
		cond.Message = "only schedules the PlatformAgent renders run for now; user schedules wait on Track 7 (#5624)"
		if sched.Labels[agentv1alpha1.AgentScheduleOriginLabel] == agentv1alpha1.AgentScheduleOriginBuiltin {
			cond.Reason = agentScheduleReasonBuiltinNotFromOperator
			cond.Message = "labelled builtin, but not a built-in schedule this install's PlatformAgent renders, so it runs as nothing"
		}
		meta.SetStatusCondition(&sched.Status.Conditions, cond)
		return nil, nil, false, nil
	}
	var profile agentv1alpha1.AgentProfile
	err := r.Get(ctx, types.NamespacedName{Namespace: sched.Namespace, Name: sched.Spec.ProfileRef.Name}, &profile)
	switch {
	case apierrors.IsNotFound(err):
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonProfileNotFound
		cond.Message = fmt.Sprintf("AgentProfile %q does not exist in this namespace", sched.Spec.ProfileRef.Name)
	case err != nil:
		return nil, nil, false, err
	case !slices.Contains(profile.AllowedTierList(), agentv1alpha1.AgentProfileTierUnattended):
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonProfileDisallowed
		cond.Message = fmt.Sprintf("AgentProfile %q does not allow the unattended tier", profile.Name)
	case sched.Spec.Script != "" && !slices.ContainsFunc(profile.Spec.Scripts, func(s agentv1alpha1.AgentProfileScript) bool {
		return s.Name == sched.Spec.Script
	}):
		cond.Status, cond.Reason = metav1.ConditionFalse, agentScheduleReasonProfileScriptUnknown
		cond.Message = fmt.Sprintf("spec.script names helper script %q, which AgentProfile %q's spec.scripts does not list", sched.Spec.Script, profile.Name)
	default:
		cond.Status, cond.Reason = metav1.ConditionTrue, agentScheduleReasonValid
		cond.Message = "the schedule parses and its profile allows the unattended tier"
		pass.activeDeadline = durationOr(sched.Spec.ActiveDeadlineSeconds,
			durationOr(&profile.Spec.Lifecycle.ActiveDeadlineSeconds, agentScheduleDefaultActiveDeadline))
	}
	meta.SetStatusCondition(&sched.Status.Conditions, cond)
	return cronSched, agent, cond.Status == metav1.ConditionTrue, nil
}

// fire is the slot logic. Each claim is written to status before its
// submission, so a restart at any point re-sends the same idempotency key.
func (r *AgentScheduleReconciler) fire(ctx context.Context, sched *agentv1alpha1.AgentSchedule, cronSched cron.Schedule,
	agent *agentv1alpha1.PlatformAgent, pass *schedulePass) (ctrl.Result, error) {
	st := &sched.Status
	now := r.now()
	startingDeadline := durationOr(sched.Spec.StartingDeadlineSeconds, agentScheduleDefaultStartingDeadline)

	// 1. Runs that ended on an earlier pass: how was each one's report
	// delivered? Asked before the run in flight is tracked, so a run that
	// ends on this pass, whose answer was just read, isn't asked again until
	// the next one.
	r.trackDeliveries(ctx, sched, now)
	// 1b. A run in flight: did it end, or vanish, or outlive its deadline?
	if st.ActiveTaskID != "" {
		r.trackActive(ctx, sched, now, pass)
	}

	// 1c. Stop: a token not handled yet ends the run in flight. A gateway
	// that doesn't answer leaves the token for the next pass.
	stopRetry, err := r.handleStop(ctx, sched, pass)
	if err != nil {
		return ctrl.Result{}, err
	}

	// 2. A claimed slot the gateway has not taken yet (a retry, or a
	// restart between the claim and the status write after it).
	if st.PendingSlot != nil {
		retry, err := r.submitPending(ctx, sched, agent, cronSched, now, startingDeadline, pass)
		if err != nil {
			return ctrl.Result{}, err
		}
		if retry {
			return ctrl.Result{RequeueAfter: agentScheduleRetry}, r.writeStatus(ctx, sched, pass)
		}
	}

	// 3. Run now: a token not handled yet starts a run, or adopts the one
	// in flight. It leaves the cadence alone.
	// run-now is handled before the slot below, whose suspend skip it must
	// not reach: a run-now-only built-in (onboarding) renders suspended and
	// runs only this way (TestARunNowRunsASuspendedSchedule).
	if token := sched.Annotations[agentv1alpha1.AgentScheduleRunNowAnnotation]; token != "" && token != st.LastHandledRunNow && st.PendingSlot == nil {
		// A run in flight that is being stopped isn't adopted: a run-now
		// asked for after a stop is a new run, started once the stopped one
		// has settled.
		stopping := st.ActiveTaskID != "" && st.ActiveTaskID == st.StoppedTaskID
		if st.ActiveTaskID != "" && !stopping {
			st.LastHandledRunNow = token
		} else if !stopping {
			st.PendingSlot = &agentv1alpha1.AgentSchedulePendingSlot{
				SlotTime: metav1.NewTime(now), IdempotencyKey: runNowKey(sched, token), RunNow: token, ClaimedAt: metav1.NewTime(now),
			}
			if err := r.writeStatus(ctx, sched, pass); err != nil {
				return ctrl.Result{}, err
			}
			retry, err := r.submitPending(ctx, sched, agent, cronSched, now, startingDeadline, pass)
			if err != nil {
				return ctrl.Result{}, err
			}
			if retry {
				return ctrl.Result{RequeueAfter: agentScheduleRetry}, r.writeStatus(ctx, sched, pass)
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
			r.skip(pass, sched, agentScheduleSkipSuspended, &st.SkipCounts.Suspended)
			st.NextScheduleTime = ptrTime(cronSched.Next(now))
		case st.ActiveTaskID != "" || st.PendingSlot != nil:
			r.skip(pass, sched, agentScheduleSkipAlreadyRunning, &st.SkipCounts.AlreadyRunning)
			st.NextScheduleTime = ptrTime(cronSched.Next(now))
		default:
			slot := next
			if now.Sub(next) > startingDeadline {
				// Missed while the operator or the gateway was down: one
				// count, and the latest slot still inside the deadline
				// runs, if there is one.
				r.skip(pass, sched, agentScheduleSkipMissedWindow, &st.SkipCounts.MissedWindow)
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
			if err := r.writeStatus(ctx, sched, pass); err != nil {
				return ctrl.Result{}, err
			}
			retry, err := r.submitPending(ctx, sched, agent, cronSched, now, startingDeadline, pass)
			if err != nil {
				return ctrl.Result{}, err
			}
			if retry {
				return ctrl.Result{RequeueAfter: agentScheduleRetry}, r.writeStatus(ctx, sched, pass)
			}
		}
	}

	if err := r.writeStatus(ctx, sched, pass); err != nil {
		return ctrl.Result{}, err
	}
	requeue := r.requeueAfter(sched, now)
	if stopRetry && agentScheduleRetry < requeue {
		requeue = agentScheduleRetry
	}
	return ctrl.Result{RequeueAfter: requeue}, nil
}

// trackActive settles a run in flight that ended, vanished or outlived its
// deadline plus grace, so a lost task never wedges the schedule.
func (r *AgentScheduleReconciler) trackActive(ctx context.Context, sched *agentv1alpha1.AgentSchedule, now time.Time, pass *schedulePass) {
	st := &sched.Status
	state, err := r.Tracker.State(ctx, sched, st.ActiveTaskID)
	if err != nil {
		// No answer from the bus is no answer: the deadline still applies,
		// so a bus that keeps failing cannot hold the schedule forever.
		logf.FromContext(ctx).Error(err, "reading a scheduled run's state", "schedule", sched.Name, "task", st.ActiveTaskID)
		state = ScheduleTaskState{Found: true}
	}
	deadline := pass.activeDeadline
	if deadline <= 0 {
		deadline = durationOr(sched.Spec.ActiveDeadlineSeconds, agentScheduleDefaultActiveDeadline)
	}
	switch {
	case state.Found && state.Terminal:
		taskID := st.ActiveTaskID
		r.settle(pass, sched, state.Outcome, state.CompletedAt)
		recordDelivery(st, taskID, state.Delivery)
	case !state.Found:
		r.settle(pass, sched, agentv1alpha1.AgentScheduleOutcomeLost, now)
	case st.ActiveTaskStartedAt != nil && now.Sub(st.ActiveTaskStartedAt.UTC()) > deadline+agentScheduleLostGrace:
		r.settle(pass, sched, agentv1alpha1.AgentScheduleOutcomeLost, now)
	}
}

// agentScheduleDeliveryWindow is how long after a run ends the reconciler
// keeps asking how its report was delivered: the relay's redeliveries finish
// well inside it, and past it the run keeps an empty Delivery.
const agentScheduleDeliveryWindow = 10 * time.Minute

// awaitingDelivery reports whether run ended recently enough, and with a task
// the gateway may still hold, for its delivery to be asked after.
func awaitingDelivery(run agentv1alpha1.AgentScheduleRun, active string, now time.Time) bool {
	return run.Delivery == "" && run.TaskID != active && run.CompletionTime != nil &&
		run.Outcome != agentv1alpha1.AgentScheduleOutcomeLost &&
		now.Sub(run.CompletionTime.UTC()) <= agentScheduleDeliveryWindow
}

// trackDeliveries asks the gateway, for each recent run still awaiting it, how
// its report was delivered, and records a final answer. A run settles when it
// ends, so this never holds up the next slot.
func (r *AgentScheduleReconciler) trackDeliveries(ctx context.Context, sched *agentv1alpha1.AgentSchedule, now time.Time) {
	st := &sched.Status
	for i := range st.RecentRuns {
		if !awaitingDelivery(st.RecentRuns[i], st.ActiveTaskID, now) {
			continue
		}
		state, err := r.Tracker.State(ctx, sched, st.RecentRuns[i].TaskID)
		if err != nil {
			logf.FromContext(ctx).Error(err, "reading a scheduled run's delivery", "schedule", sched.Name, "task", st.RecentRuns[i].TaskID)
			return
		}
		recordDelivery(st, st.RecentRuns[i].TaskID, state.Delivery)
	}
}

// recordDelivery stores delivery on taskID's run when it is final and one the
// status's enum admits, once, counting post_failed and post_unknown. posting, empty or an
// unknown spelling leaves the run as it was.
func recordDelivery(st *agentv1alpha1.AgentScheduleStatus, taskID, delivery string) {
	switch delivery {
	case agentv1alpha1.AgentScheduleReportNone, agentv1alpha1.AgentScheduleReportLocal, agentv1alpha1.AgentScheduleReportSilent,
		agentv1alpha1.AgentScheduleReportStoredNoHome, agentv1alpha1.AgentScheduleReportPosted, agentv1alpha1.AgentScheduleReportPostFailed,
		agentv1alpha1.AgentScheduleReportPostUnknown:
	default:
		return
	}
	for i := range st.RecentRuns {
		if st.RecentRuns[i].TaskID == taskID && st.RecentRuns[i].Delivery == "" {
			st.RecentRuns[i].Delivery = delivery
			switch delivery {
			case agentv1alpha1.AgentScheduleReportPostFailed:
				st.PostFailed++
			case agentv1alpha1.AgentScheduleReportPostUnknown:
				st.PostUnknown++
			}
		}
	}
}

// knownOutcome is outcome when the status's enum admits it, else Failed: an
// outcome the bus spells some other way must not make every status write
// fail validation.
func knownOutcome(outcome string) string {
	switch outcome {
	case agentv1alpha1.AgentScheduleOutcomeSucceeded, agentv1alpha1.AgentScheduleOutcomeFailed,
		agentv1alpha1.AgentScheduleOutcomeCancelled, agentv1alpha1.AgentScheduleOutcomeLost:
		return outcome
	}
	return agentv1alpha1.AgentScheduleOutcomeFailed
}

// settle records a run's end and clears it from flight.
func (r *AgentScheduleReconciler) settle(pass *schedulePass, sched *agentv1alpha1.AgentSchedule, outcome string, at time.Time) {
	st := &sched.Status
	outcome = knownOutcome(outcome)
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
	pass.count(agentScheduleRunsTotal.WithLabelValues(sched.Name, strings.ToLower(outcome)))
	st.ActiveTaskID = ""
	st.ActiveTaskStartedAt = nil
	st.StoppedTaskID = ""
}

// handleStop acts on a stop token the status hasn't handled yet. It reports
// retry when the gateway didn't answer, so the token waits for the next pass.
func (r *AgentScheduleReconciler) handleStop(ctx context.Context, sched *agentv1alpha1.AgentSchedule, pass *schedulePass) (bool, error) {
	st := &sched.Status
	token := sched.Annotations[agentv1alpha1.AgentScheduleStopAnnotation]
	if token == "" || token == st.LastHandledStop {
		return false, nil
	}
	if st.PendingSlot != nil {
		// A claim in flight may already be a run the gateway took (a submit
		// that timed out after the publish). Dropping it would leave that run
		// untracked, so the token waits until the claim resolves: into a run,
		// which the next pass stops, or into a dropped slot.
		return false, nil
	}
	if st.ActiveTaskID == "" {
		st.LastHandledStop = token
		return false, nil
	}
	if r.Stopper == nil {
		return false, nil
	}
	// Two phases. First the run being stopped (stoppedTaskID) is written and
	// checked kept: an API server still serving a CRD from before these
	// fields (helm upgrade never applies crds/) prunes it, and there a token
	// could never be recorded, would read as new on every pass, and would
	// stop every later run. The token itself is recorded only once the
	// gateway has answered, so a stop lost to a crash, a lost leadership or a
	// failed write before it is sent again on the next pass (the gateway
	// answers a repeat for a run it already stopped as stopped).
	st.StoppedTaskID = st.ActiveTaskID
	if err := r.writeStatus(ctx, sched, pass); err != nil {
		return false, err
	}
	if st.StoppedTaskID != st.ActiveTaskID {
		logf.FromContext(ctx).Error(nil, "not stopping a scheduled run: the AgentSchedule CRD drops status.stoppedTaskID and lastHandledStop; apply this release's CRDs",
			"schedule", sched.Name, "taskId", st.ActiveTaskID)
		return false, nil
	}
	result, err := r.Stopper.Stop(ctx, sched, st.ActiveTaskID)
	switch {
	case errors.Is(err, errTriggerStopRefused):
		// The gateway refused this stop for good (the run predates the
		// record of its trigger, say): the token is handled, and the run
		// ends at its deadline.
		logf.FromContext(ctx).Error(err, "the gateway refused a scheduled run's stop", "schedule", sched.Name, "taskId", st.ActiveTaskID)
		st.LastHandledStop, st.StoppedTaskID = token, ""
		return false, nil
	case err != nil:
		// Kept while the stop is retried, as a claimed slot is
		// (PendingSlot): a run-now meanwhile doesn't adopt the run being
		// stopped, and a retry pass has nothing new to write.
		logf.FromContext(ctx).Error(err, "stopping a scheduled run", "schedule", sched.Name, "taskId", st.ActiveTaskID)
		return true, nil
	}
	st.LastHandledStop = token
	// Stopped: the run's terminal (canceled) arrives through the tracker,
	// which records it as Cancelled. Already final or unknown: the tracker
	// settles it as it ends or goes missing.
	logf.FromContext(ctx).Info("scheduled run stop", "schedule", sched.Name, "taskId", st.ActiveTaskID, "result", result)
	return false, nil
}

// submitPending sends the claimed slot. It reports retry when the gateway
// did not take it and the deadline still allows another try.
func (r *AgentScheduleReconciler) submitPending(ctx context.Context, sched *agentv1alpha1.AgentSchedule, agent *agentv1alpha1.PlatformAgent, cronSched cron.Schedule,
	now time.Time, startingDeadline time.Duration, pass *schedulePass) (bool, error) {
	st := &sched.Status
	p := st.PendingSlot
	taskID, err := r.Submitter.Submit(ctx, ScheduleSubmission{
		IdempotencyKey: p.IdempotencyKey,
		TriggerName:    AgentScheduleTriggerName(sched),
		Origin:         agentScheduleOrigin(sched, agent, r.builtinNames(agent)),
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
		pass.count(agentScheduleRunsTotal.WithLabelValues(sched.Name, agentScheduleRunSubmitted))
		meta.SetStatusCondition(&st.Conditions, metav1.Condition{
			Type: agentv1alpha1.AgentScheduleConditionSubmitted, Status: metav1.ConditionTrue,
			Reason: agentScheduleReasonSubmitted, Message: "the gateway took the last run", ObservedGeneration: sched.Generation,
		})
		st.PendingSlot = nil
		return false, nil
	case errors.Is(err, ErrScheduleRateLimited):
		r.skip(pass, sched, agentScheduleSkipRateLimited, &st.SkipCounts.RateLimited)
		r.dropPending(sched, cronSched, now)
		return false, nil
	case errors.Is(err, errTriggerRefused):
		// The gateway refused the submission itself (invalid,
		// profile_unknown, tier_not_allowed): sending it again won't help, so
		// the slot is dropped at once and the refusal is shown. The next slot
		// tries again, since a profile still being published can clear.
		logf.FromContext(ctx).Error(err, "the gateway refused a scheduled run", "schedule", sched.Name, "key", p.IdempotencyKey)
		meta.SetStatusCondition(&st.Conditions, metav1.Condition{
			Type: agentv1alpha1.AgentScheduleConditionSubmitted, Status: metav1.ConditionFalse,
			Reason: agentScheduleReasonGatewayRefused, Message: agentScheduleRefusalMessage(err), ObservedGeneration: sched.Generation,
		})
		pass.count(agentScheduleSkipsTotal.WithLabelValues(sched.Name, agentScheduleSkipRefused))
		r.dropPending(sched, cronSched, now)
		return false, nil
	default:
		// The gateway unreachable, or an answer this reconciler doesn't
		// know: the claim is held and retried until the starting deadline,
		// then counted and dropped, so no error can wedge the schedule.
		if !errors.Is(err, ErrScheduleGatewayUnavailable) {
			logf.FromContext(ctx).Error(err, "submitting a scheduled run", "schedule", sched.Name, "key", p.IdempotencyKey)
		}
		claimed := p.SlotTime.UTC()
		if p.RunNow != "" {
			claimed = p.ClaimedAt.UTC()
		}
		if now.Sub(claimed) > startingDeadline {
			// Counted once here, so the next pass doesn't count the same
			// slot again as a missed window.
			r.skip(pass, sched, agentScheduleSkipGatewayUnavailable, &st.SkipCounts.GatewayUnavailable)
			r.dropPending(sched, cronSched, now)
			return false, nil
		}
		return true, nil
	}
}

// agentScheduleRefusalMaxRunes bounds the refusal shown on the Submitted
// condition: the gateway's detail is its own text, and a condition message
// has to fit the API server's limit.
const agentScheduleRefusalMaxRunes = 1024

// agentScheduleRefusalMessage is a refusal as the Submitted condition shows
// it.
func agentScheduleRefusalMessage(err error) string {
	msg := []rune(err.Error())
	if len(msg) > agentScheduleRefusalMaxRunes {
		return string(msg[:agentScheduleRefusalMaxRunes]) + "…"
	}
	return string(msg)
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

func (r *AgentScheduleReconciler) skip(pass *schedulePass, sched *agentv1alpha1.AgentSchedule, reason string, counter *int64) {
	*counter++
	pass.count(agentScheduleSkipsTotal.WithLabelValues(sched.Name, reason))
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
	for _, run := range sched.Status.RecentRuns {
		if awaitingDelivery(run, sched.Status.ActiveTaskID, now) && agentScheduleActivePoll < d {
			d = agentScheduleActivePoll
		}
	}
	if d < time.Second {
		d = time.Second
	}
	return d
}

// writeStatus stores the status and, once it is stored, the metrics the pass
// counted for it.
func (r *AgentScheduleReconciler) writeStatus(ctx context.Context, sched *agentv1alpha1.AgentSchedule, pass *schedulePass) error {
	if equality.Semantic.DeepEqual(pass.stored, sched.Status) {
		return nil
	}
	if err := r.Status().Update(ctx, sched); err != nil {
		return err
	}
	pass.stored = *sched.Status.DeepCopy()
	for _, c := range pass.metrics {
		c.Inc()
	}
	pass.metrics = nil
	return nil
}

// latestSlot is the latest slot at or before now, walking from next.
func latestSlot(cronSched cron.Schedule, next, now time.Time) time.Time {
	latest := next
	for i := 0; i < agentScheduleMaxSlotWalk; i++ {
		after := cronSched.Next(latest)
		if after.IsZero() || !after.After(latest) || after.After(now) {
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

// schedulesInNamespace maps a PlatformAgent event to every schedule beside
// it, so a schedule created before its install is accepted once it exists.
func (r *AgentScheduleReconciler) schedulesInNamespace(ctx context.Context, obj client.Object) []reconcile.Request {
	var list agentv1alpha1.AgentScheduleList
	if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
		return nil
	}
	reqs := make([]reconcile.Request, 0, len(list.Items))
	for _, s := range list.Items {
		reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: s.Namespace, Name: s.Name}})
	}
	return reqs
}

func (r *AgentScheduleReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// The bus side, unless a test injected its own: submissions, status
	// queries and stops to the gateway's trigger ingress on the operator's
	// own bus connection (agentschedule_submitter.go).
	if r.Submitter == nil || r.Tracker == nil || r.Stopper == nil {
		bus := newNATSScheduleSubmitter(newNATSCardPublisher(mgr.GetClient()), mgr.GetClient())
		if r.Submitter == nil {
			r.Submitter = bus
		}
		if r.Tracker == nil {
			r.Tracker = bus
		}
		if r.Stopper == nil {
			r.Stopper = bus
		}
	}
	return ctrl.NewControllerManagedBy(mgr).
		For(&agentv1alpha1.AgentSchedule{}).
		// Only spec changes matter here (a profile's tiers, an install
		// appearing): status writes, which the operator makes often, don't
		// re-queue every schedule.
		Watches(&agentv1alpha1.AgentProfile{}, handler.EnqueueRequestsFromMapFunc(r.schedulesForProfile),
			builder.WithPredicates(predicate.GenerationChangedPredicate{})).
		Watches(&agentv1alpha1.PlatformAgent{}, handler.EnqueueRequestsFromMapFunc(r.schedulesInNamespace),
			builder.WithPredicates(predicate.GenerationChangedPredicate{})).
		Named("agentschedule").
		Complete(r)
}
