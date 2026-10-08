package main

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// The bus credential must not reach the harness. The worker NATS user is
// shared across every session pod and its grants cover the whole task plane,
// while the harness is a model-directed subprocess with a file-reading tool
// and /proc/self/environ readable at its own UID. Assert the refusal — that
// the values are absent — rather than that the filter exists.
func TestHarnessEnvWithholdsTheBusCredential(t *testing.T) {
	t.Setenv("NATS_PASSWORD", "s3cret-worker-password")
	t.Setenv("NATS_USER", "worker")
	t.Setenv("NATS_URL", "nats://platform-agent-a2a-nats.kubeagents-system.svc:4222")
	t.Setenv("TASK_ID", "task-abc")

	env := harnessEnv()

	for _, kv := range env {
		key, value, _ := strings.Cut(kv, "=")
		for _, withheld := range busCredentialEnv {
			if key == withheld {
				t.Errorf("%s reached the harness environment", key)
			}
		}
		if strings.Contains(value, "s3cret-worker-password") {
			t.Errorf("the bus password reached the harness as %s", key)
		}
	}

	// The filter is not a blanket drop: everything else the pod was given
	// still has to arrive, or the harness loses its task identity.
	var sawTask bool
	for _, kv := range env {
		if kv == "TASK_ID=task-abc" {
			sawTask = true
		}
	}
	if !sawTask {
		t.Error("TASK_ID did not survive the filter")
	}
}

// With no model auth configured the harness is pointed at the install's
// LiteLLM, which is the one destination the session fence permits besides the
// bus and DNS.
func TestHarnessEnvDefaultsToTheInstallLiteLLM(t *testing.T) {
	for _, key := range []string{"ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_API_KEY"} {
		t.Setenv(key, "")
		_ = os.Unsetenv(key)
	}

	var base string
	for _, kv := range harnessEnv() {
		if key, value, _ := strings.Cut(kv, "="); key == "ANTHROPIC_BASE_URL" {
			base = value
		}
	}
	if base != "http://inference-gateway" {
		t.Errorf("ANTHROPIC_BASE_URL = %q, want the in-namespace inference gateway", base)
	}
}

// The default tool surface and the session fence have to agree: a tool that
// needs egress the policy denies does not fail, it hangs until the connect
// timeout, spending the task deadline on a black hole.
func TestDefaultToolSurfaceNeedsNoEgressTheFenceDenies(t *testing.T) {
	t.Setenv("A2A_ALLOWED_TOOLS", "")
	_ = os.Unsetenv("A2A_ALLOWED_TOOLS")
	t.Setenv("A2A_HARNESS_CMD", "")
	_ = os.Unsetenv("A2A_HARNESS_CMD")

	argv := harnessCommand()
	var allowed string
	for i, arg := range argv {
		if arg == "--allowedTools" && i+1 < len(argv) {
			allowed = argv[i+1]
		}
	}
	if allowed == "" {
		t.Fatalf("no --allowedTools in argv: %v", argv)
	}
	for _, networked := range []string{"WebFetch", "WebSearch", "Bash"} {
		if strings.Contains(allowed, networked) {
			t.Errorf("%s is in the default tool surface; the session fence permits only DNS, the bus and LiteLLM", networked)
		}
	}
}

