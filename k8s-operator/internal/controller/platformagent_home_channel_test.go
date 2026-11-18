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
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
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
// exactly when the gateway holds a chat backend whose home channel is empty
// (#3320), whatever spec.mode says; with both chats enabled the gateway
// holds both, and each empty home is reported, Chat first. A platform
// without a home has its posts sent to the other's home channel when that
// one has a home, and kept for the digest when neither has. With no record,
// Slack's message says a mention sets the channel.
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
		name   string
		msg    func() string
		names  []string // empty: no condition
		absent []string
	}{
		{"next slack, no home", slackHome("next", ""), []string{"Slack", "spec.integration.slack.homeChannel", "SLACK_HOME_CHANNEL", "mention the agent"}, nil},
		{"next slack, blank home", slackHome("next", "  "), []string{"spec.integration.slack.homeChannel"}, nil},
		{"next slack, home set", slackHome("next", "C0123ABCD"), nil, nil},
		{"no mode slack, no home", slackHome("", ""), []string{"Slack", "spec.integration.slack.homeChannel"}, nil},
		{"a stored today slack, no home", slackHome("today", ""), []string{"Slack", "spec.integration.slack.homeChannel"}, nil},
		{"next chat, no home", chatHome("next", ""), []string{"Google Chat", "spec.integration.googleChat.homeChannel", "GOOGLE_CHAT_HOME_CHANNEL", "keeps them"}, nil},
		{"next chat and slack, no chat home", func() string {
			a := chatAndSlackTestAgent("next")
			a.Spec.Integration.GoogleChat.HomeChannel = ""
			return homeChannelUnsetMessage(a, homeChannelRecord{})
		}, []string{"holds Google Chat", "holds Slack", "keeps them", "mention the agent"}, []string{"Slack Hermes platform", "home channel meanwhile"}},
		{"next chat, home set", chatHome("next", "spaces/AAA"), nil, nil},
		{"no mode chat, no home", chatHome("", ""), []string{"Google Chat", "spec.integration.googleChat.homeChannel"}, nil},
		// Slack shares the gateway, so its empty home is reported.
		{"next chat and slack, chat home set", bothChatHomeOnly, []string{"holds Slack", "spec.integration.slack.homeChannel", "mention the agent", "go to Google Chat's home channel meanwhile"}, []string{"holds Google Chat", "keeps them"}},
		// Slack's home, from the CR or an admitted record, takes Chat's posts.
		{"next chat and slack, slack home set", func() string {
			a := chatAndSlackTestAgent("next")
			a.Spec.Integration.Slack.HomeChannel = "C0123ABCD"
			return homeChannelUnsetMessage(a, homeChannelRecord{})
		}, []string{"holds Google Chat", "go to Slack's home channel meanwhile"}, []string{"holds Slack", "keeps them"}},
		{"next chat and slack, slack record admitted", func() string {
			a := chatAndSlackTestAgent("next")
			return homeChannelUnsetMessage(a, homeChannelRecord{channel: "C0123ABCD", recorded: "C0123ABCD"})
		}, []string{"holds Google Chat", "go to Slack's home channel meanwhile"}, []string{"holds Slack", "keeps them"}},
		// An admitted learned Slack channel is Slack's home: no condition.
		{"next chat and slack, chat home set, slack record admitted", func() string {
			a := chatAndSlackTestAgent("next")
			a.Spec.Integration.GoogleChat.HomeChannel = "spaces/AAA"
			return homeChannelUnsetMessage(a, homeChannelRecord{channel: "C0123ABCD", recorded: "C0123ABCD"})
		}, nil, nil},
		{"next, no chat", func() string { return homeChannelUnsetMessage(a2aTestAgent(), homeChannelRecord{}) }, nil, nil},
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
			for _, unwanted := range tc.absent {
				if strings.Contains(got, unwanted) {
					t.Errorf("message names %q: %q", unwanted, got)
				}
			}
			if c, sl := strings.Index(got, "holds Google Chat"), strings.Index(got, "holds Slack"); c >= 0 && sl >= 0 && c > sl {
				t.Errorf("Chat's sentence must come first: %q", got)
			}
			if !strings.Contains(got, "keeps them") && !strings.Contains(got, "home channel meanwhile") {
				t.Errorf("message must say where the posts go meanwhile, not just that a field is empty: %q", got)
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
	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED"})
	got = parked()
	if cond := meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType); cond != nil {
		t.Fatalf("%s survived an admitted record on a Degraded park: %+v", homeChannelUnsetConditionType, cond)
	}
}

