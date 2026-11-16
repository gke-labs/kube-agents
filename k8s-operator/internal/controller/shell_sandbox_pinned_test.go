package controller

import (
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// withSandboxImage pins spec.harness.experimental.shellSandbox.image on agent,
// keeping whatever harness fields it already carries.
func withSandboxImage(agent *agentv1alpha1.PlatformAgent, image string) *agentv1alpha1.PlatformAgent {
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	if agent.Spec.Harness.Experimental == nil {
		agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{}
	}
	agent.Spec.Harness.Experimental.ShellSandbox = &agentv1alpha1.ShellSandboxSpec{Image: image}
	return agent
}

// TestShellSandboxImagePinnedMessageFollowsThePin: the condition is wanted only
// when the CR pins an image other than the one the operator runs, and its
// message names both so the reader can tell which build is stale (#4966).
func TestShellSandboxImagePinnedMessageFollowsThePin(t *testing.T) {
	const own = "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.9.0"
	const old = "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.8.0"
	t.Setenv(shellSandboxImageEnvVar, own)

	if got := shellSandboxImagePinnedMsg(shellSandboxTestAgent()); got != "" {
		t.Errorf("no pin, message = %q, want none", got)
	}
	if got := shellSandboxImagePinnedMsg(withSandboxImage(shellSandboxTestAgent(), own)); got != "" {
		t.Errorf("pin equal to the operator's image, message = %q, want none", got)
	}
	got := shellSandboxImagePinnedMsg(withSandboxImage(shellSandboxTestAgent(), old))
	for _, want := range []string{old, own, "spec.harness.experimental.shellSandbox.image"} {
		if !strings.Contains(got, want) {
			t.Errorf("message does not name %q: %q", want, got)
		}
	}

	// With AGENT_SANDBOX_IMAGE unset the operator's image is the derived one,
	// and a pin to that is no skew either.
	t.Setenv(shellSandboxImageEnvVar, "")
	t.Setenv(operatorImageEnvVar, "mirror.corp.internal:5000/kube-agents/k8s-operator:0.2.0")
	derived := "mirror.corp.internal:5000/kube-agents/agent-sandbox:0.2.0"
	if got := shellSandboxImagePinnedMsg(withSandboxImage(shellSandboxTestAgent(), derived)); got != "" {
		t.Errorf("pin equal to the derived image, message = %q, want none", got)
	}
	if got := shellSandboxImagePinnedMsg(withSandboxImage(shellSandboxTestAgent(), old)); !strings.Contains(got, derived) {
		t.Errorf("message does not name the derived operator image %q: %q", derived, got)
	}
}

// TestShellSandboxImagePinnedConditionSetsAndClears: written True with the
// reason and message, read back as current, and removed once the message is "".
func TestShellSandboxImagePinnedConditionSetsAndClears(t *testing.T) {
	t.Setenv(shellSandboxImageEnvVar, "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.9.0")
	a := withSandboxImage(shellSandboxTestAgent(), "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.8.0")
	msg := shellSandboxImagePinnedMsg(a)
	if shellSandboxImagePinnedConditionCurrent(a, msg) {
		t.Fatal("a CR with no condition reads as current for a non-empty message")
	}
	setShellSandboxImagePinnedCondition(a, msg, metav1.Now())
	cond := meta.FindStatusCondition(a.Status.Conditions, shellSandboxImagePinnedConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != shellSandboxImagePinnedReason || cond.Message != msg {
		t.Fatalf("condition = %+v, want True/%s with the message", cond, shellSandboxImagePinnedReason)
	}
	if !shellSandboxImagePinnedConditionCurrent(a, msg) {
		t.Error("the condition just written does not read as current: every pass would write status")
	}
	setShellSandboxImagePinnedCondition(a, "", metav1.Now())
	if meta.FindStatusCondition(a.Status.Conditions, shellSandboxImagePinnedConditionType) != nil {
		t.Error("the condition survived an empty message")
	}
}

// TestPlatformAgentReconciler_Reconcile_ShellSandboxImagePinned goes through
// both status writers: the Ready one, and syncA2AConditions on a pass parked
// Degraded for want of the sandbox keys, which is where an install whose old
// sandbox broke its cluster questions is likely to be looked at. Clearing the
// pin clears the condition on each.
func TestPlatformAgentReconciler_Reconcile_ShellSandboxImagePinned(t *testing.T) {
	t.Setenv(shellSandboxImageEnvVar, "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.9.0")
	for _, tc := range []struct {
		name      string
		withKeys  bool
		wantPhase string
	}{
		{"ready writer", true, ""},
		{"degraded park", false, "Degraded"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scheme := setupScheme()
			agent := a2aTestAgent()
			agent.Name = "test-agent-sandbox-pinned"
			agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
				ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster",
			}
			withSandboxImage(agent, "ghcr.io/gke-labs/kube-agents/agent-sandbox:v0.8.0")
			objects := []client.Object{agent}
			if tc.withKeys {
				objects = append(objects, shellSandboxKeysSecret(agent))
			}
			cl := fake.NewClientBuilder().
				WithScheme(scheme).
				WithObjects(objects...).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
			req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
			ctx := context.Background()
			reconciled := func() *agentv1alpha1.PlatformAgent {
				t.Helper()
				for i := 1; i <= 2; i++ {
					_, _ = r.Reconcile(ctx, req) // a park may return an error; the status is what is checked
				}
				got := &agentv1alpha1.PlatformAgent{}
				if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
					t.Fatalf("get: %v", err)
				}
				if tc.wantPhase == "" && string(got.Status.Phase) == "Degraded" {
					t.Fatalf("the ready-writer fixture parked Degraded (conditions %+v); it no longer covers updateStatusReady", got.Status.Conditions)
				}
				if tc.wantPhase != "" && string(got.Status.Phase) != tc.wantPhase {
					t.Fatalf("phase = %q, want %q (conditions %+v)", got.Status.Phase, tc.wantPhase, got.Status.Conditions)
				}
				return got
			}

			got := reconciled()
			cond := meta.FindStatusCondition(got.Status.Conditions, shellSandboxImagePinnedConditionType)
			if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != shellSandboxImagePinnedReason {
				t.Fatalf("expected %s True/%s, got %+v (conditions %+v)",
					shellSandboxImagePinnedConditionType, shellSandboxImagePinnedReason, cond, got.Status.Conditions)
			}

			got.Spec.Harness.Experimental.ShellSandbox.Image = ""
			if err := cl.Update(ctx, got); err != nil {
				t.Fatalf("clear the pin: %v", err)
			}
			got = reconciled()
			if cond := meta.FindStatusCondition(got.Status.Conditions, shellSandboxImagePinnedConditionType); cond != nil {
				t.Errorf("%s survived clearing the pin: %+v", shellSandboxImagePinnedConditionType, cond)
			}
		})
	}
}
