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
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-logr/logr"
	dto "github.com/prometheus/client_model/go"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/config"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/metrics"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	// degradedLoopNamespace and degradedLoopAgentName are the CR under test.
	degradedLoopNamespace = "degraded-loop"
	degradedLoopAgentName = "test-agent"
	// degradedLoopParkTimeout bounds the wait for the CR to reach its refusal.
	// The manager has to sync its caches, add the finalizer, render every
	// object up to the sandbox-keys check and write the status; a few seconds
	// on a laptop, generous here for a loaded CI runner.
	degradedLoopParkTimeout = 90 * time.Second
	// degradedLoopPollInterval is how often the park wait re-reads the CR.
	degradedLoopPollInterval = 250 * time.Millisecond
	// degradedLoopMeasureWindow is how long the counters run once the CR is
	// parked. It covers one RequeueAfter tick (30s on this path), so the
	// reconcile count on a fixed operator is the requeue's, not zero — proof
	// the gate did not also silence the poll a parked CR still needs.
	degradedLoopMeasureWindow = 35 * time.Second
	// degradedLoopReconcileCeiling is the most reconciles the window may hold
	// with the loop fixed: the requeue tick, plus room for a late owned-object
	// event. It is a guard against a different loop appearing, not what tells
	// the fix from the bug: without the fix the same window held two reconciles
	// and two status writes, and the write count is the assertion that fails.
	degradedLoopReconcileCeiling = 10
	// reconcileTotalMetric and reconcileTotalControllerLabel are where
	// controller-runtime counts reconciles, and the label value the builder
	// derives from the kind under For().
	reconcileTotalMetric          = "controller_runtime_reconcile_total"
	reconcileTotalControllerLabel = "controller"
	reconcileTotalControllerName  = "platformagent"
	// metricsDisabledBindAddress is what metricsserver.Options reads as
	// "do not listen"; the counters are read from the registry in-process.
	metricsDisabledBindAddress = "0"
)

// statusWriteCountingClient wraps the manager's client and counts PlatformAgent
// status writes going through it, so the test can say how many the window held
// without instrumenting the controller.
type statusWriteCountingClient struct {
	client.Client
	writes atomic.Int64
}

func (c *statusWriteCountingClient) Status() client.SubResourceWriter {
	return &statusWriteCountingWriter{SubResourceWriter: c.Client.Status(), writes: &c.writes}
}

type statusWriteCountingWriter struct {
	client.SubResourceWriter
	writes *atomic.Int64
}

func (w *statusWriteCountingWriter) Update(ctx context.Context, obj client.Object, opts ...client.SubResourceUpdateOption) error {
	if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
		w.writes.Add(1)
	}
	return w.SubResourceWriter.Update(ctx, obj, opts...)
}

func (w *statusWriteCountingWriter) Patch(ctx context.Context, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
	if _, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
		w.writes.Add(1)
	}
	return w.SubResourceWriter.Patch(ctx, obj, patch, opts...)
}

// platformAgentReconcileTotal reads controller_runtime_reconcile_total for the
// PlatformAgent controller out of the process-wide registry, summed over its
// result label. The registry is global, so callers take deltas.
func platformAgentReconcileTotal(t *testing.T) float64 {
	t.Helper()
	families, err := metrics.Registry.Gather()
	if err != nil {
		t.Fatalf("gathering controller-runtime metrics: %v", err)
	}
	total := 0.0
	for _, family := range families {
		if family.GetName() != reconcileTotalMetric {
			continue
		}
		for _, m := range family.GetMetric() {
			if metricHasLabel(m, reconcileTotalControllerLabel, reconcileTotalControllerName) {
				total += m.GetCounter().GetValue()
			}
		}
	}
	return total
}

func metricHasLabel(m *dto.Metric, name, value string) bool {
	for _, pair := range m.GetLabel() {
		if pair.GetName() == name && pair.GetValue() == value {
			return true
		}
	}
	return false
}

