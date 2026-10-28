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
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// TestHomeChannelUnsetMessageFollowsTheGatewaysBackend: the advisory exists
// exactly when the next stack's gateway holds a chat backend whose home
// channel is empty (#3320). Today's mode posts through the agent, so it never
// applies there; with both chats enabled the gateway holds Chat, so only
// Chat's home channel counts. With no record, Slack's message says a mention
// sets the channel.
func TestHomeChannelUnsetMessageFollowsTheGatewaysBackend(t *testing.T) {
	slackHome := func(mode, home string) func() string {
		return func() string {
			a := slackTestAgent(mode, true)
			a.Spec.Integration.Slack.HomeChannel = home
			return homeChannelUnsetMessage(a, homeChannelRecord{})
		}
	}
	chatHome := func(mode, home string) func() string {
		return func() string {
			a := gchatTestAgent(mode, true)
			a.Spec.Integration.GoogleChat.HomeChannel = home
			return homeChannelUnsetMessage(a, homeChannelRecord{})
		}
	}
	bothChatHomeOnly := func() string {
		a := chatAndSlackTestAgent("next")
		a.Spec.Integration.GoogleChat.HomeChannel = "spaces/AAA"
		return homeChannelUnsetMessage(a, homeChannelRecord{})
	}
	for _, tc := range []struct {
		name  string
		msg   func() string
		names []string // empty: no condition
	}{
		{"next slack, no home", slackHome("next", ""), []string{"Slack", "spec.integration.slack.homeChannel", "SLACK_HOME_CHANNEL", "mention the agent"}},
		{"next slack, blank home", slackHome("next", "  "), []string{"spec.integration.slack.homeChannel"}},
		{"next slack, home set", slackHome("next", "C0123ABCD"), nil},
		{"today slack, no home", slackHome("today", ""), nil},
		{"next chat, no home", chatHome("next", ""), []string{"Google Chat", "spec.integration.googleChat.homeChannel", "GOOGLE_CHAT_HOME_CHANNEL"}},
		{"next chat, home set", chatHome("next", "spaces/AAA"), nil},
		{"today chat, no home", chatHome("", ""), nil},
		{"next chat and slack, chat home set", bothChatHomeOnly, nil},
		{"next, no chat", func() string { return homeChannelUnsetMessage(a2aTestAgent(), homeChannelRecord{}) }, nil},
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
	msg := homeChannelUnsetMessage(a, homeChannelRecord{})
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

// recordKeepingApply is the fake's server-side apply with one difference the
// real API server has: a ConfigMap applied with no data keeps the data keys
// another writer set. The fake's apply is a full Update, which would wipe the
// record's keys on every pass.
func recordKeepingApply(agent *agentv1alpha1.PlatformAgent) interceptor.Funcs {
	ssa := fakeServerSideApplyInterceptors()
	return interceptor.Funcs{Patch: func(ctx context.Context, c client.WithWatch, obj client.Object, patch client.Patch, opts ...client.PatchOption) error {
		if cm, ok := obj.(*corev1.ConfigMap); ok && cm.Name == a2aHomeChannelRecordName(agent) && len(cm.Data) == 0 {
			existing := &corev1.ConfigMap{}
			if err := c.Get(ctx, client.ObjectKeyFromObject(cm), existing); err == nil {
				cm.Data = existing.Data
			}
		}
		return ssa.Patch(ctx, c, obj, patch, opts...)
	}}
}

// writeHomeRecord replaces the record's data in the fake client.
func writeHomeRecord(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent, data map[string]string) {
	t.Helper()
	cm := &corev1.ConfigMap{}
	key := types.NamespacedName{Name: a2aHomeChannelRecordName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, cm); err != nil {
		t.Fatalf("the record was not rendered: %v", err)
	}
	cm.Data = data
	if err := cl.Update(ctx, cm); err != nil {
		t.Fatalf("write the record: %v", err)
	}
}

// TestPlatformAgentReconciler_Reconcile_HomeChannelUnsetOnADegradedPark: a
// bare helm install parks on ShellSandboxKeysMissing indefinitely with the
// next stack rendered, its gateway dropping proactive posts all the while,
// so the condition has to reach the CR on that park too (syncA2AConditions),
// and an admitted record has to clear it there too: the parked path reads
// the record off the render state, not from a read of its own.
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
		WithInterceptorFuncs(recordKeepingApply(agent)).
		Build()
	// The APIReader is set, as in production, so syncA2AConditions' live
	// re-read runs: without the home-channel term in its predicate, a live CR
	// whose other next-stack conditions are already current reads as
	// satisfied and the write is skipped on every park after the upgrade.
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
	if cond := meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType); cond == nil || cond.Status != metav1.ConditionTrue {
		t.Fatalf("expected %s True on a Degraded park with the stack rendered, got %+v (conditions %+v)",
			homeChannelUnsetConditionType, cond, got.Status.Conditions)
	}
	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: a2aGatewayImage()})
	got = parked()
	if cond := meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType); cond != nil {
		t.Fatalf("%s survived an admitted record on a Degraded park: %+v", homeChannelUnsetConditionType, cond)
	}
}

