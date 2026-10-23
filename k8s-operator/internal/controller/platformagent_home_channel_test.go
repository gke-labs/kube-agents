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
	corev1 "k8s.io/api/core/v1"
	"strings"
	"testing"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestHomeChannelUnsetMessageFollowsTheGatewaysBackend: the advisory exists
// exactly when the next stack's gateway holds a chat backend whose home
// channel is empty (#3320). Today's mode posts through the agent, so it never
// applies there; with both chats enabled the gateway holds Chat, so only
// Chat's home channel counts.
func TestHomeChannelUnsetMessageFollowsTheGatewaysBackend(t *testing.T) {
	slackHome := func(mode, home string) func() string {
		return func() string {
			a := slackTestAgent(mode, true)
			a.Spec.Integration.Slack.HomeChannel = home
			return homeChannelUnsetMessage(a)
		}
	}
	chatHome := func(mode, home string) func() string {
		return func() string {
			a := gchatTestAgent(mode, true)
			a.Spec.Integration.GoogleChat.HomeChannel = home
			return homeChannelUnsetMessage(a)
		}
	}
	bothChatHomeOnly := func() string {
		a := chatAndSlackTestAgent("next")
		a.Spec.Integration.GoogleChat.HomeChannel = "spaces/AAA"
		return homeChannelUnsetMessage(a)
	}
	for _, tc := range []struct {
		name  string
		msg   func() string
		names []string // empty: no condition
	}{
		{"next slack, no home", slackHome("next", ""), []string{"Slack", "spec.integration.slack.homeChannel", "SLACK_HOME_CHANNEL"}},
		{"next slack, blank home", slackHome("next", "  "), []string{"spec.integration.slack.homeChannel"}},
		{"next slack, home set", slackHome("next", "C0123ABCD"), nil},
		{"today slack, no home", slackHome("today", ""), nil},
		{"next chat, no home", chatHome("next", ""), []string{"Google Chat", "spec.integration.googleChat.homeChannel", "GOOGLE_CHAT_HOME_CHANNEL"}},
		{"next chat, home set", chatHome("next", "spaces/AAA"), nil},
		{"today chat, no home", chatHome("", ""), nil},
		{"next chat and slack, chat home set", bothChatHomeOnly, nil},
		{"next, no chat", func() string { return homeChannelUnsetMessage(a2aTestAgent()) }, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got := tc.msg()
			if len(tc.names) == 0 {
				if got != "" {
					t.Fatalf("want no condition, got %q", got)
				}
				return
			}
			for _, want := range tc.names {
				if !strings.Contains(got, want) {
					t.Errorf("message does not name %q: %q", want, got)
				}
			}
			if !strings.Contains(got, "dropped") {
				t.Errorf("message must say the posts are dropped, not just that a field is empty: %q", got)
			}
		})
	}
}

// TestHomeChannelUnsetConditionSetsAndClears: written True with the reason and
// message, read back as current, and removed once the message is "".
func TestHomeChannelUnsetConditionSetsAndClears(t *testing.T) {
	a := slackTestAgent("next", true)
	msg := homeChannelUnsetMessage(a)
	if homeChannelUnsetConditionCurrent(a, msg) {
		t.Fatal("a CR with no condition reads as current for a non-empty message")
	}
	setHomeChannelUnsetCondition(a, msg, metav1.Now())
	cond := meta.FindStatusCondition(a.Status.Conditions, homeChannelUnsetConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != homeChannelUnsetReason || cond.Message != msg {
		t.Fatalf("condition = %+v, want True/%s with the message", cond, homeChannelUnsetReason)
	}
	if !homeChannelUnsetConditionCurrent(a, msg) {
		t.Error("the condition just written does not read as current: every pass would write status")
	}
	if homeChannelUnsetConditionCurrent(a, msg+" reworded") {
		t.Error("a reworded message reads as current: a release's new wording would never reach the CR")
	}
	setHomeChannelUnsetCondition(a, "", metav1.Now())
	if meta.FindStatusCondition(a.Status.Conditions, homeChannelUnsetConditionType) != nil {
		t.Error("the condition survived an empty message")
	}
	if !homeChannelUnsetConditionCurrent(a, "") {
		t.Error("an absent condition does not read as current for an empty message")
	}
}

// TestPlatformAgentReconciler_Reconcile_HomeChannelUnsetCondition goes through
// the Ready status writer: a next install whose gateway holds Slack with no
// home channel gets the condition, keeps Ready's own say, and loses the
// condition once homeChannel is set. Removing either the write or its term in
// the no-op gate fails here.
func TestPlatformAgentReconciler_Reconcile_HomeChannelUnsetCondition(t *testing.T) {
	scheme := setupScheme()
	agent := slackTestAgent("next", true)
	agent.Name = "test-agent-home"
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
	cond := meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType)
	if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != homeChannelUnsetReason {
		t.Fatalf("expected %s True/%s after reconcile, got %+v (conditions %+v)",
			homeChannelUnsetConditionType, homeChannelUnsetReason, cond, got.Status.Conditions)
	}
	if !strings.Contains(cond.Message, "spec.integration.slack.homeChannel") {
		t.Errorf("message does not name the field: %q", cond.Message)
	}

	got.Spec.Integration.Slack.HomeChannel = "C0123ABCD"
	if err := cl.Update(ctx, got); err != nil {
		t.Fatalf("set homeChannel: %v", err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after setting homeChannel failed: %v", err)
	}
	restored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, restored); err != nil {
		t.Fatalf("get: %v", err)
	}
	if cond := meta.FindStatusCondition(restored.Status.Conditions, homeChannelUnsetConditionType); cond != nil {
		t.Errorf("%s survived setting homeChannel: %+v", homeChannelUnsetConditionType, cond)
	}
}

