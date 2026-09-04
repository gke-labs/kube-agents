package lib

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// TaskInSubject is where a requester publishes message and cancel envelopes
// for a task. The addressee token — the executor's profile or session name,
// added in 0.4 — is the authorization seam: it is what makes connection-time
// grants expressible on the task plane.
func TaskInSubject(addressee, taskID string) string {
	return fmt.Sprintf("a2a.tasks.%s.%s.in", addressee, taskID)
}

// TaskEventsSubject is where the executor publishes status and artifact
// updates for a task.
func TaskEventsSubject(addressee, taskID string) string {
	return fmt.Sprintf("a2a.tasks.%s.%s.events", addressee, taskID)
}

// agentsPrefix is the directory plane: a2a.agents.{profile}.
const agentsPrefix = "a2a.agents."

// AgentSubject carries a profile's agent-card, published by the profile's
// owner when the profile is created (not by workers), and the agent-closed
// tombstone on profile deletion. Chat sessions publish no card.
func AgentSubject(profile string) string {
	return agentsPrefix + profile
}

// ParseTaskSubject splits a task subject into its addressee, taskId, and
// class ("in" or "events"). ok is false for any other subject shape.
func ParseTaskSubject(subject string) (addressee, taskID, class string, ok bool) {
	rest, found := strings.CutPrefix(subject, "a2a.tasks.")
	if !found {
		return "", "", "", false
	}
	parts := strings.Split(rest, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" {
		return "", "", "", false
	}
	if parts[2] != "in" && parts[2] != "events" {
		return "", "", "", false
	}
	return parts[0], parts[1], parts[2], true
}

// dialTimeout bounds one connection attempt's TCP and auth handshake;
// reconnect pacing is the jittered backoff below, not this.
const dialTimeout = 10 * time.Second

// NR-6 backoff shape: full jitter over an exponential ceiling. Dev defaults;
// a restart must spread the herd, not synchronize it.
const (
	backoffBase = 200 * time.Millisecond
	backoffCap  = 5 * time.Second
)

// fullJitterBackoff returns a delay drawn uniformly from [0, min(cap,
// base*2^(attempt-1))) - AWS-style full jitter (NR-6).
func fullJitterBackoff(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	ceil := backoffCap
	if attempt-1 < 30 {
		if d := backoffBase << (attempt - 1); d < backoffCap {
			ceil = d
		}
	}
	return rand.N(ceil)
}

// msgIDHeaderOverhead is the wire cost of the Nats-Msg-Id header beyond the
// id itself: "NATS/1.0\r\n" + "Nats-Msg-Id: " + "\r\n\r\n".
var msgIDHeaderOverhead = len("NATS/1.0\r\n") + len(nats.MsgIdHdr) + len(": ") + len("\r\n\r\n")

// Client is the a2a-jetstream client: validated publish, durable subscribe
// with dedup, and the NR resilience contract on top of nats.go.
type Client struct {
	url  string
	opts clientOptions
	log  *slog.Logger

	mu      sync.RWMutex
	nc      *nats.Conn
	js      jetstream.JetStream
	subs    []*durableSub
	closing atomic.Bool

	// rebuildMu serializes terminal-close rebuilds so a flapping server can
	// never leave a stale rebuild storing a dead connection last.
	rebuildMu sync.Mutex

	// rebuilds counts terminal-close recoveries (NR-2); exposed for tests and
	// health.
	rebuilds atomic.Int64
	// protocolViolations counts surfaced-and-dropped protocol errors (poison
	// envelopes, to/addressee mismatches, post-final events).
	protocolViolations atomic.Int64
}

// ProtocolViolations reports how many protocol errors this client has
// surfaced and dropped - the assertion-10 metric.
func (c *Client) ProtocolViolations() int64 {
	return c.protocolViolations.Load()
}

// ClientOption configures Connect.
type ClientOption func(*clientOptions)

type clientOptions struct {
	name     string
	logger   *slog.Logger
	natsOpts []nats.Option
}

// WithName names the connection for server-side observability.
func WithName(name string) ClientOption {
	return func(o *clientOptions) { o.name = name }
}

// WithLogger routes the connection-event log lines NR-3 requires.
func WithLogger(l *slog.Logger) ClientOption {
	return func(o *clientOptions) { o.logger = l }
}

// WithNATSOptions appends raw nats.go options (reconnect tuning in tests).
// Connection callbacks are owned by the library and cannot be overridden.
func WithNATSOptions(opts ...nats.Option) ClientOption {
	return func(o *clientOptions) { o.natsOpts = append(o.natsOpts, opts...) }
}

