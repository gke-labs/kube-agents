// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"log"
	"strings"
	"time"

	pubsub "google.golang.org/api/pubsub/v1"
)

const (
	// defaultMaxMessages is how many messages one Pull asks for. The surviving
	// post-sink stream measured about 0.7 messages a second, so this drains a
	// couple of minutes of backlog per round trip and is far below the API's
	// 1000 ceiling.
	defaultMaxMessages = 100

	// idlePollInterval is how long to wait after an empty Pull before asking
	// again. Synchronous pull returns promptly when the backlog is empty, so
	// without this the loop spins against the API.
	idlePollInterval = 5 * time.Second

	// pullBackoffInitial and pullBackoffMax bound the exponential backoff
	// applied after a failed Pull. The ceiling is deliberately shorter than
	// the subscription's own message retention: a detector that backs off for
	// longer than Pub/Sub retains would lose drift rather than delay it.
	pullBackoffInitial = 1 * time.Second
	pullBackoffMax     = 60 * time.Second

	// pullBackoffMultiplier is the growth factor between retries.
	pullBackoffMultiplier = 2

	// nackAckDeadlineSeconds is the deadline set on a message being returned
	// to the subscription. Zero means "redeliver now", after which the
	// subscription's own retry_policy backoff applies.
	nackAckDeadlineSeconds = 0

	// ackDeadlineSecondsField names that field for ForceSendFields, which takes
	// the Go field name rather than the JSON one.
	ackDeadlineSecondsField = "AckDeadlineSeconds"

	// subscriptionPathFormat builds the fully qualified subscription name the
	// API expects from a project and a bare subscription id.
	subscriptionPathFormat = "projects/%s/subscriptions/%s"

	// subscriptionPathPrefix marks a --subscription value that is already
	// fully qualified. The drift-pubsub module's subscription_id output is
	// that form and its README tells the operator to feed it to this flag, so
	// prefixing unconditionally would build
	// projects/P/subscriptions/projects/P/subscriptions/name and pull a
	// subscription that does not exist -- which surfaces as an empty log,
	// indistinguishable from no drift. The gateway's Python adapter accepts
	// either form for the same reason.
	subscriptionPathPrefix = "projects/"

	// settleGracePeriod bounds the ack and nack calls issued while shutting
	// down. The pull loop's context is already cancelled by then, so settling
	// on it would abort: up to maxMessages records would be handled and then
	// redelivered to the next instance, which at T4 is a duplicate inject per
	// restart.
	settleGracePeriod = 10 * time.Second
)

// receivedMessage is one Pub/Sub message with its payload already decoded.
// The REST API delivers the body base64-encoded; nothing downstream should
// have to know that.
type receivedMessage struct {
	AckID string
	Data  []byte
}

// messageSource is the subset of Pub/Sub the loop needs. An interface so the
// loop can be tested without a subscription -- the alternative is an emulator,
// and the loop's branching (ack, skip, nack, back off) is what needs covering,
// not the transport.
type messageSource interface {
	Pull(ctx context.Context, maxMessages int64) ([]receivedMessage, error)
	Ack(ctx context.Context, ackIDs []string) error
	Nack(ctx context.Context, ackIDs []string) error
}

// pubsubSource implements messageSource against the Pub/Sub REST API.
//
// The REST client is used rather than cloud.google.com/go/pubsub because
// google.golang.org/api is already a direct dependency of this module, for the
// GKE Container API the event watcher calls. What that costs is StreamingPull:
// synchronous pull does not extend the ack deadline on a message being worked,
// and has no built-in flow control. Neither matters here, because the detector
// acks on parse -- a sub-millisecond step, against a 60-second deadline -- and
// pulls a bounded batch at a time. If the ack ever moves to after the
// managedFields join in T3, revisit this.
type pubsubSource struct {
	service      *pubsub.Service
	subscription string
}

// newPubsubSource builds a source for a subscription, using Application
// Default Credentials -- inside the agent pod, the Workload Identity the
// drift-pubsub module granted roles/pubsub.subscriber to.
func newPubsubSource(ctx context.Context, project, subscription string) (*pubsubSource, error) {
	service, err := pubsub.NewService(ctx)
	if err != nil {
		return nil, fmt.Errorf("pubsub client: %w", err)
	}
	return &pubsubSource{
		service:      service,
		subscription: subscriptionPath(project, subscription),
	}, nil
}

