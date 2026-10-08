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
	"context"
	"strconv"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The Slack token pair the tests configure: a Secret and keys of the
// install's own naming, so a render that reads a name of its own (the
// legacy default, or a fixed admin Secret) is told apart from one that
// reads the CR.
const (
	slackTestSecret = "team-slack" // #nosec G101 -- test Secret name, not a credential
	slackTestBotKey = "bot"
	slackTestAppKey = "app"
)

// slackTestAgent is a2aTestAgent with spec.integration.slack configured the
// way the CRD requires once it is enabled: both token refs present. mode is
// the CR's spec.mode ("" leaves it absent). The refs say nothing about
// optional, as the chart renders them.
func slackTestAgent(mode string, enabled bool) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	if mode == "" {
		agent.Spec.Mode = nil
	} else {
		agent.Spec.Mode = ptr.To(mode)
	}
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		Slack: &agentv1alpha1.SlackSpec{
			Enabled: ptr.To(enabled),
			BotTokenSecretRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: slackTestSecret}, Key: slackTestBotKey,
			},
			AppTokenSecretRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: slackTestSecret}, Key: slackTestAppKey,
			},
			AllowedUsers: []string{"U123"},
		},
	}
	return agent
}

// chatAndSlackTestAgent enables both integrations on one CR.
func chatAndSlackTestAgent(mode string) *agentv1alpha1.PlatformAgent {
	agent := gchatTestAgent(mode, true)
	agent.Spec.Integration.Slack = slackTestAgent(mode, true).Spec.Integration.Slack
	return agent
}

// TestSlackConsumerIsChosenByMode: one Slack app, one Socket Mode consumer.
// The two predicates are exact complements whenever Slack is enabled and
// both false when it is not. Chat holds the gateway first when both are
// enabled under next (the gateway runs one backend per process), and Slack
// then stays on the legacy consumer rather than reaching nobody.
func TestSlackConsumerIsChosenByMode(t *testing.T) {
	for _, tc := range []struct {
		name          string
		agent         *agentv1alpha1.PlatformAgent
		armed, legacy bool
	}{
		{"today with slack", slackTestAgent("", true), false, true},
		{"today explicit with slack", slackTestAgent("today", true), false, true},
		{"next with slack", slackTestAgent("next", true), true, false},
		{"next with slack disabled", slackTestAgent("next", false), false, false},
		{"today with slack disabled", slackTestAgent("", false), false, false},
		{"next with no integration", a2aTestAgent(), false, false},
		{"next with chat only", gchatTestAgent("next", true), false, false},
		{"next with chat and slack", chatAndSlackTestAgent("next"), false, true},
		{"today with chat and slack", chatAndSlackTestAgent(""), false, true},
		// Version skew fails closed to today, as a2aChatArmed does.
		{"skew with slack", slackTestAgent("later", true), false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := a2aSlackArmed(tc.agent); got != tc.armed {
				t.Errorf("a2aSlackArmed = %v, want %v", got, tc.armed)
			}
			if got := legacySlackConsumer(tc.agent); got != tc.legacy {
				t.Errorf("legacySlackConsumer = %v, want %v", got, tc.legacy)
			}
			if a2aSlackArmed(tc.agent) && a2aChatArmed(tc.agent) {
				t.Error("Slack and Chat both armed on the gateway; it refuses two real backends")
			}
		})
	}
}

