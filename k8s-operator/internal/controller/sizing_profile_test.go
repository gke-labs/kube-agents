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
	"encoding/json"
	"errors"
	"fmt"
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
// used already accounted; sizingQuotaOn the same on any hard key.
func sizingQuota(name, hard, used string) *corev1.ResourceQuota {
	return sizingQuotaOn(name, corev1.ResourceRequestsMemory, hard, used)
}

func sizingQuotaOn(name string, key corev1.ResourceName, hard, used string) *corev1.ResourceQuota {
	return &corev1.ResourceQuota{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
		Spec:       corev1.ResourceQuotaSpec{Hard: corev1.ResourceList{key: resource.MustParse(hard)}},
		Status: corev1.ResourceQuotaStatus{
			Hard: corev1.ResourceList{key: resource.MustParse(hard)},
			Used: corev1.ResourceList{key: resource.MustParse(used)},
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
			InitContainers: []corev1.Container{{Name: agentAPIAuthContainerName, Resources: renderedAgentAPIAuthResourcesAt(agent.Spec.Deployment, tier), RestartPolicy: ptr.To(corev1.ContainerRestartPolicyAlways)}},
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

// autopilotClamped rewrites the sized workloads' templates the way GKE
// Autopilot without Pod bursting admits them, record included: the figures
// clamped (clampedToRequests) and the autopilot.gke.io/resource-adjustment
// annotation on each template with the containers as they came in and as
// they left, in the shape Autopilot writes it. clampedToRequests alone is
// the figures without the record.
func autopilotClamped(objs []client.Object) []client.Object {
	recordOf := func(containers []corev1.Container) []autopilotAdjustedContainer {
		var entries []autopilotAdjustedContainer
		for _, container := range containers {
			entries = append(entries, autopilotAdjustedContainer{
				Name:     container.Name,
				Requests: container.Resources.DeepCopy().Requests,
				Limits:   container.Resources.DeepCopy().Limits,
			})
		}
		return entries
	}
	type recorded struct {
		Input    autopilotAdjustmentSide `json:"input"`
		Output   autopilotAdjustmentSide `json:"output"`
		Modified bool                    `json:"modified"`
	}
	annotate := func(template *corev1.PodTemplateSpec, input autopilotAdjustmentSide) {
		record := recorded{
			Input:    input,
			Output:   autopilotAdjustmentSide{Containers: recordOf(template.Spec.Containers), InitContainers: recordOf(template.Spec.InitContainers)},
			Modified: true,
		}
		raw, err := json.Marshal(record)
		if err != nil {
			panic(err)
		}
		if template.Annotations == nil {
			template.Annotations = map[string]string{}
		}
		template.Annotations[autopilotResourceAdjustmentAnnotation] = string(raw)
	}
	inputs := map[client.Object]autopilotAdjustmentSide{}
	for _, obj := range objs {
		switch workload := obj.(type) {
		case *appsv1.Deployment:
			inputs[obj] = autopilotAdjustmentSide{Containers: recordOf(workload.Spec.Template.Spec.Containers), InitContainers: recordOf(workload.Spec.Template.Spec.InitContainers)}
		case *appsv1.StatefulSet:
			inputs[obj] = autopilotAdjustmentSide{Containers: recordOf(workload.Spec.Template.Spec.Containers)}
		}
	}
	for _, obj := range clampedToRequests(objs) {
		switch workload := obj.(type) {
		case *appsv1.Deployment:
			annotate(&workload.Spec.Template, inputs[obj])
		case *appsv1.StatefulSet:
			annotate(&workload.Spec.Template, inputs[obj])
		}
	}
	return objs
}

// clampedToRequests rewrites the sized workloads' templates the way GKE
// Autopilot without Pod bursting admits them: every container's cpu and
// memory limit set equal to its request, and the ephemeral-storage limit
// copied into the request where a limit alone was set. No record of the
// rewrite is written; autopilotClamped adds it.
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
		setSizingCappedCondition(agent, sizingCap{reason: sizingCappedByQuotaReason, message: "left over from an earlier pass"}, metav1.Now())
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
		// fleet step, and the cap names fleet as earned, small as the tier
		// the refused step was measured from and medium as the tier that
		// stays; the event carries that step's figures, the small->fleet
		// ones the message names.
		stored, recorder := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil),
			sizingQuota("platform-baseline-quota", "10Gi", "5Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: the step the quota admits was not tried", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil {
			t.Fatal("SizingCappedByQuota absent although the fleet step was refused")
		}
		for _, want := range []string{"earns the fleet tier", "ResourceQuota platform-baseline-quota has no room on requests.memory", "step up from small", "the medium tier stays"} {
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
	step := sizingStepBetween(agent, false, false, sizingTierSmall, sizingTierFleet)
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
	autopilot := sizingStepBetween(agent, true, false, sizingTierSmall, sizingTierMedium)
	if q := autopilot.requests[corev1.ResourceCPU]; !q.IsZero() {
		t.Errorf("Autopilot small -> medium moved the CPU request by %s", q.String())
	}
	assertQuantity(t, autopilot.requests, corev1.ResourceMemory, "2Gi")
	// Autopilot without bursting (clamped): admission lowers every limit to
	// its request, so the limits side counts what it charges the clamped
	// pods, the requests' movement. Small -> fleet: the broker's 2 -> 4 CPU
	// and 2Gi -> 8Gi requests move both of its limits by the same; the
	// sidecar's 2Gi -> 8Gi and the shell's 2Gi -> 4Gi memory limits over
	// requests that do not move count nothing, where the rows' limits as
	// written would add 6Gi and 2Gi.
	fleet := sizingStepBetween(agent, true, true, sizingTierSmall, sizingTierFleet)
	assertQuantity(t, fleet.requests, corev1.ResourceCPU, "2")
	assertQuantity(t, fleet.requests, corev1.ResourceMemory, "6Gi")
	assertQuantity(t, fleet.limits, corev1.ResourceCPU, "2")
	assertQuantity(t, fleet.limits, corev1.ResourceMemory, "6Gi")
	asWritten := sizingStepBetween(agent, true, false, sizingTierSmall, sizingTierFleet)
	assertQuantity(t, asWritten.limits, corev1.ResourceCPU, "6")
	assertQuantity(t, asWritten.limits, corev1.ResourceMemory, "14Gi")
	// The clamp is the cluster's, not a name's: the broker's medium row
	// writes an equal 2/2 CPU pair itself, and the step from it still counts
	// the CPU limit on the requests' movement (2 -> 4) when the cluster
	// clamps, since the fleet pod's 8 CPU limit is lowered to its 4 CPU
	// request too; and as written (2 -> 8) when it does not, as under
	// bursting, where that equal pair is not a clamp and the fleet pod
	// keeps its 8.
	clampedFromMedium := sizingStepBetween(agent, true, true, sizingTierMedium, sizingTierFleet)
	assertQuantity(t, clampedFromMedium.limits, corev1.ResourceCPU, "2")
	burstingFromMedium := sizingStepBetween(agent, true, false, sizingTierMedium, sizingTierFleet)
	assertQuantity(t, burstingFromMedium.limits, corev1.ResourceCPU, "6")
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
	step := sizingStepBetween(agent, false, false, sizingTierSmall, sizingTierFleet)
	assertQuantity(t, step.requests, corev1.ResourceCPU, "2500m")
	assertQuantity(t, step.requests, corev1.ResourceMemory, "9728Mi")
	// An override every row admits is the override on both sides, so the
	// step on its key is zero, as before.
	admitted := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	admitted.Spec.Deployment.AgentAPIAuth = &agentv1alpha1.AgentAPIAuthSpec{Resources: &corev1.ResourceRequirements{
		Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
	}}
	sidecar := sizingStepBetween(admitted, false, false, sizingTierSmall, sizingTierFleet)
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

func TestAutopilotClampedLimitsReadsTheRecord(t *testing.T) {
	t.Parallel()
	// The clamp is read off Autopilot's own record alone, in the shape it
	// writes: a limit the output carries equal to its request where the
	// input carried a higher one. The template's figures say nothing on
	// their own, so an equal pair with no record, or one the record shows
	// came in equal (an override pinned to the lower row's limit), is not a
	// clamp; a key the record carries that this operator does not read is
	// ignored, and a record it cannot parse reads as absent with an error.
	recorded := `{"input":{"containers":[{"name":"envoy-credential-proxy","requests":{"cpu":"2","memory":"2Gi"},"limits":{"cpu":"2","ephemeral-storage":"2Gi","memory":"2Gi"}}],` +
		`"initContainers":[{"name":"agent-api-auth","requests":{"cpu":"150m","memory":"384Mi"},"limits":{"cpu":"1","ephemeral-storage":"1Gi","memory":"4Gi"}}]},` +
		`"output":{"containers":[{"limits":{"cpu":"2","ephemeral-storage":"2Gi","memory":"2Gi"},"requests":{"cpu":"2","ephemeral-storage":"2Gi","memory":"2Gi"},"name":"envoy-credential-proxy"}],` +
		`"initContainers":[{"limits":{"cpu":"150m","ephemeral-storage":"1Gi","memory":"384Mi"},"requests":{"cpu":"150m","ephemeral-storage":"1Gi","memory":"384Mi"},"name":"agent-api-auth"}]},"modified":true}`
	template := func(raw string) corev1.PodTemplateSpec {
		tpl := corev1.PodTemplateSpec{}
		if raw != "" {
			tpl.Annotations = map[string]string{autopilotResourceAdjustmentAnnotation: raw}
		}
		return tpl
	}
	sidecar, err := autopilotClampedLimits(template(recorded), agentAPIAuthContainerName)
	if err != nil {
		t.Fatalf("autopilotClampedLimits: %v", err)
	}
	if !sidecar[corev1.ResourceCPU] || !sidecar[corev1.ResourceMemory] || sidecar[corev1.ResourceEphemeralStorage] {
		t.Fatalf("sidecar clamped = %v, want cpu and memory (lowered to their requests) and not ephemeral-storage (copied into the request, the limit unchanged)", sidecar)
	}
	proxy, err := autopilotClampedLimits(template(recorded), credentialProxyContainerName)
	if err != nil || len(proxy) != 0 {
		t.Fatalf("proxy clamped = %v, %v; want nothing: its pairs came in equal and the record shows them unchanged", proxy, err)
	}
	if names, err := autopilotClampedLimits(template(recorded), shellSandboxContainerName); err != nil || len(names) != 0 {
		t.Fatalf("a container the record does not carry clamped = %v, %v; want nothing", names, err)
	}
	if names, err := autopilotClampedLimits(template(""), agentAPIAuthContainerName); err != nil || names != nil {
		t.Fatalf("no record clamped = %v, %v; want nil", names, err)
	}
	if names, err := autopilotClampedLimits(template("{not json"), agentAPIAuthContainerName); err == nil || names != nil {
		t.Fatalf("a record that does not parse clamped = %v, err %v; want nil and the error", names, err)
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
	t.Run("a dry run admission refuses holds the step", func(t *testing.T) {
		t.Parallel()
		// The dry run adds a whole pod where the step adds a difference, so
		// a quota the step would never trip (a pods count at its cap) can
		// refuse it: the class is unknown, nothing is measured, and neither
		// an In nor a DoesNotExist quota is read as binding, since a cap
		// naming one would tell the reader to raise a quota that may not
		// count these pods. The latch and the condition stand as they were.
		refused := apierrors.NewForbidden(corev1.Resource("pods"), "", errors.New("exceeded quota: pod-count, requested: pods=1, used: pods=12, limited: pods=12"))
		held := func(t *testing.T, agent *agentv1alpha1.PlatformAgent, why string) {
			t.Helper()
			if agent.Status.EffectiveSizingTier != sizingProfileSmall {
				t.Fatalf("status.effectiveSizingTier = %q, want small: %s", agent.Status.EffectiveSizingTier, why)
			}
			if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
				t.Fatalf("SizingCappedByQuota = %+v after admission refused the dry run, want none: %s", cond, why)
			}
		}
		agent, seen := run(t, inDefault, "cluster-default", refused)
		held(t, agent, "an In quota was read as binding on a refused dry run")
		if len(seen) != 1 {
			t.Fatalf("admission saw %d dry-run Pods, want 1", len(seen))
		}
		agent, _ = run(t, noClass, "", refused)
		held(t, agent, "a DoesNotExist quota was read as binding on a refused dry run")
	})
	t.Run("a refused dry run keeps a standing cap as written", func(t *testing.T) {
		t.Parallel()
		// Capped at small by the In quota on an earlier pass, then the dry
		// run refused: the condition is neither cleared nor rewritten.
		refused := apierrors.NewForbidden(corev1.Resource("pods"), "", errors.New("exceeded quota: pod-count"))
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		standing := fmt.Sprintf(sizingCappedByQuotaMessageFormat, sizingTierMedium, inDefault.Name, corev1.ResourceRequestsMemory, sizingTierSmall, sizingTierSmall)
		setSizingCappedCondition(agent, sizingCap{reason: sizingCappedByQuotaReason, message: standing}, metav1.Now())
		objs := append([]client.Object{agent, inDefault}, sizedWorkloadsAt(agent, sizingTierSmall)...)
		var seen []*corev1.Pod
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).WithStatusSubresource(agent).
			WithInterceptorFuncs(admissionStamping("cluster-default", refused, &seen)).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8)}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if agent.Status.EffectiveSizingTier != sizingProfileSmall || cond == nil || cond.Message != standing {
			t.Fatalf("status.effectiveSizingTier = %q, SizingCappedByQuota = %+v; want small with the standing message kept", agent.Status.EffectiveSizingTier, cond)
		}
	})
	t.Run("with no proxy Deployment the step is held before admission is asked", func(t *testing.T) {
		t.Parallel()
		// A missing workload has not settled (sizingTierUnsettledWorkload),
		// so the pass measures nothing and asks nothing: the latch and the
		// condition stand, here small with no condition.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierSmall)[1:]
		agent, seen := run(t, inDefault, "cluster-default", nil, workloads...)
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small: a step was measured with the proxy Deployment missing", agent.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota set on a held pass: %+v", cond)
		}
		if len(seen) != 0 {
			t.Fatalf("admission saw %d dry-run Pods with no proxy Deployment, want none", len(seen))
		}
	})
}

