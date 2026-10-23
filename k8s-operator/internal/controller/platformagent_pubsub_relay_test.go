package controller

import (
	"fmt"
	"strings"
	"testing"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// agentInProject is the test agent with its harness project set; the fixture
// may carry no harness at all.
func agentInProject(project string) *agentv1alpha1.PlatformAgent {
	agent := newTestPlatformAgent()
	if agent.Spec.Harness == nil {
		agent.Spec.Harness = &agentv1alpha1.HarnessSpec{}
	}
	agent.Spec.Harness.ProjectID = project
	return agent
}

func pubsubPlugin(name, config string) *agentv1alpha1.AgentPlugin {
	plugin := &agentv1alpha1.AgentPlugin{}
	plugin.Name = name
	plugin.Spec.AgentRef = "test-agent"
	plugin.Spec.Image = "example.com/" + name + ":v1"
	plugin.Spec.Config = config
	return plugin
}

const stockoutPluginConfig = `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        gke_stockout_alerts:
          subscription: gke-stockout-alerts-sub
          topic: gke-stockout-alerts-topic
          dispatch: kanban
`

func TestPluginPubsubSubscriptionsQualifiesShortNamesWithTheHarnessProject(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	got := pluginPubsubSubscriptions(agent, []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig)})
	want := []string{"projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestPluginPubsubSubscriptionsFollowsTheAdaptersRules(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{
		pubsubPlugin("listener", "platforms:\n  pubsub:\n    enabled: true\n"),
		pubsubPlugin("fullpath", `
platforms:
  pubsub:
    extra:
      subscriptions:
        elsewhere:
          subscription: projects/other-project/subscriptions/their-sub
        topic-only:
          topic: alerts
        topic-path-only:
          topic: projects/other-project/topics/deep
        nothing:
          prompt: "no subscription and no topic"
        same-again:
          subscription: gke-stockout-alerts-sub
`),
		pubsubPlugin("stockout", stockoutPluginConfig),
		pubsubPlugin("broken", "platforms: [not, a, map"),
		pubsubPlugin("empty", "   "),
	}
	got := pluginPubsubSubscriptions(agent, plugins)
	want := []string{
		"projects/my-gcp-project/subscriptions/alerts-sub",
		"projects/my-gcp-project/subscriptions/deep-sub",
		"projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub",
		"projects/other-project/subscriptions/their-sub",
	}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestPluginPubsubSubscriptionsLeavesOutWhatTheBrokerWouldNotAdmit(t *testing.T) {
	// A typo in one route must not reach the broker's variable: an uppercase
	// project, a space in a topic, a two-letter subscription. The well formed
	// route beside them still renders.
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("typos", `
platforms:
  pubsub:
    extra:
      subscriptions:
        upper:
          subscription: projects/My-Project/subscriptions/alerts
        spaced:
          topic: "gke alerts"
        short:
          subscription: ab
        good:
          subscription: gke-stockout-alerts-sub
`)}
	got := pluginPubsubSubscriptions(agent, plugins)
	if len(got) != 1 || got[0] != "projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub" {
		t.Fatalf("got %v, want only the well formed route", got)
	}
}

func TestPluginPubsubSubscriptionsSeesOnlyThePluginsTheGatewayLoads(t *testing.T) {
	// A hyphenated name fails the render's plugin-name rule: the plugin is set
	// Degraded and kept out of config.yaml and the pod env. Its routes must not
	// reach the broker either.
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout-investigator", stockoutPluginConfig)}
	if got := pluginPubsubSubscriptions(agent, plugins); len(got) != 0 {
		t.Fatalf("a plugin the render refuses reached the broker's set: %v", got)
	}
	if url := pubsubRelayURL(agent, plugins); url != "" {
		t.Fatalf("the relay URL %q would be pinned for a plugin the gateway never loads", url)
	}
}