// TestClusterViewAllowsBashAndSaysInspectOnly: A2A_CLUSTER_VIEW=true is the
// spawner telling the adapter the pod has the broker's read-only wrappers;
// Bash joins the surface and the system prompt says what the fence is.
func TestClusterViewAllowsBashAndSaysInspectOnly(t *testing.T) {
	for _, key := range []string{"A2A_ALLOWED_TOOLS", "A2A_HARNESS_CMD", "A2A_HARNESS_EXTRA_ARGS", "A2A_DELEGATE_TOOL"} {
		t.Setenv(key, "")
		_ = os.Unsetenv(key)
	}
	flags := func(argv []string) (allowed, disallowed, prompt string) {
		for i, arg := range argv {
			if i+1 >= len(argv) {
				break
			}
			switch arg {
			case "--allowedTools":
				allowed = argv[i+1]
			case "--disallowedTools":
				disallowed = argv[i+1]
			case "--append-system-prompt":
				prompt = argv[i+1]
			}
		}
		return
	}
	t.Setenv(lib.EnvClusterView, "")
	_ = os.Unsetenv(lib.EnvClusterView)
	allowed, disallowed, prompt := flags(harnessCommand())
	// The delegate tool is on by default (its switch is A2A_DELEGATE_TOOL,
	// not the cluster view), so it is expected here too: appended to
	// allowed, and its prompt is all there is since the view is off.
	if strings.Contains(allowed, "Bash") || !strings.Contains(disallowed, "Bash") || !strings.Contains(allowed, delegateToolID) || prompt != delegatePrompt {
		t.Fatalf("view off: allowed=%q disallowed=%q prompt=%q", allowed, disallowed, prompt)
	}
	t.Setenv(lib.EnvClusterView, "true")
	allowed, disallowed, prompt = flags(harnessCommand())
	if !strings.Contains(allowed, "Bash(kubectl:*)") || !strings.Contains(allowed, "Bash(gcloud:*)") || strings.Contains(disallowed, "Bash") || !strings.Contains(allowed, delegateToolID) {
		t.Fatalf("view on: allowed=%q disallowed=%q", allowed, disallowed)
	}
	// Confined to the two shims: a bare Bash would let the harness run node,
	// python3 or a loop against the keyless inference gateway the fence admits.
	for _, tool := range strings.Split(allowed, ",") {
		if tool == "Bash" {
			t.Fatalf("view on allows bare Bash: %q", allowed)
		}
	}
	for _, still := range []string{"Edit", "NotebookEdit"} {
		if !strings.Contains(disallowed, still) {
			t.Errorf("view on dropped %s from the disallowed list", still)
		}
	}
	// Both prompts apply with the view on and the delegate tool on (its
	// default): joined, not one replacing the other.
	if !strings.Contains(prompt, clusterViewPrompt) || !strings.Contains(prompt, "policy rule") || !strings.Contains(prompt, "delegate") {
		t.Fatalf("view on prompt = %q", prompt)
	}
	// An A2A_ALLOWED_TOOLS override without Bash wins over the view: Bash
	// stays disallowed, and the view's own prompt is not appended. The
	// delegate tool is unaffected by this override -- A2A_DELEGATE_TOOL is
	// its only switch -- so it still joins the allowed list and its prompt
	// is still the one that shows up.
	t.Setenv("A2A_ALLOWED_TOOLS", "Read,Grep")
	allowed, disallowed, prompt = flags(harnessCommand())
	if allowed != "Read,Grep,"+delegateToolID || !strings.Contains(disallowed, "Bash") || prompt != delegatePrompt {
		t.Fatalf("view on, override without Bash: allowed=%q disallowed=%q prompt=%q", allowed, disallowed, prompt)
	}
	// One that names Bash, bare or as a pattern, gets the view. The delegate
	// tool is appended on top of the override either way.
	for _, override := range []string{"Read,Bash", "Read Bash(kubectl:*)"} {
		t.Setenv("A2A_ALLOWED_TOOLS", override)
		allowed, disallowed, prompt = flags(harnessCommand())
		if allowed != override+","+delegateToolID || strings.Contains(disallowed, "Bash") || !strings.Contains(prompt, clusterViewPrompt) || !strings.Contains(prompt, "delegate") {
			t.Fatalf("view on, override %q: allowed=%q disallowed=%q prompt=%q", override, allowed, disallowed, prompt)
		}
	}
}