func TestResolveSizingTierHoldsTheStepWhileASizedWorkloadIsAbsent(t *testing.T) {
	t.Parallel()
	// Latched small at 12 clusters with one requests.memory quota, 10Gi hard
	// and 6.5Gi used: the broker's 1Gi is not in the used total because its
	// Deployment is gone (a recreate the pass before did not finish, or a
	// hand delete), yet the medium step is measured from the small row, 3Gi,
	// and 3.5Gi is free. A pass that read the missing workload as settled
	// would latch medium and recreate the broker at 4Gi against 3.5Gi free,
	// which admission refuses, with the latch upward-only and no cap written.
	// The pass holds instead, logging the workload it waits on; once the
	// workload is back at the small row, and the quota charges it, the next
	// pass measures.
	for _, tc := range []struct {
		name    string
		missing int
		waiting string
	}{
		{"the proxy Deployment", 0, "Deployment/platformagent-credential-proxy"},
		{"the gateway Deployment", 1, "Deployment/platformagent-gateway"},
		{"the shell StatefulSet", 2, "StatefulSet/platformagent-shell"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
			quota := sizingQuota("platform-baseline-quota", "10Gi", "6656Mi")
			workloads := sizedWorkloadsAt(agent, sizingTierSmall)
			present := append([]client.Object{agent, quota}, slices.Delete(slices.Clone(workloads), tc.missing, tc.missing+1)...)
			scheme := setupScheme()
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(present...).WithStatusSubresource(agent).Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8)}
			var logged strings.Builder
			ctx := logf.IntoContext(context.Background(), funcr.New(func(prefix, args string) { logged.WriteString(args + "\n") }, funcr.Options{}))
			if err := r.resolveSizingTier(ctx, agent); err != nil {
				t.Fatalf("resolveSizingTier: %v", err)
			}
			if agent.Status.EffectiveSizingTier != sizingProfileSmall {
				t.Fatalf("status.effectiveSizingTier = %q, want small: the step was measured with %s absent and the quota charging nothing of it", agent.Status.EffectiveSizingTier, tc.name)
			}
			if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
				t.Fatalf("SizingCappedByQuota set on a held pass: %+v", cond)
			}
			if !strings.Contains(logged.String(), "sizing step held until the running tier's workloads settle") || !strings.Contains(logged.String(), `"waitingOn"="`+tc.waiting+`"`) {
				t.Fatalf("the held pass did not log the hold naming %s:\n%s", tc.waiting, logged.String())
			}
			// The workload recreated at the small row: the next pass measures
			// the 3Gi step against 3.5Gi free and takes it.
			if err := cl.Create(ctx, workloads[tc.missing]); err != nil {
				t.Fatalf("recreating %s: %v", tc.name, err)
			}
			if err := r.resolveSizingTier(ctx, agent); err != nil {
				t.Fatalf("second pass: %v", err)
			}
			if agent.Status.EffectiveSizingTier != sizingProfileMedium {
				t.Fatalf("status.effectiveSizingTier = %q after %s was recreated, want medium", agent.Status.EffectiveSizingTier, tc.name)
			}
		})
	}
}

