package controller

import (
	"context"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestNoChatPlatformMessageFollowsTheChatPlatforms: the condition is written
// only on a next install with neither chat platform; a platform, enabled
// with or without a home channel, hands the question to HomeChannelUnset,
// and today installs carry nothing.
func TestNoChatPlatformMessageFollowsTheChatPlatforms(t *testing.T) {
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		want  bool
	}{
		{"next, no chat", a2aTestAgent(), true},
		{"next, slack without a home channel", slackTestAgent("next", true), false},
		{"next, chat with a home channel", withHome(gchatTestAgent("next", true), "spaces/AAAA"), false},
		{"next, slack disabled explicitly", slackTestAgent("next", false), true},
		{"today, no chat", func() *agentv1alpha1.PlatformAgent { a := a2aTestAgent(); a.Spec.Mode = nil; return a }(), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := noChatPlatformMsg(tc.agent)
			if (got != "") != tc.want {
				t.Fatalf("message = %q, want set=%v", got, tc.want)
			}
			if tc.want {
				for _, name := range []string{"report store", "spec.integration.slack", "spec.integration.googleChat"} {
					if !strings.Contains(got, name) {
						t.Errorf("message does not name %q: %q", name, got)
					}
				}
			}
		})
	}
	// Where the findings go follows the CR: a GitOps repository is named only
	// when one is registered, the report store alone otherwise.
	bare := a2aTestAgent()
	if msg := noChatPlatformMsg(bare); !strings.Contains(msg, "report store on the data volume alone") || strings.Contains(msg, "reach the GitOps repository") {
		t.Errorf("no repository registered, message = %q", msg)
	}
	withRepo := a2aTestAgent()
	withRepo.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{IntegrationSpec: agentv1alpha1.IntegrationSpec{
		Forges:       []agentv1alpha1.ForgeSpec{{Name: "github", Namespace: "acme"}},
		Repositories: []agentv1alpha1.RepositorySpec{{Forge: "github", Repository: "infra", Role: agentv1alpha1.RepositoryRoleGitOps}},
	}}
	if msg := noChatPlatformMsg(withRepo); !strings.Contains(msg, "reach the GitOps repository") {
		t.Errorf("repository registered, message = %q", msg)
	}
}

// TestPlatformAgentReconciler_Reconcile_NoChatPlatformOnADegradedPark: a bare
// helm install with no chat parks on ShellSandboxKeysMissing indefinitely,
// so the condition has to reach the CR on that park too (syncA2AConditions,
// with the live re-read's predicate carrying its term), and enabling a
// platform has to clear it there too.
func TestPlatformAgentReconciler_Reconcile_NoChatPlatformOnADegradedPark(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Name = "test-agent-nochat-parked"
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster",
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent). // no shell sandbox keys Secret: the pass parks Degraded
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	parked := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		for i := 1; i <= 2; i++ {
			_, _ = r.Reconcile(ctx, req) // a park may return an error; the status is what is checked
		}
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatalf("get: %v", err)
		}
		if got.Status.Phase != "Degraded" {
			t.Fatalf("the fixture no longer parks Degraded (phase %q, conditions %+v); this test needs a Degraded park", got.Status.Phase, got.Status.Conditions)
		}
		return got
	}
	got := parked()
	if cond := meta.FindStatusCondition(got.Status.Conditions, noChatPlatformConditionType); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("expected %s True on a Degraded park with the stack rendered, got %+v (conditions %+v)",
			noChatPlatformConditionType, cond, got.Status.Conditions)
	}
	got.Spec.Integration = slackTestAgent("next", true).Spec.Integration
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("enable slack: %v", err)
	}
	got = parked()
	if cond := meta.FindStatusCondition(got.Status.Conditions, noChatPlatformConditionType); cond != nil {
		t.Fatalf("%s survived enabling a chat platform on a Degraded park: %+v", noChatPlatformConditionType, cond)
	}
}

// TestNoChatPlatformConditionSetsAndClears: written True with the reason and
// message, read back as current, and removed once the message is "".
func TestNoChatPlatformConditionSetsAndClears(t *testing.T) {
	a := a2aTestAgent()
	msg := noChatPlatformMsg(a)
	if noChatPlatformConditionCurrent(a, msg) {
		t.Fatal("a CR with no condition reads as current for a non-empty message")
	}
	setNoChatPlatformCondition(a, msg, metav1.Now())
	cond := meta.FindStatusCondition(a.Status.Conditions, noChatPlatformConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != noChatPlatformReason || cond.Message != msg {
		t.Fatalf("condition = %+v, want True/%s with the message", cond, noChatPlatformReason)
	}
	if !noChatPlatformConditionCurrent(a, msg) {
		t.Error("the condition just written does not read as current: every pass would write status")
	}
	setNoChatPlatformCondition(a, "", metav1.Now())
	if meta.FindStatusCondition(a.Status.Conditions, noChatPlatformConditionType) != nil {
		t.Error("the condition survived an empty message")
	}
}

// TestPlatformAgentReconciler_Reconcile_NoChatPlatformCondition goes through
// the Ready status writer: a next install with no chat platform gets the
// condition, keeps Ready's own say, and loses it once a platform is enabled.
func TestPlatformAgentReconciler_Reconcile_NoChatPlatformCondition(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	agent.Name = "test-agent-nochat"
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster",
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	for i := 1; i <= 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i, err)
		}
	}
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatalf("get: %v", err)
	}
	cond := meta.FindStatusCondition(got.Status.Conditions, noChatPlatformConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != noChatPlatformReason {
		t.Fatalf("expected %s True/%s after reconcile, got %+v (conditions %+v)",
			noChatPlatformConditionType, noChatPlatformReason, cond, got.Status.Conditions)
	}
	if meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType) != nil {
		t.Error("HomeChannelUnset written beside NoChatPlatform: the two are exclusive")
	}

	got.Spec.Integration = slackTestAgent("next", true).Spec.Integration
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("enable slack: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after enabling slack failed: %v", err)
	}
	restored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, restored); err != nil {
		t.Fatalf("get: %v", err)
	}
	if cond := meta.FindStatusCondition(restored.Status.Conditions, noChatPlatformConditionType); cond != nil {
		t.Errorf("%s survived enabling a chat platform: %+v", noChatPlatformConditionType, cond)
	}
}
