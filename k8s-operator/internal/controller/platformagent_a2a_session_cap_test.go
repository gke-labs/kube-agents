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

// The session cap fitted to the TASKS stream's live width
// (platformagent_a2a_session_cap.go): the fit, the shell's agreement with it,
// the status flow, what renders from the effective cap and what stays on the
// configured one, the ordering guard, the floor, and the signal.

import (
	"context"
	goerrors "errors"
	"fmt"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// tasksAt is a bus whose TASKS holds width consumers.
func tasksAt(width int) func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
	return func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		return map[string]a2aBusStream{a2aTasksStream: {MaxConsumers: ptr.To(width)}}, nil
	}
}

func noTasks(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
	return map[string]a2aBusStream{"KV_cap": {}}, nil
}

func unreadableBus(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
	return nil, goerrors.New("dial tcp: connection refused")
}

// sessionCapRig is a stored agent on the default (session) route and a
// reconciler reading the bus through streams.
func sessionCapRig(t *testing.T, mutate func(*agentv1alpha1.PlatformAgent), streams func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error)) (*PlatformAgentReconciler, client.Client, *record.FakeRecorder, *agentv1alpha1.PlatformAgent) {
	t.Helper()
	agent := a2aTestAgent()
	if mutate != nil {
		mutate(agent)
	}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
	rec := record.NewFakeRecorder(16)
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: rec, busStreamState: streams}
	stored := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), stored); err != nil {
		t.Fatal(err)
	}
	return r, cl, rec, stored
}

