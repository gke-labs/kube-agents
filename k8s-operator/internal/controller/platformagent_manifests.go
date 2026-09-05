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
	"sort"
	"strconv"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	policyv1 "k8s.io/api/policy/v1"
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
	// agentDataStorageSize sizes the agent's own /opt/data claim, and through
	// shell_sandbox_manifests.go the sandbox's claim at the same path.
	//
	// The two are one constant on purpose. sandbox_mirror.py copies a subset of
	// the agent's volume into the sandbox's on upgrade, so destination >= source
	// makes the copy fit by construction and there is nothing left for a byte
	// budget to decide. Sized apart, the sandbox was 5Gi against the agent's
	// 10Gi and the mirror needed a cap that silently truncated the migration on
	// any install whose working directories were larger than the guess.
	agentDataStorageSize = "10Gi"
	credentialProxyPort  = 8765
	// dashboardPort is the port `hermes dashboard` listens on. It is loopback-only
	// (see the readiness probe in buildBaseContainers), so the container port, the
	// Service port, and the NetworkPolicy rule below all describe a listener that
	// only kubelet's port-forward can reach.
	dashboardPort        = 9119
	tmpScratchVolumeName = "tmp-scratch"

	// sandboxUID is the canonical unprivileged 'hermes' runtime user created in
	// the upstream NousResearch/hermes-agent Dockerfile (line 92). Everything the
	// agent image ships is owned by it, so the sandbox cannot run as anything else.
	sandboxUID = int64(10000)
	// agentFSGroup is the group both containers share. They mount one PVC and each
	// writes files the other has to change — the sandbox creates a leased GitOps
	// directory that the sidecar clones into, and the sidecar writes kubeconfig
	// pins into profile homes the sandbox created — so shared-group write access
	// is what the split UIDs must not take away. fsGroup makes the kubelet
	// group-own the volumes and set setgid on their directories; the two
	// entrypoints run with umask 0002 so files created after mount stay
	// group-writable.
	agentFSGroup = int64(10000)

	// maxAutopilotContainerNameLen is the maximum container name length that avoids
	// exceeding Kubernetes 63-byte annotation key limits when GKE Autopilot / gVisor injects
	// "dev.gvisor.internal.seccomp.<container-name>" (28-byte prefix without slash).
	maxAutopilotContainerNameLen = 35
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

