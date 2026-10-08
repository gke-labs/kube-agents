// worker-adapter is the session-pod entry point: the thin shim between the
// bus and the harness (spec-subagent-profiles.md, "The adapter"). One task
// per process; the terminal state is the exit code.
//
// Env contract, matching what the gateway's spawner sets: TASK_ID, PROFILE,
// NATS_URL are the spec trio; A2A_SESSION carries the session addressee for
// gateway-spawned pods.
//
// Bus auth has two shapes. A2A_POD_NAME present means the pod carries a
// projected ServiceAccount token and the adapter authenticates with it, as a
// principal the callout mints for this pod alone. Absent, it falls back to
// NATS_USER/NATS_PASSWORD, the static credential. No spawner sets that pair
// for a worker any more; it is the by-hand path against a bus with no callout.
// The spawner sets A2A_POD_NAME and the token volume together, which is what
// makes the switch a property of the pod rather than a flag someone can
// half-set.
//
// Exit codes: 0 completed (or nothing to do), 1 failed, 2 rejected,
// 3 canceled, 143 evicted (SIGTERM).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

const (
	// defaultTaskDeadlineSeconds is the worker half of a contract whose other
	// half is the gateway's defaultTaskDeadline (a2a/gateway/config.go). The
	// two must agree, and its comment points here by env var name -- so the
	// value it points at has to be findable by name rather than as a bare
	// literal mid-function.
	defaultTaskDeadlineSeconds = 1800
	// defaultKillGraceSeconds is how long a SIGTERMed harness has to flush
	// before the adapter stops waiting for it.
	defaultKillGraceSeconds = 10

	// defaultWorkdir is the pod's scratch emptyDir. A local run falls back to
	// the current directory, which is why this is a default and not a require.
	defaultWorkdir = "/scratch"

	// defaultModel is the profile-neutral alias LiteLLM routes; naming the
	// concrete model belongs in the install, not here.
	defaultModel = "model-default"
	// defaultMaxTurns bounds one task's agent loop. A string because it is
	// passed straight through as an argv value.
	defaultMaxTurns = "20"
	// defaultAllowedTools is the harness tool surface, deliberately narrow:
	// no Bash, no in-place edits, nothing that reaches the network. It has to
	// agree with the session pod's egress fence -- see the comment at its use.
	defaultAllowedTools = "Read,Write,Glob,Grep,TodoWrite"

	// The cluster view's signal is lib.EnvClusterView, shared with the
	// spawner that sets it. Only then is Bash allowed (confined to the two
	// shims), and only with clusterViewPrompt appended: the fence is the
	// broker's read-only gate, and the prompt tells the model what the fence
	// is so a refusal is reported rather than retried.
	clusterViewAllowedTools = defaultAllowedTools + ",Bash(kubectl:*),Bash(gcloud:*)"
	defaultDisallowedTools  = "Bash,Edit,NotebookEdit"
	clusterViewDisallowed   = "Edit,NotebookEdit"
	clusterViewPrompt       = "You have read-only `kubectl` and `gcloud` on PATH. They run through a credential broker " +
		"that permits read verbs only: inspect and report, never change anything. Bash may run only kubectl and gcloud " +
		"commands, one per call, with no pipes or other programs; read the output yourself. A refused command prints " +
		"`policy rule: <rule>` on stderr; report the refusal instead of retrying or working around it."

	// delegateToolID is the MCP-qualified tool name the harness's
	// --allowedTools expects: "mcp__" + server name + "__" + tool name
	// (worker-adapter/mcp.go names both halves so this can't drift from the
	// server it is appended alongside).
	delegateToolID = "mcp__" + workeradapter.DelegateMCPServer + "__" + workeradapter.DelegateToolName
	// delegatePrompt tells the model the tool exists and when to reach for
	// it; the tool's own schema (mcp.go) covers how to call it.
	delegatePrompt = "You can hand a task to another agent with the `delegate` tool (addressee `platform` is the platform agent, which can read and act on the fleet). " +
		"The gateway checks that the user may reach that agent before anything runs. Calling `delegate` ends your turn; the agent's result arrives as your next turn, so say what you are delegating and why in one line first. " +
		"Delegate when the ask needs the fleet, a cluster or privileges you do not have; answer yourself when you can."

	// defaultModelBaseURL is the install's inference gateway. defaultModelAPIKey
	// is not a credential: the gateway here runs keyless and the harness only
	// checks that the variable is non-empty.
	defaultModelBaseURL = "http://inference-gateway"
	defaultModelAPIKey  = "a2a-playground" // #nosec G101 -- placeholder, not a secret
)

