package controller

import (
	"context"
	"errors"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"

	authorizationv1 "k8s.io/api/authorization/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const builtinTestNS = "kubeagents-system"

func builtinTestAgent(overrides ...agentv1alpha1.AgentScheduleOverride) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: builtinTestNS, UID: "agent-uid", Generation: 3}}
	if len(overrides) > 0 {
		agent.Spec.Schedules = &agentv1alpha1.PlatformAgentSchedulesSpec{Overrides: overrides}
	}
	return agent
}

func builtinTestRow(name string, mutate func(*builtinSchedule)) builtinSchedule {
	row := builtinSchedule{Name: name, Spec: agentv1alpha1.AgentScheduleSpec{
		Schedule:   "0 6 * * *",
		ProfileRef: agentv1alpha1.AgentScheduleProfileRef{Name: "fleet-audit"},
		Prompt:     "Run " + name + ".",
		Delivery:   agentv1alpha1.AgentScheduleDelivery{Mode: "chat"},
	}}
	if mutate != nil {
		mutate(&row)
	}
	return row
}

type builtinRig struct {
	c     client.Client
	r     *PlatformAgentReconciler
	agent *agentv1alpha1.PlatformAgent
	rec   *record.FakeRecorder
}

func newBuiltinRig(t *testing.T, agent *agentv1alpha1.PlatformAgent, rows []builtinSchedule, objs ...client.Object) *builtinRig {
	t.Helper()
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(append([]client.Object{agent}, objs...)...).Build()
	rec := record.NewFakeRecorder(16)
	r := &PlatformAgentReconciler{Client: c, Scheme: c.Scheme(), Recorder: rec,
		BuiltinSchedules: func(*agentv1alpha1.PlatformAgent) []builtinSchedule { return rows }}
	return &builtinRig{c: c, r: r, agent: agent, rec: rec}
}

func (g *builtinRig) pass(t *testing.T) (*metav1.Condition, time.Duration) {
	t.Helper()
	cond, requeue, err := g.r.reconcileBuiltinSchedules(context.Background(), g.agent)
	if err != nil {
		t.Fatal(err)
	}
	return cond, requeue
}

func (g *builtinRig) get(t *testing.T, name string) (*agentv1alpha1.AgentSchedule, bool) {
	t.Helper()
	var s agentv1alpha1.AgentSchedule
	if err := g.c.Get(context.Background(), types.NamespacedName{Namespace: builtinTestNS, Name: name}, &s); err != nil {
		return nil, false
	}
	return &s, true
}

// userSchedule is a schedule the operator didn't render: no controller
// reference and no origin label.
func userSchedule(name string) *agentv1alpha1.AgentSchedule {
	return &agentv1alpha1.AgentSchedule{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: builtinTestNS},
		Spec: agentv1alpha1.AgentScheduleSpec{Schedule: "*/5 * * * *",
			ProfileRef: agentv1alpha1.AgentScheduleProfileRef{Name: "cluster"}, Prompt: "mine"},
	}
}

// A row is rendered with the operator's marks: controlled by the
// PlatformAgent, named by kind, and labelled builtin, so the schedule
// reconciler reads it as built-in.
func TestABuiltinRowIsRenderedWithTheOperatorsMarks(t *testing.T) {
	agent := builtinTestAgent()
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)})
	if cond, requeue := g.pass(t); cond != nil || requeue != 0 {
		t.Fatalf("condition = %+v, requeue = %v", cond, requeue)
	}
	s, ok := g.get(t, "compliance-audit")
	if !ok {
		t.Fatal("the built-in wasn't created")
	}
	if !ownedBuiltinSchedule(s, agent) || agentScheduleOrigin(s, agent, []string{"compliance-audit"}) != agentv1alpha1.AgentScheduleOriginBuiltin {
		t.Fatalf("the rendered schedule doesn't carry the operator's marks: %+v", s.ObjectMeta)
	}
	if s.Spec.Prompt != "Run compliance-audit." || s.Spec.Delivery.Mode != "chat" || s.Spec.Suspend {
		t.Fatalf("spec = %+v", s.Spec)
	}
}

