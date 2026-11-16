package lib

import (
	"context"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// A first-dial option reaches Connect's dial and no rebuild's, while a
// WithNATSOptions option reaches both: RetryOnFailedConnect on a rebuild
// would skip its backoff and let Reconnect report a failed dial as done.
func TestFirstDialOptionsApplyToTheFirstDialOnly(t *testing.T) {
	s := startServer(t)
	var every, first atomic.Int32
	count := func(n *atomic.Int32) nats.Option {
		return func(*nats.Options) error { n.Add(1); return nil }
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	c, err := Connect(ctx, clientURL(s), WithNATSOptions(count(&every)), WithFirstDialNATSOptions(count(&first)))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	defer c.Close()
	if err := c.Reconnect(ctx); err != nil {
		t.Fatalf("Reconnect: %v", err)
	}
	if every.Load() != 2 || first.Load() != 1 {
		t.Fatalf("dials saw every-dial option %d times and first-dial option %d times; want 2 and 1", every.Load(), first.Load())
	}
}
