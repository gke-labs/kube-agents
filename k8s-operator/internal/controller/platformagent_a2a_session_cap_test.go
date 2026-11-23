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
	"time"

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
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// tasksAt is a bus whose TASKS holds width consumers.
func tasksAt(width int) func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
	return func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		return map[string]a2aBusStream{a2aTasksStream: {MaxConsumers: width}}, nil
	}
}

func noTasks(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
	return map[string]a2aBusStream{"KV_cap": {}}, nil
}

func unreadableBus(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
	return nil, goerrors.New("dial tcp: connection refused")
}

// sessionRouteTestAgent is the bare test agent opted into the session route
// (spec.harness.tuning.defaultRoute: session), the only route the session cap
// is fitted on; an unset field is the platform route.
func sessionRouteTestAgent() *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: ptr.To(agentv1alpha1.DefaultRouteSession)}}
	return agent
}

// sessionCapRig is a stored agent on the session route and a reconciler
// reading the bus through streams.
func sessionCapRig(t *testing.T, mutate func(*agentv1alpha1.PlatformAgent), streams func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error)) (*PlatformAgentReconciler, client.Client, *record.FakeRecorder, *agentv1alpha1.PlatformAgent) {
	t.Helper()
	agent := sessionRouteTestAgent()
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
	agent := sessionRouteTestAgent() // the session route at maxSessions=10
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
	if got := streams[a2aTasksStream]; got.MaxConsumers != 110 || got.Bytes != 5 || got.Msgs != 2 {
		t.Errorf("TASKS decoded as %+v, want max_consumers 110 beside its state", got)
	}
	if streams["KV_cap"].MaxConsumers != 0 {
		t.Errorf("a stream with no config decoded a max_consumers, want 0 (unreported)")
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
		if !strings.Contains(c.Message, a2aBusReshapeAnnotation) {
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

	t.Run("unreadable on a bus never provisioned is maxSessions, with no wait", func(t *testing.T) {
		r, cl, _, agent := sessionCapRig(t, nil, unreadableBus)
		r.resolveA2ASessionCap(ctx, agent, nil)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap == nil || *got.Status.EffectiveSessionCap != 10 {
			t.Fatalf("stored effectiveSessionCap = %v on a fresh bus, want 10", got.Status.EffectiveSessionCap)
		}
		if a2aSessionCapWaiting(agent) || meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType) != nil {
			t.Errorf("a fresh install waits on the bus's width: %+v", meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType))
		}
		if resolveA2ADefaultRoute(agent) != agentv1alpha1.DefaultRouteSession || gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "10" {
			t.Errorf("route %q, A2A_MAX_SESSIONS %q; want the spec's session route at 10", resolveA2ADefaultRoute(agent), gatewayEnv(t, agent, "A2A_MAX_SESSIONS"))
		}
	})

	t.Run("a report without config for TASKS is no reading", func(t *testing.T) {
		r, cl, _, agent := sessionCapRig(t, nil, func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
			return map[string]a2aBusStream{a2aTasksStream: {Bytes: 1}}, nil
		})
		r.resolveA2ASessionCap(ctx, agent, nil)
		if got := storedCapAgent(t, cl, agent); got.Status.EffectiveSessionCap == nil || *got.Status.EffectiveSessionCap != 10 {
			t.Errorf("effectiveSessionCap = %v from a report with no max_consumers, want maxSessions (10), not a fit to width 0", got.Status.EffectiveSessionCap)
		}
		provisioned := storedCapAgent(t, cl, agent)
		provisioned.Status.EffectiveSessionCap = nil
		setBusProvisionedCondition(provisioned, true, "provision-job", metav1.Now())
		r.sessionCapReadings.Clear()
		r.resolveA2ASessionCap(ctx, provisioned, nil)
		if !a2aSessionCapWaiting(provisioned) {
			t.Errorf("a provisioned bus whose report has no TASKS config did not wait")
		}
	})

	t.Run("unreadable on a provisioned bus with no stored cap waits, on the session route at maxSessions", func(t *testing.T) {
		r, cl, rec, agent := sessionCapRig(t, func(a *agentv1alpha1.PlatformAgent) {
			setBusProvisionedCondition(a, true, "provision-job", metav1.Now())
		}, unreadableBus)
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
		if resolveA2ADefaultRoute(agent) != agentv1alpha1.DefaultRouteSession || gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "10" {
			t.Errorf("route %q, A2A_MAX_SESSIONS %q; want the opted-in session route at 10", resolveA2ADefaultRoute(agent), gatewayEnv(t, agent, "A2A_MAX_SESSIONS"))
		}
		if events := drainEvents(rec); len(events) != 0 {
			t.Errorf("events %q on a wait, want none", events)
		}
		// The same status under an unset field is the platform route, where
		// the wait neither applies nor holds the provision Job.
		unset := agent.DeepCopy()
		unset.Spec.Harness = nil
		if resolveA2ADefaultRoute(unset) != agentv1alpha1.DefaultRoutePlatform || a2aSessionCapWaiting(unset) {
			t.Errorf("an unset field under a waiting condition renders %q, waiting %v; want platform, not waiting", resolveA2ADefaultRoute(unset), a2aSessionCapWaiting(unset))
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
			r.sessionCapReadings.Clear()
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

	t.Run("the platform route fits nothing and clears what an opt-in stored, in one write", func(t *testing.T) {
		r, cl, _, agent := sessionCapRig(t, nil, tasksAt(110))
		r.resolveA2ASessionCap(ctx, agent, nil)
		agent = storedCapAgent(t, cl, agent)
		if agent.Status.EffectiveSessionCap == nil || meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType) == nil {
			t.Fatalf("precondition: the session route stored cap %v and condition %+v", agent.Status.EffectiveSessionCap, meta.FindStatusCondition(agent.Status.Conditions, sessionCapFittedConditionType))
		}
		before, _ := strconv.Atoi(agent.ResourceVersion)
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: ptr.To(agentv1alpha1.DefaultRoutePlatform)}}
		r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
			t.Error("the platform route read the bus")
			return nil, nil
		}
		instance := agent.DeepCopy()
		r.resolveA2ASessionCap(ctx, agent, instance)
		got := storedCapAgent(t, cl, agent)
		if got.Status.EffectiveSessionCap != nil || meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType) != nil {
			t.Errorf("on platform: cap %v, condition %+v; want both cleared", got.Status.EffectiveSessionCap, meta.FindStatusCondition(got.Status.Conditions, sessionCapFittedConditionType))
		}
		if after, _ := strconv.Atoi(got.ResourceVersion); after != before+1 {
			t.Errorf("clearing took %d status writes, want one", after-before)
		}
		if instance.Status.EffectiveSessionCap != nil || instance.ResourceVersion != got.ResourceVersion {
			t.Errorf("instance carries cap %v at version %q, stored %q", instance.Status.EffectiveSessionCap, instance.ResourceVersion, got.ResourceVersion)
		}
		next := got.DeepCopy()
		next.Spec.Harness = agent.Spec.Harness
		r.resolveA2ASessionCap(ctx, next, nil)
		if again := storedCapAgent(t, cl, agent); again.ResourceVersion != got.ResourceVersion {
			t.Error("the next pass on platform wrote status again")
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
	agent := sessionRouteTestAgent()
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
	agent.Spec.Harness.Tuning.MaxSessions = ptr.To(3)
	if got := resolveA2AEffectiveSessions(agent); got != 3 {
		t.Errorf("maxSessions 3 under a stored cap of 4 renders %d, want 3", got)
	}
	// On the fitted cap the narrowed install passes its own check: the
	// provision script's refusal is not reached at the live width.
	fitted := sessionRouteTestAgent()
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
	r.sessionCapReadings.Clear()
	wider := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, wider, nil)
	if events := drainEvents(rec); len(events) != 1 || !strings.Contains(events[0], "fitted to 6") {
		t.Errorf("events %q on a wider stream, want one naming the cap 6", events)
	}
	if got := storedCapAgent(t, cl, agent).Status.EffectiveSessionCap; got == nil || *got != 6 {
		t.Errorf("stored effectiveSessionCap = %v on the wider stream, want 6: the condition is unchanged, the cap is not", got)
	}
	// Widened to fit: the condition goes, and no Warning.
	r.busStreamState = tasksAt(150)
	r.sessionCapReadings.Clear()
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
	// Narrowed again to the width it warned about before the recovery: the
	// recovery forgot that Warning, so this one is recorded.
	r.busStreamState = tasksAt(110)
	r.sessionCapReadings.Clear()
	render, instance := storedCapAgent(t, cl, agent), storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, render, instance)
	if events := drainEvents(rec); len(events) != 1 || !strings.Contains(events[0], "fitted to 4") {
		t.Errorf("events %q narrowing again to a width warned about before the recovery, want the Warning again", events)
	}
	if instance.Status.EffectiveSessionCap == nil || *instance.Status.EffectiveSessionCap != 4 || instance.ResourceVersion != render.ResourceVersion {
		t.Errorf("instance carries cap %v at version %q, render %q", instance.Status.EffectiveSessionCap, instance.ResourceVersion, render.ResourceVersion)
	}
}

