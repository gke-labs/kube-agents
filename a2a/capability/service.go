package capability

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"
)

// The verification service, and the client brokers reach it with.
//
// 09 §4 makes the verifier the only component holding read across the bucket,
// which is why it is its own workload rather than a library every broker links.
// The rule it enforces that nothing else can — "the verifier authenticates its
// caller" — needs an answer to a question NATS does not answer on its own: a
// message does not carry its publisher.
//
// The answer is the subject, and it is the mechanism A3 already shipped for the
// task plane. A caller reaches the verifier at `a2a.cap.verify.<its-own-name>`,
// and the server refuses any principal publishing on a token that is not its
// own, with the permissions it attached when that principal authenticated. So
// the last token of the subject the request arrived on IS the authenticated
// caller — not a claim in the payload, which the caller writes and could write
// anything into. This is why A3b is sequential after A3 and not parallel with
// it: without subject-derived identity there is nothing here to authenticate
// against.
const (
	// VerifyPrefix is the request namespace. One token follows: the caller.
	VerifyPrefix = "a2a.cap.verify."
	// VerifySubscribe is what the verifier listens on.
	VerifySubscribe = VerifyPrefix + "*"
	// VerifyQueue lets the verifier scale horizontally without duplicating
	// answers.
	VerifyQueue = "cap-verifier"

	// ReplyPrefix is the answer namespace, and it is deliberately NOT
	// _INBOX.
	//
	// The verifier answers wherever the caller says, and the caller set is
	// every broker on the bus, so its publish grant cannot be narrowed to
	// one inbox. Under _INBOX that grant would be `_INBOX.>`: the verifier
	// could publish into the gateway's inbox, which is where the gateway
	// reads its JetStream API replies. A component whose whole job is to
	// answer questions would have been able to forge a stream-create
	// acknowledgement to the component that creates streams.
	//
	// A namespace of its own costs one subscribe grant on each caller and
	// buys the verifier a publish grant — `a2a.cap.reply.>` — that reaches
	// nothing but capability answers.
	//
	// Living under `a2a.` rather than `_INBOX` has a consequence that an
	// earlier revision of this comment got wrong, and the way it was wrong
	// is worth keeping. `a2a.>` is a real subscribe grant on this bus — the
	// read-only `web` user, the one credential published to a browser,
	// holds it — and that grant covers both subjects above. The comment
	// used to reason about what a browser could OBSERVE there (caller, key,
	// revision, verb, verdict; never a capability, since neither message
	// carries one) and accept it as a subset of what `web` already reads
	// off `a2a.tasks.>`.
	//
	// That is true and it is not the risk. NATS lets any principal
	// permitted to subscribe to a subject join any QUEUE GROUP on it, so
	// `web` could join `cap-verifier` and take a share of the verify
	// requests — not to answer them, which it cannot (no publish under
	// `a2a.cap.reply.>`), but to swallow them. Check reads the resulting
	// timeout as a denial, by design, so a browser credential would have
	// rejected a proportion of every task on the bus. A subscriber on a
	// request subject intercepts; it does not merely watch.
	//
	// So `web` is denied `a2a.cap.>` outright (webIdentity, in
	// k8s-operator/internal/controller/platformagent_a2a_identities.go).
	// The general rule this leaves behind: any future grant wide enough to
	// cover VerifySubscribe has to be checked against queue-group theft,
	// not just against disclosure.
	ReplyPrefix = "a2a.cap.reply."
	// ReplyPublish is the verifier's whole publish grant on this path.
	ReplyPublish = ReplyPrefix + ">"
)

// ReplySubject is where one request's answer goes. The caller's own name is
// the second token, so the verifier can check that a caller is not asking to
// be answered into somebody else's namespace, and so each caller subscribes to
// its own answers and no one else's.
func ReplySubject(caller string) (string, error) {
	if err := checkToken("caller", caller); err != nil {
		return "", err
	}
	return ReplyPrefix + caller + "." + nuid.Next(), nil
}

// ReplySubscribe is the one subscribe grant a broker needs to hear answers.
func ReplySubscribe(caller string) (string, error) {
	if err := checkToken("caller", caller); err != nil {
		return "", err
	}
	return ReplyPrefix + caller + ".>", nil
}

// VerifySubject is where a caller asks. The caller's own name is the subject's
// last token, and the server is what makes that true.
func VerifySubject(caller string) (string, error) {
	if err := checkToken("caller", caller); err != nil {
		return "", err
	}
	return VerifyPrefix + caller, nil
}

// callerFromSubject reads the authenticated caller off the request subject.
func callerFromSubject(subject string) (string, error) {
	rest, ok := strings.CutPrefix(subject, VerifyPrefix)
	if !ok || rest == "" || strings.Contains(rest, ".") {
		return "", refuse("the request did not arrive on a caller-scoped verify subject")
	}
	return rest, nil
}

