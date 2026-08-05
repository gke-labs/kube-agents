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
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"reflect"
	"regexp"
	"slices"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/yaml"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// manifestsLog is for logging in the manifests builder functions.
var manifestsLog = logf.Log.WithName("platformagent-manifests")

const (
	defaultPlatformAgentSecrets = "platform-agent-secrets"
	sessionKVDBPath             = "/var/lib/kube-agents/session/session_kv.db"
	defaultAgentHome            = "/opt/data"
	defaultStorageSize          = "5Gi"
	credentialProxyPort         = 8765
)

// getDefaultStorageConfig returns the access modes and storage class name based on the replica count and user configuration.
func getDefaultStorageConfig(agent *agentv1alpha1.PlatformAgent) ([]corev1.PersistentVolumeAccessMode, *string) {
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	accessModes := []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}
	var storageClassName *string

	if agent.Spec.Deployment != nil && agent.Spec.Deployment.DefaultStorageClassName != nil {
		storageClassName = agent.Spec.Deployment.DefaultStorageClassName
	} else if replicas > 1 {
		storageClassName = ptr.To("standard-rwx")
	}

	if replicas > 1 {
		accessModes = []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany}
	}

	return accessModes, storageClassName
}

var defaultAccessModes = []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}

// The broker currently receives a shell command string, so these rules allow
// flags between command components. If the protocol is extended to carry argv,
// replace this regex matching with tool-specific argument parsing.
const credentialProxyPolicyJSON = `{
  "apiVersion": "cli.proxy.kubeagents.io/v1alpha1",
  "blockedMessage": "Command blocked for security reasons.",
  "rules": [
    {"id":"gcp.access-token-disclosure","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+print-(?:access|identity)-token\\b"},
    {"id":"gcp.config-helper-disclosure","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+config\\b(?:\\s+\\S+)*?\\s+config-helper\\b"},
    {"id":"github.token-disclosure","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+token\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+status\\b(?:\\s+\\S+)*?\\s+--show-token\\b"},
    {"id":"kubernetes.token-disclosure","pattern":"\\bkubectl\\b(?:\\s+\\S+)*?\\s+create\\b(?:\\s+\\S+)*?\\s+token\\b|\\bkubectl\\b(?:\\s+\\S+)*?\\s+config\\b(?:\\s+\\S+)*?\\s+view\\b(?:\\s+\\S+)*?\\s+--raw\\b"},
    {"id":"git.credential-disclosure","pattern":"\\bgit\\b(?:\\s+\\S+)*?\\s+credential\\b(?:\\s+\\S+)*?\\s+fill\\b"},
    {"id":"gcp.credential-replacement","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+(?:login|activate-service-account)\\b"},
    {"id":"github.credential-replacement","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+(?:login|refresh|switch|logout)\\b"},
    {"id":"tool.self-modification","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+components\\b(?:\\s+\\S+)*?\\s+(?:install|update|remove)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+extension\\b(?:\\s+\\S+)*?\\s+(?:install|upgrade|remove)\\b"}
  ]
}`

// buildConfigMap generates the ConfigMap manifest containing config.yaml
func buildConfigMap(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-config",
			Namespace: agent.Namespace,
		},
		Data: buildConfigMapData(agent, agentPlugins),
	}
}

// buildConfigMapData renders the default profile's config.yaml plus one overlay per
// named profile targeted by a plugin. Overlays ride in the same ConfigMap so a change
// to either moves the existing config hash and rolls the pod — the merge happens at
// startup, so a live update without a restart would be a no-op that silently lies.
func buildConfigMapData(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) map[string]string {
	data := map[string]string{
		"config.yaml":     renderConfigYAML(agent, agentPlugins),
		"leader_elect.py": leaderElectScript,
	}

	_, targeted := partitionPluginsByProfile(filterValidAgentPlugins(agentPlugins))

	// A profile needs an overlay if a plugin targets it OR spec.harness.tuning sets
	// limits for it — tuning alone is enough, so limits can be applied to a profile that
	// hosts no plugins at all.
	profiles := make(map[string]bool, len(targeted)+1)
	for profile := range targeted {
		profiles[profile] = true
	}
	if platformProfileLimits(agent) != nil {
		profiles[platformProfileName] = true
	}
	for profile := range profiles {
		var limits *agentv1alpha1.AgentLimits
		if profile == platformProfileName {
			limits = platformProfileLimits(agent)
		}
		if overlay := renderProfileOverlayYAML(targeted[profile], limits); strings.TrimSpace(overlay) != "" {
			data[profileOverlayKey(profile)] = overlay
		}
	}

	// Cluster profiles are named at runtime, so they get one class overlay applied to
	// all of them rather than a file each.
	if overlay := renderProfileOverlayYAML(nil, clusterProfileLimits(agent)); strings.TrimSpace(overlay) != "" {
		data[clusterProfileClassKey] = overlay
	}
	return data
}

// buildSettingsConfigMap generates the ConfigMap manifest containing SETTINGS.md
func buildSettingsConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	gitRepo := ""
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		gitRepo = strings.TrimSpace(agent.Spec.Integration.GitHub.GitRepo)
	}

	if err := agentv1alpha1.ValidateGitRepoURL(gitRepo); err != nil {
		manifestsLog.Info("Invalid gitRepo URL in PlatformAgent spec, defaulting SETTINGS.md to None", "err", err, "gitRepo", gitRepo)
		gitRepo = "None"
	} else if gitRepo == "" {
		gitRepo = "None"
	}

	settingsContent := fmt.Sprintf("# GKE Scope Configuration\n- **Git Repo:** %s\n", gitRepo)
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-settings",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"SETTINGS.md": settingsContent,
		},
	}
}

// DefaultBuiltInPlugins defines the built-in plugins pre-installed in the Hermes container image.
var DefaultBuiltInPlugins = []string{
	"hermes_otel",
	"session_store",
	"session_otel_bridge",
	"tool_call_audit",
	"incident_context",
	"bootstrap_onboarding",
}

// pluginNamePattern mirrors the CEL rule on AgentPlugin.metadata.name. The name becomes
// both the on-disk directory under $AGENT_HOME/plugins and the identifier Hermes imports,
// so it is restricted to characters valid in a Python module name.
var pluginNamePattern = regexp.MustCompile(`^[a-z][a-z0-9]*$`)

// isValidPluginName reports whether a plugin name is usable as a plugin directory and
// module identifier. The CRD enforces this too; re-checking here keeps a cluster whose
// CEL rule predates this validation from producing an unmountable pod spec.
func isValidPluginName(name string) bool {
	return len(name) <= 56 && pluginNamePattern.MatchString(name)
}

// normalizePluginName reduces a name to comparable form: lowercased with separators
// stripped. AgentPlugin names may not contain separators, but the built-in plugin names
// do, so stripping them lets "sessionstore" be recognised as colliding with the built-in
// "session_store".
func normalizePluginName(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	name = strings.ReplaceAll(name, "-", "")
	name = strings.ReplaceAll(name, "_", "")
	return name
}

// IsBuiltInPlugin returns true if the plugin name matches any built-in Hermes plugin,
// handling hyphen/underscore normalization and case-insensitivity.
func IsBuiltInPlugin(name string) bool {
	norm := normalizePluginName(name)
	for _, p := range DefaultBuiltInPlugins {
		if normalizePluginName(p) == norm {
			return true
		}
	}
	return false
}

// allowedPluginConfigSubtrees bounds which top-level config.yaml keys a plugin may set.
// Anything else — notably agent, leader_election, logging, and plugins — is dropped.
//
// `agent` stays out deliberately: it holds api_max_retries and max_turns, which are
// per-persona operator policy. A plugin that could raise its own retry or iteration
// budget could stall the board for everyone. `plugins` stays out because the operator
// writes plugins.enabled itself, from the plugin set it reconciles — letting config
// touch it would let a plugin enable a plugin the operator does not know about.
var allowedPluginConfigSubtrees = map[string]bool{
	"approvals":         true,
	"platforms":         true,
	"platform_toolsets": true,
}

// gatewayScopedPluginConfigSubtrees are the allowlisted subtrees that always belong to
// the DEFAULT profile, even for a plugin with a TargetProfile.
//
// `platforms` configures platform adapters, and those are gateway-level singletons: the
// gateway process discovers them from its own HERMES_HOME (the default profile) at
// startup and opens one listener per configured entry. Routing a plugin's `platforms`
// block to a named profile would put the subscription somewhere nothing reads it — the
// adapter would come up with no subscriptions and ingress would silently stop, while
// every CR still looked correct. A subscription's own `agent_profile` key is what sends
// the resulting work to a specialist; the listener itself stays on the front door.
var gatewayScopedPluginConfigSubtrees = map[string]bool{
	"platforms": true,
}

// pluginConfigForScope filters a plugin's parsed spec.config down to the subtrees that
// belong to the given scope. Gateway-scoped keys go to the default profile's config;
// everything else follows the plugin to its target profile.
func pluginConfigForScope(pluginConfig map[string]any, gatewayScope bool) map[string]any {
	filtered := make(map[string]any)
	for k, v := range pluginConfig {
		if !allowedPluginConfigSubtrees[k] {
			continue
		}
		if gatewayScopedPluginConfigSubtrees[k] != gatewayScope {
			continue
		}
		filtered[k] = v
	}
	return filtered
}

// profileOverlayPrefix and profileOverlaySuffix bracket the ConfigMap keys holding
// per-profile config overlays. docker-entrypoint.sh globs for this shape, so the two
// must change together.
const (
	profileOverlayPrefix = "profile-"
	profileOverlaySuffix = ".overlay.yaml"

	// profileOverlayDir is where the config ConfigMap is mounted as a directory so the
	// entrypoint can find the overlays. Outside $HERMES_HOME on purpose.
	profileOverlayDir = "/opt/agent-config"
)

// profileOverlayKey returns the ConfigMap key carrying the overlay for a profile.
func profileOverlayKey(profile string) string {
	return profileOverlayPrefix + profile + profileOverlaySuffix
}

// platformProfileName is the profile the Platform Agent runs as.
const platformProfileName = "platform"

// clusterProfileClassKey is the ConfigMap key holding the overlay applied to EVERY
// cluster-* profile.
//
// Cluster profiles are scaffolded at runtime, one per managed cluster, so the operator
// cannot name them individually at render time. The distinct `profileclass-` prefix
// keeps this out of the `profile-<name>` namespace: a sentinel inside that namespace
// could collide with a real profile that happens to share the name.
const clusterProfileClassKey = "profileclass-cluster" + profileOverlaySuffix