// Through Reconcile, on an existing (provisioned) install that opts into the
// session route: with the bus's width unreadable and no stored cap, the
// provision Job is not created and the gateway routes to session at
// maxSessions; once the width reads, the Job is created at the fitted cap
// and the gateway's cap follows it.
// A provisioned bus that answers without TASKS (deleted for recreation, as
// the provision Job's refusal tells an admin to do) and no stored cap: the
// provision Job is the one thing that creates TASKS, so it is not held. The
// cap is maxSessions, and the Job runs; on platform it runs as on main.
func TestAProvisionedBusWithoutTasksRunsTheProvisionJob(t *testing.T) {
	for _, tc := range []struct {
		name  string
		route *string
		want  string
	}{
		{"session route", ptr.To(agentv1alpha1.DefaultRouteSession), agentv1alpha1.DefaultRouteSession},
		{"platform route", nil, agentv1alpha1.DefaultRoutePlatform},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := sessionRouteTestAgent()
			agent.Spec.Harness.Tuning.DefaultRoute = tc.route
			setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
			r, cl, req := a2aGateTestReconciler(t, agent)
			r.busStreamState = noTasks
			ctx := context.Background()
			for i := 0; i < 3; i++ {
				if _, err := r.Reconcile(ctx, req); err != nil {
					t.Fatalf("Reconcile %d: %v", i+1, err)
				}
			}
			letTheGatewayThrough(t, ctx, cl, r, req, agent)
			stored := storedCapAgent(t, cl, agent)
			if c := meta.FindStatusCondition(stored.Status.Conditions, sessionCapFittedConditionType); c != nil {
				t.Errorf("SessionCapFitted = %s/%s on a bus that answers without TASKS, want none", c.Status, c.Reason)
			}
			jobs := &batchv1.JobList{}
			if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
				t.Fatal(err)
			}
			if len(jobs.Items) != 1 || jobs.Items[0].Name != buildA2AProvisionJob(stored).Name {
				t.Fatalf("%d provision Jobs, want the one that creates TASKS", len(jobs.Items))
			}
			gw := &appsv1.Deployment{}
			if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, gw); err != nil {
				t.Fatal(err)
			}
			env := gw.Spec.Template.Spec.Containers[0].Env
			if v, _ := envValueCount(env, a2aDefaultAddresseeEnvVar); v != tc.want {
				t.Errorf("the gateway routes to %q, want %q", v, tc.want)
			}
			if v, _ := envValueCount(env, "A2A_MAX_SESSIONS"); v != "10" {
				t.Errorf("A2A_MAX_SESSIONS = %q, want maxSessions", v)
			}
		})
	}
}

