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
	"bytes"
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/api/option"
	"google.golang.org/api/pubsub/v1"
)

// syncBuffer collects log output written from the goroutine running Run while
// the test goroutine reads it. A bare bytes.Buffer races under -race here,
// because log.SetOutput hands the writer to whichever goroutine is logging.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (s *syncBuffer) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.Write(p)
}

func (s *syncBuffer) String() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.buf.String()
}

// fakeSource stands in for the subscription. It hands out one batch per Pull
// and records how each message was settled.
type fakeSource struct {
	batches [][]receivedMessage
	pulls   int
	pullErr error

	acked  []string
	nacked []string

	// recordCtxErr captures the context state each settle call was made on, so
	// a test can assert that shutdown does not abort them.
	recordCtxErr bool
	ackCtxErr    error
	nackCtxErr   error
}

func (f *fakeSource) Pull(ctx context.Context, maxMessages int64) ([]receivedMessage, error) {
	f.pulls++
	if f.pullErr != nil {
		return nil, f.pullErr
	}
	if len(f.batches) == 0 {
		return nil, nil
	}
	batch := f.batches[0]
	f.batches = f.batches[1:]
	return batch, nil
}

func (f *fakeSource) Ack(ctx context.Context, ackIDs []string) error {
	if f.recordCtxErr && len(ackIDs) > 0 {
		f.ackCtxErr = ctx.Err()
	}
	f.acked = append(f.acked, ackIDs...)
	return nil
}

func (f *fakeSource) Nack(ctx context.Context, ackIDs []string) error {
	if f.recordCtxErr && len(ackIDs) > 0 {
		f.nackCtxErr = ctx.Err()
	}
	f.nacked = append(f.nacked, ackIDs...)
	return nil
}

const (
	notKubernetesEntry = `{"protoPayload":{"serviceName":"compute.googleapis.com","resourceName":"projects/x/zones/y/instances/z"}}`
	malformedEntry     = `{"protoPayload": not json`
)

// The whole point of T1's settle logic: a record that parses is handled and
// acked, one that is understood but not ours is acked without being handled,
// and one that does not parse is nacked so it comes back.
func TestProcessBatchSettlesEachOutcome(t *testing.T) {
	source := &fakeSource{}
	var handled []AuditRecord
	sub := newSubscriber(source, func(_ context.Context, r AuditRecord) { handled = append(handled, r) }, defaultMaxMessages, defaultBatchJoinBudget)

	sub.processBatch(context.Background(), []receivedMessage{
		{AckID: "ack-parsed", Data: []byte(humanPatchEntry)},
		{AckID: "ack-skipped", Data: []byte(notKubernetesEntry)},
		{AckID: "ack-failed", Data: []byte(malformedEntry)},
	})

	if len(handled) != 1 {
		t.Fatalf("handler called %d time(s), want 1", len(handled))
	}
	if handled[0].Principal != "engineer@example.com" {
		t.Errorf("handled record principal = %q, want engineer@example.com", handled[0].Principal)
	}

	wantAcked := []string{"ack-parsed", "ack-skipped"}
	if !equalStrings(source.acked, wantAcked) {
		t.Errorf("acked = %v, want %v", source.acked, wantAcked)
	}
	wantNacked := []string{"ack-failed"}
	if !equalStrings(source.nacked, wantNacked) {
		t.Errorf("nacked = %v, want %v", source.nacked, wantNacked)
	}

	want := subscriberCounts{Parsed: 1, Skipped: 1, Failed: 1}
	if got := sub.Counts(); got != want {
		t.Errorf("counts = %+v, want %+v", got, want)
	}
}

