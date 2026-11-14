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

	"github.com/go-logr/logr/funcr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	logf "sigs.k8s.io/controller-runtime/pkg/log"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func TestExtractImageVersion(t *testing.T) {
	tests := []struct {
		name  string
		image string
		want  string
	}{
		{name: "empty", image: "", want: ""},
		{name: "whitespace only", image: "   \t  ", want: ""},
		{name: "semver tag", image: "ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.3", want: "v0.12.3"},
		{name: "sha tag from private mirror with port", image: "mirror.corp.internal:5000/tenant-proj/kube-agents/platform-agent:sha-abc1234", want: "sha-abc1234"},
		{name: "registry port without tag or digest", image: "mirror.corp.internal:5000/platform-agent", want: ""},
		{name: "bare image with tag", image: "platform-agent:v1.0.0", want: "v1.0.0"},
		{name: "bare image without tag or digest", image: "platform-agent", want: ""},
		{name: "digest only", image: "ghcr.io/gke-labs/kube-agents/platform-agent@sha256:0123456789abcdef", want: "sha256:0123456789abcdef"},
		{name: "tag and digest prefers tag", image: "ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.3@sha256:0123456789abcdef", want: "v0.12.3"},
		{name: "empty tag before digest falls back to digest", image: "ghcr.io/gke-labs/kube-agents/platform-agent:@sha256:0123456789abcdef", want: "sha256:0123456789abcdef"},
		{name: "trailing colon", image: "ghcr.io/gke-labs/kube-agents/platform-agent:", want: ""},
		{name: "trailing at", image: "ghcr.io/gke-labs/kube-agents/platform-agent@", want: ""},
		{name: "trailing slash", image: "ghcr.io/gke-labs/kube-agents/platform-agent/", want: ""},
		{name: "trimmed whitespace", image: "  ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.3  ", want: "v0.12.3"},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := extractImageVersion(tc.image); got != tc.want {
				t.Errorf("extractImageVersion(%q) = %q, want %q", tc.image, got, tc.want)
			}
		})
	}
}

func TestWorkloadObservedVersion(t *testing.T) {
	tests := []struct {
		name       string
		containers []corev1.Container
		want       string
	}{
		{name: "empty containers", containers: nil, want: ""},
		{
			name: "no platform-agent container",
			containers: []corev1.Container{
				{Name: "fluent-bit", Image: "fluent/fluent-bit:3.0.0"},
			},
			want: "",
		},
		{
			name: "selects platform-agent among sidecars",
			containers: []corev1.Container{
				{Name: "fluent-bit", Image: "fluent/fluent-bit:3.0.0"},
				{Name: platformAgentContainerName, Image: "us-central1-docker.pkg.dev/private-proj/kube-agents/platform-agent:v0.14.2"},
			},
			want: "v0.14.2",
		},
		{
			name: "digest pinned platform-agent container",
			containers: []corev1.Container{
				{Name: platformAgentContainerName, Image: "ghcr.io/gke-labs/kube-agents/platform-agent@sha256:deadbeef"},
			},
			want: "sha256:deadbeef",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := workloadObservedVersion(tc.containers); got != tc.want {
				t.Errorf("workloadObservedVersion() = %q, want %q", got, tc.want)
			}
		})
	}
}

func gatewayWithImage(agent *agentv1alpha1.PlatformAgent, image string) *appsv1.Deployment {
	dep := readyGateway(agent)
	dep.Spec.Template.Spec.Containers = []corev1.Container{
		{Name: "fluent-bit", Image: "fluent/fluent-bit:3.0.0"},
		{Name: platformAgentContainerName, Image: image},
	}
	return dep
}

