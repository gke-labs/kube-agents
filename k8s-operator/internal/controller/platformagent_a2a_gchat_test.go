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
	env := envMapOf(c.Env)

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

// TestDisplayModeFollowsTheCRField: debug on the CR reaches the gateway.
func TestDisplayModeFollowsTheCRField(t *testing.T) {
	agent := gchatTestAgent("next", true)
	agent.Spec.Integration.GoogleChat.Mode = "debug"
	env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
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
			env := envMapOf(c.Env)
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
			// The order, not only the set: the env list is built in pieces
			// now, and "renders as before" includes where each entry sits.
			var names []string
			for _, e := range c.Env {
				names = append(names, e.Name)
			}
			want := []string{"NATS_URL", "NATS_USER", "NATS_PASSWORD", "DISCORD_TOKEN", "A2A_MAX_SESSIONS",
				a2aGatewayMetricsPortEnvVar, "A2A_SPAWN_SESSIONS", "A2A_WORKER_IMAGE", a2aStrictEventsWriterEnvVar, a2aCapabilityRequiredEnvVar, "POD_NAMESPACE",
				"SESSION_KV_SALT", "A2A_OWNER_DEPLOYMENT", "A2A_SESSION_SERVICE_ACCOUNT", a2aPrincipalMapEnvVar}
			// The delegation target allowlists render from the CR's lists
			// whether or not Chat is armed; they are not the Chat adapter's
			// env, so they sit at the tail wherever the CR carries a list.
			for _, e := range a2aTargetAllowlistEnv(tc.agent) {
				want = append(want, e.Name)
			}
			if strings.Join(names, ",") != strings.Join(want, ",") {
				t.Errorf("unarmed env order %v, want %v", names, want)
			}
			if len(c.VolumeMounts) != 1 || c.VolumeMounts[0].Name != "principal-map" {
				t.Errorf("unarmed mounts %v, want the principal map alone", c.VolumeMounts)
			}
		})
	}
}

// TestTheBrokerArmsTheA2ARelayUnderNext: one Chat consumer per install. The
// broker's legacy relay instance is built from GOOGLE_CHAT_SUBSCRIPTION_NAME
// and the A2A one from A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME; under next the
// second gets the install's one subscription and the first is not set, so
// exactly one instance pulls and the proxy's same-name refusal has nothing
// to refuse. The third audience is what confers the a2a-chat role.
func TestTheBrokerArmsTheA2ARelayUnderNext(t *testing.T) {
	agent := gchatTestAgent("next", true)
	env := buildCredentialProxyDeployment(agent, "policy-hash").Spec.Template.Spec.Containers[0].Env
	want := map[string]string{
		"GOOGLE_CHAT_PROJECT_ID":             "chat-project",
		a2aGoogleChatSubscriptionEnvVar:      "projects/chat-project/subscriptions/platform-agent-chat-events-sub",
		credentialProxyA2AChatAudienceEnvVar: credentialProxyA2AChatAudience,
	}
	for name, expected := range want {
		if value, found := brokerEnvValue(env, name); !found || value != expected {
			t.Errorf("%s = %q (present=%v), want %q", name, value, found, expected)
		}
	}
	if value, found := brokerEnvValue(env, "GOOGLE_CHAT_SUBSCRIPTION_NAME"); found {
		t.Errorf("GOOGLE_CHAT_SUBSCRIPTION_NAME=%q rendered beside the A2A subscription: two relay instances would split one subscription, and the proxy refuses to start on the pair", value)
	}
	callers, _ := brokerEnvValue(env, "CREDENTIAL_PROXY_ALLOWED_CALLERS")
	if callers != "system:serviceaccount:test-ns:test-agent,system:serviceaccount:test-ns:test-agent-shell,system:serviceaccount:test-ns:test-agent-a2a-gateway" {
		t.Errorf("CREDENTIAL_PROXY_ALLOWED_CALLERS = %q, want the A2A gateway's ServiceAccount third", callers)
	}
}