func storedCapAgent(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.PlatformAgent {
	t.Helper()
	got := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(context.Background(), client.ObjectKeyFromObject(agent), got); err != nil {
		t.Fatal(err)
	}
	return got
}

func gatewayEnv(t *testing.T, agent *agentv1alpha1.PlatformAgent, name string) string {
	t.Helper()
	v, n := envValueCount(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env, name)
	if n != 1 {
		t.Fatalf("the gateway carries %s %d times, want once", name, n)
	}
	return v
}

// The fit's own arithmetic, at the default install's reserve (80, the
// rendered bridge's 10 workers) and the session route's 7 a session.
func TestA2ASessionsFit(t *testing.T) {
	per := a2aTasksConsumersPerSessionOnSessionRoute
	if per != 7 {
		t.Fatalf("the session route counts %d a session, want 3 named consumers plus 4 asks", per)
	}
	for _, tc := range []struct {
		live, want int
	}{
		{164, 12}, {150, 10}, {110, 4}, {83, 0}, {80, 0}, {50, -4}, {-1, a2aSessionsFitUnbounded},
	} {
		if got := a2aSessionsFit(tc.live, 80, per); got != tc.want {
			t.Errorf("a2aSessionsFit(%d, 80, %d) = %d, want %d", tc.live, per, got, tc.want)
		}
	}
}

// The provision script's closing check computes the same fit: its block is
// run under bash against a stubbed tasks_json at widths giving a cap above
// maxSessions, exactly at it, between 1 and it, 0, negative (a reserve wider
// than the stream), and -1 (no limit), and its fits must equal
// a2aSessionsFit. The block run is the one the rendered script carries.
func TestTheProvisionScriptFitsWhatTheOperatorFits(t *testing.T) {
	bash, err := exec.LookPath("bash")
	if err != nil {
		t.Fatalf("bash is required to execute the provision script's arithmetic: %v", err)
	}
	agent := a2aTestAgent() // the session route at maxSessions=10
	reserve, per := a2aTasksReserve(agent), a2aTasksConsumersPerSession(agent)
	if per != a2aTasksConsumersPerSessionOnSessionRoute {
		t.Fatalf("precondition: the default agent counts %d a session, not the session route's %d", per, a2aTasksConsumersPerSessionOnSessionRoute)
	}
	block := a2aProvisionSessionsFitBlock(reserve, per)
	if !strings.Contains(a2aProvisionScript(agent), block) {
		t.Fatal("the rendered provision script does not carry a2aProvisionSessionsFitBlock's arithmetic verbatim")
	}
	configured := resolveA2AMaxSessions(agent)
	for _, tc := range []struct {
		name          string
		live          int
		wantEffective int
	}{
		{"above maxSessions", reserve + per*(configured+2), configured},
		{"exactly maxSessions", reserve + per*configured, configured},
		{"between 1 and maxSessions", reserve + per*4, 4},
		{"zero", reserve + per - 1, 0},
		{"negative", reserve - 30, 0},
		{"no limit", -1, configured},
	} {
		t.Run(tc.name, func(t *testing.T) {
			script := fmt.Sprintf("tasks_json='{\"name\":\"TASKS\",\"max_consumers\":%d,\"max_msgs_per_subject\":4096}'\n%sprintf '%%s' \"${fits}\"\n", tc.live, block)
			out, err := exec.Command(bash, "-c", script).Output()
			if err != nil {
				t.Fatalf("bash: %v", err)
			}
			shell, err := strconv.Atoi(string(out))
			if err != nil {
				t.Fatalf("the block printed %q, not an integer", out)
			}
			want := a2aSessionsFit(tc.live, reserve, a2aTasksConsumersPerSessionOnSessionRoute)
			if shell != want {
				t.Errorf("live %d: the shell fits %d, a2aSessionsFit says %d", tc.live, shell, want)
			}
			if got := max(min(configured, want), 0); got != tc.wantEffective {
				t.Errorf("live %d: effective cap %d, want %d", tc.live, got, tc.wantEffective)
			}
		})
	}
}

// /jsz is asked for stream config, and TASKS' max_consumers is decoded from
// it; a report without config is no reading.
func TestTheBusStreamReportCarriesMaxConsumers(t *testing.T) {
	if !strings.Contains(a2aBusJszStreamsPath, "streams=true") || !strings.Contains(a2aBusJszStreamsPath, "config=true") ||
		strings.Contains(a2aBusJszStreamsPath, "consumers=true") {
		t.Errorf("a2aBusJszStreamsPath = %q, want streams and config without consumers", a2aBusJszStreamsPath)
	}
	body := `{"account_details":[{"stream_detail":[
		{"name":"TASKS","config":{"max_consumers":110},"state":{"bytes":5,"messages":2}},
		{"name":"KV_cap","state":{"bytes":1}}]}]}`
	streams, err := decodeA2AJsz("u", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if got := streams[a2aTasksStream]; got.MaxConsumers == nil || *got.MaxConsumers != 110 || got.Bytes != 5 || got.Msgs != 2 {
		t.Errorf("TASKS decoded as %+v, want max_consumers 110 beside its state", got)
	}
	if streams["KV_cap"].MaxConsumers != nil {
		t.Errorf("a stream with no config decoded a max_consumers")
	}
}

// The ordering guard's four cases, and what each renders.
func TestTheSessionCapFollowsTheLiveTasksWidth(t *testing.T) {
	ctx := context.Background()

	t.Run("TASKS absent on a bus never provisioned is maxSessions", func(t *testing.T) {
		r, cl, _, agent := sessionCapRig(t, nil, noTasks)
		r.resolveA2ASessionCap(ctx, agent, nil)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap == nil || *got.Status.EffectiveSessionCap != 10 {
			t.Fatalf("stored effectiveSessionCap = %v, want 10", got.Status.EffectiveSessionCap)
		}
		if c := meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType); c != nil {
			t.Errorf("SessionCapFitted %+v on an unfitted cap", c)
		}
		if resolveA2ADefaultRoute(agent) != agentv1alpha1.DefaultRouteSession || gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "10" {
			t.Errorf("route %q, A2A_MAX_SESSIONS %q; want session at 10", resolveA2ADefaultRoute(agent), gatewayEnv(t, agent, "A2A_MAX_SESSIONS"))
		}
	})

	t.Run("TASKS present is fitted", func(t *testing.T) {
		r, cl, rec, agent := sessionCapRig(t, nil, tasksAt(110))
		r.resolveA2ASessionCap(ctx, agent, nil)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap == nil || *got.Status.EffectiveSessionCap != 4 {
			t.Fatalf("stored effectiveSessionCap = %v, want 4: (110 - 80) / 7", got.Status.EffectiveSessionCap)
		}
		c := meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType)
		if c == nil || c.Status != metav1.ConditionTrue || c.Reason != sessionCapNarrowReason {
			t.Fatalf("SessionCapFitted = %+v, want True/%s", c, sessionCapNarrowReason)
		}
		for _, digit := range []string{"110", "150", "4"} {
			if regexp.MustCompile(`\b` + digit + `\b`).MatchString(c.Message) {
				t.Errorf("the condition's message carries the figure %s; figures go in the event: %s", digit, c.Message)
			}
		}
		if !strings.Contains(c.Message, "#6466") {
			t.Errorf("the condition does not name the reshape: %s", c.Message)
		}
		events := drainEvents(rec)
		if len(events) != 1 || !strings.Contains(events[0], corev1.EventTypeWarning) ||
			!strings.Contains(events[0], "max_consumers=110") || !strings.Contains(events[0], "needs 150") ||
			!strings.Contains(events[0], "fitted to 4") {
			t.Errorf("events %q, want one Warning naming the live 110, the 150 needed and the cap 4", events)
		}
		if resolveA2ADefaultRoute(agent) != agentv1alpha1.DefaultRouteSession {
			t.Errorf("route %q on a fitted cap of 4, want session", resolveA2ADefaultRoute(agent))
		}
	})

	t.Run("unreadable with no stored cap waits, rendering what the previous operator did", func(t *testing.T) {
		r, cl, rec, agent := sessionCapRig(t, nil, unreadableBus)
		r.resolveA2ASessionCap(ctx, agent, nil)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap != nil {
			t.Errorf("stored effectiveSessionCap = %d with no reading, want absent", *got.Status.EffectiveSessionCap)
		}
		c := meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType)
		if c == nil || c.Status != metav1.ConditionUnknown || c.Reason != sessionCapWaitingReason {
			t.Fatalf("SessionCapFitted = %+v, want Unknown/%s", c, sessionCapWaitingReason)
		}
		if !a2aSessionCapWaiting(agent) {
			t.Error("the render copy does not read as waiting, so the provision Job would not be held")
		}
		if resolveA2ADefaultRoute(agent) != agentv1alpha1.DefaultRoutePlatform || gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "10" {
			t.Errorf("route %q, A2A_MAX_SESSIONS %q; want platform at 10, what the operator before the flip rendered", resolveA2ADefaultRoute(agent), gatewayEnv(t, agent, "A2A_MAX_SESSIONS"))
		}
		if events := drainEvents(rec); len(events) != 0 {
			t.Errorf("events %q on a wait, want none", events)
		}
		// An explicit `session` is what the previous operator rendered too.
		explicit := agent.DeepCopy()
		explicit.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: ptr.To(agentv1alpha1.DefaultRouteSession)}}
		if resolveA2ADefaultRoute(explicit) != agentv1alpha1.DefaultRouteSession {
			t.Errorf("an explicit session route waiting on the width renders %q, want session as before", resolveA2ADefaultRoute(explicit))
		}
	})

	t.Run("unreadable with a stored cap keeps it", func(t *testing.T) {
		r, cl, _, agent := sessionCapRig(t, nil, tasksAt(110))
		r.resolveA2ASessionCap(ctx, agent, nil)
		before := storedCapAgent(t, cl, agent)
		for _, streams := range []func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error){
			unreadableBus,
			// A provisioned bus missing TASKS is no reading either.
			noTasks,
		} {
			r.busStreamState = streams
			agent := storedCapAgent(t, cl, agent)
			setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
			r.resolveA2ASessionCap(ctx, agent, nil)
			if agent.Status.EffectiveSessionCap == nil || *agent.Status.EffectiveSessionCap != 4 {
				t.Errorf("effectiveSessionCap = %v after an unreadable pass, want the stored 4", agent.Status.EffectiveSessionCap)
			}
			if c := meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType); c == nil || c.Reason != sessionCapNarrowReason {
				t.Errorf("SessionCapFitted = %+v after an unreadable pass, want the stored %s", c, sessionCapNarrowReason)
			}
			if a2aSessionCapWaiting(agent) || gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "4" {
				t.Errorf("an unreadable pass with a stored cap waits (%v) or renders A2A_MAX_SESSIONS %q, want 4", a2aSessionCapWaiting(agent), gatewayEnv(t, agent, "A2A_MAX_SESSIONS"))
			}
		}
		if after := storedCapAgent(t, cl, agent); after.ResourceVersion != before.ResourceVersion {
			t.Error("an unreadable pass wrote status")
		}
	})

	t.Run("a migration under way is no reading", func(t *testing.T) {
		r, _, _, agent := sessionCapRig(t, func(a *agentv1alpha1.PlatformAgent) {
			a.Status.BusMigration = &agentv1alpha1.BusMigrationStatus{Phase: string(busMigrationReleasing)}
		}, noTasks)
		if !busMigrationHoldsProvision(agent) {
			t.Fatal("precondition: the phase chosen does not hold provisioning")
		}
		r.resolveA2ASessionCap(ctx, agent, nil)
		if agent.Status.EffectiveSessionCap != nil || !a2aSessionCapWaiting(agent) {
			t.Errorf("a pass mid-migration took a reading: cap %v, waiting %v", agent.Status.EffectiveSessionCap, a2aSessionCapWaiting(agent))
		}
	})

	t.Run("the platform route fits nothing and clears what was stored", func(t *testing.T) {
		r, cl, _, agent := sessionCapRig(t, nil, tasksAt(110))
		r.resolveA2ASessionCap(ctx, agent, nil)
		agent = storedCapAgent(t, cl, agent)
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: ptr.To(agentv1alpha1.DefaultRoutePlatform)}}
		r.resolveA2ASessionCap(ctx, agent, nil)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap != nil || meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType) != nil {
			t.Errorf("on platform: cap %v, condition %+v; want neither", got.Status.EffectiveSessionCap, meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType))
		}
		if gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "10" || resolveA2ADefaultRoute(agent) != agentv1alpha1.DefaultRoutePlatform {
			t.Errorf("platform renders A2A_MAX_SESSIONS %q route %q, want 10 on platform", gatewayEnv(t, agent, "A2A_MAX_SESSIONS"), resolveA2ADefaultRoute(agent))
		}
	})
}

