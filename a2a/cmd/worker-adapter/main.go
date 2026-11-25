// worker-adapter is the session-pod entry point: the thin shim between the
// bus and the harness (spec-subagent-profiles.md, "The adapter"). One task
// per process, and its terminal state is the exit code, unless the spawner
// sets A2A_SESSION_REUSE=true: then the process serves every turn of its
// session's conversation until SIGTERM, and the exit code is the last task's
// (0 when the SIGTERM found it idle).
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
//
// A2A_PROFILE_WORKER=true is the AgentProfile worker instead (#3655): a pool
// or Job pod that takes its tasks from the dispatcher (workeradapter.
// RunProfileWorker). It needs no TASK_ID, and A2A_PROFILE_TIER names its
// pool. It answers /readyz on lib.ProfileWorkerStatusPort once it's
// listening. It exits 0 when it stops (at SIGTERM, or after one task with
// PROFILE_WORKER_ONE_SHOT), and 1 if it can't start.
// A2A_PERSONA_INTEGRITY=required refuses a persona without a build-time
// content manifest; any other non-empty value exits 1.
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
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
	// writeRefusalSubdir holds a session pod's per-turn write-refusal record
	// under the workdir (sessionWriteRefusalDir).
	writeRefusalSubdir = ".kube-agents"

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

	// noClusterPrompt is appended when a session pod has no cluster view. A
	// turn that can't reach the cluster used to search the pod's filesystem
	// and environment for credentials or tools, narrating each step, before
	// answering; there is nothing there to find. Not for an AgentProfile's
	// pod (lib.EnvProfileExecutor), which reaches clusters its own way.
	noClusterPrompt = "You have no access to any cluster or the fleet on this turn, and this pod's files and environment hold no cluster credentials or tools, so don't search them for any. When an ask needs a cluster, delegate it if you can; otherwise say plainly that you can't reach it from here."

	// delegateToolID is the MCP-qualified tool name the harness's
	// --allowedTools expects: "mcp__" + server name + "__" + tool name
	// (worker-adapter/mcp.go names both halves so this can't drift from the
	// server it is appended alongside).
	delegateToolID = "mcp__" + workeradapter.DelegateMCPServer + "__" + workeradapter.DelegateToolName
	// delegatePrompt tells the model the tool exists and when to reach for
	// it; the tool's own schema (mcp.go) covers how to call it. Only session
	// pods carry it (an AgentProfile pool sets A2A_DELEGATE_TOOL=off). Its
	// last two sentences are for a session with the cluster view: its own
	// reads pull it toward reading first, and an ask that also needs a change
	// then splits into read delegations whose change never reaches platform
	// (#6859). Handing the whole ask over in one turn is the shorter path
	// whether or not a later turn could delegate again. platform is offered
	// only to users its allowlist admits (gateway/delegatetargets.go), and an
	// agent the person named comes first; a proposal-only ask stays in the
	// reply (#2610).
	delegatePrompt = "You can hand a task to another agent with the `delegate` tool. Its addressee field lists the agents you may use this turn: usually `platform`, the platform agent, which can read and act on the fleet, and any agent profile this conversation can reach. If it says no agent is available, don't call it. " +
		"When the person asks for one of those agents by name, delegate to it under that name. " +
		"The gateway checks that the user may reach that agent before anything runs. Calling `delegate` ends your turn; the agent's result arrives as your next turn, so say what you are delegating and why in one line first. " +
		"Delegate when the ask needs the fleet, a cluster or privileges you do not have; answer yourself when you can. " +
		"When the person wants a change made, applied or opened as a pull request, and `platform` is listed, hand the whole ask to `platform` in the turn it arrives, with the reads it depends on, unless they named another agent: one handoff is the shorter path, and `platform` makes those reads itself. " +
		"A fix you are only asked to propose is not a change: write it in your reply."
)

func main() {
	os.Exit(run())
}

// serveMCP is `worker-adapter mcp`: the delegate MCP server over in and out,
// at the socket the adapter named and with the delegate targets it wrote into
// this process's env for the turn.
func serveMCP(ctx context.Context, in io.Reader, out io.Writer, log *slog.Logger) error {
	return workeradapter.ServeDelegateMCP(ctx, in, out, workeradapter.DelegateSocketPath(), workeradapter.DelegateTargetsFromEnv(log), log)
}