func TestResolveSizingTierCountsTheClampedLimitsOnAutopilot(t *testing.T) {
	t.Parallel()
	// Latched small at 25 clusters with one limits.memory quota, 20Gi hard
	// and 10Gi used. The fleet step's memory limits as the rows write them
	// add 14Gi (the broker's 2Gi -> 8Gi, the sidecar's 2Gi -> 8Gi, the
	// shell's 2Gi -> 4Gi). On Autopilot without bursting every limit is
	// clamped to its request, so admission charges the quota the requests'
	// movement, the broker's 6Gi alone, and the fleet pods are admitted
	// against 10Gi free; a step that counted the rows' limits would cap the
	// tier at medium on a quota the pods would never fill. Where the limits
	// survive (Standard, or Autopilot with bursting, where the templates
	// carry the rows' limits), the 14Gi is what admission charges and the
	// cap on fleet is right.
	quota := func() *corev1.ResourceQuota {
		return sizingQuotaOn("platform-baseline-quota", corev1.ResourceLimitsMemory, "20Gi", "10Gi")
	}
	run := func(t *testing.T, autopilot bool, workloads []client.Object) (*agentv1alpha1.PlatformAgent, *record.FakeRecorder) {
		t.Helper()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, quota()}, workloads...)...).WithStatusSubresource(agent).Build()
		recorder := record.NewFakeRecorder(8)
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder, autopilot: autopilot, autopilotResolved: true}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		return agent, recorder
	}
	cappedAtMedium := func(t *testing.T, agent *agentv1alpha1.PlatformAgent, recorder *record.FakeRecorder, why string) {
		t.Helper()
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if agent.Status.EffectiveSizingTier != sizingProfileMedium || cond == nil || !strings.Contains(cond.Message, "earns the fleet tier") || !strings.Contains(cond.Message, "has no room on limits.memory") {
			t.Fatalf("status.effectiveSizingTier = %q, SizingCappedByQuota = %+v; want medium with the cap naming fleet on limits.memory: %s", agent.Status.EffectiveSizingTier, cond, why)
		}
		// The tier change's Normal event comes first, then the cap's Warning
		// with the figures.
		var events []string
		for len(recorder.Events) > 0 {
			events = append(events, <-recorder.Events)
		}
		if !slices.ContainsFunc(events, func(event string) bool { return strings.Contains(event, "the step adds 14Gi") }) {
			t.Fatalf("events = %q, want the cap's Warning with the rows' 14Gi", events)
		}
	}
	t.Run("Autopilot without bursting takes the fleet step on the requests' movement", func(t *testing.T) {
		t.Parallel()
		seed := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
		agent, _ := run(t, true, autopilotClamped(sizedWorkloadsOn(seed, sizingTierSmall, true)))
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet: the step counted the rows' limits on templates Autopilot's record shows clamped", agent.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota set on a step the clamped pods fit: %+v", cond)
		}
	})
	t.Run("Autopilot with bursting counts the limits the templates carry", func(t *testing.T) {
		t.Parallel()
		seed := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
		agent, recorder := run(t, true, sizedWorkloadsOn(seed, sizingTierSmall, true))
		cappedAtMedium(t, agent, recorder, "the limits survive under bursting and the quota is charged them")
	})
	t.Run("Standard counts the rows' limits as written", func(t *testing.T) {
		t.Parallel()
		seed := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
		agent, recorder := run(t, false, sizedWorkloadsAt(seed, sizingTierSmall))
		cappedAtMedium(t, agent, recorder, "nothing clamps a limit on Standard")
	})
}

func TestSizingRunningLimitsClampedReadsTheRecord(t *testing.T) {
	t.Parallel()
	// The step's clamp flag comes from Autopilot's record on the running
	// templates and from nothing else: true when a record shows a limit
	// lowered to its request, false on templates that carry the rows as
	// written (Standard, bursting) and on templates whose limits equal their
	// requests with no record of a rewrite.
	agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, nil, nil)
	for name, tc := range map[string]struct {
		workloads []client.Object
		want      bool
	}{
		"Autopilot's record of a clamp":          {autopilotClamped(sizedWorkloadsOn(agent, sizingTierSmall, true)), true},
		"the rows as written":                    {sizedWorkloadsOn(agent, sizingTierSmall, true), false},
		"equal pairs without Autopilot's record": {clampedToRequests(sizedWorkloadsOn(agent, sizingTierSmall, true)), false},
		"no workload to read":                    {nil, false},
	} {
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, tc.workloads...)...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: true, autopilotResolved: true}
		if got := r.sizingRunningLimitsClamped(context.Background(), agent); got != tc.want {
			t.Errorf("%s: sizingRunningLimitsClamped = %v, want %v", name, got, tc.want)
		}
	}
	t.Run("a record it cannot read is as written, and logged", func(t *testing.T) {
		t.Parallel()
		workloads := autopilotClamped(sizedWorkloadsOn(agent, sizingTierSmall, true))
		workloads[0].(*appsv1.Deployment).Spec.Template.Annotations[autopilotResourceAdjustmentAnnotation] = "{not json"
		var lines []string
		ctx := logf.IntoContext(context.Background(), funcr.New(func(prefix, args string) { lines = append(lines, args) }, funcr.Options{}))
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, workloads...)...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: true, autopilotResolved: true}
		// The gateway's and the shell's records still read, so the cluster
		// still reads as clamping; the proxy's unreadable record is logged.
		if !r.sizingRunningLimitsClamped(ctx, agent) {
			t.Fatal("sizingRunningLimitsClamped = false with two readable records of a clamp")
		}
		if !slices.ContainsFunc(lines, func(line string) bool {
			return strings.Contains(line, "cannot read Autopilot's resource-adjustment record") && strings.Contains(line, credentialProxyName(agent))
		}) {
			t.Fatalf("log lines = %q, want the unreadable record named with the proxy Deployment", lines)
		}
	})
}

func TestResolveSizingTierCountsTheBrokersEqualCPUPairAsWrittenUnderBursting(t *testing.T) {
	t.Parallel()
	// Latched medium at 25 clusters on Autopilot with bursting, one
	// limits.cpu quota, 10 hard and 6 used. The broker's medium row writes
	// an equal 2/2 CPU pair itself, and the fleet row 4/8: the step's CPU
	// limit as written is +6 (the sidecar's and the shell's do not move),
	// and under bursting the fleet pod keeps its 8, so admission charges the
	// 6 and 4 free is short. A step that read the broker's equal pair as a
	// clamp would count +2, promote, and lose the broker to admission.
	agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](25), nil)
	quota := sizingQuotaOn("platform-baseline-quota", corev1.ResourceLimitsCPU, "10", "6")
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).
		WithObjects(append([]client.Object{agent, quota}, sizedWorkloadsOn(agent, sizingTierMedium, true)...)...).
		WithStatusSubresource(agent).Build()
	recorder := record.NewFakeRecorder(8)
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder, autopilot: true, autopilotResolved: true}
	if err := r.resolveSizingTier(context.Background(), agent); err != nil {
		t.Fatalf("resolveSizingTier: %v", err)
	}
	cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
	if agent.Status.EffectiveSizingTier != sizingProfileMedium || cond == nil || !strings.Contains(cond.Message, "has no room on limits.cpu") {
		t.Fatalf("status.effectiveSizingTier = %q, SizingCappedByQuota = %+v; want medium capped on limits.cpu", agent.Status.EffectiveSizingTier, cond)
	}
	var events []string
	for len(recorder.Events) > 0 {
		events = append(events, <-recorder.Events)
	}
	if !slices.ContainsFunc(events, func(event string) bool { return strings.Contains(event, "the step adds 6") }) {
		t.Fatalf("events = %q, want the cap's Warning with the rows' 6 CPU", events)
	}
}

func TestResolveSizingTierTakesTheEarnedTierWithNoQuotaBeforeTheWorkloadsSettle(t *testing.T) {
	t.Parallel()
	// With no ResourceQuota and no LimitRange in the namespace there is
	// nothing to measure a step against, so the earned tier is taken
	// whatever the workloads' state, as before the settled wait: here none
	// of the three exists yet.
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
	// step is measured from medium (4Gi against 2Gi free) and is still
	// short. The message now names medium as the step's start where the
	// first pass named small, so it changes this once, and the write and
	// the Warning recorded with it carry the medium->fleet figures.
	proxy.Status = appsv1.DeploymentStatus{ObservedGeneration: 2, Replicas: 1, UpdatedReplicas: 1, AvailableReplicas: 1}
	if err := cl.Status().Update(ctx, proxy); err != nil {
		t.Fatal(err)
	}
	pass("fifth pass, the medium workloads settled")
	capped("after the medium workloads settled")
	if writes != 2 || len(recorder.Events) != 3 {
		t.Fatalf("%d status writes and %d events after the medium workloads settled, want 2 and 3: the re-measured step's message was not recorded", writes, len(recorder.Events))
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); !strings.Contains(cond.Message, "step up from medium") {
		t.Fatalf("the settled pass's message does not name the step it measured: %s", cond.Message)
	}
	for len(recorder.Events) > 1 {
		<-recorder.Events
	}
	if event := <-recorder.Events; !strings.Contains(event, "2Gi of requests.memory free, the step adds 4Gi") {
		t.Fatalf("the settled pass's event does not carry the medium->fleet figures: %s", event)
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
	if agent.Status.EffectiveSizingTier != sizingProfileFleet || writes != 3 {
		t.Fatalf("after the quota was raised: tier %q, %d writes; want fleet and 3", agent.Status.EffectiveSizingTier, writes)
	}
	if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
		t.Fatalf("SizingCappedByQuota kept after the fleet step was admitted: %+v", cond)
	}
}