// A canary row always delivers locally and carries the dry-run parameter,
// whatever its spec says.
func TestACanaryRowIsLocalAndDryRun(t *testing.T) {
	agent := builtinTestAgent()
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit"+builtinCanarySuffix, func(r *builtinSchedule) {
		r.Canary = true
		r.Spec.Parameters = map[string]string{"scope": "all"}
	})})
	g.pass(t)
	s, ok := g.get(t, "compliance-audit-canary")
	if !ok {
		t.Fatal("the canary wasn't created")
	}
	if s.Spec.Delivery.Mode != "local" || s.Spec.Parameters[builtinDryRunParameter] != "true" || s.Spec.Parameters["scope"] != "all" {
		t.Fatalf("canary spec = %+v", s.Spec)
	}
}

// Every canary row in the shipped table is named <name>-canary, and every
// row name is unique, so the reserved set means what the docs say.
func TestTheShippedTablesNamesAreWellFormed(t *testing.T) {
	seen := map[string]bool{}
	for _, row := range builtinSchedules(builtinTestAgent()) {
		if seen[row.Name] {
			t.Errorf("row %s appears twice", row.Name)
		}
		seen[row.Name] = true
		if row.Canary != strings.HasSuffix(row.Name, builtinCanarySuffix) {
			t.Errorf("row %s: Canary = %v, but its name says otherwise", row.Name, row.Canary)
		}
	}
}

// A schedule on an exact reserved name that the operator didn't render is
// deleted, reported, and created again as the built-in on the next pass:
// never adopted with its content kept.
func TestAnUnownedScheduleOnAReservedNameIsReplacedAndReported(t *testing.T) {
	agent := builtinTestAgent()
	squatter := userSchedule("compliance-audit")
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)}, squatter)
	cond, requeue := g.pass(t)
	if requeue != builtinSchedulesPendingRequeue || cond == nil || cond.Reason != builtinSchedulesReasonReplaced || !strings.Contains(cond.Message, "compliance-audit") {
		t.Fatalf("condition = %+v, requeue = %v", cond, requeue)
	}
	if _, ok := g.get(t, "compliance-audit"); ok {
		t.Fatal("the squatter is still there after the first pass")
	}
	select {
	case e := <-g.rec.Events:
		if !strings.Contains(e, builtinSchedulesReasonReplaced) || !strings.Contains(e, "compliance-audit") {
			t.Fatalf("event = %q", e)
		}
	default:
		t.Fatal("no Event for the replacement")
	}
	if cond, requeue := g.pass(t); cond != nil || requeue != 0 {
		t.Fatalf("second pass: condition = %+v, requeue = %v", cond, requeue)
	}
	s, ok := g.get(t, "compliance-audit")
	if !ok || !ownedBuiltinSchedule(s, agent) || s.Spec.Prompt != "Run compliance-audit." {
		t.Fatalf("the built-in wasn't created in the squatter's place: %+v", s)
	}
}

// A schedule whose name isn't in the built-in set is never touched, however
// close its name is to one, and whatever it carries.
func TestANearMissNameIsLeftAlone(t *testing.T) {
	agent := builtinTestAgent()
	near := []*agentv1alpha1.AgentSchedule{
		userSchedule("compliance-audit-2"),
		userSchedule("compliance-audit-canary"), // the canary of a row that isn't in this table
		userSchedule("Compliance-audit"),
	}
	objs := []client.Object{}
	for _, s := range near {
		objs = append(objs, s)
	}
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)}, objs...)
	g.pass(t)
	for _, want := range near {
		got, ok := g.get(t, want.Name)
		if !ok || got.Spec.Prompt != "mine" || len(got.OwnerReferences) != 0 {
			t.Errorf("%s was touched: %+v", want.Name, got)
		}
	}
}

// A schedule the operator rendered whose row is gone is deleted, and with
// schedules off every rendered one goes. A user's schedule stays.
func TestARetiredBuiltinIsDeletedAndUserSchedulesStay(t *testing.T) {
	agent := builtinTestAgent()
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)}, userSchedule("nightly-mine"))
	g.pass(t)
	if _, ok := g.get(t, "compliance-audit"); !ok {
		t.Fatal("the built-in wasn't created")
	}
	g.r.BuiltinSchedules = func(*agentv1alpha1.PlatformAgent) []builtinSchedule { return nil }
	g.pass(t)
	if _, ok := g.get(t, "compliance-audit"); ok {
		t.Fatal("a built-in whose row is gone is still there")
	}
	if _, ok := g.get(t, "nightly-mine"); !ok {
		t.Fatal("a user's schedule was deleted")
	}
}

