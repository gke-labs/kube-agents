package lib

import (
	"context"

	"github.com/nats-io/nats.go/jetstream"
)

// SessionStateBucket is the KV bucket holding the gateway's session registry
// (provisioned by the deployment; the gateway's user is the only writer).
const SessionStateBucket = "session-state"

// KV returns a handle to the named KV bucket on the client's current
// connection. Handles bind to that connection and do not survive a terminal
// rebuild (NR-2), so callers fetch one per operation rather than caching it;
// an operation that fails after a rebuild is retried through a fresh handle.
func (c *Client) KV(ctx context.Context, bucket string) (jetstream.KeyValue, error) {
	_, js := c.conn()
	return js.KeyValue(ctx, bucket)
}