// TestAnArmedGatewayCarriesTheSlackBackend: under next with Slack enabled,
// the gateway's pair is read through the CR's own refs, required whatever
// the ref says, and the Discord reference is not rendered beside it.
func TestAnArmedGatewayCarriesTheSlackBackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := slackTestAgent("next", true)
	// An admin who marked a ref optional would get a gateway that starts on
	// half a pair and exits; the gateway's copy is required regardless.
	agent.Spec.Integration.Slack.AppTokenSecretRef.Optional = ptr.To(true)
	env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
	for name, key := range map[string]string{a2aSlackBotTokenEnvVar: slackTestBotKey, a2aSlackAppTokenEnvVar: slackTestAppKey} {
		e, ok := env[name]
		if !ok || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
			t.Fatalf("%s is not a Secret reference on the armed gateway: %+v", name, e)
		}
		ref := e.ValueFrom.SecretKeyRef
		if ref.Name != slackTestSecret || ref.Key != key {
			t.Errorf("%s reads %s/%s, want the CR's %s/%s", name, ref.Name, ref.Key, slackTestSecret, key)
		}
		if ptr.Deref(ref.Optional, false) {
			t.Errorf("%s is an optional reference: a missing key would start the gateway on half a pair, which it refuses at boot", name)
		}
	}
	if !ptr.Deref(agent.Spec.Integration.Slack.AppTokenSecretRef.Optional, false) {
		t.Error("the render mutated the CR's own ref")
	}
	if _, ok := env["DISCORD_TOKEN"]; ok {
		t.Error("DISCORD_TOKEN is rendered beside the Slack backend; a discord-bot Secret left in the namespace would stop the gateway on two backends")
	}
}

// TestAnUnarmedGatewayCarriesNoSlackPair: off next, or with Slack disabled,
// the gateway carries no Slack env at all; the Discord reference is what it
// was on main.
func TestAnUnarmedGatewayCarriesNoSlackPair(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for _, agent := range []*agentv1alpha1.PlatformAgent{a2aTestAgent(), slackTestAgent("next", false)} {
		env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
		for _, name := range []string{a2aSlackBotTokenEnvVar, a2aSlackAppTokenEnvVar} {
			if _, ok := env[name]; ok {
				t.Errorf("%s is rendered on a gateway Slack does not arm", name)
			}
		}
		if e, ok := env["DISCORD_TOKEN"]; !ok || e.ValueFrom.SecretKeyRef.Name != a2aDiscordBotSecretName {
			t.Errorf("DISCORD_TOKEN = %+v, want the optional discord-bot reference main renders", e)
		}
	}
}

// TestTheSlackIntegrationRendersTheGateway: Slack enabled under next is a
// backend on the CR alone, so the gate renders the gateway with no Secret
// in the namespace - the same no-read answer Chat gets. A missing token
// Secret is the kubelet's to report on the pod (the refs are required).
func TestTheSlackIntegrationRendersTheGateway(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := slackTestAgent("next", true)
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	if state.gatewayDark {
		t.Fatalf("Slack is enabled under next and the gateway is reported dark: %q", state.gatewayDarkReason)
	}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("the gateway Deployment was not rendered for a Slack install: %v", err)
	}
	if _, ok := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)[a2aSlackAppTokenEnvVar]; !ok {
		t.Errorf("the rendered gateway carries no %s", a2aSlackAppTokenEnvVar)
	}
}

// TestTheDarkReasonNamesSlack: the remedy an admin reads off the A2AGateway
// condition names the Slack integration beside the Chat field, the Discord
// Secret and the door.
func TestTheDarkReasonNamesSlack(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	state, err := r.reconcileA2A(ctx, agent)
	if err != nil || !state.gatewayDark {
		t.Fatalf("precondition: want a dark gateway (state=%+v err=%v)", state, err)
	}
	for _, want := range []string{"spec.integration.slack", "spec.integration.googleChat", a2aDiscordBotSecretName, a2aInjectBackendEnvVar} {
		if !strings.Contains(state.gatewayDarkReason, want) {
			t.Errorf("the reason does not name %q: %q", want, state.gatewayDarkReason)
		}
	}
}

// slackLegacyBrokerNames are what arm the broker's own Socket Mode
// connection (credential_proxy.py, serve: SlackRelay is built when both are
// set), and slackLegacyAgentNames what point Hermes's slack platform at it.
var (
	slackLegacyBrokerNames = []string{"SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"}
	slackLegacyAgentNames  = []string{"SLACK_RELAY_URL", "SLACK_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS"}
)