// originSeq is the join between the two halves the origin-sequence fix already
// pins: the spawner renders lib.EnvOriginSeq (spawn_test.go) and the adapter
// honours Config.OriginSeq / Config.OriginSeqStated (adapter_origin_cap_test.go,
// session_adapter_test.go), but both of those set the Config fields directly.
// Nothing read the pod env into them. A regression that returned (0, true) for a
// valid sequence, or (n, false), would leave both suites green while every
// gateway-spawned worker fell back to the scan this fix exists to stop — the
// scan that hands back the oldest surviving steer and executes it as the
// request.
//
// The two return values answer different questions and the test keeps them
// apart: the uint64 is the sequence, the bool is only "did the spawner say
// anything at all". Everything except an unset variable is a spawner that spoke,
// including the ones that spoke uselessly.
func TestOriginSeqReadsThePodEnv(t *testing.T) {
	for _, tc := range []struct {
		name       string
		raw        string // "" with unset=true means the variable is absent
		unset      bool
		wantSeq    uint64
		wantStated bool
		wantWarn   bool
	}{
		{
			// The by-hand and dispatcher-spawned shapes, and any pod from a
			// spawner older than the variable. Not stated, so the adapter
			// falls back to the scan.
			name:       "absent is nobody told me",
			unset:      true,
			wantSeq:    0,
			wantStated: false,
		},
		{
			// A current spawner whose own publish returned no usable
			// sequence. Stated, so the adapter can tell it from an old
			// spawner, and no warning: this is the sentinel working.
			name:       "the unknown sentinel is stated, not a fault",
			raw:        lib.OriginSeqUnknown,
			wantSeq:    0,
			wantStated: true,
		},
		{
			name:       "a real sequence",
			raw:        "42",
			wantSeq:    42,
			wantStated: true,
		},
		{
			// uint64's ceiling, to prove the parse is not an int.
			name:       "the largest sequence uint64 holds",
			raw:        "18446744073709551615",
			wantSeq:    18446744073709551615,
			wantStated: true,
		},
		{
			// Unparseable is the sentinel rather than a boot failure: the
			// worst it costs is the scan an older spawner already gets,
			// whereas refusing to start turns a typo into a dead session.
			name:       "junk warns and degrades to the sentinel",
			raw:        "not-a-number",
			wantSeq:    0,
			wantStated: true,
			wantWarn:   true,
		},
		{
			// JetStream sequences start at 1, so a zero is a spawner that
			// read an empty PubAck, not a message at the head.
			name:       "zero warns and degrades to the sentinel",
			raw:        "0",
			wantSeq:    0,
			wantStated: true,
			wantWarn:   true,
		},
		{
			// ParseUint rejects the sign rather than wrapping it.
			name:       "negative warns and degrades to the sentinel",
			raw:        "-1",
			wantSeq:    0,
			wantStated: true,
			wantWarn:   true,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(lib.EnvOriginSeq, tc.raw)
			if tc.unset {
				_ = os.Unsetenv(lib.EnvOriginSeq)
			}

			var logged bytes.Buffer
			log := slog.New(slog.NewJSONHandler(&logged, &slog.HandlerOptions{Level: slog.LevelWarn}))

			seq, stated := originSeq(log)

			if seq != tc.wantSeq {
				t.Errorf("sequence = %d, want %d for %s=%q", seq, tc.wantSeq, lib.EnvOriginSeq, tc.raw)
			}
			if stated != tc.wantStated {
				// Naming which way it is wrong, because the two
				// directions have opposite consequences: a false
				// negative sends a worker that was told the answer
				// back to the scan, a false positive makes a worker
				// that was told nothing act as if it were told 0.
				if tc.wantStated {
					t.Errorf("stated = false for %s=%q; the spawner did set the variable, so the adapter must not fall back to the scan", lib.EnvOriginSeq, tc.raw)
				} else {
					t.Errorf("stated = true with %s unset; nothing told this pod a sequence, so it has to take the scan", lib.EnvOriginSeq)
				}
			}

			warned := strings.Contains(logged.String(), "ignoring unusable origin sequence")
			if warned != tc.wantWarn {
				if tc.wantWarn {
					t.Errorf("no warning logged for %s=%q; an unusable value silently costs the worker its origin sequence, and the log line is the only place that says so. log: %q", lib.EnvOriginSeq, tc.raw, logged.String())
				} else {
					t.Errorf("warned on %s=%q, which is a usable value: %q", lib.EnvOriginSeq, tc.raw, logged.String())
				}
			}
			// The warning has to carry the value that caused it, or it
			// names no fault an operator can act on.
			if tc.wantWarn && !strings.Contains(logged.String(), tc.raw) {
				t.Errorf("warning does not quote the offending %s=%q: %q", lib.EnvOriginSeq, tc.raw, logged.String())
			}
		})
	}
}

