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
	"errors"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/go-logr/logr/funcr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

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

// sizedWorkloadsAt are the three sized workloads as a pass at tier leaves
// them once its rollout has settled: each template's sized container at the
// tier's Standard render (the sidecar as the native init container it is),
// the controller's status at the current generation with the one replica
// updated and available. sizedWorkloadsOn is the same at either cluster
// type's render.
func sizedWorkloadsAt(agent *agentv1alpha1.PlatformAgent, tier sizingTier) []client.Object {
	return sizedWorkloadsOn(agent, tier, false)
}

func sizedWorkloadsOn(agent *agentv1alpha1.PlatformAgent, tier sizingTier, autopilot bool) []client.Object {
	meta := func(name string) metav1.ObjectMeta {
		return metav1.ObjectMeta{Name: name, Namespace: agent.Namespace, Generation: 1}
	}
	proxy := &appsv1.Deployment{
		ObjectMeta: meta(credentialProxyName(agent)),
		Spec: appsv1.DeploymentSpec{Replicas: ptr.To[int32](1), Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{
			{Name: credentialProxyContainerName, Resources: renderedCredentialProxyResourcesAt(agent.Spec.Deployment, autopilot, tier)},
		}}}},
		Status: appsv1.DeploymentStatus{ObservedGeneration: 1, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1},
	}
	gateway := &appsv1.Deployment{
		ObjectMeta: meta(agent.Name + gatewayWorkloadNameSuffix),
		Spec: appsv1.DeploymentSpec{Replicas: ptr.To[int32](1), Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{
			InitContainers: []corev1.Container{{Name: agentAPIAuthContainerName, Resources: renderedAgentAPIAuthResourcesAt(agent.Spec.Deployment, tier)}},
			Containers:     []corev1.Container{{Name: platformAgentContainerName}},
		}}},
		Status: appsv1.DeploymentStatus{ObservedGeneration: 1, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1},
	}
	shell := &appsv1.StatefulSet{
		ObjectMeta: meta(shellSandboxName(agent)),
		Spec: appsv1.StatefulSetSpec{Replicas: ptr.To[int32](1), Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{
			{Name: shellSandboxContainerName, Resources: renderedShellSandboxResourcesAt(agent, tier)},
		}}}},
		Status: appsv1.StatefulSetStatus{ObservedGeneration: 1, Replicas: 1, UpdatedReplicas: 1, ReadyReplicas: 1},
	}
	return []client.Object{proxy, gateway, shell}
}

// clampedToRequests rewrites the sized workloads' templates the way GKE
// Autopilot without Pod bursting admits them: every container's cpu and
// memory limit set equal to its request, and the ephemeral-storage limit
// copied into the request where a limit alone was set.
func clampedToRequests(objs []client.Object) []client.Object {
	clamp := func(containers []corev1.Container) {
		for i := range containers {
			rr := &containers[i].Resources
			if rr.Requests == nil {
				continue
			}
			for _, name := range []corev1.ResourceName{corev1.ResourceCPU, corev1.ResourceMemory} {
				if request, ok := rr.Requests[name]; ok {
					rr.Limits[name] = request.DeepCopy()
				}
			}
			if limit, ok := rr.Limits[corev1.ResourceEphemeralStorage]; ok {
				rr.Requests[corev1.ResourceEphemeralStorage] = limit.DeepCopy()
			}
		}
	}
	for _, obj := range objs {
		switch workload := obj.(type) {
		case *appsv1.Deployment:
			clamp(workload.Spec.Template.Spec.Containers)
			clamp(workload.Spec.Template.Spec.InitContainers)
		case *appsv1.StatefulSet:
			clamp(workload.Spec.Template.Spec.Containers)
		}
	}
	return objs
}

// sizingFixtures is the CR with its three workloads landed at the tier it
// renders, plus objs.
func sizingFixtures(agent *agentv1alpha1.PlatformAgent, objs ...client.Object) []client.Object {
	fixtures := append([]client.Object{agent}, sizedWorkloadsAt(agent, renderedSizingTier(agent))...)
	return append(fixtures, objs...)
}

