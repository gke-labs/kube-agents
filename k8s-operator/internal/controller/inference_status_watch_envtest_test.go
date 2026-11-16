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
	"testing"
	"time"

	"github.com/go-logr/logr"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/config"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// inferenceWatchNamespace and inferenceWatchAgentName are the CR under test.
	inferenceWatchNamespace = "inference-watch"
	inferenceWatchAgentName = "test-agent"
	// inferenceWatchFirstTimeout bounds the wait for the first status.inference
	// write: cache sync, the finalizer pass, a full render and the Ready write.
	inferenceWatchFirstTimeout = 90 * time.Second
	// inferenceWatchEditTimeout is how long the edited model may take to reach
	// the status. It is far below every requeue the Ready path can return
	// (usageStatusReprobeInterval, secretEnvReprobeInterval) and below the 30s
	// requeue of the gated paths, so only the ConfigMap watch can land it.
	inferenceWatchEditTimeout = 5 * time.Second
	// inferenceWatchSettle is how long the CR's resourceVersion must hold still
	// before an edit, so the window measures the edit and not a pass still
	// catching up on the fixture; and how long the no-op edit is watched.
	inferenceWatchSettle = 3 * time.Second
	// inferenceWatchPoll is how often the waits re-read the CR.
	inferenceWatchPoll = 100 * time.Millisecond
	// inferenceWatchNoOpLabel is the label the no-op update adds to the ConfigMap.
	inferenceWatchNoOpLabel = "inference-watch.test/touched"
	// inferenceWatchConfigVolume is the litellm Deployment's config volume.
	inferenceWatchConfigVolume = "config"
	inferenceWatchImage        = "litellm:envtest"
	// The provider and the two models the ConfigMap states, before and after the edit.
	inferenceWatchProvider    = "vertex_ai"
	inferenceWatchFirstModel  = "gemini-first"
	inferenceWatchEditedModel = "gemini-edited"
)

// inferenceWatchConfig is a LiteLLM config.yaml whose model-default entry
// serves model under inferenceWatchProvider.
func inferenceWatchConfig(model string) string {
	return "model_list:\n" +
		"- model_name: " + agentModelName + "\n" +
		"  litellm_params:\n" +
		"    model: " + inferenceWatchProvider + "/" + model + "\n"
}