// defaultProfileLimits, platformProfileLimits and clusterProfileLimits read
// spec.harness.tuning, tolerating every level being nil.
func defaultProfileLimits(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.AgentLimits {
	if t := agentTuning(agent); t != nil {
		return t.Default
	}
	return nil
}

func platformProfileLimits(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.AgentLimits {
	if t := agentTuning(agent); t != nil {
		return t.Platform
	}
	return nil
}

func clusterProfileLimits(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.AgentLimits {
	if t := agentTuning(agent); t != nil {
		return t.Cluster
	}
	return nil
}

func agentTuning(agent *agentv1alpha1.PlatformAgent) *agentv1alpha1.TuningSpec {
	if agent == nil || agent.Spec.Harness == nil {
		return nil
	}
	return agent.Spec.Harness.Tuning
}

// agentLimitsOverlay renders the `agent` subtree for a profile overlay, or nil when
// nothing is configured — an empty overlay would rewrite the profile config for no
// reason on every reconcile.
//
// The operator may write `agent` here even though a plugin may not (it is absent from
// allowedPluginConfigSubtrees). That asymmetry is deliberate: these limits have
// board-wide consequences — under kanban.max_in_progress a single long-running worker
// blocks every other profile — so they belong to whoever can see the whole board.
func agentLimitsOverlay(limits *agentv1alpha1.AgentLimits) map[string]any {
	if limits == nil {
		return nil
	}
	out := map[string]any{}
	if limits.APIMaxRetries != nil {
		out["api_max_retries"] = *limits.APIMaxRetries
	}
	if limits.MaxTurns != nil {
		out["max_turns"] = *limits.MaxTurns
	}
	if len(out) == 0 {
		return nil
	}
	return map[string]any{"agent": out}
}

// pluginProfileMountRoot is where a profile-targeted plugin's image volume is mounted.
//
// Outside $HERMES_HOME on purpose. That directory is the data PVC, and the kubelet creates
// a volume's mount point before the container's entrypoint runs, so mounting at
// <home>/profiles/<profile>/plugins/<plugin> created profiles/<profile> inside the PVC
// ahead of the scaffold. Both scaffold gates treat an existing directory as a built
// profile, so a fresh PVC that came up with a targeted plugin got a profile Hermes had
// never registered and that never received its skills — and since the directory persists,
// every later start skipped the scaffold too. docker-entrypoint.sh step 2.65 links these
// into the profile after scaffolding; deploy/shared/profile_plugins.py has the details.
const pluginProfileMountRoot = "/opt/agent-plugins"

// pluginMountPath is where a plugin's OCI image volume is mounted.
//
// The default profile's plugins live at the home root and are mounted straight there — it
// is not scaffolded, so nothing gates on its directories. A targeted plugin is staged
// outside the PVC and linked in instead, for the reason above. Hermes resolves a profile's
// plugins from get_hermes_home()/plugins, which for a profile-scoped run is the profile
// directory, so the link is what makes the plugin visible.
func pluginMountPath(homeDir string, plugin *agentv1alpha1.AgentPlugin) string {
	if profile := plugin.Spec.TargetProfile; profile != "" {
		return fmt.Sprintf("%s/%s/%s", pluginProfileMountRoot, profile, plugin.Name)
	}
	return fmt.Sprintf("%s/plugins/%s", homeDir, plugin.Name)
}

// partitionPluginsByProfile splits plugins into those belonging to the default profile
// and those targeting a named profile, keyed by profile name. Order is preserved so the
// rendered config is stable across reconciles.
func partitionPluginsByProfile(agentPlugins []*agentv1alpha1.AgentPlugin) ([]*agentv1alpha1.AgentPlugin, map[string][]*agentv1alpha1.AgentPlugin) {
	var defaultProfile []*agentv1alpha1.AgentPlugin
	targeted := make(map[string][]*agentv1alpha1.AgentPlugin)
	for _, p := range agentPlugins {
		if profile := p.Spec.TargetProfile; profile != "" {
			targeted[profile] = append(targeted[profile], p)
			continue
		}
		defaultProfile = append(defaultProfile, p)
	}
	return defaultProfile, targeted
}

// renderProfileOverlayYAML builds the overlay merged into a named profile's config.yaml
// at pod startup.
//
// It carries only what the operator owns for that profile: the plugins.enabled entries
// and the allowlisted subtrees of each plugin's spec.config. It is deliberately NOT the
// whole config — that file is built at image build time by merging
// deploy/shared/defaults/config.yaml with the profile's own overlay, content the operator
// does not have. Rendering it in full would fork the source of truth; a cluster profile
// additionally carries a runtime `cluster_identity` stamp that overwriting would strip.
func renderProfileOverlayYAML(plugins []*agentv1alpha1.AgentPlugin, limits *agentv1alpha1.AgentLimits) string {
	overlay := map[string]any{}

	// Operator-owned execution limits from spec.harness.tuning. Written before the
	// plugin contributions so a plugin cannot displace them; the allowlist already
	// drops `agent` from plugin config, and this ordering makes that belt-and-braces.
	if agentOverlay := agentLimitsOverlay(limits); agentOverlay != nil {
		overlay = mergeMaps(overlay, agentOverlay)
	}

	enabled := make([]string, 0, len(plugins))
	for _, p := range plugins {
		if !slices.Contains(enabled, p.Name) {
			enabled = append(enabled, p.Name)
		}
	}
	if len(enabled) > 0 {
		overlay["plugins"] = map[string]any{"enabled": enabled}
	}

	for _, p := range plugins {
		if strings.TrimSpace(p.Spec.Config) == "" {
			continue
		}
		var pluginConfig map[string]any
		if err := yaml.Unmarshal([]byte(p.Spec.Config), &pluginConfig); err != nil {
			// Same contract as the default-profile path: malformed config is skipped
			// silently here and surfaced once via pluginConfigIssues/status.
			continue
		}
		// Gateway-scoped subtrees (`platforms`) are deliberately excluded: platform
		// adapters are gateway singletons read from the default profile, so a
		// subscription placed here would be configured where nothing listens.
		overlay = mergeMaps(overlay, pluginConfigForScope(pluginConfig, false))
	}

	// Nothing to say: return empty rather than "{}", which would otherwise be written
	// as a ConfigMap key and make the entrypoint rewrite a profile config for no reason
	// on every start.
	if len(overlay) == 0 {
		return ""
	}

	data, err := yaml.Marshal(overlay)
	if err != nil {
		return ""
	}
	return string(data)
}

// pluginConfigIssues reports problems with a plugin's spec.config: YAML that does not
// parse, or keys dropped for falling outside the allowlist. It mirrors the filtering in
// renderConfigYAML so the same findings can be surfaced on status and logged once,
// instead of being logged from the render path on every reconcile.
func pluginConfigIssues(plugin *agentv1alpha1.AgentPlugin) []string {
	if plugin == nil || strings.TrimSpace(plugin.Spec.Config) == "" {
		return nil
	}

	var parsed map[string]any
	if err := yaml.Unmarshal([]byte(plugin.Spec.Config), &parsed); err != nil {
		return []string{fmt.Sprintf("spec.config is not valid YAML and was ignored: %v.", err)}
	}

	var rejected []string
	for k := range parsed {
		if !allowedPluginConfigSubtrees[k] {
			rejected = append(rejected, k)
		}
	}
	if len(rejected) == 0 {
		return nil
	}
	slices.Sort(rejected)
	return []string{fmt.Sprintf(
		"Ignored config key(s) outside the allowed subtrees [approvals, platforms, platform_toolsets]: %s.",
		strings.Join(rejected, ", "))}
}

// filterValidAgentPlugins drops plugins that must not reach the pod spec or config.yaml.
// It is deliberately silent: it runs twice per reconcile (config render and pod template),
// and the reasons it rejects a plugin are reported on that plugin's status by
// updatePluginStatuses, which logs only when the status actually changes.
func filterValidAgentPlugins(agentPlugins []*agentv1alpha1.AgentPlugin) []*agentv1alpha1.AgentPlugin {
	seen := make(map[string]bool)
	var valid []*agentv1alpha1.AgentPlugin
	for _, p := range agentPlugins {
		if p == nil {
			continue
		}
		if !isValidPluginName(p.Name) {
			continue
		}
		normName := normalizePluginName(p.Name)
		if IsBuiltInPlugin(p.Name) || seen[normName] {
			continue
		}
		seen[normName] = true
		valid = append(valid, p)
	}
	return valid
}

func renderConfigYAML(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) string {
	agentPlugins = filterValidAgentPlugins(agentPlugins)
	cwd := defaultAgentHome
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		cwd = agent.Spec.Harness.Hermes.AgentHome
	}

	cfg := struct {
		Model struct {
			Default  string `json:"default"`
			Provider string `json:"provider"`
			Model    string `json:"model,omitempty"`
			BaseURL  string `json:"base_url,omitempty"`
			APIKey   string `json:"api_key,omitempty"`
		} `json:"model"`
		Terminal struct {
			Backend string `json:"backend"`
			Cwd     string `json:"cwd"`
		} `json:"terminal"`
		MCPServers       map[string]any      `json:"mcp_servers,omitempty"`
		PlatformToolsets map[string][]string `json:"platform_toolsets,omitempty"`
		// Top-level toolsets: read by the kanban tools' check_fn to expose the
		// orchestrator surface (kanban_create/list/…) to the front door. This is
		// a SEPARATE gate from platform_toolsets — both must include `kanban`.
		Toolsets []string `json:"toolsets,omitempty"`
		Agent    struct {
			DisabledToolsets []string `json:"disabled_toolsets,omitempty"`
			// LLM call retry budget. Upstream defaults to 3, which is tuned for
			// an interactive session where a human retries; the front door has
			// no such luxury when Vertex returns 429/503 under load.
			APIMaxRetries int `json:"api_max_retries,omitempty"`
			// Iterations allowed within a single turn. Upstream defaults to 90;
			// omitted unless spec.harness.tuning.default sets it, so the front
			// door keeps the upstream default it has never needed more than.
			MaxTurns int `json:"max_turns,omitempty"`
		} `json:"agent,omitempty"`
		Kanban struct {
			DispatchInGateway       bool `json:"dispatch_in_gateway"`
			AutoSubscribeOnCreate   bool `json:"auto_subscribe_on_create"`
			DispatchIntervalSeconds int  `json:"dispatch_interval_seconds"`
			// Live concurrency cap across the whole board (not a per-tick
			// spawn budget). Every worker shares one LiteLLM/Vertex quota.
			// omitempty matters: without tuning this stays 0, and emitting
			// `max_in_progress: 0` would be both meaningless (Hermes ignores
			// anything below 1) and misleading to anyone reading the ConfigMap.
			MaxInProgress int `json:"max_in_progress,omitempty"`
		} `json:"kanban,omitempty"`
		Approvals struct {
			CronMode string `json:"cron_mode,omitempty"`
		} `json:"approvals,omitempty"`
		Web struct {
			Backend string `json:"backend,omitempty"`
		} `json:"web,omitempty"`
		Memory struct {
			MemoryEnabled      bool   `json:"memory_enabled"`
			Provider           string `json:"provider"`
			UserProfileEnabled bool   `json:"user_profile_enabled"`
		} `json:"memory"`
		Platforms struct {
			GoogleChat struct {
				Enabled bool `json:"enabled"`
				// Overrides the adapter's default "Hermes is thinking…" marker
				// card text with our product name.
				TypingStatusText string `json:"typing_status_text,omitempty"`
			} `json:"google_chat"`
			Slack struct {
				Enabled bool `json:"enabled"`
			} `json:"slack"`
		} `json:"platforms"`
		Plugins struct {
			Enabled []string `json:"enabled"`
		} `json:"plugins"`
		Display struct {
			Platforms map[string]map[string]any `json:"platforms,omitempty"`
		} `json:"display,omitempty"`
		LeaderElection struct {
			Enabled   bool   `json:"enabled"`
			LeaseName string `json:"lease_name,omitempty"`
			Namespace string `json:"namespace,omitempty"`
		} `json:"leader_election,omitempty"`
	}{}

	// Model & Terminal configuration
	cfg.Model.Provider = "custom"
	cfg.Model.Default = "model-default"
	cfg.Model.Model = "model-default"
	cfg.Model.BaseURL = fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace)
	cfg.Model.APIKey = "none"
	cfg.Terminal.Backend = "local"
	cfg.Terminal.Cwd = cwd

	// MCP Servers & Toolsets configuration.
	//
	// The `default` profile is the front-door Chat Agent: its job is to analyze a
	// message, choose the best specialist, delegate, and proxy the chat session.
	// It gets NO runtime tools of its own (no terminal/gcloud/kubectl/files/etc.).
	// Its delegation surface is two things:
	//   - `router` MCP (list_agents): discovery only — lists the dynamic specialist
	//     roster so the Chat Agent can pick the right kanban `assignee`. (The old
	//     synchronous `ask_agent` relay was removed; it blocked up to 300s with no
	//     visible progress. All delegation is kanban-only now.)
	//   - `kanban`: async delegation for ALL substantive work (quick lookups and
	//     long/multi-step/mutating jobs alike). Hermes auto-subscribes this chat
	//     thread and posts the specialist's lifecycle/progress back to it as each
	//     step completes, with no blocking timeout. The dispatcher/notifier run in
	//     this gateway.
	// The privileged Platform Agent and read-only Cluster Agents run as separate
	// Hermes profiles (scaffolded from the image) with their own configs.
	cfg.MCPServers = map[string]any{
		"router": map[string]any{
			"command": "/opt/hermes/.venv/bin/python3",
			// Resolved against cwd, not hardcoded to /opt/data: the entrypoint copies
			// /opt/defaults (which carries scripts/) into $PLATFORM_AGENT_HOME, and the
			// operator sets that env from the same AgentHome that produced cwd. With a
			// custom AgentHome the script is never at /opt/data/scripts, so a literal
			// path would leave the router MCP dead and the Chat Agent unable to
			// discover any specialist to delegate to.
			"args": []string{path.Join(cwd, "scripts/router_server.py")},
			"env": map[string]string{
				"HERMES_HOME": "${HERMES_HOME}",
			},
		},
	}
	// Delegation toolset (router MCP + kanban) for every platform key the gateway
	// may resolve under, including `google_chat` (the real chat-ingress key).
	//
	// `mcp-router` maps to `mcp_servers.router`. Hermes logs a benign startup warning
	// for it ("no valid toolsets configured (unknown name(s): mcp-router)", issue
	// #38798): the startup check validates against the bare keys of `mcp_servers` and
	// does not know the prefixed spelling yet. The tools load regardless, via the alias
	// Hermes registers during discover_mcp_tools. Kept in sync with
	// agents/chat/config.yaml, which carries the same note.
	//
	// `memory` here is a GATE for the multiuser_memory provider, not a tool grant.
	// hermes_cli.tools_config._get_platform_tools() resolves this list for the
	// session's platform key and subtracts agent.disabled_toolsets LAST; what
	// survives becomes agent.enabled_toolsets. inject_memory_provider_tools()
	// then bails unless memory_provider_tools_enabled() sees "memory" there, and
	// that injection is the only path by which multiuser_memory reaches the model.
	// So `memory` must be listed HERE and must NOT be in DisabledToolsets below —
	// listing it in both nets to off (the subtraction wins), which is why the
	// front door's memories dir stayed empty despite the provider loading.
	//
	// Price: the built-in `memory` tool is exposed alongside multiuser_memory. It
	// is inert — MemoryEnabled=false leaves agent._memory_store nil and
	// tools/memory_tool.py returns "Memory is not available" without touching
	// disk. SOUL.md §1.6 tells the agent to write through multiuser_memory.
	cfg.PlatformToolsets = map[string][]string{
		"cli":         {"mcp-router", "kanban", "memory"},
		"api_server":  {"mcp-router", "kanban", "memory"},
		"google_chat": {"mcp-router", "kanban", "memory"},
	}
	// Second gate for the kanban orchestrator surface: the kanban tools' check_fn
	// reads this top-level `toolsets` key (distinct from platform_toolsets above).
	cfg.Toolsets = []string{"kanban"}
	// Pin the chat-transparency machinery on (both default True upstream, pinned
	// so a future default change can't silently disable delegated-progress).
	cfg.Kanban.DispatchInGateway = true
	cfg.Kanban.AutoSubscribeOnCreate = true
	// Dispatcher tick. Upstream defaults to 60s, which added a 0-60s (median ~38s)
	// dead wait to every delegation before the worker was even claimed. 5s matches
	// the notifier watcher's cadence and makes delegation feel immediate.
	cfg.Kanban.DispatchIntervalSeconds = 5
	// Dispatch concurrency is NOT pinned here. Upstream leaves it unbounded, and that
	// suits a fleet with headroom; capping it is a deployment decision, because every
	// worker draws on the same model quota and the right number depends on how much
	// quota this deployment has. spec.harness.tuning.maxInProgress sets it when a
	// deployment needs the cap — see the stockout example in
	// k8s-operator/examples/. Left unset, Hermes' own default applies.
	if limits := agentTuning(agent); limits != nil && limits.MaxInProgress != nil {
		cfg.Kanban.MaxInProgress = *limits.MaxInProgress
	}
	// Defense in depth: disabled_toolsets is applied last by Hermes for EVERY
	// platform key, so even if a base bundle is ever reintroduced the front door
	// still cannot touch the system (no terminal/gcloud/kubectl, files, skills,
	// code-exec, delegate_task, etc.). `kanban` is intentionally NOT disabled —
	// it is the delegation surface. Only mcp-router + kanban survive.
	// `memory` is deliberately NOT in this list: disabling it here would strip
	// "memory" from agent.enabled_toolsets, fail the gate in
	// inject_memory_provider_tools(), and silently kill multiuser_memory — the
	// provider would still load and log "registered (1 tools)" while never
	// reaching the model. See the PlatformToolsets note above. That omission is
	// conditional on the built-in store staying off; it is re-added below when
	// spec.harness.memory.memoryEnabled turns it on.
	cfg.Agent.DisabledToolsets = []string{
		"terminal", "file", "skills", "code_execution", "delegation",
		"browser", "computer_use", "cronjob", "web", "search", "x_search",
		"vision", "video", "image_gen", "video_gen", "tts", "todo",
		"session_search", "project", "homeassistant", "discord",
		"discord_admin", "spotify",
	}
	// Execution limits are NOT pinned here: Hermes' own defaults apply unless a
	// deployment opts in. What a given fleet needs depends on its model quota and on
	// what its agents actually do, so the values belong in the CR rather than baked
	// into every deployment. spec.harness.tuning.default sets them for the front door.
	// The default profile takes them here rather than through an overlay: this rendered
	// file IS the default profile's config, mounted over whatever the image shipped.
	if limits := defaultProfileLimits(agent); limits != nil {
		if limits.APIMaxRetries != nil {
			cfg.Agent.APIMaxRetries = *limits.APIMaxRetries
		}
		if limits.MaxTurns != nil {
			cfg.Agent.MaxTurns = *limits.MaxTurns
		}
	}

	// Execution & Display UX configuration
	cfg.Approvals.CronMode = "approve"
	cfg.Web.Backend = "ddgs"
	// Default built-in plugins pre-installed in the Hermes container image, plus
	// legacy_slash_commands. That one rides on the default profile because it hooks
	// pre_gateway_dispatch on inbound chat messages so a typed "/hermes sethome" reaches
	// the gateway command dispatcher instead of drawing an unknown-command reply — chat
	// ingress lands here, not on the platform specialist. It is not in
	// DefaultBuiltInPlugins because that list is also the roster an AgentPlugin may not
	// shadow, and this plugin ships in agents/chat/defaults/plugins rather than the image.
	// Keep in sync with agents/chat/config.yaml — this copy is authoritative on the
	// deployed default profile.
	cfg.Plugins.Enabled = append(slices.Clone(DefaultBuiltInPlugins), "legacy_slash_commands")
	cfg.Display.Platforms = map[string]map[string]any{}
	// Per-user memory. The built-in MEMORY.md/USER.md store stays off; the
	// multiuser_memory provider replaces it and keys each user's notes off the
	// gateway identity (agent._user_id), writing to memories/users/<user>.md with a
	// shared MEMORY.md alongside. The provider hydrates both into the system prompt
	// itself, so the agent reads without a tool call and only writes through one.
	// This is the only profile that gets it: kanban-spawned specialists carry no
	// human identity, so their writes would collapse into one anonymous bucket.
	cfg.Memory.MemoryEnabled = false
	cfg.Memory.Provider = "multiuser_memory"
	cfg.Memory.UserProfileEnabled = false

	if agent.Spec.Harness != nil && agent.Spec.Harness.Memory != nil {
		if agent.Spec.Harness.Memory.MemoryEnabled != nil {
			cfg.Memory.MemoryEnabled = *agent.Spec.Harness.Memory.MemoryEnabled
		}
		if agent.Spec.Harness.Memory.Provider != "" {
			cfg.Memory.Provider = agent.Spec.Harness.Memory.Provider
		}
		if agent.Spec.Harness.Memory.UserProfileEnabled != nil {
			cfg.Memory.UserProfileEnabled = *agent.Spec.Harness.Memory.UserProfileEnabled
		}
	}

	// Keeping `memory` out of DisabledToolsets is only safe while the built-in
	// store is off. memoryEnabled is a supported CRD field, and setting it true
	// would leave the front door holding a live built-in `memory` tool — a real
	// read/write surface over a single MEMORY.md/USER.md pair with no per-user
	// scoping, which is precisely what multiuser_memory exists to avoid. There is
	// no way to have one without the other: the same toolset name gates the
	// provider injection and exposes the built-in tool. So when the built-in
	// store is switched on, put `memory` back in the denylist. Both memory tools
	// then disappear from the front door — the behaviour this field already had
	// before the gate was opened, and better than two competing stores on a
	// profile whose whole point is a minimal tool surface.
	if cfg.Memory.MemoryEnabled {
		cfg.Agent.DisabledToolsets = append(cfg.Agent.DisabledToolsets, "memory")
	}

	if agent.Spec.Integration != nil {
		if gchat := agent.Spec.Integration.GoogleChat; gchat != nil {
			if gchat.Enabled != nil {
				cfg.Platforms.GoogleChat.Enabled = *gchat.Enabled
				if *gchat.Enabled {
					// Rebrand the Google Chat "thinking" marker card from the
					// upstream default ("Hermes is thinking…") to our product name.
					cfg.Platforms.GoogleChat.TypingStatusText = "Kage is thinking…"
				}
			}
			cfg.Display.Platforms["google_chat"] = resolveGoogleChatDisplayConfig(gchat.Mode)
		}
		if slack := agent.Spec.Integration.Slack; slack != nil && slack.Enabled != nil {
			cfg.Platforms.Slack.Enabled = *slack.Enabled
		}
	}

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		cfg.LeaderElection.Enabled = true
		cfg.LeaderElection.LeaseName = agent.Name + "-leader"
		cfg.LeaderElection.Namespace = agent.Namespace
	}

	// Only plugins without a TargetProfile belong to the default profile. Ones targeting
	// a named profile are enabled by that profile's overlay instead; enabling them here
	// too would load them into the front door as well, which for a privileged skill
	// plugin means handing it to the one agent deliberately stripped of every tool.
	// allPlugins keeps every plugin, targeted or not: gateway-scoped config subtrees
	// (`platforms`) belong to this file regardless of which profile runs the plugin.
	allPlugins := agentPlugins
	agentPlugins, _ = partitionPluginsByProfile(agentPlugins)

	for _, plugin := range agentPlugins {
		if !slices.Contains(cfg.Plugins.Enabled, plugin.Name) {
			cfg.Plugins.Enabled = append(cfg.Plugins.Enabled, plugin.Name)
		}
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return ""
	}

	mergedYAML := string(data)

	hasConfigOverrides := false
	for _, plugin := range allPlugins {
		if strings.TrimSpace(plugin.Spec.Config) != "" {
			hasConfigOverrides = true
			break
		}
	}
	if !hasConfigOverrides {
		return mergedYAML
	}

	var base map[string]any
	if err := yaml.Unmarshal([]byte(mergedYAML), &base); err == nil {
		// Rejections are not logged here: this runs on every reconcile. pluginConfigIssues
		// reports the same findings, and updatePluginStatuses logs them once per change.
		for _, plugin := range allPlugins {
			if strings.TrimSpace(plugin.Spec.Config) == "" {
				continue
			}
			var pluginConfig map[string]any
			if err := yaml.Unmarshal([]byte(plugin.Spec.Config), &pluginConfig); err != nil {
				continue
			}
			// Gateway-scoped subtrees always land here, whoever owns the plugin.
			base = mergeMaps(base, pluginConfigForScope(pluginConfig, true))
			// The rest follow a targeted plugin to its profile overlay; for an
			// untargeted plugin the default profile IS the target, so they land here.
			if plugin.Spec.TargetProfile == "" {
				base = mergeMaps(base, pluginConfigForScope(pluginConfig, false))
			}
		}

		if mergedData, err := yaml.Marshal(base); err == nil {
			return string(mergedData)
		}
	}

	return mergedYAML
}

