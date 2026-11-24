package main

import (
	"encoding/json"
	"flag"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

var updateSnapshot = flag.Bool("update-harness-snapshot", false, "rewrite testdata/harness-snapshot.json from the current code")

// snapshotEnvKeys are every variable the harness argv and env builders read,
// set to "" in each case unless the case names a value, so the host's own
// environment can't reach the snapshot.
var snapshotEnvKeys = []string{
	"A2A_HARNESS_CMD", "A2A_HARNESS_PATH", "A2A_MODEL", "A2A_MAX_TURNS", "A2A_ALLOWED_TOOLS",
	"A2A_HARNESS_EXTRA_ARGS", "A2A_DELEGATE_SOCKET", lib.EnvClusterView, lib.EnvProfileExecutor,
	lib.EnvDelegateTool, "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
	"CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DISABLE_AUTOUPDATER",
	"DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CONFIG_DIR", "HOME",
	"NATS_PASSWORD", "NATS_USER", "NATS_URL",
}

// snapshotCases cover each branch the builders take: the default surface,
// the cluster view with and without Bash, an allowlist override, the delegate
// tool off, a profile executor, extra args, a pinned path, model and turns,
// and each model-auth shape harnessEnv defaults around.
var snapshotCases = []struct {
	Name string
	Env  map[string]string
	// Unset takes every key the case doesn't name out of the environment
	// instead of setting it to "", the shape of a pod that never set them.
	Unset bool
}{
	{"default", nil, false},
	{"nothing set", nil, true},
	{"pinned path model turns", map[string]string{"A2A_HARNESS_PATH": "/opt/claude", "A2A_MODEL": "model-big", "A2A_MAX_TURNS": "7"}, false},
	{"cluster view", map[string]string{lib.EnvClusterView: "true"}, false},
	{"cluster view override without bash", map[string]string{lib.EnvClusterView: "true", "A2A_ALLOWED_TOOLS": "Read,Grep"}, false},
	{"cluster view override with bash pattern", map[string]string{lib.EnvClusterView: "true", "A2A_ALLOWED_TOOLS": "Read,Bash(kubectl:*)"}, false},
	{"allowlist override", map[string]string{"A2A_ALLOWED_TOOLS": "Read Write"}, false},
	{"delegate off", map[string]string{lib.EnvDelegateTool: "off"}, false},
	{"delegate socket", map[string]string{"A2A_DELEGATE_SOCKET": "/run/a2a/d.sock"}, false},
	{"profile executor", map[string]string{lib.EnvProfileExecutor: "true"}, false},
	{"profile executor with cluster view", map[string]string{lib.EnvProfileExecutor: "true", lib.EnvClusterView: "true"}, false},
	{"extra args", map[string]string{"A2A_HARNESS_EXTRA_ARGS": "--foo  bar --baz"}, false},
	{"harness cmd override", map[string]string{"A2A_HARNESS_CMD": "/bin/stub  --flag x"}, false},
	{"base url set", map[string]string{"ANTHROPIC_BASE_URL": "http://litellm:4000"}, false},
	{"api key set", map[string]string{"ANTHROPIC_API_KEY": "k"}, false},
	{"auth token set", map[string]string{"ANTHROPIC_AUTH_TOKEN": "t"}, false},
	{"vertex", map[string]string{"CLAUDE_CODE_USE_VERTEX": "1"}, false},
	{"switch preset", map[string]string{"DISABLE_TELEMETRY": "0", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0", "DISABLE_AUTOUPDATER": "0", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"}, false},
	{"bus credential", map[string]string{"NATS_PASSWORD": "p", "NATS_USER": "u", "NATS_URL": "nats://x"}, false},
	{"config dir", map[string]string{"CLAUDE_CONFIG_DIR": "/cfg"}, false},
	{"home only", map[string]string{"HOME": "/home/worker"}, false},
}

type harnessSnapshot struct {
	Name              string
	Command           []string
	CommandNoDelegate []string
	ProfileCommand    []string
	Env               []string
	UserSettings      string
}

// takeHarnessSnapshot is what the builders produce under env. The adapter's
// own path, which differs per build, reads as <self>; harnessEnv is kept to
// the variables above, with the pod's own run sorted and the builder's
// additions in the order the harness gets them.
func takeHarnessSnapshot(t *testing.T, name string, env map[string]string, unset bool) harnessSnapshot {
	t.Helper()
	for _, k := range snapshotEnvKeys {
		t.Setenv(k, env[k])
		if _, named := env[k]; unset && !named {
			os.Unsetenv(k)
		}
	}
	self := selfPath()
	clean := func(argv []string) []string {
		out := make([]string, len(argv))
		for i, a := range argv {
			out[i] = strings.ReplaceAll(a, self, "<self>")
		}
		return out
	}
	keep := map[string]bool{}
	for _, k := range snapshotEnvKeys {
		keep[k] = true
	}
	var kept []string
	for _, kv := range harnessEnv() {
		if k, _, ok := strings.Cut(kv, "="); ok && keep[k] {
			kept = append(kept, kv)
		}
	}
	// The harness env is the pod's own variables, in the order the host's
	// environment happens to hold them, then the builder's additions. Only
	// the second order is behaviour: a key the pod set and a default after
	// it resolve last-wins. So the pod's leading run is sorted and the
	// builder's tail is kept as built.
	inherited := map[string]bool{}
	for _, kv := range os.Environ() {
		inherited[kv] = true
	}
	lead := 0
	for lead < len(kept) && inherited[kept[lead]] {
		lead++
	}
	sort.Strings(kept[:lead])
	return harnessSnapshot{
		Name:              name,
		Command:           clean(harnessCommand()),
		CommandNoDelegate: clean(harnessCommandWithoutDelegate()),
		ProfileCommand:    clean(profileHarnessCommand()),
		Env:               kept,
		UserSettings:      harnessUserSettings(),
	}
}

// TestTheHarnessBuildersMatchTheSnapshot: the argv and env the worker hands
// the harness, across the cases above, are byte for byte the ones recorded
// from the code before the harness selector (testdata/harness-snapshot.json).
// Regenerate only for an intended change, with -update-harness-snapshot.
func TestTheHarnessBuildersMatchTheSnapshot(t *testing.T) {
	var got []harnessSnapshot
	for _, tc := range snapshotCases {
		t.Run(tc.Name, func(t *testing.T) {
			got = append(got, takeHarnessSnapshot(t, tc.Name, tc.Env, tc.Unset))
		})
	}
	path := filepath.Join("testdata", "harness-snapshot.json")
	if *updateSnapshot {
		b, err := json.MarshalIndent(got, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.MkdirAll("testdata", 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var want []harnessSnapshot
	if err := json.Unmarshal(b, &want); err != nil {
		t.Fatal(err)
	}
	if len(got) != len(want) {
		t.Fatalf("%d cases, snapshot has %d", len(got), len(want))
	}
	for i := range want {
		if !reflect.DeepEqual(got[i], want[i]) {
			t.Errorf("case %q differs from the snapshot\n got %+v\nwant %+v", want[i].Name, got[i], want[i])
		}
	}
}
