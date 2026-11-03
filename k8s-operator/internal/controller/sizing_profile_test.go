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

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// sizingAgent is a CR under the given profile with the given latched tier
// ("" for none) and cluster gauges (nil for absent).
func sizingAgent(profile, latched string, registered, monitored *int64) *agentv1alpha1.PlatformAgent {
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platformagent", Namespace: "kubeagents-system", Generation: 3},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{ClusterName: "cluster-a", Location: "us-central1", ProjectID: "example-project"},
			AgentSpec: agentv1alpha1.AgentSpec{
				Deployment: &agentv1alpha1.DeploymentSpec{SizingProfile: profile},
			},
		},
	}
	agent.Status.EffectiveSizingTier = latched
	agent.Status.Usage.ClustersRegistered = registered
	agent.Status.Usage.ClustersMonitored = monitored
	return agent
}

// sizingQuota is a ResourceQuota constraining requests.memory at hard with
// used already accounted.
func sizingQuota(name, hard, used string) *corev1.ResourceQuota {
	return &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
		Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourceRequestsMemory: resource.MustParse(hard)}},
		Status: corev1.ResourceQuotaStatus{
			Hard: corev1.ResourceList{corev1.ResourceRequestsMemory: resource.MustParse(hard)},
			Used: corev1.ResourceList{corev1.ResourceRequestsMemory: resource.MustParse(used)},
		},
	}
}

// resolveSizing runs resolveSizingTier against a fake client holding the CR
// and objs, and returns the CR as the client stores it afterwards.
func resolveSizing(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) (*agentv1alpha1.PlatformAgent, *record.FakeRecorder) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(append([]client.Object{agent}, objs...)...).
		WithStatusSubresource(agent).Build()
	recorder := record.NewFakeRecorder(8)
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder}
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatalf("resolveSizingTier: %v", err)
	}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("reading the CR back: %v", err)
	}
	return stored, recorder
}

func TestSizingTierForClusterCount(t *testing.T) {
	t.Parallel()
	for count, want := range map[int64]sizingTier{0: sizingTierSmall, 9: sizingTierSmall, 10: sizingTierMedium, 24: sizingTierMedium, 25: sizingTierFleet, 15000: sizingTierFleet} {
		if got := sizingTierForClusterCount(count); got != want {
			t.Errorf("sizingTierForClusterCount(%d) = %s, want %s", count, got, want)
		}
	}
}

func TestResolveSizingTierLatchesUpwardOnly(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name                  string
		profile, latched      string
		registered, monitored *int64
		want                  string
	}{
		{"small to medium at ten clusters", sizingProfileAuto, sizingProfileSmall, ptr.To[int64](10), ptr.To[int64](10), sizingProfileMedium},
		{"medium to fleet at twenty-five", sizingProfileAuto, sizingProfileMedium, ptr.To[int64](25), ptr.To[int64](20), sizingProfileFleet},
		{"small straight to fleet at twenty-five", sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil, sizingProfileFleet},
		{"nine clusters stay small", sizingProfileAuto, sizingProfileSmall, ptr.To[int64](9), ptr.To[int64](9), sizingProfileSmall},
		{"a falling count never lowers the tier", sizingProfileAuto, sizingProfileFleet, ptr.To[int64](3), ptr.To[int64](3), sizingProfileFleet},
		{"no reading leaves the latch", sizingProfileAuto, sizingProfileMedium, nil, nil, sizingProfileMedium},
		{"no reading and no latch is small", sizingProfileAuto, "", nil, nil, sizingProfileSmall},
		{"the larger gauge counts", sizingProfileAuto, sizingProfileSmall, ptr.To[int64](4), ptr.To[int64](12), sizingProfileMedium},
		{"an explicit tier is applied as written", sizingProfileFleet, "", ptr.To[int64](0), nil, sizingProfileFleet},
		{"an explicit small lowers a fleet latch", sizingProfileSmall, sizingProfileFleet, ptr.To[int64](100), nil, sizingProfileSmall},
		{"an absent field is small", "", "", ptr.To[int64](100), nil, sizingProfileSmall},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stored, _ := resolveSizing(t, sizingAgent(tc.profile, tc.latched, tc.registered, tc.monitored))
			if stored.Status.EffectiveSizingTier != tc.want {
				t.Fatalf("status.effectiveSizingTier = %q, want %q", stored.Status.EffectiveSizingTier, tc.want)
			}
			if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
				t.Fatalf("SizingCappedByQuota present with no quota: %+v", cond)
			}
		})
	}
}