// An edit to a built-in's spec is put back. A suspend someone set on it
// stands while no override names it; an override decides it when one does.
func TestABuiltinsSpecIsPutBackAndItsSuspendFollowsTheOverride(t *testing.T) {
	agent := builtinTestAgent()
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)})
	g.pass(t)
	s, _ := g.get(t, "compliance-audit")
	s.Spec.Prompt = "something else"
	s.Spec.Suspend = true
	if err := g.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	g.pass(t)
	if s, _ = g.get(t, "compliance-audit"); s.Spec.Prompt != "Run compliance-audit." || !s.Spec.Suspend {
		t.Fatalf("after an edit: spec = %+v; want the prompt put back and the suspend kept", s.Spec)
	}
	agent.Spec.Schedules = &agentv1alpha1.PlatformAgentSchedulesSpec{Overrides: []agentv1alpha1.AgentScheduleOverride{{Name: "compliance-audit", Suspend: false}}}
	g.pass(t)
	if s, _ = g.get(t, "compliance-audit"); s.Spec.Suspend {
		t.Fatal("an override of suspend: false didn't unsuspend a scheduled built-in")
	}
}

// suspendAll suspends every row no override names, a directly unsuspended one included; an
// override of suspend: false turns one back on; and clearing suspendAll returns a row to its
// default.
func TestSuspendAllSuspendsEveryRowButAnOverriddenOne(t *testing.T) {
	agent := builtinTestAgent(agentv1alpha1.AgentScheduleOverride{Name: "stockout-prevention", Suspend: false})
	rows := []builtinSchedule{builtinTestRow("compliance-audit", nil), builtinTestRow("stockout-prevention", nil)}
	g := newBuiltinRig(t, agent, rows)
	g.pass(t)
	agent.Spec.Schedules.SuspendAll = true
	if cond, _ := g.pass(t); cond != nil {
		t.Fatalf("condition = %+v", cond)
	}
	if s, _ := g.get(t, "compliance-audit"); !s.Spec.Suspend {
		t.Fatal("suspendAll left a row with no override running")
	}
	if s, _ := g.get(t, "stockout-prevention"); s.Spec.Suspend {
		t.Fatal("suspendAll suspended a row whose override says suspend: false")
	}
	s, _ := g.get(t, "compliance-audit")
	s.Spec.Suspend = false
	if err := g.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	g.pass(t)
	if s, _ = g.get(t, "compliance-audit"); !s.Spec.Suspend {
		t.Fatal("a suspend set directly on the schedule outranked suspendAll")
	}
	agent.Spec.Schedules.SuspendAll = false
	g.pass(t)
	if s, _ = g.get(t, "compliance-audit"); s.Spec.Suspend {
		t.Fatal("clearing suspendAll left the row suspended")
	}
}

// suspendAll leaves a run-now-only row to its override: it doesn't stop the run-now.
func TestSuspendAllDoesNotStopARunNowOnlyRow(t *testing.T) {
	row := builtinTestRow("onboarding", func(r *builtinSchedule) { r.RunNowOnly = true })
	agent := builtinTestAgent()
	agent.Spec.Schedules = &agentv1alpha1.PlatformAgentSchedulesSpec{SuspendAll: true}
	if builtinRunNowSuspended(agent, row) {
		t.Fatal("suspendAll stopped a run-now-only row's run-now")
	}
}

// An override naming a job in the image's cron roster names something, so it isn't reported;
// one naming neither a row nor a roster job still is.
func TestAnOverrideNamingAnImageCronJobIsNotReported(t *testing.T) {
	agent := builtinTestAgent(
		agentv1alpha1.AgentScheduleOverride{Name: "stall-watch", Suspend: true},
		agentv1alpha1.AgentScheduleOverride{Name: "stall-wach", Suspend: true},
	)
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)})
	cond, _ := g.pass(t)
	if cond == nil || cond.Reason != builtinSchedulesReasonOverrideUnknown || strings.Contains(cond.Message, "stall-watch") || !strings.Contains(cond.Message, "stall-wach") {
		t.Fatalf("condition = %+v", cond)
	}
}