// TestAParkedRefusalDoesNotReconcileContinuouslyEnvtest is the measurement
// #1392 asked for before the fix: the controller under its real manager, with
// the watches SetupWithManager registers, and a CR parked on a refusal — the
// sandbox-keys Secret is left uncreated, which is what a bare `helm install`
// produces, so the reconcile parks on Degraded/ShellSandboxKeysMissing after
// rendering everything else. Once parked, the reconcile and status-write
// counters run for a fixed window.
//
// Before the gate, every pass wrote status and every write re-enqueued the CR
// through the predicate-less PlatformAgent watch. The chain was bounded, not
// endless: metav1.Time serializes to the second, so the echo pass's write was
// byte-identical to the tick's and the API server dropped it without a
// resourceVersion bump or a watch event. Measured, that was two reconciles and
// two status-write requests per 30s requeue tick, with resourceVersion and
// lastReconcileTime moving every tick. After the gate the window holds the
// RequeueAfter tick and nothing else, and the status is untouched.
//
// No Deployment controller runs under envtest, so the rendered workloads never
// come up; the claim under test is the write-and-requeue behaviour of a parked
// CR, which does not depend on them.
func TestAParkedRefusalDoesNotReconcileContinuouslyEnvtest(t *testing.T) {
	cfg, scheme := startEnvtestConfig(t)
	// Without a root logger controller-runtime drops every line anyway, and
	// after thirty seconds says so with a stack trace in the middle of the
	// transcript. Discard explicitly; the counters below are the evidence.
	logf.SetLogger(logr.Discard())

	mgr, err := ctrl.NewManager(cfg, ctrl.Options{
		Scheme:                 scheme,
		Metrics:                metricsserver.Options{BindAddress: metricsDisabledBindAddress},
		HealthProbeBindAddress: metricsDisabledBindAddress,
		// One process runs every envtest case; a second manager in it would
		// otherwise refuse the controller name the first registered.
		Controller: config.Controller{SkipNameValidation: ptr.To(true)},
	})
	if err != nil {
		t.Fatalf("building the manager: %v", err)
	}
	counting := &statusWriteCountingClient{Client: mgr.GetClient()}
	r := &PlatformAgentReconciler{Client: counting, APIReader: mgr.GetAPIReader(), Scheme: scheme}
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

	// A direct client for the fixture and the readbacks, so what the test sees
	// is what the API server holds and not what the manager's cache has caught
	// up to.
	direct, err := client.New(cfg, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatalf("direct client: %v", err)
	}
	if err := direct.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: degradedLoopNamespace}}); err != nil {
		t.Fatalf("creating namespace: %v", err)
	}
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: degradedLoopAgentName, Namespace: degradedLoopNamespace},
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

	key := client.ObjectKeyFromObject(agent)
	fetch := func() *agentv1alpha1.PlatformAgent {
		t.Helper()
		got := &agentv1alpha1.PlatformAgent{}
		if err := direct.Get(ctx, key, got); err != nil {
			t.Fatalf("reading the PlatformAgent back: %v", err)
		}
		return got
	}
	parked := func(pa *agentv1alpha1.PlatformAgent) bool {
		cond := meta.FindStatusCondition(pa.Status.Conditions, "Ready")
		return pa.Status.Phase == "Degraded" && cond != nil && cond.Reason == reasonShellSandboxKeysMissing
	}

	deadline := time.Now().Add(degradedLoopParkTimeout)
	var current *agentv1alpha1.PlatformAgent
	for {
		current = fetch()
		if parked(current) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("the CR did not park on %s within %s; phase=%q conditions=%+v",
				reasonShellSandboxKeysMissing, degradedLoopParkTimeout, current.Status.Phase, current.Status.Conditions)
		}
		time.Sleep(degradedLoopPollInterval)
	}
	t.Logf("parked on %s after %s; resourceVersion=%s lastReconcileTime=%v",
		reasonShellSandboxKeysMissing, degradedLoopParkTimeout-time.Until(deadline), current.ResourceVersion, current.Status.LastReconcileTime)

	// The window. Reconciles from the registry, status writes from the client
	// wrapper, and the object's resourceVersion as the API server's own record
	// of whether anything wrote it.
	reconcilesBefore := platformAgentReconcileTotal(t)
	writesBefore := counting.writes.Load()
	rvBefore := current.ResourceVersion
	time.Sleep(degradedLoopMeasureWindow)
	reconciles := platformAgentReconcileTotal(t) - reconcilesBefore
	writes := counting.writes.Load() - writesBefore
	after := fetch()

	t.Logf("over %s with the refusal unchanged: %.0f reconciles, %d PlatformAgent status writes, resourceVersion %s -> %s",
		degradedLoopMeasureWindow, reconciles, writes, rvBefore, after.ResourceVersion)

	if writes != 0 {
		t.Errorf("%d status writes in %s with nothing about the refusal changed; each one re-enqueues the CR through the unfiltered watch (#1392)", writes, degradedLoopMeasureWindow)
	}
	if after.ResourceVersion != rvBefore {
		t.Errorf("resourceVersion moved %s -> %s across a window in which nothing about the refusal changed", rvBefore, after.ResourceVersion)
	}
	if reconciles > degradedLoopReconcileCeiling {
		t.Errorf("%.0f reconciles in %s for a parked CR, want at most %d: the requeue tick and little else", reconciles, degradedLoopMeasureWindow, degradedLoopReconcileCeiling)
	}
	if reconciles == 0 {
		t.Errorf("0 reconciles in %s: the 30s requeue a parked %s CR asks for did not fire", degradedLoopMeasureWindow, reasonShellSandboxKeysMissing)
	}
	if !parked(after) {
		t.Errorf("the CR left its refusal during the window: phase=%q conditions=%+v", after.Status.Phase, after.Status.Conditions)
	}
	if after.Status.LastReconcileTime == nil || current.Status.LastReconcileTime == nil || !after.Status.LastReconcileTime.Equal(current.Status.LastReconcileTime) {
		t.Errorf("lastReconcileTime moved %v -> %v across a window with no status write", current.Status.LastReconcileTime, after.Status.LastReconcileTime)
	}
}
