package workeradapter

import "strings"

// A harness starts the processes a turn's tools run, such as a shell command
// or an MCP stdio server, as the adapter's user. Those children get an
// allowlisted environment: the basics a command needs, KUBECONFIG, and the
// broker-routed tools' variables. Everything else is withheld, so a model,
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
	toolChildAllowedNames = []string{
		"PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "HOSTNAME",
		"LANG", "LANGUAGE", "TERM", "TZ",
		"KUBECONFIG",
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