// resolveSizing runs resolveSizingTier against a fake client holding the CR,
// its landed workloads (sizingFixtures) and objs, and returns the CR as the
// client stores it afterwards.
func resolveSizing(t *testing.T, agent *agentv1alpha1.PlatformAgent, objs ...client.Object) (*agentv1alpha1.PlatformAgent, *record.FakeRecorder) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(sizingFixtures(agent, objs...)...).
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
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(agent, quota)...).WithStatusSubresource(agent).
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
	t.Run("a pass with no reading keeps the condition and the returning reading does not record it again", func(t *testing.T) {
		t.Parallel()
		// The first pass caps the tier. The poller then clears both gauges
		// (two polls read no gateway), which is no reading, not a count that
		// stopped earning the step: the condition stands, nothing is written.
		// When the reading returns and the quota is as short as before, the
		// message is the one already on the CR, so no second write and no
		// second Warning. A count that falls under the threshold clears it.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		quota := sizingQuota("platform-baseline-quota", "10Gi", "8Gi")
		scheme := setupScheme()
		writes := 0
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(agent, quota)...).WithStatusSubresource(agent).
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
		agent.Status.Usage.ClustersRegistered = nil
		agent.Status.Usage.ClustersMonitored = nil
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("pass with no reading: %v", err)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond == nil || cond.Status != metav1.ConditionTrue {
			t.Fatalf("SizingCappedByQuota dropped by a pass with no reading: %+v", cond)
		}
		agent.Status.Usage.ClustersRegistered = ptr.To[int64](12)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("pass with the reading back: %v", err)
		}
		if writes != 1 {
			t.Errorf("status written %d times across the three passes, want the first pass's one", writes)
		}
		if len(recorder.Events) != 1 {
			t.Errorf("%d events recorded, want the first pass's one", len(recorder.Events))
		}
		agent.Status.Usage.ClustersRegistered = ptr.To[int64](3)
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("pass with the count fallen: %v", err)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil || writes != 2 {
			t.Fatalf("after the count fell under the threshold: condition %+v, %d writes; want absent and 2", cond, writes)
		}
	})
	t.Run("an override the earned row admits is counted against the quota", func(t *testing.T) {
		t.Parallel()
		// Webhook off, auto, latched small, a 3 CPU broker request: the small
		// row refuses it (2 CPU limit), so the pod runs the row's 500m; the
		// fleet row admits it, so the promotion asks the quota for 3 CPU. A
		// step read with the override merged on both sides would move
		// requests.cpu by zero and promote past a quota with 2 CPU free; the
		// step the reconciler renders adds 2500m, and the quota is short of
		// it. The medium row refuses the override too and renders 1 CPU, a
		// step of 500m the quota admits, so the tier reaches medium and the
		// cap names the fleet step.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("3")},
		}}
		quota := &corev1.ResourceQuota{
			ObjectMeta: metav1.ObjectMeta{Name: "cpu-quota", Namespace: "kubeagents-system"},
			Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{corev1.ResourceRequestsCPU: resource.MustParse("4")}},
			Status:     corev1.ResourceQuotaStatus{Used: corev1.ResourceList{corev1.ResourceRequestsCPU: resource.MustParse("2")}},
		}
		stored, recorder := resolveSizing(t, agent, quota)
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: the fleet step from the row to the admitted override was not counted, or the medium step was not tried", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || !strings.Contains(cond.Message, "earns the fleet tier") || !strings.Contains(cond.Message, "ResourceQuota cpu-quota has no room on requests.cpu") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
		// The tier change records first, then the cap with the fleet step's
		// figures.
		var events []string
		for len(recorder.Events) > 0 {
			events = append(events, <-recorder.Events)
		}
		if len(events) != 2 || !strings.Contains(events[1], "the step adds 2500m") {
			t.Errorf("events = %q, want the tier change then the cap carrying the step from the row to the override", events)
		}
	})
	t.Run("the event watcher switched off by spec clears the condition", func(t *testing.T) {
		t.Parallel()
		// The first pass caps the tier. The watcher is then switched off by
		// spec and the poller clears both gauges for as long as it stays off,
		// so a cap carried through that state would outlive the quota being
		// raised; auto cannot promote without a reading, so the pass clears
		// the condition and keeps the latch.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		quota := sizingQuota("platform-baseline-quota", "10Gi", "8Gi")
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(agent, quota)...).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8)}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("first pass: %v", err)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond == nil {
			t.Fatal("the first pass did not cap the tier")
		}
		agent.Spec.Harness.EventWatcher = &agentv1alpha1.EventWatcherSpec{Enabled: ptr.To(false)}
		agent.Status.Usage.ClustersRegistered = nil
		agent.Status.Usage.ClustersMonitored = nil
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("pass with the watcher off: %v", err)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Errorf("SizingCappedByQuota kept while the watcher is off by spec: %+v", cond)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Errorf("status.effectiveSizingTier = %q, want the small latch kept", agent.Status.EffectiveSizingTier)
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
	t.Run("a fleet step the quota refuses still takes the medium step it admits", func(t *testing.T) {
		t.Parallel()
		// Latched small at 25 clusters: the fleet step adds 7Gi of memory
		// requests (the broker's 1Gi to 8Gi), the medium step 3Gi. A quota
		// with 5Gi free refuses the first and admits the second, so the
		// tier reaches medium rather than waiting at small for the whole
		// fleet step, and the cap names fleet as earned and medium as the
		// tier that stays; the event carries the refused step's figures.
		stored, recorder := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil),
			sizingQuota("platform-baseline-quota", "10Gi", "5Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: the step the quota admits was not tried", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil {
			t.Fatal("SizingCappedByQuota absent although the fleet step was refused")
		}
		for _, want := range []string{"earns the fleet tier", "ResourceQuota platform-baseline-quota has no room on requests.memory", "step up from medium", "the medium tier stays"} {
			if !strings.Contains(cond.Message, want) {
				t.Errorf("message lacks %q: %s", want, cond.Message)
			}
		}
		// The tier change and the cap each record one event, in that order.
		var events []string
		for len(recorder.Events) > 0 {
			events = append(events, <-recorder.Events)
		}
		if len(events) != 2 || !strings.Contains(events[0], "sizing tier small -> medium") {
			t.Fatalf("events = %q, want the tier change then the cap", events)
		}
		for _, want := range []string{"at 25 clusters", "5Gi of requests.memory free", "the step adds 7Gi"} {
			if !strings.Contains(events[1], want) {
				t.Errorf("event lacks %q: %s", want, events[1])
			}
		}
	})
	t.Run("a quota short on every step reports the lowest one", func(t *testing.T) {
		t.Parallel()
		// 25 clusters with 2Gi free: both steps are refused, the tier stays
		// small, and the figures are the medium step's, the raise that
		// relieves the broker soonest.
		stored, recorder := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil),
			sizingQuota("platform-baseline-quota", "10Gi", "8Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || !strings.Contains(cond.Message, "earns the fleet tier") || !strings.Contains(cond.Message, "the small tier stays") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
		select {
		case event := <-recorder.Events:
			if !strings.Contains(event, "the step adds 3Gi") {
				t.Errorf("event does not carry the medium step: %s", event)
			}
		default:
			t.Error("no event recorded for the capped promotion")
		}
	})
	t.Run("a quota whose scopes cannot select the pods is not read", func(t *testing.T) {
		t.Parallel()
		// Each quota is short of the medium step on its own, and each is
		// scoped to pods these are not: admission never evaluates the sized
		// pods against it, so it can neither refuse the step nor be raised
		// to admit it, and reading it would cap the tier on a quota the
		// chart's preflight skips.
		forbidden := sizingQuota("forbid-cluster-services", "10Gi", "8Gi")
		forbidden.Spec.ScopeSelector = &corev1.ScopeSelector{MatchExpressions: []corev1.ScopedResourceSelectorRequirement{
			{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: corev1.ScopeSelectorOpIn, Values: []string{"cluster-services"}},
		}}
		jobs := sizingQuota("jobs", "10Gi", "8Gi")
		jobs.Spec.Scopes = []corev1.ResourceQuotaScope{corev1.ResourceQuotaScopeTerminating}
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil), forbidden, jobs)
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: a quota that cannot count the pods capped the tier", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota set by a quota whose scopes do not select the pods: %+v", cond)
		}
	})
	t.Run("a quota scoped to pods these are holds the tier", func(t *testing.T) {
		t.Parallel()
		// NotTerminating is the scope a namespace admin puts on long-running
		// pods, and the three sized pods are such pods, so the quota binds
		// and a short one caps the tier as an unscoped one does.
		quota := sizingQuota("long-running", "10Gi", "8Gi")
		quota.Spec.Scopes = []corev1.ResourceQuotaScope{corev1.ResourceQuotaScopeNotTerminating, corev1.ResourceQuotaScopeNotBestEffort}
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil), quota)
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small: a NotTerminating quota was skipped", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || !strings.Contains(cond.Message, "ResourceQuota long-running has no room on requests.memory") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
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
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(agent)...).WithStatusSubresource(agent).
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