// A run-now-only row renders suspended whatever an override says: suspend:
// true is taken (and stops the operator setting its run-now), suspend: false
// is refused and reported, since it would put the row on its nominal cron.
func TestARunNowOnlyRowTakesOnlyASuspendingOverride(t *testing.T) {
	row := builtinTestRow("onboarding", func(r *builtinSchedule) { r.RunNowOnly = true })
	agent := builtinTestAgent(agentv1alpha1.AgentScheduleOverride{Name: "onboarding", Suspend: false})
	g := newBuiltinRig(t, agent, []builtinSchedule{row})
	cond, _ := g.pass(t)
	if cond == nil || cond.Reason != builtinSchedulesReasonOverrideRefused || !strings.Contains(cond.Message, "onboarding") {
		t.Fatalf("condition = %+v", cond)
	}
	if s, ok := g.get(t, "onboarding"); !ok || !s.Spec.Suspend {
		t.Fatalf("a run-now-only row was rendered unsuspended: %+v", s)
	}
	if builtinRunNowSuspended(agent, row) {
		t.Fatal("a refused override stopped the run-now")
	}
	agent.Spec.Schedules.Overrides[0].Suspend = true
	if cond, _ := g.pass(t); cond != nil {
		t.Fatalf("an accepted override still reported: %+v", cond)
	}
	if !builtinRunNowSuspended(agent, row) {
		t.Fatal("suspend: true on a run-now-only row didn't stop its run-now")
	}
}

// An override naming no built-in is reported and changes nothing. Overrides
// key on the rendered name: the canary's entry doesn't reach the live row.
func TestAnOverrideNamingNoBuiltinIsReported(t *testing.T) {
	agent := builtinTestAgent(agentv1alpha1.AgentScheduleOverride{Name: "compliance-audit-canary", Suspend: true})
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)})
	cond, _ := g.pass(t)
	if cond == nil || cond.Reason != builtinSchedulesReasonOverrideUnknown || !strings.Contains(cond.Message, "compliance-audit-canary") {
		t.Fatalf("condition = %+v", cond)
	}
	if s, _ := g.get(t, "compliance-audit"); s.Spec.Suspend {
		t.Fatal("the canary's override reached the live row")
	}
}

// The schedule reconciler's built-in set is the table's names, so nothing
// outside the table reads as built-in.
func TestTheBuiltinNameSetIsTheTables(t *testing.T) {
	t.Setenv(AgentSchedulesEnvVar, "true")
	got := agentScheduleBuiltinNames(builtinTestAgent())
	want := builtinScheduleNames(builtinSchedules(builtinTestAgent()))
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("names = %v, want %v", got, want)
	}
	t.Setenv(AgentSchedulesEnvVar, "")
	if got := agentScheduleBuiltinNames(builtinTestAgent()); len(got) != 0 {
		t.Fatalf("names with schedules off = %v", got)
	}
}

// A role that refuses the renderer's writes, as an operator image ahead of
// its RBAC would have, is reported on the condition and doesn't fail
// the PlatformAgent's reconcile.
func TestARefusedBuiltinWriteIsReportedNotFatal(t *testing.T) {
	agent := builtinTestAgent()
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			if _, ok := obj.(*agentv1alpha1.AgentSchedule); ok {
				return apierrors.NewForbidden(schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "agentschedules"}, obj.GetName(), errors.New("no create"))
			}
			return cl.Create(ctx, obj, opts...)
		}}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: c.Scheme(),
		BuiltinSchedules: func(*agentv1alpha1.PlatformAgent) []builtinSchedule {
			return []builtinSchedule{builtinTestRow("compliance-audit", nil)}
		}}
	cond, _, err := r.reconcileBuiltinSchedules(context.Background(), agent)
	if err != nil {
		t.Fatalf("a refused write failed the reconcile: %v", err)
	}
	if cond == nil || cond.Reason != builtinSchedulesReasonForbidden || !strings.Contains(cond.Message, "create compliance-audit") ||
		!strings.Contains(cond.Message, "no create") {
		t.Fatalf("condition = %+v", cond)
	}
}