// TestTheLegacySlackConsumerIsNotRenderedUnderNext: the broker's Slack relay
// pair, the Hermes slack platform, its relay env on the agent container and
// its pins in the managed .env all go with the mode, as Chat's do. Under
// today and skew they are what main renders; with Chat holding the next
// gateway they stay, because the gateway has no room for Slack.
func TestTheLegacySlackConsumerIsNotRenderedUnderNext(t *testing.T) {
	for _, tc := range []struct {
		name   string
		agent  *agentv1alpha1.PlatformAgent
		legacy bool
	}{
		{"today with slack", slackTestAgent("", true), true},
		{"skew with slack", slackTestAgent("later", true), true},
		{"next with slack", slackTestAgent("next", true), false},
		{"next with chat and slack", chatAndSlackTestAgent("next"), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			broker := envMapOf(buildCredentialProxyDeployment(tc.agent, "h").Spec.Template.Spec.Containers[0].Env)
			for _, name := range slackLegacyBrokerNames {
				if _, ok := broker[name]; ok != tc.legacy {
					t.Errorf("%s on the broker = %v, want %v", name, ok, tc.legacy)
				}
			}
			pod := buildPodTemplateSpec(tc.agent, "h", "h", "h", "h", nil, renderOptions{})
			env := envMapOf(brokerContainerNamed(pod.Spec.Containers, "platform-agent").Env)
			managed := renderManagedEnv(tc.agent)
			for _, name := range slackLegacyAgentNames {
				if _, ok := env[name]; ok != tc.legacy {
					t.Errorf("%s in the agent container env = %v, want %v", name, ok, tc.legacy)
				}
				if got := strings.Contains(managed, name+"="); got != tc.legacy {
					t.Errorf("%s in the managed .env = %v, want %v", name, got, tc.legacy)
				}
			}
			config := renderConfigYAML(tc.agent, nil)
			if enabled := strings.Contains(config, "slack:\n    enabled: true"); enabled != tc.legacy {
				t.Errorf("platforms.slack.enabled rendered %v, want %v; config:\n%s", enabled, tc.legacy, config)
			}
		})
	}
}

// TestNoRenderCarriesTwoSlackSocketModeConsumers walks every container the
// operator renders for one CR and counts the ones handed the app token,
// which is what opens a Socket Mode connection (the broker's SlackRelay, the
// gateway's Slack adapter). Slack spreads one app's events across every open
// connection, so two would split the workspace's messages; none would drop
// them. Exactly one, in every mode and beside Chat.
func TestNoRenderCarriesTwoSlackSocketModeConsumers(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"today":             slackTestAgent("", true),
		"next":              slackTestAgent("next", true),
		"skew":              slackTestAgent("later", true),
		"next with chat":    chatAndSlackTestAgent("next"),
		"today with chat":   chatAndSlackTestAgent(""),
		"next, inject door": slackTestAgent("next", true),
	} {
		t.Run(name, func(t *testing.T) {
			if strings.HasSuffix(name, "inject door") {
				t.Setenv(a2aInjectBackendEnvVar, "true")
			}
			containers := buildCredentialProxyDeployment(agent, "h").Spec.Template.Spec.Containers
			containers = append(containers, buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers...)
			containers = append(containers, buildPodTemplateSpec(agent, "h", "h", "h", "h", nil, renderOptions{}).Spec.Containers...)
			var holders []string
			for _, c := range containers {
				if _, ok := envMapOf(c.Env)["SLACK_APP_TOKEN"]; ok {
					holders = append(holders, c.Name)
				}
			}
			if len(holders) != 1 {
				t.Errorf("containers handed SLACK_APP_TOKEN: %v, want exactly one", holders)
			}
		})
	}
}

// principalMapSources names every ConfigMap and Secret the gateway's
// principal-map volume reads, whatever shape the volume takes (a plain
// ConfigMap or Secret volume, or a projection of either), each with whether
// its reference is optional. A test that read one shape only would pass a
// render that moved the ConfigMap into the other.
func principalMapSources(t *testing.T, vol *corev1.Volume) (configMaps, secrets map[string]bool) {
	t.Helper()
	configMaps, secrets = map[string]bool{}, map[string]bool{}
	opt := func(b *bool) bool { return b != nil && *b }
	switch {
	case vol.ConfigMap != nil:
		configMaps[vol.ConfigMap.Name] = opt(vol.ConfigMap.Optional)
	case vol.Secret != nil:
		secrets[vol.Secret.SecretName] = opt(vol.Secret.Optional)
	case vol.Projected != nil:
		for _, src := range vol.Projected.Sources {
			switch {
			case src.ConfigMap != nil:
				configMaps[src.ConfigMap.Name] = opt(src.ConfigMap.Optional)
			case src.Secret != nil:
				secrets[src.Secret.Name] = opt(src.Secret.Optional)
			default:
				t.Errorf("unexpected projection source in the principal map: %+v", src)
			}
		}
	default:
		t.Fatalf("volume %s reads neither a ConfigMap nor a Secret: %+v", vol.Name, vol)
	}
	return configMaps, secrets
}

