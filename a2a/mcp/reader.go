package mcp

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

const (
	// drainFetchBatch and drainFetchWait bound one pull of a drain: the most
	// messages one pull asks for, and how long a drain waits for the next
	// message before it calls the read stalled.
	drainFetchBatch = 256
	drainFetchWait  = 2 * time.Second

	// pullMaxBytes caps every pull a read sends, in the request itself
	// (max_bytes): the server ends a pull before the message that would
	// take it past this, so no one pull brings more into the pod however
	// many messages it asks for. It holds the largest message the
	// operator's bus accepts, as the server counts it: max_payload's 1 MiB
	// of headers and payload, a subject of up to max_control_line's 64 KiB,
	// and 8 KiB for the ack reply and slack. A larger message (a bus whose
	// max_payload was raised past it) the server does not send at all; the
	// read stops at it (budgetReader.next) rather than take it into the
	// pod.
	pullMaxBytes = 1<<20 + 72<<10

	// readMaxBytes is the most one read passes to its caller, counted per
	// message as payload, subject and header keys and values (msgBytes): a
	// drain, a task watch (over its whole life, since the watch keeps what
	// it folds) and a DIRECTORY read each stop at it. The pod has 128 MiB
	// (a2aMCPMemoryLimit in the operator), with GOMEMLIMIT at 85% of it,
	// about 108 MiB. A reader has one Fetch in flight at a time and hands
	// its messages out before it sends the next, so it holds at worst one
	// pull's pullMaxBytes, 1.1 MiB, plus what it passed on, 4 MiB, plus a
	// task watch's parsed copy of that, 4 MiB more: about 9.1 MiB. The
	// eight reader slots (maxTaskReaders) and the one DIRECTORY read the
	// status cache keeps in flight, all at their worst at once, come to
	// about 82 MiB, leaving some 26 MiB of the GOMEMLIMIT for the server
	// itself. That leaves out per-message overhead (msgBytes counts
	// content only), the cached status the next refresh replaces, and each
	// response's encoding; a read's raw messages can be collected once
	// parsed, which offsets some of it. It's an estimate, not a bound.
	readMaxBytes = 4 << 20
)

// batchCompletedRetries bounds how many pulls in a row may end on "409
// Batch Completed" with nothing delivered before next gives up on the read.
const batchCompletedRetries = 3

// ErrReadByteBudget is a read stopped by its byte budget (readMaxBytes), or
// by a message the server will not send under the per-pull cap
// (pullMaxBytes).
var ErrReadByteBudget = errors.New("read byte budget reached")

// budgetReader is the one way this server reads a consumer: one pull at a
// time, each capped by the server at pullMaxBytes, handed out a message at
// a time, and a running total of what it has passed on, which stops the
// read at its budget. Its pulls are the legacy client's one-shot Fetch on a
// subscription bound to the consumer, the one client call that takes a
// byte cap alongside a batch and reports the server refusing a message
// over it ("409 Message Size Exceeds MaxBytes") as an error. The jetstream
// package's FetchBytes hides that refusal and allocates for a million
// messages a call; its Messages iterator treats it as routine and pulls
// again at once, some 37,000 pulls in a two-second wait. One per read;
// stop it when done.
type budgetReader struct {
	sub   msgFetcher
	buf   []*nats.Msg
	batch int
	// pending is what the consumer last said it still held (its info at
	// the start, then each message's metadata): each Fetch asks for that
	// many, at least one and at most batch, as the reads did before.
	pending uint64
	budget  int
	total   int
	read    int
}

// msgFetcher is the reader's bound pull subscription (a *nats.Subscription).
type msgFetcher interface {
	Fetch(batch int, opts ...nats.PullOpt) ([]*nats.Msg, error)
	Unsubscribe() error
}

