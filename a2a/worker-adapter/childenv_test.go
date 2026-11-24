package workeradapter

import (
	"os/exec"
	"slices"
	"strings"
	"testing"
)

// TestToolChildrenGetOnlyTheAllowlist: every name off the allowlist is
// withheld, whatever it is (model, provider, bus, chat credentials, and a name
// nobody has thought of), and every allowlisted name passes.
func TestToolChildrenGetOnlyTheAllowlist(t *testing.T) {
	never := []string{
		"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
		"GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GEMINI_BASE_URL",
		"GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "GROQ_API_KEY",
		"AICORE_CLIENT_SECRET", "AI_API_URL", "GW_CLIENT_SECRET",
		"NATS_URL", "NATS_PASSWORD", "A2A_BUS_TOKEN_FILE",
		"LITELLM_MASTER_KEY", "AGY_LLM_GATEWAY_URL",
		"HF_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "SLACK_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY",
		"DECISION_MEMORY_LLM_API_KEY", "SOME_FUTURE_SECRET",
	}
	keep := []string{
		"PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "KUBECONFIG",
		"CREDENTIAL_PROXY_URL", "CREDENTIAL_PROXY_TOKEN_FILE", "CREDENTIAL_PROXY_TASK_TOKEN_FILE",
	}
	var env []string
	for _, n := range append(slices.Clone(never), keep...) {
		env = append(env, n+"=v-"+n)
	}
	got := toolChildEnv(env)
	for _, n := range never {
		if slices.ContainsFunc(got, func(kv string) bool { return strings.HasPrefix(kv, n+"=") }) {
			t.Errorf("%s reaches a tool child", n)
		}
	}
	for _, n := range keep {
		if !slices.Contains(got, n+"=v-"+n) {
			t.Errorf("%s was withheld from a tool child, which needs it", n)
		}
	}
	names := WithheldFromToolChildren(env)
	slices.Sort(names)
	want := slices.Clone(never)
	slices.Sort(want)
	if !slices.Equal(names, want) {
		t.Errorf("withheld names\n got %q\nwant %q", names, want)
	}
}

// TestAToolChildBuiltFromTheFilteredEnvCannotPrintACredential runs a real
// child with the filtered environment, as a tool would, and has it print its
// environment.
func TestAToolChildBuiltFromTheFilteredEnvCannotPrintACredential(t *testing.T) {
	cmd := exec.Command("/usr/bin/env")
	cmd.Env = toolChildEnv([]string{"PATH=/usr/bin:/bin", "ANTHROPIC_API_KEY=sk-secret-value", "GOOGLE_GEMINI_BASE_URL=http://gw", "NATS_PASSWORD=bus-secret", "HF_TOKEN=hf-secret", "HOME=/h"})
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{"sk-secret-value", "http://gw", "bus-secret", "hf-secret"} {
		if strings.Contains(string(out), s) {
			t.Errorf("a tool child printed %q", s)
		}
	}
	if !strings.Contains(string(out), "HOME=/h") {
		t.Error("an allowlisted variable did not reach the child")
	}
}

// toolChildAllowedCredentialish is the only credential-like entry the
// allowlist may hold, with its reason.
var toolChildAllowedCredentialish = map[string]string{
	"CREDENTIAL_PROXY_": "the broker-routed wrappers need the broker URL, the pod's broker token and the task token; the broker scopes both",
}

// TestTheAllowlistHoldsNoCredentialUnnoticed: no allowlisted name or prefix
// looks like a credential (a KEY, TOKEN, SECRET, PASSWORD, CRED or AUTH
// part), except those listed with a reason, so widening the allowlist to a
// credential takes a deliberate entry here.
func TestTheAllowlistHoldsNoCredentialUnnoticed(t *testing.T) {
	words := map[string]bool{"KEY": true, "KEYS": true, "TOKEN": true, "SECRET": true, "PASSWORD": true,
		"CRED": true, "CREDS": true, "CREDENTIAL": true, "CREDENTIALS": true, "AUTH": true}
	for _, n := range append(slices.Clone(toolChildAllowedNames), toolChildAllowedPrefixes...) {
		for _, w := range strings.Split(strings.TrimSuffix(n, "_"), "_") {
			if words[w] {
				if _, ok := toolChildAllowedCredentialish[n]; !ok {
					t.Errorf("allowlisted %q looks like a credential; list it in toolChildAllowedCredentialish with the reason, or drop it", n)
				}
			}
		}
	}
}