func TestSizingTierSettled(t *testing.T) {
	t.Parallel()
	// The three sized workloads at a tier's render, as the fixtures leave
	// them, and each way one of them has not settled; a workload that is not
	// there has not settled at any tier, small included, since the quota's
	// used total charges nothing of it while the step is measured from its
	// row; the Autopilot rewrite of a template reads as settled, and a
	// container the template does not carry is not compared.
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
		{"at small the proxy Deployment is not there", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{g, s} }, false},
		{"at small the gateway is not there", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return []client.Object{p, s} }, false},
		{"at small none of the three is there", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object { return nil }, false},
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
		{"the proxy scaled to zero by hand has not settled", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			// The operator renders one replica and its next apply puts it
			// back; until it is available the quota charges no broker while
			// the step is measured from the broker's row.
			p.Spec.Replicas = ptr.To[int32](0)
			p.Status.UpdatedReplicas, p.Status.Replicas, p.Status.AvailableReplicas = 0, 0, 0
			return all(p, g, s)
		}, false},
		{"the shell scaled to zero by hand has not settled", sizingTierSmall, false, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			s.Spec.Replicas = ptr.To[int32](0)
			s.Status.UpdatedReplicas, s.Status.Replicas, s.Status.ReadyReplicas = 0, 0, 0
			return all(p, g, s)
		}, false},
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
		{"the template Autopilot clamped to its requests, on its record, settles small", sizingTierSmall, true, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			return autopilotClamped(all(p, g, s))
		}, true},
		{"the template Autopilot clamped to its requests, on its record, settles medium", sizingTierMedium, true, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			return autopilotClamped(all(p, g, s))
		}, true},
		{"an equal pair without Autopilot's record is the row's limit short", sizingTierSmall, true, func(p, g *appsv1.Deployment, s *appsv1.StatefulSet) []client.Object {
			return clampedToRequests(all(p, g, s))
		}, false},
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
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, autopilotClamped(sizedWorkloadsOn(agent, sizingTierSmall, true))...)...).Build()
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
		medium := autopilotClamped(sizedWorkloadsOn(agent, sizingTierMedium, true))
		small := autopilotClamped(sizedWorkloadsOn(agent, sizingTierSmall, true))
		objs := []client.Object{agent, medium[0], small[1], small[2]}
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: true, autopilotResolved: true}
		if got := r.sizingTierUnsettledWorkload(context.Background(), agent, sizingTierMedium); got != "" {
			t.Fatalf("sizingTierUnsettledWorkload = %q, want settled: the clamped small sidecar and shell are the medium ones", got)
		}
	})
	t.Run("an override's equal pair is not a clamp", func(t *testing.T) {
		t.Parallel()
		// A sidecar memory request pinned to the small row's 2Gi limit
		// writes an equal 2Gi/2Gi pair at small, which the medium row
		// (2Gi request, 4Gi limit) raises the limit of alone. Under
		// bursting there is no record and the limit is compared as written;
		// on a clamping cluster the record shows the pair came in equal and
		// left unchanged. Either way the small template has not settled
		// medium, where a reading that took any equal pair for the clamp
		// would let the requests side stand for a limit it never carried.
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
		agent.Spec.Deployment.AgentAPIAuth = &agentv1alpha1.AgentAPIAuthSpec{Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2Gi")},
		}}
		// The proxy and the shell are at the medium render, so the gateway
		// is the one workload left to settle.
		for name, templates := range map[string]struct{ small, medium []client.Object }{
			"under bursting":        {sizedWorkloadsOn(agent, sizingTierSmall, true), sizedWorkloadsOn(agent, sizingTierMedium, true)},
			"on a clamping cluster": {autopilotClamped(sizedWorkloadsOn(agent, sizingTierSmall, true)), autopilotClamped(sizedWorkloadsOn(agent, sizingTierMedium, true))},
		} {
			sidecar := sizedContainer(templates.small[1].(*appsv1.Deployment).Spec.Template, agentAPIAuthContainerName).Resources
			if sidecar.Requests.Memory().Cmp(*sidecar.Limits.Memory()) != 0 {
				t.Fatalf("%s: the small sidecar template carries %s/%s on memory, want the override's equal pair", name, sidecar.Requests.Memory(), sidecar.Limits.Memory())
			}
			objs := []client.Object{agent, templates.medium[0], templates.small[1], templates.medium[2]}
			scheme := setupScheme()
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, autopilot: true, autopilotResolved: true}
			if got := r.sizingTierUnsettledWorkload(context.Background(), agent, sizingTierMedium); got != "Deployment/"+agent.Name+gatewayWorkloadNameSuffix {
				t.Fatalf("%s: sizingTierUnsettledWorkload = %q, want the gateway: its small template read as settled at medium on the override's equal pair", name, got)
			}
		}
	})
	t.Run("the gateway at zero under scaleToZero has settled", func(t *testing.T) {
		t.Parallel()
		// The operator renders zero replicas there, so zero available is
		// every replica it renders; the proxy and the shell still want
		// their one.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		agent.Spec.Deployment.ScaleToZero = ptr.To(true)
		workloads := sizedWorkloadsAt(agent, sizingTierSmall)
		gateway := workloads[1].(*appsv1.Deployment)
		gateway.Spec.Replicas = ptr.To[int32](0)
		gateway.Status.UpdatedReplicas, gateway.Status.Replicas, gateway.Status.AvailableReplicas = 0, 0, 0
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent}, workloads...)...).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
		if got := r.sizingTierUnsettledWorkload(context.Background(), agent, sizingTierSmall); got != "" {
			t.Fatalf("sizingTierUnsettledWorkload = %q, want settled: the gateway renders zero replicas under scaleToZero", got)
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

// sizingLimitRange is a LimitRange with one Container-type item bounding
// key at quantity on the side bound names (sizingLimitRangeMax or Min).
func sizingLimitRange(name, bound string, key corev1.ResourceName, quantity string) *corev1.LimitRange {
	item := corev1.LimitRangeItem{Type: corev1.LimitTypeContainer}
	if bound == sizingLimitRangeMax {
		item.Max = corev1.ResourceList{key: resource.MustParse(quantity)}
	} else {
		item.Min = corev1.ResourceList{key: resource.MustParse(quantity)}
	}
	return &corev1.LimitRange{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "kubeagents-system"},
		Spec:       corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{item}},
	}
}