// A message that could not be base64-decoded arrives with nil Data. It must
// nack like any other unparseable payload rather than being acked away.
func TestProcessBatchNacksUndecodableMessage(t *testing.T) {
	source := &fakeSource{}
	sub := newSubscriber(source, func(context.Context, AuditRecord) {}, defaultMaxMessages, defaultBatchJoinBudget)

	sub.processBatch(context.Background(), []receivedMessage{{AckID: "ack-nil"}})

	if len(source.acked) != 0 {
		t.Errorf("acked = %v, want nothing acked", source.acked)
	}
	if !equalStrings(source.nacked, []string{"ack-nil"}) {
		t.Errorf("nacked = %v, want [ack-nil]", source.nacked)
	}
	if got := sub.Counts().Failed; got != 1 {
		t.Errorf("failed count = %d, want 1", got)
	}
}

// On SIGTERM the loop's context is already cancelled by the time the batch it
// was working on has to be settled. Settling on that context would abort every
// ack, and the whole batch -- already handled -- would be redelivered to the
// next instance. At T4 that is a duplicate inject on every restart.
func TestProcessBatchSettlesAfterContextCancelled(t *testing.T) {
	source := &fakeSource{recordCtxErr: true}
	sub := newSubscriber(source, func(context.Context, AuditRecord) {}, defaultMaxMessages, defaultBatchJoinBudget)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	sub.processBatch(ctx, []receivedMessage{
		{AckID: "ack-parsed", Data: []byte(humanPatchEntry)},
		{AckID: "ack-failed", Data: []byte(malformedEntry)},
	})

	if !equalStrings(source.acked, []string{"ack-parsed"}) {
		t.Errorf("acked = %v, want [ack-parsed]", source.acked)
	}
	if !equalStrings(source.nacked, []string{"ack-failed"}) {
		t.Errorf("nacked = %v, want [ack-failed]", source.nacked)
	}
	if source.ackCtxErr != nil {
		t.Errorf("Ack ran on a cancelled context (%v); it must survive shutdown", source.ackCtxErr)
	}
	if source.nackCtxErr != nil {
		t.Errorf("Nack ran on a cancelled context (%v); it must survive shutdown", source.nackCtxErr)
	}
}

// Synchronous pull does not extend the ack deadline while a handler runs, and
// since T3 the handler does a network lookup per record. Without a bound on the
// batch, a slow control plane holds a hundred messages past the deadline and
// Pub/Sub redelivers the batch this process is still working on -- duplicate
// drift lines now, a duplicate inject per cycle at T4. The guard is the deadline
// on the context the handler is given, so that is what this asserts: not that
// the lookups are fast, but that they cannot run unbounded.
func TestProcessBatchBoundsHandlingWithABudget(t *testing.T) {
	source := &fakeSource{}
	var deadline time.Time
	var ok bool
	sub := newSubscriber(source, func(ctx context.Context, _ AuditRecord) {
		deadline, ok = ctx.Deadline()
	}, defaultMaxMessages, defaultBatchJoinBudget)

	start := time.Now()
	sub.processBatch(context.Background(), []receivedMessage{{AckID: "ack-parsed", Data: []byte(humanPatchEntry)}})
	after := time.Now()

	if !ok {
		t.Fatal("the handler's context carries no deadline; a slow batch would run past the ack deadline")
	}
	// Bounded from both ends, and exactly, with no tolerance. processBatch sets
	// the deadline from its own clock reading, which lies somewhere in [start,
	// after]: so the distance from start always exceeds the budget by however
	// long parsing took, and the distance from after never reaches it. Measuring
	// the upper bound from after is what makes it hold by construction -- from
	// start it fails by microseconds, which is the bug this pair replaces, and
	// papering over that with a tolerance would let a subscriber inflate the
	// budget by most of a second and still pass.
	if left := deadline.Sub(after); left > defaultBatchJoinBudget {
		t.Errorf("handler deadline is %s past the call, want at most %s", left, defaultBatchJoinBudget)
	}
	if left := deadline.Sub(start); left <= 0 {
		t.Errorf("handler deadline is %s away, want a deadline in the future", left)
	}
}

