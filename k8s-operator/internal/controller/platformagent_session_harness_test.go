package controller

import (
	"context"
	"strings"
	"testing"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestTheSessionHarnessReachesTheGatewayOnlyWhenSet:
// spec.harness.sessionWorker.type goes to the gateway as A2A_SESSION_HARNESS
// when set; an install that leaves it out renders no such variable, so its
// gateway Deployment is the one it had and an upgrade doesn't roll it.
func TestTheSessionHarnessReachesTheGatewayOnlyWhenSet(t *testing.T) {
	for _, c := range []struct {
		name   string
		worker *agentv1alpha1.SessionWorkerSpec
		want   string
		set    bool
	}{
		{"no sessionWorker", nil, "", false},
		{"sessionWorker without type", &agentv1alpha1.SessionWorkerSpec{}, "", false},
		{"core-agent", &agentv1alpha1.SessionWorkerSpec{Type: "core-agent"}, "core-agent", true},
		// claude-code stays in the enum so an existing object still
		// validates, but the worker image no longer carries it: it is not
		// rendered, and session pods run the default harness.
		{"claude-code", &agentv1alpha1.SessionWorkerSpec{Type: "claude-code"}, "", false},
		// A value a newer CRD admitted (after a rollback, say) is not
		// rendered: the shared gateway keeps serving on the default harness.
		{"unsupported", &agentv1alpha1.SessionWorkerSpec{Type: "antigravity"}, "", false},
	} {
		t.Run(c.name, func(t *testing.T) {
			agent := a2aTestAgent()
			if agent.Spec.Harness == nil {
				agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
			}
			agent.Spec.Harness.SessionWorker = c.worker
			gw := buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0]
			var got string
			var set bool
			for _, e := range gw.Env {
				if e.Name == a2aSessionHarnessEnvVar {
					got, set = e.Value, true
				}
			}
			if set != c.set || got != c.want {
				t.Errorf("A2A_SESSION_HARNESS set=%v %q, want set=%v %q", set, got, c.set, c.want)
			}
		})
	}
}

// TestAnUnsupportedSessionHarnessIsReportedAndClears: a session harness the
// operator doesn't render (the release after the enum widens set
// antigravity, then the chart rolled back, which leaves CRDs as they were)
// writes SessionHarness False with SessionHarnessNotSupported and one
// Warning event, not one per pass. The condition is removed once the value
// is supported or unset.
func TestAnUnsupportedSessionHarnessIsReportedAndClears(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.SessionWorker = &agentv1alpha1.SessionWorkerSpec{Type: "antigravity"}
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	rec := record.NewFakeRecorder(16)
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: rec}
	ctx := context.Background()
	sync := func() {
		t.Helper()
		if err := r.syncSessionHarness(ctx, agent); err != nil {
			t.Fatal(err)
		}
	}

	sync()
	sync()
	c := meta.FindStatusCondition(agent.Status.Conditions, sessionHarnessCondition)
	if c == nil || c.Status != "False" || c.Reason != sessionHarnessReasonNotSupported {
		t.Fatalf("condition %+v, want False %s", c, sessionHarnessReasonNotSupported)
	}
	if !strings.Contains(c.Message, "the default harness, core-agent") {
		t.Errorf("message %q doesn't name the default harness, core-agent", c.Message)
	}
	if len(rec.Events) != 1 {
		t.Errorf("%d events over two passes, want one Warning", len(rec.Events))
	}
	if forGateway := a2aSessionHarness(agent); forGateway != "" {
		t.Errorf("the gateway is given %q, want nothing", forGateway)
	}

	for _, fixed := range []*agentv1alpha1.SessionWorkerSpec{{Type: "core-agent"}, nil} {
		agent.Spec.Harness.SessionWorker = &agentv1alpha1.SessionWorkerSpec{Type: "antigravity"}
		sync()
		agent.Spec.Harness.SessionWorker = fixed
		sync()
		if c := meta.FindStatusCondition(agent.Status.Conditions, sessionHarnessCondition); c != nil {
			t.Errorf("after %+v the condition stays: %+v", fixed, c)
		}
	}
}

// TestASessionHarnessWarningIsRecordedOnceEvenWhenItsWriteConflicts: the
// Warning event goes out only once the condition is stored, so a status
// write that conflicts (another writer in the same pass) doesn't repeat it
// on the next pass. Seen live: two events before this.
func TestASessionHarnessWarningIsRecordedOnceEvenWhenItsWriteConflicts(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.SessionWorker = &agentv1alpha1.SessionWorkerSpec{Type: "antigravity"}
	conflicts := 1
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{SubResourceUpdate: func(ctx context.Context, c client.Client, sub string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
			if conflicts > 0 {
				conflicts--
				return apierrors.NewConflict(schema.GroupResource{Resource: "platformagents"}, obj.GetName(), nil)
			}
			return c.SubResource(sub).Update(ctx, obj, opts...)
		}}).Build()
	rec := record.NewFakeRecorder(16)
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: rec}
	ctx := context.Background()
	if err := r.syncSessionHarness(ctx, agent); err == nil {
		t.Fatal("the conflicting write reported no error")
	}
	// The next pass reads the object as stored: without the condition.
	meta.RemoveStatusCondition(&agent.Status.Conditions, sessionHarnessCondition)
	if err := r.syncSessionHarness(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if len(rec.Events) != 1 {
		t.Errorf("%d Warning events, want one", len(rec.Events))
	}
}

// TestTheReconcileWritesTheSessionHarnessConditionOnce: through Reconcile,
// not the sync function alone, an unsupported session harness writes the
// SessionHarness condition and one Warning event over several passes, also
// after the generation moves while the value stays unsupported (only the
// first-write guard keeps that to one event), and a supported value removes it.
func TestTheReconcileWritesTheSessionHarnessConditionOnce(t *testing.T) {
	agent := a2aTestAgent()
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.SessionWorker = &agentv1alpha1.SessionWorkerSpec{Type: "antigravity"}
	r, cl, req := a2aGateTestReconciler(t, agent)
	rec := record.NewFakeRecorder(64)
	r.Recorder = rec
	ctx := context.Background()
	reconcileTwice(t, ctx, r, req)

	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	c := meta.FindStatusCondition(got.Status.Conditions, sessionHarnessCondition)
	if c == nil || c.Reason != sessionHarnessReasonNotSupported {
		t.Fatalf("after Reconcile the condition is %+v, want %s", c, sessionHarnessReasonNotSupported)
	}
	// The generation moves (another spec edit, which the fake client
	// doesn't bump on its own) while the value stays unsupported.
	got.Generation++
	if err := cl.Update(ctx, got); err != nil {
		t.Fatal(err)
	}
	reconcileTwice(t, ctx, r, req)
	warnings := 0
	for _, e := range drainEvents(rec) {
		if strings.Contains(e, sessionHarnessReasonNotSupported) {
			warnings++
		}
	}
	if warnings != 1 {
		t.Errorf("%d %s warnings over four passes and a generation change, want one", warnings, sessionHarnessReasonNotSupported)
	}

	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	got.Spec.Harness.SessionWorker = &agentv1alpha1.SessionWorkerSpec{Type: "core-agent"}
	if err := cl.Update(ctx, got); err != nil {
		t.Fatal(err)
	}
	reconcileTwice(t, ctx, r, req)
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	if c := meta.FindStatusCondition(got.Status.Conditions, sessionHarnessCondition); c != nil {
		t.Errorf("core-agent left the condition: %+v", c)
	}
}
