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

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/client-go/tools/record"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func sandboxAgentWithResources(override *corev1.ResourceRequirements) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "test-agent", Namespace: "test-ns"},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ProjectID: "test-project", Location: "us-central1", ClusterName: "test-cluster"},
		},
	}
	if override != nil {
		agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{
			ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Resources: override},
		}
	}
	return agent
}

// shellContainerOf is the shell container of the sandbox StatefulSet the
// operator renders for agent.
func shellContainerOf(t *testing.T, agent *agentv1alpha1.PlatformAgent) corev1.Container {
	t.Helper()
	sts := buildShellSandboxStatefulSet(agent, shellSandboxAuthorizedKeysSecretName(agent), credentialProxySandboxURL(agent), "settings-hash")
	for _, c := range sts.Spec.Template.Spec.Containers {
		if c.Name == "shell" {
			return c
		}
	}
	t.Fatal("no shell container in the sandbox StatefulSet")
	return corev1.Container{}
}

// TestShellSandboxResourcesDefaultToTheOperatorsValues: a nil override renders
// exactly the literals the shell carried before the field existed, which the
// goldens and the chart's footprint carry; nothing else is declared.
func TestShellSandboxResourcesDefaultToTheOperatorsValues(t *testing.T) {
	for _, agent := range []*agentv1alpha1.PlatformAgent{
		nil,
		{},
		sandboxAgentWithResources(nil),
		{Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{Experimental: &agentv1alpha1.ExperimentalSpec{ShellSandbox: &agentv1alpha1.ShellSandboxSpec{}}}}},
	} {
		got := resolveShellSandboxResources(agent)
		assertQuantity(t, got.Requests, corev1.ResourceCPU, "200m")
		assertQuantity(t, got.Requests, corev1.ResourceMemory, "512Mi")
		assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
		assertQuantity(t, got.Limits, corev1.ResourceMemory, "2Gi")
		if len(got.Requests) != 2 || len(got.Limits) != 2 {
			t.Errorf("default render carries %d requests and %d limits, want 2 and 2", len(got.Requests), len(got.Limits))
		}
	}
	container := shellContainerOf(t, sandboxAgentWithResources(nil))
	if container.Resources.Limits.Memory().Cmp(resource.MustParse("2Gi")) != 0 {
		t.Errorf("shell memory limit = %s, want the 2Gi default", container.Resources.Limits.Memory())
	}
}

// TestShellSandboxMemoryOverrideKeepsTheCPUDefaults is the case the field
// exists for: a CR raises the memory pair and nothing else, and both CPU
// figures survive.
func TestShellSandboxMemoryOverrideKeepsTheCPUDefaults(t *testing.T) {
	got := shellContainerOf(t, sandboxAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
	})).Resources
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "3Gi")
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "200m")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "2")
}

// TestShellSandboxFullOverrideReplacesEveryKey: a CR that states every key gets
// every key, an ephemeral-storage limit the defaults never declared included.
func TestShellSandboxFullOverrideReplacesEveryKey(t *testing.T) {
	got := shellContainerOf(t, sandboxAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("4Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("4"), corev1.ResourceMemory: resource.MustParse("8Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("10Gi")},
	})).Resources
	assertQuantity(t, got.Requests, corev1.ResourceCPU, "1")
	assertQuantity(t, got.Requests, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, got.Limits, corev1.ResourceCPU, "4")
	assertQuantity(t, got.Limits, corev1.ResourceMemory, "8Gi")
	assertQuantity(t, got.Limits, corev1.ResourceEphemeralStorage, "10Gi")
}

// TestShellSandboxOverrideLeavesTheInitContainerAlone: the image-trees init
// container keeps its own fixed figures whatever the shell is given.
func TestShellSandboxOverrideLeavesTheInitContainerAlone(t *testing.T) {
	agent := sandboxAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("8Gi")},
	})
	got := buildShellSandboxImageTreesInitContainer(agent).Resources
	assertQuantity(t, got.Requests, corev1.ResourceCPU, shellSandboxImageTreesCPURequest)
	assertQuantity(t, got.Requests, corev1.ResourceMemory, shellSandboxImageTreesMemoryRequest)
	assertQuantity(t, got.Limits, corev1.ResourceCPU, shellSandboxImageTreesCPULimit)
	assertQuantity(t, got.Limits, corev1.ResourceMemory, shellSandboxImageTreesMemoryLimit)
}

// TestShellSandboxOverrideDoesNotAliasTheCR: the resolver deep-copies, so a
// later writer of the rendered list cannot mutate the CR's quantities.
func TestShellSandboxOverrideDoesNotAliasTheCR(t *testing.T) {
	override := &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")}}
	resolved := resolveShellSandboxResources(sandboxAgentWithResources(override))
	m := resolved.Limits[corev1.ResourceMemory]
	m.Add(resource.MustParse("1Gi"))
	resolved.Limits[corev1.ResourceMemory] = m
	crLimit := override.Limits[corev1.ResourceMemory]
	if crLimit.Cmp(resource.MustParse("4Gi")) != 0 {
		t.Errorf("the CR's limit changed to %s; the resolver aliased it", crLimit.String())
	}
}