func main() {
	os.Exit(run())
}

func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)

	// `worker-adapter mcp` is not a task run: it is the stdio MCP server the
	// harness launches as a subprocess of itself, forwarding the session's
	// one delegate tool call to this adapter process over a unix socket
	// (worker-adapter/mcp.go). It has none of the task env below, so it is
	// dispatched before configFromEnv rather than folded into it.
	if len(os.Args) > 1 && os.Args[1] == "mcp" {
		if err := workeradapter.ServeDelegateMCP(context.Background(), os.Stdin, os.Stdout, workeradapter.DelegateSocketPath(), log); err != nil && !errors.Is(err, io.EOF) {
			fmt.Fprintln(os.Stderr, "mcp:", err)
			return 1
		}
		return 0
	}

	cfg, ok := configFromEnv(log)
	if !ok {
		return 1
	}

	// The harness works out of the pod's scratch emptyDir; falling back to
	// the current directory keeps the workdir working for a run by hand. The
	// delegate tool's socket defaults under /scratch too and has no such
	// fallback: a run by hand sets A2A_DELEGATE_SOCKET to a writable path or
	// A2A_DELEGATE_TOOL=off.
	workdir := os.Getenv("A2A_WORKDIR")
	if workdir == "" {
		workdir = defaultWorkdir
	}
	if err := os.Chdir(workdir); err != nil {
		log.Warn("workdir unavailable; staying put", "workdir", workdir, "err", err)
	}

	// SIGTERM is the eviction path: context cancellation tells the adapter
	// to flush, publish terminal failed reason worker-evicted, and exit 143 -
	// except on a turn that has already delegated, which completes with its
	// one-line result and exits 0 (workeradapter.Run).
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	res, err := workeradapter.Run(ctx, cfg)
	if err != nil {
		log.Error("adapter run failed", "task", cfg.TaskID, "state", string(res.State), "err", err)
	}
	switch {
	case res.Evicted:
		return 143
	case res.State == lib.StateCompleted:
		return 0
	case res.State == lib.StateRejected:
		return 2
	case res.State == lib.StateCanceled:
		return 3
	case res.State == "" && err == nil:
		// Task was already terminal on the stream; nothing to do is success.
		return 0
	default:
		return 1
	}
}