// The capability switch, asserted against the Config this binary actually
// builds. Its sibling in a2a/worker-adapter read the field off a Config the
// test itself constructed and never set, so it asserted the zero value of a
// Go bool and would have passed against the `!= "true"` spelling that turns
// an unset variable fail-open. configFromEnv exists so this reaches the real
// mapping.
func TestTheWorkerBinaryRequiresACapabilityUnlessExactlyFalse(t *testing.T) {
	for _, tc := range []struct {
		name  string
		value string
		set   bool
		want  bool
	}{
		{name: "a default install sets nothing", set: false, want: false},
		{name: "empty is not consent", value: "", set: true, want: false},
		{name: "the rollout window", value: "false", set: true, want: true},
		{name: "explicitly required", value: "true", set: true, want: false},
		{name: "a typo enforces", value: "False", set: true, want: false},
		{name: "so does a lie", value: "0", set: true, want: false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("TASK_ID", "task-env-contract")
			t.Setenv("PROFILE", "chat")
			t.Setenv("NATS_URL", "nats://127.0.0.1:1")
			t.Setenv("A2A_CAPABILITY_REQUIRED", tc.value)
			if !tc.set {
				if err := os.Unsetenv("A2A_CAPABILITY_REQUIRED"); err != nil {
					t.Fatalf("could not unset: %v", err)
				}
			}
			cfg, ok := configFromEnv(slog.New(slog.NewJSONHandler(io.Discard, nil)))
			if !ok {
				t.Fatal("configFromEnv rejected an environment that has the required trio")
			}
			if cfg.CapabilityOptional != tc.want {
				t.Errorf("CapabilityOptional = %v, want %v with A2A_CAPABILITY_REQUIRED=%q (set=%v)",
					cfg.CapabilityOptional, tc.want, tc.value, tc.set)
			}
		})
	}
}

// TestAMalformedAuthorityScopeRefusesToStart: the session executor's
// A2A_AUTHORITY_SCOPE arrives resolved from the gateway's spawner, so a bad
// one should not reach a rendered pod. The check is here anyway because the
// bridge's sibling of this variable IS hand-typed and validates at boot, and
// the two executors sharing a security parser but not a validation is how the
// pair drifts. Unset stays legitimate.
func TestAMalformedAuthorityScopeRefusesToStart(t *testing.T) {
	for _, tc := range []struct {
		scope  string
		wantOK bool
	}{
		{scope: "kubeagents-system"},
		{scope: "namespace/"},
		{scope: "namespace/a/task"},
		{scope: "namespace/kubeagents-system", wantOK: true},
		{scope: "namespace/kubeagents-system/task/t-1", wantOK: true},
		{scope: "", wantOK: true},
	} {
		t.Run(tc.scope, func(t *testing.T) {
			t.Setenv("TASK_ID", "task-scope-contract")
			t.Setenv("PROFILE", "chat")
			t.Setenv("NATS_URL", "nats://127.0.0.1:1")
			t.Setenv("A2A_AUTHORITY_SCOPE", tc.scope)
			cfg, ok := configFromEnv(slog.New(slog.NewJSONHandler(io.Discard, nil)))
			if ok != tc.wantOK {
				t.Fatalf("configFromEnv with A2A_AUTHORITY_SCOPE=%q ok = %v, want %v", tc.scope, ok, tc.wantOK)
			}
			if ok && string(cfg.Scope) != tc.scope {
				t.Errorf("configFromEnv scope = %q, want %q", cfg.Scope, tc.scope)
			}
		})
	}
}

// TestAMalformedPodNamespaceRefusesToStart is the sibling of the bridge's
// TestAMalformedPodNamespaceIsABootFailure, and it exists because the bridge
// got that check one review round before this binary did. The comment here
// used to waive it on the grounds that "NamespaceScope never produces a
// malformed pair", which is false: NamespaceScope is "namespace/" + ns with
// no check on ns.
//
// Both separator parities are rows, because they fail differently and only
// one of them is loud. "team/x" builds a three-segment scope that
// Scope.Validate refuses, so the adapter boots and refuses every submission
// with "the resource is not a well-formed scope". "a/b/c" builds a
// four-segment scope that Scope.Validate ACCEPTS, as namespace=a plus a
// second pair b/c — the adapter runs, submissions are checked, and the
// ceiling is simply not the one anyone chose. A check written against the
// scope rather than the namespace would pass this test's first row and fail
// its second.
//
// The scope is left unset on every row: with it set, adapter.go never calls
// NamespaceScope and the namespace reaches nothing that parses it. That is
// also why the shipped install does not hit this — the spawner always writes
// A2A_AUTHORITY_SCOPE — and why the rows below describe a hand-run harness
// or a Deployment the operator did not render.
func TestAMalformedPodNamespaceRefusesToStart(t *testing.T) {
	for _, tc := range []struct {
		namespace string
		wantOK    bool
	}{
		{namespace: "team/x"},
		{namespace: "a/b/c"},
		{namespace: "/"},
		{namespace: "kubeagents-system", wantOK: true},
		{namespace: "", wantOK: true},
	} {
		t.Run(tc.namespace, func(t *testing.T) {
			t.Setenv("TASK_ID", "task-namespace-contract")
			t.Setenv("PROFILE", "chat")
			t.Setenv("NATS_URL", "nats://127.0.0.1:1")
			t.Setenv("A2A_AUTHORITY_SCOPE", "")
			t.Setenv("POD_NAMESPACE", tc.namespace)
			cfg, ok := configFromEnv(slog.New(slog.NewJSONHandler(io.Discard, nil)))
			if ok != tc.wantOK {
				t.Fatalf("configFromEnv with POD_NAMESPACE=%q ok = %v, want %v", tc.namespace, ok, tc.wantOK)
			}
			if ok && cfg.Namespace != tc.namespace {
				t.Errorf("configFromEnv namespace = %q, want %q", cfg.Namespace, tc.namespace)
			}
		})
	}
}