// The effective cap reaches the gateway's cap, the session quota and the
// provision Job's check, and does not reach the width a new TASKS is created
// at, which stays on maxSessions.
func TestTheEffectiveCapRendersEverywhereButTheCreateWidth(t *testing.T) {
	agent := a2aTestAgent()
	agent.Status.EffectiveSessionCap = ptr.To(4)
	reserve := a2aTasksReserve(agent)
	if got := gatewayEnv(t, agent, "A2A_MAX_SESSIONS"); got != "4" {
		t.Errorf("A2A_MAX_SESSIONS = %q, want the effective 4", got)
	}
	quota := buildA2ASessionQuota(agent).Spec.Hard[corev1.ResourcePods]
	if want := int64(4) + a2aQuotaHeadroom(agent); quota.Value() != want {
		t.Errorf("session quota pods = %d, want %d: the effective cap plus the headroom", quota.Value(), want)
	}
	script := a2aProvisionScript(agent)
	if want := "required_consumers=" + strconv.Itoa(4*7+reserve) + "\n"; !strings.Contains(script, want) {
		t.Errorf("the provision script does not check %q", strings.TrimSpace(want))
	}
	if want := "--max-consumers=" + strconv.Itoa(10*7+reserve) + " "; !strings.Contains(script, want) {
		t.Errorf("the provision script does not create TASKS at %q, maxSessions' width", strings.TrimSpace(want))
	}
	if a2aTasksMaxConsumers(agent) != 10*7+reserve {
		t.Errorf("a2aTasksMaxConsumers = %d, want %d", a2aTasksMaxConsumers(agent), 10*7+reserve)
	}
	// A maxSessions lowered under the stored cap wins.
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: ptr.To(3)}}
	if got := resolveA2AEffectiveSessions(agent); got != 3 {
		t.Errorf("maxSessions 3 under a stored cap of 4 renders %d, want 3", got)
	}
	// On the fitted cap the narrowed install passes its own check: the
	// provision script's refusal is not reached at the live width.
	fitted := a2aTestAgent()
	fitted.Status.EffectiveSessionCap = ptr.To(4)
	if code, stderr := runProvision(t, fitted, 110); code != 0 {
		t.Errorf("a narrowed install's provision Job exits %d against the TASKS it was fitted to:\n%s", code, stderr)
	}
}

