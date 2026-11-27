package workeradapter

import (
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A harness starts the processes a turn's tools run, such as a shell command
// or an MCP stdio server, as the adapter's user. Those children get an
// allowlisted environment, so no credential the pod carries reaches them:
// the basics a command needs, the task's parameters and trigger
// (PROFILE_TASK_*, PROFILE_TRIGGER_*), the broker-routed tools' variables
// (CREDENTIAL_PROXY_*), and, by exact name, every variable a tool file or a
// shipped persona reads. Everything else is withheld. Two deny layers
// override the allowlist: a variable the operator sources from a Secret
// (named in EnvToolWithheld) and a name with a credential word are withheld
// even when allowed, CREDENTIAL_PROXY_* aside.
//
// CREDENTIAL_PROXY_* passes on purpose, credential words and all: the
// broker-routed wrappers need the broker's URL, the pod's own projected
// broker token (CREDENTIAL_PROXY_TOKEN_FILE) and the task's token
// (CREDENTIAL_PROXY_TASK_TOKEN_FILE), and the broker scopes what either can
// do.
//
// Withholding a *_FILE variable hides only the path. The file stays readable
// to the adapter's user wherever it is mounted, so a secret kept in a file
// needs file modes or another user to be out of a tool child's reach.

// EnvToolWithheld (lib.EnvToolWithheld) is the comma-separated list of the
// container's variables sourced from a Secret (an env entry's secretKeyRef).
// Worker containers take no envFrom Secret, whose keys aren't known when the
// pod is rendered. The list names variables, not values, and is itself off the
// allowlist, so tool children don't see it.
const EnvToolWithheld = lib.EnvToolWithheld

// toolChildAlwaysPassedPrefix passes despite its credential words (see above).
const toolChildAlwaysPassedPrefix = "CREDENTIAL_PROXY_" // #nosec G101 -- an env var name prefix, not a credential

// credentialWords are the name parts (split on "_") that mark a credential.
var credentialWords = map[string]bool{
	"KEY": true, "KEYS": true, "APIKEY": true,
	"TOKEN": true, "TOKENS": true,
	"SECRET": true, "SECRETS": true,
	"PASSWORD": true, "PASSWD": true, "PASSPHRASE": true,
	"AUTH": true, "CRED": true, "CREDS": true, "CREDENTIAL": true, "CREDENTIALS": true,
	"PRIVATE": true, "COOKIE": true,
}

// credentialSuffixes mark a credential when a name part ends with one,
// however the words run together (ACCESSTOKEN, APIKEY, DBPASSWORD).
var credentialSuffixes = []string{"TOKEN", "TOKENS", "SECRET", "SECRETS", "PASSWORD", "PASSWD", "KEY", "KEYS", "CREDENTIALS"}

// nameParts is name upper-cased and split on anything not a letter or a
// digit and at each lower-to-upper case change, so "api_key", "apiKey" and
// "API-KEY" all give API and KEY.
func nameParts(name string) []string {
	var parts []string
	var cur strings.Builder
	var prevLower bool
	flush := func() {
		if cur.Len() > 0 {
			parts = append(parts, strings.ToUpper(cur.String()))
			cur.Reset()
		}
	}
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z':
			cur.WriteRune(r)
			prevLower = true
		case r >= 'A' && r <= 'Z':
			if prevLower {
				flush()
			}
			cur.WriteRune(r)
			prevLower = false
		case r >= '0' && r <= '9':
			cur.WriteRune(r)
		default:
			flush()
			prevLower = false
		}
	}
	flush()
	return parts
}

