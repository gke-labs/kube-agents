package mcp

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// streamDirectory is the agent-card stream the operator provisions (its
	// subjects are lib.AgentSubject's). lib names the others.
	streamDirectory = "DIRECTORY"
	// directorySubjects is every agent's card subject.
	directorySubjects = "a2a.agents.>"

	// consumerBridgePlatform is the platform bridge's durable on TASKS
	// (a2a/hermes-bridge). The gateway's is gatewayRelayDurable.
	consumerBridgePlatform = "bridge-platform"
	gatewayRelayDurable    = "gateway-relay"

	// statusTimeout bounds one status read end to end, whatever the caller's
	// context allows: a stalled bus answers with the parts it read and a
	// problem naming the rest, not with a hung request.
	statusTimeout = 8 * time.Second
	// statusCacheTTL is how long one read answers every call. A caller that
	// polls (an IDE agent checking every few seconds) then costs the bus one
	// read, and one short-lived consumer, per window rather than per call.
	statusCacheTTL = 5 * time.Second

	// The DIRECTORY read: one plain pull consumer per read, with no acks, in
	// memory, which the server deletes itself once it has been idle for
	// directoryConsumerIdle. Not an ordered consumer: that one replaces its
	// consumer on its first fetch and deletes the old one, a delete the
	// console grant refuses, so every read would leave two behind against
	// DIRECTORY's consumer cap. directoryFetchWait bounds each fetch.
	directoryConsumerIdle = 5 * time.Second
	directoryFetchWait    = 2 * time.Second
	minFetchWait          = 100 * time.Millisecond
	directoryFetchBatch   = 256
	// maxAgentCards bounds what one read keeps; DIRECTORY holds one card per
	// profile, so more than this is a directory gone wrong, and is reported.
	maxAgentCards = 512
)

// statusStreams are the streams the console principal may read the info of,
// in the order the status shows them.
var statusStreams = []string{lib.TasksStream, streamDirectory, lib.StreamTopicsState, lib.StreamTopicsJournal}

// statusConsumers are the durables whose liveness says whether the install is
// answering: the gateway's relay and the platform bridge, both on TASKS.
var statusConsumers = []string{gatewayRelayDurable, consumerBridgePlatform}

// Deployment names the one install this server is a client of. It is fixed at
// start: a caller cannot point the server at another bus.
type Deployment struct {
	Agent     string `json:"agent"`
	Namespace string `json:"namespace"`
	Cluster   string `json:"cluster,omitempty"`
}

// BusConfig is how the server reaches the bus. The TLS trust comes from the
// environment lib reads (NATS_CA_FILE), as for every bus client.
type BusConfig struct {
	URL      string
	User     string
	Password string
	// PasswordFile, when set, is re-read on every connect and reconnect, so
	// a password rotated in the mounted Secret reaches the connection, as
	// the operator mounts it for (a directory, never a subPath). Password is
	// then the first value read, kept if a later read fails mid-rotation.
	PasswordFile string
	Deployment   Deployment
	Logger       *slog.Logger
}

// Bus is what the status tool needs from the bus, so the tool can be tested
// without one.
type Bus interface {
	Status(ctx context.Context) BusStatus
}

// NATSBus is the server's one bus connection, as the console principal.
type NATSBus struct {
	client *lib.Client
	cfg    BusConfig

	// mu guards the cached answer and the read in flight, which concurrent
	// callers share rather than each opening a consumer.
	mu       sync.Mutex
	cached   BusStatus
	cachedAt time.Time
	inflight *statusRead
	now      func() time.Time
	// readBus is one read of the bus (read, but for tests).
	readBus func(context.Context) BusStatus

	// readers holds one token per consumer this server may still hold
	// (maxTaskReaders); reapDelay is how long a released slot stays taken.
	readers   chan struct{}
	reapDelay time.Duration
}

// statusRead is one status read in flight: st is its answer once done is
// closed.
type statusRead struct {
	done chan struct{}
	st   BusStatus
}

