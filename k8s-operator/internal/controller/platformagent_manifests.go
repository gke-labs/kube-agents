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
	"net"
	"os"
	"path"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
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

// Shared-state ownership. Step 1.5 of deploy/shared/docker-entrypoint.sh reads this
// variable to decide whether the container it is starting builds the tree on the data
// PVC. Exactly one container per pod may: everything the entrypoint does below that gate
// writes to a tree that several containers mount, and the second writer erases the
// first's plugin links and reverts its config overlay.
//
// The operator names the owner rather than letting the entrypoint infer it from argv. Its
// fallback looks for a bare `gateway` argument, and the gateway container's argv only
// carries one at a single replica — above that it runs leader_elect.py, where `gateway`
// appears nowhere. Auto-detection exists for deployments with no operator to ask
// (compose, plain manifests); here there is one, and it knows.
const (
	sharedStateSetupEnvVar = "AGENT_SHARED_STATE_SETUP"
	sharedStateSetupOwner  = "owner"
	sharedStateSetupSkip   = "skip"
)

// The single model name LiteLLM is configured to serve, used both in the profile
// config the gateway reads and in the API server's own default. The two must agree:
// the API server resolves its model once at startup, and a mismatch means every
// session it creates asks LiteLLM for a model that does not exist.
const agentModelName = "model-default"

// The API server picks its model from API_SERVER_MODEL_NAME, then the active profile
// name, then a hardcoded "hermes-agent". The profile name is skipped for a custom
// provider, so without this the fallback wins and LiteLLM rejects every request the
// API server makes. Chat is unaffected — it resolves per message, not at startup —
// which is why only sessions created through the API fail.
//
// The name is not cosmetic either. `POST /api/sessions` persists what the API server
// advertises into the session row's `model` column whenever the caller does not name one
// (api_server.py `_handle_create_session`: `body.get("model") or self._model_name`), and
// a session-persisted model outranks the config model when the turn is built. Unpinned,
// every session created without an explicit model — which is every Kubernetes-event
// triage session, since scripts/session_kv_server.py posts only an id and a title — died
// with `400 Invalid model name passed in model=hermes-agent` on its first turn. Being
// process-level, the variable corrects the `platform` profile too: that one resolves to
// its own profile name, equally unserved.
const apiServerModelEnvVar = "API_SERVER_MODEL_NAME"

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