// TestHomeChannelRecordStandsInForTheField: the record stands in for the CR
// field exactly when the gateway stands behind it. Each row checks the
// effective home, the agent env, the condition and what its message names;
// a record value never reaches the status verbatim. The malformed rows are a
// hand-edit defence: the gateway checks a channel before it writes one.
func TestHomeChannelRecordStandsInForTheField(t *testing.T) {
	image := a2aGatewayImage()
	long := "us-east4-docker.pkg.dev/some-project/some-repository/kube-agents/a2a-gateway@sha256:" + strings.Repeat("ab", 64) + "-" + strings.Repeat("x", 100)
	if len([]rune(long)) <= homeChannelRecordImageMax {
		t.Fatalf("test image is %d runes, not over the display bound", len([]rune(long)))
	}
	record := func(data map[string]string) *corev1.ConfigMap { return &corev1.ConfigMap{Data: data} }
	now := time.Now()
	ago := func(d time.Duration) string { return now.Add(-d).UTC().Format(time.RFC3339) }
	// learned has no slack.setAt: a record without the gateway's timestamp
	// was not written by a learn and applies at once.
	learned := map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: image}
	restart := "rollout restart deployment/" + a2aGatewayName(slackTestAgent("next", true)) + " -n " + slackTestAgent("next", true).Namespace
	for _, tc := range []struct {
		name      string
		crHome    string
		policy    string
		cm        *corev1.ConfigMap
		image     string // the rendered gateway image; "" means a2aGatewayImage()
		wantHome  string // the agent's SLACK_HOME_CHANNEL, "" for none
		wantCond  bool
		condNames []string
		condNot   []string // never in the message
	}{
		{"no record", "", "", nil, "", "", true, []string{"mention the agent"}, nil},
		{"learned", "", "", record(learned), "", "C0LEARNED", false, nil, nil},
		{"learned a minute ago is pending", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: image, homeChannelRecordSetAtKey: ago(time.Minute)}), "", "", true, []string{"C0LEARNED", "applied to the agent at", homeChannelAdmitDelay.String(), "finishes before"}, nil},
		{"learned past the delay is admitted", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: image, homeChannelRecordSetAtKey: ago(homeChannelAdmitDelay + time.Second)}), "", "C0LEARNED", false, nil, nil},
		{"learned with an unreadable timestamp is admitted at once", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: image, homeChannelRecordSetAtKey: "yesterday"}), "", "C0LEARNED", false, nil, nil},
		{"a fresh record under another reader is stale, not pending", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: "ghcr.io/x/a2a-gateway:old", homeChannelRecordSetAtKey: ago(time.Minute)}), "", "", true, []string{"last confirmed by gateway image"}, []string{"applied to the agent at"}},
		{"learned, policy auto spelled out", "", "auto", record(learned), "", "C0LEARNED", false, nil, nil},
		{"cr wins over the record", "C0CR", "", record(learned), "", "C0CR", false, nil, nil},
		{"manual ignores the record", "", "manual", record(learned), "", "", true, []string{"manual"}, nil},
		{"manual with the cr set", "C0CR", "manual", record(learned), "", "C0CR", false, nil, nil},
		{"manual is named before a malformed record", "", "manual", record(map[string]string{homeChannelRecordSlackKey: "D0DM", homeChannelRecordReaderKey: image}), "", "", true, []string{"manual"}, []string{"not a channel id"}},
		{"stale reader", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: "ghcr.io/x/a2a-gateway:old"}), "", "", true, []string{"C0LEARNED", "ghcr.io/x/a2a-gateway:old", image, restart}, nil},
		{"stale with no reader", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED"}), "", "", true, []string{"C0LEARNED", "no confirming gateway image", restart}, nil},
		{"stale reader over the display bound is cut, not echoed", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: strings.Repeat("x", 40000)}), "", "", true, []string{"C0LEARNED", restart}, nil},
		{"stale reader with control characters is printed without them", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: "ghcr.io/x/a2a-gateway:old\n\x1b[31mred"}), "", "", true, []string{"ghcr.io/x/a2a-gateway:old", "red"}, []string{"\n", "\x1b"}},
		{"writer alone does not admit", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", "writer": image}), "", "", true, []string{"no confirming gateway image"}, nil},
		{"reader equal in full past the display bound", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: long}), long, "C0LEARNED", false, nil, nil},
		{"reader differing past the display bound", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: long + "!"}), long, "", true, []string{"C0LEARNED"}, nil},
		{"recorded channel the gateway refused", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: image, homeChannelRecordRefusedKey: "archived"}), "", "", true, []string{"C0LEARNED", "archived", "replaces it"}, nil},
		{"recorded channel refused as malformed", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: image, homeChannelRecordRefusedKey: "malformed"}), "", "", true, []string{"C0LEARNED", "not a channel id"}, nil},
		{"refused candidate, known reason", "", "", record(map[string]string{homeChannelRecordRefusedKey: "shared"}), "", "", true, []string{"refused", "Slack Connect"}, nil},
		{"refused candidate, unknown reason is not echoed", "", "", record(map[string]string{homeChannelRecordRefusedKey: strings.Repeat("x", 40000)}), "", "", true, []string{"refused", "does not know"}, nil},
		{"malformed learned id", "", "", record(map[string]string{homeChannelRecordSlackKey: "D0DM", homeChannelRecordReaderKey: image}), "", "", true, []string{"not a channel id"}, nil},
		{"malformed learned id is named, not echoed", "", "", record(map[string]string{homeChannelRecordSlackKey: "general " + strings.Repeat("y", 40000), homeChannelRecordReaderKey: image}), "", "", true, []string{"not a channel id"}, nil},
		{"over-long grammar-valid id is not echoed", "", "", record(map[string]string{homeChannelRecordSlackKey: "C" + strings.Repeat("A", 40000), homeChannelRecordReaderKey: image}), "", "", true, []string{"not a channel id"}, nil},
		{"malformed cr value does not fall through to the record", "general", "", record(learned), "", "", false, nil, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := slackTestAgent("next", true)
			a.Spec.Integration.Slack.HomeChannel = tc.crHome
			a.Spec.Integration.Slack.HomeChannelPolicy = tc.policy
			rendered := tc.image
			if rendered == "" {
				rendered = image
			}
			rec := readHomeChannelRecord(a, tc.cm, rendered, now)
			if got := a2aSlackHomeEffective(a, rec); got != tc.wantHome {
				t.Errorf("effective home = %q, want %q", got, tc.wantHome)
			}
			pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{slackHomeRecord: rec})
			env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
			if got := env[agentSlackHomeChannelEnvVar].Value; got != tc.wantHome {
				t.Errorf("%s = %q, want %q", agentSlackHomeChannelEnvVar, got, tc.wantHome)
			}
			if _, routed := env[a2aNotifyPlatformEnvVar]; routed != (tc.wantHome != "") {
				t.Errorf("%s present = %v, want %v", a2aNotifyPlatformEnvVar, routed, tc.wantHome != "")
			}
			msg := homeChannelUnsetMessage(a, rec)
			if (msg != "") != tc.wantCond {
				t.Fatalf("condition message = %q, want set=%v", msg, tc.wantCond)
			}
			if len(msg) > 1024 {
				t.Errorf("condition message is %d bytes; a record value must not reach the status verbatim", len(msg))
			}
			for _, want := range tc.condNames {
				if !strings.Contains(msg, want) {
					t.Errorf("message does not name %q: %q", want, msg)
				}
			}
			for _, never := range tc.condNot {
				if strings.Contains(msg, never) {
					t.Errorf("message carries %q: %q", never, msg)
				}
			}
		})
	}
}

