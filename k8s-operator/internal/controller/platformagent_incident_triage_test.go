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
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const incidentTriageOpenPullRequestEnv = "INCIDENT_TRIAGE_OPEN_PULL_REQUEST"

func TestSafeSandboxEnvOverridesPassesIncidentTriageOpenPullRequest(t *testing.T) {
	// session_kv_server.py reads the opt-in from its own environment; off the
	// allowlist, the documented spec.deployment.env entry never reaches it.
	got := safeSandboxEnvOverrides([]corev1.EnvVar{{Name: incidentTriageOpenPullRequestEnv, Value: "true"}})
	if len(got) != 1 || got[0].Name != incidentTriageOpenPullRequestEnv || got[0].Value != "true" {
		t.Errorf("expected %s=true to pass, got %#v", incidentTriageOpenPullRequestEnv, got)
	}
}

// TestIncidentTriageOpenPullRequestReachesTheGatewayOnlyWhenSet pins both
// sides of the opt-in on the rendered pod: an install that never sets the
// variable has no entry for it, and a spec.deployment.env entry lands on the
// platform-agent container, where session_kv_server.py runs.
func TestIncidentTriageOpenPullRequestReachesTheGatewayOnlyWhenSet(t *testing.T) {
	render := func(agent *agentv1alpha1.PlatformAgent) corev1.Container {
		t.Helper()
		dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
		return containerNamed(t, dep, "platform-agent")
	}

	if _, found := envValue(render(haAgent("default-off", 1)), incidentTriageOpenPullRequestEnv); found {
		t.Errorf("%s is set on an install that never set it", incidentTriageOpenPullRequestEnv)
	}

	agent := haAgent("opted-in", 1)
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{
		Env: []corev1.EnvVar{{Name: incidentTriageOpenPullRequestEnv, Value: "true"}},
	}
	if got, found := envValue(render(agent), incidentTriageOpenPullRequestEnv); !found || got != "true" {
		t.Errorf("%s = %q (found %v), want \"true\"", incidentTriageOpenPullRequestEnv, got, found)
	}
}