// Request is what a broker asks. It carries no identity: the subject carries
// that, and a payload field would be one the caller writes.
type Request struct {
	Ref      Ref   `json:"ref"`
	Verb     Verb  `json:"verb"`
	Resource Scope `json:"resource"`
}

// Response is a verdict and the rule behind it. It deliberately does NOT carry
// the resolved tier or scope: the broker asked whether a verb is permitted, and
// answering with the capability itself would hand the store's contents to the
// one party 09 §4 withholds them from.
type Response struct {
	Allowed bool   `json:"allowed"`
	Reason  string `json:"reason,omitempty"`
}

// Service answers verification requests. It holds the only Store in the
// deployment.
type Service struct {
	Resolver *Resolver
	Log      *slog.Logger
}

// Answer applies the rules to one request. Split out from the subscription so
// the rules can be tested without a bus.
func (s *Service) Answer(ctx context.Context, subject string, data []byte) Response {
	caller, err := callerFromSubject(subject)
	if err != nil {
		return Response{Reason: Reason(err)}
	}
	var req Request
	if err := json.Unmarshal(data, &req); err != nil {
		return Response{Reason: "the request is not well-formed"}
	}
	// The walk and the verb check are answered differently on purpose, and
	// the split is the whole anti-oracle argument.
	//
	// Every way the WALK can fail tells the caller something about a chain
	// it has not shown any right to: "no entry at the pinned revision" says
	// the key is not there, "the entry does not name the caller as its
	// delegate" says it is. Request ids are not secrets, but a verifier that
	// answers "does this one exist" to anyone who can name one is a lookup
	// service for live requests. So every walk refusal is one sentence, and
	// the rule that actually fired goes to the verifier's own log.
	//
	// The VERB check is the other side of that line. Reaching it means the
	// walk succeeded, which means this caller is the principal the chain
	// names — it already knows the capability exists, because it holds it.
	// Telling it which verb or which scope was out of bounds gives away
	// nothing it did not bring, and it is the difference between a
	// debuggable refusal and a mystery.
	leaf, err := s.Resolver.Resolve(ctx, caller, req.Ref)
	switch {
	case err == nil:
	case errors.Is(err, ErrRefused):
		if s.Log != nil {
			s.Log.Info("capability walk refused",
				"caller", caller, "key", clip(req.Ref.Key), "revision", req.Ref.Revision,
				"rule", Reason(err))
		}
		return Response{Reason: WalkRefused}
	default:
		// Store trouble is not a denial, but it is answered as one. The
		// operator's signal is the log line; the caller's is a verdict.
		if s.Log != nil {
			s.Log.Error("capability store unavailable; failing closed", "err", err)
		}
		return Response{Reason: WalkRefused}
	}
	if err := Permits(leaf, req.Verb, req.Resource); err != nil {
		return Response{Reason: Reason(err)}
	}
	return Response{Allowed: true}
}

// WalkRefused is the single answer every chain-walk refusal gets. One string
// for the whole class, so a caller cannot tell a key that is not there from
// one that is not its own.
const WalkRefused = "the capability does not authorize this caller"

// clip bounds a caller-supplied value on its way into the verifier's log. The
// key is chosen by whoever sent the request and the log is not.
func clip(s string) string {
	const max = 96
	if len(s) <= max {
		return s
	}
	return s[:max] + "…"
}

// Subscribe starts answering and returns as soon as the subscription is
// established on the server. It exists in this shape so a caller can know the
// verifier is listening before it lets anything ask: a request that races the
// subscription is answered by the client's timeout, and the client reads a
// timeout as a denial. Pair it with DrainAndCancel at shutdown; the context
// handed here is the one that ordering is about.
func (s *Service) Subscribe(ctx context.Context, nc *nats.Conn) (*nats.Subscription, error) {
	sub, err := nc.QueueSubscribe(VerifySubscribe, VerifyQueue, func(m *nats.Msg) {
		s.handle(ctx, m)
	})
	if err != nil {
		return nil, err
	}
	if err := nc.Flush(); err != nil {
		_ = sub.Unsubscribe()
		return nil, err
	}
	return sub, nil
}

