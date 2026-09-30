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

// TestAnArmedGatewayCarriesTheChatBackend: everything the gateway reads to
// select and run the Google Chat adapter, and the token it presents to the
// broker, from one CR under next.
func TestAnArmedGatewayCarriesTheChatBackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := gchatTestAgent("next", true)
	dep := buildA2AGatewayDeployment(agent)
	c := dep.Spec.Template.Spec.Containers[0]
	env := gchatEnv(c)

	want := map[string]string{
		a2aGchatRelayURLEnvVar:      "http://test-agent-credential-proxy.test-ns.svc.cluster.local:8765",
		a2aGchatAllowedUsersEnvVar:  "one@example.com,two@example.com",
		a2aGchatAllowAllUsersEnvVar: "false",
		a2aChatDisplayModeEnvVar:    "default",
		a2aGchatTokenPathEnvVar:     a2aGchatTokenPath,
	}
	for name, value := range want {
		if got, ok := env[name]; !ok || got.Value != value {
			t.Errorf("%s = %q (present=%v), want %q", name, got.Value, ok, value)
		}
	}
	// One backend per gateway process: the gateway's guard refuses two, so
	// the render hands it one. The explicit CR field beats a hand-made
	// Secret, and the Discord reference is omitted rather than left
	// optional, because the Secret being present would otherwise arm both.
	if _, ok := env["DISCORD_TOKEN"]; ok {
		t.Error("DISCORD_TOKEN is rendered beside the Chat backend; with the discord-bot Secret present the gateway would refuse to start on two backends")
	}

	var mount *corev1.VolumeMount
	for i := range c.VolumeMounts {
		if c.VolumeMounts[i].Name == a2aGchatTokenVolume {
			mount = &c.VolumeMounts[i]
		}
	}
	if mount == nil {
		t.Fatalf("no mount of %s; the gateway reads its relay token from %s", a2aGchatTokenVolume, a2aGchatTokenPath)
	}
	if mount.MountPath != a2aGchatTokenDir || !mount.ReadOnly {
		t.Errorf("token mount = %+v, want read-only at %s", *mount, a2aGchatTokenDir)
	}
	vol := podVolume(dep.Spec.Template, a2aGchatTokenVolume)
	if vol == nil || vol.Projected == nil || len(vol.Projected.Sources) != 1 || vol.Projected.Sources[0].ServiceAccountToken == nil {
		t.Fatalf("volume %s is not a single projected ServiceAccount token: %+v", a2aGchatTokenVolume, vol)
	}
	tok := vol.Projected.Sources[0].ServiceAccountToken
	if tok.Audience != credentialProxyA2AChatAudience {
		t.Errorf("token audience %q, want %q: the broker confers the a2a-chat role by audience", tok.Audience, credentialProxyA2AChatAudience)
	}
	if tok.ExpirationSeconds == nil || *tok.ExpirationSeconds != a2aGchatTokenTTLSeconds {
		t.Errorf("token expiry %v, want %d", tok.ExpirationSeconds, a2aGchatTokenTTLSeconds)
	}
	if tok.Path != a2aGchatTokenKey {
		t.Errorf("token path %q, want %q so the file lands at %s", tok.Path, a2aGchatTokenKey, a2aGchatTokenPath)
	}
	if vol.Projected.DefaultMode == nil || *vol.Projected.DefaultMode != 0400 {
		t.Errorf("token defaultMode %v, want 0400", vol.Projected.DefaultMode)
	}
}

// TestAnEmptyAllowlistArmsAllowAllUnderNext: the legacy pin's rule, kept.
// The gateway refuses to start the gchat adapter with neither an allowlist
// nor the explicit allow-all, and the CR's empty list has always meant all.
func TestAnEmptyAllowlistArmsAllowAllUnderNext(t *testing.T) {
	agent := gchatTestAgent("next", true)
	agent.Spec.Integration.GoogleChat.AllowedUsers = nil
	env := gchatEnv(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0])
	if env[a2aGchatAllowAllUsersEnvVar].Value != "true" || env[a2aGchatAllowedUsersEnvVar].Value != "" {
		t.Errorf("empty allowlist renders %s=%q %s=%q, want allow-all true and an empty list",
			a2aGchatAllowAllUsersEnvVar, env[a2aGchatAllowAllUsersEnvVar].Value,
			a2aGchatAllowedUsersEnvVar, env[a2aGchatAllowedUsersEnvVar].Value)
	}
}

// TestDisplayModeFollowsTheCRField: debug on the CR reaches the gateway.
func TestDisplayModeFollowsTheCRField(t *testing.T) {
	agent := gchatTestAgent("next", true)
	agent.Spec.Integration.GoogleChat.Mode = "debug"
	env := gchatEnv(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0])
	if env[a2aChatDisplayModeEnvVar].Value != "debug" {
		t.Errorf("%s = %q, want debug", a2aChatDisplayModeEnvVar, env[a2aChatDisplayModeEnvVar].Value)
	}
}

// TestAnUnarmedGatewayRendersAsBefore: today with Chat, next without Chat,
// and next with Chat disabled all render the gateway exactly as main does,
// Discord reference included. Compared field by field rather than against
// a golden, because the golden path cannot produce a gateway (the callout
// gate holds it in a fake client).
func TestAnUnarmedGatewayRendersAsBefore(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
	}{
		{"next without chat", a2aTestAgent()},
		{"next with chat disabled", gchatTestAgent("next", false)},
		{"today with chat", gchatTestAgent("", true)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dep := buildA2AGatewayDeployment(tc.agent)
			c := dep.Spec.Template.Spec.Containers[0]
			env := gchatEnv(c)
			for _, name := range []string{a2aGchatRelayURLEnvVar, a2aGchatAllowedUsersEnvVar, a2aGchatAllowAllUsersEnvVar, a2aChatDisplayModeEnvVar, a2aGchatTokenPathEnvVar} {
				if _, ok := env[name]; ok {
					t.Errorf("%s rendered on an unarmed gateway", name)
				}
			}
			discord, ok := env["DISCORD_TOKEN"]
			if !ok || discord.ValueFrom == nil || discord.ValueFrom.SecretKeyRef == nil || discord.ValueFrom.SecretKeyRef.Name != a2aDiscordBotSecretName {
				t.Errorf("DISCORD_TOKEN is not the optional discord-bot reference on an unarmed gateway: %+v", discord)
			}
			for _, m := range c.VolumeMounts {
				if m.Name == a2aGchatTokenVolume {
					t.Error("the relay token is mounted on an unarmed gateway")
				}
			}
			if podVolume(dep.Spec.Template, a2aGchatTokenVolume) != nil {
				t.Error("the relay token volume is rendered on an unarmed gateway")
			}
		})
	}
}