// TestTheGatewayMapIsTheArmedBackendsTable: the principal map is one volume
// at the gateway's one path, with A2A_PRINCIPAL_MAP rendered at that path so
// the two are one fact, and what it reads is the armed backend's table and
// nothing else. A Slack-armed gateway reads the a2a-slack-principal-map
// Secret alone: the gateway loads the directory as one flat map and resolves
// a Slack sender against every key in it, so a ConfigMap in the same
// directory would let a configmaps write grant a Slack principal - the
// impersonation primitive spec-chatops-gateway.md keeps the table out of a
// ConfigMap to avoid. Every other render keeps main's volume, the hand-made
// principal-map ConfigMap, and does not reference the Slack Secret. Both
// optional, the gateway's own rule for a missing table. No DefaultMode: the
// pod runs as uid 1000 with no fsGroup, so a 0400 Secret file would be root's
// and unreadable. The door rows check that arming the eval door changes none
// of this: its map is its own ConfigMap at its own path.
func TestTheGatewayMapIsTheArmedBackendsTable(t *testing.T) {
	for _, tc := range []struct {
		name   string
		agent  *agentv1alpha1.PlatformAgent
		door   bool
		secret bool // true: the Slack Secret alone; false: the Discord ConfigMap alone
	}{
		{name: "discord", agent: a2aTestAgent()},
		{name: "door", agent: a2aTestAgent(), door: true},
		{name: "slack on the legacy path", agent: slackTestAgent("", true)},
		{name: "chat holds the gateway over slack", agent: chatAndSlackTestAgent("next")},
		{name: "slack armed", agent: slackTestAgent("next", true), secret: true},
		{name: "slack armed beside the door", agent: slackTestAgent("next", true), door: true, secret: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if tc.door {
				t.Setenv(a2aInjectBackendEnvVar, "true")
			} else {
				t.Setenv(a2aInjectBackendEnvVar, "")
			}
			dep := buildA2AGatewayDeployment(tc.agent)
			c := dep.Spec.Template.Spec.Containers[0]
			if got := envMapOf(c.Env)[a2aPrincipalMapEnvVar].Value; got != a2aPrincipalMapDir {
				t.Errorf("%s = %q, want %q", a2aPrincipalMapEnvVar, got, a2aPrincipalMapDir)
			}
			var mounts []corev1.VolumeMount
			for _, m := range c.VolumeMounts {
				if m.MountPath == a2aPrincipalMapDir {
					mounts = append(mounts, m)
				}
			}
			if len(mounts) != 1 || mounts[0].Name != a2aPrincipalMapVolume || !mounts[0].ReadOnly {
				t.Fatalf("mounts at %s = %+v, want one, read-only, of volume %s", a2aPrincipalMapDir, mounts, a2aPrincipalMapVolume)
			}
			vol := podVolume(dep.Spec.Template, a2aPrincipalMapVolume)
			if vol == nil {
				t.Fatalf("no volume %s", a2aPrincipalMapVolume)
			}
			if vol.Projected != nil && vol.Projected.DefaultMode != nil {
				t.Errorf("the map carries DefaultMode %o; without an fsGroup a non-default mode leaves a Secret's files root-owned", *vol.Projected.DefaultMode)
			}
			if vol.Secret != nil && vol.Secret.DefaultMode != nil {
				t.Errorf("the map carries DefaultMode %o; without an fsGroup a non-default mode leaves the Secret's files root-owned", *vol.Secret.DefaultMode)
			}
			configMaps, secrets := principalMapSources(t, vol)
			if tc.secret {
				if len(configMaps) != 0 {
					t.Errorf("a Slack-armed gateway's map reads ConfigMaps %v; a configmaps write would grant a Slack principal", configMaps)
				}
				if optional, ok := secrets[a2aSlackPrincipalMapSecretName]; !ok || len(secrets) != 1 {
					t.Errorf("a Slack-armed gateway's map reads Secrets %v, want %s alone", secrets, a2aSlackPrincipalMapSecretName)
				} else if !optional {
					t.Errorf("the %s Secret is not optional; the gateway's rule for a missing map is to run and drop every sender", a2aSlackPrincipalMapSecretName)
				}
			} else {
				if len(secrets) != 0 {
					t.Errorf("a gateway without Slack armed reads Secrets %v in its map, want main's ConfigMap alone", secrets)
				}
				if optional, ok := configMaps[a2aPrincipalMapConfigMapName]; !ok || len(configMaps) != 1 {
					t.Errorf("the map reads ConfigMaps %v, want %s alone", configMaps, a2aPrincipalMapConfigMapName)
				} else if !optional {
					t.Error("the Discord table is not optional; an install without it would not schedule the gateway")
				}
			}
			// The door's map is untouched by the Slack arm: its own
			// operator-rendered ConfigMap, at its own path.
			if tc.door {
				door := podVolume(dep.Spec.Template, "inject-principal-map")
				if door == nil || door.ConfigMap == nil || door.ConfigMap.Name != a2aInjectName(tc.agent) {
					t.Errorf("the door's map volume = %+v, want ConfigMap %s", door, a2aInjectName(tc.agent))
				}
			}
		})
	}
}