// newBudgetReader reads cons in pulls of at most batch messages (at least
// one) and pullMaxBytes, passing at most budget bytes in all. The
// subscription binds to cons without looking it up (the read has its info
// already), so it needs no grant but the consumer's MSG.NEXT; and since the
// client did not create the consumer, unsubscribing never deletes it, a
// delete the console grant refuses.
func (b *NATSBus) newBudgetReader(cons jetstream.Consumer, batch, budget int) (*budgetReader, error) {
	js, err := b.client.Conn().JetStream()
	if err != nil {
		return nil, err
	}
	info := cons.CachedInfo()
	sub, err := js.PullSubscribe("", "", nats.Bind(info.Stream, info.Name), nats.SkipConsumerLookup())
	if err != nil {
		return nil, err
	}
	return &budgetReader{sub: sub, batch: max(1, batch), budget: budget, pending: info.NumPending}, nil
}

// next returns the next message, waiting at most wait for it. No message in
// that time is nats.ErrTimeout. A message that would take the read past its
// budget is not passed: next reports how many were read before it, wrapping
// ErrReadByteBudget, as the deadline stop does. So is a message the server
// will not send under pullMaxBytes: one refused pull ends the read.
func (r *budgetReader) next(wait time.Duration) (jetstream.Msg, error) {
	if len(r.buf) == 0 {
		// drainFetchBatch bounds the conversion; no read asks for more.
		ask := max(1, min(int(min(r.pending, drainFetchBatch)), r.batch))
		msgs, err := r.sub.Fetch(ask, nats.PullMaxBytes(pullMaxBytes), nats.MaxWait(wait))
		// The server ends a pull whose count filled with bytes to spare with a
		// "409 Batch Completed" trailer after its last message. The legacy
		// Fetch returns once it has its batch, so it never reads that trailer,
		// and every pull shares one reply inbox: a trailer that lands just
		// after the next Fetch starts is read as that Fetch's answer, which it
		// reports as an error with nothing delivered (#4097). It is the last
		// pull's end, not this one's, so ask again, a bounded number of times.
		for retry := 0; retry < batchCompletedRetries && err != nil && len(msgs) == 0 && strings.Contains(err.Error(), "Batch Completed"); retry++ {
			msgs, err = r.sub.Fetch(ask, nats.PullMaxBytes(pullMaxBytes), nats.MaxWait(wait))
		}
		switch {
		case err != nil && strings.Contains(err.Error(), "Exceeds MaxBytes"):
			// Not a sentinel in the client: the server's description.
			return nil, fmt.Errorf("read %d messages before one over the %d-byte pull cap, which the server will not send: %w", r.read, pullMaxBytes, ErrReadByteBudget)
		// "No messages" here is the same kind of stale answer: a 404 the server
		// sent to an earlier pull's no_wait request that a retry above left
		// open. An empty pull either way.
		case errors.Is(err, nats.ErrTimeout) || errors.Is(err, context.DeadlineExceeded) || (err == nil && len(msgs) == 0) ||
			(err != nil && len(msgs) == 0 && strings.Contains(err.Error(), "no messages")):
			return nil, nats.ErrTimeout
		case err != nil:
			return nil, err
		}
		r.buf = msgs
	}
	m := r.buf[0]
	r.buf[0] = nil
	r.buf = r.buf[1:]
	if md, err := m.Metadata(); err == nil {
		r.pending = md.NumPending
	}
	msg := pulledMsg{m}
	size := msgBytes(msg)
	if r.total+size > r.budget {
		return nil, fmt.Errorf("read %d messages before the byte budget (%d bytes): %w", r.read, r.budget, ErrReadByteBudget)
	}
	r.total += size
	r.read++
	return msg, nil
}

func (r *budgetReader) stop() { _ = r.sub.Unsubscribe() }

// fetchWait is wait, cut to what is left of ctx's deadline.
func fetchWait(ctx context.Context, wait time.Duration) time.Duration {
	if dl, ok := ctx.Deadline(); ok {
		wait = min(wait, time.Until(dl))
	}
	return wait
}

