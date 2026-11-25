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

// toolWrapperVars is every variable the image's tool wrappers read, by the
// file that reads it: a tool child must see each one, or the wrapper takes
// another path (a session pod's kubectl falls back to the broker, which
// refuses the session caller).
var toolWrapperVars = map[string][]string{
	"deploy/sandbox/session-kubectl.sh":                        {"CREDENTIAL_PROXY_EXEC_URL", "CREDENTIAL_PROXY_REFUSAL_FILE", "CREDENTIAL_PROXY_URL", "HOME", "KUBE_PROXY_MODE", "KUBE_PROXY_URL", "KUBEAGENTS_KUBECTL_COST_TIER", "SANDBOX_MEMORY_LIMIT_BYTES"},
	"deploy/sandbox/kubectl_context_registered.sh":             {"CREDENTIAL_PROXY_TOKEN_FILE", "HOME", "KUBE_PROXY_CA_FILE", "KUBE_PROXY_URL", "KUBECONFIG"},
	"agents/platform/scripts/credential_proxy_client.py":       {"CREDENTIAL_PROXY_TOKEN_FILE", "CREDENTIAL_PROXY_URL", "HERMES_HOME", "KUBE_PROXY_CA_FILE", "KUBE_PROXY_HOST_CONTEXT", "KUBE_PROXY_URL", "KUBECONFIG"},
	"deploy/sandbox/kubectl-admit.py":                          {"KUBEAGENTS_KUBECTL_COST_TIER", "KUBECTL_ADMIT_ON_BUSY", "KUBECTL_ADMIT_GUARD_PATH", "KUBECTL_ADMIT_RUN_DIR"},
	"deploy/sandbox/shell-memory-guard.py":                     {"SANDBOX_MEMORY_LIMIT_BYTES"},
	"agents/platform/scripts/vcs_client.py":                    {"KUBE_AGENTS_LOCAL_GIT", "KUBE_AGENTS_VCS_AUTHOR_NAME", "KUBE_AGENTS_VCS_ROOT", "KUBE_AGENTS_VCS_TTL_HOURS"},
	"a2a/worker-adapter/refusal.go (the write-refusal record)": {"CREDENTIAL_PROXY_REFUSAL_FILE", "CREDENTIAL_PROXY_REFUSAL_TASK"},
}

