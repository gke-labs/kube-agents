package mcp

import (
	"context"
	"encoding/json"
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
	// read stops at it (budgetReader.status) rather than take it into the
	// pod.
	pullMaxBytes = 1<<20 + 72<<10

	// readMaxBytes is the most one read passes to its caller, counted per
	// message as payload, subject and header keys and values (msgBytes): a
	// drain, a task watch (over its whole life, since the watch keeps what
	// it folds) and a DIRECTORY read each stop at it. The pod has 128 MiB
	// (a2aMCPMemoryLimit in the operator), with GOMEMLIMIT at 85% of it,
	// about 108 MiB. One read holds at worst two pulls in flight (one
	// taken as over after pullGap may still be arriving), 2.2 MiB, plus what it passed on, 4 MiB, plus a task watch's
	// parsed copy of that, 4 MiB more: about 10.2 MiB. The eight reader
	// slots (maxTaskReaders) and the one DIRECTORY read the status cache
	// keeps in flight, all at their worst at once, come to about 92 MiB,
	// leaving some 16 MiB of the GOMEMLIMIT for the server itself.
	readMaxBytes = 4 << 20
)

// ErrReadByteBudget is a read stopped by its byte budget (readMaxBytes), or
// by a message the server will not send under the per-pull cap
// (pullMaxBytes).
var ErrReadByteBudget = errors.New("read byte budget reached")

// budgetReader is the one way this server reads a consumer: one pull at a
// time, each capped by the server at pullMaxBytes, and a running total of
// what it has passed on, which stops the read at its budget. It sends its
// own pull requests rather than use the client's Fetch or Messages: Fetch
// has no byte cap short of FetchBytes's million-message buffers, and
// Messages treats the server's "409 Message Size Exceeds MaxBytes" as
// routine and pulls again at once, so a message over the cap had it send
// some 37,000 pulls in a two-second wait. Here that 409 on a pull that
// brought nothing ends the read. One per read; stop it when done.
type budgetReader struct {
	cons jetstream.Consumer
	nc   *nats.Conn
	sub  msgSource
	// api is the consumer's next-message subject; inbox is the prefix of
	// each pull's reply subject, which tells one pull's status from an
	// earlier pull's.
	api, inbox string
	batch      int
	budget     int
	total      int
	read       int

	// The pull in flight: its reply subject, when it expires, how many
	// messages it has brought, and whether the last of them said more were
	// pending.
	pulling     bool
	reply       string
	pulls       int
	pullEnds    time.Time
	got         int
	morePending bool
}

// msgSource is the reader's subscription (a *nats.Subscription).
type msgSource interface {
	NextMsg(time.Duration) (*nats.Msg, error)
	Unsubscribe() error
}

const (
	// jsAPINext is the JetStream API subject a pull is sent to, under the
	// default API prefix, as the bus's jetstream client uses.
	jsAPINext = "$JS.API.CONSUMER.MSG.NEXT.%s.%s"
	// pullGap is how long a pull that brought messages, the last of which
	// said more were pending, may go quiet before it is taken as over: the
	// server ends a pull that fills its byte cap exactly without a word.
	pullGap = 50 * time.Millisecond
)

// newBudgetReader reads cons in pulls of at most batch messages (at least
// one) and pullMaxBytes, passing at most budget bytes in all.
func (b *NATSBus) newBudgetReader(cons jetstream.Consumer, batch, budget int) (*budgetReader, error) {
	nc := b.client.Conn()
	inbox := nc.NewInbox()
	sub, err := nc.SubscribeSync(inbox + ".*")
	if err != nil {
		return nil, err
	}
	info := cons.CachedInfo()
	return &budgetReader{
		cons: cons, nc: nc, sub: sub,
		api: fmt.Sprintf(jsAPINext, info.Stream, info.Name), inbox: inbox,
		batch: max(1, batch), budget: budget,
	}, nil
}

