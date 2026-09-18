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
	// post-sink stream measured about 0.7 messages a second on a two-cluster
	// project and up to 10 a second on three busier ones, so a round trip
	// drains somewhere between ten seconds and two minutes of backlog -- and
	// stays far below the API's 1000 ceiling at either end.
	defaultMaxMessages = 100

	// idlePollInterval is how long to wait after an empty Pull before asking
	// again. Synchronous pull returns promptly when the backlog is empty, so
	// without this the loop spins against the API.
	idlePollInterval = 5 * time.Second

	// idleReportInterval is how long the loop goes delivering nothing before it
	// says so. Without it a detector whose subscription is empty is completely
	// silent: an empty Pull is not an error, so there is no retry line, no
	// batch-skip line, and no progress line either, because the progress line
	// is driven from driftFilter.Handle and a record that never arrives never
	// reaches it. A Log Router sink whose filter stopped matching therefore
	// reads exactly like a fleet nobody is changing -- and it is the failure
	// this whole pipeline is least able to notice, because "no drift" is the
	// expected steady state.
	//
	// Deliberately the same length as countsLogMaxInterval: between the two,
	// the pod logs something every fifteen minutes in every state it can be in,
	// which is the property an operator actually wants and neither delivers
	// alone.
	idleReportInterval = countsLogMaxInterval

	// unattributedListSeparator joins the principal names in a progress line.
	// A comma and a space rather than a space alone, because an entry is
	// "principal=count" and unattributedOverflowLabel contains spaces.
	unattributedListSeparator = ", "

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

	// defaultBatchJoinBudget bounds the total time one batch may spend handling
	// records, which since T3 means the live-object lookups the join performs.
	//
	// It exists because joinRequestTimeout bounds one lookup and the batch
	// settles only after every record in it has been handled: at the 100-message
	// default a batch of slow lookups would run for far longer than any ack
	// deadline, and Pub/Sub would redeliver the whole batch while this process
	// was still working on it -- producing duplicate drift lines, and at T4 a
	// duplicate inject per cycle, for as long as the control plane stayed slow.
	//
	// Thirty seconds is half the drift-pubsub module's 60-second deadline,
	// leaving the rest for the Ack round trip. It is the default rather than the
	// value because the deadline it is sized against is a Terraform variable and
	// this is a Go constant: an operator pointing the detector at a subscription
	// created outside the module gets Pub/Sub's own 10-second default, and
	// --batch-join-budget is how they bring the two back into line without
	// rebuilding the image.
	//
	// Exceeding it is not an error. The remaining lookups fail fast against the
	// expired context and their records are forwarded unenriched and counted,
	// which is the same fail-open stance the join takes everywhere else.
	defaultBatchJoinBudget = 30 * time.Second

	// batchJoinBudgetCeiling is the largest budget worth accepting. Pub/Sub's
	// own maximum ackDeadlineSeconds is 600, and a budget at or above the whole
	// deadline guarantees the redelivery it exists to prevent, so the ceiling
	// sits at half of it -- the same relationship the default has to the
	// module's 60.
	batchJoinBudgetCeiling = 300 * time.Second

	// ackDeadlineProbeTimeout bounds the one subscriptions.get made at startup
	// to read the deadline the budget has to fit inside. Short because the probe
	// is advisory: its only output is a log line, and a detector that waited on
	// a slow control plane to print one would delay the first batch for nothing.
	ackDeadlineProbeTimeout = 10 * time.Second

	// maxBudgetShareOfAckDeadline is the largest share of the ack deadline a
	// batch's join budget should take, as a divisor. Pub/Sub starts the deadline
	// at delivery and the batch is settled only once its Ack returns, so the
	// remainder is what that round trip runs in -- and a budget equal to the
	// whole deadline has already overrun by the time the ack is sent. Half is
	// the relationship defaultBatchJoinBudget is sized on, 30s against the
	// drift-pubsub module's 60.
	maxBudgetShareOfAckDeadline = 2

	// settleGracePeriod bounds the ack and nack calls issued while shutting
	// down. The pull loop's context is already cancelled by then, so settling
	// on it would abort: up to maxMessages records would be handled and then
	// redelivered to the next instance, which at T4 is a duplicate inject per
	// restart.
	settleGracePeriod = 10 * time.Second
)

