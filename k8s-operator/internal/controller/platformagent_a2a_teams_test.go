package controller

import (
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	teamsTestSecret   = "teams-creds"
	teamsTestTenant   = "759d68f0-65ed-44cd-8487-ce6221b72b2a"
	teamsTestUser     = "05e62547-5ee7-41be-9639-155d9212be8f"
	teamsTestHome     = "19:11fb65f1e6c941b382a410dd0e383724@thread.tacv2"
	teamsTestAppIDKey = "TEAMS_APP_ID"
	teamsTestPassKey  = "TEAMS_APP_PASSWORD"
)

// teamsTestAgent is a2aTestAgent with spec.integration.teams configured the
// way the CRD requires once it is enabled.
func teamsTestAgent(enabled bool) *agentv1alpha1.PlatformAgent {
	agent := a2aTestAgent()
	agent.Spec.Integration = &agentv1alpha1.PlatformAgentIntegrationSpec{
		Teams: &agentv1alpha1.TeamsSpec{
			Enabled: ptr.To(enabled),
			AppIdSecretRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: teamsTestSecret}, Key: teamsTestAppIDKey,
			},
			AppPasswordSecretRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: teamsTestSecret}, Key: teamsTestPassKey,
			},
			TenantId:     teamsTestTenant,
			AllowedUsers: []string{teamsTestUser},
			HomeChannel:  teamsTestHome,
		},
	}
	return agent
}

// TestTeamsArmsOnTheOperatorSwitch: the gateway backend arms only when the
// operator runs with A2A_TEAMS_BACKEND=true and the CR enables Teams; the
// CR alone (every stored object is on next) keeps the Hermes consumer, so an
// operator upgrade moves no Teams install by itself.
func TestTeamsArmsOnTheOperatorSwitch(t *testing.T) {
	for _, tc := range []struct {
		name     string
		flag     string
		agent    *agentv1alpha1.PlatformAgent
		armed    bool
		onHermes bool
	}{
		{"switch on, teams enabled", "true", teamsTestAgent(true), true, false},
		{"switch unset, teams enabled", "", teamsTestAgent(true), false, true},
		{"switch misspelt, teams enabled", "yes", teamsTestAgent(true), false, true},
		{"switch on, teams disabled", "true", teamsTestAgent(false), false, false},
		{"switch on, no integration", "true", a2aTestAgent(), false, false},
		{"switch on, slack only", "true", slackTestAgent("next", true), false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(a2aTeamsBackendEnvVar, tc.flag)
			if got := a2aTeamsArmed(tc.agent); got != tc.armed {
				t.Errorf("a2aTeamsArmed = %v, want %v", got, tc.armed)
			}
			if got := teamsOnHermes(tc.agent); got != tc.onHermes {
				t.Errorf("teamsOnHermes = %v, want %v", got, tc.onHermes)
			}
			// The people reading is open unless a Teams is served through
			// Hermes, where the gateway would count nobody writing.
			if got := peopleReadingEnabled(tc.agent); got != !tc.onHermes {
				t.Errorf("peopleReadingEnabled = %v, want %v", got, !tc.onHermes)
			}
		})
	}
}