// TestHomeChannelRequeue: a pending record brings the steady-state requeue
// forward to its admission time and never pushes it back; a record that is
// not pending leaves it alone.
func TestHomeChannelRequeue(t *testing.T) {
	steady := 15 * time.Minute
	if got := homeChannelRequeue(steady, homeChannelRecord{}); got != steady {
		t.Errorf("no record: %s, want %s", got, steady)
	}
	if got := homeChannelRequeue(steady, homeChannelRecord{pending: true, appliesIn: 4 * time.Minute}); got != 4*time.Minute {
		t.Errorf("pending in 4m: %s, want 4m", got)
	}
	if got := homeChannelRequeue(time.Minute, homeChannelRecord{pending: true, appliesIn: 4 * time.Minute}); got != time.Minute {
		t.Errorf("pending in 4m with a 1m steady requeue: %s, want 1m", got)
	}
}

// TestHomeChannelFutureTimestampWaitsTheDelayAtMost: a slack.setAt ahead of
// the operator's clock (a skewed writer, a hand edit) holds the admission for
// one delay from now, not until that future time plus the delay.
func TestHomeChannelFutureTimestampWaitsTheDelayAtMost(t *testing.T) {
	now := time.Date(2026, 10, 28, 18, 30, 0, 0, time.UTC)
	a := slackTestAgent("next", true)
	cm := &corev1.ConfigMap{Data: map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: a2aGatewayImage(),
		homeChannelRecordSetAtKey: now.Add(24 * time.Hour).Format(time.RFC3339)}}
	rec := readHomeChannelRecord(a, cm, a2aGatewayImage(), now)
	if !rec.pending || rec.appliesIn != homeChannelAdmitDelay || !rec.appliesAt.Equal(now.Add(homeChannelAdmitDelay)) {
		t.Fatalf("future timestamp: pending=%v appliesIn=%s appliesAt=%s, want pending for exactly %s from now", rec.pending, rec.appliesIn, rec.appliesAt, homeChannelAdmitDelay)
	}
}

