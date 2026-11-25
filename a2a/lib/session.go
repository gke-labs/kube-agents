package lib

import "strings"

// Session consumer naming — the contract between a session worker and the
// grants the auth callout mints for it.
//
// Under per-session credentials a worker's JetStream reach is enumerated per
// consumer, and a consumer name is a single subject token: the grant
// $JS.API.CONSUMER.MSG.NEXT.TASKS.<name> can pin an exact name and nothing
// looser, because a wildcard there lets the holder pull from — or delete — any
// consumer on the stream whose name it can guess, the gateway's own durable
// included. So the worker names every consumer it creates from its own session
// name plus one of the roles below, and the callout grants exactly those. A
// worker that invents a fourth consumer is refused at CREATE, which is the
// right place to find out.
//
// nats.go's ordered consumers cannot be used here for the same reason: they
// are named <prefix>_<serial> by the library, so they cannot be granted
// exactly.

// EnvOriginSeq carries the TASKS stream sequence of the submission a session
// pod was spawned to execute, from the spawner's own PubAck.
//
// It exists because the worker cannot derive it. TASKS carries
// max_msgs_per_subject with discard=old, and the head that evicts on a task's
// `…in` subject is the originating kind:message; every steer that follows is
// kind:message too and nothing on the envelope tells them apart. A worker
// scanning that subject from the beginning therefore hands back the oldest
// surviving steer and executes it as the request. The signals that would let
// it notice — STREAM.INFO's first sequence, a get-by-subject — are both
// withheld from a session's grants on purpose (authcallout/session.go), and
// neither is subject-scoped, so widening them to close this would trade a
// wrong prompt for a session that can read the whole task plane. The spawner
// already knows the answer; this is it telling the worker.
//
// Unset means "nobody told me", which is the by-hand and dispatcher-spawned
// shapes and any pod from a spawner older than this variable. Those fall back
// to the scan. OriginSeqUnknown is the explicit form a current spawner sets
// when its own publish returned no usable sequence, so the worker can tell a
// spawner that could not answer from one that was never asked.
const (
	EnvOriginSeq     = "A2A_ORIGIN_SEQ"
	OriginSeqUnknown = "unknown"
	// EnvClusterView is the spawner telling the worker that this pod has
	// the credential broker's read-only kubectl/gcloud wrappers on PATH
	// (the operator's A2A_SESSION_CLUSTER_VIEW flag, rendered onto the
	// gateway and translated here per pod). Literal "true" is on. One
	// constant for both ends, like EnvOriginSeq above: a spelling that
	// drifted would leave a pod with the token and the shims but no Bash,
	// and nothing would say so.
	EnvClusterView = "A2A_CLUSTER_VIEW"
	// EnvProfileExecutor marks a pod the dispatcher spawned for an
	// AgentProfile (the A2A profile resource, not a Hermes profile
	// directory). Literal "true" is on. Such a pod has no A2A_SESSION: it
	// publishes as its profile and names its consumers for its pod, which is
	// what the callout's profile narrowing grants. The worker refuses an
	// empty A2A_SESSION under a bus token unless this says otherwise, so a
	// session pod whose spawner dropped its session name still fails at
	// startup rather than publishing as its profile.
	EnvProfileExecutor = "A2A_PROFILE_EXECUTOR"
	// EnvProfileWorker runs the adapter as a profile worker, which takes its
	// tasks from the dispatcher (worker-adapter/profile.go), not one task
	// named at start. Literal "true" is on, and it needs
	// EnvProfileExecutor and EnvProfileTier too. The operator renders it on
	// every pool and Job pod.
	EnvProfileWorker = "A2A_PROFILE_WORKER"
	// EnvProfileTier is the tier a profile worker's pool serves, which with
	// the profile and the pod names its dispatch subject.
	EnvProfileTier = "A2A_PROFILE_TIER"
	// EnvProfileWorkerOneShot ends a profile worker after its first task: a
	// Job's pod. Literal "1" or "true" is on.
	EnvProfileWorkerOneShot = "PROFILE_WORKER_ONE_SHOT"
	// EnvHarness names the harness a worker runs, session pod or profile
	// worker alike (persona.ClaudeCode or persona.Antigravity): its driver,
	// its argv and env, and a profile worker's persona adapter. Unset is
	// Claude Code; a value the worker doesn't know refuses to start.
	// Antigravity also needs EnvHarnessUnisolated and A2A_HARNESS_PATH
	// (cmd/worker-adapter's selectHarness).
	EnvHarness = "A2A_HARNESS"
	// EnvProfileHarness is EnvHarness's older name, read only by a profile
	// worker and only when EnvHarness is unset. It once picked the persona
	// adapter alone.
	EnvProfileHarness = "PROFILE_HARNESS"
	// EnvHarnessUnisolated must be HarnessUnisolatedAllow for a worker to
	// select persona.Antigravity, which runs without its per-task HOME
	// locking and workspace scrub until #6710 lands; #6710 removes it.
	// Nothing in the operator or chart sets it to allow; the operator
	// renders it empty beside A2A_HARNESS on a profile that names
	// spec.harness.type.
	EnvHarnessUnisolated   = "A2A_HARNESS_UNISOLATED"
	HarnessUnisolatedAllow = "allow"
	// EnvPersonaIntegrity sets whether a profile worker requires its persona
	// to carry a build-time content manifest (persona.IntegrityManifestFile).
	// PersonaIntegrityRequired refuses a persona without one; unset loads it
	// unchecked and logs that. A manifest that is present is checked either
	// way, and any other value stops the worker.
	EnvPersonaIntegrity = "A2A_PERSONA_INTEGRITY"
	// PersonaIntegrityRequired is EnvPersonaIntegrity's one value.
	PersonaIntegrityRequired = "required"
	// EnvPrimerFile names the file the spawner mounts the conversation's
	// transcript primer at (the pod's rehydration-primer annotation, through
	// the downward API). A pod spawned cold for a conversation that already
	// has turns (after a reap, or when the gateway could not reuse the live
	// pod) picks up the conversation it is continuing from it. Unset, or an
	// empty file, is a conversation with nothing before this turn.
	EnvPrimerFile = "A2A_PRIMER_FILE"
	// EnvDelegateTool turns the session's delegate tool off when it is
	// "off". The spawner sets it off on a one-task pod started for a wake
	// turn: the wake exists to report a delegation's outcome, and a wake that
	// delegates again on an interim answer ("still running, results will post
	// here") loops until the depth bound refuses, with nothing answered. A
	// reused pod runs wakes and human turns alike, so it is started with the
	// tool on, and its adapter turns the tool off for each task that is a
	// wake, from the submission's authority block.
	EnvDelegateTool = "A2A_DELEGATE_TOOL"
	// EnvProfileDelegates is "true" on a profile worker whose AgentProfile
	// sets spec.delegates: its tasks get the delegate tool, and a task that
	// delegates stays open for its children's results. The operator renders
	// it only for such a profile, and leaves EnvDelegateTool unset there;
	// every other profile pod keeps EnvDelegateTool "off". Literal "true" is
	// on, and the worker needs both (the tool's switch not "off") before it
	// offers the tool.
	EnvProfileDelegates = "A2A_PROFILE_DELEGATES"
	// EnvSessionReuse is "true" on a session pod that serves every turn of
	// its conversation rather than one task: after the task named by
	// TASK_ID it takes each new task from its own `…in` subjects. The
	// gateway routes a second task only to a pod it spawned with this set,
	// so a pod from an older spawner, which exits after one task, is never
	// sent one.
	EnvSessionReuse = "A2A_SESSION_REUSE"
)