// The budget is derived from the pull loop's context rather than replacing it.
// A context.WithTimeout(context.Background(), ...) would compile, pass the test
// above, and quietly make SIGTERM wait out the full budget on every in-flight
// batch.
func TestProcessBatchBudgetStillHonoursShutdown(t *testing.T) {
	source := &fakeSource{recordCtxErr: true}
	var handlerErr error
	sub := newSubscriber(source, func(ctx context.Context, _ AuditRecord) {
		handlerErr = ctx.Err()
	}, defaultMaxMessages, defaultBatchJoinBudget)

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	sub.processBatch(ctx, []receivedMessage{{AckID: "ack-parsed", Data: []byte(humanPatchEntry)}})

	if handlerErr == nil {
		t.Error("the handler ran on a live context after SIGTERM; the budget must derive from the loop's context, not replace it")
	}
}

// What the budget must not do is drop the records it runs out of time for. It
// bounds the batch so Pub/Sub does not redeliver it; the records after the
// expiry still have to reach the drift log unenriched, be counted, and be
// acked, because a record the detector silently discarded is a human change
// nobody ever sees. This drives a real joiner rather than a stub handler, since
// fail-open is the joiner's behaviour and asserting it anywhere else would pass
// against a subscriber that threw the expired records away.
func TestProcessBatchForwardsRecordsTheBudgetRanOutOn(t *testing.T) {
	// Never closed: every lookup in this batch waits for the context instead,
	// which is what an unreachable control plane does to a batch.
	blocked := make(chan struct{})
	getter := &stubGetter{block: blocked}
	// humanPatchEntry's cluster, so the records reach the lookup rather than
	// being gated out as another cluster's.
	identity := clusterIdentity{Project: "example-project", Location: "us-central1", Cluster: "prod-1"}
	var forwarded []DriftEvent
	join := newJoiner(getter, identity, nil, func(_ context.Context, e DriftEvent) {
		forwarded = append(forwarded, e)
	})
	// The joiner's own per-request timeout has to be the slower of the two, or
	// it, and not the budget, is what ends the blocked lookup -- the batch would
	// then come out failed-open whether or not processBatch honoured the budget
	// at all, and the elapsed assertion below is what notices the difference.
	join.timeout = time.Minute

	source := &fakeSource{}
	filter := newDriftFilter(NewClassifier("", ""), join.Handle, false)
	sub := newSubscriber(source, filter.Handle, defaultMaxMessages, time.Millisecond)

	start := time.Now()
	sub.processBatch(context.Background(), []receivedMessage{
		{AckID: "ack-first", Data: []byte(humanPatchEntry)},
		{AckID: "ack-second", Data: []byte(humanPatchEntry)},
	})
	elapsed := time.Since(start)

	// A thousand times the budget and a thirtieth of the default: wide enough
	// that a loaded CI runner does not redden it, narrow enough that a batch
	// falling back to defaultBatchJoinBudget cannot pass.
	if elapsed > time.Second {
		t.Errorf("processBatch took %s for a 1ms budget; the budget argument is not reaching the handler's context", elapsed)
	}
	if len(forwarded) != 2 {
		t.Fatalf("forwarded %d events, want 2: an expired budget must fail open, not drop", len(forwarded))
	}
	for i, e := range forwarded {
		if e.Outcome != joinFailed {
			t.Errorf("forwarded[%d].Outcome = %q, want %q", i, e.Outcome, joinFailed)
		}
	}
	if counts := join.Counts(); counts.Failed != 2 {
		t.Errorf("join counts = %s, want failed=2", counts)
	}
	if want := []string{"ack-first", "ack-second"}; !equalStrings(source.acked, want) {
		t.Errorf("acked = %v, want %v: an expired budget must not leave records for redelivery", source.acked, want)
	}
	if got := sub.Counts(); got.Parsed != 2 {
		t.Errorf("subscriber counts = %+v, want parsed=2", got)
	}
}

