// worker-adapter is the session-pod entry point: the thin shim between the
// bus and the harness (spec-subagent-profiles.md, "The adapter"). One task
// per process; the terminal state is the exit code.
//
// Env contract, matching what the gateway's spawner sets: TASK_ID, PROFILE,
// NATS_URL are the spec trio; A2A_SESSION carries the session addressee for
// gateway-spawned pods; NATS_USER/NATS_PASSWORD are the playground's static
// bus credentials. Everything else is tuning with defaults.
//
// Exit codes: 0 completed (or nothing to do), 1 failed, 2 rejected,
// 3 canceled, 143 evicted (SIGTERM).
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

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

	// defaultModelBaseURL is the install's own LiteLLM. defaultModelAPIKey is
	// not a credential: LiteLLM here runs keyless and the harness only checks
	// that the variable is non-empty.
	defaultModelBaseURL = "http://litellm"
	defaultModelAPIKey  = "a2a-playground" // #nosec G101 -- placeholder, not a secret
)

func main() {
	os.Exit(run())
}

func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)

	taskID := os.Getenv("TASK_ID")
	profile := os.Getenv("PROFILE")
	natsURL := os.Getenv("NATS_URL")
	if taskID == "" || profile == "" || natsURL == "" {
		log.Error("TASK_ID, PROFILE, and NATS_URL are required (spec-subagent-profiles.md env contract)")
		return 1
	}

	cfg := workeradapter.Config{
		NATSURL:        natsURL,
		NATSUser:       os.Getenv("NATS_USER"),
		NATSPassword:   os.Getenv("NATS_PASSWORD"),
		TaskID:         taskID,
		Profile:        profile,
		Session:        os.Getenv("A2A_SESSION"),
		HarnessCommand: harnessCommand(),
		HarnessEnv:     harnessEnv(),
		TaskDeadline:   envDuration("A2A_TASK_DEADLINE_SECONDS", defaultTaskDeadlineSeconds),
		KillGrace:      envDuration("A2A_KILL_GRACE_SECONDS", defaultKillGraceSeconds),
		Logger:         log,
	}

	// The harness works out of the pod's scratch emptyDir; falling back to
	// the current directory keeps local runs working.
	workdir := os.Getenv("A2A_WORKDIR")
	if workdir == "" {
		workdir = defaultWorkdir
	}
	if err := os.Chdir(workdir); err != nil {
		log.Warn("workdir unavailable; staying put", "workdir", workdir, "err", err)
	}

	// SIGTERM is the eviction path: context cancellation tells the adapter
	// to flush, publish terminal failed reason worker-evicted, and exit 143.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	res, err := workeradapter.Run(ctx, cfg)
	if err != nil {
		log.Error("adapter run failed", "task", taskID, "state", string(res.State), "err", err)
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
	allowed := os.Getenv("A2A_ALLOWED_TOOLS")
	if allowed == "" {
		allowed = defaultAllowedTools
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
		"--disallowedTools", "Bash,Edit,NotebookEdit",
	}
	if extra := os.Getenv("A2A_HARNESS_EXTRA_ARGS"); extra != "" {
		argv = append(argv, strings.Fields(extra)...)
	}
	return argv
}

// busCredentialEnv names the pod env the harness must not inherit. The
// adapter is the only thing in this pod with any business talking to the bus,
// and the worker user is shared across every session pod: its password
// publishes task events for any addressee and subscribes to a2a.tasks.>. The
// harness is a model-directed subprocess with Read in its tool surface and
// /proc/self/environ readable at its own UID, so anything left in its
// environment is a prompt injection away from the artifact stream.
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

func envDuration(key string, defSeconds int) time.Duration {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Second
		}
		fmt.Fprintf(os.Stderr, "ignoring bad %s=%q\n", key, v)
	}
	return time.Duration(defSeconds) * time.Second
}