// Which Hermes profile the gateway runs as, when it is not the default one.
//
// Two readers, and both are in the gateway container. leader_elect.py builds the
// `hermes gateway run` argv it supervises, so above one replica the --profile flag
// cannot come from the container args. docker-entrypoint.sh reads it to stop
// force-syncing that profile's config.yaml from the image: as the front door it becomes
// a file the agent itself writes to (`/sethome`, monitoring.install_id), and the
// force-sync would discard those on every restart.
//
// The dashboard sidecar deliberately does NOT get it. It carries
// AGENT_SHARED_STATE_SETUP=skip, so it execs out of the entrypoint before any of the
// setup steps and never touches a profile config; the cost is that `hermes dashboard`
// still shows the default profile while the front door is the platform one, which is
// recorded as a known limit rather than fixed by re-homing a second container.
const gatewayProfileEnvVar = "HERMES_GATEWAY_PROFILE"

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
// #nosec G101 -- Policy JSON schema definition, not credentials
const credentialProxyPolicyJSON = `{
  "apiVersion": "cli.proxy.kubeagents.io/v1alpha1",
  "blockedMessage": "Command blocked for security reasons.",
  "rules": [
    {"id":"gcp.access-token-disclosure","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+print-(?:access|identity)-token\\b"},
    {"id":"gcp.config-helper-disclosure","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+config\\b(?:\\s+\\S+)*?\\s+config-helper\\b"},
    {"id":"github.token-disclosure","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+token\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+status\\b(?:\\s+\\S+)*?\\s+(?:--show-token|-t)\\b"},
    {"id":"kubernetes.token-disclosure","pattern":"\\bkubectl\\b(?:\\s+\\S+)*?\\s+create\\b(?:\\s+\\S+)*?\\s+token\\b|\\bkubectl\\b(?:\\s+\\S+)*?\\s+config\\b(?:\\s+\\S+)*?\\s+view\\b(?:\\s+\\S+)*?\\s+--raw\\b"},
    {"id":"git.credential-disclosure","pattern":"\\bgit\\b(?:\\s+\\S+)*?\\s+credential\\b(?:\\s+\\S+)*?\\s+fill\\b"},
    {"id":"gcp.credential-replacement","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+(?:login|activate-service-account)\\b"},
    {"id":"github.credential-replacement","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+auth\\b(?:\\s+\\S+)*?\\s+(?:login|refresh|switch|logout)\\b"},
    {"id":"tool.self-modification","pattern":"\\bgcloud\\b(?:\\s+\\S+)*?\\s+components\\b(?:\\s+\\S+)*?\\s+(?:install|update|remove)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+extension\\b(?:\\s+\\S+)*?\\s+(?:install|upgrade|remove)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+(?:alias|config)\\b(?:\\s+\\S+)*?\\s+(?:set|import|delete)\\b"},
    {"id":"github.merge","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+pr\\b(?:\\s+\\S+)*?\\s+merge\\b"},
    {"id":"github.assent","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+pr\\b(?:\\s+\\S+)*?\\s+review\\b(?:\\s+\\S+)*?\\s+(?:--approve|-a)\\b"},
    {"id":"github.api-mutation","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+api\\b(?:\\s+\\S+)*?\\s+(?:-X|--method)(?:\\s+|=)(?:POST|PUT|PATCH|DELETE)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+api\\b(?:\\s+\\S+)*?\\s+(?:-f|-F|--field|--raw-field|--input)\\b"},
    {"id":"github.pipeline-trigger","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+(?:workflow|run)\\b(?:\\s+\\S+)*?\\s+(?:run|rerun|cancel|enable|disable|delete)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+release\\b(?:\\s+\\S+)*?\\s+(?:create|delete|upload|edit)\\b"},
    {"id":"github.repo-administration","pattern":"\\bgh\\b(?:\\s+\\S+)*?\\s+(?:secret|variable)\\b(?:\\s+\\S+)*?\\s+(?:set|delete|remove)\\b|\\bgh\\b(?:\\s+\\S+)*?\\s+repo\\b(?:\\s+\\S+)*?\\s+(?:delete|archive|edit)\\b"}
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
// The default profile takes BOTH, and the split between them is the design:
//
//   - the managed scope (/etc/hermes, renderConfigYAML) carries what must be immutable
//     at runtime, and nothing else. It is machine-global — one file for every profile in
//     the pod — and its merge is per leaf key, so a list there replaces rather than
//     unions, for all of them at once;
//   - `profile-default.overlay.yaml` carries the rest of what the operator owns for the
//     front door: plugins.enabled for untargeted AgentPlugins, their non-gateway config
//     subtrees, and spec.harness.tuning's default limits. It is merged into the agent's
//     own writable config.yaml at startup, where lists union and the agent's unrelated
//     edits survive.
//
// Nothing the operator renders may appear in both.
//
// Two earlier shapes failed. Keying the render `config.yaml` and subPath-mounting it over
// $HERMES_HOME/config.yaml never reached the agent (the entrypoint force-copied the
// image's file over the mount) and made the live config read-only, so nothing the agent
// writes there — `/sethome`'s home channel above all — could be saved. Merging the WHOLE
// render in at startup fixed the writability but left every merged key mutable, so the
// agent could still repoint its own model endpoint and keep that across restarts. Hence
// the split: immutable keys are pinned, mutable operator-owned keys are merged.
func buildConfigMapData(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) map[string]string {
	data := map[string]string{
		managedConfigKey:  renderConfigYAML(agent, agentPlugins),
		managedEnvKey:     renderManagedEnv(agent),
		"leader_elect.py": leaderElectScript,
	}

	untargeted, targeted := partitionPluginsByProfile(filterValidAgentPlugins(agentPlugins))

	// The front door's own overlay. Emitted only when there is something to say: an
	// empty one would make the entrypoint rewrite the agent's config on every start.
	if overlay := renderDefaultProfileOverlayYAML(agent, untargeted); strings.TrimSpace(overlay) != "" {
		data[profileOverlayKey(defaultProfileName)] = overlay
	}

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
		// The default profile is written above, from the untargeted plugins, and must not
		// be reachable from here as well. An AgentPlugin naming `targetProfile: default`
		// would otherwise have this loop overwrite that key with its own overlay alone,
		// dropping every other untargeted plugin and the CR's tuning with it. AgentPlugin's
		// CEL rule rejects the value at admission, but a cluster running an older CRD, or
		// one whose apiserver has CEL disabled, would not. Two code paths must never be
		// able to write one ConfigMap key.
		if profile == defaultProfileName {
			continue
		}
		var limits *agentv1alpha1.AgentLimits
		var memory, frontDoor map[string]any
		if profile == platformProfileName {
			limits = platformProfileLimits(agent)
			memory = memoryOverlay(agent)
			// Only this profile can be the front door: it is the one the gateway is
			// re-homed onto in buildBaseContainers.
			frontDoor = frontDoorOverlay(agent)
		}
		if overlay := renderProfileOverlayYAML(targeted[profile], limits, memory, frontDoor); strings.TrimSpace(overlay) != "" {
			data[profileOverlayKey(profile)] = overlay
		}
	}

	// Cluster profiles are named at runtime, so they get one class overlay applied to
	// all of them rather than a file each. No memory subtree: agents/cluster/config.yaml
	// configures no provider at all, on purpose — a cluster agent is spawned by the
	// kanban dispatcher and carries no human identity to scope a store by.
	if overlay := renderProfileOverlayYAML(nil, clusterProfileLimits(agent), nil, nil); strings.TrimSpace(overlay) != "" {
		data[clusterProfileClassKey] = overlay
	}
	return data
}

// renderManagedEnv pins the platform settings that decide whether the agent can reach
// chat at all, for the ones that have no config.yaml equivalent.
//
// The config layer alone would not hold them. load_gateway_config applies the managed
// overlay early and then calls _apply_env_overrides LAST (gateway/config.py), so an env
// var beats a pinned `platforms.*` leaf — and $HERMES_HOME/.env, which the agent can
// write through save_env_value, is loaded with override=True and beats the container env
// under it. Pinning here closes both: the managed .env is applied last of all, and
// save_env_value refuses to write a key this file holds.
//
// Every access key is emitted on every reconcile, with its real value — never "only when
// it is true". A pin is the ABSENCE of the key from the agent's own .env being impossible,
// so a key omitted because the answer was `false` is a key the agent may still write:
// `GOOGLE_CHAT_ALLOW_ALL_USERS=true` in $HERMES_HOME/.env is checked before any allowlist
// (gateway/authz_mixin.py) and admits the whole domain past a CR that named three users.
// Writing `false` costs nothing and is what makes the restriction hold.
//
// Home channel is deliberately absent. It is the one platform setting the agent is meant
// to own — /sethome writes it to config.yaml and mirrors it into the PVC .env, and that
// mirror is what lets a user's choice outrank the CR's seed on the next start. Pinning it
// here would break /sethome exactly the way the read-only mount did.
//
// Emitted even when empty: the volume projects this key by name, and a ConfigMap item
// that names a missing key fails the mount and the pod never starts. Since API_SERVER_KEY
// below is unconditional it is no longer ever empty in practice, but the projection still
// depends on the key existing, not on it having content.
func renderManagedEnv(agent *agentv1alpha1.PlatformAgent) string {
	// Fixed order, not map iteration: this render feeds the config hash, and a hash that
	// reshuffles on every reconcile would roll the pod for no reason.
	var lines []string
	add := func(key, value string) {
		// One line per key, enforced rather than assumed. Most values here come
		// from CR strings with no pattern or maxLength on the field (chat user
		// lists, project and subscription names), and this file is line-oriented
		// to every reader it has. A newline in one of them appends a line the
		// render never intended — and the mode this file delivers is read back
		// through exactly that line shape (Hermes loads the file per-line into
		// the environment with override semantics, last occurrence winning;
		// agents/platform/scripts/runtime_mode.py answers from the result), so
		// a smuggled `KUBEAGENTS_MODE=next` line rendered after the operator's
		// own pin is a mode flip written by whoever can edit the CR's chat
		// settings. Stripped, not escaped: nothing downstream reads a
		// multi-line value, so there is nothing to preserve.
		value = strings.ReplaceAll(value, "\n", "")
		value = strings.ReplaceAll(value, "\r", "")
		lines = append(lines, fmt.Sprintf("%s=%s", key, value))
	}

	// UNCONDITIONAL, and the only pin here that is not about chat. Every other key below
	// exists because the agent could otherwise write a competing value into the PVC .env;
	// this one exists because something already does, on every boot, without being asked.
	//
	// Hermes' Docker stage2 hook generates a strong random API_SERVER_KEY into
	// $HERMES_HOME/.env whenever that file does not already carry one, and
	// load_hermes_dotenv applies the PVC .env with override=True. So the container env
	// this render is supposed to agree with was being overwritten before the gateway ever
	// read it, and the API server ended up authenticating against a 64-character value
	// that neither the operator, the Secret, the sidecar, nor the process environment had
	// ever seen. Every authenticated route 401'd — the loopback callers directly, and the
	// external path through the Service too, because the credential proxy re-signs
	// upstream with AGENT_API_UPSTREAM_KEY, which is this same sentinel. Issue #786; the
	// seven consecutive cron relay deliveries that recorded success while being rejected
	// are the reason it went unnoticed for so long.
	//
	// The managed .env is applied LAST of all, after the PVC file, so pinning it here is
	// what makes one value true everywhere without anyone having to know that .env exists.
	// It also stops the agent rotating the key out from under its own callers, since
	// save_env_value refuses to write a key this file holds.
	//
	// Deliberately not a per-boot generated secret. The value is worthless — see
	// loopbackAgentAPIKey — and a generated one would have to be transported to the
	// sidecar's AGENT_API_UPSTREAM_KEY and to the probe's bearer, reintroducing exactly
	// the several-parties-must-agree problem this closes.
	add("API_SERVER_KEY", loopbackAgentAPIKey)

	// The mode pin, also unconditional and also not about chat. The managed key
	// is the only way the mode reaches the agent runtime, and pinning it is what
	// keeps the agent from writing a competing answer into the PVC .env — which
	// stack the install runs is not the agent's to decide. Deliberately absent
	// from the container env: one delivery path means one answer
	// (docs/designs/spec-mode-switch.md).
	add(kubeagentsModeEnvKey, string(renderMode(agent, "settings")))

	integration := agent.Spec.Integration
	if integration == nil {
		return strings.Join(lines, "\n") + "\n"
	}

	// The gateway-wide pair below is pinned only when a platform is, so the chat block
	// records where it started rather than testing `lines` for emptiness.
	platformStart := len(lines)

	if gchat := integration.GoogleChat; gchat != nil && gchat.Enabled != nil && *gchat.Enabled {
		add("GOOGLE_CHAT_RELAY_URL", credentialProxyBaseURL(agent))
		add("GOOGLE_CHAT_PROJECT_ID", gchat.ProjectID)
		add("GOOGLE_CHAT_SUBSCRIPTION_NAME", fmt.Sprintf("projects/%s/subscriptions/%s", gchat.ProjectID, gchat.SubscriptionName))
		add("GOOGLE_CHAT_ALLOWED_USERS", strings.Join(gchat.AllowedUsers, ","))
		add("GOOGLE_CHAT_ALLOW_ALL_USERS", strconv.FormatBool(allowAllUsers(gchat.AllowedUsers)))
	}

	if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
		add("SLACK_RELAY_URL", credentialProxyBaseURL(agent))
		add("SLACK_ALLOWED_USERS", strings.Join(slack.AllowedUsers, ","))
		add("SLACK_ALLOW_ALL_USERS", strconv.FormatBool(allowAllUsers(slack.AllowedUsers)))
	}

	if teams := integration.Teams; teams != nil && teams.Enabled != nil && *teams.Enabled {
		add("TEAMS_RELAY_URL", credentialProxyBaseURL(agent))
		add("TEAMS_ALLOWED_USERS", strings.Join(teams.AllowedUsers, ","))
		allowAll := false
		if teams.AllowAllUsers != nil {
			allowAll = *teams.AllowAllUsers
		}
		add("TEAMS_ALLOW_ALL_USERS", strconv.FormatBool(allowAll))
		if teams.TenantId != "" {
			add("TEAMS_TENANT_ID", teams.TenantId)
		}
	}

	if len(lines) == platformStart {
		return strings.Join(lines, "\n") + "\n"
	}

	// The gateway-wide pair, pinned empty/false whenever any platform is pinned above.
	// _is_user_authorized (gateway/authz_mixin.py) unions GATEWAY_ALLOWED_USERS into the
	// per-platform allowlist and falls back to GATEWAY_ALLOW_ALL_USERS when no allowlist
	// is set at all, so leaving either unpinned would let one save_env_value call
	// re-open a restricted deployment by a route the per-platform pins do not cover.
	// An empty GATEWAY_ALLOWED_USERS reads as "not configured" (_auth_env), so this
	// pins the key without adding an allowlist of its own.
	add("GATEWAY_ALLOWED_USERS", "")
	add("GATEWAY_ALLOW_ALL_USERS", "false")

	return strings.Join(lines, "\n") + "\n"
}

// allowAllUsers reads an allowlist the way the env builder does: absent, or present but
// holding a single empty string (which is what an unset CR list marshals to), means the
// deployment did not restrict anyone.
func allowAllUsers(users []string) bool {
	if len(users) == 0 {
		return true
	}
	return len(users) == 1 && users[0] == ""
}

// settingsFileName is both the ConfigMap's only key and the file the agent reads.
// Two pods mount it now — the agent container at its Hermes home, and the shell
// sandbox at its own data path — so the string is shared rather than repeated.
const settingsFileName = "SETTINGS.md"

// settingsConfigMapName is where buildSettingsConfigMap writes and both mounts read.
func settingsConfigMapName(agent *agentv1alpha1.PlatformAgent) string {
	return agent.Name + "-settings"
}

// buildSettingsConfigMap generates the ConfigMap manifest containing SETTINGS.md
func buildSettingsConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	settingsContent := "# GKE Scope Configuration\n"
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      settingsConfigMapName(agent),
			Namespace: agent.Namespace,
		},
		Data: map[string]string{
			settingsFileName: settingsContent,
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

// Managed scope: the front door's config is administrator-pinned rather than merged.
//
// Hermes reads a second config layer from a system directory and lets it WIN, per leaf
// key, over $HERMES_HOME/config.yaml — see hermes_cli/managed_scope.py. Three things
// enforce it: load_config deep-merges the managed dict on top of the user's
// (hermes_cli/config.py), save_config strips every managed leaf before writing, and
// set_config_value hard-rejects one by name. The gateway builds its own dict and calls
// apply_managed_overlay explicitly (gateway/config.py).
//
// This replaces the three-way merge the default profile used to get at startup. That
// merge had to guess which of the live file's values were the runtime's own edits and
// which were stale operator settings, and its rule — runtime wins where the baseline has
// not moved — meant a bad value the agent wrote for itself survived every restart. The
// agent could repoint model.base_url at nothing and lose the ability to reason its way
// back. Pinning inverts that: whatever lands in the PVC file, the operator's value is
// what loads, so a restart always heals.
//
// $HERMES_HOME/config.yaml stays an ordinary writable file. Only the leaves rendered
// into the managed file are frozen, and `platforms.<p>.home_channel` is deliberately not
// one of them — /sethome has to keep working from chat.
const (
	// managedScopeDir is managed_scope.py's POSIX default. HERMES_MANAGED_DIR is set to
	// it explicitly anyway, so the policy is visible in `kubectl get pod -o yaml`.
	managedScopeDir = "/etc/hermes"

	// managedConfigKey holds the render in the config ConfigMap. Deliberately NOT of the
	// `profile-<name>.overlay.yaml` shape: that glob is what the entrypoint walks to find
	// overlays to merge, and the whole point here is that this file is not merged.
	managedConfigKey = "managed-config.yaml"

	// managedEnvKey pins the platform credentials/endpoints that have no config.yaml
	// equivalent. load_hermes_dotenv applies the managed .env LAST with override=True, so
	// it beats both the PVC .env the agent can write and the container env below it, and
	// save_env_value refuses to write a key it holds (hermes_cli/config.py).
	managedEnvKey = "managed.env"

	// managedVolumeName projects the two keys above into managedScopeDir under the names
	// Hermes expects (config.yaml and .env).
	managedVolumeName = "platform-agent-managed-vol"

	// gitopsStateVolumeName projects the GitOps state ConfigMap as a mounted directory
	// volume into the agent container so skills can read managed repositories directly from disk.
	gitopsStateVolumeName = "gitops-state-volume"
	gitopsStateDir        = "/etc/gitops"

	// kubeagentsModeEnvKey carries the mode switch into the managed .env — the
	// only way the mode reaches the agent runtime (docs/designs/spec-mode-switch.md).
	// Agent-side, exactly one reader exists: agents/platform/scripts/runtime_mode.py.
	// The spec's grep rule holds the pair to that: a third code site naming this
	// key is a review comment, so new readers go through runtime_mode, and any
	// operator-side use goes through this constant.
	kubeagentsModeEnvKey = "KUBEAGENTS_MODE"
)

// loopbackAgentAPIKey is the bearer the Hermes API server on 127.0.0.1:8642 accepts, and
// it is a MARKER RATHER THAN A SECRET on purpose: the listener binds loopback only
// (API_SERVER_HOST above), so everything that can reach it is already inside this pod.
// The credential that actually guards the API from outside is API_SERVER_EXTERNAL_KEY,
// held by the credential-proxy sidecar alone, which authenticates the caller and then
// re-signs the request upstream with this value (AGENT_API_UPSTREAM_KEY).
//
// It is a named constant because its VALUE is worthless and its AGREEMENT is the whole
// point. Four places have to say the same thing — the agent container's env, the
// sidecar's AGENT_API_UPSTREAM_KEY, the sidecar's own copy for the event watcher, and
// the managed .env pin in renderManagedEnv — and issue #786 is what a fifth party
// disagreeing costs: Hermes' Docker stage2 hook generates a strong key into
// $HERMES_HOME/.env when that file does not already carry one, load_hermes_dotenv
// applies that file with override=True, and the gateway then authenticated against a
// value invented at boot that no caller in the system knew. Every authenticated route
// 401'd, in-pod and through the Service, and every caller degraded quietly.
//
// The renderManagedEnv pin is what settles it: the managed .env is applied last of all,
// after the PVC file, so this value wins whatever stage2 wrote. Adding a fifth reader
// means using this constant, not repeating the literal.
//
// Length is load-bearing, minimally: Hermes refuses to bind the API server at all for a
// key under 16 characters (has_usable_secret(min_length=16), called from
// gateway/platforms/api_server.py's startup guard). This is 24.
const loopbackAgentAPIKey = "cluster-internal-trusted"

// profileOverlayKey returns the ConfigMap key carrying the overlay for a profile.
func profileOverlayKey(profile string) string {
	return profileOverlayPrefix + profile + profileOverlaySuffix
}

// platformProfileName is the profile the Platform Agent runs as.
const platformProfileName = "platform"

// defaultProfileName is the front-door Chat Agent's profile. It is the odd one out: it
// has no directory under $HERMES_HOME/profiles — its home IS $HERMES_HOME — and it is
// the only profile that takes operator settings by two routes at once, an overlay merged
// into its config AND the managed scope pinned over it. See buildConfigMapData for the
// split.
const defaultProfileName = "default"

// clusterProfileClassKey is the ConfigMap key holding the overlay applied to EVERY
// cluster-* profile.
//
// Cluster profiles are scaffolded at runtime, one per managed cluster, so the operator
// cannot name them individually at render time. The distinct `profileclass-` prefix
// keeps this out of the `profile-<name>` namespace: a sentinel inside that namespace
// could collide with a real profile that happens to share the name.
const clusterProfileClassKey = "profileclass-cluster" + profileOverlaySuffix

// defaultKanbanMaxInProgress caps concurrent kanban workers when spec.harness.tuning
// says nothing. Upstream Hermes leaves the board unbounded, and a worker is a full agent
// process: a burst of cards spawns them until the cgroup OOM killer intervenes, which
// kills a child rather than the container and so produces no restart and no event.
//
// The operator does NOT render this default for the default profile —
// agents/chat/config.yaml carries the same number, which is what caps an install that
// runs the image without the operator too. The constant exists so the CR override below
// can be compared against it, and so the two files can be kept in step. The one place it
// IS rendered is frontDoorKanban, where there is no image copy to defer to: the platform
// profile's config declares no `kanban` key at all.
const defaultKanbanMaxInProgress = 2

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

// platformFrontDoorEnabled reports whether spec.harness.experimental.platformFrontDoor
// asks for the Platform Agent to be the profile the gateway runs as.
func platformFrontDoorEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	if agent == nil || agent.Spec.Harness == nil || agent.Spec.Harness.Experimental == nil {
		return false
	}
	return ptr.Deref(agent.Spec.Harness.Experimental.PlatformFrontDoor, false)
}

// frontDoorToolsets is the toolset list given to each chat platform key when the
// Platform Agent is the front door.
//
// It is agents/platform/config.yaml's `cli` list verbatim, and that is the whole
// intent: a chat message should reach the same surface a kanban worker on this
// profile already has, no more. `hermes-cli` is what _get_platform_tools expands to
// infer the configurable toolsets; the `mcp-` names pass through as MCP server names,
// and `memory` is the provider gate (see the note in agents/platform/config.yaml).
//
// Declaring the key is a NARROWING, and being exact about that matters because the
// error is fail-OPEN. With no list saved for the platform key, hermes_cli's
// _get_platform_tools falls back to `hermes-<platform>`, and toolsets.resolve_toolset
// AUTO-GENERATES that name for a plugin platform such as google_chat once the adapter
// registers: _HERMES_CORE_TOOLS plus whatever tools the plugin contributed — terminal,
// write_file, execute_code, browser, delegation. The same absence also drops the MCP
// allowlist, so every globally enabled server is unioned in rather than the three named
// here. The fallback is therefore the full base bundle plus everything, on a profile
// whose overlay renders no `agent.disabled_toolsets` to bound it — which is why
// agents/chat/config.yaml pins its own `google_chat` key with the same reasoning ("so it
// never falls back to a full base bundle").
//
// TestFrontDoorToolsetsMatchPlatformConfig fails the build when this drifts from the
// image's copy.
var frontDoorToolsets = []string{
	"hermes-cli",
	"mcp-platform_control",
	"mcp-developer_knowledge",
	"mcp-gke",
	"memory",
}

// frontDoorPlugins are the plugins the profile receiving chat ingress has to run, on
// top of the three agents/platform/config.yaml already enables.
//
// They are agents/chat/config.yaml's list, less two. legacy_slash_commands unwraps a typed
// "/hermes sethome" before the gateway dispatcher sees it, and session_store and
// session_otel_bridge are what make an inbound chat session persist and trace at all —
// each hooks ingress, so enabling them on a profile no message reaches does nothing, and
// NOT enabling them on the profile every message reaches loses the behaviour outright.
//
// agent_roster is left off because it exists only to delegate: it injects the
// routable-specialist roster into every turn, which a front door that does the work
// itself does not consult.
//
// bootstrap_onboarding is left off because its state does not follow it. The hook resolves
// its markers from HERMES_HOME, which the flag moves, so on the platform profile the
// once-per-deployment gate reads a home where `.bootstrap_completed`/`.bootstrap_greeted`
// have never been written while the assets check still passes on the absolute
// /opt/defaults/onboarding — and the delivery job it binds to lives on the `default`
// roster, which the flag stops ticking. Enabling it would greet an already-onboarded
// install with the scan-in-progress text and promise a report nothing can deliver. Its
// own README states the rule ("Do not relocate any part of this flow"), and the CRD page
// carries the cost as a known limit.
//
// hermes_otel, tool_call_audit and incident_context are the three the image's own copy
// already enables; the overlay unions lists, so naming them again would be inert rather
// than wrong, and leaving them out keeps the list to what the flag actually adds.
var frontDoorPlugins = []string{
	"session_store",
	"session_otel_bridge",
	"legacy_slash_commands",
}

// kanbanDispatchIntervalSeconds and kanbanWakeOnEvents mirror the `kanban` block
// agents/chat/config.yaml declares, which is the profile the gateway is homed at until
// the front-door flag moves it. They exist in Go only so frontDoorKanban can carry that
// block to the platform profile; nothing renders them for the default profile, whose
// copy is the image's. TestFrontDoorKanbanMatchesChatConfig fails the build when the two
// drift, and the note beside each key in that file is the reasoning for its value.
const kanbanDispatchIntervalSeconds = 5

var kanbanWakeOnEvents = []string{"gave_up", "crashed", "timed_out", "blocked"}

// resolveKanbanMaxInProgress is the live board-wide worker cap: the CR's
// spec.harness.tuning.maxInProgress, or the number agents/chat/config.yaml already
// carries for an install that does not set it.
func resolveKanbanMaxInProgress(agent *agentv1alpha1.PlatformAgent) int {
	if limits := agentTuning(agent); limits != nil && limits.MaxInProgress != nil {
		return *limits.MaxInProgress
	}
	return defaultKanbanMaxInProgress
}

// frontDoorKanban renders the `kanban` subtree for the platform profile when the gateway
// runs as it.
//
// The dispatcher and the notifier run inside the gateway process and read their settings
// through hermes_cli.config.load_config(), which resolves from get_hermes_home() — so
// these keys have to live on the profile the gateway is homed at, not on a profile that
// merely exists. agents/chat/config.yaml holds them for the default profile and no
// operator render is involved there; here there is no image copy to hold them, because
// agents/platform/config.yaml declares no `kanban` key at all — that file is written for
// a kanban WORKER, for which every key in this block is inert.
//
// Which is also why the block is rendered rather than added to that file: with the flag
// off it would be dead config on every install, and the whole claim of an experimental
// flag is that an install which does not set it is untouched.
//
// Without it the front door silently reverts to upstream Hermes: unbounded dispatch, a
// 60s tick, and `completed` back in the wake set, with spec.harness.tuning.maxInProgress
// quietly having no effect at all.
func frontDoorKanban(agent *agentv1alpha1.PlatformAgent) map[string]any {
	return map[string]any{
		"dispatch_in_gateway":       true,
		"auto_subscribe_on_create":  true,
		"dispatch_interval_seconds": kanbanDispatchIntervalSeconds,
		"wake_on_events":            slices.Clone(kanbanWakeOnEvents),
		"max_in_progress":           resolveKanbanMaxInProgress(agent),
	}
}

// frontDoorOverlay renders the keys that turn the platform profile into the gateway's
// front door: the toolsets each chat platform key resolves, the ingress plugins, and the
// kanban block the dispatcher and the notifier read.
//
// It returns nil unless the experimental flag is on, which is what makes the flag
// reversible: profile_overlay.py records what it applied, so withdrawing these keys
// unapplies them rather than leaving a half-configured front door behind.
//
// The chat adapters are deliberately absent, and their absence is not a gap. The managed
// scope is machine-global — `platforms.google_chat`, `platforms.slack` and `display` land
// on this profile exactly as they land on the default one, whichever of them the gateway
// is homed at (see renderConfigYAML). Only the profile-shaped half has to follow the
// gateway: what a session arriving from each platform may reach, which plugins load, and
// how the dispatcher behaves. Rendering the adapters here as well would duplicate an
// operator-owned setting across both routes, which is the one thing the managed scope's
// contract asks callers not to do.
//
// What it deliberately does NOT carry is the Chat Agent's lockdown —
// `agent.disabled_toolsets`, the three-toolset `platform_toolsets`, `toolsets: [kanban]`
// as a ceiling. That lockdown is the Chat Agent's contract, and copying it here would
// leave the Platform Agent unable to do the work the flag exists to let it do
// directly. The trade is stated on the CRD field.
func frontDoorOverlay(agent *agentv1alpha1.PlatformAgent) map[string]any {
	if !platformFrontDoorEnabled(agent) {
		return nil
	}

	// map[string]any, not map[string][]string, and the type is load-bearing. This subtree
	// is written before the targeted plugins' own config is merged over it, and mergeMaps
	// recurses into a nested map only when toStrMap recognises it — which it does for
	// map[string]any alone. As map[string][]string it fell through to a plain assignment,
	// so a plugin targeting this profile with a `platform_toolsets:` block of its own
	// REPLACED the chat keys instead of unioning with them, dropping the front door onto
	// the auto-generated `hermes-google_chat` fallback — the full core bundle plus every
	// enabled MCP server, per the note on frontDoorToolsets, which is why the symptom was
	// an over-broad surface rather than a visibly toolless agent. That also broke the
	// union contract the AgentPlugin CRD page states outright. The []string values below
	// are fine: toSlice already handles them.
	//
	// Both platform keys unconditionally, matching the adapters the managed scope pins
	// whether or not each is enabled: a platform turned on later must not also need its
	// toolsets remembered, and a key for a platform with no adapter is never resolved.
	platformToolsets := map[string]any{
		"google_chat": slices.Clone(frontDoorToolsets),
		"slack":       slices.Clone(frontDoorToolsets),
	}

	return map[string]any{
		"platform_toolsets": platformToolsets,
		"plugins":           map[string]any{"enabled": slices.Clone(frontDoorPlugins)},
		"kanban":            frontDoorKanban(agent),
	}
}

// memoryProviderIsHindsightBacked reports whether a provider talks to the in-cluster
// Hindsight service. Keep in sync with memory_provider_uses_hindsight in
// scripts/installer/common.sh, which decides whether to deploy it.
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

// buildPluginStagingInitContainer builds an init container that extracts a plugin's container image
// into an emptyDir volume on clusters where ImageVolumeSource is unsupported or restricted (e.g. GKE Autopilot).
func buildPluginStagingInitContainer(homeDir string, plugin *agentv1alpha1.AgentPlugin) corev1.Container {
	mountPath := pluginMountPath(homeDir, plugin)
	pullPolicy := corev1.PullIfNotPresent
	if plugin.Spec.ImagePullPolicy != nil {
		pullPolicy = *plugin.Spec.ImagePullPolicy
	}
	stageScript := fmt.Sprintf("mkdir -p %s && (if [ -d /files ]; then cp -a /files/. %s/; else for item in /*; do case \"$item\" in /bin|/boot|/dev|/etc|/home|/lib*|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var) ;; *) cp -a \"$item\" %s/ ;; esac; done; fi) && [ -n \"$(ls -A %s)\" ]",
		mountPath, mountPath, mountPath, mountPath)

	return corev1.Container{
		Name:            buildPluginStagingContainerName(plugin.Name),
		Image:           plugin.Spec.Image,
		ImagePullPolicy: pullPolicy,
		SecurityContext: hardenedSecurityContext(),
		Command: []string{
			"/bin/sh",
			"-c",
			stageScript,
		},
		VolumeMounts: []corev1.VolumeMount{
			{
				Name:      buildPluginVolumeName(plugin.Name),
				MountPath: mountPath,
			},
		},
	}
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
func renderProfileOverlayYAML(plugins []*agentv1alpha1.AgentPlugin, limits *agentv1alpha1.AgentLimits, memory, frontDoor map[string]any) string {
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

	// The front-door keys, when this profile is the one the gateway runs as. Written
	// before the plugin contributions for the same reason, and mergeMaps unions the
	// `plugins.enabled` list below rather than replacing it.
	if frontDoor != nil {
		overlay = mergeMaps(overlay, frontDoor)
	}

	enabled := make([]string, 0, len(plugins))
	for _, p := range plugins {
		if !slices.Contains(enabled, p.Name) {
			enabled = append(enabled, p.Name)
		}
	}
	if len(enabled) > 0 {
		// Merged, not assigned: the front-door overlay above may already have written
		// `plugins.enabled`, and an assignment here would drop the ingress plugins the
		// moment a plugin happens to target this profile.
		overlay = mergeMaps(overlay, map[string]any{"plugins": map[string]any{"enabled": enabled}})
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

// renderDefaultProfileOverlayYAML builds the front door's overlay: everything the
// operator owns for the `default` profile that must NOT be pinned in the managed scope.
//
// Two things end up here rather than in renderConfigYAML.
//
// plugins.enabled, because the managed scope is machine-global and its merge replaces a
// list rather than unioning it — pinning the front door's plugin list there would import
// it into the platform specialist and every cluster profile as well, and would wipe each
// of their own lists on the way. Merged here it unions with the list agents/chat/config.yaml
// already declares, which is the only way an AgentPlugin with no targetProfile ever loads:
// a mounted plugin is inert until it is named, since Hermes calls register(ctx) only for
// enabled plugins. `targetProfile: default` is rejected at admission, so this route is the
// only one an untargeted plugin has.
//
// spec.harness.tuning.default, for the same machine-global reason: one profile's turn
// budget must not become every profile's.
//
// The maxInProgress cap is the CR's override only. Its default lives in
// agents/chat/config.yaml (defaultKanbanMaxInProgress), so an unset CR leaves the image's
// number in force rather than having the operator restate it on every reconcile.
func renderDefaultProfileOverlayYAML(agent *agentv1alpha1.PlatformAgent, plugins []*agentv1alpha1.AgentPlugin) string {
	overlay := renderProfileOverlayYAML(plugins, defaultProfileLimits(agent), nil, nil)

	tuning := agentTuning(agent)
	if tuning == nil || tuning.MaxInProgress == nil {
		return overlay
	}

	var parsed map[string]any
	if overlay != "" {
		if err := yaml.Unmarshal([]byte(overlay), &parsed); err != nil {
			return overlay
		}
	}
	if parsed == nil {
		parsed = map[string]any{}
	}
	parsed = mergeMaps(parsed, map[string]any{
		"kanban": map[string]any{"max_in_progress": *tuning.MaxInProgress},
	})

	data, err := yaml.Marshal(parsed)
	if err != nil {
		return overlay
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

// buildGitopsStateConfigMap generates the ConfigMap manifest containing runtime state (e.g. repos)
func buildGitopsStateConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	data := map[string]string{}

	// Extract primary repository from CR Spec if provided
	if agent.Spec.Integration != nil && agent.Spec.Integration.GitHub != nil {
		gitRepo := strings.TrimSpace(agent.Spec.Integration.GitHub.GitRepo)
		org := strings.TrimSpace(agent.Spec.Integration.GitHub.Org)
		if gitRepo != "" && gitRepo != "None" {
			if err := agentv1alpha1.ValidateGitRepoURLWithOrg(gitRepo, org); err == nil {
				if cleanedURL, err := agentv1alpha1.CleanRepoURLWithOrg(gitRepo, org); err == nil {
					entries := []agentv1alpha1.ManagedRepoEntry{
						{Type: "github", URL: cleanedURL},
					}
					if jsonBytes, err := json.Marshal(entries); err == nil {
						data["managed_repos"] = string(jsonBytes)
					}
				}
			} else {
				manifestsLog.Info("Skipping initial configmap seed due to unparseable or invalid GitRepo", "raw", gitRepo, "error", err)
			}
		}
	}

	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "v1",
			Kind:       "ConfigMap",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gitops-state",
			Namespace: agent.Namespace,
		},
		Data: data,
	}
}

// renderConfigYAML builds the MANAGED config the pod runs under.
//
// Unlike every other profile rendering, this one is not an overlay merged into the PVC.
// It is emitted as the `managed-config.yaml` ConfigMap key, mounted read-only at
// /etc/hermes/config.yaml, and Hermes overlays it leaf-by-leaf on top of whichever
// config it has just loaded — see the managedScopeDir block for the full contract and
// the three enforcement points. Two earlier arrangements failed:
//
//   - subPath-mounting this rendering over $HERMES_HOME/config.yaml. A subPath is a
//     read-only mount POINT, so the agent could save nothing to its own config at all
//     (`/sethome` returned EACCES); and the entrypoint force-copied the image's config
//     over the mount anyway, so none of these keys reached a running pod.
//   - merging it into the PVC file at startup. That fixed the writability but left every
//     merged key mutable: an agent that repointed model.base_url at nothing kept that
//     across restarts, because the merge treated the runtime's edit as the newer one.
//
// THE MANAGED SCOPE IS MACHINE-GLOBAL — it is NOT the default profile's overlay.
// get_managed_dir() takes no profile argument (managed_scope.py), so every leaf below
// lands on the platform specialist and on each scaffolded cluster agent exactly as it
// lands on the front door. And a leaf REPLACES the profile's own value rather than
// merging into it — a list is a leaf too, so `platform_toolsets.cli` rendered here
// rewrote every specialist's toolset list to the front door's two-tool delegation
// surface, and `agent.disabled_toolsets` took the specialists' terminal away. Nothing
// profile-shaped may be rendered here for that reason: toolsets, disabled toolsets,
// kanban tuning, terminal cwd, mcp servers, the plugin roster and the memory provider
// are each profile's own, and stay in that profile's config.yaml in the image.
//
// WHAT BELONGS HERE is the intersection of two tests: identical for every profile in
// the pod, AND beyond the agent's own repair once broken. That is the model endpoint —
// an agent that repoints base_url at nothing cannot be told to put it back, because
// being told requires the endpoint — and the chat platform wiring that carries the
// human's only channel to it. `approvals.cron_mode` rides along as a third: it is
// uniform by design, and Hermes' own default is `deny`. Everything else is recoverable
// the way it was broken, by a human telling the agent to fix its own config, and so
// stays writable.
//
// Keys this function says nothing about stay the image's, and stay writable: that is
// what keeps `/sethome`, the monitoring install id and saved slash-command preferences
// working. `platforms.<x>.home_channel` is deliberately among them — `/sethome` writes
// it, and a leaf rendered here would overwrite the human's choice at every load.
//
// The operator's other settings for the front door are not lost, they take the other
// route: renderDefaultProfileOverlayYAML emits profile-default.overlay.yaml, which the
// entrypoint merges into the agent's own config.yaml with lists UNIONED. Anything
// operator-owned that must remain mutable — plugins.enabled above all — belongs there
// and must not be duplicated here.
// managedTerminalConfig is the `terminal` block rendered into the managed scope
// when the shell sandbox is on. The key names are Hermes' own: hermes_cli/config.py
// maps each one onto the TERMINAL_* environment variable tools/terminal_tool.py
// reads, and tools/environments/ssh.py turns them into an ssh command line.
//
// Six of these keys are Hermes'. `cwd` is absent on purpose — it is per-profile,
// and a value here would replace each profile's own (see the note on
// renderConfigYAML). `ssh_persistent` is absent because Hermes has no env var for
// it, so rendering it would look like a setting and be one only for in-process
// callers.
//
// `lifetime_seconds` is the reaper's idle timeout, and it is here to keep the
// reaper from firing at all. Hermes gives every task its own SSHEnvironment but
// derives the ssh ControlPath from sha256(user@host:port) — all three fixed by
// this block — so every concurrent task multiplexes over ONE master connection.
// Teardown is per environment and not per connection: cleanup() runs
// `ssh -O exit` on that shared path, which drops the master and kills every
// session riding it. A sibling task loses its in-flight command with exit 255
// and an empty stderr. At the 300s default and delegation.max_concurrent_children
// of 3, the reaper reaches that state whenever one child idles while another
// works. Nothing is reclaimed by reaping here — the far side is a StatefulSet pod
// that stays up either way — so the timeout buys nothing and costs the race.
//
// `workspace_root` is the sixth and is NOT Hermes'. Hermes ignores it; the reader
// is agents/platform/scripts/sandbox_exec.py, which already parses this block for
// ssh_host and needs one more fact about the far side — the directory a proxied
// command must run in, which is shellSandboxDataPath, the sandbox's data volume.
// It is published rather than hardcoded in the script because it describes
// the sandbox, and the script runs in the agent pod: the two filesystems agree
// only because deploy/sandbox/Dockerfile creates /opt/data to match, and a script
// that reads its own HERMES_HOME to name a path in another container is relying on
// that coincidence rather than on the operator that set both.
type managedTerminalConfig struct {
	Backend         string `json:"backend"`
	SSHHost         string `json:"ssh_host"`
	SSHUser         string `json:"ssh_user"`
	SSHPort         int    `json:"ssh_port"`
	SSHKey          string `json:"ssh_key"`
	LifetimeSeconds int    `json:"lifetime_seconds"`
	WorkspaceRoot   string `json:"workspace_root"`
}

// shellSandboxEnvLifetimeSeconds is what the operator publishes as
// terminal.lifetime_seconds — see the note above. 30 days, not "off": Hermes
// takes an int and has no sentinel for never, and a number this size means the
// only environments the reaper can still collect are ones whose process has
// outlived a month of rollouts, which nothing here does.
const shellSandboxEnvLifetimeSeconds = 2592000

func renderConfigYAML(agent *agentv1alpha1.PlatformAgent, agentPlugins []*agentv1alpha1.AgentPlugin) string {
	agentPlugins = filterValidAgentPlugins(agentPlugins)

	cfg := struct {
		Model struct {
			Default  string `json:"default"`
			Provider string `json:"provider"`
			Model    string `json:"model,omitempty"`
			BaseURL  string `json:"base_url,omitempty"`
			APIKey   string `json:"api_key,omitempty"`
			// The wire protocol, rendered explicitly only so that it is PINNED:
			// a key absent from this file is a key the managed scope does not
			// hold, and `/model <x> --global` persists api_mode alongside the
			// endpoint (hermes_cli/cli.py). Leaving it unpinned let a model
			// switch write a Responses-API mode next to an immutable
			// chat-completions base_url and keep it across restarts.
			//
			// No omitempty: an empty value here would drop the key and take the
			// pin with it.
			APIMode string `json:"api_mode"`
		} `json:"model"`
		Approvals struct {
			CronMode string `json:"cron_mode,omitempty"`
		} `json:"approvals,omitempty"`
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
			Teams struct {
				Enabled          bool           `json:"enabled"`
				TypingStatusText string         `json:"typing_status_text,omitempty"`
				Extra            map[string]any `json:"extra,omitempty"`
			} `json:"teams"`
		} `json:"platforms"`
		// Chat verbosity, keyed by platform. Read by the gateway's chat adapters
		// and inert on a profile that receives no chat ingress, so it meets the
		// uniformity test the way the platform wiring above does.
		Display struct {
			Platforms map[string]map[string]any `json:"platforms,omitempty"`
		} `json:"display,omitempty"`
		// Where the agent's shell runs. Rendered only when the shell sandbox is on,
		// and rendered HERE rather than in a profile's config for the two reasons
		// this function's note gives. It is uniform: one sandbox per pod, and every
		// profile in the pod reaches it at the same address with the same key —
		// `terminal.cwd`, which is the profile-shaped part, is deliberately not
		// among these keys. And it is beyond the agent's own repair in the strongest
		// sense in the file: an agent that writes `backend: local` into its own
		// config.yaml has not broken a setting, it has left the sandbox, and no
		// human telling it to put the value back would be reason to trust the value.
		// The managed scope is what makes that write have no effect.
		Terminal *managedTerminalConfig `json:"terminal,omitempty"`
	}{}

	// Model. The endpoint every profile in the pod reasons through, and the setting
	// whose loss is not self-repairable — see the note on this function.
	cfg.Model.Provider = "custom"
	cfg.Model.Default = agentModelName
	cfg.Model.Model = agentModelName
	cfg.Model.BaseURL = fmt.Sprintf("http://litellm.%s.svc.cluster.local/v1", agent.Namespace)
	cfg.Model.APIKey = "none"
	// What `provider: custom` against a non-OpenAI base_url already resolves to
	// (_resolve_plain_custom_api_mode in hermes_cli/runtime_provider.py), so this
	// changes no behaviour — it only makes the value one the agent cannot rewrite.
	cfg.Model.APIMode = "chat_completions"

	// Cron approvals. Uniform across the pod by design — the shared image default
	// (deploy/shared/defaults/config.yaml) sets it and no persona has a reason to
	// differ — but rendered here rather than left to the image because Hermes'
	// default is `deny` (hermes_cli/config.py) and the cluster-agent template does
	// not declare the key. Leaving it out would silently deny every cron-initiated
	// approval on a scaffolded cluster profile.
	cfg.Approvals.CronMode = "approve"

	// Terminal. The ssh backend, always: the agent container has no shell tools
	// of its own, so a `local` terminal here would run the model's commands in
	// the pod holding the credentials — which is the arrangement this design
	// exists to end.
	cfg.Terminal = &managedTerminalConfig{
		Backend:         "ssh",
		SSHHost:         shellSandboxHost(agent),
		SSHUser:         shellSandboxUser,
		SSHPort:         shellSandboxPort,
		SSHKey:          shellSandboxClientKeyFilePath(),
		LifetimeSeconds: shellSandboxEnvLifetimeSeconds,
		WorkspaceRoot:   shellSandboxDataPath,
	}

	cfg.Display.Platforms = map[string]map[string]any{}

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
	// Set unconditionally, unlike Google Chat's typing text below. It is inert while
	// Slack is off, and rendering it regardless means the setting cannot be missed by
	// whichever path ends up turning Slack on. Kept in sync with the same block in
	// agents/chat/config.yaml, which carries the full note.
	cfg.Platforms.Slack.Extra = map[string]any{"rich_blocks": true}
	cfg.Platforms.Teams.Extra = map[string]any{"adaptive_cards": true}

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
		if teams := agent.Spec.Integration.Teams; teams != nil && teams.Enabled != nil {
			cfg.Platforms.Teams.Enabled = *teams.Enabled
			if *teams.Enabled {
				cfg.Platforms.Teams.TypingStatusText = "Kage is thinking…"
				if teams.AdaptiveCards != nil {
					cfg.Platforms.Teams.Extra["adaptive_cards"] = *teams.AdaptiveCards
				}
			}
		}
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return ""
	}

	mergedYAML := string(data)

	hasConfigOverrides := false
	for _, plugin := range agentPlugins {
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
		// Only the GATEWAY-SCOPED subtrees of a plugin's config land here, whoever
		// owns the plugin — `platforms`, the wiring for an ingress the pod runs
		// exactly one of. The rest (`approvals`, `platform_toolsets`) is
		// profile-shaped: merging it into a machine-global file would push one
		// plugin's toolsets onto every profile in the pod, which is the failure this
		// function's note describes. A plugin that names a targetProfile still gets
		// those subtrees, via that profile's overlay (buildProfileOverlay).
		//
		// A plugin that names NO targetProfile is handled the same way, in two
		// halves: its gateway-scoped subtrees merge here, and its name and its
		// profile-shaped subtrees go to profile-default.overlay.yaml
		// (renderDefaultProfileOverlayYAML), which the entrypoint merges into the
		// front door's own config.yaml. Enabling it here instead would replace every
		// other profile's plugins.enabled with the front door's.
		//
		// Rejections are not logged here: this runs on every reconcile.
		// pluginConfigIssues reports the same findings, and updatePluginStatuses logs
		// them once per change.
		for _, plugin := range agentPlugins {
			if strings.TrimSpace(plugin.Spec.Config) == "" {
				continue
			}
			var pluginConfig map[string]any
			if err := yaml.Unmarshal([]byte(plugin.Spec.Config), &pluginConfig); err != nil {
				continue
			}
			base = mergeMaps(base, pluginConfigForScope(pluginConfig, true))
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
					corev1.ResourceStorage: resource.MustParse(agentDataStorageSize),
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
	// otlpDisabled reports that discovery established this cluster has no collector and
	// nothing configured one (otlpSourceNone). The agent is then wired with
	// OTEL_SDK_DISABLED=true and no endpoint. A separate field rather than an empty
	// otlpEndpoint because empty already means the managed collector, and the two
	// outcomes need opposite manifests.
	otlpDisabled bool
}

// buildPodTemplateSpec generates the shared PodTemplateSpec for Deployment and StatefulSet
func buildPodTemplateSpec(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, opts renderOptions) corev1.PodTemplateSpec {
	agentPlugins = filterValidAgentPlugins(agentPlugins)
	replicas, _ := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)

	saName := agentServiceAccountName(agent)

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

	if !opts.imageVolumeSupported {
		for _, plugin := range agentPlugins {
			initContainers = append(initContainers, buildPluginStagingInitContainer(homeDir, plugin))
		}
	}

	// The shell sandbox's half of the SSH keypair, staged into an emptyDir the
	// agent container can read — see buildShellSandboxClientKeyVolumes for why the
	// Secret cannot be handed to `ssh -i` directly.
	initContainers = append(initContainers, buildShellSandboxClientKeyInitContainer(image))
	shellSandboxVolumes := buildShellSandboxClientKeyVolumes()

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
			// key with this non-secret loopback sentinel. Setting it here is NOT
			// sufficient on its own — the PVC .env beats the container env inside
			// Hermes — which is why renderManagedEnv pins the same value; see
			// loopbackAgentAPIKey.
			Name:  "API_SERVER_KEY",
			Value: loopbackAgentAPIKey,
		},
		// API_SERVER_MODEL_NAME belongs here by topic but is appended after the
		// env merge instead — see buildBaseContainers, and apiServerModelEnvVar
		// for why an override of it must not win.
		{
			Name:  "SESSION_KV_DB_PATH",
			Value: sessionKVDBPath,
		},
		{
			Name:  "GITOPS_STATE_CONFIGMAP",
			Value: agent.Name + "-gitops-state",
		},
		{
			Name:  "GITOPS_STATE_PATH",
			Value: path.Join(gitopsStateDir, "managed_repos"),
		},
	}

	// The two exceptions to "no credentials in the sandbox", both of them
	// pod-scoped and useless outside this pod's loopback interface:
	//
	//   SESSION_KV_API_KEY  authenticates callers of the Session KV server on
	//                       127.0.0.1:8699. This container both serves it and
	//                       calls it (platform_mcp_server, incident_context,
	//                       and the gateway's kanban notifier).
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

	envVars = append(envVars, otelTelemetryEnvVars("platform", agent.Name, agent.Namespace, opts.otlpEndpoint, opts.otlpDisabled)...)
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
					Value: credentialProxyBaseURL(agent),
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
			// Shared with renderManagedEnv, and emitted on the same terms: always, with
			// the real answer. The managed .env pins the same key, and the two
			// disagreeing would leave the allowlist decided by load order.
			envVars = append(envVars, corev1.EnvVar{
				Name:  "GOOGLE_CHAT_ALLOW_ALL_USERS",
				Value: strconv.FormatBool(allowAllUsers(gchat.AllowedUsers)),
			})
		}
		if slack := integration.Slack; slack != nil && slack.Enabled != nil && *slack.Enabled {
			envVars = append(envVars, []corev1.EnvVar{
				{
					Name:  "SLACK_RELAY_URL",
					Value: credentialProxyBaseURL(agent),
				},
				{
					Name:  "SLACK_ALLOWED_USERS",
					Value: strings.Join(slack.AllowedUsers, ","),
				},
				{
					Name:  "SLACK_ALLOW_ALL_USERS",
					Value: strconv.FormatBool(allowAllUsers(slack.AllowedUsers)),
				},
			}...)
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
		if github := integration.GitHub; github != nil {
			org := strings.TrimSpace(github.Org)
			if org == "" && github.GitRepo != "" {
				if cleaned, err := agentv1alpha1.CleanRepoSlug(github.GitRepo); err == nil {
					parts := strings.SplitN(cleaned, "/", 2)
					if len(parts) == 2 {
						org = parts[0]
					}
				}
			}
			if org != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "GITHUB_ORG",
					Value: org,
				})
			}
		}
		if teams := integration.Teams; teams != nil && teams.Enabled != nil && *teams.Enabled {
			allowAll := false
			if teams.AllowAllUsers != nil {
				allowAll = *teams.AllowAllUsers
			}
			envVars = append(envVars, []corev1.EnvVar{
				{
					Name:  "TEAMS_RELAY_URL",
					Value: credentialProxyBaseURL(agent),
				},
				{
					Name:  "TEAMS_ALLOWED_USERS",
					Value: strings.Join(teams.AllowedUsers, ","),
				},
				{
					Name:  "TEAMS_ALLOW_ALL_USERS",
					Value: strconv.FormatBool(allowAll),
				},
			}...)
			if teams.TenantId != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "TEAMS_TENANT_ID",
					Value: teams.TenantId,
				})
			}
			if teams.HomeChannel != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "TEAMS_HOME_CHANNEL",
					Value: teams.HomeChannel,
				})
			}
			if teams.HomeChannelName != "" {
				envVars = append(envVars, corev1.EnvVar{
					Name:  "TEAMS_HOME_CHANNEL_NAME",
					Value: teams.HomeChannelName,
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

	// APPENDED AFTER THE PLUGIN MERGE, for the same reason as CREDENTIAL_PROXY_URL below
	// and AGENT_SHARED_STATE_SETUP in buildBaseContainers: extractAgentPluginEnvVars copies
	// an AgentPlugin's spec.env verbatim with no allowlist, and mergeEnvVars replaces a
	// same-named default in place. This variable is the switch for the whole pin layer, so
	// it is the last one that may sit on the overridable side. A plugin naming it could
	// repoint the managed scope at the writable PVC, and every pin would evaporate at once
	// — model.base_url no longer overruled at load, save_config stripping nothing, and the
	// managed .env (applied with override=True) becoming an agent-writable file, so a
	// GOOGLE_CHAT_ALLOW_ALL_USERS=true written there would beat the CR's allowlist. The
	// scope fails open by design, so none of that shows up as an unhealthy pod.
	//
	// managed_scope.py defaults to this same path. Set explicitly so the policy is visible
	// in the pod spec, and so moving it later is a one-line change.
	envVars = append(envVars, corev1.EnvVar{
		Name:  "HERMES_MANAGED_DIR",
		Value: managedScopeDir,
	})
	// The Hermes base image sets HERMES_WRITE_SAFE_ROOT=/opt/data, which is the agent's
	// own home while the shell is local. agent/file_safety.py checks the path prefix in
	// the agent process before the write is routed anywhere, so with the shell in the
	// sandbox this has to name the sandbox's writable directories or write_file and
	// patch return "Write denied" for everything — which is how the earlier value was
	// found wrong on a live install. The sandbox's data volume carries the same
	// /opt/data path deliberately, so the interesting half of this is the ephemeral
	// home; the value is written out rather than left to the image default so the
	// policy is visible in the pod spec. It gives up no isolation: with backend: ssh
	// the file tools cannot reach the agent's own filesystem to begin with.
	//
	// TERMINAL_CWD is what stops the agent working in a directory that does not
	// survive a restart. Hermes' ssh backend defaults cwd to `~`
	// (tools/terminal_tool.py), which is the ephemeral home, so every relative path
	// the model wrote was lost on the next pod recycle while the volume beside it
	// stayed empty. Set as an environment variable rather than as `terminal.cwd` in
	// the managed scope: the config bridge treats an explicit config key as an
	// override of the environment (hermes_cli/config.py), so this is a pod-wide
	// default a profile can still narrow to its own directory, which is what #11 is
	// for. A managed-scope value could not be narrowed by anything.
	envVars = append(envVars, corev1.EnvVar{
		Name:  "HERMES_WRITE_SAFE_ROOT",
		Value: strings.Join([]string{shellSandboxDataPath, shellSandboxHomePath}, ":"),
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "TERMINAL_CWD",
		Value: shellSandboxDataPath,
	})
	// CREDENTIAL_PROXY_URL is emitted EMPTY here, and /opt/credential-proxy/bin is
	// off PATH. Both existed to serve the credential-proxy shims, and #737 took
	// the last of them out of the agent image: this container has no kubectl,
	// gcloud, gh or git in any form, and the code that used to invoke one goes
	// through sandbox_exec.py to the shell sandbox instead. Neither has a reader
	// here any more, and buildShellSandboxStatefulSet sets the URL on the sandbox.
	//
	// Empty rather than absent, and unconditionally, for the reason
	// HERMES_GATEWAY_PROFILE is: last-wins only settles a duplicate, so a name the
	// operator never emits leaves an AgentPlugin's spec.env as the only writer —
	// and extractAgentPluginEnvVars copies that verbatim, with no allowlist. A
	// shim reinstated in this container is meant to need deliberate wiring; a
	// plugin able to supply the URL is that wiring, arriving from a field further
	// down the same CR.
	//
	// This is tidiness, not containment. GOOGLE_CHAT_RELAY_URL and SLACK_RELAY_URL
	// above still name the broker, so anything in this container that can form an
	// HTTP request can still reach it — what stops that being free is the token
	// below, which the broker now checks on every call.
	envVars = append(envVars, corev1.EnvVar{
		Name:  "CREDENTIAL_PROXY_URL",
		Value: "",
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "CREDENTIAL_PROXY_TOKEN_FILE",
		Value: credentialProxyTokenMountPath + "/token",
	})
	envVars = append(envVars, corev1.EnvVar{
		Name:  "PATH",
		Value: "/opt/hermes/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
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

	var runtimeClassName *string
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.Availability != nil {
		runtimeClassName = agent.Spec.Deployment.Availability.RuntimeClassName
	}

	containers := buildBaseContainers(agent, image, envVars, agentPlugins, opts.imageVolumeSupported)

	// The API authenticator is a NATIVE SIDECAR -- an init container carrying
	// restartPolicy: Always -- and not an ordinary container.
	//
	// It owns port 8643, which the Service targets, and it shares a network
	// namespace with the agent container. As an ordinary container the two started
	// in parallel and raced for the bind, and a pod where the listener lost left
	// the Service routing to nothing. Reproduced on a live cluster on 10 August,
	// when the credential runtime still ran in this container and the agent could
	// take the port deliberately by binding 0.0.0.0:8643 out of its own shell.
	// That deliberate version is gone with the shell -- code the agent runs
	// executes in the sandbox pod now -- but the startup race between two
	// containers of this pod is not, and the ordering below is what settles it.
	//
	// Note what the kubelet actually waits for: the sidecar having STARTED, plus
	// its startupProbe if it declares one. This container declares only a
	// readinessProbe, which gates pod readiness and gates nothing about app
	// container startup -- so the guarantee is ordering of process creation, not
	// "the listener has bound 8643". That is a much smaller window than two
	// containers racing from the same instant, and it is not zero. Give this
	// container a startupProbe on 8643 if the remaining window ever matters.
	//
	// This is also the ordering the pod needs for its own sake: every call into
	// the Hermes API arrives through this listener, so an agent that starts first
	// is an agent nothing can reach.
	//
	// Requires Kubernetes 1.29+. SidecarContainers is beta and on by default there;
	// it went GA in 1.33 and was alpha (off) in 1.28. 1.29 is the floor because that
	// is where restartPolicy on an init container starts being honoured without a
	// feature gate.
	//
	// On 1.28 the install fails fast rather than degrading: dropDisabledFields
	// strips restartPolicy, which leaves an ordinary init container still
	// declaring the readinessProbe buildAgentAPIAuthSidecar sets, and
	// validateInitContainers does not permit probes without restartPolicy:
	// Always. So the API server rejects the pod template and the operator's
	// apply fails -- nothing is created, nothing hangs, and there is no window
	// where the listener is running without sidecar semantics.
	// charts/kube-agents/Chart.yaml pins the same floor, so Helm refuses the
	// install before it gets that far.
	//
	// None of that applies once the credential runtime leaves this Pod, which
	// two independent switches can do. There is then no shared network namespace
	// and so no bind to race for, and what stays behind is the front door for the
	// agent's own API, which cannot follow the runtime across the Pod boundary.
	//
	// buildAgentAPIAuthSidecar stays because it hosts the k8s-event-watcher as
	// well as the front door, and the watcher posts to the Session KV server on
	// this Pod's loopback with nowhere else to deliver.
	initContainers = append(initContainers, asNativeSidecar(buildAgentAPIAuthSidecar(agent, homeDir)))
	// The agent reaches the runtime over the network now, so it has to present a
	// token to be served.
	mountIntoContainer(containers, "platform-agent", corev1.VolumeMount{
		Name: agentCredentialProxyTokenVolume, MountPath: credentialProxyTokenMountPath, ReadOnly: true,
	})

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
			volumes = append(volumes, corev1.Volume{
				Name: buildPluginVolumeName(plugin.Name),
				VolumeSource: corev1.VolumeSource{
					EmptyDir: &corev1.EmptyDirVolumeSource{},
				},
			})
		}
	}
	volumes = append(volumes, buildCustomStorageVolumes(agent)...)
	// The credential runtime's volumes go with it into its own Pod. The
	// event-watcher pair stays, because buildAgentAPIAuthSidecar still hosts the
	// watcher here, and so does the token the agent presents across the network.
	volumes = append(volumes, buildAgentAPIAuthVolumes(agent)...)
	volumes = append(volumes, buildAgentCredentialProxyTokenVolume())
	if len(sidecarVolumes) > 0 {
		volumes = append(volumes, sidecarVolumes...)
	}
	if len(extraVolumes) > 0 {
		volumes = append(volumes, extraVolumes...)
	}
	volumes = append(volumes, shellSandboxVolumes...)

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
	// No kubeagents.x-k8s.io/has-credential-proxy label. That is what
	// github-token-minter's NetworkPolicy admits on 8080, and this Pod has no
	// caller for the minter: the credential runtime is in a Pod of its own and
	// carries the label there, and nothing left here can form the call.

	return corev1.PodTemplateSpec{
		ObjectMeta: metav1.ObjectMeta{
			Labels:      podLabels,
			Annotations: mergeAnnotations(defaultAnnotations, podAnnotations),
		},
		Spec: corev1.PodSpec{
			// No ShareProcessNamespace. Nothing in this Pod holds a credential
			// any more, so the field is not load-bearing here — it stays unset
			// because a Pod that shares its process namespace hands every
			// container's /proc/<pid>/environ to every other one, and the next
			// container added here should not inherit that by default.
			// See docs/security-requirements.md.
			RuntimeClassName: runtimeClassName,
			InitContainers:   initContainers,
			// Pod-scoped, so it covers the agent, both operator-injected sidecars,
			// anything in spec.deployment.sidecars/initContainers, and the OCI image
			// volumes AgentPlugins mount. nil when nothing is configured, which is
			// what keeps a default install's pod template byte-identical.
			ImagePullSecrets:             resolveImagePullSecrets(agent.Spec.Deployment),
			ServiceAccountName:           saName,
			AutomountServiceAccountToken: ptr.To(false),
			SecurityContext: &corev1.PodSecurityContext{
				FSGroup: ptr.To(agentFSGroup),
				// Every container in this Pod runs as the agent image's user;
				// none of them holds a credential.
				RunAsUser:      ptr.To(sandboxUID),
				RunAsGroup:     ptr.To(agentFSGroup),
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

// gatewayProgressDeadlineSeconds is the ceiling over every rollout wait on the
// gateway Deployment, and it has to outlast both of the budgets under it:
//
//	startupProbe budget  <  rollout gate  <  progressDeadlineSeconds
//
// Past the deadline the Deployment reports ProgressDeadlineExceeded and any
// caller's wait returns early however long it asked for, so a gate raised above
// this number buys nothing. Kubernetes defaults it to 600s, which is *below*
// the 605s cold boot agentAPIProbe(10, 60) already sanctions — the kubelet is
// told to tolerate a boot the Deployment gives up on. 1200s clears the 900s
// deploy gate in .github/workflows/reusable-deploy-agent.yml. hindsight-api
// carries an explicit 900 for the same reason; see tests/test_hindsight_probes.py.
const gatewayProgressDeadlineSeconds int32 = 1200

// buildDeployment generates the Deployment manifest for the agent payload
func buildDeployment(agent *agentv1alpha1.PlatformAgent, configHash, fluentBitHash, settingsConfigHash, policyHash string, agentPlugins []*agentv1alpha1.AgentPlugin, opts renderOptions) *appsv1.Deployment {
	replicas, strategy := resolveDeploymentReplicasAndStrategy(agent.Spec.Deployment)
	podTemplate := buildPodTemplateSpec(agent, configHash, fluentBitHash, settingsConfigHash, policyHash, agentPlugins, opts)
	progressDeadline := gatewayProgressDeadlineSeconds

	// Mirrors the pod template's labels.
	workloadLabels := map[string]string{"app": agent.Name + "-gateway"}

	return &appsv1.Deployment{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "apps/v1",
			Kind:       "Deployment",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-gateway",
			Namespace: agent.Namespace,
			Labels:    workloadLabels,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas:                &replicas,
			Strategy:                strategy,
			ProgressDeadlineSeconds: &progressDeadline,
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
		// EACCES. The operator's rendering reaches the agent as the managed scope below
		// instead, which wins at load without the file ever being written.
		{
			// Directory mount, never subPath: a subPath does not receive kubelet
			// ConfigMap updates, and managed_scope.py caches on (mtime, size) — so as a
			// directory a CR edit re-pins live, without a restart.
			Name:      managedVolumeName,
			MountPath: managedScopeDir,
			ReadOnly:  true,
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
			MountPath: path.Join(homeDir, settingsFileName),
			SubPath:   settingsFileName,
			ReadOnly:  true,
		},
		{
			Name:      "system-metadata",
			MountPath: path.Dir(sessionKVDBPath),
			SubPath:   "session",
		},
		{
			// Directory mount, never subPath: a subPath does not receive kubelet
			// ConfigMap updates. As a mounted directory, updates to managed repos
			// in the ConfigMap are automatically synced live by the kubelet without
			// restarting the agent pod.
			Name:      gitopsStateVolumeName,
			MountPath: gitopsStateDir,
			ReadOnly:  true,
		},
		{
			// The one writable path outside the PVC, and the reason
			// readOnlyRootFilesystem is survivable here: docker-entrypoint.sh runs
			// four hermes invocations with HOME=/tmp before the agent starts, and
			// the image is otherwise root-owned against a runtime UID of 10000.
			Name:      tmpScratchVolumeName,
			MountPath: "/tmp",
		},
	}
}

// dropTmpScratchIfClaimed removes the operator's /tmp mount when the CR already mounts
// something there via storages or extraVolumeMounts.
//
// Those are appended to the defaults without deduplication, and the API server rejects a
// Pod spec with two mounts on one mountPath. So without this the /tmp mount added above
// would not merely be redundant on such a CR, it would make the Deployment unappliable and
// stop reconciliation dead — on an upgrade, for a field the user set before /tmp was a
// path the operator had any opinion about. Mounting scratch space at /tmp is exactly what
// someone would have done while the root filesystem was still writable and there was no
// other writable path outside the PVC, which is what makes this worth handling rather than
// rejecting.
//
// The user's mount wins, deliberately: it is the one that predates this, and overriding it
// would be a silent behaviour change on upgrade. If they mounted something read-only there
// the agent will fail to start — but it would have failed the same way before this change,
// since the entrypoint has always needed a writable /tmp.
//
// Narrow on purpose: it detects the collision by cleaned mountPath but removes the default
// by volume name, and only ever for /tmp. The general form — drop every default whose
// cleaned path a user mount claims — is the same amount of code and would cover the next
// default the operator adds. It would also silently drop the data PVC mount if a CR set
// homeDir to a path the defaults use, turning a Deployment the API server rejects outright
// into an agent that comes up with no persistent home. A rejected Deployment is the better
// failure of the two, so the generalisation waits for a second path that actually needs it.
func dropTmpScratchIfClaimed(defaults, userMounts []corev1.VolumeMount) []corev1.VolumeMount {
	claimed := false
	for _, m := range userMounts {
		if path.Clean(m.MountPath) == "/tmp" {
			claimed = true
			break
		}
	}
	if !claimed {
		return defaults
	}
	kept := make([]corev1.VolumeMount, 0, len(defaults))
	for _, m := range defaults {
		if m.Name != tmpScratchVolumeName {
			kept = append(kept, m)
		}
	}
	return kept
}

// hardenedSecurityContext is the container hardening every container the operator builds
// carries: no privilege escalation, no capabilities, and a root filesystem the process
// cannot write to.
//
// One helper rather than a literal per container, because the alternative has already
// failed. The same three fields were written out five times, and three of the five had
// silently drifted to two of them — the read-only root was on the credential sidecar and
// the cleanup init container and on neither agent container nor the log shipper, which is
// the gap this function was introduced to close (RUNTIME-007). Nothing failed while that
// was true. A container added from here on gets the block by construction, and
// TestEveryContainerHasAHardenedSecurityContext fails if one is built without it.
//
// Anything a container needs on top — a different user, a writable path — belongs on that
// container, not here. This is the floor, not the whole context.
func hardenedSecurityContext() *corev1.SecurityContext {
	return &corev1.SecurityContext{
		AllowPrivilegeEscalation: ptr.To(false),
		ReadOnlyRootFilesystem:   ptr.To(true),
		Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
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
		VolumeMounts:    []corev1.VolumeMount{{Name: "platform-agent-data-vol", MountPath: "/workspace"}},
		SecurityContext: hardenedSecurityContext(),
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

// scopedSAPoolKey is the ConfigMap key, and the basename the broker mounts it
// under. It rides in the credential-proxy policy ConfigMap rather than one of
// its own, for a reason worth keeping: that ConfigMap is already hashed into
// the Pod template annotation, so a change to the mapping rolls the broker.
// The broker reads this file once at startup and refuses to serve if it is
// unusable, so a mapping change that did not restart it would take effect at
// the next unrelated restart — which is the kind of delay nobody debugs.
const scopedSAPoolKey = "scoped-sa-pool.json"

const scopedSAPoolMountPath = "/etc/credential-proxy/" + scopedSAPoolKey

// scopedSAPoolJSON renders the mapping the broker consumes, or "" when the
// agent has none configured.
//
// Sorted by the scope key. The CR is a list and Kubernetes preserves its order,
// so an operator reordering two entries would otherwise rewrite the ConfigMap,
// change its hash and roll the broker for no change in meaning.
//
// No error return, because there is no failure to report: the document is a
// struct of strings and ints, which json.Marshal cannot fail on. An error
// return here would have to be either swallowed or propagated through a builder
// that has nowhere to put it, and a swallowed one would leave the broker armed
// by its environment variable with no mapping file to read.
func scopedSAPoolJSON(agent *agentv1alpha1.PlatformAgent) string {
	if agent.Spec.Security == nil || len(agent.Spec.Security.ScopedServiceAccounts) == 0 {
		return ""
	}
	type entry struct {
		ProjectID           string `json:"projectId"`
		Location            string `json:"location"`
		ClusterName         string `json:"clusterName"`
		ServiceAccountEmail string `json:"serviceAccountEmail"`
	}
	entries := make([]entry, 0, len(agent.Spec.Security.ScopedServiceAccounts))
	for _, account := range agent.Spec.Security.ScopedServiceAccounts {
		entries = append(entries, entry{
			ProjectID:           account.ProjectID,
			Location:            account.Location,
			ClusterName:         account.ClusterName,
			ServiceAccountEmail: account.ServiceAccountEmail,
		})
	}
	sort.Slice(entries, func(i, j int) bool {
		return scopedSAPoolScopeKey(entries[i].ProjectID, entries[i].Location, entries[i].ClusterName) <
			scopedSAPoolScopeKey(entries[j].ProjectID, entries[j].Location, entries[j].ClusterName)
	})
	document, _ := json.Marshal(struct {
		Version         int     `json:"version"`
		ServiceAccounts []entry `json:"serviceAccounts"`
	}{Version: 1, ServiceAccounts: entries})
	return string(document)
}

// scopedSAPoolScopeKey is the GKE resource name. Written here as well as in the
// broker and in Terraform because all three have to agree; the broker's
// `scoped_sa_pool.scope_key` and the key the Terraform module files each pool
// member under are the other two, and tests compare them.
func scopedSAPoolScopeKey(project, location, cluster string) string {
	return fmt.Sprintf("projects/%s/locations/%s/clusters/%s", project, location, cluster)
}

func scopedSAPoolEnabled(agent *agentv1alpha1.PlatformAgent) bool {
	return agent.Spec.Security != nil && len(agent.Spec.Security.ScopedServiceAccounts) > 0
}

func buildCredentialProxyPolicyConfigMap(agent *agentv1alpha1.PlatformAgent) *corev1.ConfigMap {
	data := map[string]string{"policy.json": credentialProxyPolicyJSON}
	if pool := scopedSAPoolJSON(agent); pool != "" {
		data[scopedSAPoolKey] = pool
	}
	return &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: "v1", Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name + "-credential-proxy-policy",
			Namespace: agent.Namespace,
		},
		Data: data,
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

// asNativeSidecar converts a container into a Kubernetes native sidecar: an init
// container that never exits and that the kubelet keeps running for the life of
// the pod.
//
// The distinction that matters here is ordering. App containers do not start
// until every native sidecar has started, which is what lets a listener claim
// its ports before the containers that share the namespace exist to contest
// them. See buildPodTemplateSpec for what "has started" does and does not
// guarantee.
//
// A native sidecar also needs a restart policy of its own. Without it the
// kubelet treats the container as an ordinary init container and waits for it to
// exit, which a long-running listener never does. For this container the failure
// arrives earlier than that: it declares a readinessProbe, which an init
// container may not have unless it is restartable, so the API server rejects the
// pod template rather than admitting one that would stall.
func asNativeSidecar(c corev1.Container) corev1.Container {
	c.RestartPolicy = ptr.To(corev1.ContainerRestartPolicyAlways)
	return c
}

// buildAgentAPIAuthSidecar returns what is left in the gateway pod after the
// credential runtime moved out: the authenticated front door for the Hermes API,
// and the k8s-event-watcher.
//
// Neither could follow the credential proxy into its own pod, and for the same
// reason. The API authenticator forwards to 127.0.0.1:8642, which is the Hermes
// gateway in this pod; the watcher posts its events to the Session KV server on
// 127.0.0.1:8699, which the agent container starts. Both are loopback peers of
// the agent, not of the credentials.
//
// What that leaves behind is a container with no credential path in it. It runs
// the same image, with CREDENTIAL_PROXY_ROLE=api-proxy telling
// deploy/shared/start-services.sh to start neither Envoy nor the executor, and
// its environment is built here rather than by buildCredentialProxyEnv so that
// the Slack and Google Chat tokens, the token-broker URL and the gcloud
// bootstrap stay out of the gateway pod entirely. The agent container cannot
// reach a credentialed endpoint on this pod's loopback because there is no
// longer one to reach — which is the property the sandbox split is for.
//
// The only shape there is. The credential runtime always runs in its own pod —
// see credential_proxy_manifests.go — so this is what the gateway Pod carries
// on every install.
func buildAgentAPIAuthSidecar(agent *agentv1alpha1.PlatformAgent, homeDir string) corev1.Container {
	image := resolveCredentialProxyImage(agent.Spec.Deployment)
	pullPolicy := corev1.PullAlways
	if agent.Spec.Deployment != nil && agent.Spec.Deployment.ImagePullPolicy != nil {
		pullPolicy = *agent.Spec.Deployment.ImagePullPolicy
	}
	envVars := buildAgentAPIAuthEnv(agent)
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
	envVars = append(envVars, corev1.EnvVar{Name: "CREDENTIAL_PROXY_ROLE", Value: "api-proxy"})
	// The plain hardened context, with no UID of its own. The credential runtime
	// used to sit in this Pod under a second uid, so that the shell could not
	// read its memory or its files; neither is here any more — the shell is in
	// the sandbox and the runtime is in the broker's own pod. What is left holds
	// no cloud credential, and it writes the watcher's dedup snapshots to the
	// shared PVC as the agent's own user.
	securityContext := hardenedSecurityContext()
	return corev1.Container{
		Name:            "agent-api-auth",
		Image:           image,
		ImagePullPolicy: pullPolicy,
		// Starts two of the image's three peer services — the API authenticator
		// and the k8s-event-watcher. See deploy/shared/start-services.sh.
		Command: []string{"/usr/local/bin/start-services"},
		Env:     envVars,
		Ports:   []corev1.ContainerPort{{Name: "proxy-api", ContainerPort: 8643}},
		// TCP, not the HTTP probe the credential proxy uses. Every path on this
		// listener requires the bearer key, so an unauthenticated GET is a 401
		// whether the pod is healthy or not, and there is no /healthz to ask
		// instead — that endpoint belongs to the credential runtime, which no
		// longer runs here.
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{TCPSocket: &corev1.TCPSocketAction{
				Port: intstr.FromString("proxy-api"),
			}},
			InitialDelaySeconds: 5,
			PeriodSeconds:       15,
			TimeoutSeconds:      3,
			FailureThreshold:    3,
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
			// No policy mount: the executor it configures is not built in this
			// role, and a policy file here would only be misleading.
			{Name: "credential-proxy-tmp", MountPath: "/tmp"},
			{Name: "event-watcher-kubeconfig", MountPath: "/var/run/event-watcher"},
			// Default audience. This is the token rest.InClusterConfig reads, so it
			// is what lets the watcher cover the management cluster, which never
			// gets a Cluster Agent profile.
			{Name: "event-watcher-ksa-token", MountPath: "/var/run/secrets/kubernetes.io/serviceaccount", ReadOnly: true},
			// The watcher's --profiles-dir and its dedup snapshots, both under
			// CREDENTIAL_PROXY_WORKSPACE_ROOT above.
			{Name: "platform-agent-data-vol", MountPath: homeDir},
		},
		SecurityContext: securityContext,
	}
}

// buildAgentAPIAuthEnv is the credential-free subset of buildCredentialProxyEnv:
// what the API authenticator and the event watcher need, and nothing that would
// put a credential back into the gateway pod. spec.deployment.env is merged the
// same way, so the WATCHER_* tunables still reach the watcher.
func buildAgentAPIAuthEnv(agent *agentv1alpha1.PlatformAgent) []corev1.EnvVar {
	apiServerSecretRef := defaultSecretRef(nil, defaultPlatformAgentSecrets, "API_SERVER_KEY")
	if harness := agent.Spec.Harness; harness != nil && harness.Hermes != nil && harness.Hermes.ApiServerSecretRef != nil {
		apiServerSecretRef = harness.Hermes.ApiServerSecretRef
	}
	envVars := []corev1.EnvVar{
		{Name: "PLATFORM_AGENT_HOME", Value: "/tmp/credential-proxy"},
		{Name: "HOME", Value: "/tmp/credential-proxy/home"},
		// $KUBECONFIG for anything in this container that shells out. The watcher
		// itself runs --in-cluster and does not read it.
		{Name: "KUBECONFIG", Value: "/var/run/event-watcher/watcher.config"},
		{Name: "AGENT_API_PROXY_PORT", Value: "8643"},
		{Name: "AGENT_API_UPSTREAM_KEY", Value: loopbackAgentAPIKey},
		{Name: "API_SERVER_KEY", Value: loopbackAgentAPIKey},
		// The key external callers present to the Service's api port. The
		// authenticator compares against it and swaps in the loopback sentinel
		// above before forwarding, so the gateway's own key never leaves this pod.
		{Name: "API_SERVER_EXTERNAL_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: apiServerSecretRef}},
		// The watcher authenticates to the Session KV server with this; see
		// start-services.sh --token-env.
		{Name: "SESSION_KV_API_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: sessionKVApiKeySecretRef(agent)}},
	}
	if agent.Spec.Deployment != nil {
		envVars = mergeCredentialProxyEnv(envVars, agent.Spec.Deployment.Env)
	}
	return envVars
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
		// Read by the k8s-event-watcher this container hosts, via --token-env.
		// A non-secret loopback sentinel, not a credential; the real secret is
		// API_SERVER_EXTERNAL_KEY below. Declared here rather than appended by
		// the caller so mergeCredentialProxyEnv sees it in the managed set and
		// reserves the name — appending after that call would leave it
		// protected only by its presence in SensitiveEnvVars, which is
		// incidental and would not hold for a name not on that list.
		{Name: "GITOPS_STATE_CONFIGMAP", Value: agent.Name + "-gitops-state"},
		{Name: "GITOPS_STATE_PATH", Value: path.Join(gitopsStateDir, "managed_repos")},
		{Name: "API_SERVER_KEY", Value: loopbackAgentAPIKey},
	}
	// Set in both directions, deliberately. The broker's own default is off, so
	// the "0" changes nothing on its own — what it buys is that the credential
	// mode an install is in can be read off the Deployment rather than inferred
	// from an absent variable. Same reason the mapping is a ConfigMap rather
	// than something the broker derives.
	if scopedSAPoolEnabled(agent) {
		envVars = append(envVars,
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL", Value: "1"},
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE", Value: scopedSAPoolMountPath},
		)
	} else {
		envVars = append(envVars, corev1.EnvVar{Name: "CREDENTIAL_PROXY_SCOPED_SA_POOL", Value: "0"})
	}
	// What the broker's own Pod changes about its configuration. The agent-API
	// front door is gone — it stayed in the agent Pod, so none of its three
	// variables are set here — Envoy listens on the Pod IP rather than loopback,
	// and the loopback that used to be the access control is replaced by a
	// verified caller.
	//
	// The authentication is not optional: credential_proxy.py refuses to serve a
	// listener reachable from outside its Pod with CREDENTIAL_PROXY_AUTH_MODE
	// unset, so a broker rendered without these would crash-loop at boot rather
	// than serve credentials to whoever reached the port.
	//
	// CREDENTIAL_PROXY_CONTENT_WORKSPACE has no field to turn it off. The Pods
	// share no volume, so a working-tree clone in a directory the agent named is
	// not something the broker can do at all — the fallback the flag guards
	// elsewhere does not exist here, and an install that could switch it off
	// would only be choosing to have the GitHub-writing skills fail.
	envVars = append(envVars,
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_ROLE", Value: "broker"},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_ENVOY_ADDRESS", Value: "0.0.0.0"},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_AUTH_MODE", Value: "serviceaccount"},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_AUDIENCE", Value: credentialProxyAudience},
		// The gateway's audience. Its absence is what an older operator looks
		// like to this broker, and the broker treats that as "no split" rather
		// than as "the gateway has the shell's role" — so the two can roll in
		// either order without chat answering 403 in between.
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_CHAT_AUDIENCE", Value: credentialProxyChatAudience},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_ALLOWED_CALLERS", Value: allowedBrokerCallers(agent)},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_KUBE_CA_FILE", Value: kubeAPIAccessMountPath + "/ca.crt"},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_KUBE_TOKEN_FILE", Value: kubeAPIAccessMountPath + "/token"},
		corev1.EnvVar{Name: "CREDENTIAL_PROXY_CONTENT_WORKSPACE", Value: "1"},
	)
	if harness := agent.Spec.Harness; harness != nil && harness.ProjectID != "" && harness.Location != "" && harness.ClusterName != "" {
		envVars = append(envVars,
			corev1.EnvVar{Name: "GKE_PROJECT_ID", Value: harness.ProjectID}, corev1.EnvVar{Name: "GKE_CLUSTER_NAME", Value: harness.ClusterName}, corev1.EnvVar{Name: "GKE_LOCATION", Value: harness.Location},
			corev1.EnvVar{Name: "KUBE_CONTEXT_NAME", Value: fmt.Sprintf("gke_%s_%s_%s", harness.ProjectID, harness.Location, harness.ClusterName)}, corev1.EnvVar{Name: "KUBE_DEFAULT_NAMESPACE", Value: agent.Namespace},
			// The GKE_DNS_FLAG step decides whether the harness cluster has to be
			// reached over its DNS endpoint rather than its IP one. The reconciler
			// cannot answer that when it renders the manifest — the answer is a
			// property of the cluster, read at bootstrap time — so the describe is
			// inlined here. agents/platform/scripts/gke_endpoint.py and
			// scripts/installer/gke_dns_endpoint.sh implement the same predicate;
			// keep all three in step.
			//
			// Deciding on the configuration rather than trying --dns-endpoint and
			// falling back is deliberate: for a caller Google recognises as internal,
			// gcloud downgrades the allowExternalTraffic rejection to a warning and
			// still writes a kubeconfig naming the DNS endpoint, which then 403s on
			// every request. A failed probe would look like success.
			//
			// The assignment is safe inside the && chain even when the cluster cannot
			// be described: awk ends the pipeline, and it exits 0 on empty input, so
			// an unreadable cluster yields an empty flag and the get-credentials that
			// shipped before this existed. $GKE_DNS_FLAG is unquoted so that empty
			// contributes no argument at all.
			corev1.EnvVar{Name: "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND", Value: `gcloud config set project "$GKE_PROJECT_ID" >/dev/null &&
