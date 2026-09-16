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