func TestTheProvisionJobWaitsForTheBusWidth(t *testing.T) {
	agent := sessionRouteTestAgent()
	// An existing install, provisioned before it opted in.
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
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
	if v, _ := envValueCount(gw.Spec.Template.Spec.Containers[0].Env, a2aDefaultAddresseeEnvVar); v != agentv1alpha1.DefaultRouteSession {
		t.Errorf("the waiting gateway routes to %q, want the session route it opted into", v)
	}
	if v, _ := envValueCount(gw.Spec.Template.Spec.Containers[0].Env, "A2A_MAX_SESSIONS"); v != "10" {
		t.Errorf("the waiting gateway's A2A_MAX_SESSIONS = %q, want maxSessions", v)
	}

	r.busStreamState = tasksAt(110)
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatal(err)
	}
	stored = storedCapAgent(t, cl, agent)
	if !busProvisioned(stored) {
		t.Fatal("precondition: the install lost its provisioned record")
	}
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

// routesRendered is A2A_DEFAULT_ADDRESSEE and A2A_MAX_SESSIONS on the A2A
// gateway and A2A_DEFAULT_ADDRESSEE on the platform agent container, as
// applied, "" for a workload not rendered yet.
func routesRendered(t *testing.T, cl client.Client, agent *agentv1alpha1.PlatformAgent) (gwRoute, gwCap, paRoute string) {
	t.Helper()
	ctx := context.Background()
	gw := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, gw); err == nil {
		gwRoute, _ = envValueCount(gw.Spec.Template.Spec.Containers[0].Env, a2aDefaultAddresseeEnvVar)
		gwCap, _ = envValueCount(gw.Spec.Template.Spec.Containers[0].Env, "A2A_MAX_SESSIONS")
	}
	pa := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, pa); err == nil {
		for _, c := range pa.Spec.Template.Spec.Containers {
			if c.Name == platformAgentContainerName {
				paRoute, _ = envValueCount(c.Env, a2aDefaultAddresseeEnvVar)
			}
		}
	}
	return gwRoute, gwCap, paRoute
}

