package workeradapter

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
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
	names := WithheldFromToolChildren(env)
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

// toolChildAllowedCredentialish are the only credential-like variable names
// the adapter reads that tool children may still see, each with its reason.
var toolChildAllowedCredentialish = map[string]string{
	"CREDENTIAL_PROXY_":            "deliberate: the broker-routed kubectl/gcloud wrappers need the broker URL and the task's own token, which is per-task and broker-scoped; Claude's tool children see the same today",
	"A2A_TASK_SIGNING_PUBLIC_KEYS": "public verification keys, not a secret",
}

// TestNoCredentialishNameReachesToolChildrenUnnoticed reads every
// credential-like environment variable name the adapter's own (non-test)
// source mentions, and requires each to be withheld from tool children or
// listed, with a reason, in toolChildAllowedCredentialish. A new credential
// added to the adapter fails here until someone decides which it is.
func TestNoCredentialishNameReachesToolChildrenUnnoticed(t *testing.T) {
	envName := regexp.MustCompile(`"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)"`)
	// A credential word is a whole part of the name between underscores,
	// so AUTH matches A2A_AUTH_TOKEN but not A2A_AUTHORITY_SCOPE.
	credentialWords := map[string]bool{"KEY": true, "KEYS": true, "TOKEN": true, "SECRET": true, "PASSWORD": true,
		"CRED": true, "CREDS": true, "CREDENTIAL": true, "CREDENTIALS": true, "AUTH": true}
	isCredentialish := func(name string) bool {
		for _, w := range strings.Split(name, "_") {
			if credentialWords[w] {
				return true
			}
		}
		return false
	}
	var files []string
	for _, g := range []string{"*.go", "../lib/*.go", "../cmd/worker-adapter/*.go"} {
		m, err := filepath.Glob(g)
		if err != nil {
			t.Fatal(err)
		}
		files = append(files, m...)
	}
	if len(files) == 0 {
		t.Skip("the adapter's source is not beside this test binary (run outside the module)")
	}
	seen := 0
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		b, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		for _, m := range envName.FindAllStringSubmatch(string(b), -1) {
			name := m[1]
			if !isCredentialish(name) {
				continue
			}
			seen++
			if Withheld(name) {
				continue
			}
			allowed := false
			for prefix := range toolChildAllowedCredentialish {
				if strings.HasPrefix(name, prefix) {
					allowed = true
				}
			}
			if !allowed {
				t.Errorf("%s (in %s) is credential-like and reaches tool children: withhold it, or list it in toolChildAllowedCredentialish with the reason", name, f)
			}
		}
	}
	if seen == 0 {
		t.Fatal("no credential-like names found; the scan reads nothing")
	}
}