// TestAnArmedGatewayCarriesTheTeamsBackend: armed, the gateway carries the
// Teams relay URL (the broker), the CR's gates as the CRD defines them, the
// tenant and the home channel, the projected relay token, and no Discord
// reference.
func TestAnArmedGatewayCarriesTheTeamsBackend(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aTeamsBackendEnvVar, "true")
	agent := teamsTestAgent(true)
	dep := buildA2AGatewayDeployment(agent)
	container := dep.Spec.Template.Spec.Containers[0]
	env := envMapOf(container.Env)
	for name, want := range map[string]string{
		a2aTeamsRelayURLEnvVar:      credentialProxyBaseURL(agent),
		a2aTeamsTokenPathEnvVar:     a2aGchatTokenPath,
		a2aTeamsAllowedUsersEnvVar:  teamsTestUser,
		a2aTeamsAllowAllUsersEnvVar: "false",
		a2aTeamsTenantIDEnvVar:      teamsTestTenant,
		a2aTeamsHomeChannelEnvVar:   teamsTestHome,
	} {
		if got := env[name].Value; got != want {
			t.Errorf("%s = %q, want %q", name, got, want)
		}
	}
	if _, ok := env[a2aTeamsAdaptiveCardsEnvVar]; ok {
		t.Error("the card switch is rendered although the CR leaves it on")
	}
	if _, ok := env["DISCORD_TOKEN"]; ok {
		t.Error("DISCORD_TOKEN is rendered beside the Teams backend")
	}
	mounted := false
	for _, m := range container.VolumeMounts {
		if m.Name == a2aGchatTokenVolume && m.MountPath == a2aGchatTokenDir {
			mounted = true
		}
	}
	if !mounted {
		t.Error("the relay token is not mounted on a Teams-only gateway")
	}
	projected := false
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name == a2aGchatTokenVolume && v.Projected != nil {
			projected = true
		}
	}
	if !projected {
		t.Error("the relay token volume is not projected on a Teams-only gateway")
	}
	// The gates follow the CRD: an empty list with the switch off admits
	// nobody; the switch on admits everyone; the card switch renders only
	// when off.
	agent.Spec.Integration.Teams.AllowedUsers = nil
	agent.Spec.Integration.Teams.AdaptiveCards = ptr.To(false)
	env = envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
	if env[a2aTeamsAllowAllUsersEnvVar].Value != "false" || env[a2aTeamsAllowedUsersEnvVar].Value != "" {
		t.Errorf("empty list, switch off: allow-all %q, list %q", env[a2aTeamsAllowAllUsersEnvVar].Value, env[a2aTeamsAllowedUsersEnvVar].Value)
	}
	if env[a2aTeamsAdaptiveCardsEnvVar].Value != "false" {
		t.Errorf("card switch %q, want false", env[a2aTeamsAdaptiveCardsEnvVar].Value)
	}
	agent.Spec.Integration.Teams.AllowAllUsers = ptr.To(true)
	agent.Spec.Integration.Teams.AllowedUsers = []string{teamsTestUser}
	env = envMapOf(buildA2AGatewayDeployment(agent).Spec.Template.Spec.Containers[0].Env)
	if env[a2aTeamsAllowAllUsersEnvVar].Value != "true" {
		t.Errorf("switch on with a list: allow-all %q, want true", env[a2aTeamsAllowAllUsersEnvVar].Value)
	}
}

// TestATeamsOnlyGatewayIsABrokerCaller: with Teams armed and Google Chat
// off, the broker still admits the gateway: its caller list, its ingress
// NetworkPolicy and the a2a-chat audience it confers the role by.
func TestATeamsOnlyGatewayIsABrokerCaller(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aTeamsBackendEnvVar, "true")
	agent := teamsTestAgent(true)
	gatewaySA := "system:serviceaccount:" + agent.Namespace + ":" + a2aGatewayName(agent)
	if callers := allowedBrokerCallers(agent, brokerCallerOwnership{}); !strings.Contains(callers, gatewaySA) {
		t.Errorf("broker callers %q lack the A2A gateway", callers)
	}
	admitted := false
	for _, rule := range buildCredentialProxyNetworkPolicy(agent).Spec.Ingress {
		for _, peer := range rule.From {
			if peer.PodSelector != nil && peer.PodSelector.MatchLabels["app"] == a2aGatewayName(agent) {
				admitted = true
			}
		}
	}
	if !admitted {
		t.Error("the broker's NetworkPolicy does not admit the A2A gateway pod")
	}
	broker := brokerEnvOf(agent)
	if broker[credentialProxyA2AChatAudienceEnvVar].Value != credentialProxyA2AChatAudience {
		t.Errorf("the broker is not told the a2a-chat audience: %+v", broker[credentialProxyA2AChatAudienceEnvVar])
	}
	// Switch off: none of the three widens.
	t.Setenv(a2aTeamsBackendEnvVar, "")
	if callers := allowedBrokerCallers(agent, brokerCallerOwnership{}); strings.Contains(callers, gatewaySA) {
		t.Errorf("broker callers %q admit the gateway while Teams is on Hermes", callers)
	}
	broker = brokerEnvOf(agent)
	if _, ok := broker[credentialProxyA2AChatAudienceEnvVar]; ok {
		t.Error("the a2a-chat audience is rendered while Teams is on Hermes")
	}
}

// brokerEnvOf is the credential-proxy container's env for agent, keyed by
// name, as the broker Deployment renders it.
func brokerEnvOf(agent *agentv1alpha1.PlatformAgent) map[string]corev1.EnvVar {
	return envMapOf(buildCredentialProxyEnvForPlugins(agent, nil, brokerCallerOwnership{}, credentialProxyProfileConfig{}))
}