// A fresh install whose bus does not answer yet renders the spec's route at
// maxSessions from its first pass, never platform first, and creates its
// provision Job without waiting on the bus's width.
func TestAFreshInstallRendersTheSessionRouteOnce(t *testing.T) {
	agent := sessionRouteTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	r.busStreamState = unreadableBus
	ctx := context.Background()
	check := func(pass string) {
		t.Helper()
		gwRoute, gwCap, paRoute := routesRendered(t, cl, agent)
		for _, v := range []string{gwRoute, paRoute} {
			if v != "" && v != agentv1alpha1.DefaultRouteSession {
				t.Errorf("%s: a fresh install renders A2A_DEFAULT_ADDRESSEE=%q, want session from the first render", pass, v)
			}
		}
		if gwCap != "" && gwCap != "10" {
			t.Errorf("%s: A2A_MAX_SESSIONS = %q, want 10", pass, gwCap)
		}
	}
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatal(err)
		}
		check(fmt.Sprintf("pass %d", i+1))
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	check("after the gateway")
	if gwRoute, _, paRoute := routesRendered(t, cl, agent); gwRoute == "" || paRoute == "" {
		t.Fatalf("precondition: gateway route %q, platform agent route %q; both should be rendered", gwRoute, paRoute)
	}
	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatal(err)
	}
	stored := storedCapAgent(t, cl, agent)
	if len(jobs.Items) != 1 || jobs.Items[0].Name != buildA2AProvisionJob(stored).Name {
		t.Fatalf("%d provision Jobs on a fresh install whose bus does not answer, want the one at maxSessions", len(jobs.Items))
	}
	if !strings.Contains(a2aProvisionScript(stored), "required_consumers="+strconv.Itoa(10*7+a2aTasksReserve(stored))+"\n") {
		t.Error("the fresh install's Job does not check maxSessions' budget on the session route")
	}
	if a2aSessionCapWaiting(stored) {
		t.Error("a fresh install waits on the bus's width")
	}
}

// An existing install upgraded onto a readable bus: the first pass fits the
// cap and renders it, so the gateway and the platform agent each change
// once, and the Job checks the fitted budget.
func TestAnUpgradeOnAReadableBusRollsOnce(t *testing.T) {
	agent := sessionRouteTestAgent()
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	r, cl, req := a2aGateTestReconciler(t, agent)
	r.busStreamState = tasksAt(110)
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatal(err)
		}
		if _, _, paRoute := routesRendered(t, cl, agent); paRoute != "" && paRoute != agentv1alpha1.DefaultRouteSession {
			t.Fatalf("pass %d renders the platform agent at %q before the fitted session route", i+1, paRoute)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)
	gw := &appsv1.Deployment{}
	pa := &appsv1.Deployment{}
	read := func() (string, string) {
		t.Helper()
		if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, gw); err != nil {
			t.Fatal(err)
		}
		if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, pa); err != nil {
			t.Fatal(err)
		}
		return fmt.Sprint(gw.Spec.Template.Spec.Containers[0].Env), fmt.Sprint(containerNamed(t, pa, platformAgentContainerName).Env)
	}
	gwRoute, gwCap, paRoute := routesRendered(t, cl, agent)
	if gwRoute != agentv1alpha1.DefaultRouteSession || paRoute != agentv1alpha1.DefaultRouteSession || gwCap != "4" {
		t.Fatalf("after the upgrade's passes: gateway %q at %q, platform agent %q; want session at the fitted 4", gwRoute, gwCap, paRoute)
	}
	gwEnv, paEnv := read()
	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatal(err)
		}
		if g, p := read(); g != gwEnv || p != paEnv {
			t.Fatalf("pass %d changed the gateway's or the platform agent's env again: a second roll", i+1)
		}
	}
	stored := storedCapAgent(t, cl, agent)
	jobs := &batchv1.JobList{}
	if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(jobs.Items) != 1 || jobs.Items[0].Name != buildA2AProvisionJob(stored).Name {
		t.Fatalf("%d provision Jobs, want the one at the fitted cap", len(jobs.Items))
	}
	// The Job is named by the digest of its spec, script included, so the
	// one created is the render whose script checks the fitted budget.
	if want := "required_consumers=" + strconv.Itoa(4*7+a2aTasksReserve(stored)) + "\n"; !strings.Contains(a2aProvisionScript(stored), want) {
		t.Errorf("the Job does not check %q", strings.TrimSpace(want))
	}
}