// TestHomeChannelRecordDrivesTheProjection: the projection carries the CR
// field, else the admitted record; the agent env carries the CR field only
// (a learned channel reaches the agent through the file, never through env,
// so a learn rolls no pod); the condition says why a record is not admitted,
// and a record value never reaches the status verbatim. The malformed rows
// are a hand-edit defence: the gateway checks a channel before it writes one.
func TestHomeChannelRecordDrivesTheProjection(t *testing.T) {
	record := func(data map[string]string) *corev1.ConfigMap { return &corev1.ConfigMap{Data: data} }
	learned := map[string]string{homeChannelRecordSlackKey: "C0LEARNED", "writer": "ghcr.io/x/a2a-gateway:any"}
	for _, tc := range []struct {
		name      string
		crHome    string
		policy    string
		cm        *corev1.ConfigMap
		wantHome  string // the projection's slack_home
		wantEnv   string // the agent's SLACK_HOME_CHANNEL, "" for none
		wantCond  bool
		condNames []string
		condNot   []string // never in the message
	}{
		{"no record", "", "", nil, "", "", true, []string{"mention the agent"}, nil},
		{"learned", "", "", record(learned), "C0LEARNED", "", false, nil, nil},
		{"learned with the gateway's audit keys", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", "slack.setBy": "U1", "slack.setAt": "2026-10-29T12:00:00Z", "writer": "img"}), "C0LEARNED", "", false, nil, nil},
		{"learned, policy auto spelled out", "", "auto", record(learned), "C0LEARNED", "", false, nil, nil},
		{"cr wins over the record", "C0CR", "", record(learned), "C0CR", "C0CR", false, nil, nil},
		{"cr alone", "C0CR", "", nil, "C0CR", "C0CR", false, nil, nil},
		{"manual ignores the record", "", "manual", record(learned), "", "", true, []string{"manual"}, nil},
		{"manual with the cr set", "C0CR", "manual", record(learned), "C0CR", "C0CR", false, nil, nil},
		{"manual is named before a malformed record", "", "manual", record(map[string]string{homeChannelRecordSlackKey: "D0DM"}), "", "", true, []string{"manual"}, []string{"not a channel id"}},
		{"recorded channel the gateway refused", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordRefusedKey: "archived"}), "", "", true, []string{"C0LEARNED", "archived", "replaces it"}, nil},
		{"recorded channel refused as malformed", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordRefusedKey: "malformed"}), "", "", true, []string{"C0LEARNED", "not a channel id"}, nil},
		{"recorded group DM refused as a direct message", "", "", record(map[string]string{homeChannelRecordSlackKey: "G0MPIM", homeChannelRecordRefusedKey: "direct-message"}), "", "", true, []string{"G0MPIM", "direct or group direct message"}, []string{"does not know", "not a channel id"}},
		{"refused candidate, known reason", "", "", record(map[string]string{homeChannelRecordRefusedKey: "shared"}), "", "", true, []string{"refused", "Slack Connect"}, nil},
		{"refused candidate, unknown reason is not echoed", "", "", record(map[string]string{homeChannelRecordRefusedKey: strings.Repeat("x", 40000)}), "", "", true, []string{"refused", "does not know"}, nil},
		{"malformed learned id", "", "", record(map[string]string{homeChannelRecordSlackKey: "D0DM"}), "", "", true, []string{"not a channel id"}, nil},
		{"malformed learned id is named, not echoed", "", "", record(map[string]string{homeChannelRecordSlackKey: "general " + strings.Repeat("y", 40000)}), "", "", true, []string{"not a channel id"}, nil},
		{"over-long grammar-valid id is not echoed", "", "", record(map[string]string{homeChannelRecordSlackKey: "C" + strings.Repeat("A", 40000)}), "", "", true, []string{"not a channel id"}, nil},
		{"malformed cr value does not fall through to the record", "general", "", record(learned), "", "", false, nil, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := slackTestAgent("next", true)
			a.Spec.Integration.Slack.HomeChannel = tc.crHome
			a.Spec.Integration.Slack.HomeChannelPolicy = tc.policy
			rec := readHomeChannelRecord(a, tc.cm)
			proj := buildHomeChannelProjection(a, rec)
			got, present := proj.Data[homeChannelProjectionKey]
			if !present || got != tc.wantHome {
				t.Errorf("projection %s = (%q, %v), want (%q, true)", homeChannelProjectionKey, got, present, tc.wantHome)
			}
			pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{})
			env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
			if got := env[agentSlackHomeChannelEnvVar].Value; got != tc.wantEnv {
				t.Errorf("%s = %q, want %q (the learned channel travels as a file, not env)", agentSlackHomeChannelEnvVar, got, tc.wantEnv)
			}
			// Listed only with the CR's channel: a learned one reaches the
			// agent through the file, and a malformed CR value arms no route.
			wantListed := tc.wantEnv != ""
			if _, listed := env[a2aNotifyPlatformEnvVar]; listed != wantListed {
				t.Errorf("%s present = %v, want %v", a2aNotifyPlatformEnvVar, listed, wantListed)
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

// TestHomeChannelRecordLeavesTheNotifyListsAlone: with Chat and Slack on
// the gateway, Chat without a home and Slack's home only in the admitted
// record, the platform list stays absent (no CR home), the conversations
// list names every armed route, and the learned channel is not env: the pod
// template reads CR fields only, so a learn rolls no pod. The agent finds
// the learned channel in the projected file and sends home posts to Slack.
func TestHomeChannelRecordLeavesTheNotifyListsAlone(t *testing.T) {
	a := chatAndSlackTestAgent("next")
	rec := readHomeChannelRecord(a, &corev1.ConfigMap{Data: map[string]string{homeChannelRecordSlackKey: "C0LEARNED"}})
	if rec.channel == "" {
		t.Fatal("the record was not admitted; the case tests nothing")
	}
	pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{})
	env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
	if got, ok := env[a2aNotifyPlatformEnvVar]; ok {
		t.Errorf("%s = %q, want absent: no home channel is set on the CR", a2aNotifyPlatformEnvVar, got.Value)
	}
	if got := env[a2aNotifyConversationsEnvVar].Value; got != "google_chat,slack" {
		t.Errorf("%s = %q, want %q", a2aNotifyConversationsEnvVar, got, "google_chat,slack")
	}
	if got, ok := env[agentSlackHomeChannelEnvVar]; ok {
		t.Errorf("%s = %q; the learned channel travels as a file, not env", agentSlackHomeChannelEnvVar, got.Value)
	}
}