// buildConfigMapData renders one config overlay per profile the operator has something
// to say about, including the default profile. Overlays ride in the same ConfigMap so a
// change to any of them moves the existing config hash and rolls the pod — the merge
// happens at startup, so a live update without a restart would be a no-op that silently
// lies.
//
// The default profile's entry is keyed like every other profile's, `profile-default.
// overlay.yaml`, because that is what makes it reachable: docker-entrypoint.sh globs
// $OVERLAY_DIR for that shape. It was previously keyed `config.yaml` and subPath-mounted
// over $HERMES_HOME/config.yaml, which both failed to reach the agent (the entrypoint
// force-copied the image's file over the mount) and made the live config read-only, so
// nothing the agent itself writes there — `/sethome`'s home channel above all — could be
// saved. See renderConfigYAML.
func buildConfigMapData(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) map[string]string {
	data := map[string]string{
		profileOverlayKey(defaultProfileName): renderConfigYAML(agent, agentPlugins),
		"leader_elect.py":                     leaderElectScript,
	}

	_, targeted := partitionPluginsByProfile(filterValidAgentPlugins(agentPlugins))

	// A profile needs an overlay if a plugin targets it OR spec.harness.tuning sets
	// limits for it — tuning alone is enough, so limits can be applied to a profile that
	// hosts no plugins at all.
	profiles := make(map[string]bool, len(targeted)+1)
	for profile := range targeted {
		profiles[profile] = true
	}
	// The platform profile is unconditional: it always carries the memory provider,
	// which follows the CR rather than the copy baked into agents/platform/config.yaml.
	profiles[platformProfileName] = true
	for profile := range profiles {
		// "default" is not a named profile — its key is the whole-config render written
		// above, and letting a plugin reach this loop with that name would replace the
		// entire front-door config with the plugin's overlay. AgentPlugin's CEL rule
		// rejects the value at admission, but a cluster running an older CRD, or one
		// whose apiserver has CEL disabled, would not. Two code paths must never be able
		// to write one ConfigMap key.
		if profile == defaultProfileName {
			continue
		}
		var limits *agentv1alpha1.AgentLimits
		var memory map[string]any
		if profile == platformProfileName {
			limits = platformProfileLimits(agent)
			memory = memoryOverlay(agent)
		}
		if overlay := renderProfileOverlayYAML(targeted[profile], limits, memory); strings.TrimSpace(overlay) != "" {
			data[profileOverlayKey(profile)] = overlay
		}
	}

	// Cluster profiles are named at runtime, so they get one class overlay applied to
	// all of them rather than a file each. No memory subtree: agents/cluster/config.yaml
	// configures no provider at all, on purpose — a cluster agent is spawned by the
	// kanban dispatcher and carries no human identity to scope a store by.
	if overlay := renderProfileOverlayYAML(nil, clusterProfileLimits(agent), nil); strings.TrimSpace(overlay) != "" {
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

// DefaultBuiltInPlugins defines the built-in plugins pre-installed in the Hermes container
// image. This is the roster an AgentPlugin may not shadow (see IsBuiltInPlugin) — being in
// the image anywhere is enough to make a same-named AgentPlugin a collision. It is NOT the
// list to enable on a profile: shadow protection and per-profile enablement answer
// different questions, and a plugin added here for the first must not silently switch
// itself on at the front door.
var DefaultBuiltInPlugins = []string{
	"hermes_otel",
	"session_store",
	"session_otel_bridge",
	"tool_call_audit",
	"incident_context",
	"bootstrap_onboarding",
}

// defaultProfilePlugins is what the DEFAULT profile enables. Every name here resolves for
// it: agents/chat/defaults/plugins/ supplies bootstrap_onboarding, session_otel_bridge,
// session_store and tool_call_audit, the Dockerfile installs hermes_otel into /opt/defaults,
// and incident_context is COPYed to /opt/hermes/plugins — the BUNDLED directory, which
// hermes_cli/plugins.py scans for every HERMES_HOME, not just the platform profile's.
//
// It coincides with DefaultBuiltInPlugins today and is still kept apart, because the two
// lists answer different questions: that one is the shadow-protection roster, this one is
// enablement. A future built-in added for shadow protection alone must not turn itself on
// at the front door. Keep in sync with agents/chat/config.yaml's plugins.enabled, the same
// roster built at image build time — minus its trailing legacy_slash_commands and
// agent_roster, which renderConfigYAML appends. Naming either here enables it twice.
var defaultProfilePlugins = []string{
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

// defaultProfileName is the front-door Chat Agent's profile. Unlike every other profile
// it has no directory under $HERMES_HOME/profiles — its home IS $HERMES_HOME — but it
// takes its config through the same `profile-<name>.overlay.yaml` key as the rest.
const defaultProfileName = "default"

// defaultKanbanMaxInProgress bounds concurrent kanban workers when the CR does not.
//
// Two, not more, because the number has to hold on the smallest pod anyone runs, and
// the cost of being wrong is asymmetric: too low delays a delegated task, too high
// loses it silently to the OOM killer. Two keeps a second card moving while the first
// is mid-triage, which is what unbounded dispatch was buying in practice.
//
// Raise it on spec.harness.tuning.maxInProgress once a deployment has measured its own
// worker footprint and model quota. This is a floor for the untuned case, not a
// recommendation.
const defaultKanbanMaxInProgress = 2

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

// defaultMemoryProvider is the provider a PlatformAgent gets when its spec says
// nothing. It is the per-user file store, which needs nothing running outside the
// pod — the same store this operator gave an agent before the Hindsight-backed
// wrapper existed, so a CR written against the older schema reconciles unchanged
// rather than being pointed at a service the install never deployed. Keep in step
// with the kubebuilder default on MemorySpec.Provider.
const defaultMemoryProvider = "multiuser_memory"

// kubeAgentsMemoryProvider is this repo's slim wrapper around the upstream
// `hindsight` plugin. An install opts into it; nothing defaults to it.
const kubeAgentsMemoryProvider = "kube_agents_memory"

// memoryProviderNone is how the CR spells "no external memory provider — leave the
// harness with its built-in store".
//
// Hermes spells that as the empty string (`memory.provider: ""`), but an empty
// string cannot express a choice on the way in: a kubebuilder default applies to an
// absent field, so clearing spec.harness.memory.provider hands back
// defaultMemoryProvider rather than turning the provider off. A sentinel is the only
// value that survives the round trip, and the operator translates it back here.
const memoryProviderNone = "none"

// resolveMemoryProvider returns the provider name to render into a config.yaml.
func resolveMemoryProvider(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Harness == nil || agent.Spec.Harness.Memory == nil {
		return defaultMemoryProvider
	}
	provider := strings.TrimSpace(agent.Spec.Harness.Memory.Provider)
	switch {
	case provider == "":
		return defaultMemoryProvider
	case strings.EqualFold(provider, memoryProviderNone):
		return ""
	default:
		return provider
	}
}

// memoryOverlay renders the `memory` subtree for the platform profile's overlay.
//
// The specialist profiles read shared-scope memory, so they load a provider too — but
// theirs came from the static agents/platform/config.yaml baked into the image, which
// meant an install that chose a different provider (or none at all) still got
// kube_agents_memory on every specialist. The choice lives in the CR, so the operator
// owns this key the same way it owns the execution limits above.
//
// A specialist only gets a provider that can be made read-only and scoped by tag,
// which today means the Hindsight-backed pair. A per-user file provider like
// multiuser_memory keys its store off the gateway identity, and a specialist has none:
// it is spawned by the kanban dispatcher, so every write would land in one anonymous
// `default` bucket and the global MEMORY.md would be writable by a profile nobody is
// supervising. For those the specialists get no provider and read their facts from the
// kanban card, which is what agents/cluster/config.yaml already does.
//
// Only `provider` is written. Whether the specialist may store anything at all
// (memory_enabled, read_only, user_profile_enabled) is a property of the persona, not
// of the install, and stays in the image's config.yaml.
func memoryOverlay(agent *agentv1alpha1.PlatformAgent) map[string]any {
	provider := resolveMemoryProvider(agent)
	if !memoryProviderIsHindsightBacked(provider) {
		provider = ""
	}
	return map[string]any{
		"memory": map[string]any{"provider": provider},
	}
}

// memoryProviderIsHindsightBacked reports whether a provider talks to the in-cluster
// Hindsight service. Keep in sync with memory_provider_uses_hindsight in
// k8s-operator/scripts/common.sh, which decides whether to deploy it.
func memoryProviderIsHindsightBacked(provider string) bool {
	switch strings.ToLower(strings.TrimSpace(provider)) {
	case kubeAgentsMemoryProvider, "hindsight":
		return true
	default:
		return false
	}
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
func renderProfileOverlayYAML(plugins []*agentv1alpha1.AgentPlugin, limits *agentv1alpha1.AgentLimits, memory map[string]any) string {
	overlay := map[string]any{}

	// Operator-owned execution limits from spec.harness.tuning. Written before the
	// plugin contributions so a plugin cannot displace them; the allowlist already
	// drops `agent` from plugin config, and this ordering makes that belt-and-braces.
	if agentOverlay := agentLimitsOverlay(limits); agentOverlay != nil {
		overlay = mergeMaps(overlay, agentOverlay)
	}

	// Operator-owned memory settings, for the same reason and with the same ordering.
	if memory != nil {
		overlay = mergeMaps(overlay, memory)
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

// renderConfigYAML builds the default (Chat Agent) profile's config overlay.
//
// It is emitted as `profile-default.overlay.yaml`, the same ConfigMap key shape every
// other profile's overlay uses, and reaches the agent the same way: the ConfigMap is
// mounted read-only at /opt/agent-config and docker-entrypoint.sh step 2d merges this
// file onto the image's agents/chat/config.yaml.
//
// MERGED, not mounted over. Two earlier arrangements failed and the merge is what
// replaced them. Mounting this rendering over $HOME/config.yaml made the file read-only,
// so the agent could no longer save its own settings there — `/sethome` returned EACCES —
// and the entrypoint force-copied the image's config over the mount anyway, so none of
// the keys below ever reached a running pod. Letting the mount simply win was not the
// answer either: this is a whole-file rendering but it is not always a superset of the
// image's config. platforms.google_chat.typing_status_text is only rendered when the CR
// enables Google Chat, so on a Slack-only deployment an authoritative replacement would
// drop a setting rather than add one.
//
// What "merged" means for the keys here: the operator wins every scalar it renders, the
// image keeps every key the operator says nothing about, and list-valued keys UNION.
// Union has no way to express a removal, which matters because the lists below are
// duplicated in agents/chat/config.yaml — drop an entry from one copy only and the other
// puts it straight back. TestRenderConfigYAMLListsMatchChatConfig fails the build when
// the two drift, which is the only thing keeping that honest.
//
// Runtime state — a home channel, an install id, saved preferences — survives the merge
// because step 2d carries the live file's own edits across; see
// deploy/shared/default_profile_config.py for the rules.
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
			// Hermes' Python-toolchain probe, which this deployment always wants
			// off — see the rationale in deploy/shared/defaults/config.yaml. No
			// omitempty: upstream defaults the key to true, so `false` has to be
			// written out to mean anything.
			EnvironmentProbe bool `json:"environment_probe"`
		} `json:"agent,omitempty"`
		Kanban struct {
			DispatchInGateway       bool `json:"dispatch_in_gateway"`
			AutoSubscribeOnCreate   bool `json:"auto_subscribe_on_create"`
			DispatchIntervalSeconds int  `json:"dispatch_interval_seconds"`
			// Live concurrency cap across the whole board (not a per-tick
			// spawn budget). Every worker shares one LiteLLM/Vertex quota and
			// one container memory limit, so this is always rendered — see
			// defaultKanbanMaxInProgress. omitempty is retained only as a guard
			// against a future zero value reaching the ConfigMap: Hermes ignores
			// anything below 1, so `max_in_progress: 0` would read as a serial
			// board while behaving as an unbounded one.
			MaxInProgress int `json:"max_in_progress,omitempty"`
			// Terminal event kinds that wake the card's creator for a follow-up
			// turn. Read by the image patch in
			// deploy/docker/patches/kanban_notifier.py; upstream Hermes
			// hardcodes the set and ignores this key. omitempty so an unset
			// value leaves upstream behaviour rather than emitting an empty
			// list, which the patch reads as "never wake".
			WakeOnEvents []string `json:"wake_on_events,omitempty"`
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
				// Adapter presentation knobs, passed through to the Slack plugin
				// untouched. Carries `rich_blocks` — see the note where it is set.
				Extra map[string]any `json:"extra,omitempty"`
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
	cfg.Model.Default = agentModelName
	cfg.Model.Model = agentModelName
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
			// Left as a placeholder rather than joined against cwd, and this is NOT
			// cosmetic. Unlike every other profile, the default profile's config.yaml
			// is not this render — it is agents/chat/config.yaml MERGED with this one
			// (default_profile_config.py), and profile_overlay.merge unions lists.
			// `args` is a command line, so a union is a concatenation: the moment the
			// two declarations disagree the router is invoked with two script paths
			// and python3 runs the FIRST one. path.Join(cwd, …) disagrees for exactly
			// the case it was added to serve — a custom AgentHome, where the image's
			// literal /opt/data/scripts/router_server.py sorts first and does not
			// exist, because the entrypoint copied /opt/defaults into the custom home
			// instead. The router MCP then dies at startup and the Chat Agent has no
			// specialist roster to delegate against.
			//
			// ${HERMES_HOME} keeps both sides byte-identical so the union collapses to
			// one entry for any AgentHome, and each side is independently correct —
			// which the image's copy has to be anyway, since the entrypoint seeds a
			// fresh PVC from it before this render is merged in. The entrypoint
			// exports HERMES_HOME=${PLATFORM_AGENT_HOME:-/opt/data} on line 5, the
			// operator sets PLATFORM_AGENT_HOME from the same AgentHome that produced
			// cwd, and tools/mcp_tool.py `_interpolate_env_vars` resolves ${VAR}
			// recursively through `args` — the sibling `env` below already relies on
			// exactly that.
			//
			// TestRenderConfigYAMLListsMatchChatConfig compares every rendered list
			// against the image's, under a custom AgentHome as well as the default.
			"args": []string{"${HERMES_HOME}/scripts/router_server.py"},
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
	// `memory` here is a GATE for the memory provider, not a tool grant. The check
	// is provider-agnostic: it applies to whatever cfg.Memory.Provider names below.
	// hermes_cli.tools_config._get_platform_tools() resolves this list for the
	// session's platform key and subtracts agent.disabled_toolsets LAST; what
	// survives becomes agent.enabled_toolsets. inject_memory_provider_tools()
	// then bails unless memory_provider_tools_enabled() sees "memory" there, and
	// that injection is the only path by which the provider's tools reach the
	// model. So `memory` must be listed HERE and must NOT be in DisabledToolsets
	// below — listing it in both nets to off (the subtraction wins), which is why
	// the front door had no working memory despite the provider loading.
	//
	// Price: the built-in `memory` tool is exposed alongside the provider's own
	// tools. It is inert — MemoryEnabled=false leaves agent._memory_store nil and
	// tools/memory_tool.py returns "Memory is not available" without touching
	// disk. SOUL.md §1.6 tells the agent to ignore it.
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
	// Which terminal events wake the front door for a follow-up turn. Upstream
	// wakes on all five and hardcodes the set; the image patches the key in
	// (deploy/docker/patches/kanban_notifier.py).
	//
	// `completed` is deliberately absent. By the time the notifier wakes anyone
	// it has already sent the worker's own status line and its full `result` to
	// the thread, so the woken turn re-reads the card and paraphrases a message
	// the user is looking at — measured at 5.9s and 32,460 input tokens on task
	// t_c31a1f00, and a paraphrase of a verbatim answer can only lose detail.
	// The failure kinds stay: those deliver a bare status line, and the front
	// door has to decide whether to retry, escalate, or explain.
	cfg.Kanban.WakeOnEvents = []string{"gave_up", "crashed", "timed_out", "blocked"}
	// Dispatch concurrency defaults to a cap rather than to upstream's unbounded
	// behaviour. A kanban worker here is not a coroutine: it is a full
	// `hermes -p <profile> ... kanban task` process — measured at ~340 Mi resident once
	// its MCP proxies are up, and alive for the 8-14 minutes an incident triage took on
	// the deployment where this was diagnosed. Unbounded
	// dispatch therefore spawns one such process per queued card, and a burst of
	// cluster events queues them faster than they retire.
	//
	// The failure that follows is silent by construction. The cgroup OOM killer takes
	// a child process, not PID 1, so there is no container restart, no Kubernetes
	// event, and no non-zero exit anywhere the operator can see — only `pid not alive`
	// in the kanban ledger. The dispatcher's own retry budget is 1, so the card is then
	// stranded rather than re-dispatched, and the work it stood for is simply never
	// done.
	//
	// The cap is deliberately below what memory alone would allow. Model quota is the
	// other shared resource and it binds first for most deployments, so the default is
	// chosen to be safe on a small pod rather than optimal on a large one — a fleet
	// with headroom raises it on the CR, which still wins outright below.
	cfg.Kanban.MaxInProgress = defaultKanbanMaxInProgress
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
	// inject_memory_provider_tools(), and silently kill the memory provider — it
	// would still load and log that it registered its tools while never
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
	// Explicit rather than relying on the zero value: this is a deliberate
	// override of an upstream default that is true, not an unset field.
	cfg.Agent.EnvironmentProbe = false

	// Execution limits are NOT pinned here: Hermes' own defaults apply unless a
	// deployment opts in. What a given fleet needs depends on its model quota and on
	// what its agents actually do, so the values belong in the CR rather than baked
	// into every deployment. spec.harness.tuning.default sets them for the front door.
	// The default profile takes them here rather than in a separate overlay: this
	// rendering IS that profile's overlay, so there is no second file to put them in.
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
	// The plugins the default profile enables, plus two that ride on the default profile
	// specifically:
	//
	//   legacy_slash_commands hooks pre_gateway_dispatch on inbound chat messages so a
	//   typed "/hermes sethome" reaches the gateway command dispatcher instead of drawing
	//   an unknown-command reply — chat ingress lands here, not on the platform specialist.
	//
	//   agent_roster hooks pre_llm_call to inject the list of routable specialists into
	//   every turn. The front door cannot delegate without naming an assignee, and it was
	//   spending a full LLM roundtrip on the list_agents tool to re-read what amounts to a
	//   directory listing; the tool remains as the refresh path.
	//
	// Neither is in defaultProfilePlugins, because that list is ordered to mirror
	// agents/chat/config.yaml, where these two also come last.
	//
	// incident_context must be in the list for the same reason legacy_slash_commands is:
	// it hooks pre_gateway_dispatch on a human's reply in a Slack or Google Chat incident
	// thread, and the pod runs one gateway, homed at the default profile. Enabling it on
	// the platform profile alone leaves the hook with no ingress to see. It sorts ahead of
	// legacy_slash_commands here, which is safe either way: it returns early on a leading
	// "/" so the slash-command unwrap still sees the raw text.
	//
	// Built from defaultProfilePlugins, NOT DefaultBuiltInPlugins: the latter is the
	// image-wide roster an AgentPlugin may not shadow. The two coincide today, and
	// conflating them would enable the next shadow-protected built-in by accident.
	// Keep in sync with agents/chat/config.yaml — this copy is authoritative on the
	// deployed default profile.
	cfg.Plugins.Enabled = append(slices.Clone(defaultProfilePlugins), "legacy_slash_commands", "agent_roster")
	cfg.Display.Platforms = map[string]map[string]any{}
	// Memory. The built-in MEMORY.md/USER.md store stays off; the bundled
	// kube_agents_memory provider replaces it. It wraps one Hindsight instance
	// talking HTTP to a self-hosted Hindsight API, and keeps everyone's memories
	// in a single bank separated by a scope tag: "user:<id>" resolved from the
	// gateway identity (agent._user_id) for private facts, "scope:shared" for
	// organisation-wide ones. Both are recalled into the prompt each turn; only
	// the personal scope retains automatically at session end. This is the only
	// profile that gets it: kanban-spawned specialists carry no human identity,
	// and the provider fails closed there rather than collapsing their writes
	// into one anonymous bucket.
	cfg.Memory.MemoryEnabled = false
	cfg.Memory.Provider = resolveMemoryProvider(agent)
	cfg.Memory.UserProfileEnabled = false

	if agent.Spec.Harness != nil && agent.Spec.Harness.Memory != nil {
		if agent.Spec.Harness.Memory.MemoryEnabled != nil {
			cfg.Memory.MemoryEnabled = *agent.Spec.Harness.Memory.MemoryEnabled
		}
		if agent.Spec.Harness.Memory.UserProfileEnabled != nil {
			cfg.Memory.UserProfileEnabled = *agent.Spec.Harness.Memory.UserProfileEnabled
		}
	}

	// Keeping `memory` out of DisabledToolsets is only safe while the built-in
	// store is off. memoryEnabled is a supported CRD field, and setting it true
	// would leave the front door holding a live built-in `memory` tool — a real
	// read/write surface over a single MEMORY.md/USER.md pair with no per-user
	// scoping, which is precisely what the per-user provider exists to avoid. There is
	// no way to have one without the other: the same toolset name gates the
	// provider injection and exposes the built-in tool. So when the built-in
	// store is switched on, put `memory` back in the denylist. Both memory tools
	// then disappear from the front door — the behaviour this field already had
	// before the gate was opened, and better than two competing stores on a
	// profile whose whole point is a minimal tool surface.
	//
	// userProfileEnabled has to be tested too, and it is easy to miss: Hermes
	// constructs the store when EITHER flag is set (agent_init.py builds
	// MemoryStore on `_memory_enabled or _user_profile_enabled`), and the
	// built-in tool checks only that the store exists — it has no per-target
	// gate. So userProfileEnabled alone makes the tool live for MEMORY.md as
	// well as USER.md.
	if cfg.Memory.MemoryEnabled || cfg.Memory.UserProfileEnabled {
		cfg.Agent.DisabledToolsets = append(cfg.Agent.DisabledToolsets, "memory")
	}

	// Render outbound Slack messages as Block Kit rather than one flat mrkdwn
	// string. SlackAdapter.format_message already rewrites the inline markdown an
	// agent emits (`**bold**` → `*bold*`, `[label](url)` → `<url|label>`), so prose
	// has always arrived readable; what it cannot rewrite is structure, because flat
	// mrkdwn has none. A pipe table ships as literal `|---|` rows, `---` stays three
	// hyphens, a heading flattens into bold, and a nested list loses its indentation
	// — and a fleet report handed to the kanban notifier is exactly that shape. With
	// this on, block_kit.render_blocks emits real header/divider/table/rich_text
	// blocks instead. It degrades safely: a `text` fallback always ships alongside,
	// and the renderer declines (falling back to the flat string) for anything past
	// Slack's 50-block cap or its table limits.
	//
	// Set unconditionally, unlike Google Chat's typing text above. It is inert while
	// Slack is off, and rendering it regardless means the setting cannot be missed by
	// whichever path ends up turning Slack on. Kept in sync with the same block in
	// agents/chat/config.yaml, which carries the full note.
	cfg.Platforms.Slack.Extra = map[string]any{"rich_blocks": true}

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

// renderOptions carries cluster-resolved facts the manifest builders cannot work out for
// themselves: they take no client and must stay pure so the golden tests can render them
// without an API server. The controller resolves each field once per reconcile and passes
// the answers down.
//
// A struct rather than more positional parameters — the builders already take four
// same-typed hash strings, and an endpoint string added to that list could be transposed
// with one of them and still compile.
type renderOptions struct {
	// imageVolumeSupported reports whether the cluster can mount plugin image volumes.
	imageVolumeSupported bool
	// otlpEndpoint is the resolved OpenTelemetry collector base URL. Empty means the GKE
	// managed collector, so the zero value is the historical behaviour.
	otlpEndpoint string
}

// buildPodTemplateSpec generates the shared PodTemplateSpec for Deployment and StatefulSet
func buildPodTemplateSpec(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, opts renderOptions) corev1.PodTemplateSpec {
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
		// API_SERVER_MODEL_NAME belongs here by topic but is appended after the
		// env merge instead — see buildBaseContainers, and apiServerModelEnvVar
		// for why an override of it must not win.
		{
			Name:  "SESSION_KV_DB_PATH",
			Value: sessionKVDBPath,
		},
	}

	// The two exceptions to "no credentials in the sandbox", both of them
	// pod-scoped and useless outside this pod's loopback interface:
	//
	//   SESSION_KV_API_KEY  authenticates callers of the Session KV server on
	//                       127.0.0.1:8699. This container both serves it and
	//                       calls it (platform_mcp_server, incident_context).
	//   SESSION_KV_SALT     the HMAC salt for pseudonymising chat identities.
	//                       It has to be here because the hashing happens here,
	//                       at the point the identity is first seen.
	//
	// Neither grants access to any cloud API, any repository, or anything
	// outside the pod, which is the property the isolation boundary protects.
	// See docs/credential-isolation-design.md.
	envVars = append(envVars,
		corev1.EnvVar{
			Name:      "SESSION_KV_API_KEY",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: sessionKVApiKeySecretRef(agent)},
		},
		corev1.EnvVar{
			Name:      "SESSION_KV_SALT",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: sessionKVSaltSecretRef(agent)},
		},
	)

	envVars = append(envVars, otelTelemetryEnvVars("platform", agent.Name, agent.Namespace, opts.otlpEndpoint)...)
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
	// The memory provider's endpoint, derived from the namespace the same way the
	// model endpoint is (cfg.Model.BaseURL above) — the two are the same class of
	// value and had drifted into two mechanisms, one namespace-aware and one a
	// baked literal. The image-owned hindsight/config.json deliberately carries no
	// `api_url` so this wins: the plugin reads the file first and the environment
	// only as a fallback, so a value left in the file would silently outrank this.
	// Set unconditionally rather than gated on the provider — the variable is inert
	// unless a Hindsight-backed provider loads, and gating it would make the
	// endpoint depend on a field the CR may override to something unrelated.
	// Kanban workers are subprocesses of this container, so their platform profile
	// inherits it and needs no second copy.
	envVars = append(envVars, corev1.EnvVar{
		Name:  "HINDSIGHT_API_URL",
		Value: fmt.Sprintf("http://hindsight-api.%s.svc.cluster.local:8888", agent.Namespace),
	})

	// The effective memory provider, for the entrypoint rather than for Hermes —
	// Hermes reads it from the rendered config.yaml. The entrypoint needs it before
	// that file is in play, to decide whether to run the one-way import that moves a
	// file-based MEMORY.md into the provider and unlinks the original. Gating that on
	// the presence of hindsight/config.json (an image-owned file, always present) meant
	// it ran for everyone, including installs that had deliberately not chosen a
	// Hindsight-backed provider. Empty here means the CR asked for no provider, which
	// is a real answer and distinct from the variable being absent.
	envVars = append(envVars, corev1.EnvVar{
		Name:  "MEMORY_PROVIDER",
		Value: resolveMemoryProvider(agent),
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

	containers := buildBaseContainers(agent, image, envVars, agentPlugins, opts.imageVolumeSupported)
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
		if opts.imageVolumeSupported {
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

	// The recommended labels are set here as well as on the workload, so the
	// pods themselves are selectable. "app" stays out of commonLabels because
	// the Deployment and StatefulSet selectors match on it and selectors are
	// immutable once created.
	podLabels := commonLabels(agent)
	podLabels["app"] = agent.Name + "-gateway"
	podLabels["kubeagents.x-k8s.io/has-credential-proxy"] = "true"

	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{
			Labels:      podLabels,
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
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, opts renderOptions) *appsv1.Deployment {
	replicas, strategy := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash, agentPlugins, opts)

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
func buildStatefulSet(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, opts renderOptions) *appsv1.StatefulSet {
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash, agentPlugins, opts)
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
			MountPath: fmt.Sprintf("%s/leader_elect.py", homeDir),
			SubPath:   "leader_elect.py",
		},
		// config.yaml is deliberately NOT mounted here. A subPath mount is a read-only
		// mount POINT, and this is the one file the running agent writes to — `/sethome`
		// persisting a home channel, the monitoring policy minting an install id, saved
		// slash-command preferences. Mounting it made every one of those fail with
		// EACCES. The rendering reaches the agent through the read-only directory mount
		// below instead, as `profile-default.overlay.yaml`, and docker-entrypoint.sh step
		// 2d merges it into a real, writable file on the PVC.
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

// resolveHarnessClusterName names the cluster the agent itself runs on.
func resolveHarnessClusterName(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Harness != nil && agent.Spec.Harness.ClusterName != "" {
		return agent.Spec.Harness.ClusterName
	}
	return "platform-agent-host"
}

// eventWatcherEnabled reports whether the credential sidecar should start the
// k8s-event-watcher. Absent means started: the watcher is how a fleet notices its
// own incidents, so an install that never mentions the field must keep watching,
// and only an explicit false turns it off. The CRD's own default=true covers the
// case where the object is written without its `enabled` key; this covers the case
// where the object is not written at all, which is every install today.
func eventWatcherEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if harness := agent.Spec.Harness; harness != nil && harness.EventWatcher != nil && harness.EventWatcher.Enabled != nil {
		return *harness.EventWatcher.Enabled
	}
	return true
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
	// The one piece of the event watcher's configuration that varies per
	// install. Set unconditionally and from the same resolver the rest of the
	// operator uses, rather than letting the entrypoint fall back to
	// GKE_CLUSTER_NAME: that variable is only set when projectID, location and
	// clusterName are all present, so a CR naming its cluster but omitting the
	// project would silently label every payload and metric with the default
	// name instead of the one the user chose. The watcher's remaining flags
	// describe loopback plumbing inside this container and live in the
	// entrypoint.
	envVars = append(envVars, corev1.EnvVar{Name: "EVENT_WATCHER_CLUSTER_NAME", Value: resolveHarnessClusterName(agent)})
	// The emergency stop from spec.harness.eventWatcher.enabled. Written on every
	// reconcile rather than only when off, so the Deployment answers "is the
	// watcher meant to be running?" without reading the CR — the pod stays Ready
	// either way, so there is otherwise nothing to tell a deliberately silent
	// install from a broken one. Appended after mergeCredentialProxyEnv like the
	// cluster name above, so the name is reserved in that function's explicit
	// list instead: an unreserved name appended here would not shadow a
	// same-named entry in spec.deployment.env, it would sit beside it, and
	// server-side apply refuses a duplicate key in `env`.
	envVars = append(envVars, corev1.EnvVar{Name: "EVENT_WATCHER_ENABLED", Value: strconv.FormatBool(eventWatcherEnabled(agent))})
	return corev1.Container{
		Name:            "envoy-credential-proxy",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		// Starts three peer services: the credential runtime, Envoy, and the
		// k8s-event-watcher. See deploy/shared/start-services.sh.
		Command: []string{"/usr/local/bin/start-services"},
		Env:     envVars,
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
			// Memory request covers the watcher's informer and dedup caches, which
			// scale with the number of watched clusters.
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("150m"), corev1.ResourceMemory: resource.MustParse("384Mi")},
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
			// Default audience, unlike credential-proxy-ksa-token above. This is the
			// token rest.InClusterConfig reads, so it is what lets the watcher cover
			// the management cluster, which never gets a Cluster Agent profile.
			{Name: "event-watcher-ksa-token", MountPath: "/var/run/secrets/kubernetes.io/serviceaccount", ReadOnly: true},
			{Name: "platform-agent-data-vol", MountPath: homeDir},
		},
		SecurityContext: &corev1.SecurityContext{
			AllowPrivilegeEscalation: ptr.To(false), ReadOnlyRootFilesystem: ptr.To(true), Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}
}

// sessionKVApiKeySecretRef resolves the Secret key holding the bearer token for
// the pod-local Session KV server. Both containers that touch that server take
// the value from here, so they cannot disagree about which key is in force.
func sessionKVApiKeySecretRef(agent *agentv1alpha1.PlatformAgent) *corev1.SecretKeySelector {
	if harness := agent.Spec.Harness; harness != nil && harness.Hermes != nil && harness.Hermes.SessionKVApiKeySecretRef != nil {
		return harness.Hermes.SessionKVApiKeySecretRef
	}
	return defaultSecretRef(nil, defaultPlatformAgentSecrets, "SESSION_KV_API_KEY")
}

// sessionKVSaltSecretRef resolves the Secret key holding the identity-hashing
// salt. Optional by construction: a pod that starts without it degrades to a
// per-pod random salt and says so, rather than refusing to serve chat.
func sessionKVSaltSecretRef(agent *agentv1alpha1.PlatformAgent) *corev1.SecretKeySelector {
	if harness := agent.Spec.Harness; harness != nil && harness.Hermes != nil && harness.Hermes.SessionKVSaltSecretRef != nil {
		return harness.Hermes.SessionKVSaltSecretRef
	}
	return defaultSecretRef(nil, defaultPlatformAgentSecrets, "SESSION_KV_SALT")
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
		// Read by the k8s-event-watcher this container hosts, via --token-env.
		// A non-secret loopback sentinel, not a credential; the real secret is
		// API_SERVER_EXTERNAL_KEY below. Declared here rather than appended by
		// the caller so mergeCredentialProxyEnv sees it in the managed set and
		// reserves the name — appending after that call would leave it
		// protected only by its presence in SensitiveEnvVars, which is
		// incidental and would not hold for a name not on that list.
		{Name: "API_SERVER_KEY", Value: "cluster-internal-trusted"},
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
	// The k8s-event-watcher hosted here posts events to the Session KV server
	// in the sandbox container over the shared pod loopback, and that server
	// now authenticates. start-services.sh passes this name to --token-env.
	envVars = append(envVars, corev1.EnvVar{
		Name:      "SESSION_KV_API_KEY",
		ValueFrom: &corev1.EnvVarSource{SecretKeyRef: sessionKVApiKeySecretRef(agent)},
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
		// Both appended by buildCredentialProxySidecar after this merge runs,
		// so neither is in `managed` above and neither reserves its own name.
		// Without them here a same-named entry in spec.deployment.env is kept
		// and the operator's is appended alongside it — two entries with one
		// name. That is not last-wins: `containers[].env` is a listType=map,
		// and server-side apply rejects the whole Deployment rather than
		// resolving the duplicate, so the agent stops reconciling entirely.
		"EVENT_WATCHER_CLUSTER_NAME",
		"EVENT_WATCHER_ENABLED",
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
	// An allowlist, not a denylist: this env reaches the agent sandbox, so a
	// variable earns a place here only if an arbitrary value for it cannot
	// redirect state, grant access, or change what code runs. Telemetry
	// destinations qualify, and so do the alert ceilings — they bound how many
	// notifications the session server posts in a day and nothing else. A
	// path, a credential or an image reference would not.
	allowed := map[string]struct{}{
		"ALERT_DAILY_LIMIT_CRITICAL":  {},
		"ALERT_DAILY_LIMIT_INFO":      {},
		"ALERT_DAILY_LIMIT_WARNING":   {},
		"OTEL_EXPORTER_OTLP_ENDPOINT": {},
		"OTEL_EXPORTER_OTLP_PROTOCOL": {},
		"OTEL_RESOURCE_ATTRIBUTES":    {},
		"OTEL_SERVICE_NAME":           {},
	}
	var result []corev1.EnvVar
	for _, env := range custom {
		// Only literal values are copied. A ValueFrom source can reference a
		// Secret even when its environment variable name is allowlisted.
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
		// The sidecar tag must follow the agent image, which on this path is
		// untagged or digest-pinned without a tag field — i.e. effectively
		// "latest", not the build-injected default version.
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

	// Args, never Command. Command replaces the image ENTRYPOINT
	// (/usr/local/bin/agent-entrypoint), and that script is what makes $HERMES_HOME
	// usable: it seeds the PVC from /opt/defaults, force-syncs scripts/, scaffolds the
	// platform profile, links the targeted plugin volumes, merges the operator's config
	// overlays and starts the Session KV server on 8699 that the event-watcher is pointed
	// at. Setting Command skipped all of it, so a leader-elected gateway came up against
	// an unpopulated home — no scripts/router_server.py for the router MCP server the
	// rendered config.yaml names, no platform profile, no KV server. Leaving Command
	// unset makes leader_elect.py the entrypoint's `exec "$@"` target instead: the setup
	// runs first, then the wrapper starts `hermes gateway run` on top of a built tree.
	var args []string

	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	if replicas > 1 {
		args = []string{"/opt/hermes/.venv/bin/python3", fmt.Sprintf("%s/leader_elect.py", homeDir)}
	}

	if isImageVolumeSupported {
		for _, plugin := range agentPlugins {
			volumeMounts = append(volumeMounts, corev1.VolumeMount{
				Name:      buildPluginVolumeName(plugin.Name),
				MountPath: pluginMountPath(homeDir, plugin),
			})
		}
	}

	// APPENDED LAST, and that position is the guard, not a style choice. It is not routed
	// through mergeEnvVars because this is the operator's own declaration rather than a
	// default a user may replace, and one caller can in fact try: `spec.deployment.env`
	// cannot reach this container (safeSandboxEnvOverrides copies four OTEL_* names and
	// drops the rest), but extractAgentPluginEnvVars copies an AgentPlugin's spec.env
	// verbatim into envVars with no allowlist at all. A plugin naming this variable would
	// otherwise turn the shared-state setup off for the whole agent, and the symptom —
	// plugins mounted but never enabled — would look like the plugin was broken rather
	// than the cause. Appending after the merge leaves the operator's entry last, and the
	// kubelet collapses duplicate env names last-wins. Same mechanism, same reason, as
	// CREDENTIAL_PROXY_URL in buildPodTemplateSpec; both are pinned by tests, because a
	// reordering here is silent.
	gatewayEnvVars := append(append([]corev1.EnvVar{}, envVars...), corev1.EnvVar{
		Name:  sharedStateSetupEnvVar,
		Value: sharedStateSetupOwner,
	}, corev1.EnvVar{
		// Appended after the merge for the same reason as the variable above: it has
		// to agree with the model in the generated profile config, and an override
		// that disagrees breaks every API-created session rather than failing visibly.
		Name:  apiServerModelEnvVar,
		Value: agentModelName,
	})

	containers := []corev1.Container{
		{
			Name:            "platform-agent",
			Image:           image,
			ImagePullPolicy: pullPolicy,
			Args:            args,
			Ports: []corev1.ContainerPort{
				{
					Name:          "api",
					ContainerPort: 8642,
				},
			},
			Env:          gatewayEnvVars,
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
			{
				// This container runs the same image, and so the same entrypoint, against
				// the same data PVC as the gateway — but without the plugin image volumes
				// or the overlay ConfigMap, which are mounted into the gateway container
				// only. The setup code therefore sees a different world here, and running
				// it undoes the gateway's pass: its prune_stale_links() reads the
				// gateway's fresh plugin link as dangling because the target path does not
				// exist in this container and removes it, and the overlay merge finds no
				// source directory and reverts what was already applied. The symptom lands
				// far away, as a kanban worker exiting with "Unknown skill(s)".
				Name:  sharedStateSetupEnvVar,
				Value: sharedStateSetupSkip,
			},
			// The skip above keeps this container out of the shared tree; this flag
			// answers the entrypoint's OTHER ownership question — which container of
			// the pod owns the per-pod singletons a lock cannot serialise. That is
			// the session KV server's fixed port (one process may hold :8699) and
			// the OTel service-name stamp, which this container would otherwise
			// blank because it has no OTEL_SERVICE_NAME of its own. It is `sidecar`
			// here and unset on the agent container, so an image running anywhere
			// else — plain docker, the kustomize bases, a cluster profile — is the
			// primary by default.
			{
				Name:  "PLATFORM_AGENT_ROLE",
				Value: "sidecar",
			},
		}

		dashboardVolumeMounts := []corev1.VolumeMount{
			{
				Name:      "platform-agent-data-vol",
				MountPath: homeDir,
			},
			{
				// The operator's whole-file rendering of the default profile's config,
				// mounted AS config.yaml even though its ConfigMap key is the
				// profile-default overlay: nothing else puts a config on the PVC for this
				// container to find before the gateway's setup pass lands one there. The
				// gateway takes the same rendering through the /opt/agent-config
				// directory mount instead and merges it into a real, writable file on the
				// PVC — mounting it over the gateway's config.yaml made that file
				// read-only, which is why this subPath mount exists only here, on a
				// container that never writes it. The dashboard used to write one itself,
				// as a side effect of running a setup pass it must no longer run; on a
				// fresh PVC that leaves `hermes dashboard` starting against a HERMES_HOME
				// with no config at all. An existing PVC hides this — it already carries
				// the file — which is why a live-cluster check would not surface it.
				//
				// This closes the config.yaml hole, not the ordering one behind it. The
				// file is now always present, but it names scripts/router_server.py and a
				// plugins.enabled list that still arrive only when the gateway's setup
				// pass lands them, and nothing sequences the two containers. On a fresh
				// volume the dashboard can therefore read a config that is ahead of the
				// tree; with no probes on this container that costs a restart or two
				// against the kubelet's backoff. Making it an ordering rather than a race
				// means moving the setup into an initContainer — see the KNOWN LIMIT note
				// at step 1.5 of deploy/shared/docker-entrypoint.sh.
				Name:      "platform-agent-config-vol",
				MountPath: fmt.Sprintf("%s/config.yaml", homeDir),
				SubPath:   profileOverlayKey(defaultProfileName),
			},
			{
				Name:      "system-metadata",
				MountPath: path.Dir(sessionKVDBPath),
				SubPath:   "session",
			},
		}

		// What keeps this container out of the shared tree is AGENT_SHARED_STATE_SETUP
		// above, not these Args. The entrypoint's argv fallback would also exclude
		// `hermes dashboard`, but only by accident of the word `gateway` being absent —
		// which is how the leader-election gateway used to be excluded too.
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

	// The k8s-event-watcher is not a container of its own. It runs inside
	// envoy-credential-proxy, which holds the credentials it needs to reach
	// cluster API servers; see buildCredentialProxySidecar.

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

// buildMinimalPlatformRole generates the minimal read-only audit ClusterRole manifest
func buildMinimalPlatformRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.ClusterRole {
	return &rbacv1.ClusterRole{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "ClusterRole",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name: fmt.Sprintf("kubeagents:minimal:%s:%s", agent.Namespace, agent.Name),
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{""},
				Resources: []string{"nodes", "namespaces", "pods", "pods/log", "services", "endpoints", "events", "persistentvolumes", "persistentvolumeclaims", "resourcequotas", "limitranges", "configmaps", "serviceaccounts"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"metrics.k8s.io"},
				Resources: []string{"nodes", "pods"},
				Verbs:     []string{"get", "list"},
			},
			{
				APIGroups: []string{"apps"},
				Resources: []string{"deployments", "statefulsets", "daemonsets", "replicasets"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"batch"},
				Resources: []string{"jobs", "cronjobs"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"networking.k8s.io"},
				Resources: []string{"networkpolicies", "ingresses"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"autoscaling"},
				Resources: []string{"horizontalpodautoscalers"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"policy"},
				Resources: []string{"poddisruptionbudgets"},
				Verbs:     []string{"get", "list", "watch"},
			},
			{
				APIGroups: []string{"apiextensions.k8s.io"},
				Resources: []string{"customresourcedefinitions"},
				Verbs:     []string{"get", "list", "watch"},
			},
		},
	}
}

// buildPlatformLocalRole generates a namespace-scoped Role manifest for managing PlatformAgent CRs
func buildPlatformLocalRole(agent *agentv1alpha1.PlatformAgent) *rbacv1.Role {
	return &rbacv1.Role{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "rbac.authorization.k8s.io/v1",
			Kind:       "Role",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("kubeagents:local:%s:%s", agent.Namespace, agent.Name),
			Namespace: agent.Namespace,
		},
		Rules: []rbacv1.PolicyRule{
			{
				APIGroups: []string{"kubeagents.x-k8s.io"},
				Resources: []string{"platformagents", "platformagents/status"},
				Verbs:     []string{"get", "list", "watch"},
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
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      agent.Name,
				"kubeagents.x-k8s.io/agent-namespace": agent.Namespace,
			},
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

// buildRoleBinding generates a RoleBinding manifest
func buildRoleBinding(agent *agentv1alpha1.PlatformAgent, bindingName, roleName string) *rbacv1.RoleBinding {
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
			Labels: map[string]string{
				"kubeagents.x-k8s.io/agent-name":      agent.Name,
				"kubeagents.x-k8s.io/agent-namespace": agent.Namespace,
			},
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

func isFQDNNetworkPolicyEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Annotations != nil {
		if val, ok := agent.Annotations[AnnotationEnableFQDNNetworkPolicy]; ok {
			return val == "true"
		}
	}
	return false
}

// buildFQDNNetworkPolicy generates the companion FQDNNetworkPolicy (networking.gke.io/v1alpha1)
// for GKE Dataplane V2 clusters when enable-fqdn-network-policy annotation is set.
func buildFQDNNetworkPolicy(agent *agentv1alpha1.PlatformAgent) *unstructured.Unstructured {
	patterns := []string{
		// Google APIs & GCP Services (Vertex AI, GKE, Cloud Logging/Monitoring, Workload Identity)
		"googleapis.com",
		"*.googleapis.com",
		"accounts.google.com",
		"*.gstatic.com",
		// Container & Artifact Registries (Plugin OCI images)
		"gcr.io",
		"*.gcr.io",
		"pkg.dev",
		"*.pkg.dev",
		// GitOps & Source Control
		"github.com",
		"*.github.com",
		"*.githubusercontent.com",
		// Chat Integrations
		"slack.com",
		"*.slack.com",
		"*.slack-edge.com",
		"*.slack-msgs.com",
	}

	matches := make([]interface{}, 0, len(patterns))
	for _, p := range patterns {
		matches = append(matches, map[string]interface{}{
			"pattern": p,
		})
	}

	return &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "networking.gke.io/v1alpha1",
			"kind":       "FQDNNetworkPolicy",
			"metadata": map[string]interface{}{
				"name":      agent.Name + "-fqdn-netpol",
				"namespace": agent.Namespace,
				"labels": map[string]interface{}{
					"app": agent.Name + "-gateway",
				},
			},
			"spec": map[string]interface{}{
				"podSelector": map[string]interface{}{
					"matchLabels": map[string]interface{}{
						"app": agent.Name + "-gateway",
					},
				},
				"egress": []interface{}{
					map[string]interface{}{
						"matches": matches,
						"ports": []interface{}{
							map[string]interface{}{
								"protocol": "TCP",
								"port":     int64(443),
							},
						},
					},
				},
			},
		},
	}
}

func isDashboardEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if agent != nil && agent.Spec.Harness != nil && agent.Spec.Harness.Hermes != nil && agent.Spec.Harness.Hermes.DashboardEnabled != nil {
		return *agent.Spec.Harness.Hermes.DashboardEnabled
	}
	return true
}

// otlpCollectorNamespace extracts the target namespace from an OTLP endpoint URL.
func otlpCollectorNamespace(endpoint string) string {
	if endpoint == "" {
		return "gke-managed-otel"
	}
	host := strings.TrimPrefix(endpoint, "https://")
	host = strings.TrimPrefix(host, "http://")
	host = strings.SplitN(host, "/", 2)[0]
	host = strings.SplitN(host, ":", 2)[0]
	parts := strings.Split(host, ".")
	if len(parts) == 2 || (len(parts) >= 3 && parts[2] == "svc") {
		return parts[1]
	}
	return ""
}

// formatCIDRPeers normalises a mix of bare IPs and CIDRs into sorted, deduplicated
// NetworkPolicyPeers. A bare IP becomes a single-host /32 or /128; a CIDR is kept as
// written. Anything unparseable is dropped.
//
// enforceMinPrefix rejects CIDRs broader than /12 (IPv4) or /48 (IPv6), which stops a
// caller-supplied range from being weaponised into an unrestricted egress bypass. Pass
// false only where the input cannot come from outside the operator.
func formatCIDRPeers(raw []string, enforceMinPrefix bool) []networkingv1.NetworkPolicyPeer {
	seen := make(map[string]bool, len(raw))
	var cidrs []string
	add := func(cidr string) {
		if !seen[cidr] {
			seen[cidr] = true
			cidrs = append(cidrs, cidr)
		}
	}

	for _, entry := range raw {
		entry = strings.TrimSpace(entry)
		if entry == "" {
			continue
		}
		if strings.Contains(entry, "/") {
			_, ipNet, err := net.ParseCIDR(entry)
			if err != nil {
				continue
			}
			if enforceMinPrefix {
				ones, bits := ipNet.Mask.Size()
				if (bits == 32 && ones < minIPv4CIDRPrefix) || (bits == 128 && ones < minIPv6CIDRPrefix) {
					continue
				}
			}
			add(ipNet.String())
			continue
		}
		bare := strings.Trim(entry, "[]")
		ip := net.ParseIP(bare)
		if ip == nil {
			continue
		}
		if ip.To4() != nil {
			add(bare + "/32")
		} else {
			add(bare + "/128")
		}
	}

	sort.Strings(cidrs)
	peers := make([]networkingv1.NetworkPolicyPeer, 0, len(cidrs))
	for _, cidr := range cidrs {
		peers = append(peers, networkingv1.NetworkPolicyPeer{
			IPBlock: &networkingv1.IPBlock{CIDR: cidr},
		})
	}
	return peers
}

// buildNetworkPolicy generates the restrictive NetworkPolicy manifest for PlatformAgent.
// Note: This is the operator-generated version; Kustomize static deployments use deploy/kustomize/platform/.
func buildNetworkPolicy(agent *agentv1alpha1.PlatformAgent, apiCIDRs []string, dnsClusterIP string, fqdnEnabled bool, otlpEndpoint string, metadataNodeIPs []string) *networkingv1.NetworkPolicy {
	udp := corev1.ProtocolUDP
	tcp := corev1.ProtocolTCP

	dnsClusterIP = strings.Trim(dnsClusterIP, "[]")
	if dnsClusterIP == "" || net.ParseIP(dnsClusterIP) == nil {
		dnsClusterIP = "10.96.0.10"
	}
	dnsCidr := dnsClusterIP + "/32"
	if strings.Contains(dnsClusterIP, ":") {
		dnsCidr = dnsClusterIP + "/128"
	}

	apiPeers := formatCIDRPeers(apiCIDRs, true)
	if len(apiPeers) == 0 {
		apiPeers = formatCIDRPeers([]string{"10.96.0.1"}, true)
	}

	// The link-local address a workload actually connects to. Every datapath rewrites
	// it before the policy is evaluated, so it only ever matches on the pre-DNAT ports.
	linkLocalPeers := formatCIDRPeers([]string{metadataLinkLocalIP}, true)

	// Everything the rewritten packet can be addressed to, all of it on port 988:
	// the metadata daemon's own link-local address on the iptables datapath, and the
	// hosting node's internal IP on Dataplane V2. See metadataDaemonIP.
	metadataDaemonPeers := formatCIDRPeers(append([]string{metadataLinkLocalIP, metadataDaemonIP}, metadataNodeIPs...), true)

	ingressRules := []networkingv1.NetworkPolicyIngressRule{
		{
			From: []networkingv1.NetworkPolicyPeer{
				{
					PodSelector: &metav1.LabelSelector{},
				},
			},
			Ports: []networkingv1.NetworkPolicyPort{
				{
					Protocol: &tcp,
					Port:     ptr.To(intstr.FromInt32(8642)),
				},
				{
					Protocol: &tcp,
					Port:     ptr.To(intstr.FromInt32(8643)),
				},
			},
		},
	}

	if isDashboardEnabled(agent) {
		ingressRules[0].Ports = append(ingressRules[0].Ports, networkingv1.NetworkPolicyPort{
			Protocol: &tcp,
			Port:     ptr.To(intstr.FromInt32(9119)),
		})
	}

	dnsPeers := []networkingv1.NetworkPolicyPeer{
		{
			NamespaceSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"kubernetes.io/metadata.name": "kube-system",
				},
			},
			PodSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"k8s-app": "kube-dns",
				},
			},
		},
		{
			NamespaceSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"kubernetes.io/metadata.name": "kube-system",
				},
			},
			PodSelector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"k8s-app": "node-local-dns",
				},
			},
		},
		{
			IPBlock: &networkingv1.IPBlock{
				CIDR: "169.254.20.10/32",
			},
		},
		{
			IPBlock: &networkingv1.IPBlock{
				CIDR: dnsCidr,
			},
		},
	}

	egressRules := []networkingv1.NetworkPolicyEgressRule{
		// 1. Cluster DNS
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &udp, Port: ptr.To(intstr.FromInt32(53))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(53))},
			},
			To: dnsPeers,
		},
		// 2. GCP Metadata Server, link-local address only. Nothing rewrites a request to
		//    these ports onto another address, so widening this rule would grant the
		//    sandbox reach it never uses.
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(80))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8080))},
			},
			To: linkLocalPeers,
		},
		// 3. GKE Workload Identity host-network daemon (port 988). This is where a
		//    metadata request lands after the node DNATs it, so it has to permit every
		//    rewrite target the datapath can pick.
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(988))},
			},
			To: metadataDaemonPeers,
		},
		// 4. LiteLLM Gateway in the agent namespace (Service port 80, container port 4000, and standalone-replay port 8080)
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(80))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(4000))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8080))},
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					PodSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{
							"app": "litellm",
						},
					},
				},
				{
					PodSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{
							"app": "standalone-replay",
						},
					},
				},
			},
		},
		// 5. vLLM Gemma Server in the agent namespace (Service port 80 and container port 8000)
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(80))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8000))},
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					PodSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{
							"app": "gemma-server",
						},
					},
				},
			},
		},
		// 6. Kubernetes API Server (Control Plane Endpoints and ClusterIP VIP)
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(443))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(6443))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8443))},
			},
			To: apiPeers,
		},
	}

	// 7. External HTTPS (Google APIs, GitHub, etc.)
	// Note: When FQDNNetworkPolicy is enabled on Dataplane V2, this open IPBlock is omitted
	// so domain-level filtering is strictly enforced by FQDNNetworkPolicy.
	if !fqdnEnabled {
		egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(443))},
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					IPBlock: &networkingv1.IPBlock{
						CIDR:   "0.0.0.0/0",
						Except: []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16"},
					},
				},
				{
					IPBlock: &networkingv1.IPBlock{
						CIDR:   "::/0",
						Except: []string{"fc00::/7", "fe80::/10", "ff00::/8"},
					},
				},
			},
		})
	}

	// 8. GKE Managed OpenTelemetry Collector (Trace Export)
	if ns := otlpCollectorNamespace(otlpEndpoint); ns != "" {
		egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(4317))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(4318))},
			},
			To: []networkingv1.NetworkPolicyPeer{
				{
					NamespaceSelector: &metav1.LabelSelector{
						MatchLabels: map[string]string{
							"kubernetes.io/metadata.name": ns,
						},
					},
				},
			},
		})
	}

	// 9. GitHub Token Minter (Minty)
	egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{
			{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8080))},
		},
		To: []networkingv1.NetworkPolicyPeer{
			{
				PodSelector: &metav1.LabelSelector{
					MatchLabels: map[string]string{
						"app": "github-token-minter",
					},
				},
			},
		},
	})

	return &networkingv1.NetworkPolicy{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "networking.k8s.io/v1",
			Kind:       "NetworkPolicy",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway-netpol",
			Namespace: agent.Namespace,
			Labels: map[string]string{
				"app": agent.Name + "-gateway",
			},
		},
		Spec: networkingv1.NetworkPolicySpec{
			PodSelector: metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
			PolicyTypes: []networkingv1.PolicyType{
				networkingv1.PolicyTypeIngress,
				networkingv1.PolicyTypeEgress,
			},
			Ingress: ingressRules,
			Egress:  egressRules,
		},
	}
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