// TestToolChildrenGetWhatTheToolWrappersRead: every variable the image's
// tool wrappers read reaches a tool child, and each wrapper file still
// mentions the variables listed for it, so a rename there fails here. And
// every allowlisted name past the basics is one a wrapper reads, so the
// allowlist doesn't grow past the reviewed set unnoticed.
func TestToolChildrenGetWhatTheToolWrappersRead(t *testing.T) {
	for file, vars := range toolWrapperVars {
		path, _, _ := strings.Cut(file, " ")
		src, err := os.ReadFile(filepath.Join("..", "..", path))
		if err != nil {
			t.Errorf("%s: %v", path, err)
			continue
		}
		for _, v := range vars {
			if !strings.Contains(string(src), v) {
				t.Errorf("%s no longer mentions %s: update toolWrapperVars", path, v)
			}
			if Withheld(v) {
				t.Errorf("%s (read by %s) is withheld from tool children", v, path)
			}
		}
	}
	read := map[string]bool{}
	for _, vars := range toolWrapperVars {
		for _, v := range vars {
			read[v] = true
		}
	}
	for _, path := range toolFiles {
		src, err := os.ReadFile(filepath.Join("..", "..", path))
		if err != nil {
			t.Fatal(err)
		}
		for _, n := range envNamesRead(path, src) {
			read[n] = true
		}
	}
	basics := []string{"PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "TMPDIR", "HOSTNAME", "LANG", "LANGUAGE", "TERM", "TZ", "KUBECONFIG"}
	for _, n := range toolChildAllowedNames {
		if !read[n] && !slices.Contains(basics, n) {
			t.Errorf("allowlisted %s is not read by any tool file", n)
		}
	}
	if want := []string{"LC_", "CREDENTIAL_PROXY_"}; !slices.Equal(toolChildAllowedPrefixes, want) {
		t.Errorf("allowlisted prefixes %q; widening past %q needs a review here", toolChildAllowedPrefixes, want)
	}
}

// toolFiles is every file the worker image installs for a turn's tools to
// run (a2a/Dockerfile.worker's COPY lines into /usr/local/bin,
// /usr/local/libexec, /opt/credential-proxy and the skills' scripts).
var toolFiles = []string{
	"deploy/sandbox/session-kubectl.sh",
	"deploy/sandbox/kubectl_context_registered.sh",
	"deploy/sandbox/kubectl-admit.py",
	"deploy/sandbox/kubectl_session_fastpath.py",
	"deploy/sandbox/shell-memory-guard.py",
	"deploy/shared/credential-proxy-exec.py",
	"agents/platform/scripts/credential_proxy_client.py",
	"agents/platform/scripts/command_policy.py",
	"agents/platform/scripts/vcs_client.py",
	"agents/platform/scripts/vcs_readings.py",
	"agents/platform/skills/fleet-audit/scripts/audit_report.py",
	"agents/platform/skills/fleet-upgrade-verification/scripts/upgrade_readiness.py",
}

// toolChildWithheldOnPurpose is a variable a tool file reads that tool
// children still don't get, with the reason. Empty: every name the tool
// files read today is configuration, not a credential, and is allowed. A
// credential a tool file reads belongs here, with why the tool can do
// without it (the broker holds credentials; tool children get paths and
// settings).
var toolChildWithheldOnPurpose = map[string]string{}

var (
	pyEnvRead    = regexp.MustCompile(`(?:environ\.get|environ\[|getenv)\(?\s*["']([A-Z][A-Z0-9_]+)["']`)
	pyEnvConst   = regexp.MustCompile(`_ENVS?\s*=\s*\(?((?:\s*["'][A-Z][A-Z0-9_]+["']\s*,?)+)`)
	quotedName   = regexp.MustCompile(`["']([A-Z][A-Z0-9_]+)["']`)
	shEnvRead    = regexp.MustCompile(`\$\{?([A-Z][A-Z0-9_]+)`)
	shAssignment = regexp.MustCompile(`(?m)^\s*(?:local\s+|readonly\s+|export\s+)?([A-Z][A-Z0-9_]+)=`)
)

// envNamesRead is every environment variable name a tool file reads, as
// far as a scan can tell: Python's environ/getenv calls and *_ENV(S) string
// constants, and a shell script's $NAME uses minus the names it assigns
// itself.
func envNamesRead(path string, src []byte) []string {
	names := map[string]bool{}
	if strings.HasSuffix(path, ".py") {
		for _, m := range pyEnvRead.FindAllSubmatch(src, -1) {
			names[string(m[1])] = true
		}
		for _, m := range pyEnvConst.FindAllSubmatch(src, -1) {
			for _, q := range quotedName.FindAllSubmatch(m[1], -1) {
				names[string(q[1])] = true
			}
		}
	} else {
		local := map[string]bool{}
		for _, m := range shAssignment.FindAllSubmatch(src, -1) {
			local[string(m[1])] = true
		}
		for _, m := range shEnvRead.FindAllSubmatch(src, -1) {
			if n := string(m[1]); !local[n] {
				names[n] = true
			}
		}
	}
	out := make([]string, 0, len(names))
	for n := range names {
		out = append(out, n)
	}
	slices.Sort(out)
	return out
}

// TestEveryVariableAToolFileReadsReachesToolChildren scans every tool file
// the image installs for the environment variables it reads: each must
// reach a tool child, or be withheld on purpose with a reason.
func TestEveryVariableAToolFileReadsReachesToolChildren(t *testing.T) {
	for _, path := range toolFiles {
		src, err := os.ReadFile(filepath.Join("..", "..", path))
		if err != nil {
			t.Errorf("%s: %v", path, err)
			continue
		}
		for _, n := range envNamesRead(path, src) {
			if _, ok := toolChildWithheldOnPurpose[n]; ok {
				continue
			}
			if Withheld(n) {
				t.Errorf("%s reads %s, which tool children don't get: allow it, or list it in toolChildWithheldOnPurpose with the reason", path, n)
			}
		}
	}
}

// TestTheScanFindsWhatTheWrappersAreKnownToRead checks the scanner against
// the hand-kept list: every variable toolWrapperVars names for a scanned file
// is one the scan finds there, so a scan that went blind would fail here.
func TestTheScanFindsWhatTheWrappersAreKnownToRead(t *testing.T) {
	for file, vars := range toolWrapperVars {
		path, _, _ := strings.Cut(file, " ")
		if !slices.Contains(toolFiles, path) {
			continue
		}
		src, err := os.ReadFile(filepath.Join("..", "..", path))
		if err != nil {
			t.Fatal(err)
		}
		found := envNamesRead(path, src)
		for _, v := range vars {
			if !slices.Contains(found, v) {
				t.Errorf("the scan of %s misses %s, which it reads", path, v)
			}
		}
	}
}