// resolveGoogleChatDisplayConfig resolves verbosity settings for Google Chat based on mode ("default" or "debug").
func resolveGoogleChatDisplayConfig(mode string) map[string]any {
	resolvedMode := "default"
	if mode != "" {
		resolvedMode = strings.ToLower(mode)
	}

	toolProgress := "off"
	memoryNotifications := "off"
	interimMessages := false

	if resolvedMode == "debug" {
		toolProgress = "all"
		memoryNotifications = "verbose"
		interimMessages = true
	}

	return map[string]any{
		"tool_progress":              toolProgress,
		"memory_notifications":       memoryNotifications,
		"interim_assistant_messages": interimMessages,
		"long_running_notifications": true,
		"busy_ack_detail":            interimMessages,
	}
}

// buildPVC generates the PVC manifest for agent data persistence
func buildPVC(agent *agentv1alpha1.PlatformAgent) *corev1.PersistentVolumeClaim {
	accessModes, storageClassName := getDefaultStorageConfig(agent)
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-data",
			Namespace: agent.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      accessModes,
			StorageClassName: storageClassName,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("10Gi"),
				},
			},
		},
	}
}

func buildSystemPVC(agent *agentv1alpha1.PlatformAgent) *corev1.PersistentVolumeClaim {
	accessModes, storageClassName := getDefaultStorageConfig(agent)
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      "system-metadata",
			Namespace: agent.Namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      accessModes,
			StorageClassName: storageClassName,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: resource.MustParse("1Gi"),
				},
			},
		},
	}
}