// A served CRD that prunes status.effectiveSessionCap: the install is held on
// platform with SessionCapFitted saying why, from the pass that sees the
// prune. A pass past the reprobe that can't read the bus stays there rather
// than rendering maxSessions, and only an echo that keeps the cap engages
// the session route, at the fitted cap.
func TestAPrunedSessionCapHoldsThePlatformRoute(t *testing.T) {
	agent := sessionRouteTestAgent()
	// A provisioned bus, where an unreadable pass has no cap to fall back on.
	setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
	patches, pruning := 0, true
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(interceptor.Funcs{SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
			patches++
			if pa, ok := obj.(*agentv1alpha1.PlatformAgent); ok && pruning {
				pa.Status.EffectiveSessionCap = nil // the older CRD's schema
			}
			return c.SubResource(sub).Patch(ctx, obj, patch, opts...)
		}}).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(16), busStreamState: tasksAt(110)}
	ctx := context.Background()
	key := client.ObjectKeyFromObject(agent)
	held := func(step string, a *agentv1alpha1.PlatformAgent) {
		t.Helper()
		c := meta.FindStatusCondition(a.Status.Conditions, sessionCapFittedConditionType)
		if resolveA2ADefaultRoute(a) != agentv1alpha1.DefaultRoutePlatform || resolveA2AEffectiveSessions(a) != resolveA2AMaxSessions(a) ||
			a.Status.EffectiveSessionCap != nil || a2aSessionCapWaiting(a) || c == nil || c.Reason != sessionCapPrunedReason {
			t.Errorf("%s: route %q at %d, cap %v, condition %v; want platform at maxSessions, held by %s", step,
				resolveA2ADefaultRoute(a), resolveA2AEffectiveSessions(a), a.Status.EffectiveSessionCap, c, sessionCapPrunedReason)
		}
		if stored := storedCapAgent(t, cl, agent); resolveA2ADefaultRoute(stored) != agentv1alpha1.DefaultRoutePlatform {
			t.Errorf("%s: the stored status routes %q, want platform", step, resolveA2ADefaultRoute(stored))
		}
	}
	age := func() {
		r.sessionCapPruned.Store(key, time.Now().Add(-sessionCapPrunedReprobe-time.Second))
		r.sessionCapReadings.Clear()
	}

	first := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, first, nil)
	held("the pass that sees the prune", first)
	if !r.sessionCapStatusPruned(first) {
		t.Fatal("the echo without the cap was not noticed")
	}

	before := patches
	r.busStreamState = unreadableBus
	for i := 0; i < 3; i++ {
		pass := storedCapAgent(t, cl, agent)
		r.resolveA2ASessionCap(ctx, pass, nil)
		held(fmt.Sprintf("pass %d within the reprobe", i+1), pass)
	}
	if patches != before {
		t.Errorf("%d status patches within the reprobe, want none", patches-before)
	}

	// Past the reprobe, with the bus unreadable: nothing to probe with.
	age()
	pass := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, pass, nil)
	held("an unreadable pass past the reprobe", pass)
	if !r.sessionCapStatusPruned(pass) {
		t.Error("an unreadable pass past the reprobe did not wait a reprobe before the next probe")
	}

	// Past the reprobe, readable, the CRD still pruning.
	r.busStreamState = tasksAt(110)
	age()
	pass = storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, pass, nil)
	held("a probe the CRD still prunes", pass)

	// A restart: no memory, the held condition stored, the CRD still pruning.
	restarted := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(16), busStreamState: unreadableBus}
	pass = storedCapAgent(t, cl, agent)
	restarted.resolveA2ASessionCap(ctx, pass, nil)
	held("an unreadable first pass after a restart", pass)
	restarted.busStreamState = tasksAt(110)
	age()
	restarted.sessionCapPruned.Store(key, time.Now().Add(-sessionCapPrunedReprobe-time.Second))
	pass = storedCapAgent(t, cl, agent)
	restarted.resolveA2ASessionCap(ctx, pass, nil)
	held("a probe after a restart", pass)

	// The CRD updated: the next probe's echo keeps the cap.
	pruning = false
	age()
	pass = storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, pass, nil)
	stored := storedCapAgent(t, cl, agent)
	for name, a := range map[string]*agentv1alpha1.PlatformAgent{"the render copy": pass, "the store": stored} {
		c := meta.FindStatusCondition(a.Status.Conditions, sessionCapFittedConditionType)
		if resolveA2ADefaultRoute(a) != agentv1alpha1.DefaultRouteSession || resolveA2AEffectiveSessions(a) != 4 || c == nil || c.Reason != sessionCapNarrowReason {
			t.Errorf("%s after the CRD keeps the cap: route %q at %d, condition %v; want session at the fitted 4, %s", name,
				resolveA2ADefaultRoute(a), resolveA2AEffectiveSessions(a), c, sessionCapNarrowReason)
		}
	}
	if _, ok := r.sessionCapPruned.Load(key); ok {
		t.Error("the prune record outlived an echo that kept the cap")
	}
}

// A stored cap's reading is reused for a poll period; with nothing stored,
// every pass reads the bus.
func TestTheBusWidthReadingIsCachedOnlyBehindAStoredCap(t *testing.T) {
	reads := 0
	counting := func(ctx context.Context, a *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		reads++
		return tasksAt(110)(ctx, a)
	}
	ctx := context.Background()
	r, cl, _, agent := sessionCapRig(t, nil, counting)
	r.resolveA2ASessionCap(ctx, agent, nil)
	for i := 0; i < 3; i++ {
		r.resolveA2ASessionCap(ctx, storedCapAgent(t, cl, agent), nil)
	}
	if reads != 1 {
		t.Errorf("%d bus reads over four passes with a cap stored after the first, want 1", reads)
	}
	key := client.ObjectKeyFromObject(agent)
	r.sessionCapReadings.Store(key, a2aTasksWidthReading{width: 110, present: true, at: time.Now().Add(-sessionCapReadingTTL - time.Second)})
	r.resolveA2ASessionCap(ctx, storedCapAgent(t, cl, agent), nil)
	if reads != 2 {
		t.Errorf("an expired reading was reused (%d reads, want 2)", reads)
	}
	// Nothing stored (a provisioned bus waiting): every pass reads.
	r2, cl2, _, waiting := sessionCapRig(t, func(a *agentv1alpha1.PlatformAgent) {
		setBusProvisionedCondition(a, true, "provision-job", metav1.Now())
	}, func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
		reads++
		return nil, goerrors.New("refused")
	})
	reads = 0
	for i := 0; i < 3; i++ {
		r2.resolveA2ASessionCap(ctx, storedCapAgent(t, cl2, waiting), nil)
	}
	if reads != 3 {
		t.Errorf("%d bus reads over three passes with nothing stored, want 3", reads)
	}
}

