package controller

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The gateway's Pub/Sub platform adapter (agentplugins/pubsub-platform) pulls
// its subscriptions through the credential broker: the gateway Pod holds no
// Google credential, so the broker pulls on the install's identity, confined by
// its policy to the subscriptions rendered here. The operator is the one reader
// of both the AgentPlugin configs and the project, so it is the one writer of
// the set: the broker learns it from platformPubsubSubscriptionsEnvVar, and the
// gateway learns where the relay is from pubsubRelayURLEnvVar and which project
// qualifies a short route name from pubsubRelayProjectEnvVar, the same project
// this file composes the set with, so the two sides cannot name different
// subscriptions (GCP_PROJECT_ID sits on the overridable side of the plugin env
// merge; these two are appended after it and pinned). With no route
// the operator can qualify, the set is absent, the URL is rendered empty
// (pubsubRelayURL), and the broker refuses every relayed Pub/Sub call. A route
// it refuses is reported on the plugin's status (pubsubRouteIssues), since the
// adapter cannot tell a refused route from an install with no Pub/Sub platform.
const (
	pubsubRelayURLEnvVar              = "PUBSUB_RELAY_URL"
	pubsubRelayProjectEnvVar          = "PUBSUB_RELAY_PROJECT"
	platformPubsubSubscriptionsEnvVar = "PLATFORM_PUBSUB_SUBSCRIPTIONS"
	// The adapter's own rule for a route that names a topic and no
	// subscription: it subscribes to `<topic>-sub`, the name the installers
	// create (adapter.py, _parse_subscription_config).
	pubsubDerivedSubscriptionSuffix = "-sub"
	// The plugin-status lines for a route the broker's set will not carry, and
	// the bounds that keep them inside the CRD's condition-message cap: a path
	// and a route name are the author's text with no length bound, and a spec
	// can carry any number of routes (see hostPathDroppedEntryBudget for the
	// failure an unbounded status message buys). Every author-controlled
	// operand of a line goes through pubsubRouteIssueOperand.
	pubsubRouteIssuePathBudget      = 256
	pubsubRouteIssueBudget          = 8
	pubsubRouteIssueEllipsis        = "..."
	pubsubRouteIssueOverflowFormat  = "%d more Pub/Sub route(s) are left out of the broker's set for the same reasons."
	pubsubRouteNoProjectIssueFormat = "Pub/Sub route %s names a short subscription or topic and spec.harness.projectId is empty, so the broker relays nothing for it; name it as projects/<project>/subscriptions/<name> or set the project."
	pubsubRouteGrammarIssueFormat   = "Pub/Sub route %s composes to %q, which is not a subscription path the broker relays; it is left out of the broker's set."
	// The drift detector's own subscription is never a plugin's: a second
	// consumer would pull and ack drift events the detector then never sees,
	// and nothing would report it.
	pubsubRouteWatcherIssueFormat = "Pub/Sub route %s names the drift detector's subscription %q, which the platform relay does not serve; it is left out of the broker's set."
	pubsubSubscriptionPathFormat  = "projects/%s/subscriptions/%s"
	pubsubSubscriptionPathPrefix  = "projects/"
	pubsubSubscriptionsSeparator  = ","
)

// The shape the broker's policy matches a subscription on the wire by
// (agents/platform/scripts/watcher_policy.py: PUBSUB_PROJECT and SUBSCRIPTION).
// A route that does not render to this shape is left out of the set rather than
// handed to the broker: the broker drops such an entry too, and the route's
// pulls are then refused by the policy, which the adapter logs with the rule.
// Validating here as well keeps the rendered variable honest.
var pubsubSubscriptionPathPattern = regexp.MustCompile(
	`^projects/((?:[a-z0-9][a-z0-9.-]*[a-z0-9]:)?[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{1,20})/subscriptions/[A-Za-z][A-Za-z0-9._~+%-]{2,254}$`,
)

