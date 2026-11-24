package lib

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The defaults for CreateEphemeralConsumer. A healthy create answers in
// milliseconds, so an attempt that takes AttemptTimeout is one the server
// can't answer yet, not a slow one. Three attempts bound the consumers a
// timed-out create may leave behind (see CreateEphemeralConsumer).
const (
	EphemeralCreateAttemptTimeout = 10 * time.Second
	EphemeralCreateMaxAttempts    = 3
)

// ConsumerCreator is what CreateEphemeralConsumer creates a consumer on.
// jetstream.Stream satisfies it.
type ConsumerCreator interface {
	CreateConsumer(ctx context.Context, cfg jetstream.ConsumerConfig) (jetstream.Consumer, error)
}

// EphemeralCreateOptions tunes CreateEphemeralConsumer. Zero values take the
// defaults above.
type EphemeralCreateOptions struct {
	AttemptTimeout time.Duration
	MaxAttempts    int
	Logger         *slog.Logger
	// Slot, when set, is taken before every attempt and handed the attempt's
	// outcome when the attempt no longer needs it: release(false) when the
	// server refused the create, so nothing was made; release(true) when an
	// attempt timed out, so a consumer may still be made later. The winning
	// attempt's release is returned to the caller, who holds it for the
	// consumer's life. An error from Slot ends the create with that error.
	Slot func() (release func(created bool), err error)
	// Stream names the stream in log lines.
	Stream string
}

// ErrEphemeralCreateTimedOut is wrapped by the error CreateEphemeralConsumer
// returns when every attempt timed out.
var ErrEphemeralCreateTimedOut = errors.New("every attempt to create the consumer timed out")

// CreateEphemeralConsumer creates an ephemeral consumer on s, giving each
// attempt its own bounded timeout and retrying a timed-out attempt with
// jittered backoff, all within ctx's deadline.
//
// Why: the server places an R1 ephemeral consumer on one of the stream's
// peers, and it can pick a peer that is offline. A create placed there gets
// no answer until that peer returns, so a create bounded only by the
// caller's deadline hangs for all of it (#7082). Each retry is a new
// placement. A refusal from the server (a JetStream API error) is returned at
// once and not retried.
//
// A timed-out attempt may still leave a consumer, made when its peer returns
// and reaped once idle. MaxAttempts bounds how many one call can leave.
//
// The returned release is the winning attempt's Slot release (a no-op when
// Slot is nil); the caller calls it with true when done with the consumer.
func CreateEphemeralConsumer(ctx context.Context, s ConsumerCreator, cfg jetstream.ConsumerConfig, o EphemeralCreateOptions) (jetstream.Consumer, func(created bool), error) {
	if o.AttemptTimeout <= 0 {
		o.AttemptTimeout = EphemeralCreateAttemptTimeout
	}
	if o.MaxAttempts <= 0 {
		o.MaxAttempts = EphemeralCreateMaxAttempts
	}
	log := o.Logger
	if log == nil {
		log = slog.Default()
	}
	timedOut := 0
	var lastErr error
	for attempt := 1; attempt <= o.MaxAttempts; attempt++ {
		if attempt > 1 {
			select {
			case <-ctx.Done():
				return nil, nil, fmt.Errorf("create a consumer on %s: %d attempts timed out before the deadline: %w", o.Stream, timedOut, ctx.Err())
			case <-time.After(fullJitterBackoff(attempt - 1)):
			}
		}
		release := func(bool) {}
		if o.Slot != nil {
			r, err := o.Slot()
			if err != nil {
				return nil, nil, err
			}
			release = r
		}
		attemptCtx, cancel := context.WithTimeout(ctx, o.AttemptTimeout)
		cons, err := s.CreateConsumer(attemptCtx, cfg)
		attemptExpired := attemptCtx.Err() != nil
		cancel()
		if err == nil {
			if attempt > 1 {
				log.Info("consumer created after a timed-out attempt", "stream", o.Stream, "attempt", attempt,
					"consumer", cons.CachedInfo().Name, "leader", consumerLeader(cons))
			}
			return cons, release, nil
		}
		var refused *jetstream.APIError
		if errors.As(err, &refused) {
			release(false)
			return nil, nil, err
		}
		// Anything else may have been answered after we stopped waiting.
		release(true)
		if ctx.Err() != nil {
			return nil, nil, fmt.Errorf("create a consumer on %s: %d attempts timed out before the deadline: %w", o.Stream, timedOut, err)
		}
		if !attemptExpired {
			// Not this attempt's timeout (a closed connection, say): the
			// caller's error, not a placement to retry.
			return nil, nil, err
		}
		timedOut++
		lastErr = err
		log.Info("consumer create timed out; it may be placed on a bus server that is down", "stream", o.Stream,
			"attempt", attempt, "of", o.MaxAttempts, "timeout", o.AttemptTimeout, "err", err)
	}
	return nil, nil, fmt.Errorf("create a consumer on %s: %w (%d of %d, %s each; each may leave a consumer the server reaps once idle): %v",
		o.Stream, ErrEphemeralCreateTimedOut, timedOut, o.MaxAttempts, o.AttemptTimeout, lastErr)
}

// consumerLeader is the consumer's cluster leader as the create's answer
// gave it, or "" on a single server.
func consumerLeader(cons jetstream.Consumer) string {
	info := cons.CachedInfo()
	if info == nil || info.Cluster == nil {
		return ""
	}
	return info.Cluster.Leader
}