func TestValidateShellSandboxResources(t *testing.T) {
	path := field.NewPath("spec", "harness", "experimental", "shellSandbox", "resources")
	cases := []struct {
		name     string
		override *corev1.ResourceRequirements
		wantErr  bool
		wantWarn bool
	}{
		{"nil override", nil, false, false},
		// Raising limits.memory alone warns that Autopilot without bursting
		// clamps the limit to the request; it is accepted.
		{"raise memory limit only", &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")}}, false, true},
		// The memory pair raised together with the 200m CPU request kept: 5 GiB
		// per vCPU, inside the band, and each limit has its request.
		{"raise the memory pair in band", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
		}, false, false},
		{"unknown name", &corev1.ResourceRequirements{Limits: corev1.ResourceList{"nvidia.com/gpu": resource.MustParse("1")}}, true, false},
		{"claims", &corev1.ResourceRequirements{Claims: []corev1.ResourceClaim{{Name: "c"}}}, true, false},
		{"negative", &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("-1Gi")}}, true, false},
		{"zero limit", &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("0")}}, true, false},
		{"request above limit", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("5Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		}, true, false},
		// requests.memory above the 2Gi default limit the override does not raise.
		{"request above the default limit", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
		}, true, false},
		// 4 GiB on the default 200m request: 20 GiB per vCPU, outside the band.
		{"requests leave the autopilot band", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
		}, false, true},
		{"limit without its request", &corev1.ResourceRequirements{
			Limits: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("4")},
		}, false, true},
		// A small positive limit at or above its request is accepted: nothing in
		// the shell derives an admission count from the limit, so there is no
		// floor. 256Mi on the 200m default is 1.25 GiB per vCPU, inside the band.
		{"no memory floor", &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("256Mi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("300Mi")},
		}, false, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			errs, warnings := ValidateShellSandboxResources(sandboxAgentWithResources(tc.override), path)
			if tc.wantErr != (len(errs) > 0) {
				t.Errorf("errs = %v; want error: %v", errs, tc.wantErr)
			}
			if tc.wantWarn != (len(warnings) > 0) {
				t.Errorf("warnings = %v; want warning: %v", warnings, tc.wantWarn)
			}
		})
	}
}

// TestValidateShellSandboxResourcesNamesTheShell: the refusals that name the
// container say "shell", not the credential proxy or the sidecar.
func TestValidateShellSandboxResourcesNamesTheShell(t *testing.T) {
	errs, _ := ValidateShellSandboxResources(sandboxAgentWithResources(&corev1.ResourceRequirements{
		Limits: corev1.ResourceList{"nvidia.com/gpu": resource.MustParse("1")},
	}), shellSandboxResourcesPath)
	if len(errs) != 1 {
		t.Fatalf("errs = %v, want one refusal", errs)
	}
	msg := errs[0].Error()
	for _, want := range []string{"spec.harness.experimental.shellSandbox.resources.limits.nvidia.com/gpu", "the shell container accepts cpu, memory and ephemeral-storage only"} {
		if !strings.Contains(msg, want) {
			t.Errorf("refusal %q does not say %q", msg, want)
		}
	}
}