// DrainAndCancel is the verifier's shutdown, and it lives here rather than
// inline in cmd/verifier so the ordering is pinned by a test on the thing that
// ships instead of by a test that restates it.
//
// Two orderings, one of which is silently wrong. Every path into the resolver
// takes a context; cancel the handlers' context before the drain and each
// request still queued is answered through Answer's fail-closed branch --
// `allowed: false`, which a broker reads as the capability being bad and uses
// to reject a task nothing was wrong with. "This process is going away" and
// "the store is unreachable" arrive as the same error, and the fail-closed
// branch cannot tell them apart. So the drain has to outlive the signal, and
// handlerCancel runs after it, never before.
//
// The whole connection drains, not one subscription: sub.Drain() returns as
// soon as the drain is SCHEDULED, so it races the caller's nc.Close() and the
// answers it exists to deliver go out over a severed connection or not at all.
// nc.Drain() walks the subscriptions, flushes, then closes, and the closed
// handler is how a caller learns it finished. Waiting for that is the point.
//
// A Drain() error is logged and the wait still runs. Returning there would
// cancel the handlers immediately -- the exact defect above, in the one path
// where the drain has already gone wrong and in-flight answers most need the
// time.
func DrainAndCancel(log *slog.Logger, nc *nats.Conn, handlerCancel context.CancelFunc, timeout time.Duration) {
	drained := make(chan struct{})
	nc.SetClosedHandler(func(*nats.Conn) { close(drained) })
	if err := nc.Drain(); err != nil && log != nil {
		log.Warn("drain", "err", err)
	}
	select {
	case <-drained:
	case <-time.After(timeout):
		if log != nil {
			log.Warn("drain did not finish within the deadline; in-flight requests may be unanswered",
				"timeout", timeout)
		}
	}
	// Only now: a handler still writing its reply needs its context live.
	handlerCancel()
}

func (s *Service) handle(ctx context.Context, m *nats.Msg) {
	caller, cerr := callerFromSubject(m.Subject)
	// The reply subject is chosen by the caller, and the verifier's publish
	// grant covers the whole reply namespace. Answering wherever asked
	// would let one broker have the verifier deliver into another's reply
	// namespace — a forged verdict, from the one component every broker
	// believes. session.go documents the same shape on the JetStream path;
	// this is the one place on the capability path where it would apply, so
	// it is closed here rather than inherited.
	if cerr == nil && !strings.HasPrefix(m.Reply, ReplyPrefix+caller+".") {
		if s.Log != nil {
			s.Log.Warn("verify request asked for a reply outside the caller's inbox; dropped",
				"caller", caller)
		}
		return
	}
	resp := s.Answer(ctx, m.Subject, m.Data)
	b, err := json.Marshal(resp)
	if err != nil {
		return
	}
	if err := m.Respond(b); err != nil && s.Log != nil {
		s.Log.Warn("verify reply failed", "err", err)
	}
}

// DefaultTimeout bounds a broker's wait. The verifier is on the request path,
// so a broker that waits forever turns a verifier outage into a wedged task
// rather than a refused one.
const DefaultTimeout = 5 * time.Second

// Client is the broker side. It knows its own name because the operator told
// the pod, and it cannot lie about it: it can only publish on its own subject.
type Client struct {
	nc      *nats.Conn
	self    string
	subject string
	Timeout time.Duration
}

// NewClient builds the broker's verifier client. self is the principal's own
// name — the session pod name for a session, which is also the name the
// gateway wrote into the capability's delegate field.
func NewClient(nc *nats.Conn, self string) (*Client, error) {
	subj, err := VerifySubject(self)
	if err != nil {
		return nil, err
	}
	return &Client{nc: nc, self: self, subject: subj}, nil
}

// Check asks whether the capability at ref permits verb v on resource r.
// A nil error means permitted. Everything else — refused, unreachable,
// malformed — is a denial, because the alternative is a broker that proceeds
// when it could not find out.
func (c *Client) Check(ctx context.Context, ref Ref, v Verb, r Scope) error {
	b, err := json.Marshal(Request{Ref: ref, Verb: v, Resource: r})
	if err != nil {
		return err
	}
	timeout := c.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	// The reply subject is spelled here rather than taken from the
	// connection's inbox prefix. Answers ride their own namespace, not
	// _INBOX (see ReplyPrefix), so nats.Request would ask from a subject
	// the verifier drops — and the client would read the drop as the
	// verifier being down, a fail-closed bug that would take a deployment
	// out silently.
	reply, err := ReplySubject(c.self)
	if err != nil {
		return err
	}
	sub, err := c.nc.SubscribeSync(reply)
	if err != nil {
		return refuse("the verifier could not be reached")
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := c.nc.PublishRequest(c.subject, reply, b); err != nil {
		return refuse("the verifier could not be reached")
	}
	msg, err := sub.NextMsgWithContext(ctx)
	if err != nil {
		return refuse("the verifier could not be reached")
	}
	var resp Response
	if err := json.Unmarshal(msg.Data, &resp); err != nil {
		return refuse("the verifier's answer was not well-formed")
	}
	if !resp.Allowed {
		return &Refusal{Rule: resp.Reason}
	}
	return nil
}
