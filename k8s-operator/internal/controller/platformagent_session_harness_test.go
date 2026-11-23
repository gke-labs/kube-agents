package controller

import (
	"context"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/client-go/tools/record"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

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
		{"claude-code", &agentv1alpha1.SessionWorkerSpec{Type: "claude-code"}, "claude-code", true},
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
	if len(rec.Events) != 1 {
		t.Errorf("%d events over two passes, want one Warning", len(rec.Events))
	}
	if forGateway := a2aSessionHarness(agent); forGateway != "" {
		t.Errorf("the gateway is given %q, want nothing", forGateway)
	}

	for _, fixed := range []*agentv1alpha1.SessionWorkerSpec{{Type: "claude-code"}, nil} {
		agent.Spec.Harness.SessionWorker = &agentv1alpha1.SessionWorkerSpec{Type: "antigravity"}
		sync()
		agent.Spec.Harness.SessionWorker = fixed
		sync()
		if c := meta.FindStatusCondition(agent.Status.Conditions, sessionHarnessCondition); c != nil {
			t.Errorf("after %+v the condition stays: %+v", fixed, c)
		}
	}
}
