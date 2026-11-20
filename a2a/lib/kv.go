package lib

import (
	"context"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
)

// SessionStateBucket is the KV bucket holding the gateway's session registry
// (provisioned by the deployment; the gateway's user is the only writer).
const SessionStateBucket = "session-state"

// FeedbackBucket is the KV bucket holding the 👍 and 👎 ratings chat users gave the
// gateway's answers, with the exchange each one rated (provisioned by the
// deployment with a max age, which is the retention; the gateway's user is
// the only reader and writer).
const FeedbackBucket = "feedback"

// PeopleBucket is the KV bucket holding one record per chat user the gateway
// has seen, keyed by a pseudonym derived for this bucket alone (provisioned by
// the deployment with a max age, which is the retention; the gateway's user
// is the only reader and writer; provision and seed list its keys).
const PeopleBucket = "people"

// ScheduledReportsBucket is the KV bucket holding one record per finished
// unattended run on a schedule trigger: the report, whether it was posted,
// and where (docs/designs/scheduling-on-profiles.md section 4). Provisioned by
// the deployment with a long max age (90 days): it is the durable record a
// later reader (the console's #scheduled channel, onboarding) enumerates, and
// the age keeps a bucket that refuses new writes when full (KV is
// discard=new) from filling and stalling every later run. The gateway's user
// is the only writer.
const ScheduledReportsBucket = "SCHEDULED_REPORTS"

// ScheduleThreadsBucket is the KV bucket holding the home-channel thread each
// schedule posts into for one UTC day, keyed <jobKey>.<YYYY-MM-DD>
// (docs/designs/scheduling-on-profiles.md section 4, item 3). Provisioned by
// the deployment with a 48-hour max age, so a day's entry outlives its day
// and then goes; kept in the bucket rather than in memory so a gateway
// failover keeps posting into the same thread. The gateway's user is the
// only reader and writer.
const ScheduleThreadsBucket = "SCHEDULE_THREADS"

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
