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

// spec.harness.tuning.warmSessions: the resolver, the gateway env, and the
// warm pods' share of the TASKS budget, the cap's fit and the session quota.

import (
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// warmAgent is the test agent on the session route, the only route a warm
// pod runs on, with mutate applied to its tuning.
func warmAgent(mutate func(*agentv1alpha1.TuningSpec)) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	tuning := &agentv1alpha1.TuningSpec{DefaultRoute: ptr.To(agentv1alpha1.DefaultRouteSession)}
	if mutate != nil {
		mutate(tuning)
	}
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: tuning}
	return agent
}

// The warm count the operator renders on the session route: unset is the
// default of 1, the field otherwise, never the cap's last slot (none below a
// cap of 2), and the fitted cap where one is stored. The gateway's
// A2A_WARM_SESSIONS carries it. On the platform route, set or by default, it
// is 0 and the gateway carries no A2A_WARM_SESSIONS at all, so a platform
// install's gateway is what it was before the field.
func TestWarmSessionsResolveAgainstTheCapAndTheRoute(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.TuningSpec)
		fitted *int
		want   int
	}{
		{"unset is the default", nil, nil, agentv1alpha1.DefaultWarmSessions},
		{"0 turns it off", func(s *agentv1alpha1.TuningSpec) { s.WarmSessions = ptr.To(0) }, nil, 0},
		{"set", func(s *agentv1alpha1.TuningSpec) { s.WarmSessions = ptr.To(3) }, nil, 3},
		{"never the last slot", func(s *agentv1alpha1.TuningSpec) {
			s.WarmSessions = ptr.To(20)
			s.MaxSessions = ptr.To(4)
		}, nil, 3},
		{"off below a cap of two", func(s *agentv1alpha1.TuningSpec) { s.MaxSessions = ptr.To(1) }, nil, 0},
		{"bounded by the fitted cap", func(s *agentv1alpha1.TuningSpec) { s.WarmSessions = ptr.To(5) }, ptr.To(4), 3},
		{"the fitted cap of 4 keeps the default one", nil, ptr.To(4), 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := warmAgent(tc.mutate)
			agent.Status.EffectiveSessionCap = tc.fitted
			if got := resolveA2AWarmSessions(agent); got != tc.want {
				t.Fatalf("resolveA2AWarmSessions = %d, want %d", got, tc.want)
			}
			if got := gatewayEnv(t, agent, a2aWarmSessionsEnvVar); got != strconv.Itoa(tc.want) {
				t.Fatalf("%s = %q, want %d", a2aWarmSessionsEnvVar, got, tc.want)
			}
		})
	}
	for _, tc := range []struct {
		name   string
		mutate func(*agentv1alpha1.TuningSpec)
	}{
		{"unset, the default", func(s *agentv1alpha1.TuningSpec) { s.DefaultRoute = nil }},
		{"unset, with warmSessions set", func(s *agentv1alpha1.TuningSpec) {
			s.DefaultRoute = nil
			s.WarmSessions = ptr.To(3)
		}},
		{"platform", func(s *agentv1alpha1.TuningSpec) {
			s.DefaultRoute = ptr.To(agentv1alpha1.DefaultRoutePlatform)
		}},
	} {
		t.Run("the platform route keeps none: "+tc.name, func(t *testing.T) {
			agent := warmAgent(tc.mutate)
			if got := resolveA2AWarmSessions(agent); got != 0 {
				t.Fatalf("resolveA2AWarmSessions = %d, want 0", got)
			}
			if _, n := envValueCount(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env, a2aWarmSessionsEnvVar); n != 0 {
				t.Fatalf("the platform-route gateway carries %s %d times, want none", a2aWarmSessionsEnvVar, n)
			}
		})
	}
	// Held on platform under a CRD that prunes the cap (sessionCapPrunedReason):
	// the route resolves to platform, so no warm pod either.
	held := warmAgent(nil)
	held.Status.Conditions = append(held.Status.Conditions, *sessionCapCondition(held, "True", sessionCapPrunedReason, sessionCapPrunedMessage))
	if got := resolveA2AWarmSessions(held); got != 0 {
		t.Fatalf("held on platform: resolveA2AWarmSessions = %d, want 0", got)
	}
}

