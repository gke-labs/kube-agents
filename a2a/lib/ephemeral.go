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
// milliseconds, so an attempt that takes this long is one the server can't
// answer yet, not a slow one. Three attempts bound the consumers a timed-out
// create may leave behind (see CreateEphemeralConsumer). No attempt is given
// less than EphemeralCreateMinAttempt unless less than that is left.
const (
	EphemeralCreateAttemptTimeout = 10 * time.Second
	EphemeralCreateMaxAttempts    = 3
	EphemeralCreateMinAttempt     = time.Second
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
	// server refused the create, so nothing was made; release(true) when the
	// attempt failed any other way (a timeout, a dropped connection), so a
	// consumer may still be made later. The winning attempt's release is
	// returned to the caller, who holds it for the consumer's life. An error
	// from Slot ends the create with that error.
	Slot func() (release func(created bool), err error)
	// Stream names the stream in log lines and errors.
	Stream string
}

// ErrEphemeralCreateTimedOut is wrapped by the error CreateEphemeralConsumer
// returns when every attempt timed out. The error also wraps the last
// attempt's own error, context.DeadlineExceeded for a timeout.
var ErrEphemeralCreateTimedOut = errors.New("every attempt to create the consumer timed out")

// CreateEphemeralConsumer creates an ephemeral consumer on s, giving each
// attempt its own bounded timeout and retrying a timed-out attempt with
// jittered backoff, all within ctx's deadline.
//
// Why: the server places an R1 ephemeral consumer on one of the stream's
// peers, and it can pick a peer that is offline. A create placed there gets
// no answer until that peer returns, so a create bounded only by the
// caller's deadline hangs for all of it (#7082). Each retry is a new
// placement.
//
// Each attempt gets AttemptTimeout, or an even share of what is left of ctx's
// deadline across the attempts still to come when that is shorter, so a
// caller with a short deadline still gets its retries; never less than
// EphemeralCreateMinAttempt unless less than that is left.
//
// A refusal from the server (a JetStream API error) is returned at once and
// not retried. Any other error that isn't the attempt's own timeout (a
// closed connection, say) is returned at once too.
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
	ended := func(err error) error {
		return fmt.Errorf("create a consumer on %s: %d attempt(s) timed out before the caller's context ended: %w",
			o.Stream, timedOut, err)
	}
	for attempt := 1; attempt <= o.MaxAttempts; attempt++ {
		if attempt > 1 {
			select {
			case <-ctx.Done():
				return nil, nil, ended(ctx.Err())
			case <-time.After(fullJitterBackoff(attempt - 1)):
			}
		}
		release := func(bool) {}
		if o.Slot != nil {
			r, err := o.Slot()
			if err != nil {
				if timedOut > 0 {
					return nil, nil, fmt.Errorf("create a consumer on %s after %d timed-out attempt(s): %w", o.Stream, timedOut, err)
				}
				return nil, nil, err
			}
			release = r
		}
		attemptCtx, cancel := context.WithTimeout(ctx, attemptTimeout(ctx, o.AttemptTimeout, o.MaxAttempts-attempt+1))
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
		if attemptExpired {
			timedOut++
		}
		if ctx.Err() != nil {
			return nil, nil, ended(err)
		}
		if !attemptExpired {
			// Not this attempt's timeout (a closed connection, say): the
			// caller's error, not a placement to retry.
			return nil, nil, err
		}
		lastErr = err
		log.Info("consumer create timed out; it may be placed on a bus server that is down", "stream", o.Stream,
			"attempt", attempt, "of", o.MaxAttempts, "err", err)
	}
	return nil, nil, fmt.Errorf("create a consumer on %s: %w (%d of %d; each may leave a consumer the server reaps once idle): %w",
		o.Stream, ErrEphemeralCreateTimedOut, timedOut, o.MaxAttempts, lastErr)
}

// attemptTimeout is one attempt's share: limit, or what is left of ctx's
// deadline split across the attempts still to come when that is shorter, but
// not under EphemeralCreateMinAttempt unless less than that is left.
func attemptTimeout(ctx context.Context, limit time.Duration, left int) time.Duration {
	deadline, ok := ctx.Deadline()
	if !ok {
		return limit
	}
	remaining := time.Until(deadline)
	share := remaining / time.Duration(max(1, left))
	share = max(share, min(EphemeralCreateMinAttempt, remaining))
	return min(limit, share)
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