func TestReadyStatusRecordsAndUpdatesObservedVersion(t *testing.T) {
	agent := observedGenerationAgent(1)
	gateway := gatewayWithImage(agent, "mirror.corp.internal:5000/private-tenant/kube-agents/platform-agent:v0.12.3")
	counter := &statusWriteCounter{}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append(a2aStackUp(agent), agent, gateway, shellSandbox(agent, 1), credentialBroker(agent, 1), a2aGatewayWorkload(agent, 1))...).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(counter.interceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	if phase := settleReady(t, r, agent); phase != "Ready" {
		t.Fatalf("got phase %q, want Ready", phase)
	}
	if agent.Status.ObservedVersion != "v0.12.3" {
		t.Fatalf("status.observedVersion = %q, want %q", agent.Status.ObservedVersion, "v0.12.3")
	}
	if counter.writes != 1 {
		t.Fatalf("first pass made %d writes, want 1", counter.writes)
	}

	stored := &agentv1alpha1.PlatformAgent{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatalf("reading agent back: %v", err)
	}
	if stored.Status.ObservedVersion != "v0.12.3" {
		t.Errorf("persisted status.observedVersion = %q, want %q", stored.Status.ObservedVersion, "v0.12.3")
	}

	// Unchanged pass makes no write.
	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Fatalf("unchanged pass made %d writes total, want 1", counter.writes)
	}

	// Changing only the gateway container image triggers a status write even at the same generation.
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(gateway), gateway); err != nil {
		t.Fatalf("reading gateway: %v", err)
	}
	gateway.Spec.Template.Spec.Containers[1].Image = "ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.4"
	if err := r.Update(context.Background(), gateway); err != nil {
		t.Fatalf("updating gateway image: %v", err)
	}

	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Fatalf("image change made %d writes total, want 2", counter.writes)
	}
	if agent.Status.ObservedVersion != "v0.12.4" {
		t.Errorf("status.observedVersion after image update = %q, want %q", agent.Status.ObservedVersion, "v0.12.4")
	}

	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Fatalf("settled pass after image change made %d writes total, want 2", counter.writes)
	}

	// Deleting the gateway workload clears ObservedVersion without recording CRD pruning.
	if err := r.Delete(context.Background(), gateway); err != nil {
		t.Fatalf("deleting gateway: %v", err)
	}
	settleReady(t, r, agent)
	if counter.writes != 3 {
		t.Fatalf("workload deletion made %d writes total, want 3", counter.writes)
	}
	if agent.Status.ObservedVersion != "" {
		t.Errorf("status.observedVersion after workload deletion = %q, want empty", agent.Status.ObservedVersion)
	}
	if r.observedVersionPruned(agent) {
		t.Error("clearing ObservedVersion when workload is absent was misclassified as CRD pruning")
	}
}

func TestAPrunedObservedVersionDoesNotWriteEveryPass(t *testing.T) {
	agent := observedGenerationAgent(1)
	gateway := gatewayWithImage(agent, "ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.3")
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	persist := funcs.SubResourceUpdate
	pruning := true
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && pruning {
			pa.Status.ObservedVersion = ""
		}
		return persist(ctx, cl, subResourceName, obj, opts...)
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append(a2aStackUp(agent), agent, gateway, shellSandbox(agent, 1), credentialBroker(agent, 1), a2aGatewayWorkload(agent, 1))...).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	settleReady(t, r, agent)
	if agent.Status.ObservedVersion != "" {
		t.Fatalf("fixture did not prune status.observedVersion (got %q)", agent.Status.ObservedVersion)
	}
	if !r.observedVersionPruned(agent) {
		t.Fatal("expected observedVersionPruned to be true after pruned write")
	}
	if got := r.usageStatusRequeue(agent); got != usageStatusReprobeInterval {
		t.Errorf("usageStatusRequeue while pruned = %s, want %s", got, usageStatusReprobeInterval)
	}

	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 1 {
		t.Errorf("%d status writes across three unchanged passes under a pruning CRD, want 1", counter.writes)
	}

	// Expiry triggers one reprobe write; if still pruned, quiet again.
	key := client.ObjectKeyFromObject(agent)
	r.prunedObservedVersion.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	if got := r.usageStatusRequeue(agent); got != secretEnvReprobeInterval {
		t.Errorf("usageStatusRequeue after record expiry = %s, want %s", got, secretEnvReprobeInterval)
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("%d writes after expired record reprobed under still-pruning CRD, want 2", counter.writes)
	}

	// Once the CRD carries the field, a fresh record suppresses writes until expiry, then lands it.
	pruning = false
	settleReady(t, r, agent)
	if counter.writes != 2 {
		t.Errorf("%d writes after CRD applied while record still fresh, want 2", counter.writes)
	}
	r.prunedObservedVersion.Store(key, time.Now().Add(-2*usageStatusReprobeInterval))
	settleReady(t, r, agent)
	if counter.writes != 3 || agent.Status.ObservedVersion != "v0.12.3" {
		t.Fatalf("after record expiry under upgraded CRD: %d writes, observedVersion=%q; want 3 and %q", counter.writes, agent.Status.ObservedVersion, "v0.12.3")
	}
	if r.observedVersionPruned(agent) {
		t.Error("pruned record remained set after API server echoed ObservedVersion")
	}

	// Subsequent version change writes once.
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(gateway), gateway); err != nil {
		t.Fatal(err)
	}
	gateway.Spec.Template.Spec.Containers[1].Image = "ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.4"
	if err := r.Update(context.Background(), gateway); err != nil {
		t.Fatal(err)
	}
	settleReady(t, r, agent)
	settleReady(t, r, agent)
	if counter.writes != 4 || agent.Status.ObservedVersion != "v0.12.4" {
		t.Errorf("after version bump: %d writes, observedVersion=%q; want 4 and %q", counter.writes, agent.Status.ObservedVersion, "v0.12.4")
	}
}

