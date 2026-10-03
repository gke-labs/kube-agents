package capability

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// The verifier as a service, on a real bus. What is under test here is the
// half the rules cannot cover: where the caller's identity comes from, and what
// the service does with the parts of a request the caller controls.
//
// The other half — that the *server* refuses a principal publishing on another
// principal's verify subject — is not testable here, because this server has no
// permissions configured. It is in authcallout, against the operator's rendered
// nats.conf, which is the only place the assertion means anything. 09 §9 opens
// by warning about exactly the test this file would otherwise become.

func runServer(t *testing.T) *nats.Conn {
	t.Helper()
	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("nats server: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(5 * time.Second) {
		t.Fatal("nats server did not come up")
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	t.Cleanup(nc.Close)
	return nc
}

func serveVerifier(t *testing.T, s Store) *nats.Conn {
	t.Helper()
	nc := runServer(t)
	svc := &Service{Resolver: &Resolver{Store: s}}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	sub, err := svc.Subscribe(ctx, nc)
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
	return nc
}

// rawAskBytes asks the way a broker does — on the caller's subject, with a
// reply inbox inside the caller's own namespace — but with a payload the
// Request type could not produce. That is the point: the tests that use it are
// about what a caller can put in the parts of a request it controls.
func rawAskBytes(t *testing.T, nc *nats.Conn, caller string, body []byte) []byte {
	t.Helper()
	reply := ReplyPrefix + caller + ".probe"
	sub, err := nc.SubscribeSync(reply)
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	subj, err := VerifySubject(caller)
	if err != nil {
		t.Fatalf("subject: %v", err)
	}
	if err := nc.PublishRequest(subj, reply, body); err != nil {
		t.Fatalf("publish: %v", err)
	}
	m, err := sub.NextMsg(2 * time.Second)
	if err != nil {
		t.Fatalf("no answer: %v", err)
	}
	return m.Data
}

func rawAsk(t *testing.T, nc *nats.Conn, caller string, body []byte) Response {
	t.Helper()
	var resp Response
	if err := json.Unmarshal(rawAskBytes(t, nc, caller, body), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return resp
}

func clientAs(t *testing.T, nc *nats.Conn, self string) *Client {
	t.Helper()
	// Note what this does NOT do: it does not authenticate. On this server
	// any connection can publish on any subject, so `self` here is the
	// caller asserting who it is. In the deployment the server is what
	// makes that assertion true, and authcallout is where that is proven.
	c, err := NewClient(nc, self)
	if err != nil {
		t.Fatalf("client: %v", err)
	}
	c.Timeout = 2 * time.Second
	return c
}

func TestTheServiceAnswersTheDelegateAndRefusesEveryoneElse(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)

	if err := clientAs(t, nc, podB).Check(context.Background(), hop,
		VerbTaskExecute, "project/P/cluster/C"); err != nil {
		t.Fatalf("the named delegate should be permitted: %v", err)
	}
	// Over the wire the rule that fired is not disclosed: every walk
	// refusal is the same sentence, so a caller cannot learn from the
	// verifier whether the key it named exists. The rule itself is asserted
	// against the Resolver in attack_test.go, and logged by the service.
	err := clientAs(t, nc, podEvil).Check(context.Background(), hop,
		VerbTaskExecute, "project/P/cluster/C")
	mustRefuse(t, err, WalkRefused, podEvil, podB, hop.Key)
}

func TestTheCallerIdentityComesFromTheSubjectAndNotFromThePayload(t *testing.T) {
	// The whole mechanism in one test. podEvil asks on its own subject and
	// writes podB's name into every field of the payload it can reach. The
	// payload is the part a caller controls; the subject is the part the
	// server does.
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)

	body, err := json.Marshal(map[string]any{
		"ref": hop, "verb": VerbTaskExecute, "resource": "project/P/cluster/C",
		// Fields the wire format does not have. If a future Request grew
		// a caller field, this test is what notices.
		"caller": podB, "delegate": podB, "principal": podB, "self": podB,
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	resp := rawAsk(t, nc, podEvil, body)
	if resp.Allowed {
		t.Fatal("the payload named podB as the caller and the verifier believed it")
	}
	if resp.Reason != WalkRefused {
		t.Fatalf("refused for the wrong reason: %s", resp.Reason)
	}

	// The positive half, and it is what makes the negative half mean
	// something. Byte-identical payload, asked from podB's subject: now it
	// is allowed. The only thing that changed is the part the server owns.
	if allowed := rawAsk(t, nc, podB, body); !allowed.Allowed {
		t.Fatalf("the same payload from the real delegate's subject was refused: %s", allowed.Reason)
	}
}

func TestARequestOffTheCallerScopedNamespaceIsRefused(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)
	body, _ := json.Marshal(Request{Ref: hop, Verb: VerbTaskExecute, Resource: "project/P/cluster/C"})

	// Two ways in, and they fail differently on purpose.
	//
	// On the bus, a subject that is not exactly one token past the prefix
	// does not match `a2a.cap.verify.*` and is never delivered. Assert the
	// silence rather than a verdict: a test that expected a refusal here
	// would pass for the wrong reason.
	for _, subj := range []string{
		strings.TrimSuffix(VerifyPrefix, "."),
		VerifyPrefix + podB + ".extra",
		"a2a.cap.verify",
	} {
		sub, err := nc.SubscribeSync(ReplyPrefix + "probe.>")
		if err != nil {
			t.Fatal(err)
		}
		if err := nc.PublishRequest(subj, ReplyPrefix+"probe.1", body); err != nil {
			t.Fatal(err)
		}
		_ = nc.Flush()
		if m, err := sub.NextMsg(300 * time.Millisecond); err == nil {
			t.Fatalf("%s was answered at all: %s", subj, m.Data)
		}
		_ = sub.Unsubscribe()
	}

	// And directly, because the subscription is not the guard. A verifier
	// that grew a second subscription — a wildcard for metrics, a `>` for
	// debugging — would hand Answer a subject with no caller in it, and
	// Answer must not fall back to answering.
	svc := &Service{Resolver: &Resolver{Store: s}}
	for _, subj := range []string{
		"a2a.cap.verify", VerifyPrefix, VerifyPrefix + podB + ".extra", "somewhere.else",
	} {
		if got := svc.Answer(context.Background(), subj, body); got.Allowed {
			t.Errorf("Answer granted on subject %q", subj)
		} else if !strings.Contains(got.Reason, "caller-scoped verify subject") {
			t.Errorf("subject %q refused for the wrong reason: %s", subj, got.Reason)
		}
	}
}

func TestTheVerifierWillNotAnswerIntoAnotherPrincipalsInbox(t *testing.T) {
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)
	body, _ := json.Marshal(Request{Ref: hop, Verb: VerbTaskExecute, Resource: "project/P/cluster/C"})

	victim, err := nc.SubscribeSync(ReplyPrefix + podB + ".>")
	if err != nil {
		t.Fatal(err)
	}
	subj, _ := VerifySubject(podEvil)
	if err := nc.PublishRequest(subj, ReplyPrefix+podB+".stolen", body); err != nil {
		t.Fatal(err)
	}
	_ = nc.Flush()
	if m, err := victim.NextMsg(500 * time.Millisecond); err == nil {
		t.Fatalf("the verifier answered into another principal's inbox: %s", m.Data)
	}
}