// isRWOStorage checks if a storage configuration specifies ReadWriteOnce access or an RWO StorageClass
func isRWOStorage(storage agentv1alpha1.StorageSpec) bool {
	accessModes := storage.AccessModes
	for _, mode := range accessModes {
		if mode == corev1.ReadWriteOnce {
			return true
		}
	}
	if storage.StorageClassName != nil {
		sc := strings.ToLower(*storage.StorageClassName)
		if strings.Contains(sc, "rwo") {
			return true
		}
	}
	return false
}

// hasCustomRWOStorage returns true if any custom storage spec uses ReadWriteOnce access mode or an RWO StorageClass
func hasCustomRWOStorage(agent *agentv1alpha1.PlatformAgent) bool {
	if agent.Spec.Deployment == nil {
		return false
	}
	for _, storage := range agent.Spec.Deployment.Storages {
		if isRWOStorage(storage) {
			return true
		}
	}
	return false
}

// useStatefulSet returns true if the platform agent workload should be managed as a StatefulSet
func useStatefulSet(agent *agentv1alpha1.PlatformAgent) bool {
	if agent.Spec.Deployment == nil {
		return false
	}
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	return replicas > 1 && hasCustomRWOStorage(agent)
}

// buildCustomPVCInstance constructs a single PersistentVolumeClaim manifest
func buildCustomPVCInstance(name, namespace string, accessModes []corev1.PersistentVolumeAccessMode, scName *string, parsedSize resource.Quantity) *corev1.PersistentVolumeClaim {
	return &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "PersistentVolumeClaim",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      accessModes,
			StorageClassName: scName,
			Resources: corev1.VolumeResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceStorage: parsedSize,
				},
			},
		},
	}
}

// buildRWOVolumeClaimTemplates generates VolumeClaimTemplates for RWO custom storage specs in a StatefulSet
func buildRWOVolumeClaimTemplates(agent *agentv1alpha1.PlatformAgent) []corev1.PersistentVolumeClaim {
	if agent.Spec.Deployment == nil || len(agent.Spec.Deployment.Storages) == 0 {
		return nil
	}
	var vcts []corev1.PersistentVolumeClaim
	for _, storage := range agent.Spec.Deployment.Storages {
		if isRWOStorage(storage) {
			accessModes := storage.AccessModes
			if len(accessModes) == 0 {
				accessModes = defaultAccessModes
			}
			storageSize := storage.StorageSize
			if storageSize == "" {
				storageSize = "5Gi"
			}
			parsedSize, err := resource.ParseQuantity(storageSize)
			if err != nil {
				parsedSize = resource.MustParse("5Gi")
			}
			vcts = append(vcts, corev1.PersistentVolumeClaim{
				ObjectMeta: metav1.ObjectMeta{
					Name: storage.Name + "-vol",
				},
				Spec: corev1.PersistentVolumeClaimSpec{
					AccessModes:      accessModes,
					StorageClassName: storage.StorageClassName,
					Resources: corev1.VolumeResourceRequirements{
						Requests: corev1.ResourceList{
							corev1.ResourceStorage: parsedSize,
						},
					},
				},
			})
		}
	}
	return vcts
}

// buildCustomPVCs generates PVC manifests for custom storage definitions specified in DeploymentSpec.Storages
func buildCustomPVCs(agent *agentv1alpha1.PlatformAgent) ([]*corev1.PersistentVolumeClaim, error) {
	if agent.Spec.Deployment == nil || len(agent.Spec.Deployment.Storages) == 0 {
		return nil, nil
	}
	useSts := useStatefulSet(agent)
	var pvcList []*corev1.PersistentVolumeClaim
	for _, storage := range agent.Spec.Deployment.Storages {
		if storage.Name == "" {
			return nil, fmt.Errorf("storage name cannot be empty")
		}
		if useSts && isRWOStorage(storage) {
			continue // Handled by VolumeClaimTemplates in StatefulSet
		}
		scName := storage.StorageClassName
		accessModes := storage.AccessModes
		if len(accessModes) == 0 {
			accessModes = defaultAccessModes
		}
		storageSize := storage.StorageSize
		if storageSize == "" {
			storageSize = defaultStorageSize
		}
		parsedSize, err := resource.ParseQuantity(storageSize)
		if err != nil {
			parsedSize = resource.MustParse(defaultStorageSize)
		}
		pvcList = append(pvcList, buildCustomPVCInstance(storage.Name, agent.Namespace, accessModes, scName, parsedSize))
	}
	return pvcList, nil
}