// The floor: a stream with no room for one session sends plain conversations
// to platform, on both containers, never at an unlimited or negative cap.
func TestNoRoomForASessionFallsBackToPlatform(t *testing.T) {
	for _, width := range []int{86, 50} { // (86-80)/7 = 0; (50-80)/7 < 0
		r, cl, rec, agent := sessionCapRig(t, nil, tasksAt(width))
		r.resolveA2ASessionCap(context.Background(), agent, nil)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap == nil || *got.Status.EffectiveSessionCap != 0 {
			t.Fatalf("width %d: effectiveSessionCap = %v, want 0", width, got.Status.EffectiveSessionCap)
		}
		if c := meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType); c == nil || c.Reason != sessionCapNoRoomReason {
			t.Errorf("width %d: SessionCapFitted %+v, want %s", width, c, sessionCapNoRoomReason)
		}
		gateway, platformAgent := renderedDefaultRoutes(t, agent, nil)
		if gateway != agentv1alpha1.DefaultRoutePlatform || platformAgent != agentv1alpha1.DefaultRoutePlatform {
			t.Errorf("width %d: gateway %q, platform agent %q; both want platform", width, gateway, platformAgent)
		}
		// On platform the cap is maxSessions, at the platform route's share.
		if gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "10" || a2aTasksConsumersPerSession(agent) != a2aSessionConsumersPerSession {
			t.Errorf("width %d: fallback renders A2A_MAX_SESSIONS %q at %d a session", width, gatewayEnv(t, agent, "A2A_MAX_SESSIONS"), a2aTasksConsumersPerSession(agent))
		}
		if events := drainEvents(rec); len(events) != 1 || !strings.Contains(events[0], "Not one session fits") {
			t.Errorf("width %d: events %q, want one Warning saying not one session fits", width, events)
		}
		// The fallback cannot feed back: the next pass fits at the session
		// route's 7, not the platform route's 3, and stays at 0.
		r.resolveA2ASessionCap(context.Background(), got, nil)
		if *got.Status.EffectiveSessionCap != 0 {
			t.Errorf("width %d: the pass after the fallback fits %d", width, *got.Status.EffectiveSessionCap)
		}
	}
}