// drainConsumer reads what one plain pull consumer on s has pending when it
// is created, and stops: no acks, in memory, reaped by the server once idle
// (taskConsumerIdle), never an ordered consumer, whose reset deletes a
// consumer the console grant cannot. It takes one reader slot, held until the
// consumer it creates is reaped, or handed back at once when the server
// refused to create one (consumerMayExist). It reads through a budgetReader,
// waits for each message no longer than ctx's deadline, and reads at most
// limit messages, failing after them if more were pending. It passes at most
// readMaxBytes to each, and stops with ErrReadByteBudget before the message
// that would pass it, or at one the server will not send under pullMaxBytes;
// messages already passed to each stay passed, as when the deadline stops it.
func (b *NATSBus) drainConsumer(ctx context.Context, s jetstream.Stream, cfg jetstream.ConsumerConfig, limit int, each func(jetstream.Msg)) (int, error) {
	release, err := b.readerSlot()
	if err != nil {
		return 0, err
	}
	cfg.AckPolicy = jetstream.AckNonePolicy
	cfg.MemoryStorage = true
	cfg.InactiveThreshold = taskConsumerIdle
	cons, err := s.CreateConsumer(ctx, cfg)
	if err != nil {
		release(consumerMayExist(err))
		return 0, err
	}
	defer release(true)
	pending := cons.CachedInfo().NumPending
	want := min(pending, uint64(limit))
	var seen uint64
	if want > 0 {
		r, err := b.newBudgetReader(cons, int(min(want, drainFetchBatch)), readMaxBytes)
		if err != nil {
			return 0, err
		}
		defer r.stop()
		for seen < want {
			wait := fetchWait(ctx, drainFetchWait)
			if wait < minFetchWait {
				return int(seen), fmt.Errorf("read %d messages before the deadline: %w", seen, context.DeadlineExceeded)
			}
			msg, err := r.next(wait)
			if errors.Is(err, nats.ErrTimeout) && ctx.Err() != nil {
				return int(seen), fmt.Errorf("read %d messages before the deadline: %w", seen, ctx.Err())
			}
			if errors.Is(err, nats.ErrTimeout) {
				// Nothing came with messages still to read: a read that
				// stalled, not an empty stream. Say so rather than report
				// what was read as all there is.
				return int(seen), fmt.Errorf("read %d of %d pending messages before a fetch came back empty", seen, want)
			}
			if err != nil {
				return int(seen), err
			}
			seen++
			each(msg)
		}
	}
	if pending > want {
		return int(seen), fmt.Errorf("more than %d messages to read; read the first %d", limit, limit)
	}
	return int(seen), nil
}

// msgBytes is what a message counts against a read's byte budget: its
// payload, its subject, and its header keys and values.
func msgBytes(msg jetstream.Msg) int {
	n := len(msg.Data()) + len(msg.Subject())
	for k, vs := range msg.Headers() {
		for _, v := range vs {
			n += len(k) + len(v)
		}
	}
	return n
}

// errNoAck is any ack of a message read here: every read's consumer is
// AckNone.
var errNoAck = errors.New("reads here do not ack")

// pulledMsg is a message one of the reader's pulls brought, as a
// jetstream.Msg. Its consumer acks nothing.
type pulledMsg struct{ m *nats.Msg }

func (p pulledMsg) Metadata() (*jetstream.MsgMetadata, error) {
	md, err := p.m.Metadata()
	if err != nil {
		return nil, err
	}
	return &jetstream.MsgMetadata{
		Sequence:     jetstream.SequencePair{Consumer: md.Sequence.Consumer, Stream: md.Sequence.Stream},
		NumDelivered: md.NumDelivered,
		NumPending:   md.NumPending,
		Timestamp:    md.Timestamp,
		Stream:       md.Stream,
		Consumer:     md.Consumer,
		Domain:       md.Domain,
	}, nil
}
func (p pulledMsg) Data() []byte                   { return p.m.Data }
func (p pulledMsg) Headers() nats.Header           { return p.m.Header }
func (p pulledMsg) Subject() string                { return p.m.Subject }
func (p pulledMsg) Reply() string                  { return p.m.Reply }
func (pulledMsg) Ack() error                       { return errNoAck }
func (pulledMsg) DoubleAck(context.Context) error  { return errNoAck }
func (pulledMsg) Nak() error                       { return errNoAck }
func (pulledMsg) NakWithDelay(time.Duration) error { return errNoAck }
func (pulledMsg) InProgress() error                { return errNoAck }
func (pulledMsg) Term() error                      { return errNoAck }
func (pulledMsg) TermWithReason(string) error      { return errNoAck }