func TestResolveSizingTierQuotaGate(t *testing.T) {
	t.Parallel()
	// The step from small to medium on a Standard cluster adds 3Gi of memory
	// requests, all of it the broker's (1Gi to 4Gi); the sidecar and the
	// shell keep their requests.
	t.Run("a short quota keeps the lower tier and sets the condition", func(t *testing.T) {
		t.Parallel()
		stored, recorder := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil),
			sizingQuota("platform-baseline-quota", "10Gi", "8Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != sizingCappedByQuotaReason || cond.ObservedGeneration != 3 {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
		for _, want := range []string{"earns the medium tier", "ResourceQuota platform-baseline-quota has no room on requests.memory", "step up from small", "the small tier stays"} {
			if !strings.Contains(cond.Message, want) {
				t.Errorf("message lacks %q: %s", want, cond.Message)
			}
		}
		if strings.Contains(cond.Message, "Gi") {
			t.Errorf("the message carries a quantity, which moves with the quota's used total: %s", cond.Message)
		}
		select {
		case event := <-recorder.Events:
			for _, want := range []string{sizingCappedByQuotaEventReason, "at 12 clusters", "2Gi of requests.memory free", "the step adds 3Gi"} {
				if !strings.Contains(event, want) {
					t.Errorf("event lacks %q: %s", want, event)
				}
			}
		default:
			t.Error("no event recorded for the capped promotion")
		}
	})
	t.Run("a quota whose used total moves while still short writes nothing", func(t *testing.T) {
		t.Parallel()
		// The first pass caps the tier with 2Gi free; a session pod then
		// returns 512Mi to the quota. Still short of the 3Gi the step adds, so
		// the condition stands as written and the pass neither writes status
		// nor records a second Warning.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		quota := sizingQuota("platform-baseline-quota", "10Gi", "8Gi")
		scheme := setupScheme()
		writes := 0
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, quota).WithStatusSubresource(agent).
			WithInterceptorFuncs(interceptor.Funcs{
				SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
					writes++
					return c.SubResource(subResourceName).Update(ctx, obj, opts...)
				},
			}).Build()
		recorder := record.NewFakeRecorder(8)
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("first pass: %v", err)
		}
		quota.Status.Used[corev1.ResourceRequestsMemory] = resource.MustParse("7680Mi")
		if err := cl.Update(context.Background(), quota); err != nil {
			t.Fatalf("moving the quota's used total: %v", err)
		}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("second pass: %v", err)
		}
		if writes != 1 {
			t.Errorf("status written %d times across the two passes, want 1", writes)
		}
		if len(recorder.Events) != 1 {
			t.Errorf("%d events recorded, want the first pass's one", len(recorder.Events))
		}
	})
	t.Run("a quota with headroom lets the promotion through and clears the condition", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		setSizingCappedByQuotaCondition(agent, "left over from an earlier pass", metav1.Now())
		stored, _ := resolveSizing(t, agent, sizingQuota("platform-baseline-quota", "10Gi", "1Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota still present: %+v", cond)
		}
	})
	t.Run("a quota on a dimension the step does not move is not read", func(t *testing.T) {
		t.Parallel()
		quota := &corev1.ResourceQuota{
			ObjectMeta: metav1.ObjectMeta{Name: "pods-only", Namespace: "kubeagents-system"},
			Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourcePods: resource.MustParse("1")}},
			Status:     corev1.ResourceQuotaStatus{Used: corev1.ResourceList{corev1.ResourcePods: resource.MustParse("1")}},
		}
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](30), nil), quota)
		if stored.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet", stored.Status.EffectiveSizingTier)
		}
	})
	t.Run("an explicit tier skips the gate", func(t *testing.T) {
		t.Parallel()
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileFleet, "", nil, nil), sizingQuota("platform-baseline-quota", "1Gi", "1Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota set under an explicit tier: %+v", cond)
		}
	})
}

func TestResolveSizingTierWritesOnlyOnChange(t *testing.T) {
	t.Parallel()
	agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
	scheme := setupScheme()
	writes := 0
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				writes++
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatalf("resolveSizingTier: %v", err)
	}
	if writes != 0 {
		t.Fatalf("a settled tier wrote the status %d times", writes)
	}
	agent.Status.Usage.ClustersRegistered = ptr.To[int64](40)
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatalf("resolveSizingTier: %v", err)
	}
	if writes != 1 || agent.Status.EffectiveSizingTier != sizingProfileFleet {
		t.Fatalf("after the count rose: %d writes, tier %q; want 1 and fleet", writes, agent.Status.EffectiveSizingTier)
	}
}