func TestABrokerThatCannotReachTheVerifierIsRefusedRatherThanAllowed(t *testing.T) {
	// The verifier is on the request path. A broker that treated an
	// unreachable verifier as a pass would turn every verifier outage into
	// an authorization bypass, which is the opposite of the failure this
	// design is willing to accept: 09's cost is "if it is down nothing
	// authorizes", and nothing authorizing has to mean nothing proceeding.
	nc := runServer(t) // no verifier subscribed
	c := clientAs(t, nc, podB)
	c.Timeout = 200 * time.Millisecond
	err := c.Check(context.Background(), Ref{Key: "root.task-x", Revision: 1},
		VerbTaskExecute, "project/P")
	mustRefuse(t, err, "the verifier could not be reached")
}

func TestTheVerdictCarriesNoPartOfTheCapability(t *testing.T) {
	// The broker asked whether a verb is permitted. 09 §4 withholds the
	// store's contents from every broker, so the answer must not smuggle
	// them back: no tier, no scope, no delegate, no key.
	s, _, hop := twoHop(t)
	nc := serveVerifier(t, s)
	body, _ := json.Marshal(Request{Ref: hop, Verb: VerbTaskExecute, Resource: "project/P/cluster/C"})
	data := rawAskBytes(t, nc, podB, body)
	var raw map[string]any
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, forbidden := range []string{"tier", "scope", "delegate", "key", "revision", "parent"} {
		if _, ok := raw[forbidden]; ok {
			t.Errorf("the verdict carries %q", forbidden)
		}
	}
	for _, leak := range []string{string(TierClusterAdmin), "project/P", podB, hop.Key} {
		if strings.Contains(string(data), leak) {
			t.Errorf("the verdict leaks %q: %s", leak, data)
		}
	}
}