// TestChatArmedDropsTheSlackReferences: one real backend per gateway process.
// With Chat armed on the CR the gateway carries Chat alone - no Slack pair
// even with Slack enabled, and no Discord reference - so neither a Slack
// integration nor a Secret left in the namespace can make it refuse to start.
func TestChatArmedDropsTheSlackReferences(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	env := envMapOf(buildA2AGatewayDeployment(chatAndSlackTestAgent("next")).Spec.Template.Spec.Containers[0].Env)
	for _, name := range []string{a2aSlackBotTokenEnvVar, a2aSlackAppTokenEnvVar, "DISCORD_TOKEN"} {
		if _, ok := env[name]; ok {
			t.Errorf("%s is rendered beside the Chat backend", name)
		}
	}
	if _, ok := env[a2aGchatRelayURLEnvVar]; !ok {
		t.Errorf("%s is missing: Chat is armed and holds the gateway", a2aGchatRelayURLEnvVar)
	}
}

// TestNoRenderedRoleReachesASecret: the identity table is an impersonation
// primitive, so no ServiceAccount the operator mints a Role for may hold a
// verb on Secrets - the a2a-slack-principal-map Secret is reachable by the
// kubelet's mount alone. Every Role builder, next stack
// and legacy, is rendered and read; a wildcard resource counts as reaching
// them. What this does not bound is the gateway's `create` on pods, which can
// mount a Secret into a pod it builds; buildA2AGatewayRole's comment records
// that as the admission policy it owes.
func TestNoRenderedRoleReachesASecret(t *testing.T) {
	agent := a2aTestAgent()
	roles := map[string][]rbacv1.PolicyRule{
		"a2a gateway":        buildA2AGatewayRole(agent).Rules,
		"a2a callout":        buildA2ACalloutRole(agent).Rules,
		"broker tokenreview": buildCredentialBrokerTokenReviewRole(agent).Rules,
		"platform minimal":   buildMinimalPlatformRole(agent).Rules,
		"platform local":     buildPlatformLocalRole(agent).Rules,
		"platform leader":    buildPlatformLeaderRole(agent).Rules,
	}
	for name, rules := range roles {
		if len(rules) == 0 {
			t.Errorf("%s: no rules rendered; the assertion below would hold vacuously", name)
		}
		for _, rule := range rules {
			for _, res := range rule.Resources {
				if res == "secrets" || res == "*" {
					t.Errorf("%s grants %v on %q", name, rule.Verbs, res)
				}
			}
		}
	}
}