// TestHomeChannelRecordObjectsRoleAndMounts: the record with no data and the
// Role rule that reaches it; the projection with one key per backend always
// present; the projection mounted read-only, Optional and without subPath in
// the agent container and the gateway container, on a gateway that holds
// Slack or Google Chat, and not in the dashboard container; the gateway env
// that arms the learn.
func TestHomeChannelRecordObjectsRoleAndMounts(t *testing.T) {
	a := slackTestAgent("next", true)
	cm := buildHomeChannelRecord(a)
	if cm.Name != a.Name+"-home-channel" || cm.Namespace != a.Namespace || len(cm.Data) != 0 {
		t.Fatalf("record object = %+v", cm.ObjectMeta)
	}
	proj := buildHomeChannelProjection(a, homeChannelRecord{})
	if proj.Name != a.Name+"-chat-home" || proj.Namespace != a.Namespace || len(proj.Data) != 2 {
		t.Fatalf("projection object = %+v data %v", proj.ObjectMeta, proj.Data)
	}
	for _, key := range []string{homeChannelProjectionKey, homeChannelProjectionGchatKey} {
		if v, ok := proj.Data[key]; !ok || v != "" {
			t.Errorf("an empty projection must still carry %s: %v", key, proj.Data)
		}
	}
	// The fourth rule; the resolved scope's read follows it.
	rule := buildA2AGatewayRole(a).Rules[3]
	if len(rule.Resources) != 1 || rule.Resources[0] != "configmaps" ||
		len(rule.ResourceNames) != 1 || rule.ResourceNames[0] != cm.Name ||
		strings.Join(rule.Verbs, ",") != "get,patch" {
		t.Errorf("record rule = %+v (the projection needs no rule: the pods read it as a file)", rule)
	}
	mountOf := func(c *corev1.Container) *corev1.VolumeMount {
		if c == nil {
			return nil
		}
		for i := range c.VolumeMounts {
			if c.VolumeMounts[i].Name == homeChannelProjectionVolumeName {
				return &c.VolumeMounts[i]
			}
		}
		return nil
	}
	volumeOf := func(vs []corev1.Volume) *corev1.Volume {
		for i := range vs {
			if vs[i].Name == homeChannelProjectionVolumeName {
				return &vs[i]
			}
		}
		return nil
	}
	checkVolume := func(where string, v *corev1.Volume) {
		t.Helper()
		if v == nil || v.ConfigMap == nil || v.ConfigMap.Name != proj.Name || v.ConfigMap.Optional == nil || !*v.ConfigMap.Optional {
			t.Errorf("%s: projection volume = %+v, want an Optional ConfigMap volume of %s", where, v, proj.Name)
			return
		}
		if v.ConfigMap.DefaultMode == nil || *v.ConfigMap.DefaultMode != homeChannelProjectionFileMode {
			t.Errorf("%s: projection DefaultMode = %v, want %04o so the agent user can read the file", where, v.ConfigMap.DefaultMode, homeChannelProjectionFileMode)
		}
	}
	checkMount := func(where string, m *corev1.VolumeMount, dir string) {
		t.Helper()
		if m == nil || m.MountPath != dir || !m.ReadOnly || m.SubPath != "" {
			t.Errorf("%s: projection mount = %+v, want a read-only directory mount at %s with no subPath", where, m, dir)
		}
	}
	pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{})
	checkVolume("agent pod", volumeOf(pod.Spec.Volumes))
	checkMount("agent container", mountOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent")), agentChatHomeDir)
	// The bridge sidecar mirrors every agent mount by construction
	// (buildA2ABridgeContainer); no other container carries this one.
	for i := range pod.Spec.Containers {
		if c := &pod.Spec.Containers[i]; c.Name != "platform-agent" && c.Name != a2aBridgeContainerName && mountOf(c) != nil {
			t.Errorf("container %s carries the projection mount; only the agent container (and the bridge sidecar that mirrors its mounts) reads it", c.Name)
		}
	}
	gw := buildA2AGatewayDeployment(a)
	checkVolume("gateway pod", volumeOf(gw.Spec.Template.Spec.Volumes))
	checkMount("gateway container", mountOf(&gw.Spec.Template.Spec.Containers[0]), a2aGatewayChatHomeDir)
	env := envMapOf(gw.Spec.Template.Spec.Containers[0].Env)
	if env[a2aSlackHomePolicyEnvVar].Value != "auto" || env[a2aHomeChannelRecordEnvVar].Value != cm.Name || env[a2aGatewaySelfImageEnvVar].Value != a2aGatewayImage() {
		t.Errorf("gateway env: policy=%q record=%q self=%q", env[a2aSlackHomePolicyEnvVar].Value, env[a2aHomeChannelRecordEnvVar].Value, env[a2aGatewaySelfImageEnvVar].Value)
	}
	if env[a2aHomeChannelLearnEnvVar].Value != "true" {
		t.Errorf("%s = %q, want true: the projection and the learn ship together", a2aHomeChannelLearnEnvVar, env[a2aHomeChannelLearnEnvVar].Value)
	}
	a.Spec.Integration.Slack.HomeChannelPolicy = "manual"
	if got := envMapOf(buildA2AGatewayDeployment(a).Spec.Template.Spec.Containers[0].Env)[a2aSlackHomePolicyEnvVar].Value; got != "manual" {
		t.Errorf("gateway policy env = %q, want manual", got)
	}

	// Chat alone on the gateway: both pods carry the projection, and the
	// gateway carries Chat's policy with the record, image and switch once.
	chat := gchatTestAgent("next", true)
	chatPod := buildPodTemplateSpec(chat, "h", "h", "h", "h", nil, renderOptions{})
	checkVolume("chat agent pod", volumeOf(chatPod.Spec.Volumes))
	checkMount("chat agent container", mountOf(brokerContainerNamed(chatPod.Spec.Containers, "platform-agent")), agentChatHomeDir)
	chatGw := buildA2AGatewayDeployment(chat)
	checkVolume("chat gateway pod", volumeOf(chatGw.Spec.Template.Spec.Volumes))
	checkMount("chat gateway container", mountOf(&chatGw.Spec.Template.Spec.Containers[0]), a2aGatewayChatHomeDir)
	chatEnv := envMapOf(chatGw.Spec.Template.Spec.Containers[0].Env)
	if chatEnv[a2aGchatHomePolicyEnvVar].Value != "auto" || chatEnv[a2aHomeChannelRecordEnvVar].Value != a2aHomeChannelRecordName(chat) ||
		chatEnv[a2aGatewaySelfImageEnvVar].Value != a2aGatewayImage() || chatEnv[a2aHomeChannelLearnEnvVar].Value != "true" {
		t.Errorf("chat gateway env: policy=%q record=%q self=%q learn=%q", chatEnv[a2aGchatHomePolicyEnvVar].Value,
			chatEnv[a2aHomeChannelRecordEnvVar].Value, chatEnv[a2aGatewaySelfImageEnvVar].Value, chatEnv[a2aHomeChannelLearnEnvVar].Value)
	}
	if _, slack := chatEnv[a2aSlackHomePolicyEnvVar]; slack {
		t.Errorf("a Chat-only gateway carries %s", a2aSlackHomePolicyEnvVar)
	}
	chat.Spec.Integration.GoogleChat.HomeChannelPolicy = "manual"
	if got := envMapOf(buildA2AGatewayDeployment(chat).Spec.Template.Spec.Containers[0].Env)[a2aGchatHomePolicyEnvVar].Value; got != "manual" {
		t.Errorf("chat gateway policy env = %q, want manual", got)
	}

	// Neither on the gateway: no projection mount anywhere, so those
	// templates stay as they were.
	for name, other := range map[string]*agentv1alpha1.PlatformAgent{"no chat": a2aTestAgent()} {
		pod := buildPodTemplateSpec(other, "h", "h", "h", "h", nil, renderOptions{})
		if volumeOf(pod.Spec.Volumes) != nil || mountOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent")) != nil {
			t.Errorf("%s: the agent pod carries the projection mount", name)
		}
		gw := buildA2AGatewayDeployment(other)
		if volumeOf(gw.Spec.Template.Spec.Volumes) != nil || mountOf(&gw.Spec.Template.Spec.Containers[0]) != nil {
			t.Errorf("%s: the gateway pod carries the projection mount", name)
		}
	}

	// Chat beside Slack: Slack is still on the gateway, so both
	// pods carry the projection.
	both := chatAndSlackTestAgent("next")
	bothPod := buildPodTemplateSpec(both, "h", "h", "h", "h", nil, renderOptions{})
	checkVolume("chat and slack agent pod", volumeOf(bothPod.Spec.Volumes))
	checkMount("chat and slack agent container", mountOf(brokerContainerNamed(bothPod.Spec.Containers, "platform-agent")), agentChatHomeDir)
	bothGw := buildA2AGatewayDeployment(both)
	checkVolume("chat and slack gateway pod", volumeOf(bothGw.Spec.Template.Spec.Volumes))
	checkMount("chat and slack gateway container", mountOf(&bothGw.Spec.Template.Spec.Containers[0]), a2aGatewayChatHomeDir)
	// Both policies, and the shared three exactly once each.
	seen := map[string]int{}
	for _, e := range bothGw.Spec.Template.Spec.Containers[0].Env {
		seen[e.Name]++
	}
	for _, name := range []string{a2aSlackHomePolicyEnvVar, a2aGchatHomePolicyEnvVar, a2aHomeChannelRecordEnvVar, a2aGatewaySelfImageEnvVar, a2aHomeChannelLearnEnvVar} {
		if seen[name] != 1 {
			t.Errorf("chat and slack gateway carries %s %d times, want once", name, seen[name])
		}
	}
}