// configFromEnv is the whole environment contract in one place, split out of
// run so a test can reach it: run's next move is to chdir and dial, so every
// assertion about what the environment maps to had to be made against a
// Config the test built itself, which is an assertion about the test. The
// capability switch is the one that matters — see CapabilityOptional below.
// The bool is false when the required trio is missing or A2A_AUTHORITY_SCOPE
// is malformed, which run reports as exit 1.
func configFromEnv(log *slog.Logger) (workeradapter.Config, bool) {
	taskID := os.Getenv("TASK_ID")
	profile := os.Getenv("PROFILE")
	natsURL := os.Getenv("NATS_URL")
	if taskID == "" || profile == "" || natsURL == "" {
		log.Error("TASK_ID, PROFILE, and NATS_URL are required (spec-subagent-profiles.md env contract)")
		return workeradapter.Config{}, false
	}

	// The scope arrives resolved and already validated -- the gateway's
	// spawner writes it from a ceiling FromEnv ran Entry.Validate over --
	// so this refuses a shape that should not reach a rendered pod at all.
	// It is here because the bridge's sibling of this variable IS hand
	// typed, and one binary validating a security input while its twin
	// takes it raw is how the two executors drift. Empty is not checked:
	// unset is a legitimate state that the executor's own scope handling
	// governs, and what it falls back to is checked just below.
	scope := capability.Scope(os.Getenv("A2A_AUTHORITY_SCOPE"))
	if scope != "" {
		if err := scope.Validate(); err != nil {
			log.Error("A2A_AUTHORITY_SCOPE is not a well-formed scope; it is kind/name pairs, e.g. namespace/kubeagents-system",
				"scope", string(scope), "err", err)
			return workeradapter.Config{}, false
		}
	}

	// The fallback rung, checked whether or not the scope above is set,
	// because POD_NAMESPACE is read for more than the ceiling. An earlier
	// version of the comment above waived this on the grounds that
	// "NamespaceScope never produces a malformed pair"; that was false.
	// NamespaceScope is "namespace/" + ns with no check on ns, so with the
	// scope unset -- a hand-run harness, a Deployment the operator did not
	// render -- a POD_NAMESPACE carrying a separator gives adapter.go the
	// three-segment scope that refuses every submission, or worse the
	// four-segment one that quietly means something else. Same rule, same
	// function, as the bridge and the gateway: see
	// capability.ValidateNamespace.
	namespace := os.Getenv("POD_NAMESPACE")
	if err := capability.ValidateNamespace(namespace); err != nil {
		log.Error("POD_NAMESPACE is not a namespace name", "namespace", namespace, "err", err)
		return workeradapter.Config{}, false
	}

	originSeq, originSeqStated := originSeq(log)
	// delegateSocket feeds Config.DelegateSocket (commented in adapter.go):
	// it answers only the tool's own switch and knows nothing of
	// A2A_HARNESS_CMD, so with that override set the listener can start
	// while the harness it drives is never told the tool exists.
	delegateSocket := ""
	if delegateToolEnabled() {
		delegateSocket = workeradapter.DelegateSocketPath()
	}
	return workeradapter.Config{
		NATSURL:        natsURL,
		NATSUser:       os.Getenv("NATS_USER"),
		NATSPassword:   os.Getenv("NATS_PASSWORD"),
		BusTokenFile:   busTokenFile(),
		PodName:        os.Getenv(lib.EnvPodName),
		TaskID:         taskID,
		Profile:        profile,
		Session:        os.Getenv("A2A_SESSION"),
		Namespace:      namespace,
		Scope:          scope,
		DelegateSocket: delegateSocket,
		// Unset means required: a submission with no capability is
		// refused. "false" is the mixed-version window only — a gateway
		// that predates the mint. It does not switch enforcement off; a
		// capability that is present is always checked. The comparison
		// itself is in capability.OptionalFromEnv, under a table test,
		// because writing it out here is how `!= "true"` gets in.
		CapabilityOptional: capability.OptionalFromEnv(),
		OriginSeq:          originSeq,
		OriginSeqStated:    originSeqStated,
		HarnessCommand:     harnessCommand(),
		HarnessEnv:         harnessEnv(),
		TaskDeadline:       envDuration("A2A_TASK_DEADLINE_SECONDS", defaultTaskDeadlineSeconds),
		KillGrace:          envDuration("A2A_KILL_GRACE_SECONDS", defaultKillGraceSeconds),
		Logger:             log,
	}, true
}