// TestSizingStepReadsEachSideAsRendered pins the step's reading of an
// override against what the reconciler renders at each tier: a refused
// override is the row on that side, an admitted one the override.
func TestSizingStepReadsEachSideAsRendered(t *testing.T) {
	t.Parallel()
	// The broker's 3 CPU request crosses the small row's 2 CPU limit and sits
	// under the fleet row's 8; the shell's 3Gi memory request crosses the
	// small row's 2Gi limit and sits under the fleet row's 4Gi. Small renders
	// the rows (500m and 512Mi), fleet renders the overrides.
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	agent.Spec.Deployment.CredentialProxy = &agentv1alpha1.CredentialProxySpec{Resources: &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("3")},
	}}
	agent.Spec.Harness.Experimental = &agentv1alpha1.ExperimentalSpec{ShellSandbox: &agentv1alpha1.ShellSandboxSpec{Resources: &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("3Gi")},
	}}}
	small := renderedCredentialProxyResourcesAt(agent.Spec.Deployment, false, sizingTierSmall)
	assertQuantity(t, small.Requests, corev1.ResourceCPU, credentialProxyCPURequest)
	fleet := renderedCredentialProxyResourcesAt(agent.Spec.Deployment, false, sizingTierFleet)
	assertQuantity(t, fleet.Requests, corev1.ResourceCPU, "3")
	shellSmall := renderedShellSandboxResourcesAt(agent, sizingTierSmall)
	assertQuantity(t, shellSmall.Requests, corev1.ResourceMemory, shellSandboxMemoryRequest)
	shellFleet := renderedShellSandboxResourcesAt(agent, sizingTierFleet)
	assertQuantity(t, shellFleet.Requests, corev1.ResourceMemory, "3Gi")
	// The step: the broker 500m -> 3 CPU and 1Gi -> 8Gi, the shell 512Mi ->
	// 3Gi; merged on both sides, the two overrides would read as zero.
	step := sizingStepBetween(agent, false, sizingTierSmall, sizingTierFleet)
	assertQuantity(t, step.requests, corev1.ResourceCPU, "2500m")
	assertQuantity(t, step.requests, corev1.ResourceMemory, "9728Mi")
	// An override every row admits is the override on both sides, so the
	// step on its key is zero, as before.
	admitted := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	admitted.Spec.Deployment.AgentAPIAuth = &agentv1alpha1.AgentAPIAuthSpec{Resources: &corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
	}}
	sidecar := sizingStepBetween(admitted, false, sizingTierSmall, sizingTierFleet)
	// 6Gi from the broker and 2Gi from the shell; the sidecar's 1Gi limit
	// holds on both rows.
	assertQuantity(t, sidecar.limits, corev1.ResourceMemory, "8Gi")
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

func TestSizingRowsKeepTheClampReadable(t *testing.T) {
	t.Parallel()
	// sizedContainerCarries reads a limit equal to its request as Autopilot's
	// clamp on a name the row requests, and lets the requests side stand for
	// it. That reading is sound under bursting, where limits survive, only
	// while no row sets a limit equal to its request on a name the next row
	// raises the limit of alone: such a lower template would read as the
	// higher tier with its limit short. Each container's three rows, on
	// both cluster types, keep that shape.
	rows := map[string]func(tier sizingTier) corev1.ResourceRequirements{
		"proxy on Standard":  func(tier sizingTier) corev1.ResourceRequirements { return credentialProxyTierResources(tier, false) },
		"proxy on Autopilot": func(tier sizingTier) corev1.ResourceRequirements { return credentialProxyTierResources(tier, true) },
		"sidecar":            agentAPIAuthTierResources,
		"shell":              shellSandboxTierResources,
	}
	for container, row := range rows {
		for lower := sizingTierSmall; lower < sizingTierFleet; lower++ {
			from, to := row(lower), row(lower+1)
			for _, name := range sizingSettledLimits {
				fromLimit, fromRequest := from.Limits[name], from.Requests[name]
				toLimit, toRequest := to.Limits[name], to.Requests[name]
				raisesLimitAlone := toLimit.Cmp(fromLimit) > 0 && toRequest.Cmp(fromRequest) == 0
				if raisesLimitAlone && fromLimit.Cmp(fromRequest) == 0 {
					t.Errorf("%s %s -> %s raises the %s limit alone (%s -> %s) off a row whose limit equals its request: a clamped read cannot tell the two apart",
						container, lower, lower+1, name, fromLimit.String(), toLimit.String())
				}
			}
		}
	}
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

// TestResolveSizingTierUnderAPruningCRDWritesOncePerInterval pins the write
// gate under a served CRD that predates status.effectiveSizingTier. The API
// server drops the field from every write, so it reads back "" and a gate
// keyed on the field alone would write, and log a tier change, on every
// pass. The echo records the pruning and the gate treats "" as settled while
// the record is fresh; once the CRD carries the field the next probe lands it.
func TestResolveSizingTierUnderAPruningCRDWritesOncePerInterval(t *testing.T) {
	t.Parallel()
	agent := sizingAgent(sizingProfileAuto, "", ptr.To[int64](4), nil)
	scheme := setupScheme()
	writes := 0
	pruning := true
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(agent)...).WithStatusSubresource(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				writes++
				if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && pruning {
					pa.Status.EffectiveSizingTier = ""
				}
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	pass := func(what string) {
		t.Helper()
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("%s: %v", what, err)
		}
	}
	pass("first pass")
	if agent.Status.EffectiveSizingTier != "" {
		t.Fatalf("the fixture did not prune the field (got %q); the test is not exercising the skew", agent.Status.EffectiveSizingTier)
	}
	pass("second pass")
	pass("third pass")
	if writes != 1 {
		t.Errorf("%d status writes across three passes under a pruning CRD, want 1", writes)
	}
	key := client.ObjectKeyFromObject(agent)
	// The skew persists past the interval: one probe write, then quiet again.
	r.prunedSizingTier.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	pass("probe after the record expired")
	pass("pass after the probe")
	if writes != 2 {
		t.Errorf("%d writes after the record expired under a still-pruning CRD, want 2", writes)
	}
	// The CRD is applied. A fresh record still holds the field back; once it
	// expires the next pass writes once, lands it, and the record is cleared.
	pruning = false
	pass("pass under the applied CRD with a fresh record")
	if writes != 2 {
		t.Errorf("%d writes under the applied CRD with a fresh record, want 2", writes)
	}
	r.prunedSizingTier.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	pass("probe under the applied CRD")
	if writes != 3 || agent.Status.EffectiveSizingTier != sizingProfileSmall {
		t.Fatalf("after the record expired under the applied CRD: %d writes, tier %q; want 3 and small", writes, agent.Status.EffectiveSizingTier)
	}
	if r.sizingTierPruned(agent) {
		t.Error("the echo carried the field and the record was not cleared")
	}
	pass("settled pass")
	if writes != 3 {
		t.Errorf("a settled tier under the applied CRD wrote %d times, want 3", writes)
	}
}