// TestGchatHomeChannelRecordDrivesTheProjection is
// TestHomeChannelRecordDrivesTheProjection for Google Chat: the projection's
// gchat_home is "the CR, else the admitted record", the gateway's
// A2A_GCHAT_HOME_CHANNEL and the agent's A2A_NOTIFY_PLATFORM carry the CR
// field only (a learn rolls no pod), the condition says why a record is not
// admitted, and a record value never reaches the status verbatim.
func TestGchatHomeChannelRecordDrivesTheProjection(t *testing.T) {
	record := func(data map[string]string) *corev1.ConfigMap { return &corev1.ConfigMap{Data: data} }
	learned := map[string]string{homeChannelRecordGchatKey: "spaces/LEARNED", "gchat.setBy": "users/1", "gchat.setAt": "2026-11-18T12:00:00Z", "writer": "img"}
	for _, tc := range []struct {
		name      string
		crHome    string
		policy    string
		cm        *corev1.ConfigMap
		wantHome  string // the projection's gchat_home
		wantEnv   string // the gateway's A2A_GCHAT_HOME_CHANNEL, "" for none
		wantCond  bool
		condNames []string
		condNot   []string
	}{
		{"no record", "", "", nil, "", "", true, []string{"mention the agent in the space"}, nil},
		{"learned", "", "", record(learned), "spaces/LEARNED", "", false, nil, nil},
		{"learned, policy auto spelled out", "", "auto", record(learned), "spaces/LEARNED", "", false, nil, nil},
		{"cr wins over the record", "spaces/CR", "", record(learned), "spaces/CR", "spaces/CR", false, nil, nil},
		{"cr alone", "spaces/CR", "", nil, "spaces/CR", "spaces/CR", false, nil, nil},
		{"manual ignores the record", "", "manual", record(learned), "", "", true, []string{"manual"}, nil},
		{"manual with the cr set", "spaces/CR", "manual", record(learned), "spaces/CR", "spaces/CR", false, nil, nil},
		{"recorded space the gateway refused", "", "", record(map[string]string{homeChannelRecordGchatKey: "spaces/LEARNED", homeChannelRecordGchatRefusedKey: "not-member"}), "", "", true, []string{"spaces/LEARNED", "not a member of the space", "replaces it"}, nil},
		{"recorded space opened to outsiders", "", "", record(map[string]string{homeChannelRecordGchatKey: "spaces/LEARNED", homeChannelRecordGchatRefusedKey: "shared"}), "", "", true, []string{"outside the organization"}, []string{"Slack Connect"}},
		{"refused candidate, known reason", "", "", record(map[string]string{homeChannelRecordGchatRefusedKey: "direct-message"}), "", "", true, []string{"refused", "direct message or a group chat"}, nil},
		{"refused candidate, a word Chat does not write", "", "", record(map[string]string{homeChannelRecordGchatRefusedKey: "archived"}), "", "", true, []string{"refused", "does not know"}, nil},
		{"refused candidate, unknown reason is not echoed", "", "", record(map[string]string{homeChannelRecordGchatRefusedKey: strings.Repeat("x", 40000)}), "", "", true, []string{"does not know"}, nil},
		{"a thread is not a space", "", "", record(map[string]string{homeChannelRecordGchatKey: "spaces/A/threads/T"}), "", "", true, []string{"not a Chat space name"}, nil},
		{"a Slack id is not a space", "", "", record(map[string]string{homeChannelRecordGchatKey: "C0LEARNED"}), "", "", true, []string{"not a Chat space name"}, nil},
		{"over-long grammar-valid name is not echoed", "", "", record(map[string]string{homeChannelRecordGchatKey: "spaces/" + strings.Repeat("A", 40000)}), "", "", true, []string{"not a Chat space name"}, nil},
		{"Slack's keys say nothing of Chat", "", "", record(map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordRefusedKey: "archived"}), "", "", true, []string{"mention the agent in the space"}, []string{"archived", "C0LEARNED"}},
		{"malformed cr value does not fall through to the record", "general", "", record(learned), "", "general", false, nil, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := gchatTestAgent("next", true)
			a.Spec.Integration.GoogleChat.HomeChannel = tc.crHome
			a.Spec.Integration.GoogleChat.HomeChannelPolicy = tc.policy
			rec := readHomeChannelRecord(a, tc.cm)
			proj := buildHomeChannelProjection(a, rec)
			if got, present := proj.Data[homeChannelProjectionGchatKey]; !present || got != tc.wantHome {
				t.Errorf("projection %s = (%q, %v), want (%q, true)", homeChannelProjectionGchatKey, got, present, tc.wantHome)
			}
			if got := proj.Data[homeChannelProjectionKey]; got != "" {
				t.Errorf("projection %s = %q on a Chat-only install, want empty", homeChannelProjectionKey, got)
			}
			gwEnv := envMapOf(buildA2AGatewayDeployment(a).Spec.Template.Spec.Containers[0].Env)
			if got := gwEnv[a2aGchatHomeChannelEnvVar].Value; got != tc.wantEnv {
				t.Errorf("%s = %q, want %q (the learned space travels as a file, not env)", a2aGchatHomeChannelEnvVar, got, tc.wantEnv)
			}
			pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{})
			env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
			wantListed := gchatSpaceName(tc.crHome) != ""
			if _, listed := env[a2aNotifyPlatformEnvVar]; listed != wantListed {
				t.Errorf("%s present = %v, want %v", a2aNotifyPlatformEnvVar, listed, wantListed)
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

// TestHomeChannelRecordKeepsEachBackendsHome: one gateway holds Chat and
// Slack, and the one record holds both learned homes under their own keys.
// Each is admitted and projected on its own, a refusal of one leaves the
// other, each policy governs its own backend, and the condition's meanwhile
// sentence follows a learned home on the other platform.
func TestHomeChannelRecordKeepsEachBackendsHome(t *testing.T) {
	both := map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordGchatKey: "spaces/LEARNED", "writer": "img"}
	for _, tc := range []struct {
		name                 string
		mut                  func(*agentv1alpha1.PlatformAgent)
		data                 map[string]string
		wantSlack, wantGchat string
		condNames, condNot   []string
	}{
		{"both learned", nil, both, "C0LEARNED", "spaces/LEARNED", nil, nil},
		{"only Chat learned", nil, map[string]string{homeChannelRecordGchatKey: "spaces/LEARNED"}, "", "spaces/LEARNED",
			[]string{"holds Slack", homeChannelUnsetToGchat}, []string{"holds Google Chat"}},
		{"only Slack learned", nil, map[string]string{homeChannelRecordSlackKey: "C0LEARNED"}, "C0LEARNED", "",
			[]string{"holds Google Chat", homeChannelUnsetToSlackRoute}, []string{"holds Slack"}},
		{"Chat's refusal leaves Slack's home", nil, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordGchatKey: "spaces/LEARNED", homeChannelRecordGchatRefusedKey: "not-member"},
			"C0LEARNED", "", []string{"holds Google Chat", "spaces/LEARNED"}, []string{"holds Slack"}},
		{"Slack's refusal leaves Chat's home", nil, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", homeChannelRecordRefusedKey: "archived", homeChannelRecordGchatKey: "spaces/LEARNED"},
			"", "spaces/LEARNED", []string{"holds Slack", "C0LEARNED"}, []string{"holds Google Chat"}},
		{"Chat manual, Slack auto", func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.GoogleChat.HomeChannelPolicy = "manual" }, both,
			"C0LEARNED", "", []string{"holds Google Chat", "manual"}, []string{"holds Slack"}},
		{"Slack manual, Chat auto", func(a *agentv1alpha1.PlatformAgent) { a.Spec.Integration.Slack.HomeChannelPolicy = "manual" }, both,
			"", "spaces/LEARNED", []string{"holds Slack", "manual"}, []string{"holds Google Chat"}},
		{"neither", nil, nil, "", "", []string{"holds Google Chat", "holds Slack", homeChannelUnsetKept}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			a := chatAndSlackTestAgent("next")
			if tc.mut != nil {
				tc.mut(a)
			}
			rec := readHomeChannelRecord(a, &corev1.ConfigMap{Data: tc.data})
			proj := buildHomeChannelProjection(a, rec)
			if proj.Data[homeChannelProjectionKey] != tc.wantSlack || proj.Data[homeChannelProjectionGchatKey] != tc.wantGchat {
				t.Errorf("projection = %v, want slack_home=%q gchat_home=%q", proj.Data, tc.wantSlack, tc.wantGchat)
			}
			msg := homeChannelUnsetMessage(a, rec)
			if (msg == "") != (len(tc.condNames) == 0) {
				t.Fatalf("condition message = %q", msg)
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
			// The pod template reads CR fields only: no learned home is env.
			pod := buildPodTemplateSpec(a, "h", "h", "h", "h", nil, renderOptions{})
			if got, ok := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)[a2aNotifyPlatformEnvVar]; ok {
				t.Errorf("%s = %q, want absent: no home channel is set on the CR", a2aNotifyPlatformEnvVar, got.Value)
			}
		})
	}
}