// TestChatAndTeamsShareOneRelayToken: with both relay-backed backends armed
// the gateway mounts the token once and carries both relay URLs.
func TestChatAndTeamsShareOneRelayToken(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	t.Setenv(a2aTeamsBackendEnvVar, "true")
	agent := gchatTestAgent("next", true)
	agent.Spec.Integration.Teams = teamsTestAgent(true).Spec.Integration.Teams
	dep := buildA2AGatewayDeployment(agent)
	count := 0
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Name == a2aGchatTokenVolume {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("relay token volume rendered %d times", count)
	}
	env := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)
	if env[a2aGchatRelayURLEnvVar].Value == "" || env[a2aTeamsRelayURLEnvVar].Value == "" {
		t.Error("both relay URLs should be rendered")
	}
}

// TestAnUnarmedGatewayCarriesNoTeamsEnv: with the switch unset, or Teams
// disabled, the gateway carries no Teams env and keeps the Discord reference.
func TestAnUnarmedGatewayCarriesNoTeamsEnv(t *testing.T) {
	t.Setenv(a2aInjectBackendEnvVar, "")
	for name, tc := range map[string]struct {
		flag  string
		agent *agentv1alpha1.PlatformAgent
	}{
		"switch unset": {"", teamsTestAgent(true)},
		"disabled":     {"true", teamsTestAgent(false)},
	} {
		t.Run(name, func(t *testing.T) {
			t.Setenv(a2aTeamsBackendEnvVar, tc.flag)
			env := envMapOf(buildA2AGatewayDeployment(tc.agent).Spec.Template.Spec.Containers[0].Env)
			for n := range env {
				if strings.HasPrefix(n, "A2A_TEAMS_") {
					t.Errorf("%s is rendered on a gateway Teams does not arm", n)
				}
			}
			if e, ok := env["DISCORD_TOKEN"]; !ok || e.ValueFrom.SecretKeyRef.Name != a2aDiscordBotSecretName {
				t.Errorf("DISCORD_TOKEN = %+v, want the optional discord-bot reference", e)
			}
		})
	}
}

// TestTheHermesTeamsEnvFollowsTheSwitch: the Hermes gateway carries the
// Teams platform env while Teams is on Hermes and none once the gateway
// backend is armed, where the A2A gateway is the one consumer of the
// broker's queue; the broker keeps the app credential either way.
func TestTheHermesTeamsEnvFollowsTheSwitch(t *testing.T) {
	for flag, onHermes := range map[string]bool{"": true, "true": false} {
		t.Setenv(a2aTeamsBackendEnvVar, flag)
		agent := teamsTestAgent(true)
		dep := buildDeployment(agent, "h1", "h2", "h3", "h4", nil, renderOptions{imageVolumeSupported: true})
		env := envMapOf(dep.Spec.Template.Spec.Containers[0].Env)
		_, relay := env["TEAMS_RELAY_URL"]
		if relay != onHermes {
			t.Errorf("switch %q: TEAMS_RELAY_URL on the Hermes gateway = %v, want %v", flag, relay, onHermes)
		}
		for name := range env {
			if !onHermes && strings.HasPrefix(name, "TEAMS_") {
				t.Errorf("switch %q: %s rendered on the Hermes gateway", flag, name)
			}
		}
		broker := brokerEnvOf(agent)
		if _, ok := broker["TEAMS_APP_ID"]; !ok {
			t.Errorf("switch %q: the broker lost its TEAMS_APP_ID", flag)
		}
	}
}

// TestTeamsJoinsTheNotifyPlatforms: armed, Teams is a conversation route the
// agent side may report a card back to, and a home platform when the CR
// names a home channel.
func TestTeamsJoinsTheNotifyPlatforms(t *testing.T) {
	t.Setenv(a2aTeamsBackendEnvVar, "true")
	agent := teamsTestAgent(true)
	if got := a2aNotifyPlatforms(agent); strings.Join(got, ",") != a2aNotifyPlatformTeams {
		t.Errorf("notify platforms %v, want teams alone", got)
	}
	if got := a2aNotifyHomePlatforms(agent); strings.Join(got, ",") != a2aNotifyPlatformTeams {
		t.Errorf("home platforms %v, want teams alone", got)
	}
	agent.Spec.Integration.Teams.HomeChannel = " "
	if got := a2aNotifyHomePlatforms(agent); len(got) != 0 {
		t.Errorf("home platforms %v with a blank home channel", got)
	}
	if got := a2aNotifyPlatforms(agent); strings.Join(got, ",") != a2aNotifyPlatformTeams {
		t.Errorf("notify platforms %v with a blank home channel, want teams (conversation posts)", got)
	}
	// A home the gateway refuses (NewTeamsNotifier: whitespace inside, or a
	// URL) drops the whole route there, so the agent is told of neither.
	for _, malformed := range []string{"19:a b@thread.tacv2", "https://teams.microsoft.com/l/channel/x", "spaces/AAA", "C0123ABCD"} {
		agent.Spec.Integration.Teams.HomeChannel = malformed
		if got := a2aNotifyPlatforms(agent); len(got) != 0 {
			t.Errorf("notify platforms %v with home %q, which the gateway refuses", got, malformed)
		}
		if got := a2aNotifyHomePlatforms(agent); len(got) != 0 {
			t.Errorf("home platforms %v with home %q", got, malformed)
		}
	}
	for _, good := range []string{teamsTestHome, teamsTestHome + ";messageid=1794603936403", "19:group@thread.v2", "a:1UxxASmI_WSV5PQj3K0Nd3K292l62BfKLXmPpH6cQlZN7Ogxu7L_EoFVo9EhRcJ4ybzI"} {
		agent.Spec.Integration.Teams.HomeChannel = good
		if got := a2aNotifyHomePlatforms(agent); strings.Join(got, ",") != a2aNotifyPlatformTeams {
			t.Errorf("home platforms %v with home %q, which the gateway takes", got, good)
		}
	}
	agent.Spec.Integration.Teams.HomeChannel = teamsTestHome
	t.Setenv(a2aTeamsBackendEnvVar, "")
	if got := a2aNotifyPlatforms(agent); len(got) != 0 {
		t.Errorf("notify platforms %v while Teams is on Hermes", got)
	}
}