// Connect dials NATS and establishes JetStream. All four connection callbacks
// — disconnected, reconnected, closed, error — are registered here and logged
// with the server error that triggered them (NR-3).
func Connect(ctx context.Context, url string, opts ...ClientOption) (*Client, error) {
	c := &Client{url: url}
	for _, opt := range opts {
		opt(&c.opts)
	}
	c.log = c.opts.logger
	if c.log == nil {
		c.log = slog.Default()
	}
	nc, js, err := c.dial()
	if err != nil {
		return nil, err
	}
	c.nc, c.js = nc, js
	return c, nil
}

// dial builds a fresh connection with the callback set. It never touches
// existing connection objects, so the rebuild path (NR-2) can call it against
// a dead predecessor.
func (c *Client) dial() (*nats.Conn, jetstream.JetStream, error) {
	// Order matters: overridable defaults, then the caller's options, then the
	// four connection callbacks — last, so nothing can displace them (NR-3).
	opts := []nats.Option{
		nats.Name(c.opts.name),
		nats.MaxReconnects(-1),
		nats.Timeout(dialTimeout),
	}
	opts = append(opts, c.opts.natsOpts...)
	opts = append(opts,
		// NR-6: jittered backoff on every reconnection attempt, library-owned
		// like the callbacks below.
		nats.CustomReconnectDelay(func(attempts int) time.Duration {
			return fullJitterBackoff(attempts)
		}),
		nats.DisconnectErrHandler(func(_ *nats.Conn, err error) {
			// Transient (NR-1): nats.go reconnects; tear nothing down.
			if c.closing.Load() {
				return
			}
			c.log.Warn("nats disconnected", "err", err)
		}),
		nats.ReconnectHandler(func(nc *nats.Conn) {
			c.log.Info("nats reconnected", "url", nc.ConnectedUrl())
		}),
		nats.ErrorHandler(func(_ *nats.Conn, sub *nats.Subscription, err error) {
			subject := ""
			if sub != nil {
				subject = sub.Subject
			}
			c.log.Error("nats async error", "err", err, "subject", subject)
		}),
		nats.ClosedHandler(func(nc *nats.Conn) {
			if c.closing.Load() {
				c.log.Info("nats connection closed", "reason", "client shutdown")
				return
			}
			// Terminal (NR-1): the connection will never come back on its own.
			c.log.Error("nats connection closed", "err", nc.LastError())
			// Only the client's current connection triggers a rebuild; an
			// abandoned connection dying late must not schedule another one.
			c.mu.RLock()
			current := c.nc == nc
			c.mu.RUnlock()
			if current {
				go c.rebuild()
			}
		}),
	)
	nc, err := nats.Connect(c.url, opts...)
	if err != nil {
		return nil, nil, fmt.Errorf("nats connect: %w", err)
	}
	js, err := jetstream.New(nc)
	if err != nil {
		nc.Close()
		return nil, nil, fmt.Errorf("jetstream: %w", err)
	}
	return nc, js, nil
}

// rebuild is the terminal-close path (NR-2): construct a fresh client
// connection, re-establish JetStream, and re-subscribe every durable from its
// spec. Nothing is retried against objects bound to the dead connection.
// Rebuilds are serialized, and a rebuild does not declare success until every
// durable is re-subscribed and the new connection is still alive — a
// connection that is up while its consumers are dead is the motivating
// incident, not a recovery.
func (c *Client) rebuild() {
	c.rebuildMu.Lock()
	defer c.rebuildMu.Unlock()
	c.log.Warn("nats terminal close: rebuilding connection")
	dialAttempt := 0
	for {
		if c.closing.Load() {
			return
		}
		nc, js, err := c.dial()
		if err != nil {
			dialAttempt++
			c.log.Error("nats rebuild dial failed; retrying", "err", err)
			time.Sleep(fullJitterBackoff(dialAttempt))
			continue
		}
		c.mu.Lock()
		if c.closing.Load() {
			// Close won the race; do not leak the fresh connection.
			c.mu.Unlock()
			nc.Close()
			return
		}
		c.nc, c.js = nc, js
		subs := append([]*durableSub(nil), c.subs...)
		c.mu.Unlock()

		if c.resubscribe(subs, js, nc) {
			c.rebuilds.Add(1)
			c.log.Info("nats rebuild complete")
			return
		}
		// The new connection died or its ClosedHandler fired mid-rebuild;
		// that handler saw itself as current and could not schedule (this
		// rebuild holds the lock), so go around and dial again.
		c.log.Warn("nats rebuild connection died mid-rebuild; dialing again")
	}
}