// ackDeadlineWarning says why a join budget does not fit a subscription's ack
// deadline, or returns "" when it does. Split from the probe that fetches the
// deadline so the arithmetic is testable without a subscription.
func ackDeadlineWarning(budget, deadline time.Duration) string {
	// No deadline reported. Nothing to compare against, and substituting a
	// default here would warn about a number nobody configured.
	if deadline <= 0 {
		return ""
	}
	safe := deadline / maxBudgetShareOfAckDeadline
	if budget <= safe {
		return ""
	}
	// "may", not "will". The threshold is half the deadline but redelivery needs
	// the handling *and* the Ack round trip to exceed the whole of it, so a 40s
	// budget against a 60s deadline trips this and will not actually redeliver.
	// What the budget has spent is the margin, and an operator told a margin was
	// a certainty lowers it and loses enrichment to avoid a problem they do not
	// have.
	return fmt.Sprintf("--batch-join-budget=%s leaves too little of the subscription's %s ack deadline for the batch to be acked in, so Pub/Sub may redeliver batches this process is still working on; pass %s or less, or raise the subscription's ackDeadlineSeconds", budget, deadline, safe)
}

// ackDeadlinePreflight reads the subscription's own ack deadline through read
// and returns the one line to log about it: that the budget does not fit, or
// that the deadline could not be read, or "" when it fits and there is nothing
// to say.
//
// Split out of realMain rather than inlined there because the two ways this
// check dies are both silent. A conversion bug in the reader warns every install
// on every boot about a deadline nobody configured, and deleting the comparison
// turns the check off for everybody; neither reddens anything while the block
// sits in a function no test can call. Taking the reader as a function rather
// than a messageSource keeps that reachable without widening the interface for
// one caller.
func ackDeadlinePreflight(ctx context.Context, read func(context.Context) (time.Duration, error), budget time.Duration) string {
	probeCtx, cancel := context.WithTimeout(ctx, ackDeadlineProbeTimeout)
	defer cancel()
	deadline, err := read(probeCtx)
	if err != nil {
		return fmt.Sprintf("could not read the subscription's ack deadline (%v); --batch-join-budget=%s is unchecked against it", err, budget)
	}
	return ackDeadlineWarning(budget, deadline)
}

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
// and has no built-in flow control.
//
// The ack now follows the managedFields join rather than the parse, so the first
// of those does bite: a batch holds its messages for as long as its lookups take,
// with nothing extending the deadline underneath it. What keeps that bounded is
// the joinBudget below (--batch-join-budget), which caps the whole batch at half
// the module's ack deadline by default and lets the remaining lookups fail open
// rather than overrun. Flow control is
// still bounded by pulling maxMessages at a time and never overlapping batches.
//
// Moving to StreamingPull would replace that cap with deadline extension, and
// becomes worth the dependency if handling ever grows past what one budget can
// hold -- a per-record inject at T4, or a fan-in doing several clusters' lookups
// per record.
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

// AckDeadline reads the subscription's configured ack deadline, so the caller
// can check --batch-join-budget against the install it is actually pointed at
// rather than against the compile-time ceiling. Advisory: the caller logs what
// comes back and pulls either way, because roles/pubsub.subscriber alone does
// not carry subscriptions.get and a detector whose IAM stops at the pull still
// has to run.
func (p *pubsubSource) AckDeadline(ctx context.Context) (time.Duration, error) {
	sub, err := p.service.Projects.Subscriptions.Get(p.subscription).Context(ctx).Do()
	if err != nil {
		return 0, fmt.Errorf("get %s: %w", p.subscription, err)
	}
	return time.Duration(sub.AckDeadlineSeconds) * time.Second, nil
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

// recordHandler consumes one parsed audit record. T4 injects behind this
// signature; what ships behind it today is driftFilter.Handle, which classifies
// and then forwards what survives to the T3 join.
//
// The context is derived from the pull loop's, so a handler doing network I/O
// -- which the join does, one lookup per forwarded record -- is interrupted by
// SIGTERM rather than holding shutdown open for its timeout. It is deliberately
// not the settle context: an in-flight lookup abandoned at shutdown leaves its
// message acked and its drift unreported, which matches what realMain already
// documents about an interrupted batch, and is preferable to delaying the ack of
// every other message in the batch behind it.
//
// Derived rather than passed through, because processBatch also puts the batch's
// join budget on it. A handler is therefore cut short by whichever comes first,
// and the budget is the one that fires in ordinary running: it is shared by the
// whole batch, so the last record of a slow batch can be handed a context that
// is already close to expiry. A handler that treats a deadline as a bug rather
// than as the ordinary end of its turn will be wrong most of the time it fires.
type recordHandler func(context.Context, AuditRecord)

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

	// idleReport is how long the loop tolerates delivering nothing before
	// logging that fact. A field rather than the constant read directly, so a
	// test can drive the branch without waiting a quarter of an hour.
	idleReport time.Duration

	// joinBudget bounds one batch's handling. Unlike idleReport it is a
	// constructor argument rather than a field a test pokes, because realMain
	// fills it from --batch-join-budget: the ack deadline it has to fit inside
	// is a Terraform variable, so the value cannot be fixed at compile time.
	// Taking it here makes dropping that wiring a compile error instead of a
	// binary that prints the requested budget and runs on the default.
	joinBudget time.Duration

	// now is the clock idleReport is measured against. nil means time.Now.
	now func() time.Time
}

