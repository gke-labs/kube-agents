package authcallout

import (
	"fmt"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Claim narrowing: a map entry whose grants are built at mint time from a
// claim the API server attested, rather than read from the map.
//
// Session pods are the case. Every session pod runs as one shared
// ServiceAccount — a KSA per conversation would be a credential-bearing object
// created and reaped per chat, the orphan class the pod sweep just closed, in
// its most dangerous form — so the ServiceAccount alone cannot tell two
// sessions apart. What can is the pod: a projected token is bound to the pod it
// was issued to, TokenReview reports that pod's name and UID in the user's
// Extra fields, and the API server invalidates the token when the pod object
// goes. The gateway names the pod after the bus session it spawned it for, so
// the attested pod name IS the addressee, and the callout can issue exactly
// that incarnation's subjects from the claim alone.
//
// The map entry for such a principal renders no grants at all, on purpose. If
// it carried the real grants and a code path narrowed them, then a skipped or
// broken path would hand a session everything — `worker` reborn the first time
// someone edited the map without knowing the code narrowed it. An entry that
// is unusable on its own fails closed: no claim, no grants, no connection.

const (
	// NarrowingPod marks an entry whose grants derive from the attested
	// pod name. It is the only narrowing this callout knows.
	NarrowingPod = "pod"

	// The Extra keys the API server writes onto a TokenReview of a
	// pod-bound token. Written by the authenticator from the token's own
	// bound-object claim, never by the client.
	extraPodName = "authentication.kubernetes.io/pod-name"
	extraPodUID  = "authentication.kubernetes.io/pod-uid"
)

// sessionGrants is what one session incarnation may do on the bus, derived
// from its attested pod name and nothing else.
//
// Enumerated from what the worker adapter actually does (publish its own
// events; three named consumers on TASKS over its own subjects; replies on its
// own inbox) and closed under JetStream's body-field escapes the way A1 closed
// them for the callout principals and for web:
//
//   - Consumer names are exact. A wildcard in MSG.NEXT or DELETE would let a
//     session drain or destroy the gateway's task-plane durable, or another
//     session's consumers.
//   - The filter subject rides the CREATE subject, and the server refuses a
//     config whose filter disagrees with it, so a consumer this grant permits
//     can only ever filter this session's own subjects.
//   - No STREAM.INFO, no message get: an info call with a subjects filter
//     enumerates every addressee on the bus, and a get-by-subject reads any
//     subject in the stream. Neither is a subject-scoped operation and the
//     worker needs neither.
//   - No $JS.ACK, no $JS.FC: the worker's consumers are ack-none pulls.
//   - No subscribe on the task subjects at all. Pull deliveries arrive on the
//     reply subject the client names, and this session names its own inbox, so
//     the only subscribe it needs is its own inbox prefix.
//
// What this list is NOT is a statement of everywhere a session's bytes can
// end up. Subject permissions govern the subject a client publishes on, not
// the reply subject it names, and nats-server checks the second one only
// against a short reserved list — so a session can address a JetStream request
// it is granted and have the answer delivered to a subject it is refused.
// TestASessionCanRedirectAJetStreamDeliveryOffItsOwnGrants measures it and
// states the bounds (the delivery keeps its originating subject, so it is a
// redirect and not forgery). It is a property of subject-based permissions, so
// no enumeration here can close it; the shared `worker` credential this
// replaces had it too, over a far wider grant set. Read the list below as what
// a session may ASK FOR, which is what it governs, rather than as reach.
//
// The task id is not part of the claim, so the events grant is per incarnation
// (`<pod>.*.events`) rather than per task id. The gateway spawns one
// incarnation per task, which makes those the same thing today; the gateway
// pins that when it routes a session (gateway.go, the SessionRouted branch,
// which retires the previous incarnation and re-mints rec.BusSession).
func sessionGrants(pod string) Grants {
	inbox := "_INBOX." + pod + ".>"
	g := Grants{
		Publish: []string{
			lib.TaskEventsSubject(pod, "*"),
		},
		Subscribe: []string{inbox},
	}
	consumerAPI := func(op, name string) string {
		return fmt.Sprintf("$JS.API.CONSUMER.%s.%s.%s", op, lib.TasksStream, name)
	}
	for _, role := range lib.SessionConsumerRoles {
		name := lib.SessionConsumerName(pod, role)
		filter := lib.TaskInSubject(pod, "*")
		if role == lib.SessionConsumerEvents {
			filter = lib.TaskEventsSubject(pod, "*")
		}
		g.Publish = append(g.Publish,
			consumerAPI("CREATE", name)+"."+filter,
			consumerAPI("INFO", name),
			consumerAPI("MSG.NEXT", name),
			consumerAPI("DELETE", name),
		)
	}
	g.Publish = append(g.Publish, inbox)
	return g
}

// validSessionName is the check the whole derivation stands on: the pod name
// becomes subject tokens, a consumer name and an inbox prefix, so it must be
// exactly one dot-free DNS-1123 label. Pod names are DNS subdomains and may
// legally contain dots; the gateway mints names that do not, and this refuses
// a pod that was named otherwise rather than issuing grants whose token count
// does not match their intent.
func validSessionName(pod string) error {
	if pod == "" {
		return fmt.Errorf("the token is bound to no pod")
	}
	if !lib.ValidSubjectToken(pod) {
		return fmt.Errorf("pod name %q is not a single subject token (a dot-free DNS-1123 label)", pod)
	}
	return nil
}