// DialBus connects to the bus once. Its caller retries a bus that is not
// reachable yet, and gives up at once on a dial the bus refused for good
// (DialRefused); after the first connect, lib reconnects on its own.
func DialBus(ctx context.Context, cfg BusConfig) (*NATSBus, error) {
	if cfg.URL == "" || cfg.User == "" || cfg.Password == "" {
		return nil, errors.New("the bus needs a URL, a user and a password")
	}
	opts := []lib.ClientOption{lib.WithUserPassword(cfg.User, cfg.Password)}
	if cfg.PasswordFile != "" {
		// Instead of the static pair (nats refuses both): the callback is
		// asked on every connect. The inbox prefix is the one
		// lib.WithUserPassword would set, which the console grant requires.
		opts = []lib.ClientOption{lib.WithNATSOptions(
			nats.UserInfoHandler(passwordFromFile(cfg.User, cfg.PasswordFile, cfg.Password)),
			nats.CustomInboxPrefix("_INBOX."+cfg.User),
		)}
	}
	if cfg.Logger != nil {
		opts = append(opts, lib.WithLogger(cfg.Logger))
	}
	client, err := lib.Connect(ctx, cfg.URL, opts...)
	if err != nil {
		return nil, fmt.Errorf("connect to the bus at %s as %s: %w", cfg.URL, cfg.User, err)
	}
	b := &NATSBus{client: client, cfg: cfg, now: time.Now, readers: make(chan struct{}, maxTaskReaders), reapDelay: taskConsumerIdle}
	b.readBus = b.read
	return b, nil
}

// Close drops the connection.
func (b *NATSBus) Close() { b.client.Close() }

// Status reports the bus's state, read at most statusCacheTTL ago. Every
// part that fails is named in Problems rather than failing the whole report,
// so a half-working bus still tells the caller which half.
//
// The read runs detached from its caller, bounded by statusTimeout alone,
// and is cached when it ends: a caller that gives up (a client that
// disconnected) is answered at once with that, and the read it started still
// answers the next caller rather than leaving it to read DIRECTORY again.
func (b *NATSBus) Status(ctx context.Context) BusStatus {
	b.mu.Lock()
	if !b.cachedAt.IsZero() && b.now().Sub(b.cachedAt) < statusCacheTTL {
		defer b.mu.Unlock()
		return b.cached
	}
	sr := b.inflight
	if sr == nil {
		sr = &statusRead{done: make(chan struct{})}
		b.inflight = sr
		go b.refresh(sr)
	}
	b.mu.Unlock()
	select {
	case <-sr.done:
		return sr.st
	case <-ctx.Done():
		return BusStatus{
			Deployment: b.cfg.Deployment,
			URL:        b.cfg.URL,
			User:       b.cfg.User,
			Problems:   []string{fmt.Sprintf("the request ended while the status read was still running: %v", ctx.Err())},
		}
	}
}

// refresh runs one read for sr and caches its answer.
func (b *NATSBus) refresh(sr *statusRead) {
	rctx, cancel := context.WithTimeout(context.Background(), statusTimeout)
	defer cancel()
	// The read runs outside net/http's per-request recovery, so a panic in
	// it is turned into a problem here; either way the read is stored and
	// its waiters released, or every later caller would wait on it.
	defer func() {
		if p := recover(); p != nil {
			sr.st = BusStatus{Deployment: b.cfg.Deployment, URL: b.cfg.URL, User: b.cfg.User,
				Problems: []string{fmt.Sprintf("the status read failed: %v", p)}}
		}
		b.mu.Lock()
		b.cached, b.cachedAt = sr.st, b.now()
		b.inflight = nil
		b.mu.Unlock()
		close(sr.done)
	}()
	sr.st = b.readBus(rctx)
}

func (b *NATSBus) read(ctx context.Context) BusStatus {
	st := BusStatus{
		Deployment: b.cfg.Deployment,
		URL:        b.cfg.URL,
		User:       b.cfg.User,
		Connected:  b.client.Conn().IsConnected(),
	}
	if !st.Connected {
		st.Problems = append(st.Problems, "not connected to the bus; the client is reconnecting")
		return st
	}
	if cs, err := b.client.Conn().TLSConnectionState(); err == nil {
		st.TLSVersion = tlsVersionName(cs.Version)
	}
	js := b.client.JetStream()
	for _, name := range statusStreams {
		s, err := js.Stream(ctx, name)
		if err != nil {
			st.Problems = append(st.Problems, fmt.Sprintf("stream %s: %v", name, err))
			continue
		}
		info := s.CachedInfo()
		st.Streams = append(st.Streams, StreamHealth{
			Name:      name,
			Messages:  info.State.Msgs,
			Bytes:     info.State.Bytes,
			MaxBytes:  info.Config.MaxBytes,
			Consumers: info.State.Consumers,
		})
		if name != lib.TasksStream {
			continue
		}
		for _, cname := range statusConsumers {
			c, err := s.Consumer(ctx, cname)
			if err != nil {
				st.Problems = append(st.Problems, fmt.Sprintf("consumer %s/%s: %v", name, cname, err))
				continue
			}
			ci := c.CachedInfo()
			st.Consumers = append(st.Consumers, ConsumerHealth{
				Stream:        name,
				Name:          cname,
				NumPending:    ci.NumPending,
				NumAckPending: ci.NumAckPending,
				Bound:         ci.PushBound || ci.NumWaiting > 0,
			})
		}
	}
	cards, err := b.agentCards(ctx)
	if err != nil {
		st.Problems = append(st.Problems, fmt.Sprintf("agent cards: %v", err))
		if len(cards) == 0 {
			// Nil, not empty: a failed read must not report "no cards".
			cards = nil
		}
	}
	st.AgentCards = cards
	return st
}