// The Warning and the status write happen when the fit changes, not on
// every pass; an unfitted cap is no event, and recovery clears the condition.
func TestTheSessionCapSignalIsGatedOnChange(t *testing.T) {
	ctx := context.Background()
	r, cl, rec, agent := sessionCapRig(t, nil, tasksAt(110))
	r.resolveA2ASessionCap(ctx, agent, nil)
	if n := len(drainEvents(rec)); n != 1 {
		t.Fatalf("%d events on the first fitted pass, want 1", n)
	}
	first := storedCapAgent(t, cl, agent)
	again := first.DeepCopy()
	r.resolveA2ASessionCap(ctx, again, nil)
	if n := len(drainEvents(rec)); n != 0 {
		t.Errorf("%d events on a pass at the same width, want none", n)
	}
	if storedCapAgent(t, cl, agent).ResourceVersion != first.ResourceVersion {
		t.Error("a pass at the same width wrote status")
	}
	// A wider stream, still short: one new Warning for the new figures, the
	// condition unchanged.
	r.busStreamState = tasksAt(124)
	wider := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, wider, nil)
	if events := drainEvents(rec); len(events) != 1 || !strings.Contains(events[0], "fitted to 6") {
		t.Errorf("events %q on a wider stream, want one naming the cap 6", events)
	}
	// Widened to fit: the condition goes, and no Warning.
	r.busStreamState = tasksAt(150)
	widened := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, widened, nil)
	got := storedCapAgent(t, cl, agent)
	if c := meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType); c != nil || *got.Status.EffectiveSessionCap != 10 {
		t.Errorf("a stream that fits leaves cap %d and condition %+v", *got.Status.EffectiveSessionCap, c)
	}
	if n := len(drainEvents(rec)); n != 0 {
		t.Errorf("%d events once the stream fits, want none", n)
	}
	// And the in-memory instance is carried along, so the pass's later
	// status writes neither drop the cap nor conflict on its version.
	r.busStreamState = tasksAt(110)
	render, instance := storedCapAgent(t, cl, agent), storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, render, instance)
	if instance.Status.EffectiveSessionCap == nil || *instance.Status.EffectiveSessionCap != 4 || instance.ResourceVersion != render.ResourceVersion {
		t.Errorf("instance carries cap %v at version %q, render %q", instance.Status.EffectiveSessionCap, instance.ResourceVersion, render.ResourceVersion)
	}
}

