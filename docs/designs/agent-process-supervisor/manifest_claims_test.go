// Experiment E7 of ../agent-process-supervisor.md 6.0.
//
// NOT part of the operator's test suite. run_experiments.py copies this into
// k8s-operator/internal/controller/, runs it, and removes it again -- it asserts
// what the design says is true TODAY, so several of these assertions are meant
// to start failing once S1/S2 ship. Keeping it in CI would be a tripwire on the
// implementation rather than a regression test.
//
// Every claim here is checked against real rendered manifests, via the same
// buildDeployment / buildNetworkPolicy / buildPlatformLeaderRole the controller
// calls, rather than by grepping the source.

package controller

import (
	"fmt"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
)

func render(replicas int32) *appsv1.Deployment {
	return buildDeployment(haAgent("claims", replicas), "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true})
}

func report(t *testing.T, id, claim string, ok bool, detail string) {
	t.Helper()
	verdict := "HOLDS"
	if !ok {
		verdict = "FALSIFIED"
	}
	fmt.Printf("  [%s] %-9s %s\n        %s\n", id, verdict, claim, detail)
	if !ok {
		t.Fail()
	}
}

// C1 -- P1: at the default replica count nothing supervises anything.
func TestClaimC1DefaultReplicaHasNoSupervisor(t *testing.T) {
	one := containerNamed(t, render(1), "platform-agent")
	two := containerNamed(t, render(2), "platform-agent")
	ok := len(one.Args) == 0 && len(one.Command) == 0 && len(two.Args) == 2
	report(t, "C1", "Args (the supervisor) is set only above one replica", ok,
		fmt.Sprintf("replicas=1 -> command=%v args=%v ; replicas=2 -> args=%v",
			one.Command, one.Args, two.Args))
}

// C3 -- 1.4 / P4: the gateway container carries no probe of any kind.
func TestClaimC3GatewayHasNoProbe(t *testing.T) {
	for _, n := range []int32{1, 2, 3} {
		c := containerNamed(t, render(n), "platform-agent")
		ok := c.ReadinessProbe == nil && c.LivenessProbe == nil && c.StartupProbe == nil
		report(t, "C3", fmt.Sprintf("platform-agent has no probe at replicas=%d", n), ok,
			fmt.Sprintf("readiness=%v liveness=%v startup=%v",
				c.ReadinessProbe != nil, c.LivenessProbe != nil, c.StartupProbe != nil))
	}
}

// C4 -- 1.4 / 3.4: the pod's only probe is an exec, not an httpGet. This is the
// operator-side half of E2: the design must not introduce the repository's first
// httpGet probe pointing at a loopback bind.
func TestClaimC4EveryProbeIsExec(t *testing.T) {
	dep := render(2)
	probes, execs, https := 0, 0, 0
	for _, c := range dep.Spec.Template.Spec.Containers {
		for _, p := range []*corev1.Probe{c.ReadinessProbe, c.LivenessProbe, c.StartupProbe} {
			if p == nil {
				continue
			}
			probes++
			if p.Exec != nil {
				execs++
			}
			if p.HTTPGet != nil {
				https++
			}
		}
	}
	report(t, "C4", "every probe in the pod is exec; none is httpGet", probes == execs && https == 0,
		fmt.Sprintf("%d probe(s): %d exec, %d httpGet", probes, execs, https))
}

// C5 -- 1.5: Recreate at one replica, RollingUpdate above it.
func TestClaimC5Strategy(t *testing.T) {
	one := render(1).Spec.Strategy
	two := render(2).Spec.Strategy
	ok := one.Type == appsv1.RecreateDeploymentStrategyType &&
		two.Type == appsv1.RollingUpdateDeploymentStrategyType
	report(t, "C5", "strategy is Recreate at one replica, RollingUpdate above", ok,
		fmt.Sprintf("replicas=1 -> %s ; replicas=2 -> %s (surge=%v unavailable=%v)",
			one.Type, two.Type,
			two.RollingUpdate.MaxSurge.StrVal, two.RollingUpdate.MaxUnavailable.StrVal))
}

// C6 -- P4: 25% rounds maxUnavailable to zero below four replicas.
func TestClaimC6SurgeRounding(t *testing.T) {
	pct := intstr.FromString(defaultSurgePercent)
	zeroes := []int{}
	detail := ""
	for _, n := range []int{1, 2, 3, 4, 8} {
		up, _ := intstr.GetScaledValueFromIntOrPercent(&pct, n, true)
		down, _ := intstr.GetScaledValueFromIntOrPercent(&pct, n, false)
		detail += fmt.Sprintf("n=%d surge=%d unavail=%d  ", n, up, down)
		if down == 0 {
			zeroes = append(zeroes, n)
		}
	}
	report(t, "C6", "maxUnavailable is 0 at 1, 2 and 3 replicas", fmt.Sprint(zeroes) == "[1 2 3]", detail)
}

// C7 -- 3.7: the pod's grace period is unset, so it is the 30 s default, which
// the two-process shutdown budget of 3.2 does not fit inside with useful margin.
func TestClaimC7GracePeriodUnset(t *testing.T) {
	g := render(1).Spec.Template.Spec.TerminationGracePeriodSeconds
	shown := "unset (Kubernetes default 30s)"
	if g != nil {
		shown = fmt.Sprintf("%ds", *g)
	}
	report(t, "C7", "terminationGracePeriodSeconds is unset", g == nil, shown)
}

// C8 -- 3.1: the lease RBAC exists at every replica count, so avoiding it is not
// the argument for solo mode.
func TestClaimC8LeaderRoleUnconditional(t *testing.T) {
	verbs := ""
	for _, r := range buildPlatformLeaderRole(haAgent("claims", 1)).Rules {
		verbs += fmt.Sprintf("%v on %v; ", r.Verbs, r.Resources)
	}
	report(t, "C8", "the leader Role is built for a single-replica agent too", verbs != "", verbs)
}

// C11 -- 3.4: the NetworkPolicy never admitted 8700, so a 0.0.0.0 status port
// would not be reachable by a pod peer anyway.
func TestClaimC11NetworkPolicyPorts(t *testing.T) {
	np := buildNetworkPolicy(haAgent("claims", 2), []string{"10.0.0.1"}, "10.96.0.10", false, "", nil)
	ports, has8700 := "", false
	for _, rule := range np.Spec.Ingress {
		for _, p := range rule.Ports {
			ports += fmt.Sprintf("%v ", p.Port)
			if p.Port.IntVal == 8700 {
				has8700 = true
			}
		}
	}
	report(t, "C11", "the ingress allowlist does not include 8700", !has8700, "ingress ports: "+ports)
}

// C12 -- 1.1: scaleToZero collapses the effective replica count, so an agent
// asking for three replicas renders no election wiring at all.
func TestClaimC12ScaleToZeroDropsElection(t *testing.T) {
	agent := haAgent("claims", 3)
	agent.Spec.Deployment.ScaleToZero = ptr.To(true)
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gw := containerNamed(t, dep, "platform-agent")
	env := ""
	for _, e := range gw.Env {
		if e.Name == "ENABLE_LEADER_ELECTION" {
			env = e.Value
		}
	}
	report(t, "C12", "replicas:3 + scaleToZero renders no Args and no election env",
		len(gw.Args) == 0 && env == "",
		fmt.Sprintf("replicas=%d args=%v ENABLE_LEADER_ELECTION=%q", *dep.Spec.Replicas, gw.Args, env))
}