func TestRunStopsOnContextCancel(t *testing.T) {
	source := &fakeSource{batches: [][]receivedMessage{
		{{AckID: "ack-1", Data: []byte(humanPatchEntry)}},
	}}
	sub := newSubscriber(source, func(context.Context, AuditRecord) {}, defaultMaxMessages, defaultBatchJoinBudget)
	sub.idleWait = time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- sub.Run(ctx) }()

	// Let the loop drain the batch and reach its idle poll, then stop it.
	time.Sleep(20 * time.Millisecond)
	cancel()

	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Errorf("Run returned %v, want context.Canceled", err)
		}
	case <-time.After(time.Second):
		t.Fatal("Run did not return within a second of cancellation")
	}

	if got := sub.Counts().Parsed; got != 1 {
		t.Errorf("parsed = %d, want 1", got)
	}
	if !equalStrings(source.acked, []string{"ack-1"}) {
		t.Errorf("acked = %v, want [ack-1]", source.acked)
	}
}

// A failing subscription must not spin: the loop backs off and stays
// cancellable while it waits.
func TestRunBacksOffOnPullError(t *testing.T) {
	source := &fakeSource{pullErr: errors.New("subscription unavailable")}
	sub := newSubscriber(source, func(context.Context, AuditRecord) {}, defaultMaxMessages, defaultBatchJoinBudget)

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()

	if err := sub.Run(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Errorf("Run returned %v, want context.DeadlineExceeded", err)
	}

	// pullBackoffInitial is a second, so within 50ms the loop gets one attempt
	// and then waits. More than a couple means it is spinning.
	if source.pulls > 2 {
		t.Errorf("pulled %d times while backing off, want at most 2", source.pulls)
	}
}

// TestRunReportsAnIdleSubscription pins the only signal a detector gives when
// its subscription delivers nothing at all. An empty Pull is not an error, so
// there is no retry line; no message means no batch-skip line; and the progress
// line is driven from driftFilter.Handle, which a record that never arrives
// never reaches. Without this branch a Log Router sink whose filter stopped
// matching reads in the pod log exactly like a fleet nobody is changing, which
// is the steady state -- so nobody looks.
func TestRunReportsAnIdleSubscription(t *testing.T) {
	var buf syncBuffer
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })

	sub := newSubscriber(&fakeSource{}, func(context.Context, AuditRecord) {}, defaultMaxMessages, defaultBatchJoinBudget)
	sub.idleWait = time.Millisecond

	// A clock that jumps a whole interval per reading, so the branch is reached
	// without the test waiting a quarter of an hour. Read only from the loop's
	// goroutine, so the counter needs no lock.
	ticks := 0
	sub.now = func() time.Time {
		ticks++
		return time.Unix(0, 0).Add(time.Duration(ticks) * idleReportInterval)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- sub.Run(ctx) }()

	logged := false
	for i := 0; i < 2000 && !logged; i++ {
		logged = strings.Contains(buf.String(), "idle, no messages delivered")
		if !logged {
			time.Sleep(time.Millisecond)
		}
	}
	cancel()
	<-done

	if !logged {
		t.Fatalf("no idle line after the interval elapsed; log = %q", buf.String())
	}
	// The totals are what separate "working, and quiet right now" from "never
	// received anything since it started", which is the case worth paging on.
	if !strings.Contains(buf.String(), "parsed=0 skipped=0 failed=0") {
		t.Errorf("idle line does not carry the running totals; log = %q", buf.String())
	}
}