// buildCustomStorageVolumeMounts generates VolumeMounts for custom storage specs
func buildCustomStorageVolumeMounts(storages []agentv1alpha1.StorageSpec) []corev1.VolumeMount {
	var mounts []corev1.VolumeMount
	for _, storage := range storages {
		if storage.MountPath != "" {
			mounts = append(mounts, corev1.VolumeMount{
				Name:      storage.Name + "-vol",
				MountPath: storage.MountPath,
				SubPath:   storage.SubPath,
				ReadOnly:  storage.ReadOnly,
			})
		}
	}
	return mounts
}

// buildCustomStorageVolumes generates Pod Volumes for custom storage specs
func buildCustomStorageVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	if agent.Spec.Deployment == nil || len(agent.Spec.Deployment.Storages) == 0 {
		return nil
	}
	useSts := useStatefulSet(agent)
	var vols []corev1.Volume
	for _, storage := range agent.Spec.Deployment.Storages {
		if useSts && isRWOStorage(storage) {
			continue // Handled by VolumeClaimTemplates in StatefulSet
		}
		claimName := storage.Name
		vols = append(vols, corev1.Volume{
			Name: storage.Name + "-vol",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: claimName,
					ReadOnly:  storage.ReadOnly,
				},
			},
		})
	}
	return vols
}

// buildPodTemplateSpec generates the shared PodTemplateSpec for Deployment and StatefulSet
func buildPodTemplateSpec(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, isImageVolumeSupported bool) corev1.PodTemplateSpec {
	agentPlugins = filterValidAgentPlugins(agentPlugins)
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	// UID/GID 10000 matches the canonical unprivileged 'hermes' runtime user created in NousResearch/hermes-agent upstream Dockerfile
	fsGroup := int64(10000)

	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	image := resolveAgentImage(agent.Spec.Deployment, defaultPlatformAgentImage())
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}

	var initContainers []corev1.Container
	var sidecars []corev1.Container
	var sidecarVolumes []corev1.Volume
	var extraVolumes []corev1.Volume
	var podAnnotations map[string]string
	if agent.Spec.Deployment != nil {
		initContainers = agent.Spec.Deployment.InitContainers
		sidecars = agent.Spec.Deployment.Sidecars
		sidecarVolumes = agent.Spec.Deployment.SidecarVolumes
		extraVolumes = agent.Spec.Deployment.ExtraVolumes
		podAnnotations = agent.Spec.Deployment.PodAnnotations
	}

	homeDir := "/opt/data"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		homeDir = agent.Spec.Harness.Hermes.AgentHome
	}
	// The data PVC survives upgrades. Remove credential files written by older,
	// credentialed deployments before the agent sandbox can mount the PVC.
	initContainers = append([]corev1.Container{buildSandboxCredentialCleanup(image, pullPolicy)}, initContainers...)

	pluginsDebugVal := "0"
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.PluginsDebug != nil {
		if *agent.Spec.Harness.Hermes.PluginsDebug {
			pluginsDebugVal = "1"
		}
	}

	envVars := []corev1.EnvVar{
		{
			Name:  "PLATFORM_AGENT_HOME",
			Value: homeDir,
		},
		{
			Name:  "HOME",
			Value: strings.TrimSuffix(homeDir, "/") + "/home",
		},
		{
			Name:  "PLATFORM_AGENT_PLUGINS_DEBUG",
			Value: pluginsDebugVal,
		},
		{
			Name:  "API_SERVER_ENABLED",
			Value: "true",
		},
		{
			Name:  "API_SERVER_HOST",
			Value: "127.0.0.1",
		},
		{
			// The sidecar authenticates external callers and replaces their bearer
			// key with this non-secret loopback sentinel.
			Name:  "API_SERVER_KEY",
			Value: "cluster-internal-trusted",
		},
		{
			Name:  "SESSION_KV_DB_PATH",
			Value: sessionKVDBPath,
		},
	}

	envVars = append(envVars, otelTelemetryEnvVars("platform", agent.Name, agent.Namespace)...)
	if agent.Spec.Deployment != nil {
		envVars = mergeEnvVars(envVars, safeSandboxEnvOverrides(agent.Spec.Deployment.Env))
	}

	if agent.Spec.Deployment != nil && len(agent.Spec.Deployment.BrowserArgs) > 0 {
		envVars = append(envVars, corev1.EnvVar{
			Name:  "AGENT_BROWSER_ARGS",
			Value: strings.Join(agent.Spec.Deployment.BrowserArgs, " "),
		})
	}

	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.ProjectID != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_PROJECT_ID",
				Value: agent.Spec.Harness.ProjectID,
			})
		}
		if agent.Spec.Harness.ClusterName != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_CLUSTER_NAME",
				Value: agent.Spec.Harness.ClusterName,
			})
		}
		if agent.Spec.Harness.Location != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GKE_LOCATION",
				Value: agent.Spec.Harness.Location,
			})
		}
		if agent.Spec.Harness.ProjectID != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GCP_PROJECT_ID",
				Value: agent.Spec.Harness.ProjectID,
			})
		}
		if agent.Spec.Harness.ProjectID != "" && agent.Spec.Harness.Location != "" && agent.Spec.Harness.ClusterName != "" {
			envVars = append(envVars, corev1.EnvVar{
				Name: "KUBE_CONTEXT_NAME",
				Value: fmt.Sprintf(
					"gke_%s_%s_%s",
					agent.Spec.Harness.ProjectID,
					agent.Spec.Harness.Location,
					agent.Spec.Harness.ClusterName,
				),
			})
		}
		envVars = append(envVars, corev1.EnvVar{
			Name:  "KUBE_DEFAULT_NAMESPACE",
			Value: agent.Namespace,
		})
	}

	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			envVars = append(envVars, []corev1.EnvVar{
				{
					Name:  "GOOGLE_CHAT_RELAY_URL",
					Value: fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort),
				},
				{
					Name:  "GOOGLE_CHAT_PROJECT_ID",
					Value: gchat.ProjectID,
				},
				{
					Name:  "GOOGLE_CHAT_SUBSCRIPTION_NAME",
					Value: fmt.Sprintf("projects/%s/subscriptions/%s", gchat.ProjectID, gchat.SubscriptionName),
				},
				{
					Name:  "GOOGLE_CHAT_ALLOWED_USERS",
					Value: strings.Join(gchat.AllowedUsers, ","),
				},
				{
					Name:  "GOOGLE_CHAT_HOME_CHANNEL",
					Value: gchat.HomeChannel,
				},
			}...)
			allowAll := len(gchat.AllowedUsers) == 0
			if len(gchat.AllowedUsers) == 1 && gchat.AllowedUsers[0] == "" {
				allowAll = true
			}
			if allowAll {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "GOOGLE_CHAT_ALLOW_ALL_USERS",
					Value: "true",
				})
			}
		}
		if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
			envVars = append(envVars, corev1.EnvVar{
				Name:  "SLACK_RELAY_URL",
				Value: fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort),
			})
			allowAllSlack := len(slack.AllowedUsers) == 0 || (len(slack.AllowedUsers) == 1 && slack.AllowedUsers[0] == "")
			if allowAllSlack {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_ALLOW_ALL_USERS",
					Value: "true",
				})
			} else {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_ALLOWED_USERS",
					Value: strings.Join(slack.AllowedUsers, ","),
				})
			}
			if slack.HomeChannel != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_HOME_CHANNEL",
					Value: slack.HomeChannel,
				})
			}
			if slack.HomeChannelName != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "SLACK_HOME_CHANNEL_NAME",
					Value: slack.HomeChannelName,
				})
			}
		}
	}

	if replicas > 1 {
		envVars = append(envVars,
			corev1.EnvVar{
				Name:  "ENABLE_LEADER_ELECTION",
				Value: "true",
			},
			corev1.EnvVar{
				Name:  "LEADER_ELECTION_LEASE_NAME",
				Value: agent.Name + "-leader",
			},
			corev1.EnvVar{
				Name:  "LEADER_ELECTION_NAMESPACE",
				Value: agent.Namespace,
			},
		)
	}

	if len(agentPlugins) > 0 {
		extEnvs := extractAgentPluginEnvVars(agentPlugins)
		if len(extEnvs) > 0 {
			envVars = mergeEnvVars(envVars, extEnvs)
		}
	}

	envVars = append(envVars, corev1.EnvVar{
		Name:  "CREDENTIAL_PROXY_URL",
		Value: fmt.Sprintf("http://127.0.0.1:%d", credentialProxyPort),
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "PATH",
		Value: "/opt/credential-proxy/bin:/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "PYTHONPATH",
		Value: "/opt/defaults/scripts",
	})

	dashboardEnabled := isDashboardEnabled(agent)

	var shareProcessNamespace *bool
	if dashboardEnabled {
		shareProcessNamespace = ptr.To(true)
	}

	var runtimeClassName *string
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		runtimeClassName = agent.Spec.Deployment.Availability.RuntimeClassName
	}

	containers := buildBaseContainers(agent, image, envVars, agentPlugins, isImageVolumeSupported)
	containers = append(containers, buildCredentialProxySidecar(agent, homeDir))

	defaultAnnotations := map[string]string{
		"kubeagents.x-k8s.io/config-hash":            configHash,
		"kubeagents.x-k8s.io/fluent-bit-config-hash": fluentBitHash,
		"kubeagents.x-k8s.io/settings-config-hash":   settingsConfigHash,
		"kubeagents.x-k8s.io/proxy-policy-hash":      policyHash,
	}

	if len(sidecars) > 0 {
		containers = append(containers, sidecars...)
	}

	volumes := buildDefaultVolumes(agent)
	for _, plugin := range agentPlugins {
		if isImageVolumeSupported {
			pullPolicy := corev1.PullIfNotPresent
			if plugin.Spec.ImagePullPolicy != nil {
				pullPolicy = *plugin.Spec.ImagePullPolicy
			}
			volumes = append(volumes, corev1.Volume{
				Name: buildPluginVolumeName(plugin.Name),
				VolumeSource: corev1.VolumeSource{
					Image: &corev1.ImageVolumeSource{
						Reference:  plugin.Spec.Image,
						PullPolicy: pullPolicy,
					},
				},
			})
		} else {
			manifestsLog.Error(fmt.Errorf("ImageVolumeSource unsupported on Kubernetes < 1.35"),
				"skipping plugin OCI image volume mount to prevent deployment pod validation failure",
				"plugin", plugin.Name,
				"platformagent", agent.Name)
		}
	}
	volumes = append(volumes, buildCustomStorageVolumes(agent)...)
	volumes = append(volumes, buildCredentialProxyVolumes(agent)...)
	if len(sidecarVolumes) > 0 {
		volumes = append(volumes, sidecarVolumes...)
	}
	if len(extraVolumes) > 0 {
		volumes = append(volumes, extraVolumes...)
	}

	var affinity *corev1.Affinity
	var nodeSelector map[string]string
	var tolerations []corev1.Toleration

	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		affinity = agent.Spec.Deployment.Availability.Affinity
		nodeSelector = agent.Spec.Deployment.Availability.NodeSelector
		tolerations = agent.Spec.Deployment.Availability.Tolerations
	}

	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
				"kubeagents.x-k8s.io/has-credential-proxy": "true",
			},
			Annotations: mergeAnnotations(defaultAnnotations, podAnnotations),
		},
		Spec: corev1.PodSpec{
			ShareProcessNamespace:        shareProcessNamespace,
			RuntimeClassName:             runtimeClassName,
			InitContainers:               initContainers,
			ServiceAccountName:           saName,
			AutomountServiceAccountToken: ptr.To(false),
			SecurityContext: &corev1.PodSecurityContext{
				FSGroup: &fsGroup,
				// UID 10000 matches canonical 'hermes' runtime user in upstream image (NousResearch/hermes-agent Dockerfile line 92)
				RunAsUser:      ptr.To(int64(10000)),
				RunAsNonRoot:   ptr.To(true),
				SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
			},
			Affinity:     affinity,
			NodeSelector: nodeSelector,
			Tolerations:  tolerations,
			Containers:   containers,
			Volumes:      volumes,
		},
	}
}