// Removing an override resumes what the row says, and a release changing a
// row's default reaches an existing schedule, since the operator records the
// value it applied. A suspend someone set directly still stands.
func TestASuspendTheOperatorAppliedFollowsTheRowOnceTheOverrideGoes(t *testing.T) {
	row := builtinTestRow("compliance-audit", nil)
	agent := builtinTestAgent(agentv1alpha1.AgentScheduleOverride{Name: "compliance-audit", Suspend: true})
	g := newBuiltinRig(t, agent, nil)
	g.r.BuiltinSchedules = func(*agentv1alpha1.PlatformAgent) []builtinSchedule { return []builtinSchedule{row} }
	g.pass(t)
	if s, _ := g.get(t, "compliance-audit"); !s.Spec.Suspend {
		t.Fatal("the override's suspend wasn't applied")
	}
	agent.Spec.Schedules = nil
	g.pass(t)
	if s, _ := g.get(t, "compliance-audit"); s.Spec.Suspend {
		t.Fatal("removing the override didn't resume the schedule")
	}
	// A release flips the row's default.
	row.Spec.Suspend = true
	g.pass(t)
	if s, _ := g.get(t, "compliance-audit"); !s.Spec.Suspend {
		t.Fatal("a new row default didn't reach the existing schedule")
	}
	// Someone unsuspends it directly: that stands while no override names it.
	s, _ := g.get(t, "compliance-audit")
	s.Spec.Suspend = false
	if err := g.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	g.pass(t)
	if s, _ := g.get(t, "compliance-audit"); s.Spec.Suspend {
		t.Fatal("a suspend set directly was overwritten with no override")
	}
}

// A squatter whose deletion something else holds isn't deleted again, and
// the next look comes at the slower interval.
func TestAHeldSquatterIsNotHammered(t *testing.T) {
	agent := builtinTestAgent()
	squatter := userSchedule("compliance-audit")
	now := metav1.Now()
	squatter.DeletionTimestamp = &now
	squatter.Finalizers = []string{"example.com/hold"}
	deletes := 0
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, squatter).
		WithInterceptorFuncs(interceptor.Funcs{Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
			deletes++
			return cl.Delete(ctx, obj, opts...)
		}}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: c.Scheme(),
		BuiltinSchedules: func(*agentv1alpha1.PlatformAgent) []builtinSchedule {
			return []builtinSchedule{builtinTestRow("compliance-audit", nil)}
		}}
	cond, requeue, err := r.reconcileBuiltinSchedules(context.Background(), agent)
	if err != nil || deletes != 0 || requeue != builtinSchedulesTerminatingRequeue || cond == nil || cond.Reason != builtinSchedulesReasonReplaced {
		t.Fatalf("err = %v, deletes = %d, requeue = %v, condition = %+v", err, deletes, requeue, cond)
	}
}

// A refused delete of a squatter creates nothing in its place, and a
// conflicting update (the schedule reconciler wrote status since the read)
// asks for another pass rather than failing this one.
func TestARefusedSquatterDeleteCreatesNothingAndAConflictRequeues(t *testing.T) {
	agent := builtinTestAgent()
	creates := 0
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, userSchedule("compliance-audit")).
		WithInterceptorFuncs(interceptor.Funcs{
			Delete: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
				return apierrors.NewForbidden(schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "agentschedules"}, obj.GetName(), errors.New("no delete"))
			},
			Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				creates++
				return cl.Create(ctx, obj, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: c.Scheme(),
		BuiltinSchedules: func(*agentv1alpha1.PlatformAgent) []builtinSchedule {
			return []builtinSchedule{builtinTestRow("compliance-audit", nil)}
		}}
	cond, _, err := r.reconcileBuiltinSchedules(context.Background(), agent)
	if err != nil || creates != 0 || cond == nil || cond.Reason != builtinSchedulesReasonForbidden || !strings.Contains(cond.Message, "delete compliance-audit") {
		t.Fatalf("err = %v, creates = %d, condition = %+v", err, creates, cond)
	}

	owned := builtinTestAgent()
	g := newBuiltinRig(t, owned, []builtinSchedule{builtinTestRow("compliance-audit", nil)})
	g.pass(t)
	conflicting := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(owned).
		WithInterceptorFuncs(interceptor.Funcs{Update: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
			return apierrors.NewConflict(schema.GroupResource{Group: "kubeagents.x-k8s.io", Resource: "agentschedules"}, obj.GetName(), errors.New("stale"))
		}}).Build()
	s, _ := g.get(t, "compliance-audit")
	s.ResourceVersion = ""
	s.Spec.Prompt = "drifted"
	if err := conflicting.Create(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	r2 := &PlatformAgentReconciler{Client: conflicting, Scheme: conflicting.Scheme(), BuiltinSchedules: g.r.BuiltinSchedules}
	if _, requeue, err := r2.reconcileBuiltinSchedules(context.Background(), owned); err != nil || requeue != builtinSchedulesPendingRequeue {
		t.Fatalf("a conflicting update: err = %v, requeue = %v", err, requeue)
	}
}