// TestRunDoesNotReportIdleBeforeTheInterval guards the other side: the line is
// owed once an interval has passed, not on every empty poll. idlePollInterval
// is five seconds, so an unguarded report would be 17,000 lines a day saying
// nothing.
func TestRunDoesNotReportIdleBeforeTheInterval(t *testing.T) {
	var buf syncBuffer
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })

	sub := newSubscriber(&fakeSource{}, func(context.Context, AuditRecord) {}, defaultMaxMessages, defaultBatchJoinBudget)
	sub.idleWait = time.Millisecond

	// A clock that never advances: the loop polls repeatedly and no interval
	// ever elapses.
	frozen := time.Unix(0, 0)
	sub.now = func() time.Time { return frozen }

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := sub.Run(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Run returned %v, want context.DeadlineExceeded", err)
	}

	if strings.Contains(buf.String(), "idle") {
		t.Errorf("idle line printed before the interval elapsed; log = %q", buf.String())
	}
}

// TestRunIdleIntervalRestartsAfterADelivery is the case a mutation of the two
// tests above slips through: both use a subscription that delivers nothing, so
// neither notices if Run forgets to reset the interval once a batch arrives.
// Forgotten, the clock still runs from start-up, and a detector that is pulling
// records perfectly well announces itself idle a quarter of an hour in -- a
// false statement in the log, and worse than the silence it replaced, because
// it is the line an operator would act on.
func TestRunIdleIntervalRestartsAfterADelivery(t *testing.T) {
	var buf syncBuffer
	prev := log.Writer()
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(prev) })

	source := &fakeSource{batches: [][]receivedMessage{
		{{AckID: "ack-1", Data: []byte(humanPatchEntry)}},
	}}

	// The clock jumps a whole interval the moment a record is handled and then
	// stands still, so what the assertion turns on is which side of that jump
	// lastDelivery was read on -- nothing else. Both the handler and the clock
	// run on the loop's goroutine, so the flag needs no lock.
	base := time.Unix(0, 0)
	delivered := false
	sub := newSubscriber(source, func(context.Context, AuditRecord) { delivered = true }, defaultMaxMessages, defaultBatchJoinBudget)
	sub.idleWait = time.Millisecond
	sub.now = func() time.Time {
		if delivered {
			return base.Add(idleReportInterval)
		}
		return base
	}

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := sub.Run(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Run returned %v, want context.DeadlineExceeded", err)
	}

	if got := sub.Counts().Parsed; got != 1 {
		t.Fatalf("parsed = %d, want 1: the batch never reached the handler", got)
	}
	if strings.Contains(buf.String(), "idle") {
		t.Errorf("reported idle after delivering a record; log = %q", buf.String())
	}
}

func TestNextBackoffCapsAtCeiling(t *testing.T) {
	tests := []struct {
		current time.Duration
		want    time.Duration
	}{
		{pullBackoffInitial, 2 * time.Second},
		{30 * time.Second, pullBackoffMax},
		{pullBackoffMax, pullBackoffMax},
	}
	for _, tc := range tests {
		if got := nextBackoff(tc.current); got != tc.want {
			t.Errorf("nextBackoff(%s) = %s, want %s", tc.current, got, tc.want)
		}
	}
}