// newSubscriber builds the loop. joinBudget is not defaulted when it is zero or
// negative, the way maxMessages is: realMain rejects those at startup, so the
// only caller that can pass one is a test, and a zero that silently became
// thirty seconds would hide exactly the wiring bug this argument exists to
// prevent.
func newSubscriber(source messageSource, handle recordHandler, maxMessages int64, joinBudget time.Duration) *subscriber {
	if maxMessages <= 0 {
		maxMessages = defaultMaxMessages
	}
	return &subscriber{
		source:      source,
		handle:      handle,
		maxMessages: maxMessages,
		idleWait:    idlePollInterval,
		idleReport:  idleReportInterval,
		joinBudget:  joinBudget,
	}
}

// clock reads the loop's injectable time source.
func (s *subscriber) clock() time.Time {
	if s.now == nil {
		return time.Now()
	}
	return s.now()
}

// Run pulls until the context is cancelled, returning the context's error.
//
// lastDelivery starts at the current time rather than the zero value, so the
// first idle line is owed idleReportInterval after start-up instead of on the
// first empty pull. The startup line has already said the loop is alive; the
// idle line's job is to keep saying it.
func (s *subscriber) Run(ctx context.Context) error {
	backoff := pullBackoffInitial
	lastDelivery := s.clock()

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
			// Reset on report rather than only on delivery, so a subscription
			// that stays empty says so every interval instead of once.
			if now := s.clock(); now.Sub(lastDelivery) >= s.idleReport {
				logIdle(s.idleReport, s.counts)
				lastDelivery = now
			}
			if !sleepCtx(ctx, s.idleWait) {
				return ctx.Err()
			}
			continue
		}

		s.processBatch(ctx, messages)
		// After the batch, not before: a subscription delivering steadily must
		// never report itself idle, and settling a full batch is the slowest
		// step in the loop. Reading the clock on the near side would start the
		// interval before the work rather than at the end of it.
		lastDelivery = s.clock()
	}
}

// processBatch parses each message and settles the whole batch in two calls,
// rather than one round trip per message.
func (s *subscriber) processBatch(ctx context.Context, messages []receivedMessage) {
	ackIDs := make([]string, 0, len(messages))
	nackIDs := make([]string, 0)

	skipped := 0
	var firstSkip error

	// The whole batch's handling shares one budget, so no batch can hold its
	// messages past the ack deadline however slow the cluster is. Derived from
	// the pull loop's context rather than replacing it: a SIGTERM still cuts the
	// batch short.
	handleCtx, cancelHandle := context.WithTimeout(ctx, s.joinBudget)
	defer cancelHandle()

	for _, msg := range messages {
		record, err := parseAuditEntry(msg.Data)
		switch {
		case err == nil:
			s.counts.Parsed++
			s.handle(handleCtx, record)
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

// logCountsProgress reports the running tally every countsLogInterval records,
// so that the measurement survives a pod that is killed rather than stopped and
// so that a detector seeing no human changes still says it is alive.
func logCountsProgress(handled int, counts TierCounts, unattributed []string) {
	if len(unattributed) == 0 {
		log.Printf("%s: progress handled=%d (%s)", commandName, handled, counts)
		return
	}
	log.Printf("%s: progress handled=%d (%s) unattributed_principals=[%s]",
		commandName, handled, counts, strings.Join(unattributed, unattributedListSeparator))
}

// logIdle reports that the subscription has delivered nothing for a while.
//
// The running totals go out with it because they are what separates the two
// cases an operator has to tell apart: a detector that has been working and
// has gone quiet carries non-zero counts, while one whose sink or subscription
// was never wired up correctly reports zeroes and has done since it started.
// Without them the line says the process is alive, which is the less useful
// half of the question.
func logIdle(interval time.Duration, counts subscriberCounts) {
	log.Printf("%s: idle, no messages delivered in %s (parsed=%d skipped=%d failed=%d)",
		commandName, interval, counts.Parsed, counts.Skipped, counts.Failed)
}

// logDroppedRecord reports one filtered record, behind --log-dropped. The tier
// and the reason are both given because they answer different questions: the
// tier is what the principal was taken to be, the reason is why that meant no
// inject. A record dropped for a failed call carries the tier it would have
// had, which is what makes "my change was rejected" distinguishable from "my
// change was classified as automation".
func logDroppedRecord(record AuditRecord, tier Tier, reason string) {
	log.Printf("drift-detector: dropped tier=%s reason=%q cluster=%s principal=%q verb=%s resource=%s insert_id=%s",
		tier,
		reason,
		record.Cluster,
		record.Principal,
		record.Verb,
		record.Resource.String(),
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