// buildDeployment generates the Deployment manifest for the agent payload
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, isImageVolumeSupported bool) *appsv1.Deployment {
	replicas, strategy := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash, agentPlugins, isImageVolumeSupported)

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "Deployment",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway",
			Namespace: agent.Namespace,
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
				"kubeagents.x-k8s.io/has-credential-proxy": "true",
			},
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Strategy: strategy,
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
			Template: podTemplate,
		},
	}
}

// buildStatefulSet generates the StatefulSet manifest for PlatformAgent when RWO custom storage is used with multiple replicas
func buildStatefulSet(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, isImageVolumeSupported bool) *appsv1.StatefulSet {
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash, agentPlugins, isImageVolumeSupported)
	vcts := buildRWOVolumeClaimTemplates(agent)

	return &appsv1.StatefulSet{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "StatefulSet",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway",
			Namespace: agent.Namespace,
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
			},
		},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    &replicas,
			ServiceName: agent.Name,
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
			Template:             podTemplate,
			VolumeClaimTemplates: vcts,
		},
	}
}

// buildDefaultVolumeMounts generates default volume mounts for PlatformAgent
func buildDefaultVolumeMounts(homeDir string) []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{
			Name:      "platform-agent-data-vol",
			MountPath: homeDir,
		},
		{
			Name:      "platform-agent-config-vol",
			MountPath: fmt.Sprintf("%s/config.yaml", homeDir),
			SubPath:   "config.yaml",
		},
		{
			Name:      "platform-agent-config-vol",
			MountPath: fmt.Sprintf("%s/leader_elect.py", homeDir),
			SubPath:   "leader_elect.py",
		},
		{
			// Whole-ConfigMap directory mount so docker-entrypoint.sh can glob the
			// per-profile overlays without the operator having to enumerate them as
			// individual subPath mounts. Read-only and outside $HERMES_HOME so it
			// cannot shadow anything the agent writes.
			Name:      "platform-agent-config-vol",
			MountPath: profileOverlayDir,
			ReadOnly:  true,
		},
		{
			Name:      "settings-volume",
			MountPath: path.Join(homeDir, "SETTINGS.md"),
			SubPath:   "SETTINGS.md",
			ReadOnly:  true,
		},
		{
			Name:      "system-metadata",
			MountPath: path.Dir(sessionKVDBPath),
			SubPath:   "session",
		},
	}
}

func buildSandboxCredentialCleanup(image string, pullPolicy corev1.PullPolicy) corev1.Container {
	return corev1.Container{
		Name:            "sandbox-credential-cleanup",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command:         []string{"sh", "-ec"},
		Args: []string{`rm -rf -- \
  /workspace/home/.config/gcloud \
  /workspace/home/.config/gh \
  /workspace/home/.aws/credentials \
  /workspace/home/.aws/cli/cache \
  /workspace/home/.aws/sso/cache \
  /workspace/home/.azure \
  /workspace/home/.docker/config.json \
  /workspace/home/.git-credentials \
  /workspace/home/.hermes/.env \
  /workspace/home/.kube/config \
  /workspace/home/.netrc \
  /workspace/home/.npmrc \
  /workspace/home/.pypirc`},
		VolumeMounts: []corev1.VolumeMount{{Name: "platform-agent-data-vol", MountPath: "/workspace"}},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			ReadOnlyRootFilesystem:   ptr.To(true),
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
		Resources: corev1.ResourceRequirements{
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("256Mi"),
			},
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("100m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		},
	}
}

func buildCredentialProxyPolicyConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-credential-proxy-policy",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{"policy.json": credentialProxyPolicyJSON},
	}
}