func TestAPrunedObservedVersionIsLoggedOncePerRecordAndClearedOnForget(t *testing.T) {
	var lines []string
	logger := funcr.New(func(prefix, args string) { lines = append(lines, args) }, funcr.Options{})
	ctx := logf.IntoContext(context.Background(), logger)
	said := func() int {
		n := 0
		for _, l := range lines {
			if strings.Contains(l, observedVersionPrunedLogMsg) {
				n++
			}
		}
		return n
	}

	agent := observedGenerationAgent(1)
	gateway := gatewayWithImage(agent, "ghcr.io/gke-labs/kube-agents/platform-agent:v0.12.3")
	counter := &statusWriteCounter{}
	funcs := counter.interceptors()
	persist := funcs.SubResourceUpdate
	funcs.SubResourceUpdate = func(ctx context.Context, cl client.Client, subResourceName string, obj client.Object, opts ...client.SubResourceUpdateOption) error {
		if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
			pa.Status.ObservedVersion = ""
		}
		return persist(ctx, cl, subResourceName, obj, opts...)
	}
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append(a2aStackUp(agent), agent, gateway, shellSandbox(agent, 1), credentialBroker(agent, 1), a2aGatewayWorkload(agent, 1))...).
		WithStatusSubresource(agent).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}

	if _, _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aProvisionState{}); err != nil {
		t.Fatalf("first updateStatusReady failed: %v", err)
	}
	if said() != 1 {
		t.Fatalf("logged %d times on initial prune, want 1", said())
	}

	// Another write while the record is fresh (triggered by a spec change) refreshes silently.
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		Slack: &agentv1alpha1.SlackSpec{Enabled: ptr.To(true)},
	}
	if err := r.Update(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if _, _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aProvisionState{}); err != nil {
		t.Fatalf("second updateStatusReady failed: %v", err)
	}
	if said() != 1 {
		t.Errorf("logged %d times while pruning record was still fresh, want 1", said())
	}

	// Expiry logs once more on reprobe.
	r.prunedObservedVersion.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-2*usageStatusReprobeInterval))
	if _, _, err := r.updateStatusReady(ctx, agent, "", otlpSourceNone, r.resolveNetpolProfile(ctx, agent), a2aProvisionState{}); err != nil {
		t.Fatalf("reprobe updateStatusReady failed: %v", err)
	}
	if said() != 2 {
		t.Errorf("logged %d times after record expired and reprobed, want 2", said())
	}

	r.forgetUsageStatus(agent)
	if r.observedVersionPruned(agent) {
		t.Error("forgetUsageStatus left prunedObservedVersion record behind")
	}
}

func TestDegradedStatusPreservesObservedVersion(t *testing.T) {
	agent := observedGenerationAgent(2)
	agent.Status.ObservedVersion = "v0.12.3"
	r := observedGenerationReconciler(agent, &statusWriteCounter{})

	if err := r.updateStatusDegraded(context.Background(), agent, reasonRuntimeClassNotFound, "RuntimeClass 'gvisor' is not configured in this cluster", workloadNotRendered); err != nil {
		t.Fatalf("updateStatusDegraded failed: %v", err)
	}
	if agent.Status.ObservedVersion != "v0.12.3" {
		t.Errorf("status.observedVersion after Degraded update = %q, want %q", agent.Status.ObservedVersion, "v0.12.3")
	}
}