// TestTheHarnessIsHandedTheDelegateTool: the harness learns about the
// delegate tool over --mcp-config, pointed at this same binary run as `mcp`
// (worker-adapter/mcp.go), with --strict-mcp-config so a stray project or
// user MCP config cannot add tools the gateway never vetted. The tool is
// appended to --allowedTools regardless of what A2A_ALLOWED_TOOLS already
// names, because A2A_DELEGATE_TOOL=off -- not the allowed-tools override -- is
// the one switch for it.
func TestTheHarnessIsHandedTheDelegateTool(t *testing.T) {
	for _, key := range []string{"A2A_ALLOWED_TOOLS", "A2A_HARNESS_CMD", "A2A_HARNESS_EXTRA_ARGS", "A2A_DELEGATE_TOOL", lib.EnvClusterView} {
		t.Setenv(key, "")
	}
	argv := harnessCommand()
	flags := func(name string) string {
		for i, a := range argv {
			if a == name && i+1 < len(argv) {
				return argv[i+1]
			}
		}
		return ""
	}
	if !strings.Contains(flags("--allowedTools"), "mcp__a2a__delegate") {
		t.Fatalf("allowedTools = %q", flags("--allowedTools"))
	}
	var mcp struct {
		Servers map[string]struct {
			Command string            `json:"command"`
			Args    []string          `json:"args"`
			Env     map[string]string `json:"env"`
		} `json:"mcpServers"`
	}
	if err := json.Unmarshal([]byte(flags("--mcp-config")), &mcp); err != nil {
		t.Fatalf("mcp-config: %v (%q)", err, flags("--mcp-config"))
	}
	srv, ok := mcp.Servers[workeradapter.DelegateMCPServer]
	if !ok || len(srv.Args) != 1 || srv.Args[0] != "mcp" || srv.Env[workeradapter.EnvDelegateSocket] == "" {
		t.Fatalf("server = %+v", srv)
	}
	if !slices.Contains(argv, "--strict-mcp-config") {
		t.Fatal("strict mcp config missing")
	}
	if !strings.Contains(flags("--append-system-prompt"), "delegate") {
		t.Fatalf("prompt = %q", flags("--append-system-prompt"))
	}
	t.Setenv("A2A_DELEGATE_TOOL", "off")
	argv = harnessCommand()
	if slices.Contains(argv, "--mcp-config") || strings.Contains(flags("--allowedTools"), "mcp__") {
		t.Fatal("A2A_DELEGATE_TOOL=off still hands the tool over")
	}
}

// TestBothPromptsWhenTheViewAndTheToolAreOn: the cluster-view prompt and the
// delegate prompt are two different features that can both be on for the
// same pod, and --append-system-prompt only accepts the flag once, so this
// pins that the two get joined into a single value rather than the second
// silently winning.
func TestBothPromptsWhenTheViewAndTheToolAreOn(t *testing.T) {
	t.Setenv("A2A_DELEGATE_TOOL", "")
	t.Setenv(lib.EnvClusterView, "true")
	t.Setenv("A2A_ALLOWED_TOOLS", "")
	argv := harnessCommand()
	var prompt string
	for i, a := range argv {
		if a == "--append-system-prompt" {
			prompt = argv[i+1]
		}
	}
	if !strings.Contains(prompt, "kubectl") || !strings.Contains(prompt, "delegate") {
		t.Fatalf("prompt = %q", prompt)
	}
	if n := strings.Count(strings.Join(argv, " "), "--append-system-prompt"); n != 1 {
		t.Fatalf("append-system-prompt given %d times", n)
	}
}
