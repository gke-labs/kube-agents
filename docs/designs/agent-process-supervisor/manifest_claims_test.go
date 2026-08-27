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
//
// Ids are contiguous C1..C11 on purpose. They used to skip C2, C9 and C10 with
// no record of what had been dropped, which reads as three claims quietly lost.

package controller

import (
	"fmt"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
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

// C1 -- P1 and 1.1: Args is a THREE-way switch, and at neither single-replica
// case is anything supervising the gateway.
//
// The front-door row is why this renders three fixtures rather than two: an
// earlier version rendered only 1 and 2 with the front door off, so it modelled
// the operator as a two-way `if` and could not have noticed the third branch.
func TestClaimC1DefaultReplicaHasNoSupervisor(t *testing.T) {
	one := containerNamed(t, render(1), "platform-agent")
	two := containerNamed(t, render(2), "platform-agent")

	fd := haAgent("claims", 1)
	fd.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{PlatformFrontDoor: ptr.To(true)},
	}
	front := containerNamed(t, buildDeployment(fd, "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true}), "platform-agent")

	supervised := func(a []string) bool { return len(a) == 2 && strings.HasSuffix(a[1], "leader_elect.py") }
	ok := len(one.Args) == 0 && len(one.Command) == 0 && // 1 replica, front door off
		len(front.Args) == 5 && front.Args[0] == "hermes" && // 1 replica, front door on
		supervised(two.Args) // above one
	report(t, "C1", "Args is three-way; only the >1 case is the supervisor", ok,
		fmt.Sprintf("replicas=1 -> args=%v ; replicas=1+frontDoor -> args=%v ; replicas=2 -> args=%v",
			one.Args, front.Args, two.Args))
}

// C12 -- 3.1: the gateway profile reaches the container through the ENVIRONMENT,
// unconditionally, not through Args. This is the coupling S1 depends on: the
// wrapper replaces the front-door argv and still runs the right profile only
// because HERMES_GATEWAY_PROFILE is set either way. If a future change moves the
// profile back into Args, S1 silently re-homes the front door to the default
// profile -- so assert the mechanism rather than trusting the comment.
func TestClaimC12ProfileTravelsInTheEnvironment(t *testing.T) {
	get := func(c corev1.Container, k string) (string, bool) {
		for _, e := range c.Env {
			if e.Name == k {
				return e.Value, true
			}
		}
		return "", false
	}
	off := containerNamed(t, render(1), "platform-agent")
	fd := haAgent("claims", 1)
	fd.Spec.Harness = &agentv1alpha1.HarnessSpec{
		Experimental: &agentv1alpha1.ExperimentalSpec{PlatformFrontDoor: ptr.To(true)},
	}
	on := containerNamed(t, buildDeployment(fd, "h1", "h2", "h3", "h4", nil,
		renderOptions{imageVolumeSupported: true}), "platform-agent")

	vOff, setOff := get(off, "HERMES_GATEWAY_PROFILE")
	vOn, setOn := get(on, "HERMES_GATEWAY_PROFILE")
	report(t, "C12", "HERMES_GATEWAY_PROFILE is always set; only its value varies",
		setOff && setOn && vOff == "" && vOn != "",
		fmt.Sprintf("front door off -> set=%v value=%q ; on -> set=%v value=%q",
			setOff, vOff, setOn, vOn))
}

// C2 -- 1.4 / P4: the gateway container carries no probe of any kind.
func TestClaimC2GatewayHasNoProbe(t *testing.T) {
	for _, n := range []int32{1, 2, 3} {
		c := containerNamed(t, render(n), "platform-agent")
		ok := c.ReadinessProbe == nil && c.LivenessProbe == nil && c.StartupProbe == nil
		report(t, "C2", fmt.Sprintf("platform-agent has no probe at replicas=%d", n), ok,
			fmt.Sprintf("readiness=%v liveness=%v startup=%v",
				c.ReadinessProbe != nil, c.LivenessProbe != nil, c.StartupProbe != nil))
	}
}

// C3 -- 1.4 / 3.4: the pod's only probe is an exec, not an httpGet. This is the
// operator-side half of E2: the design must not introduce the repository's first
// httpGet probe pointing at a loopback bind.
func TestClaimC3EveryProbeIsExec(t *testing.T) {
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
	report(t, "C3", "every probe in the pod is exec; none is httpGet", probes == execs && https == 0,
		fmt.Sprintf("%d probe(s): %d exec, %d httpGet", probes, execs, https))
}

// C4 -- 1.5: Recreate at one replica, RollingUpdate above it.
func TestClaimC4Strategy(t *testing.T) {
	one := render(1).Spec.Strategy
	two := render(2).Spec.Strategy
	ok := one.Type == appsv1.RecreateDeploymentStrategyType &&
		two.Type == appsv1.RollingUpdateDeploymentStrategyType
	report(t, "C4", "strategy is Recreate at one replica, RollingUpdate above", ok,
		fmt.Sprintf("replicas=1 -> %s ; replicas=2 -> %s (surge=%v unavailable=%v)",
			one.Type, two.Type,
			two.RollingUpdate.MaxSurge.StrVal, two.RollingUpdate.MaxUnavailable.StrVal))
}

// C5 -- P4: 25% rounds maxUnavailable to zero below four replicas.
func TestClaimC5SurgeRounding(t *testing.T) {
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
	report(t, "C5", "maxUnavailable is 0 at 1, 2 and 3 replicas", fmt.Sprint(zeroes) == "[1 2 3]", detail)
}

// C6 -- 3.7: the pod's grace period is unset, so it is the 30 s default, which
// the two-process shutdown budget of 3.2 does not fit inside with useful margin.
func TestClaimC6GracePeriodUnset(t *testing.T) {
	g := render(1).Spec.Template.Spec.TerminationGracePeriodSeconds
	shown := "unset (Kubernetes default 30s)"
	if g != nil {
		shown = fmt.Sprintf("%ds", *g)
	}
	report(t, "C6", "terminationGracePeriodSeconds is unset", g == nil, shown)
}

// C7 -- 3.1: the lease RBAC exists at every replica count, so avoiding it is not
// the argument for solo mode.
func TestClaimC7LeaderRoleUnconditional(t *testing.T) {
	verbs := ""
	for _, r := range buildPlatformLeaderRole(haAgent("claims", 1)).Rules {
		verbs += fmt.Sprintf("%v on %v; ", r.Verbs, r.Resources)
	}
	report(t, "C7", "the leader Role is built for a single-replica agent too", verbs != "", verbs)
}

// C8 -- 3.4: the NetworkPolicy never admitted 8700, so a 0.0.0.0 status port
// would not be reachable by a pod peer anyway.
func TestClaimC8NetworkPolicyPorts(t *testing.T) {
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
	report(t, "C8", "the ingress allowlist does not include 8700", !has8700, "ingress ports: "+ports)
}

// C9 -- 1.1: the Deployment strategy is a FIFTH replica-derived value and it
// reads the INTENDED count, where the four gates of 1.1 read the effective one.
// This asserts the disagreement rather than agreement: it is the one place the
// two counts diverge, and C10's "they all agree" would otherwise read as a
// property of the whole file.
func TestClaimC9StrategyReadsIntendedReplicas(t *testing.T) {
	agent := haAgent("claims", 3)
	agent.Spec.Deployment.ScaleToZero = ptr.To(true)
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
	gw := containerNamed(t, dep, "platform-agent")
	// effective count drives the pods and the supervisor gate ...
	effective := *dep.Spec.Replicas == 0 && len(gw.Args) == 0
	// ... while the strategy is chosen from the intended 3.
	intended := dep.Spec.Strategy.Type == appsv1.RollingUpdateDeploymentStrategyType
	report(t, "C9", "the strategy reads intended replicas while the gates read effective",
		effective && intended,
		fmt.Sprintf("replicas=%d args=%v strategy=%s (Recreate would mean it read the effective count)",
			*dep.Spec.Replicas, gw.Args, dep.Spec.Strategy.Type))
}

// C10 -- 1.1: scaleToZero collapses the effective replica count, and all four
// gates read it independently. They agree today, which is why the rendering is
// inert rather than wrong -- and why removing only the Args gate (3.1) would
// break the agreement rather than fix it.
func TestClaimC10ScaleToZeroDropsElection(t *testing.T) {
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
	svc := buildPlatformService(agent)
	_, hasLeaderSelector := svc.Spec.Selector["kubeagents.io/is-leader"]
	modes, _ := getDefaultStorageConfig(agent)
	rwo := len(modes) == 1 && modes[0] == corev1.ReadWriteOnce
	report(t, "C10", "replicas:3 + scaleToZero: all four gates say 'single replica'",
		len(gw.Args) == 0 && env == "" && !hasLeaderSelector && rwo,
		fmt.Sprintf("replicas=%d args=%v ENABLE_LEADER_ELECTION=%q leaderSelector=%v accessModes=%v",
			*dep.Spec.Replicas, gw.Args, env, hasLeaderSelector, modes))
}

// C11 -- 3.4: $PLATFORM_AGENT_HOME is the data PVC's mount point, and above one
// replica that PVC is ReadWriteMany. So a fixed path under it is ONE file shared
// by every replica -- which is why the status file is pod-local instead.
func TestClaimC11AgentHomeIsSharedAboveOneReplica(t *testing.T) {
	for _, n := range []int32{1, 2, 3} {
		gw := containerNamed(t, render(n), "platform-agent")
		home := ""
		for _, e := range gw.Env {
			if e.Name == "PLATFORM_AGENT_HOME" {
				home = e.Value
			}
		}
		mountedThere := false
		for _, m := range gw.VolumeMounts {
			if m.Name == "platform-agent-data-vol" && m.MountPath == home {
				mountedThere = true
			}
		}
		modes, class := getDefaultStorageConfig(haAgent("claims", n))
		shared := len(modes) == 1 && modes[0] == corev1.ReadWriteMany
		// The claim: home is always the data volume, and it is shared exactly
		// when there is more than one replica to share it between.
		ok := home != "" && mountedThere && shared == (n > 1)
		report(t, "C11",
			fmt.Sprintf("PLATFORM_AGENT_HOME is the data PVC; shared=%v at replicas=%d", shared, n), ok,
			fmt.Sprintf("home=%s data-vol mounted there=%v accessModes=%v class=%s",
				home, mountedThere, modes, ptr.Deref(class, "<none>")))
	}
}