// TestReconcileInvalidShellSandboxResources pins the webhook-off reconciler
// path for a refused spec.harness.experimental.shellSandbox.resources: the
// StatefulSet is applied regardless, at the operator's defaults, the CR
// reports Degraded with the reason while Ready and the phase keep what the
// workloads say, and a Warning event names the field. A request above its
// limit is the refusal the API server would also reject, so the test proves the
// operator ignores it rather than writing it.
func TestReconcileInvalidShellSandboxResources(t *testing.T) {
	scheme := setupScheme()
	agent := sandboxAgentWithResources(&corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("5Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	})
	agent.Name = "test-agent-sandbox-resources"
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	recorder := record.NewFakeRecorder(64)
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: recorder}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	ctx := context.Background()
	for i := range 2 {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i+1, err)
		}
	}

	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	degraded := meta.FindStatusCondition(updated.Status.Conditions, "Degraded")
	if degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonInvalidShellSandboxResources {
		t.Fatalf("Degraded = %v, want True/%s", degraded, conditionReasonInvalidShellSandboxResources)
	}
	if !strings.Contains(degraded.Message, "spec.harness.experimental.shellSandbox.resources.requests.memory") {
		t.Errorf("Degraded message %q does not name the crossed field", degraded.Message)
	}
	if ready := meta.FindStatusCondition(updated.Status.Conditions, "Ready"); ready != nil && ready.Reason == conditionReasonInvalidShellSandboxResources {
		t.Errorf("Ready reason = %s; the refusal is reported on Degraded only", ready.Reason)
	}
	if updated.Status.Phase == "Degraded" {
		t.Errorf("Status.Phase = Degraded; the shell runs at defaults, so the refusal is reported on the Degraded condition only")
	}

	sts := &appsv1.StatefulSet{}
	if err := cl.Get(ctx, client.ObjectKey{Name: shellSandboxName(agent), Namespace: agent.Namespace}, sts); err != nil {
		t.Fatalf("the sandbox StatefulSet was not applied: %v", err)
	}
	var shell *corev1.Container
	for i := range sts.Spec.Template.Spec.Containers {
		if sts.Spec.Template.Spec.Containers[i].Name == "shell" {
			shell = &sts.Spec.Template.Spec.Containers[i]
		}
	}
	if shell == nil {
		t.Fatal("no shell container in the sandbox StatefulSet")
	}
	wantResources := resolveShellSandboxResources(nil)
	if !equality.Semantic.DeepEqual(shell.Resources, wantResources) {
		t.Errorf("the shell container's resources = %v, want the operator's defaults %v; a refused override is ignored", shell.Resources, wantResources)
	}

	// With every workload ready the agent reaches Ready while Degraded still
	// carries the refusal, as the two sibling tests assert for their fields.
	deps := &appsv1.DeploymentList{}
	if err := cl.List(ctx, deps, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatalf("list deployments: %v", err)
	}
	for i := range deps.Items {
		d := &deps.Items[i]
		want := int32(1)
		if d.Spec.Replicas != nil {
			want = *d.Spec.Replicas
		}
		d.Status.Replicas = want
		d.Status.ReadyReplicas = want
		if err := cl.Status().Update(ctx, d); err != nil {
			t.Fatalf("mark %s ready: %v", d.Name, err)
		}
	}
	stses := &appsv1.StatefulSetList{}
	if err := cl.List(ctx, stses, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatalf("list statefulsets: %v", err)
	}
	for i := range stses.Items {
		st := &stses.Items[i]
		want := int32(1)
		if st.Spec.Replicas != nil {
			want = *st.Spec.Replicas
		}
		st.Status.Replicas = want
		st.Status.ReadyReplicas = want
		if err := cl.Status().Update(ctx, st); err != nil {
			t.Fatalf("mark %s ready: %v", st.Name, err)
		}
	}
	r.APIReader = cl
	phase, err := r.updateStatusReady(ctx, updated, "", otlpSourceNone, r.resolveNetpolProfile(ctx, updated), a2aStateFrom(t, ctx, r, updated))
	if err != nil {
		t.Fatalf("updateStatusReady failed: %v", err)
	}
	if phase != "Ready" {
		t.Errorf("phase with every workload ready = %q, want Ready; a refused override must not hold the agent out of Ready", phase)
	}
	if degraded := meta.FindStatusCondition(updated.Status.Conditions, "Degraded"); degraded == nil || degraded.Status != metav1.ConditionTrue || degraded.Reason != conditionReasonInvalidShellSandboxResources {
		t.Errorf("Degraded with every workload ready = %v, want True/%s", degraded, conditionReasonInvalidShellSandboxResources)
	}

	close(recorder.Events)
	warned := false
	for event := range recorder.Events {
		if strings.HasPrefix(event, corev1.EventTypeWarning+" "+conditionReasonInvalidShellSandboxResources) && strings.Contains(event, "shellSandbox.resources") {
			warned = true
		}
	}
	if !warned {
		t.Errorf("no Warning %s event naming the field", conditionReasonInvalidShellSandboxResources)
	}
}

// TestShellSandboxRefusalRanksAfterTheOtherResourceRefusals: with a refused
// credential-proxy override beside a refused sandbox one, Degraded carries the
// proxy's reason; the sandbox's is reported once the higher-ranked one clears.
func TestShellSandboxRefusalRanksAfterTheOtherResourceRefusals(t *testing.T) {
	crossed := &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("5Gi")},
		Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("4Gi")},
	}
	agent := sandboxAgentWithResources(crossed)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{AgentAPIAuth: &agentv1alpha1.AgentAPIAuthSpec{Resources: crossed}}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent, shellSandboxKeysSecret(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(64)}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	ctx := context.Background()
	for i := range 2 {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d failed: %v", i+1, err)
		}
	}
	updated := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, updated); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	degraded := meta.FindStatusCondition(updated.Status.Conditions, "Degraded")
	if degraded == nil || degraded.Reason != conditionReasonInvalidAgentAPIAuthResources {
		t.Fatalf("Degraded = %v, want the higher-ranked %s", degraded, conditionReasonInvalidAgentAPIAuthResources)
	}
}