func TestSizingStepBetweenSumsTheThreeContainers(t *testing.T) {
	t.Parallel()
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	step := sizingStepBetween(agent, false, sizingTierSmall, sizingTierFleet)
	// Requests: the broker 500m -> 4 CPU and 1Gi -> 8Gi; the sidecar and the
	// shell keep theirs. Limits: CPU 2 -> 8 on the broker; memory 2Gi -> 8Gi
	// on the broker, 2Gi -> 8Gi on the sidecar, 2Gi -> 4Gi on the shell;
	// ephemeral-storage unchanged on both containers that declare it, so it
	// reads zero on both sides.
	assertQuantity(t, step.requests, corev1.ResourceCPU, "3500m")
	assertQuantity(t, step.requests, corev1.ResourceMemory, "7Gi")
	assertQuantity(t, step.limits, corev1.ResourceCPU, "6")
	assertQuantity(t, step.limits, corev1.ResourceMemory, "14Gi")
	if q := step.requests[corev1.ResourceEphemeralStorage]; !q.IsZero() {
		t.Errorf("ephemeral-storage requests moved by %s", q.String())
	}
	if q := step.limits[corev1.ResourceEphemeralStorage]; !q.IsZero() {
		t.Errorf("ephemeral-storage limits moved by %s", q.String())
	}
	// On Autopilot the broker's small row already requests 2 CPU and 2Gi.
	autopilot := sizingStepBetween(agent, true, sizingTierSmall, sizingTierMedium)
	if q := autopilot.requests[corev1.ResourceCPU]; !q.IsZero() {
		t.Errorf("Autopilot small -> medium moved the CPU request by %s", q.String())
	}
	assertQuantity(t, autopilot.requests, corev1.ResourceMemory, "2Gi")
}

func TestSizingTierRows(t *testing.T) {
	t.Parallel()
	// The small row is the operator's defaults, which the goldens carry.
	small := resolveCredentialProxyResources(nil, sizingTierSmall)
	assertQuantity(t, small.Requests, corev1.ResourceCPU, credentialProxyCPURequest)
	assertQuantity(t, small.Requests, corev1.ResourceMemory, credentialProxyMemoryRequest)
	assertQuantity(t, small.Limits, corev1.ResourceCPU, credentialProxyCPULimit)
	assertQuantity(t, small.Limits, corev1.ResourceMemory, credentialProxyMemoryLimit)
	assertQuantity(t, small.Limits, corev1.ResourceEphemeralStorage, credentialProxyEphemeralStorageLimit)
	assertQuantity(t, resolveAgentAPIAuthResources(nil, sizingTierSmall).Limits, corev1.ResourceMemory, agentAPIAuthMemoryLimit)
	assertQuantity(t, resolveShellSandboxResources(nil, sizingTierSmall).Limits, corev1.ResourceMemory, shellSandboxMemoryLimit)

	medium := resolveCredentialProxyResources(nil, sizingTierMedium)
	assertQuantity(t, medium.Requests, corev1.ResourceCPU, "1")
	assertQuantity(t, medium.Requests, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, medium.Limits, corev1.ResourceCPU, "2")
	assertQuantity(t, medium.Limits, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, medium.Limits, corev1.ResourceEphemeralStorage, "8Gi")
	mediumAutopilot := resolveCredentialProxyResourcesFor(nil, true, sizingTierMedium)
	assertQuantity(t, mediumAutopilot.Requests, corev1.ResourceCPU, "2")
	assertQuantity(t, mediumAutopilot.Requests, corev1.ResourceMemory, "4Gi")
	auth := resolveAgentAPIAuthResources(nil, sizingTierMedium)
	assertQuantity(t, auth.Requests, corev1.ResourceCPU, agentAPIAuthCPURequest)
	assertQuantity(t, auth.Limits, corev1.ResourceMemory, "4Gi")
	assertQuantity(t, resolveShellSandboxResources(nil, sizingTierMedium).Limits, corev1.ResourceMemory, shellSandboxMemoryLimit)

	fleet := resolveCredentialProxyResources(nil, sizingTierFleet)
	assertQuantity(t, fleet.Requests, corev1.ResourceCPU, "4")
	assertQuantity(t, fleet.Requests, corev1.ResourceMemory, "8Gi")
	assertQuantity(t, fleet.Limits, corev1.ResourceCPU, "8")
	assertQuantity(t, fleet.Limits, corev1.ResourceMemory, "8Gi")
	assertQuantity(t, fleet.Limits, corev1.ResourceEphemeralStorage, "8Gi")
	fleetAutopilot := resolveCredentialProxyResourcesFor(nil, true, sizingTierFleet)
	assertQuantity(t, fleetAutopilot.Requests, corev1.ResourceCPU, "4")
	assertQuantity(t, resolveAgentAPIAuthResources(nil, sizingTierFleet).Limits, corev1.ResourceMemory, "8Gi")
	shell := resolveShellSandboxResources(nil, sizingTierFleet)
	assertQuantity(t, shell.Requests, corev1.ResourceMemory, shellSandboxMemoryRequest)
	assertQuantity(t, shell.Limits, corev1.ResourceMemory, "4Gi")
}