// Through Reconcile: with the bus's width unreadable and no stored cap, the
// provision Job is not created and the gateway keeps platform routing; once
// the width reads, the Job is created at the fitted cap and the gateway
// moves to the session route.
func TestTheProvisionJobWaitsForTheBusWidth(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	r.busStreamState = unreadableBus
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Items) != 0 {
		t.Fatalf("%d provision Jobs exist while the session cap waits on the bus's width, want none", len(jobs.Items))
	}
	stored := storedCapAgent(t, cl, agent)
	if c := meta.FindStatusCondition(stored.Status.Conditions, sessionCapFittedConditionType); c == nil || c.Reason != sessionCapWaitingReason {
		t.Fatalf("SessionCapFitted = %+v, want %s", c, sessionCapWaitingReason)
	}
	gw := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, gw); err != nil {
		t.Fatalf("precondition: the gateway is not rendered: %v", err)
	}
	if v, _ := envValueCount(gw.Spec.Template.Spec.Containers[0].Env, a2aDefaultAddresseeEnvVar); v != agentv1alpha1.DefaultRoutePlatform {
		t.Errorf("the waiting gateway routes to %q, want platform", v)
	}

	r.busStreamState = tasksAt(110)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	stored = storedCapAgent(t, cl, agent)
	if stored.Status.EffectiveSessionCap == nil || *stored.Status.EffectiveSessionCap != 4 {
		t.Fatalf("effectiveSessionCap = %v once the width reads, want 4", stored.Status.EffectiveSessionCap)
	}
	if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Items) != 1 || jobs.Items[0].Name != buildA2AProvisionJob(stored).Name {
		t.Fatalf("provision Jobs %d once the width reads, want the one rendered at the fitted cap", len(jobs.Items))
	}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, gw); err != nil {
		t.Fatal(err)
	}
	env := gw.Spec.Template.Spec.Containers[0].Env
	if v, _ := envValueCount(env, a2aDefaultAddresseeEnvVar); v != agentv1alpha1.DefaultRouteSession {
		t.Errorf("the gateway routes to %q once the width reads, want session", v)
	}
	if v, _ := envValueCount(env, "A2A_MAX_SESSIONS"); v != "4" {
		t.Errorf("the gateway's A2A_MAX_SESSIONS = %q, want the fitted 4", v)
	}
}