// TestPlatformAgentReconciler_Reconcile_HomeChannelRecordDrivesTheProjection
// goes through the reconciler: with the CR field empty, a record the gateway
// learned reaches the projection and clears the condition, with the agent's
// pod template byte-identical before and after (no roll) and no requeue
// brought forward; a changed record reaches the projection on the next
// single pass; a refused record empties the projection and the condition
// names why; a hand-edited projection is rewritten on the next pass; a CR
// field reaches both the projection and the env.
func TestPlatformAgentReconciler_Reconcile_HomeChannelRecordDrivesTheProjection(t *testing.T) {
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
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
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
	agentTemplate := func() corev1.PodTemplateSpec {
		t.Helper()
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, dep); err != nil {
			t.Fatalf("get agent Deployment: %v", err)
		}
		return dep.Spec.Template
	}
	agentEnv := func() map[string]corev1.EnvVar {
		return envMapOf(brokerContainerNamed(agentTemplate().Spec.Containers, "platform-agent").Env)
	}
	projection := func() map[string]string {
		t.Helper()
		cm := &corev1.ConfigMap{}
		if err := cl.Get(ctx, types.NamespacedName{Name: a2aHomeChannelProjectionName(agent), Namespace: agent.Namespace}, cm); err != nil {
			t.Fatalf("get the projection: %v", err)
		}
		return cm.Data
	}
	condition := func() *metav1.Condition {
		t.Helper()
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatalf("get: %v", err)
		}
		return meta.FindStatusCondition(got.Status.Conditions, homeChannelUnsetConditionType)
	}

	first := reconcile()
	if _, ok := agentEnv()[agentSlackHomeChannelEnvVar]; ok {
		t.Fatal("SLACK_HOME_CHANNEL rendered with no CR field and an empty record")
	}
	if got := projection(); got[homeChannelProjectionKey] != "" {
		t.Fatalf("projection with an empty record = %v, want the key empty", got)
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "mention the agent") {
		t.Fatalf("no HomeChannelUnset naming the mention with an empty record: %+v", c)
	}
	before := agentTemplate()

	// The gateway learned a channel: the projection follows, the condition
	// clears, the agent's template does not change, and the pass asks for
	// no earlier requeue than before.
	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0LEARNED", "slack.setBy": "U1", "writer": "img"})
	res := reconcile()
	if got := projection(); got[homeChannelProjectionKey] != "C0LEARNED" {
		t.Fatalf("projection after the learn = %v, want C0LEARNED", got)
	}
	if _, ok := agentEnv()[agentSlackHomeChannelEnvVar]; ok {
		t.Fatal("the learned channel reached the agent env: that is the pod roll this design removes")
	}
	if !equality.Semantic.DeepEqual(before, agentTemplate()) {
		t.Fatal("the agent's pod template changed on a learn: the projection must stay out of it and its hashes")
	}
	if c := condition(); c != nil {
		t.Fatalf("HomeChannelUnset survived an admitted record: %+v", c)
	}
	if res.RequeueAfter != first.RequeueAfter {
		t.Errorf("RequeueAfter = %s after the learn, want %s: nothing waits on a record", res.RequeueAfter, first.RequeueAfter)
	}

	// A changed record reaches the projection on the very next pass: the
	// projection is built from this pass's read, never the previous one's.
	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0SECOND", "slack.setBy": "U2", "writer": "img"})
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile after the record changed failed: %v", err)
	}
	if got := projection(); got[homeChannelProjectionKey] != "C0SECOND" {
		t.Fatalf("projection one pass after the record changed = %v, want C0SECOND", got)
	}

	// A hand edit of the projection is the operator's to undo.
	proj := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aHomeChannelProjectionName(agent), Namespace: agent.Namespace}, proj); err != nil {
		t.Fatal(err)
	}
	proj.Data[homeChannelProjectionKey] = "C0HAND"
	if err := cl.Update(ctx, proj); err != nil {
		t.Fatal(err)
	}
	reconcile()
	if got := projection(); got[homeChannelProjectionKey] != "C0SECOND" {
		t.Fatalf("projection after a hand edit and a pass = %v, want C0SECOND back", got)
	}

	// A refusal beside the recorded channel empties the projection.
	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordSlackKey: "C0SECOND", homeChannelRecordRefusedKey: "archived"})
	reconcile()
	if got := projection(); got[homeChannelProjectionKey] != "" {
		t.Fatalf("projection with a refused record = %v, want the key empty", got)
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "archived") {
		t.Fatalf("refused record not named in the condition: %+v", c)
	}

	// The CR field wins: projection and env both carry it.
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
		t.Fatal(err)
	}
	got.Spec.Integration.Slack.HomeChannel = "C0CR"
	if err := cl.Update(ctx, got); err != nil {
		t.Fatal(err)
	}
	reconcile()
	if p := projection(); p[homeChannelProjectionKey] != "C0CR" {
		t.Fatalf("projection with the CR set = %v, want C0CR", p)
	}
	if env := agentEnv(); env[agentSlackHomeChannelEnvVar].Value != "C0CR" || env[a2aNotifyPlatformEnvVar].Value != a2aNotifyPlatformSlack {
		t.Fatalf("the CR field did not reach the agent env: %v", env)
	}
	if c := condition(); c != nil {
		t.Fatalf("HomeChannelUnset survived the CR field: %+v", c)
	}
}