// A spec edit refreshes the condition's observedGeneration.
func TestTheSessionCapConditionFollowsTheGeneration(t *testing.T) {
	ctx := context.Background()
	r, cl, _, agent := sessionCapRig(t, nil, tasksAt(110))
	r.resolveA2ASessionCap(ctx, agent, nil)
	for _, streams := range []func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error){tasksAt(110), unreadableBus} {
		r.busStreamState = streams
		r.sessionCapReadings.Clear()
		edited := storedCapAgent(t, cl, agent)
		edited.Generation++
		r.resolveA2ASessionCap(ctx, edited, nil)
		c := meta.FindStatusCondition(storedCapAgent(t, cl, agent).Status.Conditions, sessionCapFittedConditionType)
		if c == nil || c.ObservedGeneration != edited.Generation {
			t.Errorf("SessionCapFitted %+v after a spec edit to generation %d", c, edited.Generation)
		}
	}
}

// The refusal names the effective cap it budgeted for, not maxSessions.
func TestTheRefusalNamesTheFittedCap(t *testing.T) {
	agent := sessionRouteTestAgent()
	agent.Status.EffectiveSessionCap = ptr.To(4)
	if msg := a2aProvisionRefusalStatus(agent); !strings.Contains(msg, "the session cap of 4") || strings.Contains(msg, "maxSessions=10 needs") {
		t.Errorf("the refusal status does not name the fitted cap it budgeted:\n%s", msg)
	}
	if script := a2aProvisionScript(agent); !strings.Contains(script, "the session cap is 4 (status.effectiveSessionCap, maxSessions 10") {
		t.Error("the provision script's refusal does not name the fitted cap it budgeted")
	}
}

// The operator's A2A_SESSION_CLUSTER_VIEW doesn't hold the session route off:
// since 2026-11-20 the cluster view is on by default for the session route
// (#6538), so an explicit session routes to session on both containers with
// the view on, and the cap is fitted to TASKS as with it off. Off, it is
// session too. (An unset field is platform either way, and writes nothing:
// TestTheDefaultPlatformRouteDoesNothingNew.)
func TestTheClusterViewDoesNotHoldTheSessionRouteOff(t *testing.T) {
	ctx := context.Background()
	for _, route := range []*string{ptr.To(agentv1alpha1.DefaultRouteSession)} {
		t.Run(ptr.Deref(route, "unset"), func(t *testing.T) {
			t.Setenv(a2aSessionClusterViewEnvVar, "true")
			r, cl, _, agent := sessionCapRig(t, func(a *agentv1alpha1.PlatformAgent) {
				a.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: route}}
			}, tasksAt(110))
			r.resolveA2ASessionCap(ctx, agent, nil)
			gateway, platformAgent := renderedDefaultRoutes(t, agent, nil)
			if gateway != agentv1alpha1.DefaultRouteSession || platformAgent != agentv1alpha1.DefaultRouteSession {
				t.Errorf("with the cluster view on: gateway %q, platform agent %q; both want session", gateway, platformAgent)
			}
			if gatewayEnv(t, agent, "A2A_MAX_SESSIONS") != "4" {
				t.Errorf("A2A_MAX_SESSIONS = %q with the view on, want the cap fitted to a TASKS 110 wide (4)", gatewayEnv(t, agent, "A2A_MAX_SESSIONS"))
			}
			c := meta.FindStatusCondition(storedCapAgent(t, cl, agent).Status.Conditions, sessionCapFittedConditionType)
			if c == nil || c.Reason != sessionCapNarrowReason {
				t.Errorf("SessionCapFitted = %+v, want %s: the view changes nothing about the fit", c, sessionCapNarrowReason)
			}
		})
	}
	t.Run("off", func(t *testing.T) {
		t.Setenv(a2aSessionClusterViewEnvVar, "")
		r, cl, _, agent := sessionCapRig(t, nil, tasksAt(150))
		r.resolveA2ASessionCap(ctx, agent, nil)
		if gateway, platformAgent := renderedDefaultRoutes(t, agent, nil); gateway != agentv1alpha1.DefaultRouteSession || platformAgent != agentv1alpha1.DefaultRouteSession {
			t.Errorf("with the view off: gateway %q, platform agent %q; want the session default", gateway, platformAgent)
		}
		if c := meta.FindStatusCondition(storedCapAgent(t, cl, agent).Status.Conditions, sessionCapFittedConditionType); c != nil {
			t.Errorf("SessionCapFitted %+v with the view off and a stream that fits", c)
		}
	})
}