// agentCards reads the latest card of every agent off DIRECTORY. A closed
// card removes the agent. It stops at ctx's deadline with what it has read.
func (b *NATSBus) agentCards(ctx context.Context) (map[string]AgentCard, error) {
	s, err := b.client.JetStream().Stream(ctx, streamDirectory)
	if err != nil {
		return nil, err
	}
	cards := map[string]AgentCard{}
	if s.CachedInfo().State.Msgs == 0 {
		return cards, nil
	}
	cons, err := s.CreateConsumer(ctx, jetstream.ConsumerConfig{
		FilterSubject:     directorySubjects,
		DeliverPolicy:     jetstream.DeliverLastPerSubjectPolicy,
		AckPolicy:         jetstream.AckNonePolicy,
		MemoryStorage:     true,
		InactiveThreshold: directoryConsumerIdle,
	})
	if err != nil {
		return nil, err
	}
	pending := cons.CachedInfo().NumPending
	if pending == 0 {
		return cards, nil
	}
	// Read through the shared byte-bounded reader, as every read here is:
	// a DIRECTORY that grew past readMaxBytes is reported, with the cards
	// read before it, rather than read whole into the pod.
	r, err := b.newBudgetReader(cons, int(min(pending, directoryFetchBatch)), readMaxBytes)
	if err != nil {
		return cards, err
	}
	defer r.stop()
	seen := 0
	for {
		wait := fetchWait(ctx, directoryFetchWait)
		if wait < minFetchWait {
			return cards, fmt.Errorf("read %d of the directory before the deadline", seen)
		}
		msg, err := r.next(wait)
		if errors.Is(err, nats.ErrTimeout) {
			if ctx.Err() != nil {
				return cards, ctx.Err()
			}
			// Messages were pending and none came: the read did not
			// complete, which is not an empty directory.
			return cards, fmt.Errorf("read %d of the directory; a fetch came back empty with cards still pending", seen)
		}
		if err != nil {
			return cards, fmt.Errorf("DIRECTORY: %w", err)
		}
		seen++
		if seen > maxAgentCards {
			return cards, fmt.Errorf("DIRECTORY holds more than %d cards; showing the first %d", maxAgentCards, maxAgentCards)
		}
		addCard(cards, msg.Subject(), msg.Data())
		if md, err := msg.Metadata(); err == nil {
			if pending = md.NumPending; pending == 0 {
				return cards, nil
			}
		}
	}
}

// addCard folds one DIRECTORY message into cards.
func addCard(cards map[string]AgentCard, subject string, data []byte) {
	// The profile is shown in a code span, so only a valid subject token,
	// which cannot carry a backtick or a space, is accepted.
	profile, ok := strings.CutPrefix(subject, lib.AgentSubject(""))
	if !ok || !lib.ValidSubjectToken(profile) {
		return
	}
	env, err := lib.ParseEnvelope(data)
	if err != nil {
		return
	}
	if env.Kind == lib.KindAgentClosed {
		delete(cards, profile)
		return
	}
	var card AgentCard
	if json.Unmarshal(env.Payload, &card) != nil {
		return
	}
	cards[profile] = card
}

// DialRefused reports whether a failed first connect will fail again however
// long it is retried: the bus refused the credential, or its certificate did
// not verify (an unknown authority or the wrong name). A pod with either is
// misconfigured, and saying so beats retrying it as "not reachable yet".
func DialRefused(err error) bool {
	var unknownCA x509.UnknownAuthorityError
	var wrongName x509.HostnameError
	var unverified *tls.CertificateVerificationError
	return errors.Is(err, nats.ErrAuthorization) ||
		strings.Contains(strings.ToLower(err.Error()), "authorization violation") ||
		errors.As(err, &unknownCA) || errors.As(err, &wrongName) || errors.As(err, &unverified)
}

// passwordFromFile is the credential callback for a password kept in a
// mounted file: it reads the file on each call, trimming the line ending a
// `kubectl create secret --from-file` keeps, and falls back to the last good
// value when the file is unreadable or empty (the kubelet swaps it in place).
func passwordFromFile(user, path, initial string) func() (string, string) {
	var mu sync.Mutex
	last := initial
	return func() (string, string) {
		mu.Lock()
		defer mu.Unlock()
		if raw, err := os.ReadFile(path); err == nil {
			if pw := strings.TrimRight(string(raw), "\r\n"); pw != "" {
				last = pw
			}
		}
		return user, last
	}
}