// TestPlatformAgentReconciler_Reconcile_GchatHomeChannelRecordDrivesTheProjection
// goes through the reconciler on a Chat-only install: with the CR field
// empty, a space the gateway learned reaches the projection's gchat_home and
// clears the condition, with the agent's pod template identical before and
// after (no roll; the gateway's render never reads the record); a refused
// space empties the
// projection and the condition names why; a CR field wins.
func TestPlatformAgentReconciler_Reconcile_GchatHomeChannelRecordDrivesTheProjection(t *testing.T) {
	scheme := setupScheme()
	agent := gchatTestAgent("next", true)
	agent.Name = "test-agent-gchat-home-record"
	agent.Spec.Integration.GoogleChat.HomeChannel = ""
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{
		ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster",
	}
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(recordKeepingApply(agent)).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	reconcile := func() {
		t.Helper()
		for i := 1; i <= 2; i++ {
			if _, err := r.Reconcile(ctx, req); err != nil {
				t.Fatalf("Reconcile %d failed: %v", i, err)
			}
		}
	}
	template := func(name string) corev1.PodTemplateSpec {
		t.Helper()
		dep := &appsv1.Deployment{}
		if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, dep); err != nil {
			t.Fatalf("get Deployment %s: %v", name, err)
		}
		return dep.Spec.Template
	}
	projection := func() map[string]string {
		t.Helper()
		cm := &corev1.ConfigMap{}
		if err := cl.Get(ctx, types.NamespacedName{Name: a2aHomeChannelProjectionName(agent), Namespace: agent.Namespace}, cm); err != nil {
			t.Fatalf("get the projection: %v", err)
		}
		return cm.Data
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
	if got, ok := projection()[homeChannelProjectionGchatKey]; !ok || got != "" {
		t.Fatalf("projection with an empty record = %v, want %s present and empty", projection(), homeChannelProjectionGchatKey)
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "mention the agent in the space") {
		t.Fatalf("no HomeChannelUnset naming the mention with an empty record: %+v", c)
	}
	agentBefore := template(agent.Name + "-gateway")

	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordGchatKey: "spaces/LEARNED", "gchat.setBy": "users/1", "writer": "img"})
	reconcile()
	if got := projection()[homeChannelProjectionGchatKey]; got != "spaces/LEARNED" {
		t.Fatalf("projection after the learn = %v, want spaces/LEARNED", projection())
	}
	if c := condition(); c != nil {
		t.Errorf("HomeChannelUnset still set after the learn: %s", c.Message)
	}
	if !equality.Semantic.DeepEqual(agentBefore, template(agent.Name+"-gateway")) {
		t.Error("the agent's pod template changed with the learn; a learn must roll no pod")
	}

	writeHomeRecord(t, ctx, cl, agent, map[string]string{homeChannelRecordGchatKey: "spaces/LEARNED", homeChannelRecordGchatRefusedKey: "not-member"})
	reconcile()
	if got := projection()[homeChannelProjectionGchatKey]; got != "" {
		t.Errorf("projection after the refusal = %v, want gchat_home empty", projection())
	}
	if c := condition(); c == nil || !strings.Contains(c.Message, "spaces/LEARNED") || !strings.Contains(c.Message, "not a member of the space") {
		t.Errorf("HomeChannelUnset after the refusal = %+v, want it to name the space and the reason", c)
	}

	live := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, live); err != nil {
		t.Fatal(err)
	}
	live.Spec.Integration.GoogleChat.HomeChannel = "spaces/CR"
	if err := cl.Update(ctx, live); err != nil {
		t.Fatal(err)
	}
	reconcile()
	if got := projection()[homeChannelProjectionGchatKey]; got != "spaces/CR" {
		t.Errorf("projection with the CR field set = %v, want spaces/CR", projection())
	}
	if c := condition(); c != nil {
		t.Errorf("HomeChannelUnset set with the CR field set: %s", c.Message)
	}
}