func TestPluginPubsubSubscriptionsWithoutAProjectKeepsOnlyFullPaths(t *testing.T) {
	agent := agentInProject("")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig), pubsubPlugin("full", `
platforms:
  pubsub:
    extra:
      subscriptions:
        r:
          subscription: projects/other-project/subscriptions/their-sub
`)}
	got := pluginPubsubSubscriptions(agent, plugins)
	if len(got) != 1 || got[0] != "projects/other-project/subscriptions/their-sub" {
		t.Fatalf("got %v, want the one full path", got)
	}
	if pluginPubsubSubscriptions(agent, nil) != nil && len(pluginPubsubSubscriptions(agent, nil)) != 0 {
		t.Fatalf("no plugins must mean no subscriptions")
	}
}

func TestTheBrokerLearnsTheSubscriptionSetAndTheGatewayTheRelayURL(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig)}

	brokerEnv := buildCredentialProxyEnvForPlugins(agent, plugins)
	value, count := envValueCount(brokerEnv, platformPubsubSubscriptionsEnvVar)
	if count != 1 || value != "projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub" {
		t.Fatalf("broker env %s: value %q count %d", platformPubsubSubscriptionsEnvVar, value, count)
	}
	if _, count := envValueCount(buildCredentialProxyEnv(agent), platformPubsubSubscriptionsEnvVar); count != 0 {
		t.Fatalf("with no plugins the broker must not carry %s", platformPubsubSubscriptionsEnvVar)
	}

	deployment := buildCredentialProxyDeploymentForPlugins(agent, "hash", plugins)
	if _, count := envValueCount(deployment.Spec.Template.Spec.Containers[0].Env, platformPubsubSubscriptionsEnvVar); count != 1 {
		t.Fatalf("the broker Deployment render must carry the set through to the container")
	}

	gateway := buildDeployment(agent, "cfg", "fb", "settings", "policy", plugins, renderOptions{imageVolumeSupported: true})
	agentContainer, found := findContainer(gateway.Spec.Template.Spec, "platform-agent")
	if !found {
		t.Fatalf("no platform-agent container")
	}
	wantURL := credentialProxyBaseURL(agent)
	if value, count := envValueCount(agentContainer.Env, pubsubRelayURLEnvVar); count != 1 || value != wantURL {
		t.Fatalf("gateway env %s: value %q count %d, want %q once", pubsubRelayURLEnvVar, value, count, wantURL)
	}
	// Empty rather than absent without a Pub/Sub platform: the adapter reads
	// an empty value as "no relay", and a name the operator always emits is one
	// a plugin's spec.env cannot supply.
	bare := buildDeployment(agent, "cfg", "fb", "settings", "policy", nil, renderOptions{imageVolumeSupported: true})
	bareContainer, _ := findContainer(bare.Spec.Template.Spec, "platform-agent")
	if value, count := envValueCount(bareContainer.Env, pubsubRelayURLEnvVar); count != 1 || value != "" {
		t.Fatalf("with no Pub/Sub platform the gateway must carry %s empty once, got %q count %d", pubsubRelayURLEnvVar, value, count)
	}
}

func TestAPluginCannotSupplyTheRelayURLTheOperatorDidNotRender(t *testing.T) {
	// A route outside the grammar renders no relay; a plugin that names the URL
	// in its own spec.env must not become the writer the adapter trusts.
	agent := agentInProject("my-gcp-project")
	plugin := pubsubPlugin("stockout", `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        gke_stockout_alerts:
          subscription: projects/my-gcp-project/subscriptions/ab
`)
	plugin.Spec.Env = []corev1.EnvVar{
		{Name: pubsubRelayURLEnvVar, Value: "http://attacker.example"},
		{Name: pubsubRelayProjectEnvVar, Value: "elsewhere"},
		{Name: "GCP_PROJECT_ID", Value: "elsewhere"},
	}
	plugins := []*agentv1alpha1.AgentPlugin{plugin}
	if url := pubsubRelayURL(agent, plugins); url != "" {
		t.Fatalf("a route outside the grammar rendered a relay URL %q", url)
	}
	gateway := buildDeployment(agent, "cfg", "fb", "settings", "policy", plugins, renderOptions{imageVolumeSupported: true})
	agentContainer, _ := findContainer(gateway.Spec.Template.Spec, "platform-agent")
	if value, count := envValueCount(agentContainer.Env, pubsubRelayURLEnvVar); count != 1 || value != "" {
		t.Fatalf("the plugin's %s reached the gateway: value %q count %d", pubsubRelayURLEnvVar, value, count)
	}
	// The project the adapter qualifies a short name with is the operator's,
	// whatever a plugin says in its own env, GCP_PROJECT_ID included.
	if value, count := envValueCount(agentContainer.Env, pubsubRelayProjectEnvVar); count != 1 || value != "my-gcp-project" {
		t.Fatalf("%s on the gateway: value %q count %d, want the harness project once", pubsubRelayProjectEnvVar, value, count)
	}
	managed := buildConfigMapData(agent, plugins)[managedEnvKey]
	if !strings.Contains(managed, "\n"+pubsubRelayURLEnvVar+"=\n") {
		t.Fatalf("%s is not pinned empty in the managed .env:\n%s", pubsubRelayURLEnvVar, managed)
	}
	if !strings.Contains(managed, "\n"+pubsubRelayProjectEnvVar+"=my-gcp-project\n") {
		t.Fatalf("%s is not pinned to the harness project in the managed .env:\n%s", pubsubRelayProjectEnvVar, managed)
	}
}