// TestNoEgressPolicySelectsTheGatewayPod: Slack is reached outbound (the
// Socket Mode websocket and the Web API), and the gateway pod carries no
// egress fence - its one policy is the ingress-only inject fence - so no
// rule has to admit those hosts, and none could name them: the repository's
// policies are selector and CIDR based. Pinned here so the day a deny-default
// egress fence is put on the gateway, this test is what says Slack's egress
// must come with it.
func TestNoEgressPolicySelectsTheGatewayPod(t *testing.T) {
	agent := a2aTestAgent()
	dns := []string{"10.96.0.10"}
	gatewayPod := labels.Set(buildA2AGatewayDeployment(agent).Spec.Template.Labels)
	agentEgress, _ := buildAgentEgressNetworkPolicy(agent, dns, "")
	policies := []*networkingv1.NetworkPolicy{
		buildA2AGatewayNetworkPolicy(agent),
		buildA2ASessionNetworkPolicy(agent, dns),
		buildA2ANATSNetworkPolicy(agent),
		buildA2AVerifierNetworkPolicy(agent, dns),
		buildCredentialProxyNetworkPolicy(agent),
		buildShellSandboxNetworkPolicy(agent, dns),
		agentEgress,
	}
	for _, pol := range policies {
		if pol == nil {
			continue
		}
		sel, err := metav1.LabelSelectorAsSelector(&pol.Spec.PodSelector)
		if err != nil {
			t.Fatalf("%s: %v", pol.Name, err)
		}
		if !sel.Matches(gatewayPod) {
			continue
		}
		for _, pt := range pol.Spec.PolicyTypes {
			if pt == networkingv1.PolicyTypeEgress {
				t.Errorf("%s fences the gateway pod's egress; Slack's websocket and Web API need admitting in it", pol.Name)
			}
		}
	}
}

// TestAnArmedGatewayCarriesTheSlackAllowlist: under next the gateway gates a
// Slack sender on spec.integration.slack.allowedUsers as well as the
// principal map, the way Chat's gateway gates on its own list. The list is
// normalized with the gateway's grammar and the allow-all flag is the legacy
// consumer's rule on the RAW list, exactly as Chat's render does
// (TestTheAllowlistIsNormalizedTheWayTheGatewayReadsIt), so a degenerate
// list restricts to nobody rather than widening to everyone on the flip.
func TestAnArmedGatewayCarriesTheSlackAllowlist(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for _, tc := range []struct {
		name     string
		users    []string
		wantList string
		wantAll  string
	}{
		{"one member", []string{"U1"}, "U1", "false"},
		{"padded and empty entries", []string{" U1 ", "", "U2"}, "U1,U2", "false"},
		{"a comma inside one entry", []string{"U1, U2"}, "U1,U2", "false"},
		{"whitespace only", []string{" "}, "", "false"},
		{"commas only", []string{","}, "", "false"},
		{"nil", nil, "", "true"},
		{"empty list", []string{}, "", "true"},
		{"the legacy pin's single empty string", []string{""}, "", "true"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			agent := slackTestAgent("next", true)
			agent.Spec.Integration.Slack.AllowedUsers = tc.users
			env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
			list, ok := env[a2aSlackAllowedUsersEnvVar]
			if !ok {
				t.Fatalf("%s is not rendered on the armed gateway", a2aSlackAllowedUsersEnvVar)
			}
			if list.Value != tc.wantList {
				t.Errorf("%s = %q, want %q", a2aSlackAllowedUsersEnvVar, list.Value, tc.wantList)
			}
			if got := env[a2aSlackAllowAllUsersEnvVar].Value; got != tc.wantAll {
				t.Errorf("%s = %q, want %q", a2aSlackAllowAllUsersEnvVar, got, tc.wantAll)
			}
		})
	}
}