func TestResolveSizingTierLimitRangeGate(t *testing.T) {
	t.Parallel()
	// Latched small at 25 clusters, no ResourceQuota. The fleet row's broker
	// and sidecar carry an 8Gi memory limit, the medium row's a 4Gi one, so
	// a per-container max of 4Gi admits medium and refuses fleet.
	t.Run("a max under the earned row takes the tier the LimitRange admits and names the bound", func(t *testing.T) {
		t.Parallel()
		stored, recorder := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil),
			sizingLimitRange("tenant-bounds", sizingLimitRangeMax, corev1.ResourceMemory, "4Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: the gate promoted past a LimitRange max the fleet row crosses", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != sizingCappedByLimitRangeReason || cond.ObservedGeneration != 3 {
			t.Fatalf("SizingCappedByQuota = %+v, want True with reason %s", cond, sizingCappedByLimitRangeReason)
		}
		for _, want := range []string{"earns the fleet tier", "LimitRange tenant-bounds sets a per-container max on memory that the " + credentialProxyContainerName + " container crosses", "step up from small", "the medium tier stays"} {
			if !strings.Contains(cond.Message, want) {
				t.Errorf("message lacks %q: %s", want, cond.Message)
			}
		}
		if strings.Contains(cond.Message, "Gi") {
			t.Errorf("the message carries a quantity: %s", cond.Message)
		}
		// The tier change's Normal event, then the cap's Warning with the
		// figures.
		var events []string
		for len(recorder.Events) > 0 {
			events = append(events, <-recorder.Events)
		}
		if len(events) != 2 || !strings.Contains(events[0], sizingTierChangedReason) {
			t.Fatalf("events = %q, want the tier change then the cap", events)
		}
		for _, want := range []string{sizingCappedByLimitRangeEventReason, "at 25 clusters", "the max is 4Gi", "the row's figure 8Gi"} {
			if !strings.Contains(events[1], want) {
				t.Errorf("event lacks %q: %s", want, events[1])
			}
		}
	})
	t.Run("a bound every row admits lets the promotion through and clears a standing cap", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		setSizingCappedCondition(agent, sizingCap{reason: sizingCappedByLimitRangeReason, message: "left over from a tighter LimitRange"}, metav1.Now())
		stored, _ := resolveSizing(t, agent, sizingLimitRange("tenant-bounds", sizingLimitRangeMax, corev1.ResourceMemory, "16Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota still set after the LimitRange was raised: %+v", cond)
		}
	})
	t.Run("a min a row's request falls under refuses every step and names the container", func(t *testing.T) {
		t.Parallel()
		// The sidecar requests 384Mi at every tier; a 2Gi per-container min
		// refuses its recreation at medium and at fleet alike. The broker's
		// medium request (4Gi) clears it, so the first container that falls
		// under the min is the sidecar.
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil),
			sizingLimitRange("tenant-bounds", sizingLimitRangeMin, corev1.ResourceMemory, "2Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByLimitRangeReason {
			t.Fatalf("SizingCappedByQuota = %+v, want the LimitRange reason", cond)
		}
		for _, want := range []string{"earns the fleet tier", "sets a per-container min on memory that the agent-api-auth container crosses", "step up from small", "the small tier stays"} {
			if !strings.Contains(cond.Message, want) {
				t.Errorf("message lacks %q: %s", want, cond.Message)
			}
		}
	})
	t.Run("a Pod-type max is read against the pod's sum with the candidate's render", func(t *testing.T) {
		t.Parallel()
		// The gateway pod runs its agent container beside the sidecar; give
		// it a 1Gi memory limit. A per-pod max of 4Gi admits the broker's
		// pod at medium (4Gi alone) and refuses the gateway's (the medium
		// sidecar's 4Gi plus 1Gi), so the step to medium is refused on the
		// sidecar's pod rather than on any one container, and fleet with it.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierSmall)
		gateway := workloads[1].(*appsv1.Deployment)
		gateway.Spec.Template.Spec.Containers[0].Resources = corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
		}
		podBound := sizingLimitRange("pod-bounds", sizingLimitRangeMax, corev1.ResourceMemory, "4Gi")
		podBound.Spec.Limits[0].Type = corev1.LimitTypePod
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, podBound}, workloads...)...).WithStatusSubresource(agent).Build()
		recorder := record.NewFakeRecorder(8)
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small: the gate promoted past a per-pod max the gateway pod's sum crosses", agent.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByLimitRangeReason {
			t.Fatalf("SizingCappedByQuota = %+v, want the LimitRange reason", cond)
		}
		for _, want := range []string{"LimitRange pod-bounds sets a per-pod max on memory that the agent-api-auth container's pod crosses", "step up from small", "the small tier stays"} {
			if !strings.Contains(cond.Message, want) {
				t.Errorf("message lacks %q: %s", want, cond.Message)
			}
		}
		if event := <-recorder.Events; !strings.Contains(event, "the max is 4Gi, the row's figure 5Gi") {
			t.Errorf("event does not carry the pod's sum: %s", event)
		}
	})
	t.Run("a Pod-type item holds the step while a sized workload is absent", func(t *testing.T) {
		t.Parallel()
		// The same pod bound, no ResourceQuota, and the gateway Deployment
		// gone at the pass (a hand delete; the gauges still answer for two
		// polls). A pass that read the pod's sum with no template would read
		// the medium sidecar alone, 4Gi, under the 4Gi bound, latch medium,
		// and recreate the gateway into the LimitRanger's refusal of the
		// real 5Gi sum, which nothing reads. The pass holds instead, logging
		// the workload it waits on; once the gateway is back the sum is read
		// and the step refused with the condition.
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](12), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierSmall)
		gateway := workloads[1].(*appsv1.Deployment)
		gateway.Spec.Template.Spec.Containers[0].Resources = corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("512Mi")},
			Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")},
		}
		podBound := sizingLimitRange("pod-bounds", sizingLimitRangeMax, corev1.ResourceMemory, "4Gi")
		podBound.Spec.Limits[0].Type = corev1.LimitTypePod
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, podBound, workloads[0], workloads[2]).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8)}
		var logged strings.Builder
		ctx := logf.IntoContext(context.Background(), funcr.New(func(prefix, args string) { logged.WriteString(args + "\n") }, funcr.Options{}))
		if err := r.resolveSizingTier(ctx, agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small: the pod's sum was read with the gateway Deployment absent", agent.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota set on a held pass: %+v", cond)
		}
		if !strings.Contains(logged.String(), "sizing step held until the running tier's workloads settle") || !strings.Contains(logged.String(), `"waitingOn"="Deployment/`+agent.Name+gatewayWorkloadNameSuffix+`"`) {
			t.Fatalf("the held pass did not log the hold naming the gateway Deployment:\n%s", logged.String())
		}
		if err := cl.Create(ctx, gateway); err != nil {
			t.Fatalf("recreating the gateway Deployment: %v", err)
		}
		if err := r.resolveSizingTier(ctx, agent); err != nil {
			t.Fatalf("second pass: %v", err)
		}
		cond := meta.FindStatusCondition(agent.Status.Conditions, sizingCappedByQuotaConditionType)
		if agent.Status.EffectiveSizingTier != sizingProfileSmall || cond == nil || cond.Reason != sizingCappedByLimitRangeReason {
			t.Fatalf("after the gateway was recreated: status.effectiveSizingTier = %q, SizingCappedByQuota = %+v; want small with the LimitRange reason", agent.Status.EffectiveSizingTier, cond)
		}
	})
	t.Run("a Pod-type max every pod's sum admits lets the step through", func(t *testing.T) {
		t.Parallel()
		// The fixtures' pods carry the sized container alone, so a per-pod
		// 4Gi max admits medium (4Gi) and refuses fleet (8Gi), as the
		// per-container max above does.
		podBound := sizingLimitRange("pod-bounds", sizingLimitRangeMax, corev1.ResourceMemory, "4Gi")
		podBound.Spec.Limits[0].Type = corev1.LimitTypePod
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil), podBound)
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium", stored.Status.EffectiveSizingTier)
		}
	})
	t.Run("a maxLimitRequestRatio the row crosses refuses the step", func(t *testing.T) {
		t.Parallel()
		// The sidecar requests 384Mi of memory at every tier over a 2Gi
		// limit at small (a ratio of 5.3), 4Gi at medium (10.7) and 8Gi at
		// fleet (21.3): a per-container ratio of 10 admits small alone. The
		// broker's rows keep limit and request equal on memory.
		ratio := &corev1.LimitRange{
			ObjectMeta: metav1.ObjectMeta{Name: "tenant-ratio", Namespace: "kubeagents-system"},
			Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
				Type:                 corev1.LimitTypeContainer,
				MaxLimitRequestRatio: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("10")},
			}}},
		}
		stored, recorder := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil), ratio)
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small: the gate promoted past a ratio the sidecar's medium row crosses", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || !strings.Contains(cond.Message, "LimitRange tenant-ratio sets a per-container maxLimitRequestRatio on memory that the agent-api-auth container crosses") {
			t.Fatalf("SizingCappedByQuota = %+v, want the ratio named on the sidecar", cond)
		}
		if event := <-recorder.Events; !strings.Contains(event, "the maxLimitRequestRatio is 10, the row's figure 10.7") {
			t.Errorf("event does not carry the ratio: %s", event)
		}
	})
	t.Run("a ratio is one under the Autopilot clamp", func(t *testing.T) {
		t.Parallel()
		// Without bursting Autopilot lowers every limit to its request, so
		// a limit-to-request ratio read after the rewrite is one and a ratio
		// bound admits every row.
		ratio := &corev1.LimitRange{
			ObjectMeta: metav1.ObjectMeta{Name: "tenant-ratio", Namespace: "kubeagents-system"},
			Spec: corev1.LimitRangeSpec{Limits: []corev1.LimitRangeItem{{
				Type:                 corev1.LimitTypeContainer,
				MaxLimitRequestRatio: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("2")},
			}}},
		}
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, ratio}, autopilotClamped(sizedWorkloadsOn(agent, sizingTierSmall, true))...)...).WithStatusSubresource(agent).Build()
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8), autopilot: true, autopilotResolved: true}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Errorf("status.effectiveSizingTier = %q, want fleet: a ratio bound capped a clamped row", agent.Status.EffectiveSizingTier)
		}
	})
	t.Run("the cap names the check that refused the lowest step", func(t *testing.T) {
		t.Parallel()
		// The LimitRange refuses fleet (8Gi over a 4Gi max) and a quota with
		// 2Gi of requests.memory free refuses medium (the step adds 3Gi):
		// the tier stays small and the condition names the quota, the check
		// that refused the step raising the least relieves.
		stored, _ := resolveSizing(t, sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil),
			sizingLimitRange("tenant-bounds", sizingLimitRangeMax, corev1.ResourceMemory, "4Gi"),
			sizingQuota("platform-baseline-quota", "10Gi", "8Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Reason != sizingCappedByQuotaReason || !strings.Contains(cond.Message, "ResourceQuota platform-baseline-quota has no room on requests.memory") {
			t.Fatalf("SizingCappedByQuota = %+v, want the quota named for the medium step", cond)
		}
	})
	t.Run("a limit is compared through the Autopilot clamp", func(t *testing.T) {
		t.Parallel()
		// A 4 CPU per-container max. The fleet row's broker limit is 8 CPU
		// as written, which crosses it on Standard and under bursting; on
		// Autopilot without bursting admission lowers the limit to the 4 CPU
		// request, which the LimitRanger's validation, running after that
		// rewrite, admits.
		bound := func() *corev1.LimitRange {
			return sizingLimitRange("tenant-bounds", sizingLimitRangeMax, corev1.ResourceCPU, "4")
		}
		run := func(t *testing.T, autopilot bool, workloads []client.Object) *agentv1alpha1.PlatformAgent {
			t.Helper()
			agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
			scheme := setupScheme()
			cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append([]client.Object{agent, bound()}, workloads...)...).WithStatusSubresource(agent).Build()
			r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: record.NewFakeRecorder(8), autopilot: autopilot, autopilotResolved: true}
			if err := r.resolveSizingTier(context.Background(), agent); err != nil {
				t.Fatalf("resolveSizingTier: %v", err)
			}
			return agent
		}
		standard := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		if agent := run(t, false, sizedWorkloadsAt(standard, sizingTierSmall)); agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Errorf("Standard: status.effectiveSizingTier = %q, want medium with fleet refused on the broker's 8 CPU limit", agent.Status.EffectiveSizingTier)
		}
		clamped := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		if agent := run(t, true, autopilotClamped(sizedWorkloadsOn(clamped, sizingTierSmall, true))); agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Errorf("Autopilot clamped: status.effectiveSizingTier = %q, want fleet: the broker's limit is its 4 CPU request there", agent.Status.EffectiveSizingTier)
		}
	})
}