const (
	// SessionConsumerOrigin fetches the submission that spawned the worker
	// from its own …in subject.
	SessionConsumerOrigin = "origin"
	// SessionConsumerIn is the live consumer on the same subject, positioned
	// after the submission: steers, follow-ups, cancel.
	SessionConsumerIn = "in"
	// SessionConsumerEvents reads the worker's own …events subject — the
	// respawn check asks it whether the task already reached a terminal
	// state.
	SessionConsumerEvents = "events"
)

// SessionConsumerRoles is every role above, in one place, so the callout's
// grant enumeration and the worker's usage cannot disagree about the set.
var SessionConsumerRoles = []string{SessionConsumerOrigin, SessionConsumerIn, SessionConsumerEvents}

// SessionConsumerName is the consumer name for one session and role. The
// session name is a dot-free DNS-1123 label (it is a subject token already —
// the addressee), so the result is a legal consumer name and a single subject
// token.
func SessionConsumerName(session, role string) string {
	return session + "-" + role
}

// ProfileWorkerStatusPort is the pod-local port a profile worker answers
// /readyz and /healthz on. The operator's readiness probe names it, so the
// two ends share this number.
const ProfileWorkerStatusPort = 8080

// The session pod's shipped Claude Code home: the persona file and the
// skills directory cmd/session-persona writes at image build, beside the
// state the harness keeps in the same directory.
const (
	SessionHomePersonaFile = "CLAUDE.md"
	SessionHomeSkillsDir   = "skills"
)

// SessionHomeCovers reports whether a path relative to the session pod's
// Claude Code home is part of what the image shipped there, and so what its
// build-time content manifest covers: the persona file and everything under
// the skills directory. The harness's own state beside them is not.
func SessionHomeCovers(rel string) bool {
	return rel == SessionHomePersonaFile || rel == SessionHomeSkillsDir ||
		strings.HasPrefix(rel, SessionHomeSkillsDir+"/")
}

// ProfilePersonaDir is where a profile pod mounts its persona, read-only
// (#3662). The operator's profilepod.PersonaMountPath is the same path.
const ProfilePersonaDir = "/persona"

// EnvProfileConfigRoot is where a profile worker makes each task's harness
// config directory: its own emptyDir, apart from the working directory.
// ProfileConfigRoot is the path the operator mounts it at.
const (
	EnvProfileConfigRoot = "A2A_PROFILE_CONFIG_ROOT"
	ProfileConfigRoot    = "/var/run/kube-agents/harness-config"
)

// EnvProfileResetPaths lists, colon-separated, the writable directories a
// profile worker empties after every task, so one task's files never reach
// the next on a warm pod. The operator renders it from the pod's writable
// mounts; the worker empties only what it names.
const EnvProfileResetPaths = "A2A_PROFILE_RESET_PATHS"