// TestALiteLLMConfigMapEditMovesTheStatusBeforeTheRequeueEnvtest wires
// SetupWithManager under a real manager, so the ConfigMap watch and its
// ResourceVersionChangedPredicate are the ones the operator runs; the unit test
// of liteLLMConfigMapRequests covers the map function only, and a predicate
// that never passes would leave it green. A CR reaches updateStatusReady with
// a litellm Deployment mounting litellm-config, the status takes the pair, and
// an edit of config.yaml must move it within inferenceWatchEditTimeout, which
// no requeue the pass returns can meet. A label-only update then reconciles
// without changing anything observable.
//
// No Deployment controller runs under envtest, so the phase stays Provisioning;
// the Ready writer still runs on every full pass, and it is what reads the
// LiteLLM config.
func TestALiteLLMConfigMapEditMovesTheStatusBeforeTheRequeueEnvtest(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	logf.SetLogger(logr.Discard())

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsDisabledBindAddress},
		HealthProbeBindAddress: metricsDisabledBindAddress,
		Controller:             config.Controller{SkipNameValidation: ptr.To(true)},
	})
	if err != nil {
		t.Fatalf("building the manager: %v", err)
	}
	r := &PlatformAgentReconciler{Client: mgr.GetClient(), APIReader: mgr.GetAPIReader(), Scheme: scheme}
	if err := r.SetupWithManager(mgr); err != nil {
		t.Fatalf("SetupWithManager: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	managerDone := make(chan error, 1)
	go func() { managerDone <- mgr.Start(ctx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-managerDone; err != nil {
			t.Errorf("manager stopped with: %v", err)
		}
	})
	if !mgr.GetCache().WaitForCacheSync(ctx) {
		t.Fatal("the manager's caches did not sync")
	}

	direct, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("direct client: %v", err)
	}
	if err := direct.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: inferenceWatchNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: litellmConfigMapName, Namespace: inferenceWatchNamespace},
		Data:       map[string]string{litellmConfigKey: inferenceWatchConfig(inferenceWatchFirstModel)},
	}
	if err := direct.Create(ctx, cm); err != nil {
		t.Fatalf("creating the LiteLLM ConfigMap: %v", err)
	}
	labels := map[string]string{"app": litellmAppName}
	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: litellmDeploymentName, Namespace: inferenceWatchNamespace},
		Spec: appsv1.DeploymentSpec{
			Selector: &metav1.LabelSelector{MatchLabels: labels},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: labels},
				Spec: corev1.PodSpec{
					Containers: []corev1.Container{{Name: litellmDeploymentName, Image: inferenceWatchImage}},
					Volumes: []corev1.Volume{{
						Name: inferenceWatchConfigVolume,
						VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{
							LocalObjectReference: corev1.LocalObjectReference{Name: litellmConfigMapName},
						}},
					}},
				},
			},
		},
	}
	if err := direct.Create(ctx, dep); err != nil {
		t.Fatalf("creating the litellm Deployment: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: inferenceWatchAgentName, Namespace: inferenceWatchNamespace},
		Spec: agentv1alpha1.PlatformAgentSpec{
			Harness: &agentv1alpha1.HarnessSpec{
				ProjectID:   envtestHarnessProject,
				Location:    envtestHarnessLocation,
				ClusterName: envtestHarnessCluster,
			},
		},
	}
	if err := direct.Create(ctx, agent); err != nil {
		t.Fatalf("creating PlatformAgent: %v", err)
	}
	if err := direct.Create(ctx, shellSandboxKeysSecret(agent)); err != nil {
		t.Fatalf("creating sandbox keys Secret: %v", err)
	}

	key := client.ObjectKeyFromObject(agent)
	fetch := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		got := &agentv1alpha1.PlatformAgent{}
		if err := direct.Get(ctx, key, got); err != nil {
			t.Fatalf("reading the PlatformAgent back: %v", err)
		}
		return got
	}
	carries := func(pa *agentv1alpha1.PlatformAgent, model string) bool {
		return pa.Status.Inference != nil &&
			pa.Status.Inference.Provider == inferenceWatchProvider && pa.Status.Inference.Model == model
	}
	waitFor := func(model string, timeout time.Duration) (*agentv1alpha1.PlatformAgent, time.Duration) {
		t.Helper()
		start := time.Now()
		for {
			got := fetch()
			if carries(got, model) {
				return got, time.Since(start)
			}
			if time.Since(start) > timeout {
				t.Fatalf("status.inference did not carry %s/%s within %s; inference=%+v phase=%q",
					inferenceWatchProvider, model, timeout, got.Status.Inference, got.Status.Phase)
			}
			time.Sleep(inferenceWatchPoll)
		}
	}
	// settle waits until the CR's resourceVersion has held for inferenceWatchSettle.
	settle := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		deadline := time.Now().Add(inferenceWatchFirstTimeout)
		last := fetch()
		since := time.Now()
		for time.Since(since) < inferenceWatchSettle {
			if time.Now().After(deadline) {
				t.Fatalf("the CR's resourceVersion did not settle within %s", inferenceWatchFirstTimeout)
			}
			time.Sleep(inferenceWatchPoll)
			got := fetch()
			if got.ResourceVersion != last.ResourceVersion {
				last, since = got, time.Now()
			}
		}
		return last
	}

	first, took := waitFor(inferenceWatchFirstModel, inferenceWatchFirstTimeout)
	if cond := meta.FindStatusCondition(first.Status.Conditions, "Ready"); cond == nil {
		t.Fatalf("status.inference was written with no Ready condition; phase=%q", first.Status.Phase)
	}
	t.Logf("status.inference=%s/%s after %s; phase=%s", first.Status.Inference.Provider, first.Status.Inference.Model, took, first.Status.Phase)
	settled := settle()

	// The edit. Nothing else changes, so a reconcile inside the window can only
	// come from the ConfigMap watch.
	current := &corev1.ConfigMap{}
	if err := direct.Get(ctx, client.ObjectKeyFromObject(cm), current); err != nil {
		t.Fatalf("reading the ConfigMap back: %v", err)
	}
	current.Data[litellmConfigKey] = inferenceWatchConfig(inferenceWatchEditedModel)
	reconcilesBefore := platformAgentReconcileTotal(t)
	if err := direct.Update(ctx, current); err != nil {
		t.Fatalf("editing the ConfigMap: %v", err)
	}
	edited, took := waitFor(inferenceWatchEditedModel, inferenceWatchEditTimeout)
	t.Logf("the edit reached status.inference after %s (%.0f reconciles); resourceVersion %s -> %s",
		took, platformAgentReconcileTotal(t)-reconcilesBefore, settled.ResourceVersion, edited.ResourceVersion)

	// The no-op: a label change moves the ConfigMap's resourceVersion, so the
	// watch enqueues the agent, but the config is the same and nothing the
	// status derives from moved.
	before := settle()
	if err := direct.Get(ctx, client.ObjectKeyFromObject(cm), current); err != nil {
		t.Fatalf("reading the ConfigMap back: %v", err)
	}
	current.Labels = map[string]string{inferenceWatchNoOpLabel: "1"}
	reconcilesBefore = platformAgentReconcileTotal(t)
	if err := direct.Update(ctx, current); err != nil {
		t.Fatalf("relabelling the ConfigMap: %v", err)
	}
	time.Sleep(inferenceWatchSettle)
	reconciles := platformAgentReconcileTotal(t) - reconcilesBefore
	after := fetch()
	t.Logf("label-only update: %.0f reconciles, resourceVersion %s -> %s", reconciles, before.ResourceVersion, after.ResourceVersion)
	if reconciles == 0 {
		t.Errorf("a ConfigMap update that moved its resourceVersion enqueued no reconcile in %s", inferenceWatchSettle)
	}
	if !carries(after, inferenceWatchEditedModel) {
		t.Errorf("status.inference moved on a label-only update: %+v", after.Status.Inference)
	}
	if after.ResourceVersion != before.ResourceVersion {
		t.Errorf("the CR's resourceVersion moved %s -> %s on a label-only ConfigMap update", before.ResourceVersion, after.ResourceVersion)
	}
	if after.Status.Phase != before.Status.Phase {
		t.Errorf("phase moved %q -> %q on a label-only ConfigMap update", before.Status.Phase, after.Status.Phase)
	}
}
