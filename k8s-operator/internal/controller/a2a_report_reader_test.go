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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The report reader's token, its two variables and the broker's audience and
// binding render on every install, Google Chat on or off.
func TestA2AReportReaderRendersOnEveryInstall(t *testing.T) {
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
	}{
		{"no chat", a2aTestAgent()},
		{"chat off", gchatTestAgent("next", false)},
		{"chat on", gchatTestAgent("next", true)},
		{"no mode, chat on", gchatTestAgent("", true)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := tc.agent
			gateway := buildA2AGatewayDeployment(agent)
			spec := gateway.Spec.Template.Spec
			volume := podVolume(gateway.Spec.Template, a2aReportsTokenVolume)
			if volume == nil || volume.Projected == nil || len(volume.Projected.Sources) != 1 {
				t.Fatalf("the report reader's token volume is missing: %+v", volume)
			}
			token := volume.Projected.Sources[0].ServiceAccountToken
			if token == nil || token.Audience != credentialProxyA2AReportsAudience || token.Path != "token" ||
				token.ExpirationSeconds == nil || *token.ExpirationSeconds != 3600 {
				t.Errorf("the report reader's token is not a one-hour %s token: %+v", credentialProxyA2AReportsAudience, token)
			}
			if volume.Projected.DefaultMode == nil || *volume.Projected.DefaultMode != 0400 {
				t.Errorf("the token volume's mode is %v, want 0400", volume.Projected.DefaultMode)
			}
			c := spec.Containers[0]
			var mounted bool
			for _, m := range c.VolumeMounts {
				if m.Name == a2aReportsTokenVolume {
					mounted = m.MountPath == "/var/run/secrets/a2a-reports" && m.ReadOnly
				}
			}
			if !mounted {
				t.Errorf("the token is not mounted read-only at /var/run/secrets/a2a-reports: %+v", c.VolumeMounts)
			}
			env := envMapOf(c.Env)
			if got := env[a2aReportStoreTokenFileEnvVar].Value; got != "/var/run/secrets/a2a-reports/token" {
				t.Errorf("%s = %q", a2aReportStoreTokenFileEnvVar, got)
			}
			if got := env[a2aReportStoreURLEnvVar].Value; got != credentialProxyBaseURL(agent) {
				t.Errorf("%s = %q, want the broker %q", a2aReportStoreURLEnvVar, got, credentialProxyBaseURL(agent))
			}

			broker := buildCredentialProxyDeployment(agent, "hash").Spec.Template.Spec.Containers[0].Env
			caller := "system:serviceaccount:" + agent.Namespace + ":" + agent.Name + "-a2a-gateway"
			if v, _ := brokerEnvValue(broker, credentialProxyA2AReportsAudienceEnvVar); v != credentialProxyA2AReportsAudience {
				t.Errorf("%s = %q", credentialProxyA2AReportsAudienceEnvVar, v)
			}
			if v, _ := brokerEnvValue(broker, credentialProxyA2AReportsCallersEnvVar); v != caller {
				t.Errorf("%s = %q, want the gateway's ServiceAccount %q alone", credentialProxyA2AReportsCallersEnvVar, v, caller)
			}
			allowed, _ := brokerEnvValue(broker, "CREDENTIAL_PROXY_ALLOWED_CALLERS")
			if !strings.Contains(","+allowed+",", ","+caller+",") {
				t.Errorf("the gateway is not a broker caller: %q", allowed)
			}
			var peer bool
			for _, p := range buildCredentialProxyNetworkPolicy(agent).Spec.Ingress[0].From {
				peer = peer || (p.PodSelector != nil && p.PodSelector.MatchLabels["app"] == agent.Name+"-a2a-gateway")
			}
			if !peer {
				t.Error("the broker fence does not admit the A2A gateway pod")
			}
		})
	}
}

// The audience is its own: no other token the operator renders shares it.
func TestA2AReportReaderAudienceIsItsOwn(t *testing.T) {
	for _, other := range []string{credentialProxyAudience, credentialProxyChatAudience, credentialProxyA2AChatAudience,
		credentialProxySessionAudience, credentialProxyWatcherAudience, credentialProxyMCPAudience, auditReportStoreAudience} {
		if other == credentialProxyA2AReportsAudience {
			t.Errorf("the report reader's audience collides with %s; the broker would refuse to confer the role", other)
		}
	}
}

// The exec tier drops the audience, so it confers no a2a-reports role, and
// keeps the binding, so the gateway's account reaches no other role there.
func TestA2AReportReaderIsNotServedByTheExecTier(t *testing.T) {
	agent := gchatTestAgent("next", false)
	broker := buildCredentialProxyDeployment(agent, "hash")
	exec := buildCredentialProxyExecTierDeployment(agent, broker.DeepCopy())
	for _, c := range exec.Spec.Template.Spec.Containers {
		if _, ok := brokerEnvValue(c.Env, credentialProxyA2AReportsAudienceEnvVar); ok {
			t.Errorf("container %s of the exec tier confers the a2a-reports role", c.Name)
		}
	}
	if v, ok := brokerEnvValue(exec.Spec.Template.Spec.Containers[0].Env, credentialProxyA2AReportsCallersEnvVar); !ok || v == "" {
		t.Errorf("the exec tier drops the gateway's binding (%q), leaving its account free to present any audience", v)
	}
}

// A CR cannot set the reader's names through spec.deployment.env.
func TestA2AReportReaderEnvIsReserved(t *testing.T) {
	agent := gchatTestAgent("next", false)
	var custom []corev1.EnvVar
	for _, name := range a2aReportsEnvNames {
		custom = append(custom, corev1.EnvVar{Name: name, Value: "attacker"})
	}
	agent.Spec.Deployment = &agentv1alpha1.DeploymentSpec{Env: custom}
	for _, e := range buildCredentialProxyContainer(agent).Env {
		if e.Value == "attacker" {
			t.Errorf("spec.deployment.env set %s on the broker", e.Name)
		}
	}
	for _, e := range buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env {
		if e.Value == "attacker" {
			t.Errorf("spec.deployment.env set %s on the gateway", e.Name)
		}
	}
}