func TestSizingExplicitKeysWinOverThePreset(t *testing.T) {
	t.Parallel()
	proxy := resolveCredentialProxyResources(&agentv1alpha1.DeploymentSpec{
		SizingProfile:   sizingProfileFleet,
		CredentialProxy: &agentv1alpha1.CredentialProxySpec{Resources: &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")}}},
	}, sizingTierFleet)
	assertQuantity(t, proxy.Limits, corev1.ResourceMemory, "3Gi")
	// The limit set alone below the row's request lowers the request to it.
	assertQuantity(t, proxy.Requests, corev1.ResourceMemory, "3Gi")
	assertQuantity(t, proxy.Requests, corev1.ResourceCPU, "4")
	assertQuantity(t, proxy.Limits, corev1.ResourceCPU, "8")

	auth := resolveAgentAPIAuthResources(&agentv1alpha1.DeploymentSpec{
		AgentAPIAuth: &agentv1alpha1.AgentAPIAuthSpec{Resources: &corev1.ResourceRequirements{Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")}}},
	}, sizingTierFleet)
	assertQuantity(t, auth.Limits, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, auth.Limits, corev1.ResourceCPU, agentAPIAuthCPULimit)

	sandbox := sandboxAgentWithResources(&corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")}})
	shell := resolveShellSandboxResources(sandbox, sizingTierFleet)
	assertQuantity(t, shell.Requests, corev1.ResourceMemory, "1Gi")
	assertQuantity(t, shell.Limits, corev1.ResourceMemory, "4Gi")
}

func TestRenderedSizingTier(t *testing.T) {
	t.Parallel()
	if got := renderedSizingTier(sizingAgent(sizingProfileAuto, sizingProfileMedium, nil, nil)); got != sizingTierMedium {
		t.Errorf("auto with a medium latch renders %s", got)
	}
	if got := renderedSizingTier(sizingAgent(sizingProfileAuto, "", nil, nil)); got != sizingTierSmall {
		t.Errorf("auto without a latch renders %s", got)
	}
	if got := renderedSizingTier(sizingAgent(sizingProfileFleet, sizingProfileSmall, nil, nil)); got != sizingTierFleet {
		t.Errorf("an explicit fleet with a small latch renders %s", got)
	}
	if got := renderedSizingTier(nil); got != sizingTierSmall {
		t.Errorf("a nil agent renders %s", got)
	}
}

func TestSizingValidationFollowsTheRenderedTier(t *testing.T) {
	t.Parallel()
	// A 3 CPU request crosses the small row's 2 CPU limit and sits under the
	// fleet row's 8. Admission under auto checks the small row, so it refuses;
	// the reconciler checks the latched fleet row, so it renders.
	agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, nil, nil)
	agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{Resources: &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("3")},
	}}
	if errs, _ := ValidateCredentialProxyResources(agent.Spec.Deployment, credentialProxyResourcesPath); len(errs) == 0 {
		t.Error("admission under auto admitted a 3 CPU request against the small row")
	}
	if refusal, _ := credentialProxyResourcesRefusal(agent); refusal != "" {
		t.Errorf("the reconciler refused against the fleet latch: %s", refusal)
	}
	agent.Spec.Deployment.SizingProfile = sizingProfileFleet
	if errs, _ := ValidateCredentialProxyResources(agent.Spec.Deployment, credentialProxyResourcesPath); len(errs) != 0 {
		t.Errorf("admission under an explicit fleet refused: %v", errs)
	}

	// The shell follows the same rule: a 3Gi memory request alone crosses
	// the small row's 2Gi limit and sits under the fleet row's 4Gi.
	shell := sizingAgent(sizingProfileAuto, sizingProfileFleet, nil, nil)
	shell.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Resources: &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
	}}}
	if errs, _ := ValidateShellSandboxResources(shell, shellSandboxResourcesPath); len(errs) == 0 {
		t.Error("admission under auto admitted a 3Gi shell request against the small row")
	}
	if refusal, _ := shellSandboxResourcesRefusal(shell); refusal != "" {
		t.Errorf("the reconciler refused the shell override against the fleet latch: %s", refusal)
	}
}