// subscriptionPath qualifies a bare subscription id with its project, and
// passes an already-qualified one through. Both forms reach this binary: the
// drift-pubsub module's README hands the operator its subscription_id output,
// which is fully qualified, while the default and the Helm values carry the
// bare name.
func subscriptionPath(project, subscription string) string {
	if strings.HasPrefix(subscription, subscriptionPathPrefix) {
		return subscription
	}
	return fmt.Sprintf(subscriptionPathFormat, project, subscription)
}

func (p *pubsubSource) Pull(ctx context.Context, maxMessages int64) ([]receivedMessage, error) {
	req := &pubsub.PullRequest{MaxMessages: maxMessages}
	resp, err := p.service.Projects.Subscriptions.Pull(p.subscription, req).Context(ctx).Do()
	if err != nil {
		return nil, fmt.Errorf("pull %s: %w", p.subscription, err)
	}

	out := make([]receivedMessage, 0, len(resp.ReceivedMessages))
	for _, rm := range resp.ReceivedMessages {
		if rm.Message == nil {
			continue
		}
		data, err := base64.StdEncoding.DecodeString(rm.Message.Data)
		if err != nil {
			// Undecodable at the transport layer, before the detector has seen
			// a payload at all. Returning it as a message with nil Data lets
			// the loop nack it through the same path as a parse failure.
			log.Printf("drift-detector: message %s: base64 decode: %v", rm.Message.MessageId, err)
			out = append(out, receivedMessage{AckID: rm.AckId})
			continue
		}
		out = append(out, receivedMessage{AckID: rm.AckId, Data: data})
	}
	return out, nil
}

func (p *pubsubSource) Ack(ctx context.Context, ackIDs []string) error {
	if len(ackIDs) == 0 {
		return nil
	}
	req := &pubsub.AcknowledgeRequest{AckIds: ackIDs}
	if _, err := p.service.Projects.Subscriptions.Acknowledge(p.subscription, req).Context(ctx).Do(); err != nil {
		return fmt.Errorf("acknowledge %d message(s): %w", len(ackIDs), err)
	}
	return nil
}

func (p *pubsubSource) Nack(ctx context.Context, ackIDs []string) error {
	if len(ackIDs) == 0 {
		return nil
	}
	req := &pubsub.ModifyAckDeadlineRequest{
		AckIds:             ackIDs,
		AckDeadlineSeconds: nackAckDeadlineSeconds,
		// The generated client omits zero-valued scalars from the request body
		// unless they are named here, and the deadline we want is zero. Without
		// this the field is absent, the server keeps the subscription's own
		// ackDeadlineSeconds, and a nacked message sits invisible for a minute
		// instead of redelivering now.
		ForceSendFields: []string{ackDeadlineSecondsField},
	}
	if _, err := p.service.Projects.Subscriptions.ModifyAckDeadline(p.subscription, req).Context(ctx).Do(); err != nil {
		return fmt.Errorf("nack %d message(s): %w", len(ackIDs), err)
	}
	return nil
}

// recordHandler consumes one parsed audit record. T2 classifies, T3 enriches,
// and T4 injects behind this signature; T1 ships logRecord.
type recordHandler func(AuditRecord)

// subscriberCounts is what the loop has done since it started. Exported
// through the log on shutdown, and the shape a metrics exporter will read.
type subscriberCounts struct {
	// Parsed is records handed to the handler.
	Parsed int

	// Skipped is entries understood and acked without being handled: not a
	// Kubernetes audit record, or a call that named no object.
	Skipped int

	// Failed is entries that did not have the expected shape and were nacked.
	// A non-zero value here means the payload changed or this parser is wrong;
	// either way the messages are still on the subscription.
	Failed int
}

// subscriber is the T1 ingestion loop: pull, parse, ack on success, nack on a
// shape this detector does not understand.
type subscriber struct {
	source      messageSource
	handle      recordHandler
	maxMessages int64
	idleWait    time.Duration
	counts      subscriberCounts
}

func newSubscriber(source messageSource, handle recordHandler, maxMessages int64) *subscriber {
	if maxMessages <= 0 {
		maxMessages = defaultMaxMessages
	}
	return &subscriber{
		source:      source,
		handle:      handle,
		maxMessages: maxMessages,
		idleWait:    idlePollInterval,
	}
}