// With the CRD missing or the role unable to read schedules, the renderer
// reads and writes nothing.
func TestAnUnreadableScheduleKindRendersNothing(t *testing.T) {
	agent := builtinTestAgent()
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{List: func(ctx context.Context, cl client.WithWatch, list client.ObjectList, opts ...client.ListOption) error {
			t.Fatal("the renderer listed schedules it can't read")
			return nil
		}}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: c.Scheme(), schedulesUnreadable: true,
		BuiltinSchedules: func(*agentv1alpha1.PlatformAgent) []builtinSchedule {
			return []builtinSchedule{builtinTestRow("compliance-audit", nil)}
		}}
	if cond, requeue, err := r.reconcileBuiltinSchedules(context.Background(), agent); err != nil || cond != nil || requeue != 0 {
		t.Fatalf("cond = %+v, requeue = %v, err = %v", cond, requeue, err)
	}
}

// With schedules off nothing renders, so an override isn't reported as
// naming nothing.
func TestOverridesAreNotReportedWhileSchedulesAreOff(t *testing.T) {
	t.Setenv(AgentSchedulesEnvVar, "")
	agent := builtinTestAgent(agentv1alpha1.AgentScheduleOverride{Name: "onboarding", Suspend: true})
	g := newBuiltinRig(t, agent, nil)
	if cond, _ := g.pass(t); cond != nil {
		t.Fatalf("condition with schedules off = %+v", cond)
	}
}