GKE_DNS_FLAG="$(gcloud container clusters describe "$GKE_CLUSTER_NAME" --location "$GKE_LOCATION" --project "$GKE_PROJECT_ID" --format='value(controlPlaneEndpointsConfig.dnsEndpointConfig.endpoint,controlPlaneEndpointsConfig.dnsEndpointConfig.allowExternalTraffic)' 2>/dev/null | awk -F'\t' '$1 != "" && $2 == "True" { print "--dns-endpoint" }')" &&
gcloud container clusters get-credentials "$GKE_CLUSTER_NAME" --location "$GKE_LOCATION" --project "$GKE_PROJECT_ID" $GKE_DNS_FLAG &&
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
		if teams := integration.Teams; teams != nil && teams.Enabled != nil && *teams.Enabled {
			envVars = append(envVars,
				corev1.EnvVar{Name: "TEAMS_APP_ID", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(teams.AppIdSecretRef, defaultPlatformAgentSecrets, "TEAMS_APP_ID")}},
				corev1.EnvVar{Name: "TEAMS_APP_PASSWORD", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: defaultSecretRef(teams.AppPasswordSecretRef, defaultPlatformAgentSecrets, "TEAMS_APP_PASSWORD")}},
			)
			if teams.TenantId != "" {
				envVars = append(envVars, corev1.EnvVar{Name: "TEAMS_TENANT_ID", Value: teams.TenantId})
			}
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
		// The authentication settings are reserved for the same reason the
		// bootstrap command is: a plugin that could set CREDENTIAL_PROXY_AUTH_MODE
		// could turn the caller check off, and one that could set
		// CREDENTIAL_PROXY_ALLOWED_CALLERS could add itself to it.
		"CREDENTIAL_PROXY_ALLOWED_CALLERS",
		"CREDENTIAL_PROXY_AUDIENCE",
		"CREDENTIAL_PROXY_AUTH_MODE",
		// And one that could set CREDENTIAL_PROXY_CHAT_AUDIENCE to the shell's
		// audience would collapse the two roles into one, which is how the
		// broker spells "no split".
		"CREDENTIAL_PROXY_CHAT_AUDIENCE",
		"CREDENTIAL_PROXY_BOOTSTRAP_COMMAND",
		// The listen address is reserved for the placements as well as for the
		// authentication: it is appended after this merge in every container
		// the sidecar split into, and an operator who set it to 127.0.0.1
		// through spec.deployment.env would take the credential proxy off the
		// network and break every wrapped CLI in the sandbox.
		"CREDENTIAL_PROXY_ENVOY_ADDRESS",
		"CREDENTIAL_PROXY_KUBE_CA_FILE",
		"CREDENTIAL_PROXY_KUBE_TOKEN_FILE",
		// The read-only kill switch. Unreserved, a one-line
		// `CREDENTIAL_PROXY_ENFORCE_READ_ONLY: "false"` under
		// spec.deployment.env turns off every refusal the policy makes --
		// for all commands, all agents and all clusters in the Pod, with no
		// expiry and nothing in the CR that reads like a security change.
		// A control that its own subject can switch off is not a control.
		//
		// Listed here as well as in SensitiveEnvVars, which this loop already
		// folds in above, because the two do different jobs: the webhook's
		// rejection is the explanation and this drop is the enforcement. The
		// chart defaults failurePolicy to Ignore, so a webhook that cannot be
		// reached admits the CR with validation skipped, and this line is
		// what still holds when that happens.
		"CREDENTIAL_PROXY_ENFORCE_READ_ONLY",
		"CREDENTIAL_PROXY_MAX_OUTPUT_BYTES",
		"CREDENTIAL_PROXY_MAX_REQUEST_BYTES",
		"CREDENTIAL_PROXY_POLICY",
		"CREDENTIAL_PROXY_PORT",
		"CREDENTIAL_PROXY_ROLE",
		// Same argument as the authentication settings above, one layer over.
		// A plugin that could set CREDENTIAL_PROXY_SCOPED_SA_POOL would switch
		// the broker back onto the agent's own project-wide identity, and one
		// that could set the FILE variable would point it at a mapping of its
		// own choosing — naming, for instance, an account it would rather be.
		//
		// The flag is in `managed` on every render, so the loop above already
		// reserves it and this line is belt and braces. The FILE variable is
		// only in `managed` when a pool is configured, so on an install with no
		// pool this line is the only thing reserving it.
		"CREDENTIAL_PROXY_SCOPED_SA_POOL",
		"CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE",
		"CREDENTIAL_PROXY_STATE_DIR",
		"CREDENTIAL_PROXY_TIMEOUT_SECONDS",
		"CREDENTIAL_PROXY_UNIX_SOCKET",
		"CREDENTIAL_PROXY_WORKSPACE_ROOT",
		// Both appended by buildAgentAPIAuthSidecar after this merge runs,
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
	//
	// EOD_EXCLUDE_NAMESPACES is the end-of-day recap's only tunable. It
	// narrows what its listing prints and reaches nothing the notifier does: no
	// event stops being forwarded, no alert stops being posted, and a ceiling
	// drop or a failed delivery in an excluded namespace is still counted and
	// still withholds the recap's all-clear — `eod_report_generator.py` flags
	// an excluded row rather than skipping it, so the loop keeps tallying it.
	// That is the property this allowlist entry rests on: no value of
	// EOD_EXCLUDE_NAMESPACES can tune the recap into hiding a withheld alert
	// or a refused post.
	//
	// It buys less than that in one respect, and the difference is worth
	// stating rather than rounding off. What an exclusion does reach is the
	// informational tally, which is the point of it, and the exclusion count
	// is deliberately not a veto term — so a day whose only informational
	// churn sat in an excluded namespace still grades green, over a window the
	// recap did not fully read. The scope caveat rides a qualifier line in the
	// report body instead. The bound is that the overclaim is confined to
	// informational churn; the two alert tallies above are what an operator
	// setting this variable cannot touch.
	//
	// Any value parses: `excluded_namespaces` comma-splits the string and
	// matches the parts literally, so an arbitrary one names namespaces that do
	// not exist and excludes nothing. There is no validation to fail.
	allowed := map[string]struct{}{
		"ALERT_DAILY_LIMIT_CRITICAL":  {},
		"ALERT_DAILY_LIMIT_INFO":      {},
		"ALERT_DAILY_LIMIT_WARNING":   {},
		"EOD_EXCLUDE_NAMESPACES":      {},
		"OTEL_EXPORTER_OTLP_ENDPOINT": {},
		"OTEL_EXPORTER_OTLP_PROTOCOL": {},
		"OTEL_RESOURCE_ATTRIBUTES":    {},
		"OTEL_SDK_DISABLED":           {},
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

// buildEventWatcherKubeconfigVolume is the kubeconfig the broker writes for the
// event-watcher. It belongs to the event-watcher container, so when the broker
// moves to its own Pod this stays here as well as going there.
func buildEventWatcherKubeconfigVolume() corev1.Volume {
	return corev1.Volume{Name: "event-watcher-kubeconfig", VolumeSource: corev1.VolumeSource{
		EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("1Mi"))},
	}}
}

