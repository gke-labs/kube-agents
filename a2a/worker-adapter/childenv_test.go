package workeradapter

import (
	"os/exec"
	"slices"
	"strings"
	"testing"
)

// TestToolChildrenNeverSeeModelOrBusCredentials names each credential a
// turn's tool children must not see, and the broker-routed tools' variables
// they must keep.
func TestToolChildrenNeverSeeModelOrBusCredentials(t *testing.T) {
	never := []string{
		"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
		"GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_CLOUD_PROJECT",
		"GEMINI_API_KEY", "OPENAI_API_KEY",
		"NATS_URL", "NATS_USER", "NATS_PASSWORD", "A2A_BUS_TOKEN_FILE",
		"LITELLM_MASTER_KEY", "AGY_LLM_GATEWAY_URL", "A2A_LIVE_GATEWAY_PASSWORD",
	}
	keep := []string{
		"PATH", "HOME", "LANG",
		"CREDENTIAL_PROXY_URL", "CREDENTIAL_PROXY_TASK_TOKEN_FILE",
		"KUBECONFIG", "A2A_TASK_ID",
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
	names := withheldFromToolChildren(env)
	slices.Sort(names)
	want := slices.Clone(never)
	slices.Sort(want)
	if !slices.Equal(names, want) {
		t.Errorf("withheld names\n got %q\nwant %q", names, want)
	}
}

// TestAToolChildBuiltFromTheFilteredEnvCannotPrintACredential runs a real
// shell child with the filtered environment, as a tool would, and has it
// print its environment.
func TestAToolChildBuiltFromTheFilteredEnvCannotPrintACredential(t *testing.T) {
	cmd := exec.Command("/usr/bin/env")
	cmd.Env = toolChildEnv([]string{"PATH=/usr/bin:/bin", "ANTHROPIC_API_KEY=sk-secret-value", "GOOGLE_GEMINI_BASE_URL=http://gw", "NATS_PASSWORD=bus-secret", "KEEP=yes"})
	out, err := cmd.Output()
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range []string{"sk-secret-value", "http://gw", "bus-secret"} {
		if strings.Contains(string(out), s) {
			t.Errorf("a tool child printed %q", s)
		}
	}
	if !strings.Contains(string(out), "KEEP=yes") {
		t.Error("a kept variable did not reach the child")
	}
}
