package workeradapter

import "strings"

// A harness that runs in the adapter's process (rather than as its own
// subprocess) starts the processes a turn's tools run, such as a shell
// command or an MCP stdio server, from the adapter's own environment. Those
// children need the broker-routed tools' variables (CREDENTIAL_PROXY_*,
// which carry only the task's own token) but never the adapter's model,
// provider or bus credentials. WithheldFromToolChildren names what must not
// reach them; a harness passes the result to its own withhold list, and
// toolChildEnv strips the same from an environment it builds. Withheld is
// matched by prefix (withheldPrefixes) or exact name (withheldNames).

// withheldPrefixes are variable name prefixes whose every variable is a
// model, provider or bus credential or endpoint.
var withheldPrefixes = []string{
	"ANTHROPIC_", // the model gateway: key, auth token, base URL
	"GOOGLE_",    // Google credentials and endpoints, GOOGLE_GEMINI_BASE_URL among them
	"GEMINI_",    // GEMINI_API_KEY
	"OPENAI_",    // OPENAI_API_KEY, OPENAI_BASE_URL
	"NATS_",      // the bus: URL, user, password
	"A2A_BUS_",   // the bus token file
	"AGY_LLM_",   // a gateway URL for a model client
	"XAI_",       // XAI_API_KEY
	"GROQ_",      // GROQ_API_KEY
	"AICORE_",    // SAP AI Core: client id and secret, service key, URLs
	"GW_",        // a model gateway's OAuth client: id, secret, token URL
}

// withheldNames are single variables withheld besides the prefixes.
var withheldNames = []string{
	"AI_API_URL", // SAP AI Core's API endpoint
	"LITELLM_MASTER_KEY",
	"LITELLM_API_KEY",
	"A2A_LIVE_GATEWAY_PASSWORD",
}

// Withheld reports whether a turn's tool children must not see the
// variable name.
func Withheld(name string) bool {
	for _, p := range withheldPrefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	for _, n := range withheldNames {
		if name == n {
			return true
		}
	}
	return false
}

// WithheldFromToolChildren is the names in env (KEY=value entries) that a
// turn's tool children must not see, for an in-process harness's withhold
// list.
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

// toolChildEnv is env without the variables a turn's tool children must not
// see.
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