// refusedByQuota is admission's refusal of a Deployment's pod on a
// ResourceQuota, as the ReplicaSet controller records it and the Deployment
// controller copies it: the ReplicaFailure condition True with the quota
// plugin's message, the pod's generateName naming the ReplicaSet.
func refusedByQuota(dep *appsv1.Deployment, replicaSet, quota, requested string) {
	refusedByQuotaOn(dep, replicaSet, quota, corev1.ResourceRequestsMemory, requested)
}

// refusedByQuotaOn is refusedByQuota on any quota key.
func refusedByQuotaOn(dep *appsv1.Deployment, replicaSet, quota string, key corev1.ResourceName, requested string) {
	dep.Status = appsv1.DeploymentStatus{ObservedGeneration: dep.Generation, Replicas: 1, UpdatedReplicas: 1, UnavailableReplicas: 1, Conditions: []appsv1.DeploymentCondition{{
		Type:   appsv1.DeploymentReplicaFailure,
		Status: corev1.ConditionTrue,
		Reason: "FailedCreate",
		Message: fmt.Sprintf("pods %q is forbidden: exceeded quota: %s, requested: %s=%s, used: %s=7Gi, limited: %s=10Gi",
			replicaSet+"-", quota, key, requested, key, key),
	}}}
}

func TestResolveSizingTierStepsBackFromARefusedRecreate(t *testing.T) {
	t.Parallel()
	// The window the settled wait does not cover: the gate measured the
	// step with room to spare and latched fleet, step 11 updated the proxy
	// Deployment, and while its Recreate drained the old broker another pod
	// landed in the namespace and took the room. The 8Gi pod is refused and
	// the Deployment carries ReplicaFailure naming the quota.
	resolve := func(t *testing.T, agent *agentv1alpha1.PlatformAgent, workloads []client.Object, objs ...client.Object) (*agentv1alpha1.PlatformAgent, *record.FakeRecorder) {
		t.Helper()
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append(append([]client.Object{agent}, workloads...), objs...)...).WithStatusSubresource(agent).Build()
		recorder := record.NewFakeRecorder(8)
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder}
		if err := r.resolveSizingTier(context.Background(), agent); err != nil {
			t.Fatalf("resolveSizingTier: %v", err)
		}
		return agent, recorder
	}
	t.Run("a quota refusal of the recreated proxy steps the latch back and names the quota", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierFleet)
		refusedByQuota(workloads[0].(*appsv1.Deployment), credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", "8Gi")
		stored, recorder := resolve(t, agent, workloads, sizingQuota("platform-baseline-quota", "10Gi", "7Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: the refused step was not stepped back from", stored.Status.EffectiveSizingTier)
		}
		cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType)
		if cond == nil || cond.Status != metav1.ConditionTrue || cond.Reason != sizingCappedByQuotaReason {
			t.Fatalf("SizingCappedByQuota = %+v, want True with reason %s", cond, sizingCappedByQuotaReason)
		}
		for _, want := range []string{"the pod Deployment/" + credentialProxyName(agent) + " recreates at the fleet tier", "ResourceQuota platform-baseline-quota", "exceeded quota: platform-baseline-quota", "the medium tier stays"} {
			if !strings.Contains(cond.Message, want) {
				t.Errorf("message lacks %q: %s", want, cond.Message)
			}
		}
		var events []string
		for len(recorder.Events) > 0 {
			events = append(events, <-recorder.Events)
		}
		if len(events) != 2 || !strings.Contains(events[0], "sizing tier fleet -> medium") || !strings.Contains(events[1], sizingCappedByQuotaEventReason) {
			t.Fatalf("events = %q, want the tier change then the cap", events)
		}
	})
	t.Run("the refusal the condition already records is not stepped back from again", func(t *testing.T) {
		t.Parallel()
		// The pass after the step back: the Deployment still copies the old
		// ReplicaSet's condition while that ReplicaSet is scaled away, the
		// template is at medium and its pod not yet available. The latch
		// stays at medium and the gate holds for the settled wait.
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierMedium)
		proxy := workloads[0].(*appsv1.Deployment)
		refusedByQuota(proxy, credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", "8Gi")
		recorded := fmt.Sprintf(sizingStepRefusedMessageFormat, "Deployment/"+credentialProxyName(agent), sizingTierFleet, "platform-baseline-quota", proxy.Status.Conditions[0].Message, sizingTierMedium)
		setSizingCappedCondition(agent, sizingCap{reason: sizingCappedByQuotaReason, message: recorded}, metav1.Now())
		stored, recorder := resolve(t, agent, workloads, sizingQuota("platform-baseline-quota", "10Gi", "7Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium: the same refusal was stepped back from twice", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond == nil || cond.Message != recorded {
			t.Fatalf("SizingCappedByQuota = %+v, want the recorded refusal as written", cond)
		}
		if len(recorder.Events) != 0 {
			t.Fatalf("%d events on the pass that read the recorded refusal, want none", len(recorder.Events))
		}
	})
	t.Run("the lower row's pod refused in turn is stepped back from again", func(t *testing.T) {
		t.Parallel()
		// A new ReplicaSet's pod at the medium row is refused too: a new
		// message, so the latch steps back to small.
		agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierMedium)
		proxy := workloads[0].(*appsv1.Deployment)
		refusedByQuota(proxy, credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", "8Gi")
		recorded := fmt.Sprintf(sizingStepRefusedMessageFormat, "Deployment/"+credentialProxyName(agent), sizingTierFleet, "platform-baseline-quota", proxy.Status.Conditions[0].Message, sizingTierMedium)
		setSizingCappedCondition(agent, sizingCap{reason: sizingCappedByQuotaReason, message: recorded}, metav1.Now())
		refusedByQuota(proxy, credentialProxyName(agent)+"-5c6d7e8f9", "platform-baseline-quota", "4Gi")
		stored, _ := resolve(t, agent, workloads, sizingQuota("platform-baseline-quota", "10Gi", "7Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond == nil || !strings.Contains(cond.Message, "recreates at the medium tier") || !strings.Contains(cond.Message, "the small tier stays") {
			t.Fatalf("SizingCappedByQuota = %+v, want the medium step's refusal", cond)
		}
	})
	t.Run("a refusal at small has no tier to step back to", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileSmall, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierSmall)
		refusedByQuota(workloads[0].(*appsv1.Deployment), credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", "1Gi")
		stored, _ := resolve(t, agent, workloads, sizingQuota("platform-baseline-quota", "10Gi", "10Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileSmall {
			t.Fatalf("status.effectiveSizingTier = %q, want small", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota = %+v, want none: the proxy has not settled, so nothing was measured", cond)
		}
	})
	t.Run("a refusal that names no quota is not read", func(t *testing.T) {
		t.Parallel()
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierFleet)
		proxy := workloads[0].(*appsv1.Deployment)
		refusedByQuota(proxy, credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", "8Gi")
		proxy.Status.Conditions[0].Message = `pods "x-" is forbidden: violates PodSecurity "restricted:latest"`
		stored, _ := resolve(t, agent, workloads, sizingQuota("platform-baseline-quota", "10Gi", "7Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet: a refusal that is not the quota's moved the latch", stored.Status.EffectiveSizingTier)
		}
	})
	t.Run("the gateway Deployment's refusal is read as the proxy's is", func(t *testing.T) {
		t.Parallel()
		// A limits.memory quota: the fleet step raises the sidecar's memory
		// limit from 4Gi to 8Gi, and the gateway pod's limits sum in the
		// fixture is the sidecar's alone.
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierFleet)
		refusedByQuotaOn(workloads[1].(*appsv1.Deployment), agent.Name+gatewayWorkloadNameSuffix+"-7d9f8b6c4", "platform-baseline-quota", corev1.ResourceLimitsMemory, "8Gi")
		stored, _ := resolve(t, agent, workloads, sizingQuotaOn("platform-baseline-quota", corev1.ResourceLimitsMemory, "10Gi", "7Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q, want medium", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond == nil || !strings.Contains(cond.Message, "Deployment/"+agent.Name+gatewayWorkloadNameSuffix) {
			t.Fatalf("SizingCappedByQuota = %+v, want the gateway Deployment named", cond)
		}
	})
	t.Run("a refusal on a key the step did not move on that pod is not read", func(t *testing.T) {
		t.Parallel()
		// The sidecar requests 384Mi of memory at every tier, so a
		// requests.memory quota refusing the gateway's pod is not the fleet
		// step's doing and a lower row would not relieve it; the pod shows
		// as the settled hold instead. The latch and the condition stand.
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierFleet)
		refusedByQuota(workloads[1].(*appsv1.Deployment), agent.Name+gatewayWorkloadNameSuffix+"-7d9f8b6c4", "platform-baseline-quota", "384Mi")
		stored, recorder := resolve(t, agent, workloads, sizingQuota("platform-baseline-quota", "10Gi", "7Gi"))
		if stored.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet: a refusal on the sidecar's unchanged request moved the latch", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota = %+v, want none", cond)
		}
		if len(recorder.Events) != 0 {
			t.Fatalf("%d events, want none", len(recorder.Events))
		}
	})
	t.Run("a pods count refusal is not read", func(t *testing.T) {
		t.Parallel()
		// A pods quota at its cap refuses the recreated broker whatever its
		// row: no tier is read from it.
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierFleet)
		refusedByQuotaOn(workloads[0].(*appsv1.Deployment), credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", corev1.ResourcePods, "1")
		stored, _ := resolve(t, agent, workloads, sizingQuotaOn("platform-baseline-quota", corev1.ResourcePods, "10", "10"))
		if stored.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q, want fleet: a pods count refusal moved the latch", stored.Status.EffectiveSizingTier)
		}
		if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
			t.Fatalf("SizingCappedByQuota = %+v, want none", cond)
		}
	})
	t.Run("the row above's refusal lingering on the other Deployment after a step back is not stepped back from", func(t *testing.T) {
		t.Parallel()
		// Both Deployments were refused at fleet on a limits.memory quota:
		// the pass before stepped back from the proxy's refusal and
		// recorded its message. This pass reads the gateway's, which the
		// condition does not record, and which asks for the fleet sidecar's
		// 8Gi, the row above the latch: the old ReplicaSet's, not the medium
		// pod's. With the condition recorded, and with it cleared since (a
		// count that fell, the watcher switched off), the latch stays; the
		// count here, 12, earns the latched tier, so the pass clears the
		// standing record as any pass with no step to earn does.
		for _, recorded := range []bool{true, false} {
			agent := sizingAgent(sizingProfileAuto, sizingProfileMedium, ptr.To[int64](12), nil)
			workloads := sizedWorkloadsAt(agent, sizingTierMedium)
			proxy := workloads[0].(*appsv1.Deployment)
			refusedByQuotaOn(proxy, credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", corev1.ResourceLimitsMemory, "8Gi")
			if recorded {
				message := fmt.Sprintf(sizingStepRefusedMessageFormat, "Deployment/"+credentialProxyName(agent), sizingTierFleet, "platform-baseline-quota", proxy.Status.Conditions[0].Message, sizingTierMedium)
				setSizingCappedCondition(agent, sizingCap{reason: sizingCappedByQuotaReason, message: message}, metav1.Now())
			}
			refusedByQuotaOn(workloads[1].(*appsv1.Deployment), agent.Name+gatewayWorkloadNameSuffix+"-6c8d9e0f1", "platform-baseline-quota", corev1.ResourceLimitsMemory, "8Gi")
			stored, recorder := resolve(t, agent, workloads, sizingQuotaOn("platform-baseline-quota", corev1.ResourceLimitsMemory, "10Gi", "7Gi"))
			if stored.Status.EffectiveSizingTier != sizingProfileMedium {
				t.Fatalf("recorded=%v: status.effectiveSizingTier = %q, want medium: the fleet row's lingering refusal was stepped back from twice", recorded, stored.Status.EffectiveSizingTier)
			}
			if cond := meta.FindStatusCondition(stored.Status.Conditions, sizingCappedByQuotaConditionType); cond != nil {
				t.Fatalf("recorded=%v: SizingCappedByQuota = %+v, want none at a count that earns the latched tier", recorded, cond)
			}
			if len(recorder.Events) != 0 {
				t.Fatalf("recorded=%v: %d events, want none", recorded, len(recorder.Events))
			}
		}
	})
	t.Run("the next step up is measured only once the hold interval after a step back has passed", func(t *testing.T) {
		t.Parallel()
		// The proxy's fleet pod is refused with 13Gi free: the room came
		// back as soon as the pod that took it ended. The pass steps back
		// to medium; once the medium workloads settle, a pass that measured
		// at once would take the fleet step again, into the next pod the
		// namespace admits during the drain. The pass holds for
		// sizingStepBackHoldInterval, logging it, and measures after.
		agent := sizingAgent(sizingProfileAuto, sizingProfileFleet, ptr.To[int64](25), nil)
		workloads := sizedWorkloadsAt(agent, sizingTierFleet)
		refusedByQuota(workloads[0].(*appsv1.Deployment), credentialProxyName(agent)+"-7d9f8b6c4", "platform-baseline-quota", "8Gi")
		scheme := setupScheme()
		cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(append(append([]client.Object{agent}, workloads...), sizingQuota("platform-baseline-quota", "20Gi", "7Gi"))...).WithStatusSubresource(agent).Build()
		recorder := record.NewFakeRecorder(8)
		r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme, Recorder: recorder}
		var logged strings.Builder
		ctx := logf.IntoContext(context.Background(), funcr.New(func(prefix, args string) { logged.WriteString(args + "\n") }, funcr.Options{}))
		if err := r.resolveSizingTier(ctx, agent); err != nil {
			t.Fatalf("first pass: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q after the refusal, want medium", agent.Status.EffectiveSizingTier)
		}
		// The medium row rendered and settled: the proxy and the gateway at
		// the medium render with the refusal gone, the shell unchanged.
		for i, obj := range sizedWorkloadsAt(agent, sizingTierMedium) {
			if i == 2 {
				continue
			}
			settled := obj.(*appsv1.Deployment)
			current := &appsv1.Deployment{}
			if err := cl.Get(ctx, client.ObjectKeyFromObject(settled), current); err != nil {
				t.Fatal(err)
			}
			current.Spec.Template = settled.Spec.Template
			if err := cl.Update(ctx, current); err != nil {
				t.Fatal(err)
			}
			current.Status = settled.Status
			if err := cl.Status().Update(ctx, current); err != nil {
				t.Fatal(err)
			}
		}
		for len(recorder.Events) > 0 {
			<-recorder.Events
		}
		if err := r.resolveSizingTier(ctx, agent); err != nil {
			t.Fatalf("held pass: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileMedium {
			t.Fatalf("status.effectiveSizingTier = %q on the pass after the step back, want medium: the step was measured again inside the hold", agent.Status.EffectiveSizingTier)
		}
		if !strings.Contains(logged.String(), "sizing step held after a step back until the hold interval passes") {
			t.Fatalf("the held pass did not log the hold:\n%s", logged.String())
		}
		if len(recorder.Events) != 0 {
			t.Fatalf("%d events on the held pass, want none", len(recorder.Events))
		}
		// The interval passed: the fleet step is measured against 13Gi free
		// and taken.
		r.steppedBackAt.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-sizingStepBackHoldInterval))
		if err := r.resolveSizingTier(ctx, agent); err != nil {
			t.Fatalf("pass after the hold: %v", err)
		}
		if agent.Status.EffectiveSizingTier != sizingProfileFleet {
			t.Fatalf("status.effectiveSizingTier = %q after the hold interval, want fleet", agent.Status.EffectiveSizingTier)
		}
	})
}