// resubscribe re-establishes every durable on js, retrying failures — after a
// server restart JetStream stream recovery can lag connection acceptance, and
// abandoning a durable then would leave the process up, TCP green, and the
// consumer silently deaf. Returns false if nc dies before every durable is
// re-subscribed.
func (c *Client) resubscribe(subs []*durableSub, js jetstream.JetStream, nc *nats.Conn) bool {
	pending := subs
	attempt := 0
	for len(pending) > 0 {
		if c.closing.Load() {
			return false
		}
		if nc.IsClosed() {
			return false
		}
		var failed []*durableSub
		for _, s := range pending {
			if s.stopped.Load() {
				continue
			}
			if err := s.start(context.Background(), js); err != nil {
				c.log.Error("nats rebuild re-subscribe failed; will retry", "durable", s.cfg.Durable, "err", err)
				failed = append(failed, s)
			} else {
				c.log.Info("nats rebuild re-subscribed", "durable", s.cfg.Durable)
			}
		}
		if len(failed) == 0 {
			break
		}
		pending = failed
		attempt++
		time.Sleep(fullJitterBackoff(attempt))
	}
	return !nc.IsClosed()
}

// Close shuts the client down deliberately; no rebuild follows.
func (c *Client) Close() {
	c.closing.Store(true)
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, s := range c.subs {
		s.stopLocal()
	}
	c.subs = nil
	if c.nc != nil {
		c.nc.Close()
	}
}

// conn returns the current connection pair under the read lock, so callers
// never race a rebuild.
func (c *Client) conn() (*nats.Conn, jetstream.JetStream) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.nc, c.js
}

// Publish validates env, enforces the server's max message size client-side
// (assertion 8 — the alternative is a silent drop at the server), and
// publishes to subject with the envelopeId as the JetStream dedup id.
func (c *Client) Publish(ctx context.Context, subject string, env *Envelope) error {
	if err := env.ValidateEmit(); err != nil {
		return err
	}
	// 0.4: the envelope's to MUST agree with the subject's addressee token; a
	// mismatch is a protocol error, refused at the source. Task-subject tokens
	// must also be dot-free DNS-1123 labels - dots change the token count.
	if strings.HasPrefix(subject, "a2a.tasks.") {
		addressee, taskID, _, ok := ParseTaskSubject(subject)
		if !ok || !validDNS1123Label(addressee) || !validDNS1123Label(taskID) {
			return &ProtocolError{Msg: fmt.Sprintf("malformed task subject %q: addressee and taskId must be dot-free DNS-1123 labels", subject)}
		}
		if env.To != nil && env.To.Session != addressee {
			return &ProtocolError{Msg: fmt.Sprintf("envelope to %q disagrees with subject addressee %q", env.To.Session, addressee)}
		}
		// Same rule for the taskId: one event published onto another task's
		// subject would fold into the wrong task's history, so the
		// disagreement is refused at the source like the to/addressee one.
		if env.TaskID != taskID {
			return &ProtocolError{Msg: fmt.Sprintf("envelope taskId %q disagrees with subject taskId %q", env.TaskID, taskID)}
		}
	}
	// The topic plane has the same shape of rule: dot-free tokens, and the
	// artifact's name is the topic the subject names (assertion 16).
	if strings.HasPrefix(subject, topicPrefix) {
		if err := checkTopicPublish(subject, env); err != nil {
			return err
		}
	}
	// The directory plane too: one dot-free profile token, carrying only the
	// two kinds the spec puts there - agent-card on create, the agent-closed
	// tombstone on delete. The token grammar is library-enforced on every
	// plane, not left to the caller.
	if profile, ok := strings.CutPrefix(subject, agentsPrefix); ok {
		if !validDNS1123Label(profile) {
			return &ProtocolError{Msg: fmt.Sprintf("malformed agent subject %q: profile must be a dot-free DNS-1123 label", subject)}
		}
		if env.Kind != KindAgentCard && env.Kind != KindAgentClosed {
			return &ProtocolError{Msg: fmt.Sprintf("kind %q on agent subject %q: the directory carries agent-card and agent-closed only", env.Kind, subject)}
		}
	}
	data, err := json.Marshal(env)
	if err != nil {
		return fmt.Errorf("marshal envelope: %w", err)
	}
	nc, js := c.conn()
	// The server counts headers against max message size, so the gate must
	// too, or an envelope inside the header-width window would pass here and
	// fail inside nats.go with a non-A2A error.
	wire := len(data) + msgIDHeaderOverhead + len(env.EnvelopeID)
	if max := nc.MaxPayload(); int64(wire) > max {
		return &A2AError{
			Code:    CodeInvalidParams,
			Message: fmt.Sprintf("envelope is %d bytes on the wire; bus max message size is %d", wire, max),
		}
	}
	_, err = js.Publish(ctx, subject, data, jetstream.WithMsgID(env.EnvelopeID))
	if err != nil {
		return fmt.Errorf("publish %s: %w", subject, err)
	}
	return nil
}

// SubscribeConfig describes a durable subscription.
type SubscribeConfig struct {
	Stream  string
	Subject string
	Durable string
	// Session is this consumer's own session name; envelopes addressed to
	// another session are ignored per assertion 4.
	Session string
}