// TestTheBrokerKeepsTheLegacyRelayOffNext: today with Chat renders exactly
// the pair main renders; nothing of the A2A side leaks into a today install.
func TestTheBrokerKeepsTheLegacyRelayOffNext(t *testing.T) {
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
	}{
		{"today with chat", gchatTestAgent("", true)},
		{"skew with chat", gchatTestAgent("later", true)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			env := buildCredentialProxyDeployment(tc.agent, "policy-hash").Spec.Template.Spec.Containers[0].Env
			if value, _ := brokerEnvValue(env, "GOOGLE_CHAT_SUBSCRIPTION_NAME"); value != "projects/chat-project/subscriptions/platform-agent-chat-events-sub" {
				t.Errorf("legacy subscription = %q", value)
			}
			for _, name := range []string{a2aGoogleChatSubscriptionEnvVar, credentialProxyA2AChatAudienceEnvVar} {
				if value, found := brokerEnvValue(env, name); found {
					t.Errorf("%s=%q rendered on a today install", name, value)
				}
			}
			if callers, _ := brokerEnvValue(env, "CREDENTIAL_PROXY_ALLOWED_CALLERS"); strings.Contains(callers, "a2a-gateway") {
				t.Errorf("the A2A gateway is a broker caller on a today install: %q", callers)
			}
		})
	}
}

// TestNoRenderCarriesBothChatSubscriptions walks every container the
// operator renders for one CR, under today and under next, and asserts the
// two subscription variables never meet. This is the double-answer guard
// in one assertion.
func TestNoRenderCarriesBothChatSubscriptions(t *testing.T) {
	for _, agent := range []*agentv1alpha1.PlatformAgent{gchatTestAgent("", true), gchatTestAgent("next", true)} {
		containers := buildCredentialProxyDeployment(agent, "h").Spec.Template.Spec.Containers
		containers = append(containers, buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers...)
		containers = append(containers, buildPodTemplateSpec(agent, "h", "h", "h", "h", nil, renderOptions{}).Spec.Containers...)
		legacy, a2a := false, false
		for _, c := range containers {
			env := envMapOf(c.Env)
			if _, ok := env["GOOGLE_CHAT_SUBSCRIPTION_NAME"]; ok {
				legacy = true
			}
			if _, ok := env[a2aGoogleChatSubscriptionEnvVar]; ok {
				a2a = true
			}
		}
		if legacy && a2a {
			t.Errorf("mode %v: both Chat subscription variables are rendered; every message would be answered twice", ptr.Deref(agent.Spec.Mode, "today"))
		}
		if !legacy && !a2a {
			t.Errorf("mode %v: neither Chat subscription variable is rendered; Chat is enabled and nobody consumes it", ptr.Deref(agent.Spec.Mode, "today"))
		}
	}
}

// TestTheBrokerFenceAdmitsTheGatewayWhenArmed: TokenReview rejects a caller
// the broker does not serve; the NetworkPolicy is the layer that keeps it
// from opening the connection, and the A2A gateway pod must be a peer on
// the broker port when, and only when, it is a caller.
func TestTheBrokerFenceAdmitsTheGatewayWhenArmed(t *testing.T) {
	armed := buildCredentialProxyNetworkPolicy(gchatTestAgent("next", true))
	from := armed.Spec.Ingress[0].From
	if len(from) != 3 {
		t.Fatalf("armed: %d peers on the broker port, want 3 (sandbox, gateway, A2A gateway): %#v", len(from), from)
	}
	var found bool
	for _, peer := range from {
		if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == "test-agent-a2a-gateway" {
			found = true
			if peer.NamespaceSelector != nil || peer.IPBlock != nil {
				t.Errorf("the A2A gateway peer reaches outside the namespace: %#v", peer)
			}
		}
	}
	if !found {
		t.Error("the A2A gateway pod is not a peer; its relay pulls would be refused before TokenReview")
	}
	for _, agent := range []*agentv1alpha1.PlatformAgent{gchatTestAgent("", true), a2aTestAgent()} {
		if got := len(buildCredentialProxyNetworkPolicy(agent).Spec.Ingress[0].From); got != 2 {
			t.Errorf("unarmed: %d peers, want the two main renders", got)
		}
	}
}