// The schedule controller's denials count reads and the status write only:
// a role with no create, update or delete on agentschedules (an image ahead
// of its RBAC) keeps the controller running, and one without list
// doesn't.
func TestAgentScheduleAccessDeniedIgnoresTheRenderersWrites(t *testing.T) {
	writes := NewRBACChecker((&fakeAuthorizer{deny: func(a *authorizationv1.ResourceAttributes) bool {
		return a.Resource == "agentschedules" && a.Subresource == "" && slices.Contains(agentScheduleWriteVerbs, a.Verb)
	}}).reviews(), "")
	if _, err := writes.Probe(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := AgentScheduleAccessDenied(writes); len(got) != 0 {
		t.Fatalf("write denials disable the schedule controller: %v", got)
	}
	reads := NewRBACChecker((&fakeAuthorizer{deny: func(a *authorizationv1.ResourceAttributes) bool {
		return a.Resource == "agentschedules" && a.Verb == "list"
	}}).reviews(), "")
	if _, err := reads.Probe(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := AgentScheduleAccessDenied(reads); len(got) != 1 {
		t.Fatalf("a list denial = %v, want it", got)
	}
}

// The dry-run parameter is a name the gateway accepts: a task parameter's
// name is a2a/lib's TaskParameterNameRE, ^[a-z][a-z0-9_]{0,62}$, and an
// undeclared or ill-formed one refuses the run. (dryRun, the first spelling,
// would have refused every canary run.)
func TestTheDryRunParameterIsANameTheGatewayAccepts(t *testing.T) {
	if !regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`).MatchString(builtinDryRunParameter) {
		t.Fatalf("%q isn't a task parameter name the gateway accepts", builtinDryRunParameter)
	}
}

// An older Role without the renderer's writes doesn't degrade the agent: the
// renderer reports the refusal as WritesForbidden, and the rest of the
// reconcile runs. Other namespaced denials still degrade it.
func TestScheduleWriteDenialsDoNotDegradeTheAgent(t *testing.T) {
	agent := &agentv1alpha1.PlatformAgent{ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "team-a"}}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	writes := NewRBACChecker((&fakeAuthorizer{deny: func(a *authorizationv1.ResourceAttributes) bool {
		return a.Resource == "agentschedules" && a.Subresource == "" && slices.Contains(agentScheduleWriteVerbs, a.Verb)
	}}).reviews(), builtinTestNS)
	if _, err := writes.Probe(context.Background()); err != nil {
		t.Fatal(err)
	}
	r := &PlatformAgentReconciler{Client: cl, Scheme: cl.Scheme(), RBAC: writes}
	degraded, err := r.reportRBACSkew(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if degraded {
		t.Fatalf("schedule write denials degraded the agent: %v", agent.Status.Conditions)
	}
	others := NewRBACChecker((&fakeAuthorizer{deny: func(a *authorizationv1.ResourceAttributes) bool {
		return a.Resource == "configmaps" && a.Verb == "create"
	}}).reviews(), builtinTestNS)
	if _, err := others.Probe(context.Background()); err != nil {
		t.Fatal(err)
	}
	r.RBAC = others
	if degraded, err = r.reportRBACSkew(context.Background(), agent); err != nil || !degraded {
		t.Fatalf("a configmaps create denial: degraded=%v err=%v, want degraded", degraded, err)
	}
}

// Once the API server has applied the CRD's defaults, a schedule that matches
// its row isn't written again: the comparison is against the defaulted row.
func TestADefaultedBuiltinIsNotRewritten(t *testing.T) {
	agent := builtinTestAgent()
	row := builtinTestRow("compliance-audit", func(r *builtinSchedule) { r.Spec.Delivery = agentv1alpha1.AgentScheduleDelivery{} })
	updates := 0
	c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			Create: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
				if s, ok := obj.(*agentv1alpha1.AgentSchedule); ok {
					// What the API server does with the CRD's defaults.
					if s.Spec.ConcurrencyPolicy == "" {
						s.Spec.ConcurrencyPolicy = agentv1alpha1.AgentScheduleConcurrencyForbid
					}
					if s.Spec.StartingDeadlineSeconds == nil {
						d := int64(900)
						s.Spec.StartingDeadlineSeconds = &d
					}
					if s.Spec.Delivery.Mode == "" {
						s.Spec.Delivery.Mode = agentv1alpha1.AgentScheduleDeliveryChat
					}
				}
				return cl.Create(ctx, obj, opts...)
			},
			Update: func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.UpdateOption) error {
				if _, ok := obj.(*agentv1alpha1.AgentSchedule); ok {
					updates++
				}
				return cl.Update(ctx, obj, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: c.Scheme(),
		BuiltinSchedules: func(*agentv1alpha1.PlatformAgent) []builtinSchedule { return []builtinSchedule{row} }}
	for range 3 {
		if _, _, err := r.reconcileBuiltinSchedules(context.Background(), agent); err != nil {
			t.Fatal(err)
		}
	}
	if updates != 0 {
		t.Fatalf("a defaulted schedule matching its row was rewritten %d times", updates)
	}
}

// A built-in missing the applied-suspend annotation (stripped by a GitOps
// tool, say) follows its row and gets the annotation back; it isn't read as
// a suspend someone set directly.
func TestABuiltinWithoutTheAppliedAnnotationFollowsItsRow(t *testing.T) {
	agent := builtinTestAgent()
	g := newBuiltinRig(t, agent, []builtinSchedule{builtinTestRow("compliance-audit", nil)})
	g.pass(t)
	s, _ := g.get(t, "compliance-audit")
	delete(s.Annotations, builtinAppliedSuspendAnnotation)
	s.Spec.Suspend = true
	if err := g.c.Update(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	g.pass(t)
	s, _ = g.get(t, "compliance-audit")
	if s.Spec.Suspend || s.Annotations[builtinAppliedSuspendAnnotation] != "false" {
		t.Fatalf("suspend=%v annotations=%v; want the row's suspend and the annotation back", s.Spec.Suspend, s.Annotations)
	}
}
