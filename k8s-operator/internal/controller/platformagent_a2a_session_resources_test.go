package controller

import (
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The gateway's env carries the session pod's resolved requests and limits,
// always all four (#2371): the defaults with nothing set, and an override
// merged per key over them, so the rendered Deployment states what the
// gateway will request.
func TestTheGatewayGetsTheSessionPodsResources(t *testing.T) {
	sessionEnv := func(agent *agentv1alpha1.PlatformAgent) map[string]string {
		t.Helper()
		dep := buildA2AGatewayDeployment(agent)
		got := map[string]string{}
		for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
			switch e.Name {
			case "A2A_SESSION_CPU_REQUEST", "A2A_SESSION_MEMORY_REQUEST", "A2A_SESSION_CPU_LIMIT", "A2A_SESSION_MEMORY_LIMIT":
				got[e.Name] = e.Value
			}
		}
		return got
	}
	defaults := sessionEnv(a2aTestAgent())
	want := map[string]string{
		"A2A_SESSION_CPU_REQUEST": agentv1alpha1.DefaultSessionCPURequest, "A2A_SESSION_MEMORY_REQUEST": agentv1alpha1.DefaultSessionMemoryRequest,
		"A2A_SESSION_CPU_LIMIT": agentv1alpha1.DefaultSessionCPULimit, "A2A_SESSION_MEMORY_LIMIT": agentv1alpha1.DefaultSessionMemoryLimit,
	}
	for k, v := range want {
		if defaults[k] != v {
			t.Errorf("with nothing set, %s = %q, want %q", k, defaults[k], v)
		}
	}

	agent := a2aTestAgent()
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.Tuning = &agentv1alpha1.TuningSpec{SessionResources: &corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("384Mi")},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1")},
	}}
	got := sessionEnv(agent)
	for k, v := range map[string]string{
		"A2A_SESSION_CPU_REQUEST": agentv1alpha1.DefaultSessionCPURequest, "A2A_SESSION_MEMORY_REQUEST": "384Mi",
		"A2A_SESSION_CPU_LIMIT": "1", "A2A_SESSION_MEMORY_LIMIT": agentv1alpha1.DefaultSessionMemoryLimit,
	} {
		if got[k] != v {
			t.Errorf("with an override, %s = %q, want %q", k, got[k], v)
		}
	}
}