// pluginPubsubSubscriptions is the sorted, deduplicated set of fully qualified
// subscriptions the agent's plugins configure under
// `platforms.pubsub.extra.subscriptions.<route>`, qualified with the harness
// project the way the adapter qualifies them. A plugin whose config does not
// parse, or a route that names neither a subscription nor a topic, contributes
// nothing: the adapter logs and skips the same route, and the broker's refusal
// of an unlisted subscription is the loud signal if the two ever disagree.
func pluginPubsubSubscriptions(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) []string {
	// The same plugins the gateway loads: a plugin the render refuses (an
	// invalid name, a built-in collision) must not reach the broker's set
	// either, or the broker admits a subscription no adapter route exists for
	// and the gateway's env and its managed pin disagree.
	agentPlugins = filterValidAgentPlugins(agentPlugins)
	if len(agentPlugins) == 0 {
		return nil
	}
	project := pubsubRelayProject(agent)
	seen := map[string]struct{}{}
	for _, plugin := range agentPlugins {
		if plugin == nil || strings.TrimSpace(plugin.Spec.Config) == "" {
			continue
		}
		var config map[string]any
		if err := yaml.Unmarshal([]byte(plugin.Spec.Config), &config); err != nil {
			continue
		}
		for _, route := range pubsubRoutes(config) {
			path := pubsubSubscriptionPath(route, project)
			if pubsubSubscriptionPathPattern.MatchString(path) && path != watcherPubsubSubscription(agent) {
				seen[path] = struct{}{}
			}
		}
	}
	paths := make([]string, 0, len(seen))
	for path := range seen {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	return paths
}

// pubsubRouteIssues names each Pub/Sub route of one plugin the operator cannot
// render into the broker's set, for the plugin's status: the adapter then sees
// an empty relay URL and cannot say why. A route that names nothing is the
// adapter's own error to report; one that names a short subscription or topic
// on an agent with no spec.harness.projectId, or that composes to a path
// outside the subscription grammar, is the operator's.
func pubsubRouteIssues(agent *agentv1alpha1.PlatformAgent, plugin *agentv1alpha1.AgentPlugin) []string {
	if plugin == nil || strings.TrimSpace(plugin.Spec.Config) == "" {
		return nil
	}
	var config map[string]any
	if err := yaml.Unmarshal([]byte(plugin.Spec.Config), &config); err != nil {
		return nil
	}
	project := pubsubRelayProject(agent)
	var issues []string
	refused := 0
	for _, name := range pubsubRouteNames(config) {
		route, _ := pubsubRouteMap(config)[name].(map[string]any)
		if route == nil {
			continue
		}
		subscription, _ := route["subscription"].(string)
		topic, _ := route["topic"].(string)
		if strings.TrimSpace(subscription) == "" && strings.TrimSpace(topic) == "" {
			continue
		}
		path := pubsubSubscriptionPath(route, project)
		var issue string
		switch {
		case path == "":
			issue = fmt.Sprintf(pubsubRouteNoProjectIssueFormat, pubsubRouteIssueOperand(name))
		case !pubsubSubscriptionPathPattern.MatchString(path):
			issue = fmt.Sprintf(pubsubRouteGrammarIssueFormat, pubsubRouteIssueOperand(name), pubsubRouteIssueOperand(path))
		case path == watcherPubsubSubscription(agent):
			issue = fmt.Sprintf(pubsubRouteWatcherIssueFormat, pubsubRouteIssueOperand(name), path)
		default:
			continue
		}
		refused++
		if refused <= pubsubRouteIssueBudget {
			issues = append(issues, issue)
		}
	}
	if refused > pubsubRouteIssueBudget {
		issues = append(issues, fmt.Sprintf(pubsubRouteIssueOverflowFormat, refused-pubsubRouteIssueBudget))
	}
	return issues
}

// pubsubRouteIssueOperand is an author-written value cut to fit a status line.
func pubsubRouteIssueOperand(value string) string {
	if len(value) <= pubsubRouteIssuePathBudget {
		return value
	}
	return truncateToValidUTF8(value, pubsubRouteIssuePathBudget) + pubsubRouteIssueEllipsis
}

// pubsubRelayProject is the project a short route name is qualified with, on
// both sides: the set this file composes and the adapter, through
// pubsubRelayProjectEnvVar. Empty when the agent names no harness project.
func pubsubRelayProject(agent *agentv1alpha1.PlatformAgent) string {
	if agent == nil || agent.Spec.Harness == nil {
		return ""
	}
	return strings.TrimSpace(agent.Spec.Harness.ProjectID)
}

// pubsubRouteMap is the route map under platforms.pubsub.extra.subscriptions,
// or nil when the config carries no such map.
func pubsubRouteMap(config map[string]any) map[string]any {
	platforms, _ := config["platforms"].(map[string]any)
	pubsub, _ := platforms["pubsub"].(map[string]any)
	extra, _ := pubsub["extra"].(map[string]any)
	subscriptions, _ := extra["subscriptions"].(map[string]any)
	return subscriptions
}

// pubsubRouteNames is the route map's keys in order.
func pubsubRouteNames(config map[string]any) []string {
	subscriptions := pubsubRouteMap(config)
	names := make([]string, 0, len(subscriptions))
	for name := range subscriptions {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// pubsubRoutes is the route map under platforms.pubsub.extra.subscriptions, in
// route-name order, or nothing when the config carries no such map.
func pubsubRoutes(config map[string]any) []map[string]any {
	platforms, _ := config["platforms"].(map[string]any)
	pubsub, _ := platforms["pubsub"].(map[string]any)
	extra, _ := pubsub["extra"].(map[string]any)
	subscriptions, _ := extra["subscriptions"].(map[string]any)
	names := make([]string, 0, len(subscriptions))
	for name := range subscriptions {
		names = append(names, name)
	}
	sort.Strings(names)
	routes := make([]map[string]any, 0, len(names))
	for _, name := range names {
		if route, ok := subscriptions[name].(map[string]any); ok {
			routes = append(routes, route)
		}
	}
	return routes
}

// pubsubSubscriptionPath is the fully qualified subscription one route pulls,
// by the adapter's rule: a `subscription` that is already a path as it is; a
// short one under the project; a `topic` alone as `<topic>-sub` under the
// project. "" when the route names nothing, or names a short name with no
// project to qualify it by. The caller checks the shape; this only composes.
func pubsubSubscriptionPath(route map[string]any, project string) string {
	subscription, _ := route["subscription"].(string)
	subscription = strings.TrimSpace(subscription)
	if strings.HasPrefix(subscription, pubsubSubscriptionPathPrefix) {
		return subscription
	}
	if project == "" {
		return ""
	}
	if subscription != "" {
		return fmt.Sprintf(pubsubSubscriptionPathFormat, project, subscription)
	}
	topic, _ := route["topic"].(string)
	topic = strings.TrimSpace(topic)
	if topic == "" {
		return ""
	}
	if strings.HasPrefix(topic, pubsubSubscriptionPathPrefix) {
		topic = topic[strings.LastIndex(topic, "/")+1:]
	}
	return fmt.Sprintf(pubsubSubscriptionPathFormat, project, topic+pubsubDerivedSubscriptionSuffix)
}

// platformPubsubEnv is the broker's variable for the set, or nothing when the
// set is empty, so an install without a Pub/Sub platform renders no relay.
func platformPubsubEnv(subscriptions []string) []corev1.EnvVar {
	if len(subscriptions) == 0 {
		return nil
	}
	return []corev1.EnvVar{{
		Name:  platformPubsubSubscriptionsEnvVar,
		Value: strings.Join(subscriptions, pubsubSubscriptionsSeparator),
	}}
}

// pubsubRelayURL is the gateway's relay URL: the broker's base URL exactly when
// the broker gets a non-empty set, so the two cannot disagree, and empty
// otherwise. Empty rather than absent, on every render: the operator is the
// only writer of the name only while it emits it, and an AgentPlugin's spec.env
// is copied onto the gateway verbatim. A plugin whose route the operator
// refused (a path outside the grammar) could otherwise supply the URL itself
// and have the adapter present the gateway's token to whatever it named. The
// adapter treats an empty value as "no relay" and does not connect.
func pubsubRelayURL(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) string {
	if len(pluginPubsubSubscriptions(agent, agentPlugins)) == 0 {
		return ""
	}
	return credentialProxyBaseURL(agent)
}