// Run pulls until the context is cancelled, returning the context's error.
func (s *subscriber) Run(ctx context.Context) error {
	backoff := pullBackoffInitial

	for {
		if err := ctx.Err(); err != nil {
			return err
		}

		messages, err := s.source.Pull(ctx, s.maxMessages)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			log.Printf("drift-detector: pull failed, retrying in %s: %v", backoff, err)
			if !sleepCtx(ctx, backoff) {
				return ctx.Err()
			}
			backoff = nextBackoff(backoff)
			continue
		}
		backoff = pullBackoffInitial

		if len(messages) == 0 {
			if !sleepCtx(ctx, s.idleWait) {
				return ctx.Err()
			}
			continue
		}

		s.processBatch(ctx, messages)
	}
}

// processBatch parses each message and settles the whole batch in two calls,
// rather than one round trip per message.
func (s *subscriber) processBatch(ctx context.Context, messages []receivedMessage) {
	ackIDs := make([]string, 0, len(messages))
	nackIDs := make([]string, 0)

	skipped := 0
	var firstSkip error

	for _, msg := range messages {
		record, err := parseAuditEntry(msg.Data)
		switch {
		case err == nil:
			s.counts.Parsed++
			s.handle(record)
			ackIDs = append(ackIDs, msg.AckID)

		case errors.Is(err, errNotKubernetesAudit), errors.Is(err, errNoResourceName):
			// Understood and not actionable. Acking is the point: leaving these
			// on the subscription would redeliver them until retention expired.
			s.counts.Skipped++
			skipped++
			if firstSkip == nil {
				firstSkip = err
			}
			ackIDs = append(ackIDs, msg.AckID)

		default:
			// A shape this detector does not understand. Nack so it redelivers
			// and the parse-failure count moves, rather than acking drift away.
			s.counts.Failed++
			log.Printf("drift-detector: parse failed, message returned to the subscription: %v", err)
			nackIDs = append(nackIDs, msg.AckID)
		}
	}

	// One line per batch rather than one per message: a misconfigured sink can
	// make every message a skip, and the point is that dropping is visible, not
	// that each drop is. Silence here is what would make a sink filter that
	// stopped matching Kubernetes audit look identical to a quiet cluster.
	if skipped > 0 {
		log.Printf("drift-detector: acked and dropped %d of %d message(s) as not actionable; first: %v",
			skipped, len(messages), firstSkip)
	}

	settleCtx, cancel := s.settleContext(ctx)
	defer cancel()

	if err := s.source.Ack(settleCtx, ackIDs); err != nil {
		log.Printf("drift-detector: %v", err)
	}
	if err := s.source.Nack(settleCtx, nackIDs); err != nil {
		log.Printf("drift-detector: %v", err)
	}
}

// settleContext returns the context the ack and nack calls run on. It survives
// cancellation of the pull loop's context, because that is exactly when
// settling matters: on SIGTERM the loop's context is already cancelled, and
// settling on it would abort every call, leaving a whole batch of already
// handled records to be redelivered to the next instance.
func (s *subscriber) settleContext(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.WithoutCancel(ctx), settleGracePeriod)
}

// Counts reports what the loop has settled so far.
func (s *subscriber) Counts() subscriberCounts {
	return s.counts
}

// logRecord is T1's handler: it writes the parsed record to the structured log
// and does nothing else. T1's acceptance criterion is that a kubectl patch
// shows up here, with all five fields populated and the resource path
// decomposed.
func logRecord(record AuditRecord) {
	// project and location are logged alongside the cluster name because a
	// cluster name is only unique within a project and a location -- the fleet
	// ambiguity AuditRecord's comment describes. Reading the log without them
	// cannot tell two same-named clusters apart, which is the mistake T2's
	// classification would then inherit.
	log.Printf("drift-detector: audit cluster=%s project=%s location=%s principal=%q verb=%s method=%s resource=%s group=%q version=%s namespace=%q name=%q subresource=%q user_agent=%q timestamp=%s insert_id=%s",
		record.Cluster,
		record.Project,
		record.Location,
		record.Principal,
		record.Verb,
		record.MethodName,
		record.Resource.String(),
		record.Resource.Group,
		record.Resource.Version,
		record.Resource.Namespace,
		record.Resource.Name,
		record.Resource.Subresource,
		record.UserAgent,
		record.Timestamp.Format(time.RFC3339),
		record.InsertID,
	)
}

// nextBackoff grows the retry delay up to the ceiling.
func nextBackoff(current time.Duration) time.Duration {
	next := current * pullBackoffMultiplier
	if next > pullBackoffMax {
		return pullBackoffMax
	}
	return next
}

// sleepCtx waits for d, reporting false if the context was cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}