// pull asks for the next batch, expiring after expires.
func (r *budgetReader) pull(expires time.Duration) error {
	req, err := json.Marshal(struct {
		Batch    int           `json:"batch"`
		MaxBytes int           `json:"max_bytes"`
		Expires  time.Duration `json:"expires"`
	}{r.batch, pullMaxBytes, expires})
	if err != nil {
		return err
	}
	r.pulls++
	r.reply = fmt.Sprintf("%s.%d", r.inbox, r.pulls)
	if err := r.nc.PublishRequest(r.api, r.reply, req); err != nil {
		return err
	}
	r.pulling, r.got, r.morePending = true, 0, false
	// A pull whose end was never seen (its status lost) is given up a
	// second after it expired.
	r.pullEnds = time.Now().Add(expires + time.Second)
	return nil
}

// next returns the next message, waiting at most wait for it. No message in
// that time is nats.ErrTimeout. A message that would take the read past its
// budget is not passed: next reports how many were read before it, wrapping
// ErrReadByteBudget, as the deadline stop does. So is a message the server
// will not send under pullMaxBytes, on the first pull it refuses.
func (r *budgetReader) next(wait time.Duration) (jetstream.Msg, error) {
	deadline := time.Now().Add(wait)
	for {
		left := time.Until(deadline)
		if left <= 0 {
			return nil, nats.ErrTimeout
		}
		if r.pulling && time.Now().After(r.pullEnds) {
			r.pulling = false
		}
		if !r.pulling {
			if err := r.pull(max(left, minFetchWait)); err != nil {
				return nil, err
			}
		}
		timeout := left
		if r.got > 0 && r.morePending {
			timeout = min(left, pullGap)
		}
		m, err := r.sub.NextMsg(timeout)
		if errors.Is(err, nats.ErrTimeout) {
			if timeout < left {
				r.pulling = false
				continue
			}
			return nil, nats.ErrTimeout
		}
		if err != nil {
			return nil, err
		}
		if status := m.Header.Get("Status"); status != "" && len(m.Data) == 0 {
			if done, err := r.status(m, status); err != nil || done {
				return nil, err
			}
			continue
		}
		r.got++
		if r.got >= r.batch {
			r.pulling = false
		}
		msg := pulledMsg{m}
		if md, err := m.Metadata(); err == nil {
			r.morePending = md.NumPending > 0
		}
		size := msgBytes(msg)
		if r.total+size > r.budget {
			return nil, fmt.Errorf("read %d messages before the byte budget (%d bytes): %w", r.read, r.budget, ErrReadByteBudget)
		}
		r.total += size
		r.read++
		return msg, nil
	}
}

// status handles a status message: the end of the pull in flight (a 408 or
// 404, or a 409 for a filled byte cap or batch), the end of the read (a 409
// for a byte cap on a pull that brought nothing, which is a message over
// pullMaxBytes), or an error. A status for an earlier pull is ignored.
func (r *budgetReader) status(m *nats.Msg, status string) (bool, error) {
	if m.Subject != r.reply || status == "100" {
		return false, nil
	}
	desc := m.Header.Get("Description")
	switch {
	case status == "404" || status == "408":
	case status == "409" && strings.Contains(desc, "Exceeds MaxBytes"):
		if r.got == 0 {
			return true, fmt.Errorf("read %d messages before one over the %d-byte pull cap, which the server will not send: %w", r.read, pullMaxBytes, ErrReadByteBudget)
		}
	case status == "409" && strings.Contains(desc, "Batch Completed"):
	default:
		return true, fmt.Errorf("pull: %s %s", status, desc)
	}
	r.pulling = false
	return false, nil
}

// stalled explains a read that got nothing in its wait with messages still
// to read, as base when the consumer has nothing left to send (they went
// away under it) or cannot be asked. When the server still holds messages
// for it, a live server that sends none of them is refusing the next one
// under pullMaxBytes: it ends each pull on it (a 409) rather than deliver
// it, so the read could never get past it, and saying so beats a bare stall.
func (r *budgetReader) stalled(ctx context.Context, base error) error {
	info, err := r.cons.Info(ctx)
	if err != nil || info.NumPending == 0 {
		return base
	}
	return fmt.Errorf("%w; the server holds %d more and sent none, which is a message over the %d-byte pull cap: %w", base, info.NumPending, pullMaxBytes, ErrReadByteBudget)
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
				return int(seen), r.stalled(ctx, fmt.Errorf("read %d of %d pending messages before a fetch came back empty", seen, want))
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