// buildCredentialProxySidecar returns the Envoy-fronted credential runtime.
// Its environment and volume mounts are intentionally disjoint from the agent
// container even though both containers share a Pod network namespace.
func buildCredentialProxySidecar(agent *agentv1alpha1.PlatformAgent, homeDir string) corev1.Container {
	image := resolveCredentialProxyImage(agent.Spec.Deployment)
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}
	envVars := buildCredentialProxyEnv(agent)
	envVars = append(envVars, corev1.EnvVar{Name: "CREDENTIAL_PROXY_WORKSPACE_ROOT", Value: homeDir})
	return corev1.Container{
		Name:            "envoy-credential-proxy",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command:         []string{"/usr/local/bin/envoy-credential-sidecar"},
		Env:             envVars,
		Ports: []corev1.ContainerPort{
			{Name: "cred-proxy", ContainerPort: credentialProxyPort},
			{Name: "proxy-api", ContainerPort: 8643},
		},
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{Exec: &corev1.ExecAction{Command: []string{
				"curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8765/healthz",
			}}},
			InitialDelaySeconds: 5,
			PeriodSeconds:       15,
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("100m"), corev1.ResourceMemory: resource.MustParse("256Mi")},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU: resource.MustParse("2"), corev1.ResourceMemory: resource.MustParse("2Gi"), corev1.ResourceEphemeralStorage: resource.MustParse("2Gi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "credential-proxy-policy", MountPath: "/etc/credential-proxy/policy.json", SubPath: "policy.json", ReadOnly: true},
			{Name: "credential-proxy-tmp", MountPath: "/tmp"},
			{Name: "credential-proxy-state", MountPath: "/var/lib/credential-proxy"},
			{Name: "credential-proxy-runtime", MountPath: "/var/run/credential-proxy"},
			{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher"},
			{Name: "credential-proxy-ksa-token", MountPath: "/var/run/secrets/kubeagents/serviceaccount", ReadOnly: true},
			{Name: "platform-agent-data-vol", MountPath: homeDir},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

func buildCredentialProxyEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	envVars := []corev1.EnvVar{
		{Name: "PLATFORM_AGENT_HOME", Value: "/tmp/credential-proxy"},
		{Name: "HOME", Value: "/tmp/credential-proxy/home"},
		{Name: "CREDENTIAL_PROXY_POLICY", Value: "/etc/credential-proxy/policy.json"},
		{Name: "CREDENTIAL_PROXY_STATE_DIR", Value: "/var/lib/credential-proxy"},
		{Name: "CREDENTIAL_PROXY_UNIX_SOCKET", Value: "/var/run/credential-proxy/backend.sock"},
		{Name: "KUBECONFIG", Value: "/var/run/event-watcher/watcher.config"},
		{Name: "KSA_TOKEN_FILE", Value: "/var/run/secrets/kubeagents/serviceaccount/token"},
		{Name: "TOKEN_BROKER_URL", Value: fmt.Sprintf("http://github-token-minter.%s.svc.cluster.local:8080/token", agent.Namespace)},
		{Name: "AGENT_API_PROXY_PORT", Value: "8643"},
		{Name: "AGENT_API_UPSTREAM_KEY", Value: "cluster-internal-trusted"},
	}
	apiServerSecretRef := defaultSecretRef(nil, defaultPlatformAgentSecrets, "API_SERVER_KEY")
	if harness := agent.Spec.Harness; harness != nil && harness.Hermes != nil && harness.Hermes.ApiServerSecretRef != nil {
		apiServerSecretRef = harness.Hermes.ApiServerSecretRef
	}
	envVars = append(envVars, corev1.EnvVar{
		Name: "API_SERVER_EXTERNAL_KEY",
		ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: apiServerSecretRef,
		},
	})
	if harness := agent.Spec.Harness; harness != nil && harness.ProjectID != "" && harness.Location != "" && harness.ClusterName != "" {
		envVars = append(envVars,
			corev1.EnvVar{Name: "GKE_PROJECT_ID", Value: harness.ProjectID}, corev1.EnvVar{Name: "GKE_CLUSTER_NAME", Value: harness.ClusterName}, corev1.EnvVar{Name: "GKE_LOCATION", Value: harness.Location},
			corev1.EnvVar{Name: "KUBE_CONTEXT_NAME", Value: fmt.Sprintf("gke_%s_%s_%s", harness.ProjectID, harness.Location, harness.ClusterName)}, corev1.EnvVar{Name: "KUBE_DEFAULT_NAMESPACE", Value: agent.Namespace},
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", Value: `gcloud config set project "$GKE_PROJECT_ID" >/dev/null &&
gcloud container clusters get-credentials "$GKE_CLUSTER_NAME" --location "$GKE_LOCATION" --project "$GKE_PROJECT_ID" &&
kubectl config use-context "$KUBE_CONTEXT_NAME" >/dev/null &&
kubectl config set-context "$KUBE_CONTEXT_NAME" --namespace="$KUBE_DEFAULT_NAMESPACE" >/dev/null`},
		)
	}
	if integration := agent.Spec.Integration; integration != nil {
		if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
			envVars = append(envVars, corev1.EnvVar{Name: "GOOGLE_CHAT_PROJECT_ID", Value: gchat.ProjectID}, corev1.EnvVar{Name: "GOOGLE_CHAT_SUBSCRIPTION_NAME", Value: fmt.Sprintf("projects/%s/subscriptions/%s", gchat.ProjectID, gchat.SubscriptionName)})
		}
		if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
			envVars = append(envVars,
				corev1.EnvVar{Name: "SLACK_BOT_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.BotTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_BOT_TOKEN")}},
				corev1.EnvVar{Name: "SLACK_APP_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(slack.AppTokenSecretRef, defaultPlatformAgentSecrets, "SLACK_APP_TOKEN")}},
			)
		}
	}
	if agent.Spec.Deployment != nil {
		envVars = mergeCredentialProxyEnv(envVars, agent.Spec.Deployment.Env)
	}
	return envVars
}

func mergeCredentialProxyEnv(managed, custom []corev1.EnvVar) []corev1.EnvVar {
	reserved := map[string]struct{}{
		"PATH": {}, "PYTHONPATH": {}, "ENV": {}, "BASH_ENV": {},
		"LD_PRELOAD": {}, "LD_LIBRARY_PATH": {},
		"KUBERNETES_SERVICE_HOST": {}, "KUBERNETES_SERVICE_PORT": {},
	}
	for _, env := range managed {
		reserved[env.Name] = struct{}{}
	}
	for name := range agentv1alpha1.SensitiveEnvVars {
		reserved[name] = struct{}{}
	}
	for _, name := range []string{
		"CREDENTIAL_PROXY_BOOTSTRAP_COMMAND",
		"CREDENTIAL_PROXY_MAX_OUTPUT_BYTES",
		"CREDENTIAL_PROXY_MAX_REQUEST_BYTES",
		"CREDENTIAL_PROXY_POLICY",
		"CREDENTIAL_PROXY_PORT",
		"CREDENTIAL_PROXY_STATE_DIR",
		"CREDENTIAL_PROXY_TIMEOUT_SECONDS",
		"CREDENTIAL_PROXY_UNIX_SOCKET",
		"CREDENTIAL_PROXY_WORKSPACE_ROOT",
		"KSA_TOKEN_FILE",
		"TOKEN_BROKER_URL",
	} {
		reserved[name] = struct{}{}
	}

	result := append([]corev1.EnvVar{}, managed...)
	for _, env := range custom {
		if _, found := reserved[env.Name]; !found {
			result = append(result, env)
		}
	}
	return result
}

// safeSandboxEnvOverrides preserves non-secret telemetry customization without
// copying arbitrary deployment environment variables into the agent sandbox.
func safeSandboxEnvOverrides(custom []corev1.EnvVar) []corev1.EnvVar {
	allowed := map[string]struct{}{
		"OTEL_EXPORTER_OTLP_ENDPOINT": {},
		"OTEL_EXPORTER_OTLP_PROTOCOL": {},
		"OTEL_RESOURCE_ATTRIBUTES":    {},
		"OTEL_SERVICE_NAME":           {},
	}
	var result []corev1.EnvVar
	for _, env := range custom {
		// Only literal telemetry settings are safe to copy. A ValueFrom source can
		// reference a Secret even when its environment variable name is allowlisted.
		if _, ok := allowed[env.Name]; ok && env.ValueFrom == nil {
			result = append(result, env)
		}
	}
	return result
}

func buildCredentialProxyVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return []corev1.Volume{
		{Name: "credential-proxy-policy", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: agent.Name + "-credential-proxy-policy"}}}},
		{Name: "credential-proxy-tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("2Gi"))}}},
		{Name: "credential-proxy-state", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("5Gi"))}}},
		{Name: "credential-proxy-runtime", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("16Mi"))}}},
		{Name: "event-watcher-kubeconfig", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("1Mi"))}}},
		{Name: "credential-proxy-ksa-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: "kubeagents-credential-proxy", ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}}},
		{Name: "event-watcher-ksa-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{
				{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{ExpirationSeconds: ptr.To(int64(3600)), Path: "token"}},
				{ConfigMap: &corev1.ConfigMapProjection{
					LocalObjectReference: corev1.LocalObjectReference{Name: "kube-root-ca.crt"},
					Items:                []corev1.KeyToPath{{Key: "ca.crt", Path: "ca.crt"}},
				}},
				{DownwardAPI: &corev1.DownwardAPIProjection{Items: []corev1.DownwardAPIVolumeFile{{
					Path: "namespace", FieldRef: &corev1.ObjectFieldSelector{APIVersion: "v1", FieldPath: "metadata.namespace"},
				}}}},
			},
		}}},
	}
}

// resolveCredentialProxyImage returns the credential-proxy sidecar image. An
// explicit CREDENTIAL_PROXY_IMAGE env var wins; otherwise the image is derived
// from the resolved agent image — same registry and tag as the image the agent
// container actually runs, with the name platform-agent → credential-proxy —
// so agent and sidecar can never end up on different versions.
func resolveCredentialProxyImage(deployment *agentv1alpha1.DeploymentSpec) string {
	if override := os.Getenv(credentialProxyImageEnvVar); override != "" {
		return override
	}
	image := resolveAgentImage(deployment, defaultPlatformAgentImage())
	lastSlash := strings.LastIndex(image, "/")
	prefix, name := "", image
	if lastSlash >= 0 {
		prefix, name = image[:lastSlash+1], image[lastSlash+1:]
	}
	suffix := ""
	if digest := strings.Index(name, "@"); digest >= 0 {
		// The agent image's digest cannot name the proxy image; fall back to
		// the tag field or latest.
		name = name[:digest]
		sidecarTag := "latest"
		if deployment != nil && deployment.Tag != nil && *deployment.Tag != "" {
			suffix = ":" + *deployment.Tag
			sidecarTag = *deployment.Tag
		}
		manifestsLog.Info("digest-pinned agent image cannot pin the credential-proxy sidecar; using a mutable tag instead",
			"agentImage", image, "sidecarTag", sidecarTag)
	} else if tag := strings.LastIndex(name, ":"); tag >= 0 {
		suffix, name = name[tag:], name[:tag]
	}
	if name == "platform-agent" {
		name = "credential-proxy"
	} else {
		name += "-credential-proxy"
	}
	if suffix == "" {
		suffix = ":latest"
	}
	return prefix + name + suffix
}

// buildBaseContainers generates the base containers for PlatformAgent.
func buildBaseContainers(agent *agentv1alpha1.PlatformAgent, image string, envVars []corev1.EnvVar, agentPlugins []*agentv1alpha1.AgentPlugin, isImageVolumeSupported bool) []corev1.Container {
	homeDir := defaultAgentHome
	if agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.AgentHome != "" {
		homeDir = agent.Spec.Harness.Hermes.AgentHome
	}

	pullPolicy := corev1.PullAlways
	var extraVolumeMounts []corev1.VolumeMount
	var storages []agentv1alpha1.StorageSpec
	if agent.Spec.Deployment != nil {
		if agent.Spec.Deployment.ImagePullPolicy != nil {
			pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
		}
		extraVolumeMounts = agent.Spec.Deployment.ExtraVolumeMounts
		storages = agent.Spec.Deployment.Storages
	}

	resources := resolveResources(agent.Spec.Deployment)

	volumeMounts := buildDefaultVolumeMounts(homeDir)
	if len(storages) > 0 {
		volumeMounts = append(volumeMounts, buildCustomStorageVolumeMounts(storages)...)
	}
	if len(extraVolumeMounts) > 0 {
		volumeMounts = append(volumeMounts, extraVolumeMounts...)
	}

	var command []string
	var args []string

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		command = []string{"/opt/hermes/.venv/bin/python3"}
		args = []string{fmt.Sprintf("%s/leader_elect.py", homeDir)}
	}

	clusterName := "platform-agent-host"
	if agent.Spec.Harness != nil {
		if agent.Spec.Harness.ClusterName != "" {
			clusterName = agent.Spec.Harness.ClusterName
		}
	}

	if isImageVolumeSupported {
		for _, plugin := range agentPlugins {
			volumeMounts = append(volumeMounts, corev1.VolumeMount{
				Name:      buildPluginVolumeName(plugin.Name),
				MountPath: pluginMountPath(homeDir, plugin),
			})
		}
	}

	containers := []corev1.Container{
		{
			Name:            "platform-agent",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Command:         command,
			Args:            args,
			Ports: []corev1.ContainerPort{
				{
					Name:          "api",
					ContainerPort: 8642,
				},
			},
			Env:          envVars,
			Resources:    resources,
			VolumeMounts: volumeMounts,
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		},
	}

	if isDashboardEnabled(agent) {
		dashboardEnvVars := []corev1.EnvVar{
			{
				Name:  "PLATFORM_AGENT_HOME",
				Value: homeDir,
			},
			{
				Name:  "HOME",
				Value: strings.TrimSuffix(homeDir, "/") + "/home",
			},
			{
				Name:  "SESSION_KV_DB_PATH",
				Value: sessionKVDBPath,
			},
		}

		dashboardVolumeMounts := []corev1.VolumeMount{
			{
				Name:      "platform-agent-data-vol",
				MountPath: homeDir,
			},
			{
				Name:      "system-metadata",
				MountPath: path.Dir(sessionKVDBPath),
				SubPath:   "session",
			},
		}

		containers = append(containers, corev1.Container{
			Name:            "platform-agent-dashboard",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Args:            []string{"hermes", "dashboard"},
			Ports: []corev1.ContainerPort{
				{
					Name:          "dashboard",
					ContainerPort: 9119,
				},
			},
			Env: dashboardEnvVars,
			Resources: corev1.ResourceRequirements{
				Requests: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("256m"),
					corev1.ResourceMemory: resource.MustParse("512Mi"),
				},
				Limits: corev1.ResourceList{
					corev1.ResourceCPU:    resource.MustParse("1"),
					corev1.ResourceMemory: resource.MustParse("2Gi"),
				},
			},
			VolumeMounts: append(dashboardVolumeMounts, extraVolumeMounts...),
			SecurityContext: &corev1.SecurityContext{
				AllowPrivilegeEscalation: ptr.To(false),
				Capabilities: &corev1.Capabilities{
					Drop: []corev1.Capability{"ALL"},
				},
			},
		})
	}

	containers = append(containers, corev1.Container{
		Name:  "fluent-bit",
		Image: fluentBitImage(),
		Args: []string{
			"-c",
			"/fluent-bit/etc/fluent-bit.conf",
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:              resource.MustParse("100m"),
				corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
				corev1.ResourceMemory:           resource.MustParse("128Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:              resource.MustParse("500m"),
				corev1.ResourceEphemeralStorage: resource.MustParse("1Gi"),
				corev1.ResourceMemory:           resource.MustParse("256Mi"),
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{
				Name:      "platform-agent-data-vol",
				MountPath: "/opt/data",
				ReadOnly:  true,
			},
			{
				Name:      "fluent-bit-config",
				MountPath: "/fluent-bit/etc/fluent-bit.conf",
				SubPath:   "fluent-bit.conf",
				ReadOnly:  true,
			},
			{
				Name:      "fluent-bit-config",
				MountPath: "/fluent-bit/etc/parsers.conf",
				SubPath:   "parsers.conf",
				ReadOnly:  true,
			},
			{
				Name:      "fluent-bit-state",
				MountPath: "/fluent-bit/state",
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			Capabilities: &corev1.Capabilities{
				Drop: []corev1.Capability{"ALL"},
			},
		},
	})

	// Inject the k8s-event-watcher sidecar container to capture GKE warnings and stream them to the local REST bridge
	containers = append(containers, corev1.Container{
		Name:            "event-watcher",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		Command: []string{
			"/usr/local/bin/k8s-event-watcher",
		},
		Args: []string{
			"--cluster-name=" + clusterName,
			"--daemon-url=http://127.0.0.1:8699",
			"--token-env=API_SERVER_KEY",
			"--owner=platform",
			"--reason=Failed,FailedToDrainNode,CrashLoopBackOff,BackOff,ImagePullBackOff,ErrImagePull,OOMKilled",
			"--kubeconfig=/var/run/event-watcher/watcher.config",
		},
		Env: []corev1.EnvVar{
			{
				Name:  "API_SERVER_KEY",
				Value: "cluster-internal-trusted",
			},
			{
				Name:  "HOME",
				Value: strings.TrimSuffix(homeDir, "/") + "/home",
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher", ReadOnly: true},
			{Name: "event-watcher-ksa-token", MountPath: "/var/run/secrets/kubernetes.io/serviceaccount", ReadOnly: true},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("50m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("200m"),
				corev1.ResourceMemory: resource.MustParse("128Mi"),
			},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false),
			Capabilities: &corev1.Capabilities{
				Drop: []corev1.Capability{"ALL"},
			},
		},
	})

	return containers
}

// buildDefaultVolumes generates the default volumes for PlatformAgent
func buildDefaultVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return []corev1.Volume{
		{
			Name: "platform-agent-data-vol",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: agent.Name + "-data",
				},
			},
		},
		{
			Name: "platform-agent-config-vol",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-config",
					},
					DefaultMode: ptr.To(int32(0755)),
				},
			},
		},
		{
			Name: "fluent-bit-config",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-fluent-bit-config",
					},
					DefaultMode: ptr.To(int32(420)),
				},
			},
		},
		{
			Name: "fluent-bit-state",
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{},
			},
		},
		{
			Name: "system-metadata",
			VolumeSource: corev1.VolumeSource{
				PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{
					ClaimName: "system-metadata",
				},
			},
		},
		{
			Name: "settings-volume",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-settings",
					},
					DefaultMode: ptr.To(int32(0644)),
				},
			},
		},
	}
}

// buildPlatformExplorerRole generates the custom ClusterRole manifest
func buildPlatformExplorerRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.ClusterRole {
	return &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRole",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("kubeagents:explorer:%s:%s", agent.Namespace, agent.Name),
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{""},
				Resources: []string{"nodes", "pods", "namespaces"},
				Verbs:     []string{"get", "list"},
			},
			{
				APIGroups: []string{"apiextensions.k8s.io"},
				Resources: []string{"customresourcedefinitions"},
				Verbs:     []string{"get", "list"},
			},
		},
	}
}

// buildClusterRoleBinding generates a ClusterRoleBinding manifest
func buildClusterRoleBinding(agent *agentv1alpha1.PlatformAgent, bindingName, roleName string) *rbacv1.ClusterRoleBinding {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	return &rbacv1.ClusterRoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: bindingName,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      saName,
				Namespace: agent.Namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "ClusterRole",
			Name:     roleName,
		},
	}
}

// Helper to calculate the SHA256 hash of ConfigMap Data for rolling restarts.
func getConfigMapHash(configMap *corev1.ConfigMap) (string, error) {
	if configMap == nil {
		return "", nil
	}
	dataBytes, err := json.Marshal(configMap.Data)
	if err != nil {
		return "", err
	}
	hash := sha256.Sum256(dataBytes)
	return fmt.Sprintf("%x", hash), nil
}

// buildFluentBitConfigMap generates the ConfigMap manifest containing fluent-bit.conf
func buildFluentBitConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-fluent-bit-config",
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			"fluent-bit.conf": `[SERVICE]
    Flush         1
    Daemon        Off
    Log_Level     info
    Parsers_File  parsers.conf

[INPUT]
    Name              tail
    Tag               agent.logs
    Path              /opt/data/logs/*.log
    DB                /fluent-bit/state/fluent-bit.db
    Refresh_Interval  5
    Rotate_Wait       30
    Mem_Buf_Limit     20MB
    Skip_Long_Lines   On
    Read_from_Head    On
    Path_Key          file_path

[FILTER]
    Name          parser
    Match         agent.logs
    Key_Name      log
    Parser        gchat_event
    Reserve_Data  On
    Preserve_Key  On

[FILTER]
    Name              record_modifier
    Match             agent.logs
    Record            app agent
    Record            log_source agent-file

[OUTPUT]
    Name              stdout
    Match             agent.logs
    Format            json_lines
`,
			"parsers.conf": `[PARSER]
    Name    gchat_event
    Format  regex
    Regex   User=(?<gchat_user>[^,\s]+),\s*Session=(?<gchat_session>[^,\s]+)
`,
		},
	}
}

// buildPlatformService generates the Service manifest for PlatformAgent
func buildPlatformService(agent *agentv1alpha1.PlatformAgent) *corev1.Service {
	selector := map[string]string{
		"app": agent.Name + "-gateway",
	}

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		selector["kubeagents.io/is-leader"] = "true"
	}
	dashboardEnabled := isDashboardEnabled(agent)

	ports := []corev1.ServicePort{
		{
			Name:       "api",
			Port:       8642,
			TargetPort: intstr.FromInt32(8643),
		},
	}

	if dashboardEnabled {
		ports = append(ports, corev1.ServicePort{
			Name:       "dashboard",
			Port:       9119,
			TargetPort: intstr.FromString("dashboard"),
		})
	}

	return &corev1.Service{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "Service",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
		Spec: corev1.ServiceSpec{
			Selector: selector,
			Ports:    ports,
		},
	}
}

// buildPlatformLeaderRole generates the Role manifest for leader election leases in the agent namespace
func buildPlatformLeaderRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "Role",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("kubeagents:leader:%s:%s", agent.Namespace, agent.Name),
			Namespace: agent.Namespace,
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{"coordination.k8s.io"},
				Resources: []string{"leases"},
				Verbs:     []string{"get", "list", "watch", "create", "update", "patch", "delete"},
			},
			{
				APIGroups: []string{""},
				Resources: []string{"pods"},
				Verbs:     []string{"get", "patch"},
			},
		},
	}
}

// buildLeaderRoleBinding generates the RoleBinding manifest for leader election in the agent namespace
func buildLeaderRoleBinding(agent *agentv1alpha1.PlatformAgent, bindingName, roleName string) *rbacv1.RoleBinding {
	saName := agent.Name
	if agent.Spec.Security != nil && agent.Spec.Security.ServiceAccountName != "" {
		saName = agent.Spec.Security.ServiceAccountName
	}

	return &rbacv1.RoleBinding{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "RoleBinding",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      bindingName,
			Namespace: agent.Namespace,
		},
		Subjects: []rbacv1.Subject{
			{
				Kind:      "ServiceAccount",
				Name:      saName,
				Namespace: agent.Namespace,
			},
		},
		RoleRef: rbacv1.RoleRef{
			APIGroup: "rbac.authorization.k8s.io",
			Kind:     "Role",
			Name:     roleName,
		},
	}
}

func isDashboardEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.DashboardEnabled != nil {
		return *agent.Spec.Harness.Hermes.DashboardEnabled
	}
	return true
}

func extractAgentPluginEnvVars(agentPlugins []*agentv1alpha1.AgentPlugin) []corev1.EnvVar {
	var envs []corev1.EnvVar
	for _, plugin := range agentPlugins {
		envs = append(envs, plugin.Spec.Env...)
	}
	return envs
}

func mergeMaps(base, extra map[string]any) map[string]any {
	for k, v := range extra {
		if baseVal, ok := base[k]; ok {
			baseMap := toStrMap(baseVal)
			extraMap := toStrMap(v)
			if baseMap != nil && extraMap != nil {
				base[k] = mergeMaps(baseMap, extraMap)
				continue
			}

			baseSlice, okBase := toSlice(baseVal)
			extraSlice, okExtra := toSlice(v)
			if okBase && okExtra {
				for _, item := range extraSlice {
					if !containsValue(baseSlice, item) {
						baseSlice = append(baseSlice, item)
					}
				}
				base[k] = baseSlice
				continue
			}
		}
		base[k] = v
	}
	return base
}

// containsValue reports whether list already holds an element deep-equal to item.
//
// Not slices.Contains: that compares with ==, which panics when two elements share an
// uncomparable dynamic type. A plugin listing YAML mappings under an allowlisted key —
// perfectly ordinary config — would otherwise panic the reconcile and, since the panic is
// recovered and retried, wedge that PlatformAgent permanently.
func containsValue(list []any, item any) bool {
	for _, existing := range list {
		if reflect.DeepEqual(existing, item) {
			return true
		}
	}
	return false
}

func toStrMap(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	if m, ok := v.(map[any]any); ok {
		res := make(map[string]any)
		for k, val := range m {
			if strK, okStr := k.(string); okStr {
				res[strK] = val
			}
		}
		return res
	}
	return nil
}

func toSlice(v any) ([]any, bool) {
	if s, ok := v.([]any); ok {
		return s, true
	}
	if s, ok := v.([]string); ok {
		res := make([]any, len(s))
		for i, val := range s {
			res[i] = val
		}
		return res, true
	}
	return nil, false
}

//go:embed leader_elect.py
var leaderElectScript string

func buildPluginVolumeName(pluginName string) string {
	name := "plugin-" + pluginName
	if len(name) > 63 {
		hash := fmt.Sprintf("%x", sha256.Sum256([]byte(pluginName)))[:8]
		name = name[:54] + "-" + hash
	}
	return name
}
