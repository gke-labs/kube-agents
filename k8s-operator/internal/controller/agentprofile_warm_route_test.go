package controller

import (
	"testing"

	"k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// sessionRouteProfileAgent is the session-route test agent with the profile
// ceiling and warmReplicas set (nil leaves one unset).
func sessionRouteProfileAgent(ceiling, warm *int) *agentv1alpha1.PlatformAgent {
	agent := sessionRouteTestAgent()
	agent.Spec.Harness.Tuning.Profiles = &agentv1alpha1.ProfileTuning{Ceiling: ceiling, WarmReplicas: warm}
	return agent
}

// On the session route every session pod hands its reads to a profile as
// children, which run only on the profile's warm pool, so the pool's
// default follows the route: the profile's concurrency there, held to the
// ceiling, and one on any other route, as before (#7014). A warmReplicas the
// CR sets wins on every route.
func TestTheWarmPoolSizeFollowsTheRouteAndAnExplicitCountWins(t *testing.T) {
	profile := func(concurrency int32) *agentv1alpha1.AgentProfile {
		p := testAgentProfile("test-ns", "cluster", func(p *agentv1alpha1.AgentProfile) { p.Spec.Concurrency = concurrency })
		return &p
	}
	platform := func(ceiling, warm *int) *agentv1alpha1.PlatformAgent { return profileTunedAgent(ceiling, warm) }
	// The session route falls back to platform when there's no room for a
	// session; the pool follows the route the operator resolved.
	heldOnPlatform := func(ceiling, warm *int) *agentv1alpha1.PlatformAgent {
		a := sessionRouteProfileAgent(ceiling, warm)
		a.Status.EffectiveSessionCap = ptr.To(0)
		return a
	}
	for _, tc := range []struct {
		name        string
		agent       func(ceiling, warm *int) *agentv1alpha1.PlatformAgent
		ceiling     *int
		warm        *int
		concurrency int32
		want        int
	}{
		{"platform, unset", platform, nil, nil, 2, agentv1alpha1.DefaultProfileWarmReplicas},
		{"platform, unset, high concurrency", platform, nil, nil, 7, agentv1alpha1.DefaultProfileWarmReplicas},
		{"platform, set", platform, nil, ptr.To(3), 2, 3},
		{"session, unset: the concurrency", sessionRouteProfileAgent, nil, nil, 2, 2},
		{"session, unset, concurrency 1", sessionRouteProfileAgent, nil, nil, 1, 1},
		{"session, unset: held to the ceiling", sessionRouteProfileAgent, ptr.To(3), nil, 7, 3},
		{"session, unset: held to warmReplicas' maximum", sessionRouteProfileAgent, ptr.To(agentv1alpha1.MaxProfileCeiling), nil, 500, agentv1alpha1.MaxProfileWarmReplicas},
		{"session, set lower", sessionRouteProfileAgent, nil, ptr.To(1), 2, 1},
		{"session, set to 0", sessionRouteProfileAgent, nil, ptr.To(0), 2, 0},
		{"session, set higher", sessionRouteProfileAgent, nil, ptr.To(5), 2, 5},
		{"session held on platform, unset", heldOnPlatform, nil, nil, 2, agentv1alpha1.DefaultProfileWarmReplicas},
		{"session held on platform, set", heldOnPlatform, nil, ptr.To(4), 2, 4},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := tc.agent(tc.ceiling, tc.warm)
			p := profile(tc.concurrency)
			if got := resolveProfileWarmReplicas(agent, p); got != tc.want {
				t.Errorf("warm replicas = %d, want %d", got, tc.want)
			}
			pool := buildProfilePool(agent, p, agentv1alpha1.AgentProfileTierRead, "agentprofile-cluster-read", false)
			if got := int(*pool.Spec.Replicas); got != tc.want {
				t.Errorf("the pool renders %d replicas, want %d", got, tc.want)
			}
		})
	}
}

// Growing the pool on upgrade, or shrinking it on rollback, changes the
// Deployment's replicas alone: the pod template, and so the
// pod-template-hash, is the same, so the pool's running pods don't roll.
// The previous release's rendering is the same agent with warmReplicas 1.
func TestTheSessionRoutesLargerPoolKeepsThePodTemplate(t *testing.T) {
	p := testAgentProfile("test-ns", "cluster")
	before := buildProfilePool(sessionRouteProfileAgent(nil, ptr.To(agentv1alpha1.DefaultProfileWarmReplicas)), &p, agentv1alpha1.AgentProfileTierRead, "agentprofile-cluster-read", false)
	after := buildProfilePool(sessionRouteProfileAgent(nil, nil), &p, agentv1alpha1.AgentProfileTierRead, "agentprofile-cluster-read", false)
	if *before.Spec.Replicas != 1 || *after.Spec.Replicas != 2 {
		t.Fatalf("replicas %d then %d, want 1 then 2", *before.Spec.Replicas, *after.Spec.Replicas)
	}
	if !equality.Semantic.DeepEqual(before.Spec.Template, after.Spec.Template) {
		t.Error("the pod template changed with the pool's size, so its pods would roll")
	}
	before.Spec.Replicas, after.Spec.Replicas = nil, nil
	if !equality.Semantic.DeepEqual(before, after) {
		t.Error("the pool differs in more than its replicas")
	}
	// The platform route renders the pool exactly as before.
	plat := buildProfilePool(a2aTestAgent(), &p, agentv1alpha1.AgentProfileTierRead, "agentprofile-cluster-read", false)
	if *plat.Spec.Replicas != int32(agentv1alpha1.DefaultProfileWarmReplicas) {
		t.Errorf("the platform route renders %d replicas, want %d", *plat.Spec.Replicas, agentv1alpha1.DefaultProfileWarmReplicas)
	}
}