// TestTheHermesTeamsPlatformFollowsTheSwitch: the Planning Agent's config.yaml
// enables the Hermes teams platform exactly when the gateway backend is not
// armed; armed, it stays off, so one consumer pulls the broker's queue
// (the env half is TestTheHermesTeamsEnvFollowsTheSwitch).
func TestTheHermesTeamsPlatformFollowsTheSwitch(t *testing.T) {
	for _, tc := range []struct {
		name     string
		flag     string
		onHermes bool
	}{
		{"switch on", "true", false},
		{"switch unset", "", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(a2aTeamsBackendEnvVar, tc.flag)
			config := renderConfigYAML(teamsTestAgent(true), nil)
			if enabled := strings.Contains(config, "teams:\n    enabled: true"); enabled != tc.onHermes {
				t.Errorf("platforms.teams.enabled rendered %v, want %v; config:\n%s", enabled, tc.onHermes, config)
			}
		})
	}
}

// TestAnArmedTeamsBackendIsAChatPlatform: NoChatPlatform says nothing the
// agent raises unprompted reaches chat; with the gateway's Teams backend armed
// the delivery resolver names Teams, so the condition is absent. On the
// Hermes path it stands as before.
func TestAnArmedTeamsBackendIsAChatPlatform(t *testing.T) {
	t.Setenv(a2aTeamsBackendEnvVar, "true")
	if msg := noChatPlatformMsg(teamsTestAgent(true)); msg != "" {
		t.Errorf("NoChatPlatform set with the Teams backend armed: %q", msg)
	}
	t.Setenv(a2aTeamsBackendEnvVar, "")
	if msg := noChatPlatformMsg(teamsTestAgent(true)); msg == "" {
		t.Error("NoChatPlatform absent while Teams is on Hermes and no other platform is enabled")
	}
}

// TestTheBrokerLearnsTheTeamsConsumerFromTheOperator: the broker admits the
// a2a-chat role on its Teams routes only while CREDENTIAL_PROXY_TEAMS_A2A_CALLER
// reads true, which the operator renders exactly when the gateway's Teams
// backend is armed; a Chat-only gateway, or Teams on Hermes, gets no such word.
func TestTheBrokerLearnsTheTeamsConsumerFromTheOperator(t *testing.T) {
	t.Setenv(a2aTeamsBackendEnvVar, "true")
	if got := brokerEnvOf(teamsTestAgent(true))[credentialProxyTeamsA2ACallerEnvVar].Value; got != "true" {
		t.Errorf("%s = %q with the backend armed, want true", credentialProxyTeamsA2ACallerEnvVar, got)
	}
	for name, agent := range map[string]*agentv1alpha1.PlatformAgent{
		"teams disabled": teamsTestAgent(false),
		"chat only":      gchatTestAgent("next", true),
	} {
		if _, ok := brokerEnvOf(agent)[credentialProxyTeamsA2ACallerEnvVar]; ok {
			t.Errorf("%s: %s rendered", name, credentialProxyTeamsA2ACallerEnvVar)
		}
	}
	t.Setenv(a2aTeamsBackendEnvVar, "")
	if _, ok := brokerEnvOf(teamsTestAgent(true))[credentialProxyTeamsA2ACallerEnvVar]; ok {
		t.Errorf("%s rendered while Teams is on Hermes", credentialProxyTeamsA2ACallerEnvVar)
	}
}
