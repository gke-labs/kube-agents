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
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// gchatTestAgent is a2aTestAgent with Google Chat configured the way the
// chart renders it. mode is the CR's spec.mode ("" leaves it absent).
func gchatTestAgent(mode string, enabled bool) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	if mode == "" {
		agent.Spec.Mode = nil
	} else {
		agent.Spec.Mode = ptr.To(mode)
	}
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		GoogleChat: &agentv1alpha1.GoogleChatSpec{
			Enabled:          ptr.To(enabled),
			ProjectID:        "chat-project",
			TopicName:        "platform-agent-chat-events",
			SubscriptionName: "platform-agent-chat-events-sub",
			AllowedUsers:     []string{"one@example.com", "two@example.com"},
		},
	}
	return agent
}

// gchatEnv indexes a container's env by name.
func gchatEnv(c corev1.Container) map[string]corev1.EnvVar {
	out := map[string]corev1.EnvVar{}
	for _, e := range c.Env {
		out[e.Name] = e
	}
	return out
}

// TestChatConsumerIsChosenByMode: the two predicates are exact complements
// of each other whenever Chat is enabled, and both false when it is not, so
// an install is never rendered with two Chat consumers or none.
func TestChatConsumerIsChosenByMode(t *testing.T) {
	for _, tc := range []struct {
		name          string
		agent         *agentv1alpha1.PlatformAgent
		armed, legacy bool
	}{
		{"today with chat", gchatTestAgent("", true), false, true},
		{"today explicit with chat", gchatTestAgent("today", true), false, true},
		{"next with chat", gchatTestAgent("next", true), true, false},
		{"next with chat disabled", gchatTestAgent("next", false), false, false},
		{"today with chat disabled", gchatTestAgent("", false), false, false},
		{"next with no integration", a2aTestAgent(), false, false},
		// Version skew: an unrecognized mode fails closed to today, so the
		// legacy consumer renders and the A2A side is not armed.
		{"skew with chat", gchatTestAgent("later", true), false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := a2aChatArmed(tc.agent); got != tc.armed {
				t.Errorf("a2aChatArmed = %v, want %v", got, tc.armed)
			}
			if got := legacyChatConsumer(tc.agent); got != tc.legacy {
				t.Errorf("legacyChatConsumer = %v, want %v", got, tc.legacy)
			}
			if a2aChatArmed(tc.agent) && legacyChatConsumer(tc.agent) {
				t.Error("both consumers armed: every Chat message would be answered twice")
			}
		})
	}
}

// TestChatDisplayModeUnsetIsDefault: the gateway's own unset resolves to
// debug for Discord installs; the render is what makes the CR field and the
// env agree, so unset on the CR must reach the gateway as "default".
func TestChatDisplayModeUnsetIsDefault(t *testing.T) {
	if got := a2aChatDisplayMode(""); got != "default" {
		t.Errorf("unset mode renders %q, want default", got)
	}
	if got := a2aChatDisplayMode("debug"); got != "debug" {
		t.Errorf("debug renders %q", got)
	}
	if got := a2aChatDisplayMode("default"); got != "default" {
		t.Errorf("default renders %q", got)
	}
}

// TestTheRelayTokenPathIsTheGatewaysDefault pins the operator's spelling
// of the mount to the gateway's defaultGchatTokenPath in
// a2a/gateway/config.go; the two modules cannot import each other, and
// docs/README.md says they must agree.
func TestTheRelayTokenPathIsTheGatewaysDefault(t *testing.T) {
	if a2aGchatTokenPath != "/var/run/secrets/a2a-chat-relay/token" {
		t.Errorf("a2aGchatTokenPath = %q; a2a/gateway/config.go defaultGchatTokenPath is /var/run/secrets/a2a-chat-relay/token", a2aGchatTokenPath)
	}
	if credentialProxyA2AChatAudience != "kubeagents-credential-proxy-a2a-chat" {
		t.Errorf("audience = %q", credentialProxyA2AChatAudience)
	}
	if credentialProxyA2AChatAudience == credentialProxyChatAudience || credentialProxyA2AChatAudience == credentialProxyAudience {
		t.Error("the a2a-chat audience collides with another; the broker would not confer the a2a-chat role")
	}
}
