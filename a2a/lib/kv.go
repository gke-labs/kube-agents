package lib

import (
	"context"

	"github.com/nats-io/nats.go"
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

// JetStream hands out the live JetStream handle. Like KV above it does not
// survive a terminal rebuild (NR-2), so callers fetch one per operation rather
// than caching it. Exported for the capability path, which publishes to the KV
// bucket's own subject directly rather than binding the bucket — binding would
// require a stream-info read, and 09 §4's rule is that no writer reads the
// capability store at all.
func (c *Client) JetStream() jetstream.JetStream {
	_, js := c.conn()
	return js
}

// Conn hands out the live core connection, for the request/reply paths that
// are not JetStream. Same rebuild caveat.
func (c *Client) Conn() *nats.Conn {
	nc, _ := c.conn()
	return nc
}