// A cached reading is not carried across a stay on the platform route: an
// install that leaves the session route (its stored cap left as it was) and
// returns reads the bus again rather than refitting to the old reading.
func TestAClearedCapReadsTheBusAgain(t *testing.T) {
	ctx := context.Background()
	r, cl, _, agent := sessionCapRig(t, nil, tasksAt(110))
	r.resolveA2ASessionCap(ctx, agent, nil)
	optOut := storedCapAgent(t, cl, agent)
	optOut.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{DefaultRoute: ptr.To(agentv1alpha1.DefaultRoutePlatform)}}
	r.resolveA2ASessionCap(ctx, optOut, nil)
	r.busStreamState = tasksAt(150) // widened meanwhile
	back := storedCapAgent(t, cl, agent)
	r.resolveA2ASessionCap(ctx, back, nil)
	if back.Status.EffectiveSessionCap == nil || *back.Status.EffectiveSessionCap != 10 {
		t.Errorf("effectiveSessionCap = %v back on the session route, want 10 from a fresh reading of the widened stream", back.Status.EffectiveSessionCap)
	}
}

// A recovery forgets the Warning it last recorded, so narrowing again to the
// very width warned about before warns again.
func TestNarrowingAgainAfterARecoveryWarnsAgain(t *testing.T) {
	ctx := context.Background()
	r, cl, rec, agent := sessionCapRig(t, nil, tasksAt(110))
	for i, width := range []int{110, 150, 110} {
		r.busStreamState = tasksAt(width)
		r.sessionCapReadings.Clear()
		r.resolveA2ASessionCap(ctx, storedCapAgent(t, cl, agent), nil)
		want := 1
		if width == 150 {
			want = 0
		}
		if n := len(drainEvents(rec)); n != want {
			t.Errorf("step %d (width %d): %d Warnings, want %d", i+1, width, n, want)
		}
	}
}