// toolChildAllowedPrefixes and toolChildAllowedNames are the only variables
// a turn's tool children may see, deny layers aside. Each name past
// KUBECONFIG is one a tool file or a shipped persona reads, listed by name
// rather than by prefix, so a variable added later under the same prefix is
// withheld until someone reviews it; the completeness scan
// (TestEveryVariableAToolOrPersonaReadsReachesToolChildren) finds them.
var (
	toolChildAllowedPrefixes = []string{
		"LC_",               // locale
		"CREDENTIAL_PROXY_", // the broker-routed tools (see above)
		"PROFILE_TASK_",     // the task's declared parameters (profile.go's taskParameterEnv)
		"PROFILE_TRIGGER_",  // the submission's trigger (profile.go's taskTriggerEnv)
	}
	toolChildAllowedNames = []string{
		"PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "HOSTNAME",
		"LANG", "LANGUAGE", "TERM", "TZ",
		"KUBECONFIG",
		// a session pod's kubectl through its kube-proxy (session-kubectl.sh,
		// kubectl-context-registered, credential_proxy_client.py). A profile
		// worker with a kube-proxy gets the adapter's loopback as the URL, the
		// task's target as the host context and no CA file (kubeproxy.go).
		"KUBE_PROXY_URL", "KUBE_PROXY_CA_FILE", "KUBE_PROXY_HOST_CONTEXT", "KUBE_PROXY_MODE",
		// kubectl admission (kubectl-admit)
		"KUBEAGENTS_KUBECTL_COST_TIER", "KUBECTL_ADMIT_ON_BUSY", "KUBECTL_ADMIT_GUARD_PATH", "KUBECTL_ADMIT_RUN_DIR",
		// the shell memory guard
		"SANDBOX_MEMORY_LIMIT_BYTES",
		// the VCS tools (vcs_client.py)
		"KUBE_AGENTS_LOCAL_GIT", "KUBE_AGENTS_VCS_AUTHOR_NAME", "KUBE_AGENTS_VCS_ROOT", "KUBE_AGENTS_VCS_TTL_HOURS",
		// read by credential_proxy_client.py: the Google universe domain the
		// broker-routed gcloud and API calls use, and the session ids it
		// attributes a call to
		"GOOGLE_CLOUD_UNIVERSE_DOMAIN", "CLOUDSDK_CORE_UNIVERSE_DOMAIN",
		"HERMES_HOME", "HERMES_CRON_RUN", "HERMES_KANBAN_TASK", "HERMES_SESSION_ID",
		// read by vcs_readings.py: the commit author and the forge app id
		"KUBE_AGENTS_VCS_AUTHOR_EMAIL", "GITHUB_APP_ID",
		// read by the fleet-audit skill's audit_report.py
		"FLEET_AUDIT_GITOPS_ROOT", "FLEET_AUDIT_LEDGER_SCOPE", "FLEET_AUDIT_REPORTS_DIR", "FLEET_AUDIT_SCRATCH_DIR",
		"GKE_PROJECT_ID", "GKE_CLUSTER_NAME", "GKE_LOCATION", "KUBEAGENTS_AUDIT_AUTO_PROMOTE",
		// read by the fleet-audit skill's report_status.py and the fleet
		// scope arguments (fleet_scope_args.py)
		"FLEET_AUDIT_REPORT_STORE", "KUBEAGENTS_SCOPE_DECLARED",
		// read by credential_proxy_client.kubeconfig_dir and the fleet-audit
		// collectors: the per-cluster kubeconfig directory, on a profile
		// worker's scratch mount
		"KUBE_AGENTS_KUBECONFIG_DIR",
		// read by gitops_workspace.py: the commit author, the forge org, the
		// lease and state store, and the kube context
		"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GITHUB_ORG", "GITOPS_ORG",
		"GITOPS_LEASE_TTL_HOURS", "GITOPS_STATE_CONFIGMAP", "GITOPS_STATE_PATH",
		"KUBE_CONTEXT_NAME", "KUBE_DEFAULT_NAMESPACE", "PLATFORM_AGENT_HOME",
		// read by the cluster persona's workload-triage-scan script
		"WORKLOAD_TRIAGE_BUDGET_SECONDS",
	}
)

// Withheld reports whether a turn's tool children must not see the variable
// name on its own merits: it is off the allowlist, or has a credential word.
// The Secret-sourced names come from the environment itself
// (WithheldFromToolChildren, toolChildEnv).
func Withheld(name string) bool {
	if credentialish(name) {
		return true
	}
	for _, p := range toolChildAllowedPrefixes {
		if strings.HasPrefix(name, p) {
			return false
		}
	}
	for _, n := range toolChildAllowedNames {
		if name == n {
			return false
		}
	}
	return true
}

// credentialish reports whether name has a credential word in it, outside
// CREDENTIAL_PROXY_*: a deny layer that overrides the allowlist.
func credentialish(name string) bool {
	if strings.HasPrefix(name, toolChildAlwaysPassedPrefix) {
		return false
	}
	for _, part := range nameParts(name) {
		if credentialWords[part] {
			return true
		}
		for _, w := range credentialSuffixes {
			if strings.HasSuffix(part, w) {
				return true
			}
		}
	}
	return false
}

// secretSourced is the set of names env's EnvToolWithheld lists.
func secretSourced(env []string) map[string]bool {
	out := map[string]bool{}
	for _, kv := range env {
		name, value, _ := strings.Cut(kv, "=")
		if name != EnvToolWithheld {
			continue
		}
		for _, n := range strings.Split(value, ",") {
			if n = strings.TrimSpace(n); n != "" {
				out[n] = true
			}
		}
	}
	return out
}

func withheldIn(name string, secrets map[string]bool) bool {
	return secrets[name] || Withheld(name)
}

// WithheldFromToolChildren is the names in env (KEY=value entries) that a
// turn's tool children must not see, for a harness's withhold list.
func WithheldFromToolChildren(env []string) []string {
	secrets := secretSourced(env)
	var out []string
	for _, kv := range env {
		name, _, _ := strings.Cut(kv, "=")
		if withheldIn(name, secrets) {
			out = append(out, name)
		}
	}
	return out
}

// toolChildEnv is env without the variables a turn's tool children must not
// see.
func toolChildEnv(env []string) []string {
	secrets := secretSourced(env)
	out := make([]string, 0, len(env))
	for _, kv := range env {
		name, _, _ := strings.Cut(kv, "=")
		if !withheldIn(name, secrets) {
			out = append(out, kv)
		}
	}
	return out
}
