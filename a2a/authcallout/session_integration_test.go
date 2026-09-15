package authcallout

import (
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Claim narrowing, proven the same way everything else in this package is:
// against a real nats-server enforcing a real minted JWT, asserting on client
// outcomes rather than on the grant strings the callout computed. A test that
// compared sessionGrants() to a literal list would pass just as happily if the
// server ignored every one of them.
//
// The property under test is the one gke-labs#1270 turns on. Two session pods
// share one ServiceAccount, so the map cannot tell them apart; the API server's
// pod claim can, and the credential each ends up holding must reach its own
// task and nothing else — not the other session's, and above all not the
// gateway's durable on the shared stream.

const (
	sessionSA = "system:serviceaccount:kubeagents-system:agent-a2a-session"

	podA = "chat-otter-1a2b"
	podB = "chat-badger-9f9f"

	tokenPodA   = "token-for-the-session-sa-bound-to-pod-chat-otter-padded-out-here"
	tokenPodB   = "token-for-the-session-sa-bound-to-pod-chat-badger-padded-out-ok"
	tokenNoPod  = "token-for-the-session-sa-bound-to-no-pod-at-all-padded-out-here"
	tokenDotted = "token-for-a-session-pod-whose-name-carries-dots-padded-out-here"

	// relayDurable duplicates a2a/gateway's unexported constant on purpose:
	// the point of naming it here is that a session must not reach the
	// gateway's consumer, and a test that imported the real name would stop
	// testing the string an attacker would actually guess if the gateway
	// ever renamed it.
	relayDurable = "gateway-relay"
)

// sessionMap is one ordinary entry and one narrowed one. The narrowed entry
// carries no grants at all — that is the shape the operator renders and the
// shape ParseIdentityMap insists on.
const sessionMap = `{
  "version": "session-itest-1",
  "identities": [
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-gateway",
      "user": "gateway",
      "account": "APP",
      "grants": {
        "publish": ["a2a.tasks.>", "$JS.API.>", "_INBOX.gateway.>"],
        "subscribe": ["a2a.tasks.>", "_INBOX.gateway.>"]
      }
    },
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-session",
      "user": "session",
      "account": "APP",
      "narrowing": "pod",
      "grants": {"publish": [], "subscribe": []}
    }
  ]
}`

func sessionTokens() map[string]Attested {
	return map[string]Attested{
		gatewayToken: {ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-gateway"},
		tokenPodA:    {ServiceAccount: sessionSA, PodName: podA, PodUID: "uid-a"},
		tokenPodB:    {ServiceAccount: sessionSA, PodName: podB, PodUID: "uid-b"},
		tokenNoPod:   {ServiceAccount: sessionSA},
		tokenDotted:  {ServiceAccount: sessionSA, PodName: "chat.otter.1a2b", PodUID: "uid-d"},
	}
}

// consumerSubject is the JetStream API subject for one operation on one
// consumer, spelled out here rather than built by the helper the callout uses,
// so that a change to that helper shows up as a failing test.
func consumerSubject(op, name string) string {
	return "$JS.API.CONSUMER." + op + ".TASKS." + name
}

// subscribeRefused reports whether the server refused a subscription. Like a
// refused publish it is asynchronous: Subscribe returns nil and the violation
// arrives on the error handler, which is exactly why an ungranted subscribe
// presents as silence rather than as an error.
func subscribeRefused(t *testing.T, nc *nats.Conn, violations chan error, subject string) bool {
	t.Helper()
	sub, err := nc.SubscribeSync(subject)
	if err != nil {
		t.Fatalf("SubscribeSync(%s) returned a synchronous error: %v", subject, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush after subscribing to %s: %v", subject, err)
	}
	select {
	case e := <-violations:
		if !strings.Contains(e.Error(), "ermissions") {
			t.Fatalf("unexpected async error subscribing to %s: %v", subject, e)
		}
		return true
	case <-time.After(500 * time.Millisecond):
		return false
	}
}

// checkPublish runs a table of subjects against one connection. Draining the
// violation channel between cases keeps a refusal from one subject being read
// as the refusal of the next.
func checkPublish(t *testing.T, nc *nats.Conn, violations chan error, cases map[string]bool) {
	t.Helper()
	for subject, wantRefused := range cases {
		got := publishRefused(t, nc, violations, subject)
		if got != wantRefused {
			verb := map[bool]string{true: "refused", false: "allowed"}
			t.Errorf("publish %s was %s; want %s", subject, verb[got], verb[wantRefused])
		}
	}
}

// The positive half: everything the worker adapter actually does on the bus
// works for the session it belongs to. If this fails the session pod cannot
// run, which is a worse outcome than the hole being open, so it is asserted
// first and in full.
func TestASessionReachesEverythingItsOwnWorkNeeds(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	nc, violations := h.connectAs(t, podA, tokenPodA)

	allowed := map[string]bool{
		// Publishing its own task's events, which is the worker's only
		// output on the task plane.
		lib.TaskEventsSubject(podA, "task-1"): false,

		// Its three named consumers, each operation the adapter uses.
		consumerSubject("CREATE", lib.SessionConsumerName(podA, lib.SessionConsumerOrigin)) + "." + lib.TaskInSubject(podA, "*"):     false,
		consumerSubject("CREATE", lib.SessionConsumerName(podA, lib.SessionConsumerIn)) + "." + lib.TaskInSubject(podA, "*"):         false,
		consumerSubject("CREATE", lib.SessionConsumerName(podA, lib.SessionConsumerEvents)) + "." + lib.TaskEventsSubject(podA, "*"): false,
		consumerSubject("MSG.NEXT", lib.SessionConsumerName(podA, lib.SessionConsumerIn)):                                            false,
		consumerSubject("INFO", lib.SessionConsumerName(podA, lib.SessionConsumerOrigin)):                                            false,
		consumerSubject("DELETE", lib.SessionConsumerName(podA, lib.SessionConsumerEvents)):                                          false,

		// Its own inbox, where pull deliveries and replies land.
		"_INBOX." + podA + ".reply-1": false,
	}
	checkPublish(t, nc, violations, allowed)

	if subscribeRefused(t, nc, violations, "_INBOX."+podA+".>") {
		t.Error("a session may not subscribe to its own inbox; every reply it waits on would time out")
	}
}

// The negative half, and the reason the narrowing exists. Everything here is
// something the shared `worker` credential could do and a session must not.
func TestASessionIsRefusedEverythingBeyondItsOwnTask(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	nc, violations := h.connectAs(t, podA, tokenPodA)

	refused := map[string]bool{
		// The other session's plane, in all three of its shapes.
		lib.TaskEventsSubject(podB, "task-2"):                                             true,
		consumerSubject("MSG.NEXT", lib.SessionConsumerName(podB, lib.SessionConsumerIn)): true,
		consumerSubject("DELETE", lib.SessionConsumerName(podB, lib.SessionConsumerIn)):   true,
		"_INBOX." + podB + ".reply-1":                                                     true,

		// The gateway's durable on the shared stream. A wildcard consumer
		// grant — which is what an ordered consumer would have forced —
		// reaches this, and draining or deleting it takes down the whole
		// task plane for every session at once.
		consumerSubject("MSG.NEXT", relayDurable): true,
		consumerSubject("DELETE", relayDurable):   true,
		consumerSubject("INFO", relayDurable):     true,

		// Stream-level operations whose scope is a body field, not the
		// subject: a get reads any subject in the stream and an info with a
		// subjects filter enumerates every addressee and task id on the bus.
		"$JS.API.STREAM.INFO.TASKS":    true,
		"$JS.API.STREAM.MSG.GET.TASKS": true,
		"$JS.API.DIRECT.GET.TASKS":     true,
		"$JS.API.STREAM.DELETE.TASKS":  true,
		"$JS.API.STREAM.PURGE.TASKS":   true,

		// The blanket the shared worker user holds today.
		"$JS.API.CONSUMER.LIST.TASKS": true,
		"$JS.API.STREAM.LIST":         true,

		// Writing its own input subject. The session reads that subject; the
		// gateway writes it. A session that could write it could forge a
		// steer or a cancel from the user to itself.
		lib.TaskInSubject(podA, "task-1"): true,

		// Planes it has no business on at all.
		"a2a.topics.anything":  true,
		"a2a.agents.some-prof": true,
		"agents.hb.some-agent": true,
	}
	checkPublish(t, nc, violations, refused)

	// The task plane is read through pull consumers delivering into the
	// session's own inbox, so a session needs no subscribe on it — and a
	// session that had one could read every other session's traffic.
	for _, subject := range []string{"a2a.tasks.>", lib.TaskInSubject(podA, "*"), "_INBOX." + podB + ".>", ">"} {
		if !subscribeRefused(t, nc, violations, subject) {
			t.Errorf("subscribe %s was allowed; a session subscribes to nothing but its own inbox", subject)
		}
	}
}

// The consumer name is granted exactly, but the filter rides the CREATE
// subject, so the grant has to pin that too. Without it a session could create
// its own legitimately-named consumer over another session's subjects and pull
// their traffic through a consumer it is fully entitled to read.
func TestASessionCannotFilterItsOwnConsumerOntoAnotherSessionsSubjects(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	nc, violations := h.connectAs(t, podA, tokenPodA)

	own := lib.SessionConsumerName(podA, lib.SessionConsumerIn)
	checkPublish(t, nc, violations, map[string]bool{
		consumerSubject("CREATE", own) + "." + lib.TaskInSubject(podB, "*"):      true,
		consumerSubject("CREATE", own) + "." + lib.TaskEventsSubject(podB, "*"):  true,
		consumerSubject("CREATE", own) + ".a2a.tasks.>":                          true,
		consumerSubject("CREATE", own) + "." + lib.TaskInSubject(podA, "task-1"): false,
	})
}

// Symmetry, so that neither result above is an artifact of which pod the test
// happened to pick.
func TestTwoSessionsOnOneServiceAccountAreRefusedOnEachOthersSubjects(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	a, aViolations := h.connectAs(t, podA, tokenPodA)
	b, bViolations := h.connectAs(t, podB, tokenPodB)

	checkPublish(t, a, aViolations, map[string]bool{
		lib.TaskEventsSubject(podA, "t"): false,
		lib.TaskEventsSubject(podB, "t"): true,
	})
	checkPublish(t, b, bViolations, map[string]bool{
		lib.TaskEventsSubject(podB, "t"): false,
		lib.TaskEventsSubject(podA, "t"): true,
	})
}

// A narrowed entry with no claim to narrow on must refuse. The alternative —
// falling back to the entry's own grants — is a connection with the empty
// grant set, which succeeds at connect and then hangs forever on its first
// reply, and would read in production as "the bus is slow".
func TestASessionTokenBoundToNoPodIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	if nc, err := nats.Connect(h.url, nats.Token(tokenNoPod), nats.CustomInboxPrefix("_INBOX."+podA)); err == nil {
		nc.Close()
		t.Fatal("a session token bound to no pod was accepted")
	}
}

// The whole derivation — subjects, consumer names, inbox prefix — assumes the
// pod name is one subject token. Pod names are DNS subdomains and may legally
// carry dots, and a dotted one would silently shift every token position in
// every grant.
func TestASessionPodNameThatIsNotOneSubjectTokenIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	if nc, err := nats.Connect(h.url, nats.Token(tokenDotted), nats.CustomInboxPrefix("_INBOX.chat")); err == nil {
		nc.Close()
		t.Fatal("a session whose pod name is not a single subject token was accepted")
	}
}

// The identity the server records is the pod's, not the shared entry's. This
// is what makes `connz` and the $SYS advisories able to say which session did
// something; with every session connecting as "session" an incident could not
// be attributed at all.
func TestTheMintedUserIsNamedForThePodNotTheMapEntry(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	nc, _ := h.connectAs(t, podA, tokenPodA)
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	// Username: true is what fills AuthorizedUser in. Without it the field
	// is empty for every connection and this test passes vacuously.
	connz, err := h.server.Connz(&natsserver.ConnzOptions{Username: true})
	if err != nil {
		t.Fatalf("Connz: %v", err)
	}
	var users []string
	for _, c := range connz.Conns {
		users = append(users, c.AuthorizedUser)
	}
	if !containsString(users, podA) {
		t.Errorf("connz authorized users = %v, want one named %q", users, podA)
	}
	if containsString(users, "session") {
		t.Errorf("connz authorized users = %v; a session connected as the shared map entry name", users)
	}
}

func containsString(hay []string, needle string) bool {
	for _, s := range hay {
		if s == needle {
			return true
		}
	}
	return false
}

// sessionGrants is derived, so it must be derived from the pod and nothing
// else. This catches the shape where a refactor threads some ambient session
// name through instead of the attested one.
func TestSessionGrantsMentionNoPodButTheirOwn(t *testing.T) {
	g := sessionGrants(podA)
	for _, subject := range append(append([]string{}, g.Publish...), g.Subscribe...) {
		if strings.Contains(subject, podB) {
			t.Errorf("grant %q mentions another pod", subject)
		}
		if !strings.Contains(subject, podA) {
			t.Errorf("grant %q is not scoped to the pod it was derived from", subject)
		}
	}
	if len(g.Publish) == 0 || len(g.Subscribe) == 0 {
		t.Fatalf("sessionGrants(%q) = %+v, want grants on both sides", podA, g)
	}
}

// A KNOWN LIMIT of the capability model, pinned as a test because it is the
// one place the grant enumeration in session.go cannot be read as closure.
//
// Subject permissions govern the subject a client publishes ON. They do not
// govern the reply subject it names, and nats-server does not check one:
// isReservedReply (server/client.go) refuses service-import replies, $JS.ACK
// and gateway-prefixed replies, and accepts anything else unchecked. So a
// session can address a JetStream request it IS granted — MSG.NEXT on its own
// consumer — and have the server deliver the answer to a subject it is
// refused. Measured here: the payload lands on the gateway's inbox, which the
// same connection is refused a direct publish to two lines above.
//
// What it is worth is much less than "the allowlist does not hold", and the
// bounds are asserted rather than asserted-away:
//
//   - The delivered message keeps its ORIGINAL subject in the Subject field —
//     measured, not assumed: it arrives at the reply subject but reads as
//     a2a.tasks.<attacker>.<task>.events. So a receiver that decides anything
//     from the subject is not fooled, and the attacker cannot make a message
//     look like it came from a subject it cannot publish to. The assertion
//     below pins that, because if it stopped being true this would stop being
//     a redirect and become subject forgery.
//   - The content is a message the session could already read. It is a
//     redirect, not a read primitive: it cannot fetch what its filter subject
//     does not cover.
//   - Reaching a request/reply peer means naming its inbox exactly, and those
//     carry a random NUID token per request.
//
// It is not fixable with subject permissions, so it is not a bug in the
// narrowing: the `worker` credential this replaces had the same property over
// a far wider grant set. It is recorded so the next person to read
// sessionGrants as an exhaustive statement of reach finds this first.
func TestASessionCanRedirectAJetStreamDeliveryOffItsOwnGrants(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())

	gw, _ := h.connectAs(t, "gateway", gatewayToken)
	js, err := gw.JetStream()
	if err != nil {
		t.Fatalf("jetstream from the gateway connection: %v", err)
	}
	if _, err := js.AddStream(&nats.StreamConfig{Name: "TASKS", Subjects: []string{"a2a.tasks.>"}}); err != nil {
		t.Fatalf("AddStream: %v", err)
	}
	const victimSubject = "_INBOX.gateway.reply-42"
	victim, err := gw.SubscribeSync(victimSubject)
	if err != nil {
		t.Fatalf("the victim could not subscribe: %v", err)
	}
	if err := gw.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	nc, violations := h.connectAs(t, podA, tokenPodA)

	// The baseline this test is only interesting against: a direct publish
	// to that subject is refused.
	if !publishRefused(t, nc, violations, victimSubject) {
		t.Fatalf("the session was ALLOWED a direct publish to %s; the grant set has widened and everything below is moot", victimSubject)
	}

	const payload = "written-by-the-session"
	if err := nc.Publish(lib.TaskEventsSubject(podA, "task-1"), []byte(payload)); err != nil {
		t.Fatalf("publishing to its own events subject: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	name := lib.SessionConsumerName(podA, lib.SessionConsumerEvents)
	filter := lib.TaskEventsSubject(podA, "*")
	createBody := `{"stream_name":"TASKS","config":{"name":"` + name +
		`","filter_subject":"` + filter + `","ack_policy":"none","deliver_policy":"all"}}`
	if _, err := nc.Request(consumerSubject("CREATE", name)+"."+filter, []byte(createBody), 3*time.Second); err != nil {
		t.Fatalf("creating its own granted consumer: %v", err)
	}

	// Granted subject, ungranted reply.
	if err := nc.PublishRequest(consumerSubject("MSG.NEXT", name), victimSubject, []byte(`{"batch":1,"no_wait":true}`)); err != nil {
		t.Fatalf("PublishRequest: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	got, err := victim.NextMsg(3 * time.Second)
	if err != nil {
		t.Fatalf("the redirect did not land, so this limit no longer holds as described: %v.\n"+
			"That is good news, but the comment above and the closure discussion in session.go "+
			"were written around it and both need rereading before this test is deleted", err)
	}
	if string(got.Data) != payload {
		t.Errorf("redirected payload = %q, want %q", got.Data, payload)
	}

	// The bound that makes this a redirect rather than subject forgery: the
	// message reads as what it is. It was DELIVERED to the gateway's inbox,
	// which is the escape, but its Subject is still the attacker's own events
	// subject, so nothing downstream can be made to believe the gateway
	// published it or that it came off a subject the session cannot reach.
	// If this ever fails the finding is a great deal worse than recorded.
	if got.Subject != lib.TaskEventsSubject(podA, "task-1") {
		t.Errorf("the redirected delivery reads as subject %q, want the originating %q; "+
			"a receiver can no longer tell a redirected message from one published to it",
			got.Subject, lib.TaskEventsSubject(podA, "task-1"))
	}
}