// The platform route, which an unset defaultRoute resolves to, runs nothing
// this file adds: through Reconcile on an existing (provisioned) install and
// on a fresh one, with the operator's cluster view on and off, the bus's
// monitoring endpoint is never read, no session cap or SessionCapFitted is
// written, no Warning is recorded, the provision Job is created without
// waiting, and the gateway, the platform agent container and the
// hermes-bridge sidecar render platform at the configured cap, with TASKS'
// budget and create width at the platform route's 3 a session.
func TestTheDefaultPlatformRouteDoesNothingNew(t *testing.T) {
	t.Run("the session cap writes no status on a fresh install", func(t *testing.T) {
		writes := 0
		agent := a2aTestAgent()
		cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
			WithInterceptorFuncs(interceptor.Funcs{SubResourcePatch: func(ctx context.Context, c client.Client, sub string, obj client.Object, patch client.Patch, opts ...client.SubResourcePatchOption) error {
				writes++
				return c.SubResource(sub).Patch(ctx, obj, patch, opts...)
			}}).Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(16), busStreamState: func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
			t.Error("the platform route read the bus")
			return nil, nil
		}}
		stored := storedCapAgent(t, cl, agent)
		for i := 0; i < 3; i++ {
			r.resolveA2ASessionCap(context.Background(), stored, stored.DeepCopy())
		}
		if writes != 0 {
			t.Errorf("%d status writes from the session cap on a default install, want none", writes)
		}
	})
	for _, tc := range []struct {
		name        string
		provisioned bool
		clusterView bool
	}{
		{"fresh install", false, false},
		{"existing install", true, false},
		{"existing install with the cluster view on", true, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.clusterView {
				t.Setenv(a2aSessionClusterViewEnvVar, "true")
			}
			agent := a2aTestAgent() // defaultRoute unset
			if tc.provisioned {
				setBusProvisionedCondition(agent, true, "provision-job", metav1.Now())
			}
			r, cl, req := a2aGateTestReconciler(t, agent)
			rec := record.NewFakeRecorder(256)
			r.Recorder = rec
			reads := 0
			r.busStreamState = func(context.Context, *agentv1alpha1.PlatformAgent) (map[string]a2aBusStream, error) {
				reads++
				return map[string]a2aBusStream{a2aTasksStream: {MaxConsumers: 64}}, nil
			}
			ctx := context.Background()
			for i := 0; i < 2; i++ {
				if _, err := r.Reconcile(ctx, req); err != nil {
					t.Fatalf("Reconcile %d: %v", i+1, err)
				}
			}
			letTheGatewayThrough(t, ctx, cl, r, req, agent)
			if reads != 0 {
				t.Errorf("the platform route read the bus's streams %d times, want never", reads)
			}
			stored := storedCapAgent(t, cl, agent)
			if stored.Status.EffectiveSessionCap != nil {
				t.Errorf("status.effectiveSessionCap = %d on the platform route, want absent", *stored.Status.EffectiveSessionCap)
			}
			if c := meta.FindStatusCondition(stored.Status.Conditions, sessionCapFittedConditionType); c != nil {
				t.Errorf("SessionCapFitted %+v on the platform route, want none", c)
			}
			for _, e := range drainEvents(rec) {
				if strings.Contains(e, sessionCapFittedConditionType) {
					t.Errorf("event %q on the platform route", e)
				}
			}
			if resolveA2AEffectiveSessions(stored) != defaultA2AMaxSessions || a2aSessionCapWaiting(stored) {
				t.Errorf("effective sessions %d, waiting %v; want maxSessions, not waiting", resolveA2AEffectiveSessions(stored), a2aSessionCapWaiting(stored))
			}
			jobs := &batchv1.JobList{}
			if err := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); err != nil {
				t.Fatal(err)
			}
			if len(jobs.Items) != 1 {
				t.Fatalf("%d provision Jobs on the platform route, want the one, not held", len(jobs.Items))
			}
			script := jobs.Items[0].Spec.Template.Spec.Containers[0].Args
			joined := strings.Join(script, "\n") + strings.Join(jobs.Items[0].Spec.Template.Spec.Containers[0].Command, "\n")
			for _, want := range []string{"required_consumers=110\n", "--max-consumers=110 ", "fits=$(( (live_consumers - 80) / 3 ))"} {
				if !strings.Contains(joined, want) {
					t.Errorf("the platform route's provision script lacks %q", want)
				}
			}
			if strings.Contains(joined, strconv.Itoa(a2aSessionsFitUnbounded)) {
				t.Error("the platform route's provision script carries the session route's fit block")
			}

			gwRoute, gwCap, paRoute := routesRendered(t, cl, agent)
			if gwRoute != agentv1alpha1.DefaultRoutePlatform || gwCap != "10" || paRoute != agentv1alpha1.DefaultRoutePlatform {
				t.Errorf("gateway %q at A2A_MAX_SESSIONS %q, platform agent %q; want platform at 10 on both", gwRoute, gwCap, paRoute)
			}
			pa := &appsv1.Deployment{}
			if err := cl.Get(ctx, types.NamespacedName{Name: agent.Name + "-gateway", Namespace: agent.Namespace}, pa); err != nil {
				t.Fatalf("precondition: the platform agent Deployment is not rendered: %v", err)
			}
			bridge := false
			for _, c := range pa.Spec.Template.Spec.Containers {
				if c.Name == a2aBridgeContainerName {
					bridge = true
					if v, _ := envValueCount(c.Env, a2aDefaultAddresseeEnvVar); v != agentv1alpha1.DefaultRoutePlatform {
						t.Errorf("the hermes-bridge sidecar carries %s=%q, want platform", a2aDefaultAddresseeEnvVar, v)
					}
				}
			}
			if !bridge {
				t.Error("precondition: the hermes-bridge sidecar is not rendered")
			}
			q := buildA2ASessionQuota(stored)
			if want := buildA2ASessionQuota(a2aTestAgent()); !q.Spec.Hard.Pods().Equal(*want.Spec.Hard.Pods()) {
				t.Errorf("the session quota renders %s pods, want the configured %s", q.Spec.Hard.Pods(), want.Spec.Hard.Pods())
			}
			if a2aTasksConsumerBudget(stored) != 110 || a2aTasksMaxConsumers(stored) != 110 {
				t.Errorf("TASKS budget %d, create width %d; want main's 110 and 110", a2aTasksConsumerBudget(stored), a2aTasksMaxConsumers(stored))
			}
		})
	}
}

// A pruned status.effectiveSessionCap alone brings the steady-state requeue
// down to the pruned-status reprobe, as the other pruned records do, so the
// held-off write is retried on time rather than at the secret re-read's
// interval.
func TestAPrunedSessionCapAloneShortensTheRequeue(t *testing.T) {
	agent := sessionRouteTestAgent()
	r := &PlatformAgentReconciler{}
	if got := r.usageStatusRequeue(agent); got != secretEnvReprobeInterval {
		t.Fatalf("precondition: usageStatusRequeue with no record = %s, want %s", got, secretEnvReprobeInterval)
	}
	r.sessionCapPruned.Store(client.ObjectKeyFromObject(agent), time.Now())
	if got := r.usageStatusRequeue(agent); got != usageStatusReprobeInterval {
		t.Errorf("usageStatusRequeue with only the session cap pruned = %s, want %s", got, usageStatusReprobeInterval)
	}
	r.sessionCapPruned.Store(client.ObjectKeyFromObject(agent), time.Now().Add(-2*sessionCapPrunedReprobe))
	if got := r.usageStatusRequeue(agent); got != secretEnvReprobeInterval {
		t.Errorf("usageStatusRequeue after the record expired = %s, want %s", got, secretEnvReprobeInterval)
	}
}
