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
	corev1 "k8s.io/api/core/v1"
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

// agentProfileReconcileTotal is controller-runtime's reconcile count for the
// AgentProfile controller.
func agentProfileReconcileTotal(t *testing.T) float64 {
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
			if metricHasLabel(m, reconcileTotalControllerLabel, "agentprofile") {
				total += m.GetCounter().GetValue()
			}
		}
	}
	return total
}

// A Tier created in a profile's namespace re-reconciles the profile at once
// (#3659): which listed tiers render depends on the Tier resources, and
// without the watch a profile listing a tier created after it would wait for
// the five-minute resync, with no ServiceAccount for the pool the
// PlatformAgent controller has already rendered.
func TestATierChangeReconcilesTheProfilesInItsNamespaceEnvtest(t *testing.T) {
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
	r := &AgentProfileReconciler{Client: mgr.GetClient(), Scheme: scheme}
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
		t.Fatal(err)
	}
	const ns = "tier-watch"
	if err := direct.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}}); err != nil {
		t.Fatal(err)
	}
	p := testAgentProfile(ns, "tse", func(p *agentv1alpha1.AgentProfile) { p.Spec.AllowedTiers = []string{"read", "oncall"} })
	if err := direct.Create(ctx, &p); err != nil {
		t.Fatal(err)
	}

	// Let the profile's own passes finish: the count holds still for 3s.
	settled := func() float64 {
		last, since := agentProfileReconcileTotal(t), time.Now()
		for deadline := time.Now().Add(60 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
			if n := agentProfileReconcileTotal(t); n != last {
				last, since = n, time.Now()
			} else if last > 0 && time.Since(since) > 3*time.Second {
				return last
			}
		}
		t.Fatal("the profile's reconciles never settled")
		return 0
	}
	before := settled()

	tier := &agentv1alpha1.Tier{
		ObjectMeta: metav1.ObjectMeta{Name: "oncall", Namespace: ns},
		Spec:       agentv1alpha1.TierSpec{Mode: agentv1alpha1.TierModeInteractive, WriteMode: agentv1alpha1.TierWriteModeNone},
	}
	if err := direct.Create(ctx, tier); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(10 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
		if agentProfileReconcileTotal(t) > before {
			return
		}
	}
	t.Fatalf("creating a Tier did not reconcile the profile in its namespace (reconciles stayed at %v)", before)
}