// Subscription is a live durable subscription.
type Subscription interface {
	Stop()
}

// SubscribeDurable creates or binds the durable consumer and delivers each
// envelope to handler at most once per envelopeId (assertion 5). The
// subscription survives connection rebuilds: its spec, not its JetStream
// objects, is what the client retains.
func (c *Client) SubscribeDurable(ctx context.Context, cfg SubscribeConfig, handler func(*Envelope)) (Subscription, error) {
	switch {
	case cfg.Stream == "":
		return nil, fmt.Errorf("SubscribeConfig.Stream is required")
	case cfg.Subject == "":
		return nil, fmt.Errorf("SubscribeConfig.Subject is required")
	case cfg.Durable == "":
		return nil, fmt.Errorf("SubscribeConfig.Durable is required")
	case cfg.Session == "":
		// Without a session name the to-filter (assertion 4) cannot run, and
		// silently delivering other sessions' envelopes is non-conforming.
		return nil, fmt.Errorf("SubscribeConfig.Session is required")
	}
	s := &durableSub{c: c, cfg: cfg, handler: handler, seen: newDedupSet(dedupWindow)}
	// Register before starting so a rebuild racing this call cannot snapshot
	// c.subs without it and leave its consumer bound to a dead connection.
	c.mu.Lock()
	c.subs = append(c.subs, s)
	js := c.js
	c.mu.Unlock()
	if err := s.start(ctx, js); err != nil {
		s.Stop()
		return nil, err
	}
	return s, nil
}

type durableSub struct {
	c       *Client
	cfg     SubscribeConfig
	handler func(*Envelope)
	seen    *dedupSet
	stopped atomic.Bool

	mu sync.Mutex
	cc jetstream.ConsumeContext
}

// start creates or updates the durable consumer on js and begins consuming.
// Called at subscribe time and again by rebuild with a fresh js; it holds no
// reference to any prior connection's objects.
func (s *durableSub) start(ctx context.Context, js jetstream.JetStream) error {
	cons, err := js.CreateOrUpdateConsumer(ctx, s.cfg.Stream, jetstream.ConsumerConfig{
		Durable:       s.cfg.Durable,
		FilterSubject: s.cfg.Subject,
		AckPolicy:     jetstream.AckExplicitPolicy,
	})
	if err != nil {
		return fmt.Errorf("consumer %s on %s: %w", s.cfg.Durable, s.cfg.Stream, err)
	}
	cc, err := cons.Consume(s.deliver)
	if err != nil {
		return fmt.Errorf("consume %s: %w", s.cfg.Durable, err)
	}
	s.mu.Lock()
	old := s.cc
	s.cc = cc
	s.mu.Unlock()
	if old != nil {
		// Usually already dead with its connection; stopping is a no-op then,
		// and prevents double delivery if a start ever displaces a live one.
		old.Stop()
	}
	return nil
}

func (s *durableSub) deliver(msg jetstream.Msg) {
	env, err := ParseEnvelope(msg.Data())
	if err != nil {
		// Poison messages are surfaced and terminated, not redelivered forever.
		s.c.protocolViolations.Add(1)
		s.c.log.Error("a2a envelope rejected", "subject", msg.Subject(), "err", err)
		_ = msg.Term()
		return
	}
	// Assertion 4 (0.4 clause): an envelope whose to disagrees with its
	// subject's addressee token is a protocol error — surfaced and terminated
	// like any poison, never passed through to the application.
	if addressee, _, _, ok := ParseTaskSubject(msg.Subject()); ok && env.To != nil && env.To.Session != addressee {
		s.c.protocolViolations.Add(1)
		s.c.log.Error("a2a envelope rejected", "subject", msg.Subject(),
			"err", fmt.Sprintf("to %q disagrees with subject addressee %q", env.To.Session, addressee))
		_ = msg.Term()
		return
	}
	// Assertion 4: a wildcard consumer ignores envelopes addressed elsewhere.
	if env.To != nil && env.To.Session != s.cfg.Session {
		_ = msg.Ack()
		return
	}
	// Assertion 5: at most once per envelopeId across redeliveries.
	if !s.seen.add(env.EnvelopeID) {
		_ = msg.Ack()
		return
	}
	s.handler(env)
	_ = msg.Ack()
}

// Stop ends the subscription and removes it from the rebuild registry.
func (s *durableSub) Stop() {
	s.stopLocal()
	s.c.mu.Lock()
	defer s.c.mu.Unlock()
	for i, sub := range s.c.subs {
		if sub == s {
			s.c.subs = append(s.c.subs[:i], s.c.subs[i+1:]...)
			break
		}
	}
}

func (s *durableSub) stopLocal() {
	s.stopped.Store(true)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cc != nil {
		s.cc.Stop()
	}
}