// TestTheLegacyChatConsumerIsNotRenderedUnderNext: the Hermes google_chat
// platform, its relay env on the gateway container and its pins in the
// managed .env all go with the mode. Under today they are exactly what main
// renders; under skew they come back, because renderMode fails closed.
func TestTheLegacyChatConsumerIsNotRenderedUnderNext(t *testing.T) {
	legacyNames := []string{"GOOGLE_CHAT_RELAY_URL", "GOOGLE_CHAT_PROJECT_ID", "GOOGLE_CHAT_SUBSCRIPTION_NAME", "GOOGLE_CHAT_ALLOWED_USERS", "GOOGLE_CHAT_ALLOW_ALL_USERS"}
	for _, tc := range []struct {
		name   string
		agent  *agentv1alpha1.PlatformAgent
		legacy bool
	}{
		{"today with chat", gchatTestAgent("", true), true},
		{"skew with chat", gchatTestAgent("later", true), true},
		{"next with chat", gchatTestAgent("next", true), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pod := buildPodTemplateSpec(tc.agent, "h", "h", "h", "h", nil, renderOptions{})
			env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
			managed := renderManagedEnv(tc.agent)
			config := renderConfigYAML(tc.agent, nil)
			for _, name := range legacyNames {
				_, inEnv := env[name]
				inManaged := strings.Contains(managed, name+"=")
				if inEnv != tc.legacy {
					t.Errorf("%s in the gateway container env = %v, want %v", name, inEnv, tc.legacy)
				}
				if inManaged != tc.legacy {
					t.Errorf("%s in the managed .env = %v, want %v", name, inManaged, tc.legacy)
				}
			}
			enabled := strings.Contains(config, "google_chat:\n    enabled: true")
			if enabled != tc.legacy {
				t.Errorf("platforms.google_chat.enabled rendered %v, want %v; config:\n%s", enabled, tc.legacy, config)
			}
		})
	}
}

// TestTheAllowlistIsNormalizedTheWayTheGatewayReadsIt: the list the gateway
// sees is normalized with its own grammar (joined, split on commas, trimmed,
// empties dropped), and the allow-all decision is the legacy consumer's rule
// on the RAW CR list (allowAllUsers: absent or a single empty string). So a
// degenerate list - whitespace or commas only - is a restriction to nobody in
// both modes, which the gateway announces at boot, never a silent widening to
// everyone on the mode flip.
func TestTheAllowlistIsNormalizedTheWayTheGatewayReadsIt(t *testing.T) {
	for _, tc := range []struct {
		name     string
		users    []string
		wantList string
		wantAll  string
	}{
		{"whitespace only", []string{" "}, "", "false"},
		{"empty strings only", []string{"", "  "}, "", "false"},
		{"padded and empty entries", []string{" a@example.com ", "", "b@example.com"}, "a@example.com,b@example.com", "false"},
		// The gateway splits the JOINED string on commas, so an entry that is
		// only commas and whitespace is empty on its side too, and an entry
		// that holds a comma is two entries there.
		{"commas only", []string{","}, "", "false"},
		{"commas and whitespace", []string{" , ", ",,"}, "", "false"},
		{"a comma inside one entry", []string{"a@example.com, b@example.com"}, "a@example.com,b@example.com", "false"},
		{"nil", nil, "", "true"},
		{"the legacy pin's single empty string", []string{""}, "", "true"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := gchatTestAgent("next", true)
			agent.Spec.Integration.GoogleChat.AllowedUsers = tc.users
			env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
			if got := env[a2aGchatAllowedUsersEnvVar].Value; got != tc.wantList {
				t.Errorf("%s = %q, want %q", a2aGchatAllowedUsersEnvVar, got, tc.wantList)
			}
			if got := env[a2aGchatAllowAllUsersEnvVar].Value; got != tc.wantAll {
				t.Errorf("%s = %q, want %q", a2aGchatAllowAllUsersEnvVar, got, tc.wantAll)
			}
		})
	}
}

// TestTheAllowAllDecisionMatchesTheLegacyConsumer: one CR, one answer to "is
// everyone allowed", whichever consumer the mode renders. The A2A render's
// flag is compared with the legacy pod env's for the same list, so a CR that
// answers nobody under today cannot answer everyone under next.
func TestTheAllowAllDecisionMatchesTheLegacyConsumer(t *testing.T) {
	for _, users := range [][]string{nil, {""}, {" "}, {","}, {" , ", ",,"}, {"", "  "}, {"a@example.com"}, {" a@example.com ", ""}} {
		next := gchatTestAgent("next", true)
		next.Spec.Integration.GoogleChat.AllowedUsers = users
		today := gchatTestAgent("", true)
		today.Spec.Integration.GoogleChat.AllowedUsers = users
		a2a := envMapOf(buildA2AGatewayDeployment(next).Spec.Template.Spec.Containers[0].Env)[a2aGchatAllowAllUsersEnvVar].Value
		legacy := envMapOf(brokerContainerNamed(buildPodTemplateSpec(today, "h", "h", "h", "h", nil, renderOptions{}).Spec.Containers, "platform-agent").Env)["GOOGLE_CHAT_ALLOW_ALL_USERS"].Value
		if a2a != legacy {
			t.Errorf("allowedUsers=%q: next renders allow-all %s where today renders %s", users, a2a, legacy)
		}
	}
}