// TestHomeChannelRecordObjectAndRole: the record is an owned ConfigMap with
// no data (the gateway owns the keys), rendered on every next stack, Slack on
// the gateway or not, torn down with the stack; the gateway Role reaches that
// one name with get and patch only; the gateway Deployment carries the policy,
// the record's name, its own image and the learn switch, on.
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
	if env[a2aHomeChannelLearnEnvVar].Value != "true" {
		t.Errorf("%s = %q, want true: the admission and the learn ship together", a2aHomeChannelLearnEnvVar, env[a2aHomeChannelLearnEnvVar].Value)
	}
	a.Spec.Integration.Slack.HomeChannelPolicy = "manual"
	if got := envMapOf(buildA2AGatewayDeployment(a).Spec.Template.Spec.Containers[0].Env)[a2aSlackHomePolicyEnvVar].Value; got != "manual" {
		t.Errorf("gateway policy env = %q, want manual", got)
	}
}

// TestPlatformAgentReconciler_Reconcile_HomeChannelRecordDrivesEnvAndCondition
// goes through the reconciler: with the CR field empty, a record the rendered
// gateway image confirmed arms the agent's env and clears the condition; the
// same record under another reader, or with a refusal beside it, arms nothing
// and the condition names why.
func TestPlatformAgentReconciler_Reconcile_HomeChannelRecordDrivesEnvAndCondition(t *testing.T) {
	scheme := setupScheme()
	agent := slackTestAgent("next", true)
	agent.Name = "test-agent-home-record"
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster",
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(recordKeepingApply(agent)).
		Build()
	// A fixed clock: the admit delay is measured against it, so the pending
	// and admitted legs do not depend on how long the test takes.
	clock := time.Date(2026, 10, 28, 18, 30, 0, 0, time.UTC)
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, now: func() time.Time { return clock }}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	reconcile := func() ctrl.Result {
		t.Helper()
		var res ctrl.Result
		for i := 1; i <= 2; i++ {
			var err error
			if res, err = r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile %d failed: %v", i, err)
			}
		}
		return res
	}
	agentEnv := func() map[string]corev1.EnvVar {
		t.Helper()
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, dep); err != nil {
			t.Fatalf("get agent Deployment: %v", err)
		}
		return envMapOf(brokerContainerNamed(dep.Spec.Template.Spec.Containers, "platform-agent").Env)
	}
	condition := func() *metav1.Condition {
		t.Helper()
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatalf("get: %v", err)
		}
		return meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType)
	}

	reconcile()
	if _, ok := agentEnv()[agentSlackHomeChannelEnvVar]; ok {
		t.Fatal("SLACK_HOME_CHANNEL rendered with no CR field and an empty record")
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "mention the agent") {
		t.Fatalf("no HomeChannelUnset naming the mention with an empty record: %+v", c)
	}

	// A fresh learn: recorded, not yet applied; the pass requeues for the
	// remainder of the delay, since nothing else will wake it.
	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: a2aGatewayImage(), homeChannelRecordSetAtKey: clock.Add(-time.Minute).Format(time.RFC3339)})
	res := reconcile()
	if _, ok := agentEnv()[agentSlackHomeChannelEnvVar]; ok {
		t.Fatal("a record younger than the admit delay reached the agent env")
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "applied to the agent at") {
		t.Fatalf("pending record not named in the condition: %+v", c)
	}
	// The fake install never reaches Ready, so this pass returns on an
	// earlier 30 s requeue; what matters is that no pass waits past the
	// admission time. TestHomeChannelRequeue pins the steady-state path.
	if want := homeChannelAdmitDelay - time.Minute; res.RequeueAfter <= 0 || res.RequeueAfter > want {
		t.Fatalf("RequeueAfter = %s, want within (0, %s]: the admission pass would come late or never", res.RequeueAfter, want)
	}

	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: a2aGatewayImage(), homeChannelRecordSetAtKey: clock.Add(-homeChannelAdmitDelay).Format(time.RFC3339)})
	reconcile()
	env := agentEnv()
	if env[agentSlackHomeChannelEnvVar].Value != "C0LEARNED" || env[a2aNotifyPlatformEnvVar].Value != a2aNotifyPlatformSlack {
		t.Fatalf("the record did not reach the agent env: home=%q platform=%q", env[agentSlackHomeChannelEnvVar].Value, env[a2aNotifyPlatformEnvVar].Value)
	}
	if c := condition(); c != nil {
		t.Fatalf("HomeChannelUnset survived an admitted record: %+v", c)
	}

	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: "ghcr.io/x/a2a-gateway:rolled-back"})
	reconcile()
	if _, ok := agentEnv()[agentSlackHomeChannelEnvVar]; ok {
		t.Fatal("a record another gateway image confirmed armed the agent")
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "rolled-back") || !strings.Contains(c.Message, "rollout restart") {
		t.Fatalf("stale record not named with its exit in the condition: %+v", c)
	}

	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordReaderKey: a2aGatewayImage(), homeChannelRecordRefusedKey: "archived"})
	reconcile()
	if _, ok := agentEnv()[agentSlackHomeChannelEnvVar]; ok {
		t.Fatal("a recorded channel the gateway refused armed the agent")
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "archived") {
		t.Fatalf("refused record not named in the condition: %+v", c)
	}
}