func run() int {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	slog.SetDefault(log)

	// The two subcommands are not task runs: each is a stdio MCP server the
	// harness launches as a subprocess of itself, with none of the task env
	// below, so they are dispatched before configFromEnv rather than folded
	// into it.
	//
	// `worker-adapter mcp-relay <url>` is what a profile persona's broker
	// servers are rendered as (worker-adapter/mcprelay.go): it carries
	// messages to the broker's relay with the pod's credentials.
	if len(os.Args) > 1 && os.Args[1] == workeradapter.MCPRelayCommand {
		if len(os.Args) != 3 {
			fmt.Fprintln(os.Stderr, "usage: worker-adapter mcp-relay <broker relay URL>")
			return 2
		}
		if err := workeradapter.ServeMCPRelay(context.Background(), os.Args[2], os.Stdin, os.Stdout, log); err != nil && !errors.Is(err, io.EOF) {
			fmt.Fprintln(os.Stderr, "mcp-relay:", err)
			return 1
		}
		return 0
	}
	// `worker-adapter mcp` forwards the session's one delegate tool call to
	// this adapter process over a unix socket (worker-adapter/mcp.go).
	if len(os.Args) > 1 && os.Args[1] == "mcp" {
		if err := serveMCP(context.Background(), os.Stdin, os.Stdout, log); err != nil && !errors.Is(err, io.EOF) {
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
	cfg.WriteRefusalDir = sessionWriteRefusalDir(cfg.Session, workdir)

	// SIGTERM is the eviction path: context cancellation tells the adapter
	// to flush, publish terminal failed reason worker-evicted, and exit 143 -
	// except on a turn that has already delegated, which completes with its
	// one-line result and exits 0 (workeradapter.Run).
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	if os.Getenv(lib.EnvProfileWorker) == "true" {
		oneShot := os.Getenv(lib.EnvProfileWorkerOneShot)
		// The harness configFromEnv selected: its driver names it.
		harness := cfg.Driver.Harness()
		cfg = profileModeConfig(cfg)
		requireIntegrity, err := workeradapter.ParsePersonaIntegrity(os.Getenv(lib.EnvPersonaIntegrity))
		if err != nil {
			log.Error("profile worker failed", "err", err)
			return 1
		}
		pw := workeradapter.ProfileWorkerConfig{
			Tier:       os.Getenv(lib.EnvProfileTier),
			OneShot:    oneShot == "1" || oneShot == "true",
			ReadyAddr:  fmt.Sprintf(":%d", lib.ProfileWorkerStatusPort),
			PersonaDir: lib.ProfilePersonaDir,
			// Whether the persona must carry a build-time content manifest.
			RequirePersonaIntegrity: requireIntegrity,
			Harness:                 harness,
			Workdir:                 workdir,
			ConfigRoot:              envOrDefault(lib.EnvProfileConfigRoot, lib.ProfileConfigRoot),
			// The broker's task-token file (#3889's proxy confinement).
			TaskTokenFile: os.Getenv("CREDENTIAL_PROXY_TASK_TOKEN_FILE"),
			// The persona's MCP servers through the broker's relay (#5495).
			Relay:      workeradapter.ProfileRelay(selfPath(), os.Getenv),
			ResetPaths: resetPaths(os.Getenv(lib.EnvProfileResetPaths)),
			// What a script-run task's credential is verified against
			// (#5622): rendered into Job templates only.
			TaskSigningKeys: os.Getenv("A2A_TASK_SIGNING_PUBLIC_KEYS"),
		}
		if err := workeradapter.RunProfileWorker(ctx, cfg, pw); err != nil {
			log.Error("profile worker failed", "err", err)
			return 1
		}
		return 0
	}

	// The session's shipped persona and skills, checked against their
	// build-time manifest before the harness first runs.
	if dir := harnessConfigDir(); dir != "" {
		if err := workeradapter.CheckSessionHome(dir, log); err != nil {
			log.Error("session worker failed", "err", err)
			return 1
		}
	}

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

// resetPaths splits lib.EnvProfileResetPaths, dropping empty entries and any
// that isn't absolute: a relative path would empty whatever directory the
// worker happened to start in.
func resetPaths(v string) []string {
	var out []string
	for _, p := range strings.Split(v, ":") {
		if p != "" && filepath.IsAbs(p) && filepath.Clean(p) != "/" {
			out = append(out, filepath.Clean(p))
		}
	}
	return out
}

// profileModeConfig is cfg for a profile worker. The persona supplies the
// tool flags, so the base argv carries none, and the delegate tool is not
// among them, so no delegate socket is bound for a harness that would never
// be told of it.
func profileModeConfig(cfg workeradapter.Config) workeradapter.Config {
	cfg.HarnessCommand = profileHarnessCommand()
	cfg.HarnessCommandNoDelegate = nil
	// The persona is a profile worker's instruction layer; the session
	// lines are not its own.
	cfg.Driver = workeradapter.WithInstructions(cfg.Driver, nil)
	cfg.DelegateSocket = ""
	return cfg
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
	// A profile worker learns each task from the dispatcher's offer, so it
	// starts with no TASK_ID.
	profileWorker := os.Getenv(lib.EnvProfileWorker) == "true"
	if (taskID == "" && !profileWorker) || profile == "" || natsURL == "" {
		log.Error("TASK_ID, PROFILE, and NATS_URL are required (spec-subagent-profiles.md env contract); a profile worker needs no TASK_ID")
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

	harness, driver, builder, err := selectHarness(os.Getenv)
	if err != nil {
		log.Error("the worker's harness is not one it can run",
			lib.EnvHarness, os.Getenv(lib.EnvHarness), lib.EnvProfileHarness, os.Getenv(lib.EnvProfileHarness), "err", err)
		return workeradapter.Config{}, false
	}
	// core-agent reads a session pod's system-prompt lines from each turn's
	// envelope, not its argv. No delegate line: core-agent has no delegate
	// tool yet (its MCP servers aren't wired), and a model told of a tool it
	// doesn't have calls it. A profile worker drops these (profileModeConfig).
	driver = workeradapter.WithInstructions(driver, sessionPrompts(os.Getenv))
	// Chosen before anything below builds an argv or env from it.
	activeHarness = builder
	log.Info("harness selected", "harness", string(harness))

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
		NATSURL:      natsURL,
		NATSUser:     os.Getenv("NATS_USER"),
		NATSPassword: os.Getenv("NATS_PASSWORD"),
		BusTokenFile: busTokenFile(),
		PodName:      os.Getenv(lib.EnvPodName),
		TaskID:       taskID,
		Profile:      profile,
		Session:      os.Getenv("A2A_SESSION"),
		Primer:       readPrimer(log),
		// Literal "true" only, like EnvClusterView.
		ProfileExecutor: os.Getenv(lib.EnvProfileExecutor) == "true",
		Namespace:       namespace,
		Scope:           scope,
		DelegateSocket:  delegateSocket,
		// Unset means required: a submission with no capability is
		// refused. "false" is the mixed-version window only — a gateway
		// that predates the mint. It does not switch enforcement off; a
		// capability that is present is always checked. The comparison
		// itself is in capability.OptionalFromEnv, under a table test,
		// because writing it out here is how `!= "true"` gets in.
		CapabilityOptional: capability.OptionalFromEnv(),
		OriginSeq:          originSeq,
		OriginSeqStated:    originSeqStated,
		Driver:             driver,
		HarnessCommand:     harnessCommand(),
		HarnessEnv:         harnessEnv(),
		// Removed before each turn of a reused pod; see the field's comment.
		HarnessUserSettings: harnessUserSettings(),
		TaskDeadline:        envDuration("A2A_TASK_DEADLINE_SECONDS", defaultTaskDeadlineSeconds),
		KillGrace:           envDuration("A2A_KILL_GRACE_SECONDS", defaultKillGraceSeconds),
		Logger:              log,

		// The same argv with the delegate tool off, for a task that runs
		// without it whatever the pod was started with (a wake in a reused
		// pod; workeradapter's delegateOffForTask).
		HarnessCommandNoDelegate: harnessCommandWithoutDelegate(),
		// Literal "true" only, like EnvClusterView; a profile pod never
		// serves a second task, and validate refuses the pair.
		SessionReuse: os.Getenv(lib.EnvSessionReuse) == "true",
	}, true
}

// sessionWriteRefusalDir is where a session pod with the cluster view keeps
// each turn's write-refusal record (Config.WriteRefusalDir): under the workdir,
// the pod's scratch emptyDir, which the harness's own kubectl wrapper can
// append to. Any other pod gets none: a profile pod's worker makes its own
// file, and a session pod with no cluster view runs no kubectl.
func sessionWriteRefusalDir(session, workdir string) string {
	if session == "" || os.Getenv(lib.EnvClusterView) != "true" {
		return ""
	}
	return filepath.Join(workdir, writeRefusalSubdir)
}

// sessionToolLists is a session pod's tool allowlist and denylist, whether
// it has the cluster view, and whether that view's Bash is allowed. The view
// is only on when Bash is actually allowed: an override that leaves it out
// keeps Bash disallowed and gets no prompt telling the model to use it.
func sessionToolLists(getenv func(string) string) (allowed, disallowed string, clusterView, bashView bool) {
	allowed = getenv("A2A_ALLOWED_TOOLS")
	disallowed = defaultDisallowedTools
	clusterView = getenv(lib.EnvClusterView) == "true"
	if allowed == "" {
		allowed = defaultAllowedTools
		if clusterView {
			allowed = clusterViewAllowedTools
		}
	}
	bashView = clusterView && allowsBash(allowed)
	if bashView {
		disallowed = clusterViewDisallowed
	}
	return allowed, disallowed, clusterView, bashView
}

// sessionPrompts is a session pod's system-prompt lines for its cluster
// access, before the delegate line, which goes only with the delegate tool.
// The no-cluster line keys on the view, not on Bash: a view pod whose
// override leaves Bash out still holds the broker's URL, token file and
// shims, so telling it they aren't there would be false. It gets neither.
func sessionPrompts(getenv func(string) string) []string {
	_, _, clusterView, bashView := sessionToolLists(getenv)
	switch {
	case bashView:
		return []string{clusterViewPrompt}
	case !clusterView && getenv(lib.EnvProfileExecutor) != "true":
		return []string{noClusterPrompt}
	}
	return nil
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
		path = activeHarness.defaultPath()
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
	allowed, disallowed, _, _ := sessionToolLists(os.Getenv)
	// The delegate tool has its own switch, independent of
	// A2A_ALLOWED_TOOLS: it is appended to whatever surface was resolved
	// above, including an explicit override, because the gateway -- not
	// this allowlist -- is what checks the addressee (worker-adapter/mcp.go).
	// A2A_DELEGATE_TOOL=off is the one way to run without it, for a stub
	// harness in tests or an older image.
	delegateTool := delegateToolEnabled()
	prompts := sessionPrompts(os.Getenv)
	if delegateTool {
		allowed += "," + delegateToolID
		prompts = append(prompts, delegatePrompt)
	}
	spec := harnessSpec{
		path:       path,
		model:      model,
		maxTurns:   maxTurns,
		tools:      true,
		allowed:    allowed,
		disallowed: disallowed,
		prompts:    prompts,
		extra:      strings.Fields(os.Getenv("A2A_HARNESS_EXTRA_ARGS")),
		turnBudget: harnessTurnBudget(),
	}
	if delegateTool {
		// The harness launches this same binary as `mcp` (worker-adapter/mcp.go)
		// and speaks MCP to it over stdio; the builder keeps any other MCP
		// config the harness might find from adding tools the gateway never
		// vetted.
		spec.delegate = &delegateServer{command: selfPath(), socket: workeradapter.DelegateSocketPath()}
	}
	return activeHarness.command(spec)
}

// profileHarnessCommand is a profile worker's base argv: the harness over the
// headless stream-json contract, the model and the turn budget, and no tool,
// MCP or settings flags. The worker appends the persona's rendering of those
// for each task (worker-adapter/profile.go), and a second copy here would
// leave the harness to pick between two allowlists. A2A_HARNESS_CMD still
// overrides it whole, for tests.
func profileHarnessCommand() []string {
	if cmd := os.Getenv("A2A_HARNESS_CMD"); cmd != "" {
		return strings.Fields(cmd)
	}
	path := os.Getenv("A2A_HARNESS_PATH")
	if path == "" {
		path = activeHarness.defaultPath()
	}
	model := os.Getenv("A2A_MODEL")
	if model == "" {
		model = defaultModel
	}
	maxTurns := os.Getenv("A2A_MAX_TURNS")
	if maxTurns == "" {
		maxTurns = defaultMaxTurns
	}
	return activeHarness.command(harnessSpec{
		path:       path,
		model:      model,
		maxTurns:   maxTurns,
		extra:      strings.Fields(os.Getenv("A2A_HARNESS_EXTRA_ARGS")),
		turnBudget: harnessTurnBudget(),
	})
}

// harnessTurnBudget is harnessSpec.turnBudget from the same variables
// Config.TaskDeadline and Config.KillGrace read. A profile worker's task
// deadline comes from its offer and can be shorter; the adapter's own
// deadline still bounds that task, and only the harness's clean ending is
// lost.
func harnessTurnBudget() time.Duration {
	return envDuration("A2A_TASK_DEADLINE_SECONDS", defaultTaskDeadlineSeconds) -
		envDuration("A2A_KILL_GRACE_SECONDS", defaultKillGraceSeconds)
}

// delegateToolEnabled is the delegate tool's single switch: everything that
// advertises or starts it (the harness flags below, the Config wiring in
// configFromEnv, and the socket listener it feeds) reads this and nothing
// else, so A2A_DELEGATE_TOOL=off is one decision rather than three that could
// drift apart.
func delegateToolEnabled() bool {
	return os.Getenv(lib.EnvDelegateTool) != "off" && !delegateToolSuppressed
}

// delegateToolSuppressed is set only while harnessCommandWithoutDelegate
// builds its argv, so harnessCommand stays the one place the harness flags are
// assembled. configFromEnv runs once, before anything else is started, so
// nothing reads it concurrently.
var delegateToolSuppressed bool

// harnessCommandWithoutDelegate is harnessCommand as it would be under
// A2A_DELEGATE_TOOL=off: no delegate tool on the allowlist, no MCP config, no
// delegate prompt. It keeps --strict-mcp-config, so the harness runs with no
// MCP servers at all.
func harnessCommandWithoutDelegate() []string {
	delegateToolSuppressed = true
	defer func() { delegateToolSuppressed = false }()
	return harnessCommand()
}

// harnessUserSettings is the settings file the harness reads for its user
// source, removed before each turn of a reused pod, or "" when there is none
// to find.
func harnessUserSettings() string {
	return activeHarness.userSettings(os.Getenv)
}

// harnessConfigDir is the harness's config directory: $CLAUDE_CONFIG_DIR,
// else ~/.claude. "" when neither is known.
func harnessConfigDir() string {
	return claudeConfigDir(os.Getenv)
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
// auth story replaces this). The placeholder key goes in whenever the pod
// sets no key, no bearer token and no Vertex, so a pod spec never needs one.
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
	return activeHarness.env(env, func(key string) bool { return os.Getenv(key) != "" })
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

// readPrimer reads the conversation so far from the file the spawner mounts
// (lib.EnvPrimerFile). Unset or missing is a first turn, not an error: a pod
// from a spawner older than the mount, or one started by hand. A read error
// is logged and the turn runs without it, since a turn with no context
// beats no turn.
func readPrimer(log *slog.Logger) string {
	path := os.Getenv(lib.EnvPrimerFile)
	if path == "" {
		return ""
	}
	b, err := os.ReadFile(path) // #nosec G304 G703 -- the spawner's own mount path (lib.EnvPrimerFile)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Warn("could not read the transcript primer; this turn runs without the conversation so far", "path", path, "error", err)
		}
		return ""
	}
	return string(b)
}

// envOrDefaultFrom is getenv(key), or def when that is empty.
func envOrDefaultFrom(getenv func(string) string, key, def string) string {
	if v := getenv(key); v != "" {
		return v
	}
	return def
}

// envOrDefault is the variable's value, or def when it's unset.
func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// selfPath is this binary's path, for the harness to launch it again as an
// MCP server (`mcp`, `mcp-relay`): the image's install path when the running
// executable can't be found.
func selfPath() string {
	self, err := os.Executable()
	if err != nil || self == "" {
		return "/usr/local/bin/worker-adapter"
	}
	return self
}

// selectHarness picks the pod's harness: lib.EnvHarness, else on a profile
// worker lib.EnvProfileHarness (its older name), else Claude Code. It returns
// the harness's stream driver and argv builder. A value the worker doesn't
// know is an error, never a fall back to Claude Code: a typo would otherwise
// run the wrong harness without a word.
func selectHarness(getenv func(string) string) (persona.Harness, workeradapter.Driver, harnessBuilder, error) {
	h := persona.Harness(getenv(lib.EnvHarness))
	if h == "" && getenv(lib.EnvProfileWorker) == "true" {
		h = persona.Harness(getenv(lib.EnvProfileHarness))
	}
	driver, err := workeradapter.DriverFor(h)
	if err != nil {
		return "", nil, nil, err
	}
	// Antigravity runs without the per-task HOME locking and workspace scrub
	// it needs (#6710), so only a pod that says it accepts that may select
	// it: a test, or a deliberate dev run. Nothing in the operator or chart
	// sets it to allow. Where the operator renders A2A_HARNESS, it renders
	// this one empty beside it, so the worker image's own ENV can't set it
	// there: on a profile worker whose profile names spec.harness.type
	// (#6832), and later on session pods (#6716). Elsewhere only the pod
	// spec or the image's ENV can set it; a profile with no type gets
	// neither variable, by design, since its image is already trusted code.
	// core-agent likewise, until its end-of-turn sweep lands (#6710).
	if (driver.Harness() == persona.Antigravity || driver.Harness() == persona.CoreAgent) && getenv(lib.EnvHarnessUnisolated) != lib.HarnessUnisolatedAllow {
		return "", nil, nil, fmt.Errorf("harness %q runs without its isolation until #6710 lands; set %s=%s to run it anyway",
			driver.Harness(), lib.EnvHarnessUnisolated, lib.HarnessUnisolatedAllow)
	}
	builder, err := builderFor(driver.Harness())
	if err != nil {
		return "", nil, nil, err
	}
	// core-agent's tool grant travels in every turn's envelope, so the
	// driver carries it. One that doesn't map or render stops the worker
	// here rather than running with every tool refused.
	if driver.Harness() == persona.CoreAgent {
		p, err := coreAgentSessionGrant(getenv, envOrDefaultFrom(getenv, "A2A_WORKDIR", defaultWorkdir))
		if err != nil {
			return "", nil, nil, fmt.Errorf("the tool grant for harness %q: %w", driver.Harness(), err)
		}
		driver = workeradapter.NewCoreAgentDriver(p, nil)
	}
	// A harness with no default path in the image needs one named, unless a
	// test replaces the whole argv. Refused here, at startup, rather than at
	// the first task's exec.
	if builder.defaultPath() == "" && getenv("A2A_HARNESS_PATH") == "" && getenv("A2A_HARNESS_CMD") == "" {
		return "", nil, nil, fmt.Errorf("harness %q has no default path in this image; set A2A_HARNESS_PATH", driver.Harness())
	}
	return driver.Harness(), driver, builder, nil
}

// builderFor is the argv and env builder for a harness that has a driver. A
// harness with a driver but no builder is an error, never Claude Code's
// argv and env under another harness's driver.
func builderFor(h persona.Harness) (harnessBuilder, error) {
	switch h {
	case persona.ClaudeCode:
		return claudeBuilder{}, nil
	case persona.Antigravity:
		return agyBuilder{}, nil
	case persona.CoreAgent:
		return coreAgentBuilder{}, nil
	}
	return nil, fmt.Errorf("harness %q has no argv builder", h)
}