func TestResourceQuotaNamedIn(t *testing.T) {
	t.Parallel()
	for message, want := range map[string]string{
		`pods "p-" is forbidden: exceeded quota: platform-baseline-quota, requested: requests.memory=8Gi, used: requests.memory=7Gi, limited: requests.memory=10Gi`: "platform-baseline-quota",
		`pods "p-" is forbidden: exceeded quota: a, b`: "a",
		`pods "p-" is forbidden: violates PodSecurity`: "",
		`exceeded quota: `: "",
	} {
		got, named := resourceQuotaNamedIn(message)
		if got != want || named != (want != "") {
			t.Errorf("resourceQuotaNamedIn(%q) = %q, %v; want %q", message, got, named, want)
		}
	}
}

func TestQuotaRefusalRequested(t *testing.T) {
	t.Parallel()
	requested := quotaRefusalRequested(`pods "p-" is forbidden: exceeded quota: platform-baseline-quota, requested: limits.memory=12Gi,requests.memory=4608Mi, used: limits.memory=7Gi,requests.memory=3Gi, limited: limits.memory=16Gi,requests.memory=10Gi`)
	limits, requests := requested[corev1.ResourceLimitsMemory], requested[corev1.ResourceRequestsMemory]
	if len(requested) != 2 || limits.Cmp(resource.MustParse("12Gi")) != 0 || requests.Cmp(resource.MustParse("4608Mi")) != 0 {
		t.Errorf("requested = %v, want limits.memory=12Gi and requests.memory=4608Mi", requested)
	}
	if got := quotaRefusalRequested(`pods "p-" is forbidden: violates PodSecurity`); len(got) != 0 {
		t.Errorf("requested on a message without the list = %v, want none", got)
	}
	got := quotaRefusalRequested(`exceeded quota: q, requested: pods=1,memory=not-a-quantity, used: pods=10, limited: pods=10`)
	if pods := got[corev1.ResourcePods]; len(got) != 1 || pods.Cmp(resource.MustParse("1")) != 0 {
		t.Errorf("requested with one pair that does not parse = %v, want pods=1 alone", got)
	}
}

