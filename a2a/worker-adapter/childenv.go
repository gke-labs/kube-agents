package workeradapter

import "strings"

// A harness starts the processes a turn's tools run, such as a shell command
// or an MCP stdio server, as the adapter's user. Those children get an
// allowlisted environment: the basics a command needs, KUBECONFIG, and what
// the image's tool wrappers read (the broker-routed tools, the session
// kube-proxy path, kubectl admission, the shell memory guard, the VCS tools). Everything else is withheld, so a model,
// provider, bus or chat credential the pod carries never reaches them,
// whatever its name. Withheld names what must not reach them;
// WithheldFromToolChildren lists those present in an environment, for a
// harness's own withhold list; toolChildEnv keeps only what may.
//
// CREDENTIAL_PROXY_* is allowed on purpose: the broker-routed kubectl and
// gcloud wrappers need the broker's URL, the pod's own projected broker token
// (CREDENTIAL_PROXY_TOKEN_FILE) and the task's token
// (CREDENTIAL_PROXY_TASK_TOKEN_FILE), and the broker scopes what either can
// do. Tool children see the same today.
//
// Withholding a *_FILE variable hides only the path. The file stays readable
// to the adapter's user wherever it is mounted, so a secret kept in a file
// needs file modes or another user to be out of a tool child's reach.

// toolChildAllowedPrefixes and toolChildAllowedNames are the only variables
// a turn's tool children may see.
var (
	toolChildAllowedPrefixes = []string{
		"LC_",               // locale
		"CREDENTIAL_PROXY_", // the broker-routed tools (see above)
	}
	// Each name past KUBECONFIG is one a tool wrapper in the image reads,
	// listed by name rather than by prefix, so a variable added later under
	// the same prefix is withheld until someone reviews it
	// (TestToolChildrenGetWhatTheToolWrappersRead ties the list to the
	// wrappers' source).
	toolChildAllowedNames = []string{
		"PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "HOSTNAME",
		"LANG", "LANGUAGE", "TERM", "TZ",
		"KUBECONFIG",
		// a session pod's kubectl through its kube-proxy (session-kubectl.sh,
		// kubectl-context-registered, credential_proxy_client.py)
		"KUBE_PROXY_URL", "KUBE_PROXY_CA_FILE", "KUBE_PROXY_HOST_CONTEXT", "KUBE_PROXY_MODE",
		// kubectl admission (kubectl-admit)
		"KUBEAGENTS_KUBECTL_COST_TIER", "KUBECTL_ADMIT_ON_BUSY", "KUBECTL_ADMIT_GUARD_PATH", "KUBECTL_ADMIT_RUN_DIR",
		// the shell memory guard
		"SANDBOX_MEMORY_LIMIT_BYTES",
		// the VCS tools (vcs_client.py)
		"KUBE_AGENTS_LOCAL_GIT", "KUBE_AGENTS_VCS_AUTHOR_NAME", "KUBE_AGENTS_VCS_ROOT", "KUBE_AGENTS_VCS_TTL_HOURS",
		// read by credential_proxy_client.py
		"HERMES_HOME",
	}
)

// Withheld reports whether a turn's tool children must not see the variable
// name: true for every name not allowlisted.
func Withheld(name string) bool {
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

// WithheldFromToolChildren is the names in env (KEY=value entries) that a
// turn's tool children must not see, for a harness's withhold list.
func WithheldFromToolChildren(env []string) []string {
	var out []string
	for _, kv := range env {
		name, _, _ := strings.Cut(kv, "=")
		if Withheld(name) {
			out = append(out, name)
		}
	}
	return out
}

// toolChildEnv is env with only the variables a turn's tool children may see.
func toolChildEnv(env []string) []string {
	out := make([]string, 0, len(env))
	for _, kv := range env {
		name, _, _ := strings.Cut(kv, "=")
		if !Withheld(name) {
			out = append(out, kv)
		}
	}
	return out
}