func TestSizingQuotaBindsThePods(t *testing.T) {
	t.Parallel()
	// The fixed answer per scope: the sized pods are long-running, have
	// requests and no cross-namespace affinity. PriorityClass is read against
	// the class the live pods carry: none on a cluster without a
	// globalDefault PriorityClass, the default's name on one with it, and
	// with no pod to read the quota is taken as binding.
	scoped := func(scopes ...corev1.ResourceQuotaScope) *corev1.ResourceQuota {
		return &corev1.ResourceQuota{Spec: corev1.ResourceQuotaSpec{Scopes: scopes}}
	}
	selected := func(reqs ...corev1.ScopedResourceSelectorRequirement) *corev1.ResourceQuota {
		return &corev1.ResourceQuota{Spec: corev1.ResourceQuotaSpec{ScopeSelector: &corev1.ScopeSelector{MatchExpressions: reqs}}}
	}
	priority := func(op corev1.ScopeSelectorOperator, values ...string) corev1.ScopedResourceSelectorRequirement {
		return corev1.ScopedResourceSelectorRequirement{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: op, Values: values}
	}
	noClass := []string{""}
	defaultClass := []string{"cluster-default"}
	cases := []struct {
		name    string
		quota   *corev1.ResourceQuota
		classes []string
		want    bool
	}{
		{"unscoped", &corev1.ResourceQuota{}, noClass, true},
		{"NotTerminating", scoped(corev1.ResourceQuotaScopeNotTerminating), noClass, true},
		{"NotBestEffort", scoped(corev1.ResourceQuotaScopeNotBestEffort), noClass, true},
		{"both of them", scoped(corev1.ResourceQuotaScopeNotTerminating, corev1.ResourceQuotaScopeNotBestEffort), noClass, true},
		{"Terminating", scoped(corev1.ResourceQuotaScopeTerminating), noClass, false},
		{"BestEffort", scoped(corev1.ResourceQuotaScopeBestEffort), noClass, false},
		{"CrossNamespacePodAffinity", scoped(corev1.ResourceQuotaScopeCrossNamespacePodAffinity), noClass, false},
		{"VolumeAttributesClass", scoped(corev1.ResourceQuotaScopeVolumeAttributesClass), noClass, false},
		{"PriorityClass in scopes is Exists", scoped(corev1.ResourceQuotaScopePriorityClass), noClass, false},
		{"NotTerminating beside Terminating", scoped(corev1.ResourceQuotaScopeNotTerminating, corev1.ResourceQuotaScopeTerminating), noClass, false},
		{"priorityClass In", selected(priority(corev1.ScopeSelectorOpIn, "cluster-services")), noClass, false},
		{"priorityClass Exists", selected(priority(corev1.ScopeSelectorOpExists)), noClass, false},
		{"priorityClass NotIn", selected(priority(corev1.ScopeSelectorOpNotIn, "cluster-services")), noClass, true},
		{"priorityClass DoesNotExist", selected(priority(corev1.ScopeSelectorOpDoesNotExist)), noClass, true},
		{"a selector with a NotTerminating requirement", selected(corev1.ScopedResourceSelectorRequirement{ScopeName: corev1.ResourceQuotaScopeNotTerminating, Operator: corev1.ScopeSelectorOpExists}), noClass, true},
		{"a scope this operator does not know", scoped(corev1.ResourceQuotaScope("SomethingNewer")), noClass, true},
		// The pods carry the cluster's globalDefault class: In and Exists
		// on it bind, DoesNotExist and NotIn on it do not, In on another
		// class still does not.
		{"the default class: PriorityClass in scopes", scoped(corev1.ResourceQuotaScopePriorityClass), defaultClass, true},
		{"the default class: In it", selected(priority(corev1.ScopeSelectorOpIn, "cluster-default")), defaultClass, true},
		{"the default class: In another", selected(priority(corev1.ScopeSelectorOpIn, "cluster-services")), defaultClass, false},
		{"the default class: Exists", selected(priority(corev1.ScopeSelectorOpExists)), defaultClass, true},
		{"the default class: NotIn it", selected(priority(corev1.ScopeSelectorOpNotIn, "cluster-default")), defaultClass, false},
		{"the default class: NotIn another", selected(priority(corev1.ScopeSelectorOpNotIn, "cluster-services")), defaultClass, true},
		{"the default class: DoesNotExist", selected(priority(corev1.ScopeSelectorOpDoesNotExist)), defaultClass, false},
		// Pods from before and after the default was created: a requirement
		// that selects either binds.
		{"mixed classes: In the default", selected(priority(corev1.ScopeSelectorOpIn, "cluster-default")), []string{"", "cluster-default"}, true},
		{"mixed classes: DoesNotExist", selected(priority(corev1.ScopeSelectorOpDoesNotExist)), []string{"", "cluster-default"}, true},
		// No pod to read: every PriorityClass requirement binds, the side
		// that holds the tier; the fixed scopes keep their answers.
		{"no pod: In", selected(priority(corev1.ScopeSelectorOpIn, "cluster-services")), nil, true},
		{"no pod: Exists", selected(priority(corev1.ScopeSelectorOpExists)), nil, true},
		{"no pod: DoesNotExist", selected(priority(corev1.ScopeSelectorOpDoesNotExist)), nil, true},
		{"no pod: Terminating", scoped(corev1.ResourceQuotaScopeTerminating), nil, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := sizingQuotaBindsThePods(tc.quota, tc.classes); got != tc.want {
				t.Fatalf("sizingQuotaBindsThePods = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestSizingQuotaScopesPriorityClass(t *testing.T) {
	t.Parallel()
	inScopes := &corev1.ResourceQuota{Spec: corev1.ResourceQuotaSpec{Scopes: []corev1.ResourceQuotaScope{corev1.ResourceQuotaScopeNotTerminating, corev1.ResourceQuotaScopePriorityClass}}}
	inSelector := &corev1.ResourceQuota{Spec: corev1.ResourceQuotaSpec{ScopeSelector: &corev1.ScopeSelector{MatchExpressions: []corev1.ScopedResourceSelectorRequirement{
		{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: corev1.ScopeSelectorOpIn, Values: []string{"cluster-default"}},
	}}}}
	other := &corev1.ResourceQuota{Spec: corev1.ResourceQuotaSpec{Scopes: []corev1.ResourceQuotaScope{corev1.ResourceQuotaScopeNotTerminating}}}
	for name, tc := range map[string]struct {
		quota *corev1.ResourceQuota
		want  bool
	}{"in scopes": {inScopes, true}, "in the selector": {inSelector, true}, "another scope": {other, false}, "unscoped": {&corev1.ResourceQuota{}, false}} {
		if got := sizingQuotaScopesPriorityClass(tc.quota); got != tc.want {
			t.Errorf("%s: sizingQuotaScopesPriorityClass = %v, want %v", name, got, tc.want)
		}
	}
}

// admissionStamping is a fake client's Create that answers a dry-run Pod the
// way the API server's Priority plugin does: it fills class into
// spec.priorityClassName ("" for a cluster with no globalDefault
// PriorityClass) and persists nothing. refuse, when set, is what admission
// returns instead. Every dry-run Pod it saw is appended to seen.
func admissionStamping(class string, refuse error, seen *[]*corev1.Pod) interceptor.Funcs {
	return interceptor.Funcs{
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			pod, isPod := obj.(*corev1.Pod)
			options := &client.CreateOptions{}
			options.ApplyOptions(opts)
			if !isPod || !slices.Contains(options.DryRun, metav1.DryRunAll) {
				return c.Create(ctx, obj, opts...)
			}
			*seen = append(*seen, pod.DeepCopy())
			if refuse != nil {
				return refuse
			}
			if pod.Spec.PriorityClassName == "" {
				pod.Spec.PriorityClassName = class
			}
			return c.Create(ctx, obj, opts...)
		},
	}
}

func TestResolveSizingTierAsksAdmissionForThePriorityClass(t *testing.T) {
	t.Parallel()
	// The class a PriorityClass scope is read against is the one admission
	// gives the pod a step recreates, asked with a dry-run create of a Pod
	// built from the proxy's template. On a cluster with a globalDefault
	// class, created before or after the install, admission fills it in, so
	// a quota scoped In that class binds the pods and a short one caps the
	// tier, and a DoesNotExist quota does not bind them and is not read; on
	// a cluster with no default the answer is "", and the two swap. Each
	// quota is short of the medium step on its own.
	inDefault := sizingQuota("default-class", "10Gi", "8Gi")
	inDefault.Spec.ScopeSelector = &corev1.ScopeSelector{MatchExpressions: []corev1.ScopedResourceSelectorRequirement{
		{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: corev1.ScopeSelectorOpIn, Values: []string{"cluster-default"}},
	}}
	noClass := sizingQuota("no-class", "10Gi", "8Gi")
	noClass.Spec.ScopeSelector = &corev1.ScopeSelector{MatchExpressions: []corev1.ScopedResourceSelectorRequirement{
		{ScopeName: corev1.ResourceQuotaScopePriorityClass, Operator: corev1.ScopeSelectorOpDoesNotExist},
	}}
	unscoped := sizingQuota("unscoped", "10Gi", "8Gi")
	// run resolves one pass at 12 clusters from the small latch, the three
	// workloads settled at small (or the objs given), with quota in the
	// namespace and admission answering class or refusing with refuse, and
	// returns the CR as written and the dry-run Pods admission saw.
	run := func(t *testing.T, quota *corev1.ResourceQuota, class string, refuse error, workloads ...client.Object) (*agentv1alpha1.PlatformAgent, []*corev1.Pod) {
		t.Helper()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		if workloads == nil {
			workloads = sizedWorkloadsAt(agent, sizingTierSmall)
		}
		objs := append([]client.Object{agent, quota}, workloads...)
		var seen []*corev1.Pod
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).WithStatusSubresource(agent).
			WithInterceptorFuncs(admissionStamping(class, refuse, &seen)).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8)}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		pods := &corev1.PodList{}
		if err := cl.List(context.Background(), pods, client.InNamespace(agent.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(pods.Items) != 0 {
			t.Fatalf("%d Pods exist after the pass, want none: the dry run persisted", len(pods.Items))
		}
		return agent, seen
	}
	cappedBy := func(t *testing.T, agent *agentv1alpha1.PlatformAgent, quota, why string) {
		t.Helper()
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small: %s", agent.Status.EffectiveSizingTier, why)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || !strings.Contains(cond.Message, "ResourceQuota "+quota+" has no room on requests.memory") {
			t.Fatalf("SizingCappedByQuota = %+v", cond)
		}
	}
	promoted := func(t *testing.T, agent *agentv1alpha1.PlatformAgent, why string) {
		t.Helper()
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: %s", agent.Status.EffectiveSizingTier, why)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota set by a quota whose scope does not select the pods: %+v", cond)
		}
	}
	t.Run("a quota In the class admission stamps binds the pods and caps the tier", func(t *testing.T) {
		t.Parallel()
		agent, _ := run(t, inDefault, "cluster-default", nil)
		cappedBy(t, agent, "default-class", "a quota scoped to the class admission gives the pods was skipped")
	})
	t.Run("a DoesNotExist quota does not bind pods admission stamps a class on", func(t *testing.T) {
		t.Parallel()
		agent, _ := run(t, noClass, "cluster-default", nil)
		promoted(t, agent, "a quota that cannot count pods carrying a class capped the tier")
	})
	t.Run("with no default class an In quota does not bind and a DoesNotExist one does", func(t *testing.T) {
		t.Parallel()
		agent, _ := run(t, inDefault, "", nil)
		promoted(t, agent, "an In quota capped pods admission gives no class")
		agent, _ = run(t, noClass, "", nil)
		cappedBy(t, agent, "no-class", "a DoesNotExist quota was skipped with no class on the pods")
	})
	t.Run("the dry-run Pod is the proxy's template, asked once, and only with a PriorityClass scope", func(t *testing.T) {
		t.Parallel()
		agent, seen := run(t, inDefault, "cluster-default", nil)
		if len(seen) != 1 {
			t.Fatalf("admission saw %d dry-run Pods, want 1", len(seen))
		}
		pod := seen[0]
		if pod.Namespace != agent.Namespace || !strings.HasPrefix(pod.GenerateName, credentialProxyName(agent)+"-sizing-dry-run-") || pod.Name != "" {
			t.Fatalf("dry-run Pod named %q/%q (generateName %q), want the proxy's name and the dry-run suffix in the agent's namespace", pod.Namespace, pod.Name, pod.GenerateName)
		}
		if len(pod.Spec.Containers) != 1 || pod.Spec.Containers[0].Name != credentialProxyContainerName {
			t.Fatalf("dry-run Pod containers = %+v, want the proxy's template", pod.Spec.Containers)
		}
		if pod.Spec.PriorityClassName != "" {
			t.Fatalf("dry-run Pod sent with priorityClassName %q, want none: the class is admission's to fill in", pod.Spec.PriorityClassName)
		}
		_, seen = run(t, unscoped, "cluster-default", nil)
		if len(seen) != 0 {
			t.Fatalf("admission saw %d dry-run Pods for a quota with no PriorityClass scope, want none", len(seen))
		}
	})
	t.Run("a dry run admission refuses leaves a PriorityClass quota binding", func(t *testing.T) {
		t.Parallel()
		// A quota short of a whole extra pod refuses the dry run (the quota
		// plugin checks a dry run too): the class is unknown, and both an In
		// and a DoesNotExist quota are read as binding.
		refused := apierrors.NewForbidden(corev1.Resource("pods"), "", errors.New("exceeded quota: default-class"))
		agent, seen := run(t, inDefault, "cluster-default", refused)
		cappedBy(t, agent, "default-class", "an In quota was skipped after admission refused the dry run")
		if len(seen) != 1 {
			t.Fatalf("admission saw %d dry-run Pods, want 1", len(seen))
		}
		agent, _ = run(t, noClass, "", refused)
		cappedBy(t, agent, "no-class", "a DoesNotExist quota was skipped after admission refused the dry run")
	})
	t.Run("with no proxy Deployment to build the Pod from a PriorityClass quota binds", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierSmall)[1:]
		agent, seen := run(t, inDefault, "cluster-default", nil, workloads...)
		cappedBy(t, agent, "default-class", "an In quota was skipped with no template to ask admission about")
		if len(seen) != 0 {
			t.Fatalf("admission saw %d dry-run Pods with no proxy Deployment, want none", len(seen))
		}
	})
}