func TestSizingPodFigures(t *testing.T) {
	t.Parallel()
	mem := func(request, limit string) corev1.ResourceRequirements {
		rr := corev1.ResourceRequirements{Requests: corev1.ResourceList{}, Limits: corev1.ResourceList{}}
		if request != "" {
			rr.Requests[corev1.ResourceMemory] = resource.MustParse(request)
		}
		if limit != "" {
			rr.Limits[corev1.ResourceMemory] = resource.MustParse(limit)
		}
		return rr
	}
	// A sidecar init container adds to the sum; a plain init container,
	// with the sidecars before it, is a floor on it; a limit set alone is
	// read as the request too.
	template := corev1.PodTemplateSpec{Spec: corev1.PodSpec{
		InitContainers: []corev1.Container{
			{Name: "setup", Resources: mem("3Gi", "3Gi")},
			{Name: agentAPIAuthContainerName, Resources: mem("384Mi", "2Gi"), RestartPolicy: ptr.To(corev1.ContainerRestartPolicyAlways)},
		},
		Containers: []corev1.Container{
			{Name: platformAgentContainerName, Resources: mem("", "1Gi")},
		},
	}}
	requests, limits := sizingPodFigures(template, agentAPIAuthContainerName, mem("384Mi", "8Gi"), false)
	// Requests: the agent's 1Gi (from its limit) plus the sidecar's 384Mi,
	// floored by the setup container's 3Gi, which runs before the sidecar.
	if got := requests[corev1.ResourceMemory]; got.Cmp(resource.MustParse("3Gi")) != 0 {
		t.Errorf("pod memory request = %s, want 3Gi", got.String())
	}
	// Limits: 1Gi plus the swapped-in 8Gi.
	if got := limits[corev1.ResourceMemory]; got.Cmp(resource.MustParse("9Gi")) != 0 {
		t.Errorf("pod memory limit = %s, want 9Gi", got.String())
	}
	// Under the clamp every limit is its request: 1Gi plus 384Mi.
	_, clamped := sizingPodFigures(template, agentAPIAuthContainerName, mem("384Mi", "8Gi"), true)
	if got := clamped[corev1.ResourceMemory]; got.Cmp(resource.MustParse("3Gi")) != 0 {
		t.Errorf("clamped pod memory limit = %s, want 3Gi (the setup container's floor)", got.String())
	}
	// A template without the container is the container alone.
	alone, _ := sizingPodFigures(corev1.PodTemplateSpec{}, agentAPIAuthContainerName, mem("384Mi", "8Gi"), false)
	if got := alone[corev1.ResourceMemory]; got.Cmp(resource.MustParse("384Mi")) != 0 {
		t.Errorf("pod memory request with no template = %s, want 384Mi", got.String())
	}
}

// TestSizingPodFiguresLeavesTheTemplateAlone reads a template whose
// restartable sidecar runs before a plain init container twice, as the gate
// does once per candidate tier and per Pod-type item: the second read is the
// first, the template's own limits map is as it was, and a plain init
// container that sets no limits (a nil map) is summed rather than written
// into.
func TestSizingPodFiguresLeavesTheTemplateAlone(t *testing.T) {
	t.Parallel()
	mem := func(request, limit string) corev1.ResourceRequirements {
		rr := corev1.ResourceRequirements{Requests: corev1.ResourceList{}, Limits: corev1.ResourceList{}}
		if request != "" {
			rr.Requests[corev1.ResourceMemory] = resource.MustParse(request)
		}
		if limit != "" {
			rr.Limits[corev1.ResourceMemory] = resource.MustParse(limit)
		}
		return rr
	}
	sidecarFirst := corev1.PodTemplateSpec{Spec: corev1.PodSpec{
		InitContainers: []corev1.Container{
			{Name: agentAPIAuthContainerName, Resources: mem("384Mi", "2Gi"), RestartPolicy: ptr.To(corev1.ContainerRestartPolicyAlways)},
			{Name: "setup", Resources: mem("1Gi", "3Gi")},
		},
		Containers: []corev1.Container{
			{Name: platformAgentContainerName, Resources: mem("", "1Gi")},
		},
	}}
	// The swapped-in 8Gi and the agent's 1Gi sum to 9Gi; the setup
	// container's 3Gi with the sidecar's 8Gi before it floors the pod at
	// 11Gi. A sum written into the setup container's map would read 19Gi
	// the second time.
	_, first := sizingPodFigures(sidecarFirst, agentAPIAuthContainerName, mem("384Mi", "8Gi"), false)
	_, second := sizingPodFigures(sidecarFirst, agentAPIAuthContainerName, mem("384Mi", "8Gi"), false)
	if got := first[corev1.ResourceMemory]; got.Cmp(resource.MustParse("11Gi")) != 0 {
		t.Errorf("pod memory limit = %s, want 11Gi", got.String())
	}
	if got, want := second[corev1.ResourceMemory], first[corev1.ResourceMemory]; got.Cmp(want) != 0 {
		t.Errorf("pod memory limit on the second read = %s, want the first read's %s", got.String(), want.String())
	}
	if got := sidecarFirst.Spec.InitContainers[1].Resources.Limits[corev1.ResourceMemory]; got.Cmp(resource.MustParse("3Gi")) != 0 {
		t.Errorf("setup container's limit after two reads = %s, want 3Gi as written", got.String())
	}
	// No limits on the plain init container: a nil map, which the sum
	// must not write into; the pod's 9Gi stands above its 8Gi floor.
	noLimits := sidecarFirst.DeepCopy()
	noLimits.Spec.InitContainers[1].Resources = corev1.ResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("1Gi")}}
	_, limits := sizingPodFigures(*noLimits, agentAPIAuthContainerName, mem("384Mi", "8Gi"), false)
	if got := limits[corev1.ResourceMemory]; got.Cmp(resource.MustParse("9Gi")) != 0 {
		t.Errorf("pod memory limit with an unlimited init container = %s, want 9Gi", got.String())
	}
	if noLimits.Spec.InitContainers[1].Resources.Limits != nil {
		t.Errorf("unlimited init container's limits after the read = %v, want nil as written", noLimits.Spec.InitContainers[1].Resources.Limits)
	}
}