// TestTheSlackAllowAllDecisionMatchesTheLegacyConsumer: one CR, one answer
// to "is every Slack member allowed", whichever consumer the mode renders.
func TestTheSlackAllowAllDecisionMatchesTheLegacyConsumer(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for _, users := range [][]string{nil, {}, {""}, {" "}, {","}, {" , ", ",,"}, {"", "  "}, {"U1"}, {" U1 ", ""}} {
		next := slackTestAgent("next", true)
		next.Spec.Integration.Slack.AllowedUsers = users
		today := slackTestAgent("", true)
		today.Spec.Integration.Slack.AllowedUsers = users
		a2a := envMapOf(buildA2AGatewayDeployment(next).Spec.Template.Spec.Containers[0].Env)[a2aSlackAllowAllUsersEnvVar].Value
		legacy := envMapOf(brokerContainerNamed(buildPodTemplateSpec(today, "h", "h", "h", "h", nil, renderOptions{}).Spec.Containers, "platform-agent").Env)["SLACK_ALLOW_ALL_USERS"].Value
		if a2a == "" || a2a != legacy {
			t.Errorf("allowedUsers=%q: next renders allow-all %q where today renders %q", users, a2a, legacy)
		}
	}
}

// TestAnUnarmedGatewayCarriesNoSlackAllowlist: the pair rides with the
// backend, so a gateway Slack does not arm (off next, Slack disabled, or
// Chat holding the gateway) carries neither.
func TestAnUnarmedGatewayCarriesNoSlackAllowlist(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for _, agent := range []*agentv1alpha1.PlatformAgent{a2aTestAgent(), slackTestAgent("next", false), chatAndSlackTestAgent("next")} {
		env := envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
		for _, name := range []string{a2aSlackAllowedUsersEnvVar, a2aSlackAllowAllUsersEnvVar} {
			if _, ok := env[name]; ok {
				t.Errorf("%s is rendered on a gateway Slack does not arm", name)
			}
		}
	}
}

// gatewayDigestAfterPass runs one A2A pass and returns the secret-env digest
// on the rendered gateway's pod template, failing if there is no gateway: a
// digest read off an absent Deployment is "" and would pass on absence.
func gatewayDigestAfterPass(t *testing.T, ctx context.Context, r *PlatformAgentReconciler, agent *agentv1alpha1.PlatformAgent) string {
	t.Helper()
	if _, err := r.reconcileA2A(ctx, agent); err != nil {
		t.Fatalf("reconcileA2A: %v", err)
	}
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("the gateway Deployment was not rendered: %v", err)
	}
	return dep.Spec.Template.Annotations[secretEnvHashAnnotation]
}

// rotateSecretKey rewrites one key of a Secret in place, the way `kubectl
// apply` of a new value does: same object, same UID, new data.
func rotateSecretKey(t *testing.T, ctx context.Context, r *PlatformAgentReconciler, namespace, name, key, value string) {
	t.Helper()
	secret := &corev1.Secret{}
	if err := r.Get(ctx, types.NamespacedName{Name: name, Namespace: namespace}, secret); err != nil {
		t.Fatalf("read Secret %s back: %v", name, err)
	}
	secret.Data[key] = []byte(value)
	if err := r.Update(ctx, secret); err != nil {
		t.Fatalf("rotate %s/%s: %v", name, key, err)
	}
}

// TestRotatingASlackTokenRollsTheArmedGateway: on a Slack-armed next install
// the pair is read by the A2A gateway, not the broker (legacySlackConsumer is
// off), so the secret-env digest has to ride the gateway's pod template or a
// rotated token reaches no pod at all. A rotated bot token alone is the
// silent case: the socket stays up on the app token and every post fails.
func TestRotatingASlackTokenRollsTheArmedGateway(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	agent := slackTestAgent("next", true)
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	if err := cl.Create(ctx, secretHashTestSecret(slackTestSecret, map[string][]byte{
		slackTestBotKey: []byte("xoxb-before-rotation"),
		slackTestAppKey: []byte("xapp-unchanged"),
	})); err != nil {
		t.Fatal(err)
	}
	theCalloutIsServing(t, ctx, cl, r, agent)

	before := gatewayDigestAfterPass(t, ctx, r, agent)
	if before == "" {
		t.Fatalf("the Slack-armed gateway carries no %s; a rotated Slack token would reach nothing", secretEnvHashAnnotation)
	}
	if idle := gatewayDigestAfterPass(t, ctx, r, agent); idle != before {
		t.Fatalf("the digest moved on an idle pass (%s then %s): every pass would roll the gateway", before, idle)
	}
	rotateSecretKey(t, ctx, r, agent.Namespace, slackTestSecret, slackTestBotKey, "xoxb-after-rotation")
	if after := gatewayDigestAfterPass(t, ctx, r, agent); after == before {
		t.Errorf("the gateway's pod template is unchanged after the bot token rotated (%s), so the running pod keeps the revoked token", before)
	}
}

