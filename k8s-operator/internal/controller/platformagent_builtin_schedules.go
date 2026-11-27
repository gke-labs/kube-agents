package controller

import (
	"context"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The built-in AgentSchedules the operator renders (docs/designs/scheduling-on-profiles.md,
// section 5). One table, builtinSchedules, is both what the PlatformAgent reconciler renders
// and the name set the schedule reconciler reads a schedule as built-in against
// (agentScheduleBuiltinNames), so the two can't disagree.
//
// A built-in's name is reserved: exact, listed in the docs, and the operator's alone. A schedule
// on a reserved name that the operator didn't render (no controller reference to this
// PlatformAgent, or no builtin origin label) is deleted and created again, with an Event, never
// adopted with its content kept (#6605). Any other schedule is never touched, whatever its name
// looks like: the operator's create, update and delete on agentschedules reach the whole
// namespace, so the renderer changes or deletes only what carries its own marks, and a squatter
// on an exact reserved name.

// The renderer creates, rewrites and deletes the built-in schedules: the only writes the
// operator makes to agentschedules, and checked against its own marks before each one. In the
// install namespace only, like every other write the operator makes there (the reads stay
// cluster-wide, on the schedule reconciler's marker).
// +kubebuilder:rbac:groups=kubeagents.x-k8s.io,resources=agentschedules,verbs=create;update;patch;delete,namespace=kubeagents-system

// builtinCanarySuffix ends a canary row's name. A canary runs a built-in in shadow beside the
// job it will replace: its own name gives it its own report stream (the stream is the schedule's
// name), so the real job's baseline and ledger are untouched. Going live removes the canary row
// and adds the real one; it never renames an object, so the two streams never cross and the
// canary's history stays readable for the comparison.
const builtinCanarySuffix = "-canary"

// builtinDryRunParameter is the task parameter every canary row carries, set to "true": the
// profile's scripts read it and skip every write but the report store's own stream (no ledger,
// no findings queue, no remediation PRs, no cluster change). It is behaviour, not enforcement:
// what stops a shadow run writing is that its unattended grant holds no write permit. The
// profile declares it on its card, so the gateway's declared-parameter check carries it.
const builtinDryRunParameter = "dry_run"

// builtinSchedule is one row of the built-in table.
type builtinSchedule struct {
	// Name is the schedule's exact, reserved name. A canary's ends in builtinCanarySuffix.
	Name string
	// Canary marks a shadow row: delivery is forced to local and the dry-run parameter is set,
	// whatever Spec says.
	Canary bool
	// RunNowOnly marks a row that runs only when the operator sets its run-now annotation, such
	// as onboarding. spec.schedule is required, so it renders suspended on a nominal schedule;
	// the schedule reconciler handles run-now before it skips a suspended slot
	// (TestARunNowRunsASuspendedSchedule pins that order). An override may only suspend it
	// further (builtinRunNowSuspended), never unsuspend it.
	RunNowOnly bool
	// Spec is what the schedule runs. Its Suspend is the default for a row no override names.
	Spec agentv1alpha1.AgentScheduleSpec
}

// builtinSchedules is the install's built-in table. No row ships yet: onboarding (#5623) and
// the compliance-audit canary (#5620) each add theirs.
//
//	{Name: "compliance-audit-canary", Canary: true, Spec: ...}, // #5620, Track 4b PR 1
func builtinSchedules(*agentv1alpha1.PlatformAgent) []builtinSchedule { return nil }

// The BuiltinSchedules condition's reasons.
const (
	builtinSchedulesConditionType = "BuiltinSchedules"
	// builtinSchedulesReasonOverrideUnknown: an override names no built-in schedule.
	builtinSchedulesReasonOverrideUnknown = "OverrideUnknown"
	// builtinSchedulesReasonOverrideRefused: an override would unsuspend a run-now-only schedule.
	builtinSchedulesReasonOverrideRefused = "OverrideRefused"
	// builtinSchedulesReasonReplaced: a schedule on a reserved name that the operator didn't
	// render was deleted, to be created again as the built-in.
	builtinSchedulesReasonReplaced = "ReplacedUnownedSchedule"
	// builtinSchedulesReasonForbidden: the operator's role refuses a write the renderer needs,
	// as an operator image ahead of its RBAC (the namespaced Role holds the writes) would see. The
	// rest of the reconcile goes on.
	builtinSchedulesReasonForbidden = "WritesForbidden"
)

// builtinSchedulesConditionReasons are the condition's reasons, for its docs row.
var builtinSchedulesConditionReasons = []string{
	builtinSchedulesReasonOverrideUnknown,
	builtinSchedulesReasonOverrideRefused,
	builtinSchedulesReasonReplaced,
	builtinSchedulesReasonForbidden,
}

// builtinScheduleRows is the reconciler's table: BuiltinSchedules when a test sets it, the
// shipped table otherwise. Off while the schedule reconciler is off (A2A_AGENT_SCHEDULES), so a
// schedule is rendered only where something will run it.
func (r *PlatformAgentReconciler) builtinScheduleRows(agent *agentv1alpha1.PlatformAgent) []builtinSchedule {
	if r.BuiltinSchedules != nil {
		return r.BuiltinSchedules(agent)
	}
	if !AgentSchedulesEnabled() {
		return nil
	}
	return builtinSchedules(agent)
}

// builtinScheduleNames is the table's names.
func builtinScheduleNames(rows []builtinSchedule) []string {
	names := make([]string, 0, len(rows))
	for _, row := range rows {
		names = append(names, row.Name)
	}
	return names
}

// overrideFor is the PlatformAgent's override for a rendered name.
func overrideFor(agent *agentv1alpha1.PlatformAgent, name string) (agentv1alpha1.AgentScheduleOverride, bool) {
	if agent.Spec.Schedules == nil {
		return agentv1alpha1.AgentScheduleOverride{}, false
	}
	for _, o := range agent.Spec.Schedules.Overrides {
		if o.Name == name {
			return o, true
		}
	}
	return agentv1alpha1.AgentScheduleOverride{}, false
}

// builtinSuspendAll is spec.schedules.suspendAll: every built-in no override names is suspended.
func builtinSuspendAll(agent *agentv1alpha1.PlatformAgent) bool {
	return agent.Spec.Schedules != nil && agent.Spec.Schedules.SuspendAll
}

// builtinRunNowSuspended reports a run-now-only built-in that an override suspends: the
// operator then never sets its run-now, so an operator can stop it without a release.
func builtinRunNowSuspended(agent *agentv1alpha1.PlatformAgent, row builtinSchedule) bool {
	o, ok := overrideFor(agent, row.Name)
	return row.RunNowOnly && ok && o.Suspend
}

// ownedBuiltinSchedule reports a schedule carrying the operator's marks for this PlatformAgent:
// its controller is the PlatformAgent, named by kind, and its origin label reads builtin.
func ownedBuiltinSchedule(sched *agentv1alpha1.AgentSchedule, agent *agentv1alpha1.PlatformAgent) bool {
	return metav1.IsControlledBy(sched, agent) && controlledByAPlatformAgent(sched) &&
		sched.Labels[agentv1alpha1.AgentScheduleOriginLabel] == agentv1alpha1.AgentScheduleOriginBuiltin
}

// builtinAppliedSuspendAnnotation records the spec.suspend the operator last wrote on a built-in,
// from an override or the row's default. With no override, a spec.suspend that still equals it
// is the operator's own and follows the row's default, so removing an override, or a release
// changing a row's default, reaches existing schedules. One that differs was set by someone
// directly, which the admission policy allows, and stands.
const builtinAppliedSuspendAnnotation = "kubeagents.x-k8s.io/builtin-applied-suspend"

// desiredBuiltinSpec is the spec a row renders onto existing (nil on a create), and the suspend
// value the operator applied, for builtinAppliedSuspendAnnotation, or nil when it kept a value
// someone set directly.
func desiredBuiltinSpec(agent *agentv1alpha1.PlatformAgent, row builtinSchedule, existing *agentv1alpha1.AgentSchedule) (agentv1alpha1.AgentScheduleSpec, *bool) {
	spec := *row.Spec.DeepCopy()
	defaultBuiltinSpec(&spec)
	if row.Canary {
		spec.Delivery.Mode = agentv1alpha1.AgentScheduleDeliveryLocal
		if spec.Parameters == nil {
			spec.Parameters = map[string]string{}
		}
		spec.Parameters[builtinDryRunParameter] = "true"
	}
	o, overridden := overrideFor(agent, row.Name)
	switch {
	case row.RunNowOnly:
		spec.Suspend = true
	case overridden:
		spec.Suspend = o.Suspend
	case builtinSuspendAll(agent):
		// Ahead of a directly set value: the PlatformAgent says every built-in is off.
		spec.Suspend = true
	case existing != nil && directlySetSuspend(existing):
		// Someone set it directly since the operator last did: it stands.
		spec.Suspend = existing.Spec.Suspend
		return spec, nil
	default:
		spec.Suspend = row.Spec.Suspend
	}
	applied := spec.Suspend
	return spec, &applied
}

// defaultBuiltinSpec fills in what the AgentSchedule CRD defaults, so a
// schedule the API server has defaulted compares equal to its row and isn't
// written again on every pass.
func defaultBuiltinSpec(spec *agentv1alpha1.AgentScheduleSpec) {
	if spec.ConcurrencyPolicy == "" {
		spec.ConcurrencyPolicy = agentv1alpha1.AgentScheduleConcurrencyForbid
	}
	if spec.StartingDeadlineSeconds == nil {
		d := int64(agentScheduleDefaultStartingDeadline / time.Second)
		spec.StartingDeadlineSeconds = &d
	}
	if spec.Delivery.Mode == "" {
		spec.Delivery.Mode = agentv1alpha1.AgentScheduleDeliveryChat
	}
}

// directlySetSuspend is a built-in whose suspend someone set since the
// operator last did: the annotation records what the operator applied, and
// the schedule now says otherwise. A schedule without the annotation (one a
// GitOps tool stripped, say) isn't one: it follows its row and gets the
// annotation back.
func directlySetSuspend(s *agentv1alpha1.AgentSchedule) bool {
	applied, ok := s.Annotations[builtinAppliedSuspendAnnotation]
	return ok && applied != strconv.FormatBool(s.Spec.Suspend)
}

// builtinOverrideProblems is what the condition reports about the overrides: entries naming no
// built-in, and entries that would unsuspend a run-now-only one. With schedules off nothing
// renders, so no entry is reported as naming nothing. An entry naming a job in the image's cron
// roster (imageCronJobIDs) names something: the agent pod applies it.
func builtinOverrideProblems(agent *agentv1alpha1.PlatformAgent, rows []builtinSchedule, schedulesOn bool) (unknown, refused []string) {
	if agent.Spec.Schedules == nil {
		return nil, nil
	}
	byName := map[string]builtinSchedule{}
	for _, row := range rows {
		byName[row.Name] = row
	}
	for _, o := range agent.Spec.Schedules.Overrides {
		row, ok := byName[o.Name]
		switch {
		case !ok && imageCronJob(o.Name):
		case !ok && schedulesOn:
			unknown = append(unknown, o.Name)
		case ok && row.RunNowOnly && !o.Suspend:
			refused = append(refused, o.Name)
		}
	}
	return unknown, refused
}

// builtinSchedulesTerminatingRequeue is how soon a pass looks again at a replaced squatter whose
// deletion is held (a finalizer someone else owns): slower than builtinSchedulesPendingRequeue,
// which follows a deletion this pass issued.
const builtinSchedulesTerminatingRequeue = time.Minute

// reconcileBuiltinSchedules renders the built-in table into the PlatformAgent's namespace. Each
// row's schedule is created, or brought back to what the row says (labels, controller reference
// and spec) when it drifted. A schedule on a row's name that the operator didn't render is
// deleted, with an Event, and created again on a later pass once it is gone. A schedule the
// operator rendered whose row is gone (or every row, with schedules off) is deleted. Nothing
// else is touched. It does nothing when the CRD is missing or the role can't read schedules
// (schedulesUnreadable). It returns the BuiltinSchedules condition to write (nil: none), and how
// soon to look again (zero: no need).
func (r *PlatformAgentReconciler) reconcileBuiltinSchedules(ctx context.Context, agent *agentv1alpha1.PlatformAgent) (condition *metav1.Condition, requeue time.Duration, err error) {
	if r.schedulesUnreadable {
		return nil, 0, nil
	}
	log := logf.FromContext(ctx)
	rows := r.builtinScheduleRows(agent)
	want := map[string]builtinSchedule{}
	for _, row := range rows {
		want[row.Name] = row
	}
	soon := func(d time.Duration) {
		if requeue == 0 || d < requeue {
			requeue = d
		}
	}

	var list agentv1alpha1.AgentScheduleList
	if err := r.List(ctx, &list, client.InNamespace(agent.Namespace)); err != nil {
		return nil, 0, fmt.Errorf("list agent schedules: %w", err)
	}
	have := map[string]*agentv1alpha1.AgentSchedule{}
	var replaced, forbidden []string
	// refused records a write that was refused with a 403 (the role, an admission policy, a
	// quota), naming the verb and the API's reason, and skips it so the rest of the
	// PlatformAgent's reconcile goes on.
	refused := func(verb, name string, err error) bool {
		if !apierrors.IsForbidden(err) {
			return false
		}
		log.Info("a built-in schedule write was refused; skipped", "verb", verb, "schedule", name, "err", err)
		forbidden = append(forbidden, fmt.Sprintf("%s %s (%v)", verb, name, err))
		return true
	}
	for i := range list.Items {
		sched := &list.Items[i]
		row, reserved := want[sched.Name]
		owned := ownedBuiltinSchedule(sched, agent)
		switch {
		case reserved && !owned:
			// A squatter on a reserved name: replaced, never adopted.
			replaced = append(replaced, sched.Name)
			if sched.DeletionTimestamp != nil {
				// Already going; something else holds it.
				soon(builtinSchedulesTerminatingRequeue)
				continue
			}
			if err := r.Delete(ctx, sched); err != nil && !apierrors.IsNotFound(err) {
				if refused("delete", sched.Name, err) {
					continue
				}
				return nil, 0, fmt.Errorf("delete the unowned schedule on reserved name %s: %w", sched.Name, err)
			}
			log.Info("deleted a schedule on a reserved built-in name that the operator didn't render; it is created again as the built-in", "schedule", sched.Name)
			if r.Recorder != nil {
				r.Recorder.Eventf(agent, corev1.EventTypeWarning, builtinSchedulesReasonReplaced,
					"deleted AgentSchedule %s, which sat on a reserved built-in name without the operator's marks; it is created again as the built-in", sched.Name)
			}
			soon(builtinSchedulesPendingRequeue)
		case reserved:
			have[sched.Name] = sched
			if err := r.updateBuiltinSchedule(ctx, agent, row, sched); err != nil {
				switch {
				case apierrors.IsConflict(err):
					// The schedule reconciler wrote its status since the cache read.
					soon(builtinSchedulesPendingRequeue)
				case refused("update", sched.Name, err):
				default:
					return nil, 0, err
				}
			}
		case owned && sched.DeletionTimestamp == nil:
			// Rendered by the operator, and no longer in the table.
			if err := r.Delete(ctx, sched); err != nil && !apierrors.IsNotFound(err) {
				if refused("delete", sched.Name, err) {
					continue
				}
				return nil, 0, fmt.Errorf("delete the retired built-in schedule %s: %w", sched.Name, err)
			}
			log.Info("deleted a built-in schedule no longer rendered", "schedule", sched.Name)
		}
	}
	for _, row := range rows {
		if _, ok := have[row.Name]; ok || slices.Contains(replaced, row.Name) {
			continue
		}
		if err := r.createBuiltinSchedule(ctx, agent, row); err != nil {
			switch {
			case apierrors.IsAlreadyExists(err):
				soon(builtinSchedulesPendingRequeue)
			case refused("create", row.Name, err):
			default:
				return nil, 0, err
			}
		}
	}
	return builtinSchedulesCondition(agent, rows, replaced, forbidden, len(rows) > 0 || AgentSchedulesEnabled()), requeue, nil
}

func (r *PlatformAgentReconciler) createBuiltinSchedule(ctx context.Context, agent *agentv1alpha1.PlatformAgent, row builtinSchedule) error {
	sched := &agentv1alpha1.AgentSchedule{ObjectMeta: metav1.ObjectMeta{Name: row.Name, Namespace: agent.Namespace}}
	sched.Labels = map[string]string{agentv1alpha1.AgentScheduleOriginLabel: agentv1alpha1.AgentScheduleOriginBuiltin}
	spec, applied := desiredBuiltinSpec(agent, row, nil)
	sched.Spec = spec
	if applied != nil {
		sched.Annotations = map[string]string{builtinAppliedSuspendAnnotation: strconv.FormatBool(*applied)}
	}
	if err := controllerutil.SetControllerReference(agent, sched, r.Scheme); err != nil {
		return fmt.Errorf("controller reference on built-in schedule %s: %w", row.Name, err)
	}
	if err := r.Create(ctx, sched); err != nil {
		return fmt.Errorf("create built-in schedule %s: %w", row.Name, err)
	}
	logf.FromContext(ctx).Info("created a built-in schedule", "schedule", row.Name)
	return nil
}

// updateBuiltinSchedule brings an owned built-in back to its row. Annotations other than
// builtinAppliedSuspendAnnotation (run-now among them) and labels other than the origin label are
// left as they are.
func (r *PlatformAgentReconciler) updateBuiltinSchedule(ctx context.Context, agent *agentv1alpha1.PlatformAgent, row builtinSchedule, sched *agentv1alpha1.AgentSchedule) error {
	spec, applied := desiredBuiltinSpec(agent, row, sched)
	annotationCurrent := applied == nil || sched.Annotations[builtinAppliedSuspendAnnotation] == strconv.FormatBool(*applied)
	if equality.Semantic.DeepEqual(sched.Spec, spec) && annotationCurrent {
		return nil
	}
	sched.Spec = spec
	if applied != nil {
		if sched.Annotations == nil {
			sched.Annotations = map[string]string{}
		}
		sched.Annotations[builtinAppliedSuspendAnnotation] = strconv.FormatBool(*applied)
	}
	if err := r.Update(ctx, sched); err != nil {
		return fmt.Errorf("update built-in schedule %s: %w", row.Name, err)
	}
	logf.FromContext(ctx).Info("brought a built-in schedule back to its row", "schedule", row.Name)
	return nil
}

// builtinSchedulesCondition is the BuiltinSchedules condition while something needs saying, and
// nil otherwise. The reason is the most serious that holds: a refused write, a squatter
// replaced, an override refused, an override naming nothing. The message lists all of them.
func builtinSchedulesCondition(agent *agentv1alpha1.PlatformAgent, rows []builtinSchedule, replaced, forbidden []string, schedulesOn bool) *metav1.Condition {
	unknown, refused := builtinOverrideProblems(agent, rows, schedulesOn)
	var reason string
	var parts []string
	note := func(r, part string) {
		if reason == "" {
			reason = r
		}
		parts = append(parts, part)
	}
	if len(forbidden) > 0 {
		note(builtinSchedulesReasonForbidden, "a built-in schedule write was refused: "+strings.Join(forbidden, "; ")+". If RBAC refused it, apply the chart's RBAC: the writes are on the operator's namespaced Role in the install namespace")
	}
	if len(replaced) > 0 {
		note(builtinSchedulesReasonReplaced, "replacing the unowned schedule on reserved name(s) "+strings.Join(replaced, ", "))
	}
	if len(refused) > 0 {
		note(builtinSchedulesReasonOverrideRefused, "refused suspend: false for run-now-only schedule(s) "+strings.Join(refused, ", ")+", which would put them on their nominal cron")
	}
	if len(unknown) > 0 {
		note(builtinSchedulesReasonOverrideUnknown, "override(s) naming no built-in schedule: "+strings.Join(unknown, ", "))
	}
	if reason == "" {
		return nil
	}
	return &metav1.Condition{
		Type:               builtinSchedulesConditionType,
		Status:             metav1.ConditionFalse,
		Reason:             reason,
		Message:            strings.Join(parts, "; "),
		ObservedGeneration: agent.Generation,
	}
}

// builtinSchedulesConditionCurrent reports whether the agent already carries want (nil: none).
func builtinSchedulesConditionCurrent(agent *agentv1alpha1.PlatformAgent, want *metav1.Condition) bool {
	existing := meta.FindStatusCondition(agent.Status.Conditions, builtinSchedulesConditionType)
	if want == nil {
		return existing == nil
	}
	return existing != nil && existing.Status == want.Status && existing.Reason == want.Reason &&
		existing.Message == want.Message && existing.ObservedGeneration == want.ObservedGeneration
}

// setBuiltinSchedulesCondition writes want, or removes the condition for nil.
func setBuiltinSchedulesCondition(agent *agentv1alpha1.PlatformAgent, want *metav1.Condition, now metav1.Time) {
	if want == nil {
		meta.RemoveStatusCondition(&agent.Status.Conditions, builtinSchedulesConditionType)
		return
	}
	condition := *want
	condition.LastTransitionTime = now
	meta.SetStatusCondition(&agent.Status.Conditions, condition)
}

// builtinSchedulesPendingRequeue is how soon the pass after a replaced schedule's deletion comes.
const builtinSchedulesPendingRequeue = 5 * time.Second