// Each warm pod the operator renders is one TASKS consumer in the budget the
// provision script checks, and one pod in the session quota; a new TASKS is
// created wide enough for them beside maxSessions. warmSessions 0 leaves all
// three as they were before the field.
func TestWarmPodsCountInTheBudgetTheQuotaAndTheNewStreamsWidth(t *testing.T) {
	on := warmAgent(nil)
	off := warmAgent(func(s *agentv1alpha1.TuningSpec) { s.WarmSessions = ptr.To(0) })
	reserve := a2aTasksReserve(on)
	if got, want := a2aTasksConsumerBudget(on), 10*a2aTasksConsumersPerSessionOnSessionRoute+1+reserve; got != want {
		t.Errorf("budget with the default warm pod = %d, want 10 sessions, 1 warm pod and the reserve: %d", got, want)
	}
	if got, want := a2aTasksConsumerBudget(off), 10*a2aTasksConsumersPerSessionOnSessionRoute+reserve; got != want {
		t.Errorf("budget with warmSessions 0 = %d, want %d", got, want)
	}
	if got, want := a2aTasksMaxConsumers(on), a2aTasksMaxConsumers(off)+1; got != want {
		t.Errorf("a new TASKS with the default warm pod is created at %d, want %d", got, want)
	}
	quota := func(agent *agentv1alpha1.PlatformAgent) int64 {
		q := buildA2ASessionQuota(agent).Spec.Hard[corev1.ResourcePods]
		return q.Value()
	}
	if got, want := quota(on), quota(off)+1; got != want {
		t.Errorf("session quota pods with the default warm pod = %d, want %d", got, want)
	}
	// The default install (defaultRoute unset, which is platform): the budget,
	// a new TASKS's width and the session quota are what they were before the
	// field, warmSessions set or not.
	for _, mutate := range []func(*agentv1alpha1.TuningSpec){
		func(s *agentv1alpha1.TuningSpec) { s.DefaultRoute = nil },
		func(s *agentv1alpha1.TuningSpec) { s.DefaultRoute = nil; s.WarmSessions = ptr.To(3) },
	} {
		platform := warmAgent(mutate)
		bare := a2aTestAgent()
		if got, want := a2aTasksConsumerBudget(platform), 10*a2aSessionConsumersPerSession+a2aTasksReserve(platform); got != want {
			t.Errorf("platform-route budget = %d, want %d with no warm pod", got, want)
		}
		if got, want := a2aTasksMaxConsumers(platform), a2aTasksMaxConsumers(bare); got != want {
			t.Errorf("platform-route TASKS width = %d, want the bare install's %d", got, want)
		}
		if got, want := quota(platform), quota(bare); got != want {
			t.Errorf("platform-route session quota pods = %d, want the bare install's %d", got, want)
		}
	}
}

// The fit end to end: the operator fits the cap as it always has and funds
// warm pods from what the cap leaves, the provision script's closing check
// requires the same budget, and its own fit, run under bash, says the same
// cap and warm count. A TASKS of 110 (an install created on the platform
// route) fits 4 sessions at 7 beside the reserve of 80 and leaves 2 spare:
// one warm pod, 80 + 28 + 1 = 109. One of 108 leaves none: cap 4, no warm
// pod. One of exactly 150 (the session budget with nothing spare) keeps its
// cap of 10 and runs no warm pod, so the pool never costs it a session; 151,
// the width a new TASKS is created at, funds it.
func TestTheFitFundsWarmPodsFromSpareConsumersInTheOperatorAndTheScript(t *testing.T) {
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("bash is required to execute the provision script's arithmetic: %v", err)
	}
	ctx := context.Background()
	for _, tc := range []struct {
		width, wantCap, wantWarm, wantBudget int
	}{
		{110, 4, 1, 109},
		{108, 4, 0, 108},
		{150, 10, 0, 150},
		{151, 10, 1, 151},
	} {
		t.Run(fmt.Sprintf("width %d", tc.width), func(t *testing.T) {
			r, cl, _, agent := sessionCapRig(t, nil, tasksAt(tc.width))
			r.resolveA2ASessionCap(ctx, agent, nil)
			if got := resolveA2AEffectiveSessions(agent); got != tc.wantCap {
				t.Fatalf("effective cap = %d, want %d", got, tc.wantCap)
			}
			if got := resolveA2AWarmSessions(agent); got != tc.wantWarm {
				t.Fatalf("warm pods = %d, want %d", got, tc.wantWarm)
			}
			if got := storedCapAgent(t, cl, agent).Status.EffectiveWarmSessions; got == nil || *got != tc.wantWarm {
				t.Fatalf("stored effectiveWarmSessions = %v, want %d", got, tc.wantWarm)
			}
			if got := gatewayEnv(t, agent, a2aWarmSessionsEnvVar); got != strconv.Itoa(tc.wantWarm) {
				t.Fatalf("%s = %q, want %d", a2aWarmSessionsEnvVar, got, tc.wantWarm)
			}
			budget := a2aTasksConsumerBudget(agent)
			if budget != tc.wantBudget || budget > tc.width {
				t.Fatalf("budget = %d, want %d, within the live width %d", budget, tc.wantBudget, tc.width)
			}
			script := a2aProvisionScript(agent)
			if !strings.Contains(script, "required_consumers="+strconv.Itoa(budget)+"\n") {
				t.Fatalf("the provision script does not require the budget %d", budget)
			}
			block := a2aProvisionSessionsFitBlock(a2aTasksReserve(agent), a2aTasksConsumersPerSession(agent),
				a2aFitWarmSessions(resolveA2ADefaultRoute(agent), agent), resolveA2AMaxSessions(agent))
			if !strings.Contains(script, block) {
				t.Fatal("the rendered provision script does not carry the fit block with the warm pods")
			}
			run := fmt.Sprintf("tasks_json='{\"name\":\"TASKS\",\"max_consumers\":%d}'\n%sprintf '%%s %%s' \"${fits}\" \"${warm_fits}\"\n", tc.width, block)
			out, err := exec.Command(bash, "-c", run).Output()
			if err != nil {
				t.Fatalf("bash: %v", err)
			}
			var fits, warm int
			if _, err := fmt.Sscanf(string(out), "%d %d", &fits, &warm); err != nil {
				t.Fatalf("the block printed %q", out)
			}
			if min(fits, resolveA2AMaxSessions(agent)) != tc.wantCap || warm != tc.wantWarm {
				t.Fatalf("the script fits cap %d and %d warm at %d, the operator %d and %d", fits, warm, tc.width, tc.wantCap, tc.wantWarm)
			}
		})
	}
}