// buildEventWatcherTokenVolume is a default-audience ServiceAccount token with
// the cluster CA and namespace beside it — the conventional in-cluster client
// bundle. The event-watcher authenticates to the API server with it, and when
// the broker is split it makes the broker's TokenReview call with it too.
func buildEventWatcherTokenVolume() corev1.Volume {
	return corev1.Volume{Name: "event-watcher-ksa-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
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
	}}}
}

func buildCredentialProxyVolumes(agent *agentv1alpha1.PlatformAgent) []corev1.Volume {
	return []corev1.Volume{
		{Name: "credential-proxy-policy", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: agent.Name + "-credential-proxy-policy"}}}},
		{Name: "credential-proxy-tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("2Gi"))}}},
		{Name: "credential-proxy-state", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{SizeLimit: ptr.To(resource.MustParse("5Gi"))}}},
		{Name: "credential-proxy-runtime", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{Medium: corev1.StorageMediumMemory, SizeLimit: ptr.To(resource.MustParse("16Mi"))}}},
		buildEventWatcherKubeconfigVolume(),
		{Name: "credential-proxy-ksa-token", VolumeSource: corev1.VolumeSource{Projected: &corev1.ProjectedVolumeSource{
			DefaultMode: ptr.To(int32(0400)),
			Sources: []corev1.VolumeProjection{{ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
				Audience: credentialProxyAudience, ExpirationSeconds: ptr.To(int64(3600)), Path: "token",
			}}},
		}}},
		buildEventWatcherTokenVolume(),
	}
}