func TestResolveSizingTierTakesTheEarnedTierWithNoQuotaBeforeTheWorkloadsSettle(t *testing.T) {
	t.Parallel()
	// With no ResourceQuota in the namespace there is nothing to measure a
	// step against, so the earned tier is taken whatever the workloads'
	// state, as before the settled wait: here none of the three exists yet.
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithStatusSubresource(agent).Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8)}
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatalf("resolveSizingTier: %v", err)
	}
	if agent.Status.EffectiveSizingTier != sizingProfileMedium {
		t.Fatalf("status.effectiveSizingTier = %q, want medium: the settled wait held a step no quota measures", agent.Status.EffectiveSizingTier)
	}
}

func TestResolveSizingTierMeasuresNoStepUntilTheLatchedTierSettles(t *testing.T) {
	t.Parallel()
	// The walk a partial promotion takes. Latched small at 25 clusters with
	// one requests.memory quota, 10Gi hard and 5Gi used, the broker's 1Gi
	// inside it: the fleet step (7Gi) is refused, the medium step (3Gi)
	// admitted, so the first pass latches medium and caps fleet. The status
	// write re-enqueues the CR at once: before the workloads are re-rendered
	// at all, then while the proxy's Recreate rollout has deleted the old pod
	// and not yet created the new one, so the quota charges 4Gi and nothing
	// of the medium step, then with the new pod created and not yet
	// available. A pass in any of those measuring medium->fleet (4Gi) from
	// the latch against 6Gi free would promote to fleet, and admission would
	// then refuse the 8Gi pod with the 1Gi one gone. The pass measures
	// nothing until the medium workloads have settled; once they have, and
	// the quota charges them, the fleet step is short by the right figure,
	// and a raised quota admits it.
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
	quota := sizingQuota("platform-baseline-quota", "10Gi", "5Gi")
	scheme := setupScheme()
	writes := 0
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(sizingFixtures(agent, quota)...).WithStatusSubresource(agent).
		WithInterceptorFuncs(interceptor.Funcs{
			SubResourceUpdate: func(ctx context.Context, c client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
				// The CR's writes alone: the test moves the workloads' status
				// through the same writer.
				if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
					writes++
				}
				return c.SubResource(subResourceName).Update(ctx, obj, opts...)
			},
		}).Build()
	recorder := record.NewFakeRecorder(8)
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder}
	var logged strings.Builder
	ctx := logf.IntoContext(context.Background(), funcr.New(func(prefix, args string) { logged.WriteString(args + "\n") }, funcr.Options{}))
	pass := func(what string) {
		t.Helper()
		if err := r.resolveSizingTier(ctx, agent); err != nil {
			t.Fatalf("%s: %v", what, err)
		}
	}
	// held counts the passes so far that logged the hold naming workload,
	// the one line a pass that measures nothing leaves.
	held := func(workload string) int {
		n := 0
		for _, line := range strings.Split(logged.String(), "\n") {
			if strings.Contains(line, "sizing step held until the running tier's workloads settle") && strings.Contains(line, `"waitingOn"="`+workload+`"`) {
				n++
			}
		}
		return n
	}
	capped := func(what string) {
		t.Helper()
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if agent.Status.EffectiveSizingTier != sizingProfileMedium || cond == nil || !strings.Contains(cond.Message, "earns the fleet tier") || !strings.Contains(cond.Message, "the medium tier stays") {
			t.Fatalf("%s: status.effectiveSizingTier = %q, SizingCappedByQuota = %+v; want medium with the cap naming fleet", what, agent.Status.EffectiveSizingTier, cond)
		}
	}
	pass("first pass")
	capped("after the first pass")
	if writes != 1 {
		t.Fatalf("%d status writes after the first pass, want 1", writes)
	}
	if strings.Contains(logged.String(), "sizing step held") {
		t.Fatalf("the first pass, with the small workloads settled, logged a hold:\n%s", logged.String())
	}

	// The status write's echo lands before the pass has re-rendered any
	// workload: the three still carry the small render, rolled out.
	pass("second pass, the echo before the renders")
	capped("before the renders")
	if writes != 1 || len(recorder.Events) != 2 {
		t.Fatalf("before the renders: %d status writes and %d events, want the first pass's 1 and 2: the pass measured a step from the latch", writes, len(recorder.Events))
	}
	if held("Deployment/"+credentialProxyName(agent)) != 1 {
		t.Fatalf("the pass that measured nothing did not log the hold naming the proxy Deployment:\n%s", logged.String())
	}

	// Step 11 of the same pass re-rendered the proxy at medium and the
	// gateway's sidecar with it; the Deployment controller has observed
	// both, deleted the old proxy pod and not yet created the new one; the
	// quota controller has released the old pod's 1Gi.
	proxy := &appsv1.Deployment{}
	if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: credentialProxyName(agent)}, proxy); err != nil {
		t.Fatal(err)
	}
	proxy.Generation = 2
	proxy.Spec.Template.Spec.Containers[0].Resources = renderedCredentialProxyResourcesAt(agent.Spec.Deployment, false, sizingTierMedium)
	if err := cl.Update(ctx, proxy); err != nil {
		t.Fatal(err)
	}
	proxy.Status = appsv1.DeploymentStatus{ObservedGeneration: 2}
	if err := cl.Status().Update(ctx, proxy); err != nil {
		t.Fatal(err)
	}
	gateway := &appsv1.Deployment{}
	if err := cl.Get(ctx, client.ObjectKey{Namespace: agent.Namespace, Name: agent.Name + gatewayWorkloadNameSuffix}, gateway); err != nil {
		t.Fatal(err)
	}
	gateway.Generation = 2
	gateway.Spec.Template.Spec.InitContainers[0].Resources = renderedAgentAPIAuthResourcesAt(agent.Spec.Deployment, sizingTierMedium)
	if err := cl.Update(ctx, gateway); err != nil {
		t.Fatal(err)
	}
	gateway.Status = appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1}
	if err := cl.Status().Update(ctx, gateway); err != nil {
		t.Fatal(err)
	}
	quota.Status.Used[corev1.ResourceRequestsMemory] = resource.MustParse("4Gi")
	if err := cl.Update(ctx, quota); err != nil {
		t.Fatal(err)
	}
	pass("third pass, the proxy's Recreate window")
	capped("mid-rollout")
	if writes != 1 || len(recorder.Events) != 2 {
		t.Fatalf("mid-rollout: %d status writes and %d events, want the first pass's 1 and 2: the pass measured a step from the latch", writes, len(recorder.Events))
	}

	// The medium pod is admitted and not yet available: the quota charges
	// its 4Gi, 2Gi stay free, and the fleet step from medium adds 4Gi. The
	// workload has not settled, so nothing is measured.
	proxy.Status = appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 1, UpdatedReplicas: 1}
	if err := cl.Status().Update(ctx, proxy); err != nil {
		t.Fatal(err)
	}
	quota.Status.Used[corev1.ResourceRequestsMemory] = resource.MustParse("8Gi")
	if err := cl.Update(ctx, quota); err != nil {
		t.Fatal(err)
	}
	pass("fourth pass, the medium pod created and not yet available")
	capped("before the medium pod is available")
	if writes != 1 {
		t.Fatalf("%d status writes before the medium pod is available, want 1: the cap stood as written", writes)
	}

	// The medium pod is available: the workloads have settled, the fleet
	// step is measured, and it is still short, as written.
	proxy.Status = appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1}
	if err := cl.Status().Update(ctx, proxy); err != nil {
		t.Fatal(err)
	}
	pass("fifth pass, the medium workloads settled")
	capped("after the medium workloads settled")
	if writes != 1 {
		t.Fatalf("%d status writes after the medium workloads settled, want 1: the cap stood as written", writes)
	}
	if held("Deployment/"+credentialProxyName(agent)) != 3 {
		t.Fatalf("the three passes in the proxy's rollout logged the hold %d times, want 3, and the settled pass none:\n%s", held("Deployment/"+credentialProxyName(agent)), logged.String())
	}

	// The quota is raised by the fleet step: the pass takes it.
	quota.Spec.Hard[corev1.ResourceRequestsMemory] = resource.MustParse("12Gi")
	if err := cl.Update(ctx, quota); err != nil {
		t.Fatal(err)
	}
	pass("sixth pass, the quota raised")
	if agent.Status.EffectiveSizingTier != sizingProfileFleet || writes != 2 {
		t.Fatalf("after the quota was raised: tier %q, %d writes; want fleet and 2", agent.Status.EffectiveSizingTier, writes)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
		t.Fatalf("SizingCappedByQuota kept after the fleet step was admitted: %+v", cond)
	}
}