func TestTheRouteIssuesStayInsideTheConditionMessageCap(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	long := "projects/my-gcp-project/subscriptions/" + strings.Repeat("x", 40000)
	var routes strings.Builder
	for i := 0; i < 30; i++ {
		fmt.Fprintf(&routes, "        route%02d:\n          subscription: %s\n", i, long)
	}
	plugin := pubsubPlugin("stockout", "platforms:\n  pubsub:\n    enabled: true\n    extra:\n      subscriptions:\n"+routes.String())
	issues := pubsubRouteIssues(agent, plugin)
	if len(issues) != pubsubRouteIssueBudget+1 {
		t.Fatalf("want %d route lines and one overflow line, got %d", pubsubRouteIssueBudget, len(issues))
	}
	joined := strings.Join(issues, " ")
	if len(joined) > 8192 {
		t.Fatalf("the joined issues are %d bytes; the CRD caps a condition message at 32768 and the budget is meant to stay far under it", len(joined))
	}
	if !strings.Contains(issues[0], pubsubRouteIssueEllipsis) {
		t.Fatalf("an over-long path is not cut: %s", issues[0])
	}
	if !strings.Contains(issues[len(issues)-1], "22 more") {
		t.Fatalf("the overflow line does not count the rest: %s", issues[len(issues)-1])
	}
	// The route name is the other author-written operand; a 33 KB key alone
	// would otherwise carry the message past the cap.
	named := pubsubPlugin("stockout", "platforms:\n  pubsub:\n    enabled: true\n    extra:\n      subscriptions:\n        "+
		strings.Repeat("n", 33000)+":\n          subscription: projects/p/subscriptions/-x\n        "+
		strings.Repeat("m", 33000)+":\n          subscription: short\n")
	for _, issue := range pubsubRouteIssues(agentInProject(""), named) {
		if len(issue) > 2*pubsubRouteIssuePathBudget+len(pubsubRouteGrammarIssueFormat)+2*len(pubsubRouteIssueEllipsis) {
			t.Fatalf("a route name is not cut: %d bytes", len(issue))
		}
		if !strings.Contains(issue, pubsubRouteIssueEllipsis) {
			t.Fatalf("an over-long route name is not marked as cut: %.120s", issue)
		}
	}
}

func TestTheManagedEnvPinsTheRelayURLWhenAPluginConfiguresPubsub(t *testing.T) {
	agent := agentInProject("my-gcp-project")
	plugins := []*agentv1alpha1.AgentPlugin{pubsubPlugin("stockout", stockoutPluginConfig)}
	data := buildConfigMapData(agent, plugins)
	if !strings.Contains(data[managedEnvKey], pubsubRelayURLEnvVar+"=") {
		t.Fatalf("%s is not pinned in the managed .env, so the agent could write its own:\n%s", pubsubRelayURLEnvVar, data[managedEnvKey])
	}
	if !strings.Contains(data[managedEnvKey], pubsubRelayURLEnvVar+"="+credentialProxyBaseURL(agent)+"\n") {
		t.Fatalf("%s pinned to something other than the broker:\n%s", pubsubRelayURLEnvVar, data[managedEnvKey])
	}
	if !strings.Contains(buildConfigMapData(agent, nil)[managedEnvKey], "\n"+pubsubRelayURLEnvVar+"=\n") {
		t.Fatalf("%s must be pinned empty with no Pub/Sub platform configured", pubsubRelayURLEnvVar)
	}
}

