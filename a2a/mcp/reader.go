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
)

// drainConsumer reads what one plain pull consumer on s has pending when it
// is created, and stops: no acks, in memory, reaped by the server once idle
// (taskConsumerIdle), never an ordered consumer, whose reset deletes a
// consumer the console grant cannot. It takes one reader slot, held until the
// consumer it creates is reaped, or handed back at once when the server
// refused to create one (consumerMayExist), and it sizes each fetch to what
// is left, bounds each fetch by ctx's deadline, and reads at most limit
// messages, failing after them if more were pending.
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
	for seen < want {
		wait := drainFetchWait
		if dl, ok := ctx.Deadline(); ok {
			wait = min(wait, time.Until(dl))
		}
		if wait < minFetchWait {
			return int(seen), fmt.Errorf("read %d messages before the deadline: %w", seen, context.DeadlineExceeded)
		}
		batch, err := cons.Fetch(int(min(want-seen, drainFetchBatch)), jetstream.FetchMaxWait(wait))
		if err != nil {
			return int(seen), err
		}
		n := 0
		for msg := range batch.Messages() {
			n++
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