// TestRotatingTheDiscordTokenRollsTheGatewayToo: the stamp covers the
// gateway's Secret-sourced env generally, not Slack's pair alone, so the
// Discord gateway (never stamped before) is digested on the same terms, and
// it does not move on an idle pass.
func TestRotatingTheDiscordTokenRollsTheGatewayToo(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	agent := a2aTestAgent()
	r, cl, _ := a2aGateTestReconciler(t, agent)
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)

	before := gatewayDigestAfterPass(t, ctx, r, agent)
	if before == "" {
		t.Fatalf("the Discord gateway carries no %s", secretEnvHashAnnotation)
	}
	if idle := gatewayDigestAfterPass(t, ctx, r, agent); idle != before {
		t.Fatalf("the digest moved on an idle pass (%s then %s)", before, idle)
	}
	rotateSecretKey(t, ctx, r, agent.Namespace, a2aDiscordBotSecretName, a2aDiscordBotTokenKey, "rotated-token")
	if after := gatewayDigestAfterPass(t, ctx, r, agent); after == before {
		t.Errorf("the gateway's pod template is unchanged after the Discord token rotated (%s)", before)
	}
}

// TestTheStampedSlackGatewayKeepsItsMetricsListenerAndFence: #2401 stamps the
// secret-env digest onto the A2A gateway's pod template and #2473 gives the
// same template a metrics-only port and the pod its own fence. They landed on
// separate branches; this holds them together on one applied Deployment, so a
// later edit to either cannot drop the other's half without a red.
func TestTheStampedSlackGatewayKeepsItsMetricsListenerAndFence(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aAgentDoorEnvVar, "")
	agent := slackTestAgent("next", true)
	r, cl, _ := a2aGateTestReconcilerWithoutABackend(t, agent)
	ctx := context.Background()
	if err := cl.Create(ctx, secretHashTestSecret(slackTestSecret, map[string][]byte{
		slackTestBotKey: []byte("xoxb-token"),
		slackTestAppKey: []byte("xapp-token"),
	})); err != nil {
		t.Fatal(err)
	}
	theCalloutIsServing(t, ctx, cl, r, agent)

	if digest := gatewayDigestAfterPass(t, ctx, r, agent); digest == "" {
		t.Fatalf("the Slack-armed gateway carries no %s", secretEnvHashAnnotation)
	}
	dep := &appsv1.Deployment{}
	if err := r.Get(ctx, types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatal(err)
	}
	c := dep.Spec.Template.Spec.Containers[0]
	var port bool
	for _, p := range c.Ports {
		if p.Name == a2aGatewayMetricsPortName && p.ContainerPort == a2aGatewayMetricsPort {
			port = true
		}
	}
	if !port {
		t.Errorf("the stamped gateway declares no %s port %d: %+v", a2aGatewayMetricsPortName, a2aGatewayMetricsPort, c.Ports)
	}
	if got := envMapOf(c.Env)[a2aGatewayMetricsPortEnvVar].Value; got != strconv.Itoa(int(a2aGatewayMetricsPort)) {
		t.Errorf("%s = %q on the stamped gateway, want %d", a2aGatewayMetricsPortEnvVar, got, a2aGatewayMetricsPort)
	}
	for _, name := range []string{a2aSlackBotTokenEnvVar, a2aSlackAppTokenEnvVar} {
		if _, ok := envMapOf(c.Env)[name]; !ok {
			t.Errorf("%s is missing from the gateway the metrics listener rides", name)
		}
	}
	fence := &networkingv1.NetworkPolicy{}
	if err := r.Get(ctx, types.NamespacedName{Name: a2aGatewayNetpolName(agent), Namespace: agent.Namespace}, fence); err != nil {
		t.Fatalf("the stamped gateway has no fence of its own: %v", err)
	}
	assertA2AGatewayFenceAdmitsOnlyTheCollector(t, fence)
}