func TestARefusedRouteIsReportedOnThePluginsStatus(t *testing.T) {
	// The adapter sees an empty relay URL either way; the plugin's status is
	// where the reason has to be.
	withoutProject := agentInProject("")
	short := pubsubPlugin("stockout", stockoutPluginConfig)
	issues := pubsubRouteIssues(withoutProject, short)
	if len(issues) != 1 || !strings.Contains(issues[0], "spec.harness.projectId is empty") {
		t.Fatalf("a short route on an agent with no project: %v", issues)
	}
	if issues := pubsubRouteIssues(agentInProject("my-gcp-project"), short); len(issues) != 0 {
		t.Fatalf("a short route on an agent with a project is fine, got %v", issues)
	}
	outside := pubsubPlugin("stockout", `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        gke_stockout_alerts:
          subscription: projects/my-gcp-project/subscriptions/ab
`)
	issues = pubsubRouteIssues(agentInProject("my-gcp-project"), outside)
	if len(issues) != 1 || !strings.Contains(issues[0], "not a subscription path the broker relays") {
		t.Fatalf("a route outside the grammar: %v", issues)
	}
	empty := pubsubPlugin("stockout", `
platforms:
  pubsub:
    enabled: true
    extra:
      subscriptions:
        gke_stockout_alerts:
          filter: "true"
`)
	if issues := pubsubRouteIssues(withoutProject, empty); len(issues) != 0 {
		t.Fatalf("a route naming nothing is the adapter's own error to report, got %v", issues)
	}
	if issues := pubsubRouteIssues(withoutProject, nil); issues != nil {
		t.Fatalf("nil plugin: %v", issues)
	}
}

func TestAPluginRouteCannotNameTheDriftDetectorsSubscription(t *testing.T) {
	// A second consumer on the detector's subscription would pull and ack
	// drift events the detector never sees; the set leaves it out and the
	// plugin's status says so.
	agent := agentInProject("my-gcp-project")
	drift := watcherPubsubSubscription(agent)
	plugin := pubsubPlugin("stockout", "platforms:\n  pubsub:\n    enabled: true\n    extra:\n      subscriptions:\n        drift:\n          subscription: "+drift+"\n        alerts:\n          subscription: gke-stockout-alerts-sub\n")
	got := pluginPubsubSubscriptions(agent, []*agentv1alpha1.AgentPlugin{plugin})
	if len(got) != 1 || got[0] != "projects/my-gcp-project/subscriptions/gke-stockout-alerts-sub" {
		t.Fatalf("the detector's subscription reached the broker's set: %v", got)
	}
	issues := pubsubRouteIssues(agent, plugin)
	if len(issues) != 1 || !strings.Contains(issues[0], "drift detector's subscription") {
		t.Fatalf("the refused route is not on the plugin's status: %v", issues)
	}
}

func TestTheReservedListNamesThePlatformPubsubSubscriptions(t *testing.T) {
	merged := mergeCredentialProxyEnv(nil, []corev1.EnvVar{
		{Name: platformPubsubSubscriptionsEnvVar, Value: "projects/p/subscriptions/theirs"},
		{Name: "HARMLESS_PLUGIN_SETTING", Value: "kept"},
	})
	if _, count := envValueCount(merged, platformPubsubSubscriptionsEnvVar); count != 0 {
		t.Fatalf("%s survived the merge from spec.deployment.env: a CR could point the relay at any subscription", platformPubsubSubscriptionsEnvVar)
	}
	if _, count := envValueCount(merged, "HARMLESS_PLUGIN_SETTING"); count != 1 {
		t.Fatalf("an unreserved name must still merge")
	}
}