// TestPlatformAgentReconciler_Reconcile_HomeChannelUnsetOnADegradedPark: a
// bare helm install parks on ShellSandboxKeysMissing indefinitely with the
// next stack rendered, its gateway dropping proactive posts all the while,
// so the condition has to reach the CR on that park too (syncA2AConditions).
func TestPlatformAgentReconciler_Reconcile_HomeChannelUnsetOnADegradedPark(t *testing.T) {
	scheme := setupScheme()
	agent := slackTestAgent("next", true)
	agent.Name = "test-agent-home-parked"
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster",
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent). // no shell sandbox keys Secret: the pass parks Degraded
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	// The APIReader is set, as in production, so syncA2AConditions' live
	// re-read runs: without the home-channel term in its predicate, a live CR
	// whose other next-stack conditions are already current reads as
	// satisfied and the write is skipped on every park after the upgrade.
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
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
	if cond := meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("expected %s True on a Degraded park with the stack rendered, got %+v (conditions %+v)",
			homeChannelUnsetConditionType, cond, got.Status.Conditions)
	}
}

// TestHomeChannelRecordObjectAndRole: the record is an owned ConfigMap with
// no data (the gateway owns the keys), rendered on every next stack, Slack on
// the gateway or not, torn down with the stack; the gateway Role reaches that
// one name with get and patch only; the gateway Deployment carries the policy,
// the record's name and its own image. Nothing in this release reads the
// record: the agent env and the condition follow the CR field alone.
func TestHomeChannelRecordObjectAndRole(t *testing.T) {
	a := slackTestAgent("next", true)
	cm := buildHomeChannelRecord(a)
	if cm.Name != a.Name+"-home-channel" || cm.Namespace != a.Namespace || len(cm.Data) != 0 {
		t.Fatalf("record object = %+v", cm.ObjectMeta)
	}
	rules := buildA2AGatewayRole(a).Rules
	rule := rules[len(rules)-1]
	if len(rule.Resources) != 1 || rule.Resources[0] != "configmaps" ||
		len(rule.ResourceNames) != 1 || rule.ResourceNames[0] != cm.Name ||
		strings.Join(rule.Verbs, ",") != "get,patch" {
		t.Errorf("record rule = %+v", rule)
	}
	var torn bool
	r := &PlatformAgentReconciler{}
	for _, e := range r.a2aNamespacedTeardown(a) {
		if c, ok := e.obj.(*corev1.ConfigMap); ok && c.Name == cm.Name {
			torn = true
		}
	}
	if !torn {
		t.Error("the record is not in the next stack's teardown list")
	}
	gw := buildA2AGatewayDeployment(a)
	env := envMapOf(gw.Spec.Template.Spec.Containers[0].Env)
	if env[a2aSlackHomePolicyEnvVar].Value != "auto" || env[a2aHomeChannelRecordEnvVar].Value != cm.Name || env[a2aGatewaySelfImageEnvVar].Value != a2aGatewayImage() {
		t.Errorf("gateway env: policy=%q record=%q self=%q", env[a2aSlackHomePolicyEnvVar].Value, env[a2aHomeChannelRecordEnvVar].Value, env[a2aGatewaySelfImageEnvVar].Value)
	}
	a.Spec.Integration.Slack.HomeChannelPolicy = "manual"
	if got := envMapOf(buildA2AGatewayDeployment(a).Spec.Template.Spec.Containers[0].Env)[a2aSlackHomePolicyEnvVar].Value; got != "manual" {
		t.Errorf("gateway policy env = %q, want manual", got)
	}
	// The policy and the record have no effect on the agent in this release.
	for _, policy := range []string{"", "auto", "manual"} {
		a.Spec.Integration.Slack.HomeChannelPolicy = policy
		a.Spec.Integration.Slack.HomeChannel = ""
		pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{})
		agentEnv := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
		if _, ok := agentEnv[agentSlackHomeChannelEnvVar]; ok {
			t.Errorf("policy %q: SLACK_HOME_CHANNEL rendered with no CR home", policy)
		}
		if homeChannelUnsetMessage(a) == "" {
			t.Errorf("policy %q: no HomeChannelUnset with no CR home", policy)
		}
	}
}