func buildGitopsStateVolume(agent *agentv1alpha1.PlatformAgent) corev1.Volume {
	return corev1.Volume{
		Name: gitopsStateVolumeName,
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{
					Name: agent.Name + "-gitops-state",
				},
				DefaultMode: ptr.To(int32(0644)),
			},
		},
	}
}

// mountIntoContainer adds a mount to the named container, by name rather than
// by index: the container list is assembled from several builders and the
// indices move.
func mountIntoContainer(containers []corev1.Container, name string, mount corev1.VolumeMount) {
	for index := range containers {
		if containers[index].Name == name {
			containers[index].VolumeMounts = append(containers[index].VolumeMounts, mount)
			return
		}
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
		// "latest", not the default platform-agent version.
		suffix = ":latest"
	}
	return prefix + name + suffix
}

// agentAPIProbe returns a probe that asks the Hermes API on loopback for one
// session. Callers supply periodSeconds and failureThreshold, which is the only
// difference between the gateway's startup and readiness probes: the startup
// one has to cover a cold boot that scaffolds every profile onto a fresh PVC,
// while readiness afterwards should withdraw the pod quickly.
//
// /api/sessions is the endpoint the agent's own callers use — see the pubsub
// adapter and admin_console — and the Authorization: Bearer form is theirs too.
// Every timing is explicit, per the gke-reliability skill's rule 3; kubelet's
// 1-second default timeout is far too tight for a container this busy at boot.
//
// The exit-7 branch is what makes this probe safe above one replica. At
// replicas > 1 the container runs leader_elect.py, and a pod that does not hold
// the lease never starts `hermes gateway run` at all — nothing binds 8642, so a
// plain curl probe would fail every attempt and kubelet would kill a standby
// that is doing exactly its job. curl exits 7 for "could not connect", which is
// precisely that state, so it counts as healthy while leader election is on.
// It is deliberately not tolerated on a single-replica agent, where nothing
// listening means the gateway is down.
//
// Tolerating 7 does not hide a dead leader: leader_elect.py exits with the
// gateway's own status when the process it started dies, so the container
// restarts rather than lingering unreachable. And it is a connection refusal
// only — a gateway that answers with 5xx exits 22, and a hung one 28, both of
// which still fail. Detecting the standby by looking for the process instead
// would not work: `hermes` is a shim that execs `s6-suid hermes $REAL "$@"`, so
// the string "hermes gateway run" never appears in any command line to match.
func agentAPIProbe(periodSeconds, failureThreshold int32) *corev1.Probe {
	return &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{
			Exec: &corev1.ExecAction{
				Command: []string{
					"sh", "-c",
					`curl --fail --silent --show-error -o /dev/null ` +
						`-H "Authorization: Bearer $API_SERVER_KEY" ` +
						`http://127.0.0.1:8642/api/sessions?limit=1; rc=$?; ` +
						`[ "$rc" -eq 0 ] && exit 0; ` +
						`[ "$rc" -eq 7 ] && [ "$ENABLE_LEADER_ELECTION" = "true" ] && exit 0; ` +
						`exit "$rc"`,
				},
			},
		},
		InitialDelaySeconds: 5,
		PeriodSeconds:       periodSeconds,
		TimeoutSeconds:      5,
		FailureThreshold:    failureThreshold,
	}
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

	var userVolumeMounts []corev1.VolumeMount
	if len(storages) > 0 {
		userVolumeMounts = append(userVolumeMounts, buildCustomStorageVolumeMounts(storages)...)
	}
	if len(extraVolumeMounts) > 0 {
		userVolumeMounts = append(userVolumeMounts, extraVolumeMounts...)
	}
	volumeMounts := append(dropTmpScratchIfClaimed(buildDefaultVolumeMounts(homeDir), userVolumeMounts), userVolumeMounts...)
	// The staged SSH key, read-only. Only the emptyDir the init container wrote —
	// the container that opens the connection has no reason to see the Secret mount
	// the init container read from.
	volumeMounts = append(volumeMounts, buildShellSandboxClientKeyMount())

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
	switch {
	case replicas > 1:
		// The wrapper starts the gateway itself, so the profile reaches it through
		// gatewayProfileEnvVar rather than through this argv.
		args = []string{"/opt/hermes/.venv/bin/python3", fmt.Sprintf("%s/leader_elect.py", homeDir)}
	case platformFrontDoorEnabled(agent):
		// Overrides the image's CMD, which is a bare `hermes gateway run`. The flag is
		// global and pre-parsed: hermes_cli/main.py strips -p/--profile out of argv
		// before any import and re-points HERMES_HOME at the profile's home, so the
		// gateway comes up as the Platform Agent. `hermes gateway run --profile` would
		// not work — the subcommand has no such flag — and the position therefore
		// matters.
		args = []string{"hermes", "--profile", platformProfileName, "gateway", "run"}
	}

	for _, plugin := range agentPlugins {
		volumeMounts = append(volumeMounts, corev1.VolumeMount{
			Name:      buildPluginVolumeName(plugin.Name),
			MountPath: pluginMountPath(homeDir, plugin),
		})
	}

	// APPENDED LAST, and that position is the guard, not a style choice. It is not routed
	// through mergeEnvVars because this is the operator's own declaration rather than a
	// default a user may replace, and one caller can in fact try: `spec.deployment.env`
	// cannot reach this container (safeSandboxEnvOverrides copies a fixed allowlist and
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

	// Appended last for the same reason as the two above: an AgentPlugin's spec.env
	// reaches envVars verbatim, and a plugin that named this variable would re-home the
	// gateway — or, worse, un-home it while the overlay still configures the platform
	// profile as the front door, leaving chat on the default profile while the toolsets,
	// ingress plugins and kanban settings meant for it sit on a profile receiving none.
	//
	// Which is why it is appended UNCONDITIONALLY, empty when the flag is off, rather
	// than only when there is a profile to name. Last-wins only settles a duplicate; a
	// name the operator never emits is not a duplicate, so a plugin declaring
	// HERMES_GATEWAY_PROFILE=platform on a flag-off install would be the only writer and
	// would re-home the gateway to a profile whose overlay carries no ingress keys at
	// all. Both readers treat empty as off — leader_elect.py falls back to the default
	// profile, and the entrypoint's platform_is_front_door tests for `platform`
	// exactly — so the off value is a real answer rather than a placeholder.
	frontDoorProfile := ""
	if platformFrontDoorEnabled(agent) {
		frontDoorProfile = platformProfileName
	}
	gatewayEnvVars = append(gatewayEnvVars, corev1.EnvVar{
		Name:  gatewayProfileEnvVar,
		Value: frontDoorProfile,
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
			// Without these the Service publishes this pod the moment the container
			// process starts, minutes before the Hermes API binds :8642 — the
			// entrypoint scaffolds every profile onto the PVC before it execs the
			// gateway. Callers that resolve the Service in that window get
			// connection-refused from a pod Kubernetes calls Ready.
			//
			// exec, not httpGet: API_SERVER_HOST is 127.0.0.1 (the sidecar's Envoy on
			// :8643 is what the Service targets), and kubelet dials the pod IP, so an
			// httpGet or tcpSocket probe would never reach a loopback listener. This
			// is the same shape as the credential proxy's own probe below.
			//
			// The bearer key is the non-secret loopback sentinel already in this
			// container's env, and API_SERVER_ENABLED is unconditionally true above,
			// so the probe is valid in every configuration.
			StartupProbe:    agentAPIProbe(10, 60),
			ReadinessProbe:  agentAPIProbe(15, 3),
			SecurityContext: hardenedSecurityContext(),
		},
	}

	if isDashboardEnabled(agent) {
		dashboardEnvVars := []corev1.EnvVar{
			{
				Name:  "PLATFORM_AGENT_HOME",
				Value: homeDir,
			},
			{
				// Same value as the gateway's, and it has to be: this container loads the
				// same PVC config.yaml, so it must have the same operator pins overlaid on
				// top of it. Without this the dashboard would read the agent's own writes
				// unpinned — including a model endpoint or a front-door allowlist the
				// agent had changed for itself.
				Name:  "HERMES_MANAGED_DIR",
				Value: managedScopeDir,
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
				// The gateway's arrangement exactly: the PVC's own config.yaml, with the
				// managed scope overlaid at load. That equality is the point. This
				// container used to subPath-mount the operator's render over
				// $HERMES_HOME/config.yaml instead, to guarantee SOME config existed on a
				// fresh volume before the gateway's setup pass seeded one — but a mount
				// cannot be conditional. It shadowed the PVC copy on every volume, so the
				// dashboard read a config the gateway never read, and narrowing
				// renderConfigYAML to the pinned subtrees silently narrowed this
				// container's entire config to them: no plugins.enabled, no kanban, no
				// toolsets, and no agent.disabled_toolsets — the denylist that
				// agents/chat/config.yaml calls the authoritative guarantee that the
				// front door has no runtime tools of its own.
				//
				// The presence guarantee moved to where it can be conditional: the
				// non-owner branch at step 1.5 of deploy/shared/docker-entrypoint.sh waits
				// (bounded) for $TARGET_DIR/config.yaml before exec'ing. Anything added to
				// the render from here on reaches both containers or neither.
				Name:      managedVolumeName,
				MountPath: managedScopeDir,
				ReadOnly:  true,
			},
			{
				Name:      "system-metadata",
				MountPath: path.Dir(sessionKVDBPath),
				SubPath:   "session",
			},
			{
				// Same emptyDir the gateway gets. Sharing it is not a new channel:
				// these two containers already run the same image against the same
				// data PVC, so they are one trust domain either way.
				Name:      tmpScratchVolumeName,
				MountPath: "/tmp",
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
					ContainerPort: dashboardPort,
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
			// Through the same guard as the gateway's list above. extraVolumeMounts
			// reaches both containers, so a CR claiming /tmp collides here too.
			VolumeMounts: append(dropTmpScratchIfClaimed(dashboardVolumeMounts, extraVolumeMounts), extraVolumeMounts...),
			// What this buys is not what a probe on a serving container buys. The
			// Service's :9119 endpoint is unreachable over the pod network either
			// way (see dashboardPort), so nothing is being kept out of rotation.
			// Pod readiness is the AND of every container, so this reports a
			// broken dashboard through the pod's own Ready condition — and, the
			// other side of the same coin, a dashboard that hangs or OOM-loops
			// now withdraws the agent API on :8642 with it. That coupling is the
			// price of reporting it at all; the alternative is no probe here,
			// which leaves a dead dashboard silent.
			//
			// exec on loopback, not tcpSocket. `hermes dashboard` takes no --host
			// argument here and the CLI's default is 127.0.0.1, so a tcpSocket probe
			// — which kubelet dials against the pod IP — was refused on every
			// attempt and this container never went Ready. That held the whole pod
			// NotReady, drove the CR to Ready=False, and failed the install's
			// rollout gate on any install that did not pin dashboardEnabled=false
			// (#822). The comment this replaces asserted the opposite binding.
			// Same shape, and the same reason, as agentAPIProbe above.
			//
			// Binding all interfaces instead is not the smaller fix, and not just
			// because no auth provider is configured: the dashboard's auth gate
			// keys on the bind host, so 0.0.0.0 switches authentication on and the
			// server then exits at startup rather than serve unauthenticated. The
			// loopback bind is what keeps it usable. scripts/hermes-dashboard-
			// tunnel.py is canonical on that and on how a human reaches it.
			//
			// No --fail and no health path: `hermes dashboard` exposes no health
			// endpoint we have verified, and demanding a 2xx from a guessed one
			// would 404 and hold the pod unready for the wrong reason. Without
			// --fail curl exits 0 on any HTTP status, so serving the SPA at / is
			// enough and so would be a 401. Plain http:// is right — that tunnel
			// script relays cleartext HTTP off this port.
			//
			// So the exit code passes straight through: 0 means it answered, 7
			// means connection refused — the exact state that made this container
			// never go Ready — and 28 means it accepted and then hung. --max-time
			// sits under TimeoutSeconds so 28 is curl's to report rather than
			// kubelet's to kill.
			ReadinessProbe: &corev1.Probe{
				ProbeHandler: corev1.ProbeHandler{
					Exec: &corev1.ExecAction{
						Command: []string{
							"sh", "-c",
							fmt.Sprintf("curl --silent --show-error --max-time 3 -o /dev/null http://127.0.0.1:%d/", dashboardPort),
						},
					},
				},
				InitialDelaySeconds: 5,
				PeriodSeconds:       15,
				TimeoutSeconds:      5,
				FailureThreshold:    3,
			},
			SecurityContext: hardenedSecurityContext(),
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
		// Read-only root and no /tmp, the only container here with neither. The config
		// above buffers in memory (Mem_Buf_Limit, no storage.path), keeps its tail DB on
		// the fluent-bit-state volume and outputs to stdout, so it writes nothing to the
		// root filesystem. Handing it the agent's tmp-scratch would only give an
		// LLM-driven container a path into the log shipper.
		SecurityContext: hardenedSecurityContext(),
	})

	// The k8s-event-watcher is not a container of its own. It runs inside
	// agent-api-auth, beside the Session KV server it posts to; see
	// buildAgentAPIAuthSidecar.

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
			// The same ConfigMap again, projected under the two names Hermes looks for in
			// a managed scope. Item-projected rather than a whole-directory mount so
			// /etc/hermes holds exactly config.yaml and .env — managed_scope.py reads
			// only those, and the profile overlays and leader_elect.py alongside them in
			// this ConfigMap have no business in an administrator policy directory.
			//
			// 0444: managed scope's v1 enforcement is filesystem permissions only —
			// hermes_cli/managed_scope.py says so in its module docstring, and the design
			// note it cites lives in the Hermes tree, not this one. The mount is already
			// ReadOnly; the mode makes the intent legible from the manifest.
			Name: managedVolumeName,
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: agent.Name + "-config",
					},
					Items: []corev1.KeyToPath{
						{Key: managedConfigKey, Path: "config.yaml"},
						{Key: managedEnvKey, Path: ".env"},
					},
					DefaultMode: ptr.To(int32(0444)),
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
						Name: settingsConfigMapName(agent),
					},
					DefaultMode: ptr.To(int32(0644)),
				},
			},
		},
		buildGitopsStateVolume(agent),
		{
			// Bounded, like every other scratch emptyDir here. Without a
			// sizeLimit a runaway write fills the node's ephemeral storage and the
			// kubelet evicts whoever it decides is the worst offender, which need
			// not be this pod. With one, the eviction is this pod, for a stated
			// reason, at a predictable threshold. Note that it is an eviction
			// either way: enforcing the limit as an in-container ENOSPC needs the
			// alpha LocalStorageCapacityIsolationFSQuotaMonitoring gate, which GKE
			// does not enable. 2Gi matches credential-proxy-tmp, the closest
			// analogue.
			//
			// Declared unconditionally, while dropTmpScratchIfClaimed can remove the
			// mount from both containers -- so a CR that claims /tmp itself leaves
			// this volume in the pod with nothing mounting it. That is legal and
			// inert: the kubelet creates an empty directory and no container sees it.
			// Making the declaration conditional would mean deciding it here from
			// state that lives in the mount builders, which buys a tidier pod spec
			// for a coupling that is easier to get wrong than this is to explain.
			Name: tmpScratchVolumeName,
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{
					SizeLimit: ptr.To(resource.MustParse("2Gi")),
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
	saName := agentServiceAccountName(agent)

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
		// Connecting to this port from another pod gets connection refused: the
		// dashboard listens on loopback only (see dashboardPort). It is published
		// anyway because `kubectl port-forward svc/<agent> 9119:9119` needs the
		// Service to name the port, and port-forward is how the dashboard is
		// reached.
		ports = append(ports, corev1.ServicePort{
			Name:       "dashboard",
			Port:       dashboardPort,
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

// buildPlatformPDB generates the PodDisruptionBudget manifest for PlatformAgent.
//
// maxUnavailable: 1 at every replica count, which is the shape the Workload
// Reliability Audit this project ships requires:
// agents/platform/governance/obtainability_audit_sop.md §3.3 — "Always
// maxUnavailable, never minAvailable ... maxUnavailable: 1 is structurally safe
// at any replica count >= 2."
//
// The reason it is unconditional rather than derived from the replica count is
// that a budget keyed to replicas is only safe while the replica count holds.
// minAvailable: 1 against one replica leaves zero allowed disruptions, so
// `kubectl drain` never completes and node-pool upgrades, auto-repair, and
// autoscaler scale-down all stall until a human deletes this object — the
// critical `blocking-pdb` finding of §3.4. Deriving the field from the resolved
// count avoids that on the way up but not on the way down: a scaled-out agent
// carrying minAvailable: 1 that is later scaled back to one produces exactly
// that deadlock, and nothing reconciles the budget at the moment someone runs
// `kubectl scale`.
//
// The selector is the Deployment's, NOT the Service's. Above, a multi-replica
// Service narrows to kubeagents.io/is-leader so only the leader serves; a PDB
// carrying that label would budget the single leader pod rather than the
// Deployment's pods.
func buildPlatformPDB(agent *agentv1alpha1.PlatformAgent) *policyv1.PodDisruptionBudget {
	return &policyv1.PodDisruptionBudget{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "policy/v1",
			Kind:       "PodDisruptionBudget",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      agent.Name,
			Namespace: agent.Namespace,
		},
		Spec: policyv1.PodDisruptionBudgetSpec{
			MaxUnavailable: ptr.To(intstr.FromInt32(1)),
			Selector: &metav1.LabelSelector{
				MatchLabels: map[string]string{
					"app": agent.Name + "-gateway",
				},
			},
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
		// GKE DNS-based control plane endpoints. get-credentials prefers these
		// over the IP endpoint wherever a cluster publishes one that accepts
		// external traffic, so the kubeconfig names a Google frontend rather
		// than an address in apiCIDRs. Without this the pod authenticates
		// against the control plane it can no longer reach: rule 6 covers the
		// IP endpoints only, and FQDN mode is exactly when the blanket
		// 0.0.0.0/0:443 rule is withheld.
		//
		// A pattern wildcard spans one label and no dots, so the two-label
		// form is what actually matches an endpoint: the hostname is
		// <cluster-hash>-<project-number>.<region>.gke.goog. Every other
		// wildcard in this list needs exactly one label, so nothing here
		// exercises the deeper shape — see TestFQDNPatternList_MatchesRealHostnames.
		"*.gke.goog",
		"*.*.gke.goog",
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
		"login.microsoftonline.com",
		"*.login.microsoftonline.com",
		"botframework.com",
		"*.botframework.com",
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
		return managedOTelCollectorNamespace
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
// NetworkPolicyPeers. A bare IP becomes a single-host /32 or /128. Anything unparseable
// is dropped.
//
// enforceMinPrefix rejects CIDRs broader than /12 (IPv4) or /48 (IPv6), which stops a
// caller-supplied range from being weaponised into an unrestricted egress bypass. Pass
// false only where the input cannot come from outside the operator.
//
// normalizeCIDRTarget does the per-entry work, shared with toEgressRules -- including
// the address-family rule that keeps an IPv4-mapped IPv6 block from clearing the IPv6
// floor and then printing as 0.0.0.0/0.
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
		if ipNet, ok := normalizeCIDRTarget(entry, enforceMinPrefix); ok {
			add(ipNet.String())
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

// peersNotAlreadyPresent returns the candidates whose ipBlock CIDR no peer in
// present already names. It exists because formatCIDRPeers dedupes only within
// a single call, so two calls contributing to one rule's peer list can each
// emit the same CIDR. Peers carrying no ipBlock are always kept: a selector
// peer is not comparable to a CIDR and is never the duplicate being removed.
func peersNotAlreadyPresent(present, candidates []networkingv1.NetworkPolicyPeer) []networkingv1.NetworkPolicyPeer {
	seen := make(map[string]bool, len(present))
	for _, peer := range present {
		if peer.IPBlock != nil {
			seen[peer.IPBlock.CIDR] = true
		}
	}
	kept := make([]networkingv1.NetworkPolicyPeer, 0, len(candidates))
	for _, candidate := range candidates {
		if candidate.IPBlock != nil && seen[candidate.IPBlock.CIDR] {
			continue
		}
		kept = append(kept, candidate)
	}
	return kept
}

// buildNetworkPolicy generates the restrictive NetworkPolicy manifest for PlatformAgent.
// Note: This is the operator-generated version; Kustomize static deployments use deploy/kustomize/platform/.
//
// otlpDisabled carries the same meaning as renderOptions.otlpDisabled: discovery found no
// collector, so there is no export to allow and the collector egress rule is left out.
// clusterDNSPeers is every peer a pod's DNS egress rule has to name on GKE, and
// the one definition both this file's gateway policy and the shell sandbox's own
// policy use. Naming it once is not tidiness: the sandbox policy shipped with
// only the kube-dns podSelector, and on a cluster running NodeLocal DNSCache
// every lookup from the sandbox failed with "Temporary failure in name
// resolution" — which reads as the credential broker being down rather than as a
// policy drop, because the broker is the first thing the sandbox resolves.
//
// The peers cover every way a lookup leaves the pod: the kube-dns Pods
// themselves, the node-local-dns Pods, the link-local address NodeLocal DNSCache
// listens on, the metadata address that answers DNS under Cloud DNS for GKE, and
// the kube-dns Service VIP for a dataplane that evaluates policy before the
// ClusterIP is translated.
func clusterDNSPeers(dnsIPs []string) []networkingv1.NetworkPolicyPeer {
	peers := []networkingv1.NetworkPolicyPeer{
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
				CIDR: nodeLocalDNSCacheIP,
			},
		},
	}

	// Cloud DNS for GKE. There the cluster does not resolve through kube-dns at
	// all: the node answers DNS on the metadata address, and a Pod's resolv.conf
	// names 169.254.169.254. So none of the peers above is the resolver, and
	// without this one the Pod has no name resolution — which is a total outage,
	// because every destination in the rules built from these peers is reached by
	// name. For the sandbox that reads as the credential broker being down, since
	// the broker is the first name it resolves.
	//
	// Unconditional rather than detected. Cloud DNS is detectable in principle —
	// kubelet's --cluster-dns carries this address there, so the operator's own
	// resolv.conf names it — but the discovery this policy already does is
	// resolveNetpolProfile reading the kube-system/kube-dns Service ClusterIP,
	// and under Cloud DNS that Service still exists and still answers nothing.
	// That discovery succeeds and is wrong, which is the failure being avoided:
	// a detector that guesses wrong costs the install its name resolution, while
	// granting the peer always costs one port-53 rule on clusters not using it.
	//
	// On a kube-dns cluster the Pod's resolver is the kube-dns ClusterIP, so the
	// peer carries no traffic — but it is not inert. On GCE this address answers
	// DNS on 53 unless Workload Identity's gke-metadata-server or metadata
	// concealment intercepts it, so on a cluster running neither, the rule does
	// reach the node's GCE resolver. That is a resolver and not a credential
	// path: the token API is HTTP on 80 pre-NAT and 988 post-NAT, and every
	// caller of this helper puts these peers behind port 53 alone. The sandbox
	// policy is the one that matters there — it grants no other rule naming a
	// metadata address, so port 53 is the whole of its reach.
	//
	// Through metadataResolverCIDR, the name the egress-allowlist builder grants
	// it under, so a grep for that constant finds both places the resolver is
	// permitted. The grant is IPv4-only on purpose: fd20:ce::254 is documented as
	// a metadata endpoint rather than as a resolver, and no static copy in
	// charts/ or deploy/kustomize names it in a DNS rule, so it stays out until a
	// dual-stack Cloud DNS cluster is observed naming it in a Pod's resolv.conf.
	peers = append(peers, formatCIDRPeers([]string{metadataResolverCIDR}, true)...)

	// Through formatCIDRPeers rather than another spelling of /32-or-/128 in this
	// file: it shares normalizeCIDRTarget with toEgressRules, and it sorts and
	// dedupes. enforceMinPrefix is false because these are bare IPs resolved by the
	// operator, which always widen to a single host; the resolver peer above passes
	// true, and a bare address clears the floor either way. The default is the
	// fallback for nothing surviving, not for each entry that does not parse -- two
	// bad entries used to emit the default twice.
	dnsIPPeers := formatCIDRPeers(dnsIPs, false)
	if len(dnsIPPeers) == 0 {
		dnsIPPeers = formatCIDRPeers([]string{defaultDNSClusterIP}, false)
	}
	// formatCIDRPeers dedupes within one call, not across the two above, and on a
	// Cloud DNS cluster the two overlap: 169.254.169.254 is what kubelet's
	// --cluster-dns carries there, so an operator setting
	// spec.networkPolicy.dnsClusterIPs to the value their nodes actually use names
	// the address the resolver peer already grants. Without this filter that
	// renders the same ipBlock twice — legal, and no wider, but a policy sold as
	// auditable should not make a reader wonder which of the two is doing the work.
	return append(peers, peersNotAlreadyPresent(peers, dnsIPPeers)...)
}

func buildNetworkPolicy(agent *agentv1alpha1.PlatformAgent, apiCIDRs []string, profile netpolProfile, fqdnEnabled bool, otlpEndpoint string, otlpDisabled bool) *networkingv1.NetworkPolicy {
	udp := corev1.ProtocolUDP
	tcp := corev1.ProtocolTCP

	dnsIPs := profile.DNSClusterIPs
	if len(dnsIPs) == 0 {
		dnsIPs = []string{defaultDNSClusterIP}
	}

	apiPeers := formatCIDRPeers(apiCIDRs, true)
	if len(apiPeers) == 0 {
		apiPeers = formatCIDRPeers([]string{"10.96.0.1"}, true)
	}

	// The link-local address a workload actually connects to. Dataplane V2 (eBPF)
	// evaluates policy pre-NAT, so this peer matches there on the pre-DNAT ports;
	// Dataplane V1 DNATs first, so its token fetches are matched by rule 3 instead.
	linkLocalPeers := formatCIDRPeers([]string{metadataLinkLocalIP}, true)

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
		// Kept in step with the Service port rather than because pod-network
		// traffic reaches the dashboard — it does not, the listener is loopback
		// (see dashboardPort). Removing the rule would make the policy the reason
		// a future non-loopback bind fails, which is not the failure to leave
		// behind.
		ingressRules[0].Ports = append(ingressRules[0].Ports, networkingv1.NetworkPolicyPort{
			Protocol: &tcp,
			Port:     ptr.To(intstr.FromInt32(dashboardPort)),
		})
	}

	dnsPeers := clusterDNSPeers(dnsIPs)

	egressRules := []networkingv1.NetworkPolicyEgressRule{
		// 1. Cluster DNS
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &udp, Port: ptr.To(intstr.FromInt32(53))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(53))},
			},
			To: dnsPeers,
		},
		// 2. GCP Metadata Server (pre-NAT link-local address). Workloads dial 169.254.169.254
		//    on port 80 for HTTP metadata / OAuth2 token fetches. On Dataplane V2 (eBPF),
		//    policy evaluates pre-NAT and this rule admits token fetches directly. Port 8080,
		//    the pre-NAT ALTS handshaker port, is deliberately absent — rule 3 says why.
		{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(80))},
			},
			To: linkLocalPeers,
		},
	}

	// 3. GKE Workload Identity host-network daemon (port 988). On Dataplane V1 (iptables),
	//    the node DNATs 169.254.169.254:80 to 169.254.169.252:988 before NetworkPolicy is
	//    evaluated, so this rule admits the post-DNAT token fetch. On Dataplane V2 (eBPF),
	//    policy is evaluated pre-NAT at the socket layer and matched by rule 2. If
	//    profile.MetadataDaemonIP == "", rule 3 is suppressed.
	//
	//    Google network policy guidance recommends allowing ports 988/987 (iptables) and
	//    80/8080 (eBPF). Ports 987 and 8080 (ALTS DirectPath) are deliberately omitted here
	//    because kube-agents components use standard OAuth2/REST token fetches and no client
	//    takes the gRPC DirectPath / ALTS route. Omitting both ports enforces least-privilege
	//    sandbox egress symmetrically across Dataplane V1 and Dataplane V2.
	//
	//    That is a deviation from the guidance, which warns that workloads omitting these
	//    ports "might experience disruptions during auto-upgrades". If a token fetch starts
	//    failing during a node auto-upgrade, this narrowed allowlist is the first thing to
	//    check: capture the drop's destination port and reopen 987/8080 here if it is one
	//    of them.
	if profile.MetadataDaemonIP != "" {
		metadataDaemonPeers := formatCIDRPeers([]string{metadataLinkLocalIP, profile.MetadataDaemonIP}, true)
		port := profile.MetadataDaemonPort
		if port == 0 {
			port = metadataDaemonDefaultPort
		}
		egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(port))},
			},
			To: metadataDaemonPeers,
		})
	}

	egressRules = append(egressRules,
		// 4. LiteLLM Gateway in the agent namespace (Service port 80, container port 4000, and standalone-replay port 8080)
		networkingv1.NetworkPolicyEgressRule{
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
		networkingv1.NetworkPolicyEgressRule{
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
		networkingv1.NetworkPolicyEgressRule{
			Ports: []networkingv1.NetworkPolicyPort{
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(443))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(6443))},
				{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8443))},
			},
			To: apiPeers,
		},
	)

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

	// 8. GKE Managed OpenTelemetry Collector (Trace Export). Skipped when the agent is
	// exporting nothing: there is no endpoint to reach, so the rule would grant egress
	// for traffic that is never sent. Usually the collector's namespace does not exist
	// either, though not always — a collector Service exposing only gRPC 4317 is rejected
	// by otlpHTTPEndpointForService and also resolves to None, and there the namespace is
	// real. The rule is dropped in both cases, because neither one exports.
	if ns := otlpCollectorNamespace(otlpEndpoint); ns != "" && !otlpDisabled {
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

	// 10. Hindsight memory API in the agent namespace (Service and container port 8888).
	//     Unconditional, like rules 4, 5 and 9: the selector matches nothing on an
	//     install without --memory=hindsight. Without it every memory_retain and
	//     memory_recall from the gateway times out. A cluster that does not enforce
	//     NetworkPolicy at all connects anyway, which is why the missing rule went
	//     unnoticed: the install it breaks is the one that enforces the policy.
	egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{
			{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(8888))},
		},
		To: []networkingv1.NetworkPolicyPeer{
			{
				PodSelector: &metav1.LabelSelector{
					MatchLabels: map[string]string{
						"app.kubernetes.io/name":      "hindsight",
						"app.kubernetes.io/component": "api",
					},
				},
			},
		},
	})

	// 11. The shell sandbox's sshd. Everything the model runs executes there, so
	//     without this rule the agent has no shell at all: buildShellSandboxNetworkPolicy
	//     opens the matching ingress, and a one-sided pair still drops the packet.
	//     A live install on a Dataplane V2 cluster is what surfaced it — the ssh
	//     dial timed out while both policies read as though they permitted it.
	//     Unconditional, like rules 9 and 10: the sandbox is not optional, and a
	//     cluster that does not enforce NetworkPolicy connects either way, which is
	//     exactly the install that would hide the omission again.
	egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{
			{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(shellSandboxPort))},
		},
		To: []networkingv1.NetworkPolicyPeer{
			{
				PodSelector: &metav1.LabelSelector{
					MatchLabels: shellSandboxSelector(agent),
				},
			},
		},
	})

	// 12. The credential broker. The gateway holds no credential and calls nothing
	//     on the broker's credential surface, but the chat relay moved into that
	//     pod with it, so GOOGLE_CHAT_RELAY_URL and SLACK_RELAY_URL both address
	//     this Service now — see credentialProxyBaseURL. Without the rule the
	//     gateway's pull against /v1/chat/events is dropped and the agent stops
	//     receiving chat, while every pod stays Running and the CR reads Ready.
	//     buildCredentialProxyNetworkPolicy already admits the gateway on this
	//     port; this is the other half, and rule 11's comment is there because
	//     the same pair was written one-sided once already.
	egressRules = append(egressRules, networkingv1.NetworkPolicyEgressRule{
		Ports: []networkingv1.NetworkPolicyPort{
			{Protocol: &tcp, Port: ptr.To(intstr.FromInt32(credentialProxyPort))},
		},
		To: []networkingv1.NetworkPolicyPeer{
			{
				PodSelector: &metav1.LabelSelector{
					MatchLabels: credentialProxySelector(agent),
				},
			},
		},
	})

	// Additional Egress rules from spec. Last, so a spec-supplied rule reads as an
	// addition to the operator's own set rather than being interleaved with it.
	if len(profile.AdditionalEgress) > 0 {
		egressRules = append(egressRules, profile.AdditionalEgress...)
	}

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

// buildPluginStagingContainerName generates the container name for the plugin staging initContainer.
// GKE Autopilot / gVisor injects the annotation "dev.gvisor.internal.seccomp.<container-name>" (28 bytes)
// into pod metadata without a slash prefix. The Kubernetes annotation name length limit is 63 bytes,
// so any container name longer than 35 bytes causes admission rejection.
func buildPluginStagingContainerName(pluginName string) string {
	name := "stage-" + pluginName
	if len(name) > maxAutopilotContainerNameLen {
		hash := fmt.Sprintf("%x", sha256.Sum256([]byte(pluginName)))[:8]
		name = name[:maxAutopilotContainerNameLen-9] + "-" + hash
	}
	return name
}