// A served CRD from before the warm pool keeps status.effectiveSessionCap and
// prunes status.effectiveWarmSessions. The count can't be stored, so it fails
// safe: the session route and its fitted cap stand, no warm pod renders (the
// budget, the quota and A2A_WARM_SESSIONS all count 0), and later passes do
// not write the count again until the reprobe, which writes it once more.
func TestAPrunedWarmCountRendersNoWarmPodAndIsNotRewritten(t *testing.T) {
	agent := sessionRouteTestAgent()
	patches := 0
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			patches++
			if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok {
				pa.Status.EffectiveWarmSessions = nil // the older CRD's schema
			}
			return c.SubResource(sub).Patch(ctx, obj, patch, opts...)
		}}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(16), busStreamState: tasksAt(110)}
	ctx := context.Background()
	first := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, first, nil)
	if !r.sessionWarmStatusPruned(first) {
		t.Fatal("the echo without the warm count was not noticed")
	}
	if r.sessionCapStatusPruned(first) {
		t.Fatal("an echo that kept the cap was read as pruning the cap")
	}
	check := func(pass *agentv1alpha1.PlatformAgent, when string) {
		t.Helper()
		if got := resolveA2ADefaultRoute(pass); got != agentv1alpha1.DefaultRouteSession {
			t.Errorf("%s: route %q, want the session route kept", when, got)
		}
		if got := resolveA2AEffectiveSessions(pass); got != 4 {
			t.Errorf("%s: effective cap %d, want the fitted 4", when, got)
		}
		if got := resolveA2AWarmSessions(pass); got != 0 {
			t.Errorf("%s: warm pods %d, want none while the count can't be stored", when, got)
		}
		if got := gatewayEnv(t, pass, a2aWarmSessionsEnvVar); got != "0" {
			t.Errorf("%s: %s = %q, want 0", when, a2aWarmSessionsEnvVar, got)
		}
		if got, want := a2aTasksConsumerBudget(pass), 4*a2aTasksConsumersPerSessionOnSessionRoute+a2aTasksReserve(pass); got != want {
			t.Errorf("%s: budget %d, want %d with no warm pod", when, got, want)
		}
	}
	check(first, "the pass that noticed")
	if got := storedCapAgent(t, cl, agent).Status.EffectiveSessionCap; got == nil || *got != 4 {
		t.Fatalf("stored cap = %v, want 4", got)
	}
	before := patches
	for i := 0; i < 3; i++ {
		pass := storedCapAgent(t, cl, agent)
		r.resolveA2ASessionCap(ctx, pass, nil)
		check(pass, fmt.Sprintf("pass %d", i+1))
	}
	if patches != before {
		t.Errorf("%d status patches on passes that changed nothing but the pruned count, want none", patches-before)
	}
	// Past the reprobe the count is written again, and still renders none.
	r.sessionWarmPruned.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-2*sessionCapPrunedReprobe))
	pass := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, pass, nil)
	if patches != before+1 {
		t.Errorf("the reprobe made %d patches, want one", patches-before)
	}
	check(pass, "the reprobe")
}
