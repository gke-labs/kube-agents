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

// A script schedule applied before its profile lists the script (one
// kubectl apply, in any order) recovers by itself: it reads
// ProfileScriptUnknown, and once the profile lists the script the profile
// watch re-queues it and it reads Accepted, with no edit to the schedule. The
// same holds for a schedule created before its profile exists.
func TestAScriptScheduleIsAcceptedOnceItsProfileListsTheScriptEnvtest(t *testing.T) {
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
	gw := &fakeScheduleGateway{}
	r := &AgentScheduleReconciler{
		Client: mgr.GetClient(), Scheme: scheme,
		Submitter: gw, Tracker: &fakeScheduleTracker{state: ScheduleTaskState{Found: true}}, Stopper: &fakeScheduleStopper{result: triggerStopStopped},
		BuiltinNames: func(*agentv1alpha1.PlatformAgent) []string { return []string{"stall-watch", "late-profile"} },
	}
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
	const ns = "schedule-profile-watch"
	mustCreate(ctx, t, direct, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: ns}})
	agent := &agentv1alpha1.PlatformAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "platform-agent", Namespace: ns},
		Spec: agentv1alpha1.PlatformAgentSpec{Harness: &agentv1alpha1.HarnessSpec{
			ProjectID: envtestHarnessProject, Location: envtestHarnessLocation, ClusterName: envtestHarnessCluster,
		}},
	}
	mustCreate(ctx, t, direct, agent)

	unattended := func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
	}
	listsStallWatch := func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Scripts = []agentv1alpha1.AgentProfileScript{
			{Name: "stall-watch", Path: agentv1alpha1.AgentProfileScriptRoot + "stall_watch.py", TimeoutSeconds: 300},
		}
	}
	scriptSchedule := func(name, profile string) *agentv1alpha1.AgentSchedule {
		return &agentv1alpha1.AgentSchedule{
			ObjectMeta: metav1.ObjectMeta{
				Name: name, Namespace: ns,
				Labels: map[string]string{agentv1alpha1.AgentScheduleOriginLabel: agentv1alpha1.AgentScheduleOriginBuiltin},
				OwnerReferences: []metav1.OwnerReference{{
					APIVersion: agentv1alpha1.GroupVersion.String(), Kind: "PlatformAgent",
					Name: agent.Name, UID: agent.UID, Controller: ptr.To(true),
				}},
			},
			Spec: agentv1alpha1.AgentScheduleSpec{
				Schedule:   "0 0 * * *",
				ProfileRef: agentv1alpha1.AgentScheduleProfileRef{Name: profile},
				Script:     "stall-watch",
			},
		}
	}
	waitFor := func(name, reason string) {
		t.Helper()
		var last *metav1.Condition
		for deadline := time.Now().Add(30 * time.Second); time.Now().Before(deadline); time.Sleep(100 * time.Millisecond) {
			var s agentv1alpha1.AgentSchedule
			if err := direct.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &s); err != nil {
				t.Fatal(err)
			}
			if last = meta.FindStatusCondition(s.Status.Conditions, agentv1alpha1.AgentScheduleConditionAccepted); last != nil && last.Reason == reason {
				return
			}
		}
		t.Fatalf("%s: Accepted = %+v, want reason %s", name, last, reason)
	}

	// The profile exists but doesn't list the script yet.
	profile := testAgentProfile(ns, "fleet-audit", unattended)
	mustCreate(ctx, t, direct, &profile)
	mustCreate(ctx, t, direct, scriptSchedule("stall-watch", "fleet-audit"))
	waitFor("stall-watch", agentScheduleReasonProfileScriptUnknown)
	var live agentv1alpha1.AgentProfile
	if err := direct.Get(ctx, client.ObjectKeyFromObject(&profile), &live); err != nil {
		t.Fatal(err)
	}
	listsStallWatch(&live)
	if err := direct.Update(ctx, &live); err != nil {
		t.Fatal(err)
	}
	waitFor("stall-watch", agentScheduleReasonValid)

	// The profile doesn't exist yet, then arrives listing the script.
	mustCreate(ctx, t, direct, scriptSchedule("late-profile", "late"))
	waitFor("late-profile", agentScheduleReasonProfileNotFound)
	late := testAgentProfile(ns, "late", unattended, listsStallWatch)
	mustCreate(ctx, t, direct, &late)
	waitFor("late-profile", agentScheduleReasonValid)
}