// The values the Google Chat learn keeps in step by hand across four places
// that cannot import one another: the bound on a recorded space name (gateway
// and operator) and the projected file's key (gateway, operator, and the
// agent's two readers). Each file is read as text for its one declaration.
const (
	gatewayGchatHomeSource  = "../../../a2a/gateway/gchat_home_channel.go"
	agentChatNotifySource   = "../../../agents/platform/scripts/chat_notify.py"
	kanbanChatNotifySource  = "../../../deploy/docker/patches/kanban_chat_notify.py"
	gatewaySpaceMaxPattern  = `(?m)^\tgchatHomeSpaceMax = (\d+)$`
	gatewayGchatKeyPattern  = `(?m)^\tgchatHomeFileKey = "([^"]+)"$`
	gatewaySlackKeyPattern  = `(?m)^\thomeFileKey\s+= "([^"]+)"$`
	pythonGchatKeyPattern   = `(?m)^GCHAT_HOME_FILE_KEY = "([^"]+)"$`
	pythonSlackKeyPattern   = `(?m)^HOME_FILE_KEY = "([^"]+)"$`
	homeValueMissingMessage = "%s declares nothing matching %s; if the declaration moved or changed shape, this test's pattern must follow it"
)

// TestGchatHomeChannelValuesMatchTheGatewayAndTheAgent fails when the bound
// or a projected file key differs between the operator and the other side
// that reads or writes it.
func TestGchatHomeChannelValuesMatchTheGatewayAndTheAgent(t *testing.T) {
	declared := func(path, pattern string) string {
		t.Helper()
		raw, err := os.ReadFile(path)
		if err != nil {
			t.Fatalf("read %s: %v", path, err)
		}
		m := regexp.MustCompile(pattern).FindSubmatch(raw)
		if m == nil {
			t.Fatalf(homeValueMissingMessage, path, pattern)
		}
		return string(m[1])
	}
	if got := declared(gatewayGchatHomeSource, gatewaySpaceMaxPattern); got != strconv.Itoa(gchatHomeChannelRecordIDMax) {
		t.Errorf("the gateway bounds a learned space at %s runes, the operator admits %d: a space one side takes the other refuses", got, gchatHomeChannelRecordIDMax)
	}
	for _, tc := range []struct{ path, pattern, want string }{
		{gatewayHomeChannelSource, gatewayGchatKeyPattern, homeChannelProjectionGchatKey},
		{gatewayHomeChannelSource, gatewaySlackKeyPattern, homeChannelProjectionKey},
		{agentChatNotifySource, pythonGchatKeyPattern, homeChannelProjectionGchatKey},
		{agentChatNotifySource, pythonSlackKeyPattern, homeChannelProjectionKey},
		{kanbanChatNotifySource, pythonGchatKeyPattern, homeChannelProjectionGchatKey},
		{kanbanChatNotifySource, pythonSlackKeyPattern, homeChannelProjectionKey},
	} {
		if got := declared(tc.path, tc.pattern); got != tc.want {
			t.Errorf("%s reads the projected file %q, the operator writes %q", tc.path, got, tc.want)
		}
	}
}