func TestSizingTierSettled(t *testing.T) {
	t.Parallel()
	// The three sized workloads at a tier's render, as the fixtures leave
	// them, and each way one of them has not settled; a workload not there
	// yet is settled at small and not above it; the Autopilot rewrite of a
	// template reads as settled, and a container the template does not carry
	// is not compared.
	type mutate func(proxy, gateway *appsv1.Deployment, shell *appsv1.StatefulSet) []client.Object
	all := func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{p, g, s} }
	cases := []struct {
		name      string
		tier      sizingTier
		autopilot bool
		mut       mutate
		want      bool
	}{
		{"all three settled at small", sizingTierSmall, false, all, true},
		{"all three settled at medium", sizingTierMedium, false, all, true},
		{"at small the proxy Deployment is not there yet", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{g, s} }, true},
		{"at small none of the three is there yet", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return nil }, true},
		{"at medium the proxy Deployment is not there", sizingTierMedium, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{g, s} }, false},
		{"at medium the shell StatefulSet is not there", sizingTierMedium, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{p, g} }, false},
		{"at medium the gateway is not there", sizingTierMedium, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{p, s} }, false},
		{"the proxy's controller has not observed the generation", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			p.Generation = 2
			return all(p, g, s)
		}, false},
		{"the proxy's Recreate window: the old pod gone, the new one not created", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			p.Status.UpdatedReplicas, p.Status.Replicas, p.Status.AvailableReplicas = 0, 0, 0
			return all(p, g, s)
		}, false},
		{"the proxy's new pod is created and not yet available", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			p.Status.AvailableReplicas = 0
			return all(p, g, s)
		}, false},
		{"the gateway's old pod is still draining", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			g.Status.Replicas = 2
			return all(p, g, s)
		}, false},
		{"the shell's new pod is not created yet", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			s.Status.UpdatedReplicas, s.Status.ReadyReplicas = 0, 0
			return all(p, g, s)
		}, false},
		{"the shell's new pod is not ready", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			s.Status.ReadyReplicas = 0
			return all(p, g, s)
		}, false},
		{"the proxy scaled to zero has nothing to settle", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			p.Spec.Replicas = ptr.To[int32](0)
			p.Status.UpdatedReplicas, p.Status.Replicas, p.Status.AvailableReplicas = 0, 0, 0
			return all(p, g, s)
		}, true},
		{"the gateway without the sidecar is not compared", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			g.Spec.Template.Spec.InitContainers = nil
			return all(p, g, s)
		}, true},
		{"the template Autopilot rewrote upward", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			rr := &p.Spec.Template.Spec.Containers[0].Resources
			rr.Requests[corev1.ResourceEphemeralStorage] = resource.MustParse("2Gi")
			rr.Requests[corev1.ResourceCPU] = resource.MustParse("2")
			rr.Requests[corev1.ResourceMemory] = resource.MustParse("2Gi")
			return all(p, g, s)
		}, true},
		{"the template Autopilot clamped to its requests settles small", sizingTierSmall, true, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			return clampedToRequests(all(p, g, s))
		}, true},
		{"the template Autopilot clamped to its requests settles medium", sizingTierMedium, true, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			return clampedToRequests(all(p, g, s))
		}, true},
		{"on Standard a limit equal to its request is the row's limit short", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			return clampedToRequests(all(p, g, s))
		}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			agent := sizingAgent(sizingProfileAuto, tc.tier.String(), ptr.To[int64](12), nil)
			workloads := sizedWorkloadsOn(agent, tc.tier, tc.autopilot)
			objs := tc.mut(workloads[0].(*appsv1.Deployment), workloads[1].(*appsv1.Deployment), workloads[2].(*appsv1.StatefulSet))
			scheme := setupScheme()
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, objs...)...).Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: tc.autopilot, autopilotResolved: true}
			if got := r.sizingTierSettled(context.Background(), agent, tc.tier); got != tc.want {
				t.Fatalf("sizingTierSettled = %v, want %v", got, tc.want)
			}
		})
	}
	t.Run("a small template Autopilot clamped has not settled medium", func(t *testing.T) {
		t.Parallel()
		// The broker's memory request moves with the step (2Gi to 4Gi on
		// Autopilot), and the clamp leaves the requests alone, so the
		// requests side still tells the small template from the medium one.
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, clampedToRequests(sizedWorkloadsOn(agent, sizingTierSmall, true))...)...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: true, autopilotResolved: true}
		if got := r.sizingTierUnsettledWorkload(context.Background(), agent, sizingTierMedium); got != "Deployment/"+credentialProxyName(agent) {
			t.Fatalf("sizingTierUnsettledWorkload = %q for a small template clamped at the medium latch, want the proxy Deployment", got)
		}
	})
	t.Run("rows that differ in a limit alone clamp to one template", func(t *testing.T) {
		t.Parallel()
		// The sidecar's small and medium rows differ in the memory limit
		// alone, and the shell has no medium row: clamped, the gateway and
		// the shell at the small render are what medium runs there, and
		// the proxy at the medium render settles the tier with them.
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
		medium := clampedToRequests(sizedWorkloadsOn(agent, sizingTierMedium, true))
		small := clampedToRequests(sizedWorkloadsOn(agent, sizingTierSmall, true))
		objs := []client.Object{agent, medium[0], small[1], small[2]}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: true, autopilotResolved: true}
		if got := r.sizingTierUnsettledWorkload(context.Background(), agent, sizingTierMedium); got != "" {
			t.Fatalf("sizingTierUnsettledWorkload = %q, want settled: the clamped small sidecar and shell are the medium ones", got)
		}
	})
	t.Run("a template at the lower tier has not settled the higher one", func(t *testing.T) {
		t.Parallel()
		// The latch moved to medium and the pass parked before the proxy was
		// re-rendered: the template is the small render, rolled out.
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](25), nil)
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, sizedWorkloadsAt(agent, sizingTierSmall)...)...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if r.sizingTierSettled(context.Background(), agent, sizingTierMedium) {
			t.Fatal("sizingTierSettled = true for a template rendered at small")
		}
		if !r.sizingTierSettled(context.Background(), agent, sizingTierSmall) {
			t.Fatal("sizingTierSettled = false for the tier the template carries")
		}
	})
}
