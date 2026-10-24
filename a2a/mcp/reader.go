package mcp

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

const (
	// drainFetchBatch and drainFetchWait bound one fetch of a drain.
	drainFetchBatch = 256
	drainFetchWait  = 2 * time.Second

	// drainFetchBytes bounds the bytes one fetch can bring into the pod. The
	// client has no per-fetch byte option that leaves the batch count alone
	// (FetchBytes asks for up to a million messages and allocates its
	// buffers for that many, about 30 MiB a call), so the bound is kept by
	// the count: a fetch asks for at most drainFetchBytes / max_payload
	// messages, 4 at the bus's 1 MiB default, and never fewer than one, so a
	// message the server accepts can always be fetched. A message larger
	// than this (only on a bus whose max_payload is raised past it) is
	// refused, not passed on.
	drainFetchBytes = 4 << 20
	// drainMaxBytes is the most one drain passes to its caller, counted per
	// message as payload, subject and header keys and values. The pod has
	// 128 MiB (a2aMCPMemoryLimit in the operator). A drain holds at most its
	// budget plus the rest of one fetch, 12 MiB, so two drains at once with
	// parsing stay well inside the limit; all eight reader slots
	// (maxTaskReaders) draining at their worst at once would come to 96 MiB.
	drainMaxBytes = 8 << 20
)

// ErrReadByteBudget is a drain stopped by its byte budget (drainMaxBytes), or
// by a single message over the per-fetch cap (drainFetchBytes).
var ErrReadByteBudget = errors.New("read byte budget reached")

// drainConsumer reads what one plain pull consumer on s has pending when it
// is created, and stops: no acks, in memory, reaped by the server once idle
// (taskConsumerIdle), never an ordered consumer, whose reset deletes a
// consumer the console grant cannot. It takes one reader slot, held until the
// consumer it creates is reaped, or handed back at once when the server
// refused to create one (consumerMayExist), and it sizes each fetch to what
// is left and to drainFetchBytes, bounds each fetch by ctx's deadline, and
// reads at most limit messages, failing after them if more were pending. It
// passes at most drainMaxBytes to each, and stops with ErrReadByteBudget
// before the message that would pass it, or at a message larger than
// drainFetchBytes; messages already passed to each stay passed, as when the
// deadline stops it.
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
	var maxPayload int64
	if nc := b.client.Conn(); nc != nil {
		maxPayload = nc.MaxPayload()
	}
	batchCap := drainBatch(maxPayload)
	pending := cons.CachedInfo().NumPending
	want := min(pending, uint64(limit))
	var seen uint64
	var total int
	for seen < want {
		wait := drainFetchWait
		if dl, ok := ctx.Deadline(); ok {
			wait = min(wait, time.Until(dl))
		}
		if wait < minFetchWait {
			return int(seen), fmt.Errorf("read %d messages before the deadline: %w", seen, context.DeadlineExceeded)
		}
		ask := batchCap
		if left := want - seen; left < drainFetchBatch {
			ask = min(ask, int(left))
		}
		batch, err := cons.Fetch(ask, jetstream.FetchMaxWait(wait))
		if err != nil {
			return int(seen), err
		}
		n := 0
		for msg := range batch.Messages() {
			n++
			size := msgBytes(msg)
			if size > drainFetchBytes {
				return int(seen), fmt.Errorf("read %d messages before one of %d bytes, over the %d-byte fetch cap: %w", seen, size, drainFetchBytes, ErrReadByteBudget)
			}
			if total+size > drainMaxBytes {
				return int(seen), fmt.Errorf("read %d messages before the byte budget (%d bytes): %w", seen, drainMaxBytes, ErrReadByteBudget)
			}
			total += size
			seen++
			each(msg)
		}
		if err := batch.Error(); err != nil && !errors.Is(err, nats.ErrTimeout) {
			return int(seen), err
		}
		if n == 0 {
			break
		}
	}
	// A fetch that came back empty with messages still to read is a read
	// that stalled, not an empty stream: say so rather than report what
	// was read as all there is.
	if seen < want {
		return int(seen), fmt.Errorf("read %d of %d pending messages before a fetch came back empty", seen, want)
	}
	if pending > want {
		return int(seen), fmt.Errorf("more than %d messages to read; read the first %d", limit, limit)
	}
	return int(seen), nil
}

// drainBatch is the most messages one fetch asks for when the server accepts
// messages of up to maxPayload bytes: as many as fit drainFetchBytes, at
// least one, at most drainFetchBatch. A maxPayload the connection does not
// know (zero) is taken as the largest, so the fetch asks for one.
func drainBatch(maxPayload int64) int {
	if maxPayload <= 0 || maxPayload >= drainFetchBytes {
		return 1
	}
	return int(min(drainFetchBytes/maxPayload, drainFetchBatch))
}

// msgBytes is what a message counts against a drain's byte budget: its
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
