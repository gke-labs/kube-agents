package lib

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
	// EnvPrimerFile names the file the spawner mounts the conversation's
	// transcript primer at (the pod's rehydration-primer annotation, through
	// the downward API). Every turn is a fresh pod, so this is how one picks
	// up the conversation it is continuing. Unset, or an empty file, is a
	// conversation with nothing before this turn.
	EnvPrimerFile = "A2A_PRIMER_FILE"
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