// harnessCommand builds the harness argv: the native binary driven over the
// headless stream-json contract. A2A_HARNESS_CMD overrides the whole argv
// (tests, stubs); A2A_HARNESS_EXTRA_ARGS appends without replacing.
func harnessCommand() []string {
	if cmd := os.Getenv("A2A_HARNESS_CMD"); cmd != "" {
		return strings.Fields(cmd)
	}
	path := os.Getenv("A2A_HARNESS_PATH")
	if path == "" {
		path = workeradapter.DefaultHarnessPath
	}
	model := os.Getenv("A2A_MODEL")
	if model == "" {
		model = defaultModel
	}
	maxTurns := os.Getenv("A2A_MAX_TURNS")
	if maxTurns == "" {
		maxTurns = defaultMaxTurns
	}
	// The tool surface is deliberately narrow: no Bash, no in-place edits,
	// and nothing that reaches the network. The session pod's egress fence
	// (the operator's session NetworkPolicy) permits DNS, the bus and
	// LiteLLM, so WebFetch and WebSearch would not fail fast — a denied
	// egress under NetworkPolicy is a black hole, and each call would burn
	// its connect timeout against the task deadline. The fence is the
	// control; this list agreeing with it is what keeps the failure legible.
	// Under A2A_CLUSTER_VIEW the egress fence has one more peer, the
	// credential broker, and Bash is what reaches it.
	allowed := os.Getenv("A2A_ALLOWED_TOOLS")
	disallowed := defaultDisallowedTools
	clusterView := os.Getenv(lib.EnvClusterView) == "true"
	if allowed == "" {
		allowed = defaultAllowedTools
		if clusterView {
			allowed = clusterViewAllowedTools
		}
	}
	// The view is only on when Bash is actually allowed: an override that
	// leaves it out keeps Bash disallowed and gets no prompt telling the
	// model to use it.
	bashView := clusterView && allowsBash(allowed)
	if bashView {
		disallowed = clusterViewDisallowed
	}
	// The delegate tool has its own switch, independent of
	// A2A_ALLOWED_TOOLS: it is appended to whatever surface was resolved
	// above, including an explicit override, because the gateway -- not
	// this allowlist -- is what checks the addressee (worker-adapter/mcp.go).
	// A2A_DELEGATE_TOOL=off is the one way to run without it, for a stub
	// harness in tests or an older image.
	delegateTool := delegateToolEnabled()
	var prompts []string
	if bashView {
		prompts = append(prompts, clusterViewPrompt)
	}
	if delegateTool {
		allowed += "," + delegateToolID
		prompts = append(prompts, delegatePrompt)
	}
	argv := []string{
		path,
		"--print",
		"--output-format", "stream-json",
		"--input-format", "stream-json",
		"--verbose",
		"--model", model,
		"--max-turns", maxTurns,
		"--allowedTools", allowed,
		"--disallowedTools", disallowed,
	}
	if delegateTool {
		// The harness launches this same binary as `mcp` (worker-adapter/mcp.go)
		// and speaks MCP to it over stdio; --strict-mcp-config keeps a stray
		// project or user MCP config from adding tools the gateway never vetted.
		self, err := os.Executable()
		if err != nil || self == "" {
			self = "/usr/local/bin/worker-adapter"
		}
		sock := workeradapter.DelegateSocketPath()
		mcpCfg, _ := json.Marshal(map[string]any{"mcpServers": map[string]any{
			workeradapter.DelegateMCPServer: map[string]any{
				"command": self,
				"args":    []string{"mcp"},
				"env":     map[string]string{workeradapter.EnvDelegateSocket: sock},
			},
		}})
		argv = append(argv, "--mcp-config", string(mcpCfg), "--strict-mcp-config")
	}
	if len(prompts) > 0 {
		argv = append(argv, "--append-system-prompt", strings.Join(prompts, "\n\n"))
	}
	if extra := os.Getenv("A2A_HARNESS_EXTRA_ARGS"); extra != "" {
		argv = append(argv, strings.Fields(extra)...)
	}
	return argv
}

// delegateToolEnabled is the delegate tool's single switch: everything that
// advertises or starts it (the harness flags below, the Config wiring in
// configFromEnv, and the socket listener it feeds) reads this and nothing
// else, so A2A_DELEGATE_TOOL=off is one decision rather than three that could
// drift apart.
func delegateToolEnabled() bool {
	return os.Getenv("A2A_DELEGATE_TOOL") != "off"
}

// allowsBash reports whether a harness --allowedTools value names Bash, bare
// or as a pattern such as Bash(kubectl:*). The harness takes the list comma-
// or space-separated.
func allowsBash(allowed string) bool {
	for _, tool := range strings.FieldsFunc(allowed, func(r rune) bool { return r == ',' || r == ' ' }) {
		if tool == "Bash" || strings.HasPrefix(tool, "Bash(") {
			return true
		}
	}
	return false
}