// The budget's own validation is against Pub/Sub's maximum deadline, which is
// six times what the drift-pubsub module configures, so a budget that passes
// validation can still overrun the subscription it is pointed at. This is the
// arithmetic behind the startup warning that closes that gap.
func TestAckDeadlineWarning(t *testing.T) {
	tests := []struct {
		name     string
		budget   time.Duration
		deadline time.Duration
		wantWarn bool
	}{
		{
			// The shipped pairing: the module's deadline and the flag's default.
			name:     "default budget against the module's deadline",
			budget:   defaultBatchJoinBudget,
			deadline: 60 * time.Second,
		},
		{
			// Exactly half. The boundary is inclusive, so the default above is
			// not warned about by the check that is meant to endorse it.
			name:     "budget at exactly half the deadline",
			budget:   30 * time.Second,
			deadline: 60 * time.Second,
		},
		{
			name:     "budget one second past half",
			budget:   31 * time.Second,
			deadline: 60 * time.Second,
			wantWarn: true,
		},
		{
			// The case the ceiling lets through: valid flag, stock module.
			name:     "budget inside the ceiling but past the deadline",
			budget:   120 * time.Second,
			deadline: 60 * time.Second,
			wantWarn: true,
		},
		{
			// A subscription created outside the module carries Pub/Sub's own
			// 10s default, against which even the flag's default is too long.
			name:     "default budget against a hand-made subscription",
			budget:   defaultBatchJoinBudget,
			deadline: 10 * time.Second,
			wantWarn: true,
		},
		{
			// An operator who raised the deadline has bought the headroom, and
			// warning them would train them to ignore the line.
			name:     "raised deadline accommodates a raised budget",
			budget:   120 * time.Second,
			deadline: 600 * time.Second,
		},
		{
			// Nothing to compare against. Substituting a default here would
			// warn about a number nobody configured.
			name:     "no deadline reported",
			budget:   defaultBatchJoinBudget,
			deadline: 0,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := ackDeadlineWarning(tc.budget, tc.deadline)
			if (got != "") != tc.wantWarn {
				t.Fatalf("ackDeadlineWarning(%s, %s) = %q, want warning=%v", tc.budget, tc.deadline, got, tc.wantWarn)
			}
			// A warning an operator cannot act on is noise. Both numbers it is
			// comparing have to be in it, or the line says a budget is wrong
			// without saying what it is wrong against.
			if tc.wantWarn {
				for _, want := range []string{tc.budget.String(), tc.deadline.String()} {
					if !strings.Contains(got, want) {
						t.Errorf("warning %q does not name %s", got, want)
					}
				}
			}
		})
	}
}

// The preflight has two silent deaths and neither is reachable through
// ackDeadlineWarning: a reader that errors must still produce a line, and a
// deadline the budget fits must produce none. Deleting the comparison in
// realMain turns the check off for every install without reddening anything, so
// the comparison is tested where it now lives.
func TestAckDeadlinePreflight(t *testing.T) {
	const (
		fittingBudget = 20 * time.Second
		largeBudget   = 45 * time.Second
		moduleDefault = 60 * time.Second
	)
	probeErr := errors.New("get subscription: 403 permission denied")

	tests := []struct {
		name     string
		read     func(context.Context) (time.Duration, error)
		budget   time.Duration
		wantLine bool
		wantHas  []string
	}{
		{
			name:   "budget fits, nothing to say",
			read:   func(context.Context) (time.Duration, error) { return moduleDefault, nil },
			budget: fittingBudget,
		},
		{
			name:     "budget takes more than half",
			read:     func(context.Context) (time.Duration, error) { return moduleDefault, nil },
			budget:   largeBudget,
			wantLine: true,
			wantHas:  []string{largeBudget.String(), moduleDefault.String()},
		},
		{
			// The grant this probe needs is one roles/pubsub.subscriber does not
			// carry, so a denied probe is the ordinary case on a hand-made
			// install, not an exotic one. It has to say the budget went
			// unchecked rather than pass silently as though it had fit.
			name:     "probe denied, the budget is reported as unchecked",
			read:     func(context.Context) (time.Duration, error) { return 0, probeErr },
			budget:   largeBudget,
			wantLine: true,
			wantHas:  []string{"unchecked", probeErr.Error(), largeBudget.String()},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := ackDeadlinePreflight(context.Background(), tc.read, tc.budget)
			if (got != "") != tc.wantLine {
				t.Fatalf("ackDeadlinePreflight(...) = %q, want a line=%v", got, tc.wantLine)
			}
			for _, want := range tc.wantHas {
				if !strings.Contains(got, want) {
					t.Errorf("line %q does not name %s", got, want)
				}
			}
		})
	}
}

