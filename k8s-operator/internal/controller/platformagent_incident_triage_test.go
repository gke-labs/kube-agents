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
	"reflect"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func incidentTriageAgent(triage *agentv1alpha1.IncidentTriageSpec) *agentv1alpha1.PlatformAgent {
	agent := haAgent("triage-agent", 1)
	agent.Spec.Harness = &agentv1alpha1.HarnessSpec{IncidentTriage: triage}
	return agent
}

func gatewayEnv(t *testing.T, agent *agentv1alpha1.PlatformAgent, plugins ...*agentv1alpha1.AgentPlugin) corev1.Container {
	t.Helper()
	dep := buildDeployment(agent, "h1", "h2", "h3", "h4", plugins, renderOptions{imageVolumeSupported: true})
	return containerNamed(t, dep, "platform-agent")
}

// TestIncidentTriageOpenPullRequestIsOffByDefault pins the promise the field
// makes to every install that never set it: the gateway container's env is the
// one it had, with no INCIDENT_TRIAGE_OPEN_PULL_REQUEST entry at all, whether
// the block is absent, empty, or says false.
func TestIncidentTriageOpenPullRequestIsOffByDefault(t *testing.T) {
	baseline := gatewayEnv(t, incidentTriageAgent(nil)).Env
	for name, triage := range map[string]*agentv1alpha1.IncidentTriageSpec{
		"empty block": {},
		"false":       {OpenPullRequest: ptr.To(false)},
	} {
		got := gatewayEnv(t, incidentTriageAgent(triage))
		if _, found := envValue(got, incidentTriageOpenPullRequestEnv); found {
			t.Errorf("%s: %s is set, want it absent", name, incidentTriageOpenPullRequestEnv)
		}
		if !reflect.DeepEqual(got.Env, baseline) {
			t.Errorf("%s: gateway env differs from an install without the block", name)
		}
	}
	if _, found := envValue(gatewayEnv(t, haAgent("no-harness", 1)), incidentTriageOpenPullRequestEnv); found {
		t.Errorf("no harness: %s is set, want it absent", incidentTriageOpenPullRequestEnv)
	}
}

func TestIncidentTriageOpenPullRequestSetsTheEnv(t *testing.T) {
	got := gatewayEnv(t, incidentTriageAgent(&agentv1alpha1.IncidentTriageSpec{OpenPullRequest: ptr.To(true)}))
	value, found := envValue(got, incidentTriageOpenPullRequestEnv)
	if !found || value != "true" {
		t.Errorf("%s = %q (found %v), want \"true\"", incidentTriageOpenPullRequestEnv, value, found)
	}
	count := 0
	for _, env := range got.Env {
		if env.Name == incidentTriageOpenPullRequestEnv {
			count++
		}
	}
	if count != 1 {
		t.Errorf("%s appears %d times, want once", incidentTriageOpenPullRequestEnv, count)
	}
}

// TestIncidentTriageEnvIsOnlySetByTheField: neither spec.deployment.env nor
// an AgentPlugin's spec.env may turn the behaviour on or override the field.
func TestIncidentTriageEnvIsOnlySetByTheField(t *testing.T) {
	overrideFalse := []corev1.EnvVar{{Name: incidentTriageOpenPullRequestEnv, Value: "false"}}
	overrideTrue := []corev1.EnvVar{{Name: incidentTriageOpenPullRequestEnv, Value: "true"}}
	pluginWith := func(env []corev1.EnvVar) *agentv1alpha1.AgentPlugin {
		return &agentv1alpha1.AgentPlugin{
			Spec: agentv1alpha1.AgentPluginSpec{Env: env},
		}
	}

	on := incidentTriageAgent(&agentv1alpha1.IncidentTriageSpec{OpenPullRequest: ptr.To(true)})
	on.Spec.Deployment.Env = overrideFalse
	if value, _ := envValue(gatewayEnv(t, on, pluginWith(overrideFalse)), incidentTriageOpenPullRequestEnv); value != "true" {
		t.Errorf("field true with deployment.env + plugin override: %s = %q, want \"true\"", incidentTriageOpenPullRequestEnv, value)
	}

	off := incidentTriageAgent(nil)
	off.Spec.Deployment.Env = overrideTrue
	if _, found := envValue(gatewayEnv(t, off, pluginWith(overrideTrue)), incidentTriageOpenPullRequestEnv); found {
		t.Errorf("field unset with deployment.env + plugin entry: %s is set, want it dropped", incidentTriageOpenPullRequestEnv)
	}
}
