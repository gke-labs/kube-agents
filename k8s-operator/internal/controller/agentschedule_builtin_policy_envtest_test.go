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
	"path/filepath"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The policy source the chart's copy is generated from, relative to this package.
var agentScheduleBuiltinPolicyFile = filepath.Join("..", "..", "config", "admission", "agentschedule-builtin-policy.yaml")

const (
	// The kustomize path's operator identity, which the source file names.
	agentScheduleBuiltinPolicyNamespace  = "kubeagents-system"
	agentScheduleBuiltinPolicyOperator   = "kube-agents-operator-sa"
	agentScheduleBuiltinPolicyActivation = 30 * time.Second
)

// TestTheBuiltinPolicyLeavesBuiltinToTheOperator runs the shipped policy in a
// real API server (#6038). Anyone but the operator is refused when a write
// would set, change or remove a schedule's origin: builtin label, or add,
// change or remove its controller reference to a PlatformAgent, on create and
// on update. The operator may do all of it, and anyone may still annotate a
// built-in schedule (run-now, stop) or edit a user one.
func TestTheBuiltinPolicyLeavesBuiltinToTheOperator(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	admin, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	ns := agentScheduleBuiltinPolicyNamespace
	mustCreate(ctx, t, admin, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	for _, sa := range []string{agentScheduleBuiltinPolicyOperator, "kubeagents-controller", "schedule-writer"} {
		mustCreate(ctx, t, admin, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: sa, Namespace: ns}})
	}
	// Both identities may write schedules, so only the policy tells them apart.
	mustCreate(ctx, t, admin, &rbacv1.Role{
		ObjectMeta: metav1.ObjectMeta{Name: "schedules", Namespace: ns},
		Rules: []rbacv1.PolicyRule{{
			APIGroups: []string{agentv1alpha1.GroupVersion.Group}, Resources: []string{"agentschedules"},
			Verbs: []string{"get", "create", "update", "patch"},
		}},
	})
	mustCreate(ctx, t, admin, &rbacv1.RoleBinding{
		ObjectMeta: metav1.ObjectMeta{Name: "schedules", Namespace: ns},
		RoleRef:    rbacv1.RoleRef{APIGroup: rbacv1.GroupName, Kind: "Role", Name: "schedules"},
		Subjects: []rbacv1.Subject{
			{Kind: "ServiceAccount", Name: agentScheduleBuiltinPolicyOperator, Namespace: ns},
			{Kind: "ServiceAccount", Name: "kubeagents-controller", Namespace: ns},
			{Kind: "ServiceAccount", Name: "schedule-writer", Namespace: ns},
			{Kind: "ServiceAccount", Name: "generic-garbage-collector", Namespace: "kube-system"},
		},
	})
	applyAdmissionPolicyFile(ctx, t, admin, agentScheduleBuiltinPolicyFile)
	operator := impersonatedClientWith(t, cfg, scheme, ns, agentScheduleBuiltinPolicyOperator)
	kustomizeOperator := impersonatedClientWith(t, cfg, scheme, ns, "kubeagents-controller")
	writer := impersonatedClientWith(t, cfg, scheme, ns, "schedule-writer")
	collector := impersonatedClientWith(t, cfg, scheme, "kube-system", "generic-garbage-collector")

	controller := metav1.OwnerReference{
		APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent",
		Name: "platform-agent", UID: "agent-uid", Controller: ptr.To(true),
	}
	schedule := func(name string, builtin, controlled bool) *agentv1alpha1.AgentSchedule {
		s := &agentv1alpha1.AgentSchedule{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, Labels: map[string]string{}},
			Spec: agentv1alpha1.AgentScheduleSpec{
				Schedule: "0 * * * *", ProfileRef: agentv1alpha1.AgentScheduleProfileRef{Name: "fleet-audit"}, Prompt: "Run the audit.",
			},
		}
		if builtin {
			s.Labels[agentv1alpha1.AgentScheduleOriginLabel] = agentv1alpha1.AgentScheduleOriginBuiltin
		}
		if controlled {
			s.OwnerReferences = []metav1.OwnerReference{controller}
		}
		return s
	}
	refused := func(err error, what string) {
		t.Helper()
		if err == nil || !strings.Contains(err.Error(), "only the operator may") {
			t.Errorf("%s: %v, want the policy's refusal", what, err)
		}
	}
	allowed := func(err error, what string) {
		t.Helper()
		if err != nil {
			t.Errorf("%s: %v", what, err)
		}
	}

	// The policy takes a moment to be enforced after it's stored.
	deadline := time.Now().Add(agentScheduleBuiltinPolicyActivation)
	for {
		err := writer.Create(ctx, schedule("probe", true, true))
		if err != nil && strings.Contains(err.Error(), "only the operator may") {
			break
		}
		if err == nil {
			_ = admin.Delete(ctx, schedule("probe", true, true))
		}
		if time.Now().After(deadline) {
			t.Fatalf("the policy was not enforced within %s (last create: %v)", agentScheduleBuiltinPolicyActivation, err)
		}
		time.Sleep(250 * time.Millisecond)
	}

	// Create.
	refused(writer.Create(ctx, schedule("w-label", true, false)), "a writer's create labelled builtin")
	refused(writer.Create(ctx, schedule("w-ref", false, true)), "a writer's create with a PlatformAgent controller")
	disguised := schedule("w-disguised", false, false)
	disguised.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: "v1", Kind: "ConfigMap", Name: "not-an-agent", UID: controller.UID, Controller: ptr.To(true),
	}}
	refused(writer.Create(ctx, disguised), "a writer's create with a controller of another kind carrying the PlatformAgent's uid")
	allowed(writer.Create(ctx, schedule("w-user", false, false)), "a writer's user schedule")
	allowed(operator.Create(ctx, schedule("builtin", true, true)), "the operator's built-in schedule")

	// Update of a user schedule.
	update := func(c client.Client, name string, mutate func(*agentv1alpha1.AgentSchedule)) error {
		var s agentv1alpha1.AgentSchedule
		if err := admin.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &s); err != nil {
			t.Fatal(err)
		}
		if s.Labels == nil {
			s.Labels = map[string]string{}
		}
		mutate(&s)
		return c.Update(ctx, &s)
	}
	refused(update(writer, "w-user", func(s *agentv1alpha1.AgentSchedule) {
		s.Labels[agentv1alpha1.AgentScheduleOriginLabel] = agentv1alpha1.AgentScheduleOriginBuiltin
	}), "a writer adding the builtin label")
	refused(update(writer, "w-user", func(s *agentv1alpha1.AgentSchedule) {
		s.OwnerReferences = []metav1.OwnerReference{controller}
	}), "a writer adding a PlatformAgent controller")
	allowed(update(writer, "w-user", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Prompt = "Run it differently." }),
		"a writer editing a user schedule")

	// Update of the operator's built-in schedule.
	allowed(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) {
		s.Annotations = map[string]string{
			agentv1alpha1.AgentScheduleRunNowAnnotation: "t1",
		}
	}), "a writer annotating a built-in schedule")
	refused(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) {
		delete(s.Labels, agentv1alpha1.AgentScheduleOriginLabel)
	}), "a writer removing the builtin label")
	refused(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) {
		s.Labels[agentv1alpha1.AgentScheduleOriginLabel] = agentv1alpha1.AgentScheduleOriginUser
	}), "a writer changing the builtin label")
	refused(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) { s.OwnerReferences = nil }),
		"a writer removing the PlatformAgent controller")
	refused(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) {
		s.OwnerReferences[0].Name = "another-agent"
	}), "a writer changing the PlatformAgent controller")
	refused(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Prompt = "Run something else." }),
		"a writer changing a built-in schedule's spec")
	// Suspending is the exception: the one way to pause a built-in audit
	// until PlatformAgent.spec.schedules.overrides exists.
	allowed(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Suspend = true }),
		"a writer suspending a built-in schedule")
	refused(update(writer, "builtin", func(s *agentv1alpha1.AgentSchedule) {
		s.Spec.Suspend = false
		s.Spec.Schedule = "*/5 * * * *"
	}), "a writer changing a built-in schedule's spec beside its suspend")
	refused(writer.Patch(ctx, schedule("w-user", false, false), client.RawPatch(types.MergePatchType,
		[]byte(`{"metadata":{"labels":{"kubeagents.x-k8s.io/origin":"builtin"}}}`))), "a writer's merge patch adding the builtin label")
	allowed(update(operator, "builtin", func(s *agentv1alpha1.AgentSchedule) { s.Spec.Prompt = "Run the audit again." }),
		"the operator changing its built-in schedule's spec")
	allowed(kustomizeOperator.Create(ctx, schedule("builtin-kustomize", true, true)), "the kustomize path's operator creating a built-in schedule")
	// Orphaning a PlatformAgent's schedules: the garbage collector removes
	// their controller reference.
	allowed(update(collector, "builtin-kustomize", func(s *agentv1alpha1.AgentSchedule) { s.OwnerReferences = nil }),
		"the garbage collector orphaning a built-in schedule")
	allowed(update(operator, "builtin", func(s *agentv1alpha1.AgentSchedule) {
		s.Labels[agentv1alpha1.AgentScheduleOriginLabel] = agentv1alpha1.AgentScheduleOriginUser
		s.OwnerReferences = nil
	}), "the operator unmaking its built-in schedule")
}