// The probe bounds its own call rather than trusting the caller's context. A
// reader that blocks must give up at ackDeadlineProbeTimeout, not hold the first
// batch behind a slow control plane.
func TestAckDeadlinePreflightBoundsItsOwnProbe(t *testing.T) {
	var got time.Duration
	ackDeadlinePreflight(context.Background(), func(ctx context.Context) (time.Duration, error) {
		deadline, ok := ctx.Deadline()
		if !ok {
			t.Fatal("the probe's context carries no deadline; a hung subscriptions.get would block startup")
		}
		got = time.Until(deadline)
		return 0, errors.New("unused")
	}, defaultBatchJoinBudget)

	if got <= 0 || got > ackDeadlineProbeTimeout {
		t.Errorf("probe deadline is %s away, want (0, %s]", got, ackDeadlineProbeTimeout)
	}
}

// AckDeadlineSeconds is a count of seconds and the caller compares it against a
// Duration, so the conversion is the whole of what this method does beyond the
// call. Dropping the multiplication turns a stock 60-second subscription into
// 60ns, which warns every install on every boot about a deadline nobody
// configured -- and the arithmetic test above cannot see it, because it is
// handed a Duration already.
func TestPubsubSourceAckDeadlineConvertsSeconds(t *testing.T) {
	const (
		reportedSeconds = 60
		wantDeadline    = reportedSeconds * time.Second
		subscriptionID  = "projects/example-project/subscriptions/drift-audit-sub"
	)

	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"name":%q,"ackDeadlineSeconds":%d}`, subscriptionID, reportedSeconds)
	}))
	defer srv.Close()

	service, err := pubsub.NewService(context.Background(),
		option.WithEndpoint(srv.URL), option.WithoutAuthentication())
	if err != nil {
		t.Fatalf("pubsub.NewService: %v", err)
	}
	source := &pubsubSource{service: service, subscription: subscriptionID}

	got, err := source.AckDeadline(context.Background())
	if err != nil {
		t.Fatalf("AckDeadline: %v", err)
	}
	if got != wantDeadline {
		t.Errorf("AckDeadline = %s, want %s", got, wantDeadline)
	}
	// The subscription it asked about, not just the number that came back: a
	// probe reading some other subscription's deadline would compare the budget
	// against a deadline this run is not subject to.
	if !strings.Contains(gotPath, subscriptionID) {
		t.Errorf("probed %q, want a path naming %s", gotPath, subscriptionID)
	}
}

// A probe that fails has to surface the subscription it failed on. The detector
// pulls anyway, so this line is the operator's only notice, and one that named
// no subscription would not say which of a multi-subscription install was
// unreadable.
func TestPubsubSourceAckDeadlineReportsTheSubscriptionOnError(t *testing.T) {
	const subscriptionID = "projects/example-project/subscriptions/drift-audit-sub"

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, `{"error":{"code":403,"message":"permission denied"}}`, http.StatusForbidden)
	}))
	defer srv.Close()

	service, err := pubsub.NewService(context.Background(),
		option.WithEndpoint(srv.URL), option.WithoutAuthentication())
	if err != nil {
		t.Fatalf("pubsub.NewService: %v", err)
	}
	source := &pubsubSource{service: service, subscription: subscriptionID}

	if _, err := source.AckDeadline(context.Background()); err == nil {
		t.Fatal("AckDeadline returned no error on a 403; the preflight would report a zero deadline as a real one")
	} else if !strings.Contains(err.Error(), subscriptionID) {
		t.Errorf("error %q does not name %s", err, subscriptionID)
	}
}

func TestNewSubscriberDefaultsMaxMessages(t *testing.T) {
	sub := newSubscriber(&fakeSource{}, func(context.Context, AuditRecord) {}, 0, defaultBatchJoinBudget)
	if sub.maxMessages != defaultMaxMessages {
		t.Errorf("maxMessages = %d, want %d", sub.maxMessages, defaultMaxMessages)
	}
}

func equalStrings(got, want []string) bool {
	if len(got) != len(want) {
		return false
	}
	for i := range got {
		if got[i] != want[i] {
			return false
		}
	}
	return true
}