// TestTheHomeChannelReachesTheArmedGateway: the chat.notify route posts to
// googleChat.homeChannel, so the armed gateway carries it, trimmed, and an
// install with no home channel leaves the route unarmed rather than handing
// the gateway an empty value it would have to refuse.
func TestTheHomeChannelReachesTheArmedGateway(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for _, tc := range []struct {
		name, home, want string
		present          bool
	}{
		{"set", "spaces/AAAA", "spaces/AAAA", true},
		{"padded", "  spaces/AAAA ", "spaces/AAAA", true},
		{"unset", "", "", false},
		{"blank", "   ", "", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := gchatTestAgent("next", true)
			agent.Spec.Integration.GoogleChat.HomeChannel = tc.home
			env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
			got, ok := env[a2aGchatHomeChannelEnvVar]
			if ok != tc.present || got.Value != tc.want {
				t.Errorf("%s = %q (present=%v), want %q (present=%v)", a2aGchatHomeChannelEnvVar, got.Value, ok, tc.want, tc.present)
			}
		})
	}
	// Not under today: the legacy Hermes platform has the channel there.
	today := gchatTestAgent("", true)
	today.Spec.Integration.GoogleChat.HomeChannel = "spaces/AAAA"
	if _, ok := envMapOf(buildA2AGatewayDeployment(today).Spec.Template.Spec.Containers[0].Env)[a2aGchatHomeChannelEnvVar]; ok {
		t.Errorf("%s rendered on an unarmed gateway", a2aGchatHomeChannelEnvVar)
	}
}

// TestTheAgentRoutesProactivePostsToTheGatewayExactlyWhenArmed: the agent
// container is told to send proactive posts through chat.notify exactly when
// the next stack holds Google Chat AND the home channel is a space name, which
// is when the gateway arms the route. Telling it with no route behind it
// would send every post to a subject nobody answers.
func TestTheAgentRoutesProactivePostsToTheGatewayExactlyWhenArmed(t *testing.T) {
	for _, tc := range []struct {
		name  string
		agent *agentv1alpha1.PlatformAgent
		want  bool
	}{
		{"today with chat", gchatTestAgent("", true), false},
		{"skew with chat", gchatTestAgent("later", true), false},
		{"next with chat", withHome(gchatTestAgent("next", true), "spaces/AAAA"), true},
		{"next with chat, no home channel", gchatTestAgent("next", true), false},
		{"next with chat, not a space", withHome(gchatTestAgent("next", true), "spaces/AAAA/threads/B"), false},
		{"next with chat, bare id", withHome(gchatTestAgent("next", true), "AAAA"), false},
		{"next without chat", withHome(gchatTestAgent("next", false), "spaces/AAAA"), false},
		{"today with chat and home", withHome(gchatTestAgent("", true), "spaces/AAAA"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pod := buildPodTemplateSpec(tc.agent, "h", "h", "h", "h", nil, renderOptions{})
			env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
			got, ok := env[a2aNotifyPlatformEnvVar]
			if ok != tc.want {
				t.Fatalf("%s present = %v, want %v", a2aNotifyPlatformEnvVar, ok, tc.want)
			}
			if ok && got.Value != "google_chat" {
				t.Errorf("%s = %q, want google_chat (the platform name the agent-side callers use)", a2aNotifyPlatformEnvVar, got.Value)
			}
			if tc.want && legacyChatConsumer(tc.agent) {
				t.Error("the notify route and the legacy Hermes platform both render")
			}
		})
	}
}

func withHome(agent *agentv1alpha1.PlatformAgent, home string) *agentv1alpha1.PlatformAgent {
	agent.Spec.Integration.GoogleChat.HomeChannel = home
	return agent
}