// gatewayHomeChannelSource holds the gateway's refusal vocabulary, the
// homeRefused* constants it writes into slack.refused. The a2a module cannot be
// imported here, so the words are read from the source.
const gatewayHomeChannelSource = "../../../a2a/gateway/home_channel.go"

// homeChannelRefusedReasons is a copy of the gateway's vocabulary: a word the
// gateway writes and the operator does not map renders as a reason the operator
// does not know. Every step fails rather than passing on nothing found.
func TestHomeChannelRefusedReasonsMatchTheGatewaysVocabulary(t *testing.T) {
	f, err := parser.ParseFile(token.NewFileSet(), gatewayHomeChannelSource, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v (if the gateway's home-channel file moved, this test's path must move with it; it is what keeps homeChannelRefusedReasons honest)", gatewayHomeChannelSource, err)
	}
	var gateway []string
	ast.Inspect(f, func(n ast.Node) bool {
		spec, ok := n.(*ast.ValueSpec)
		if !ok {
			return true
		}
		for i, name := range spec.Names {
			if !strings.HasPrefix(name.Name, "homeRefused") || i >= len(spec.Values) {
				continue
			}
			lit, ok := spec.Values[i].(*ast.BasicLit)
			if !ok || lit.Kind != token.STRING {
				t.Fatalf("%s in %s is not a string literal; this test reads the words as literals", name.Name, gatewayHomeChannelSource)
			}
			word, err := strconv.Unquote(lit.Value)
			if err != nil {
				t.Fatalf("%s in %s: %v", name.Name, gatewayHomeChannelSource, err)
			}
			gateway = append(gateway, word)
		}
		return true
	})
	if len(gateway) == 0 {
		t.Fatalf("no homeRefused* string constants in %s; they are what homeChannelRefusedReasons mirrors", gatewayHomeChannelSource)
	}
	operator := make([]string, 0, len(homeChannelRefusedReasons))
	for word := range homeChannelRefusedReasons {
		operator = append(operator, word)
	}
	sort.Strings(gateway)
	sort.Strings(operator)
	if strings.Join(gateway, ",") != strings.Join(operator, ",") {
		t.Errorf("the gateway writes refusal words %q, homeChannelRefusedReasons maps %q: a word on one side only renders as %q or is never written", gateway, operator, homeChannelRefusedUnknown)
	}
}
