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
	"os"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// envtestAssetsEnvVar is the variable setup-envtest exports and envtest reads.
	// `make -C k8s-operator test` sets it; a bare `go test` does not, and without
	// it there is no API server to start.
	envtestAssetsEnvVar = "KUBEBUILDER_ASSETS"
	// envtestCRDDir is the operator's CRDs, relative to this package, so the API
	// server under test serves the schema the branch generated rather than one
	// hand-written for the test.
	envtestCRDDir = "../../config/crd/bases"
	// envtestNamespace and envtestAgentName are the CR under test.
	envtestNamespace = "observed-gen"
	envtestAgentName = "test-agent"
	// envtestSpecEditAnnotation is the pod annotation the test adds to move
	// metadata.generation. A metadata annotation would not: only spec edits bump it.
	envtestSpecEditAnnotation = "observed-gen.test/edit"
	// The CRD requires spec.harness, and the credential proxy bootstrap wants
	// the full project/location/cluster triple; none of it is contacted here.
	envtestHarnessProject  = "envtest-project"
	envtestHarnessLocation = "envtest-location"
	envtestHarnessCluster  = "envtest-cluster"
)

// startEnvtestConfig boots an API server carrying the operator's CRDs and hands
// back its connection config. Skips — loudly, naming the variable — when the
// binaries are not configured, because envtest cannot start without them and a
// silent skip here would leave this file looking like coverage it is not.
func startEnvtestConfig(t *testing.T) (*rest.Config, *runtime.Scheme) {
	t.Helper()
	if os.Getenv(envtestAssetsEnvVar) == "" {
		t.Skipf("%s is unset: run through `make -C k8s-operator test`, or export it from `bin/setup-envtest use -p path`", envtestAssetsEnvVar)
	}
	env := &envtest.Environment{
		CRDDirectoryPaths:     []string{envtestCRDDir},
		ErrorIfCRDPathMissing: true,
	}
	cfg, err := env.Start()
	if err != nil {
		t.Fatalf("envtest start: %v", err)
	}
	t.Cleanup(func() {
		if err := env.Stop(); err != nil {
			t.Errorf("envtest stop: %v", err)
		}
	})
	return cfg, setupScheme()
}

// startEnvtest is startEnvtestConfig plus a direct, uncached client to the
// server it started.
func startEnvtest(t *testing.T) (client.Client, *runtime.Scheme) {
	t.Helper()
	cfg, scheme := startEnvtestConfig(t)
	cl, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("envtest client: %v", err)
	}
	return cl, scheme
}

// TestObservedGenerationFollowsMetadataGenerationEnvtest is the fix against a
// real API server, which is the only place metadata.generation is assigned:
// create a PlatformAgent, reconcile, edit the spec, reconcile again, and read
// status.observedGeneration and the Ready condition's observedGeneration
// tracking metadata.generation at each step. Between the edit and the second
// reconcile the status is stale, and the field says so — that gap is what a
// caller gating on the CR could not see before (#534).
//
// No Deployment controller runs under envtest, so the phase stays Provisioning
// throughout; the claim under test is the generation bookkeeping, not readiness.
func TestObservedGenerationFollowsMetadataGenerationEnvtest(t *testing.T) {
	cl, scheme := startEnvtest(t)
	ctx := context.Background()

	if err := cl.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: envtestNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: envtestAgentName, Namespace: envtestNamespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
		},
	}
	if err := cl.Create(ctx, agent); err != nil {
		t.Fatalf("creating PlatformAgent: %v", err)
	}
	// Without the sandbox keypair the reconcile parks on Degraded/ShellSandboxKeysMissing
	// before reaching updateStatusReady; the fixture Secret lets it get there.
	if err := cl.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}

	r := &PlatformAgentReconciler{Client: cl, APIReader: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(agent)}
	reconcile := func(pass string) {
		t.Helper()
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile (%s) failed: %v", pass, err)
		}
	}
	fetch := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		got := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, req.NamespacedName, got); err != nil {
			t.Fatalf("reading the PlatformAgent back: %v", err)
		}
		return got
	}
	readyGeneration := func(pa *agentv1alpha1.PlatformAgent) int64 {
		t.Helper()
		cond := meta.FindStatusCondition(pa.Status.Conditions, "Ready")
		if cond == nil {
			t.Fatalf("no Ready condition on the PlatformAgent at generation %d", pa.Generation)
		}
		return cond.ObservedGeneration
	}

	// The first pass adds the finalizer and returns; the second is the real one.
	reconcile("finalizer")
	reconcile("first full pass")

	first := fetch()
	if first.Generation != 1 {
		t.Fatalf("metadata.generation = %d after create, want 1", first.Generation)
	}
	if got := first.Status.ObservedGeneration; got != first.Generation {
		t.Errorf("status.observedGeneration = %d after the first reconcile, want %d", got, first.Generation)
	}
	if got := readyGeneration(first); got != first.Generation {
		t.Errorf("Ready condition observedGeneration = %d after the first reconcile, want %d", got, first.Generation)
	}
	t.Logf("after first reconcile: generation=%d status.observedGeneration=%d phase=%s ready.observedGeneration=%d",
		first.Generation, first.Status.ObservedGeneration, first.Status.Phase, readyGeneration(first))

	// A spec edit that changes nothing the status derives from, so the only
	// thing the next write can be about is the generation.
	first.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		PodAnnotations: map[string]string{envtestSpecEditAnnotation: "1"},
	}
	if err := cl.Update(ctx, first); err != nil {
		t.Fatalf("editing the spec: %v", err)
	}

	edited := fetch()
	if edited.Generation != 2 {
		t.Fatalf("metadata.generation = %d after a spec edit, want 2", edited.Generation)
	}
	// Honest staleness: the operator has not run, and the status says which
	// generation it still describes.
	if got := edited.Status.ObservedGeneration; got != 1 {
		t.Errorf("status.observedGeneration = %d before the reconcile that follows the edit, want 1 (the status has not been recomputed)", got)
	}
	if got := readyGeneration(edited); got != 1 {
		t.Errorf("Ready condition observedGeneration = %d before the reconcile that follows the edit, want 1", got)
	}
	t.Logf("after spec edit, before reconcile: generation=%d status.observedGeneration=%d ready.observedGeneration=%d",
		edited.Generation, edited.Status.ObservedGeneration, readyGeneration(edited))

	reconcile("after spec edit")

	final := fetch()
	if got := final.Status.ObservedGeneration; got != final.Generation {
		t.Errorf("status.observedGeneration = %d after reconciling generation %d, want %d", got, final.Generation, final.Generation)
	}
	if got := readyGeneration(final); got != final.Generation {
		t.Errorf("Ready condition observedGeneration = %d after reconciling generation %d, want %d", got, final.Generation, final.Generation)
	}
	if final.Status.Phase != first.Status.Phase {
		t.Errorf("phase moved from %q to %q across an edit that changed nothing the status derives from", first.Status.Phase, final.Status.Phase)
	}
	t.Logf("after second reconcile: generation=%d status.observedGeneration=%d phase=%s ready.observedGeneration=%d",
		final.Generation, final.Status.ObservedGeneration, final.Status.Phase, readyGeneration(final))
}