// busTokenFile is where the adapter reads its bus credential, or "" for the
// static-credential path.
//
// Keyed on A2A_POD_NAME rather than on the file existing, because the two
// arrive together from the same pod spec and a missing file with the env set
// is a mount that failed — which should refuse at connect naming the path,
// not fall back to a credential this pod was deliberately not given.
func busTokenFile() string {
	if os.Getenv(lib.EnvPodName) == "" {
		return ""
	}
	if p := os.Getenv(lib.EnvBusTokenFile); p != "" {
		return p
	}
	return lib.BusTokenPath
}

// busCredentialEnv names the pod env the harness must not inherit. The
// adapter is the only thing in this pod with any business talking to the bus,
// and the harness is a model-directed subprocess with Read in its tool
// surface, so anything left in its environment is a prompt injection away
// from the bus.
//
// This list is no longer the defence it was, and it is worth being exact
// about why. It never worked: the adapter is PID 1 and the harness is its
// child at the same UID, so /proc/1/environ hands the harness the adapter's
// environment whatever this list says (gke-labs#1270). What closed that hole
// is the credential changing shape — a projected token, bound to this pod,
// good only for this session's own subjects. The list stays as tidiness, and
// as defence in depth on the by-hand static-credential path above.
//
// NATS_PASSWORD and NATS_USER stay listed even though the spawner no longer
// sets them. A name is removed from a list like this when it can never appear
// again, not when the current renderer stopped emitting it.
var busCredentialEnv = []string{"NATS_PASSWORD", "NATS_USER", "NATS_URL"}

// harnessEnv is the subprocess environment: the pod env minus the bus
// credential, plus model-auth defaults. With nothing configured, the harness
// talks to the install's own LiteLLM - Vertex-backed via the install's
// credentials, no per-worker key (playground posture; the deployment spec's
// auth story replaces this).
func harnessEnv() []string {
	withheld := make(map[string]bool, len(busCredentialEnv))
	for _, key := range busCredentialEnv {
		withheld[key] = true
	}
	var env []string
	for _, kv := range os.Environ() {
		if i := strings.Index(kv, "="); i > 0 && withheld[kv[:i]] {
			continue
		}
		env = append(env, kv)
	}
	has := func(key string) bool {
		return os.Getenv(key) != ""
	}
	if !has("ANTHROPIC_BASE_URL") && !has("CLAUDE_CODE_USE_VERTEX") && !has("ANTHROPIC_API_KEY") {
		env = append(env,
			"ANTHROPIC_BASE_URL="+defaultModelBaseURL,
			// LiteLLM here runs keyless; the value only satisfies the
			// harness's "an API key exists" check.
			"ANTHROPIC_API_KEY="+defaultModelAPIKey)
	}
	for _, kv := range []string{
		"CLAUDE_CODE_DISABLE_AUTO_MEMORY=1",
		"DISABLE_AUTOUPDATER=1",
		"DISABLE_TELEMETRY=1",
		"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1",
	} {
		key := kv[:strings.Index(kv, "=")]
		if !has(key) {
			env = append(env, kv)
		}
	}
	return env
}

// originSeq reads the submission's stream sequence from the pod env, and
// reports separately whether the spawner said anything at all.
//
// Unparseable is treated as the sentinel rather than as a boot failure: the
// worst it costs is the scan the older spawners already get, whereas refusing
// to start would turn a typo in one env var into a dead session.
func originSeq(log *slog.Logger) (uint64, bool) {
	raw := os.Getenv(lib.EnvOriginSeq)
	if raw == "" {
		return 0, false
	}
	if raw == lib.OriginSeqUnknown {
		return 0, true
	}
	seq, err := strconv.ParseUint(raw, 10, 64)
	if err != nil || seq == 0 {
		log.Warn("ignoring unusable origin sequence", lib.EnvOriginSeq, raw)
		return 0, true
	}
	return seq, true
}

func envDuration(key string, defSeconds int) time.Duration {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
		fmt.Fprintf(os.Stderr, "ignoring bad %s=%q\n", key, v)
	}
	return time.Duration(defSeconds) * time.Second
}
