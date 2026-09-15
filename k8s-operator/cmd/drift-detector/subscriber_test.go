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
	"errors"
	"testing"
	"time"
)

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
	sub := newSubscriber(source, func(r AuditRecord) { handled = append(handled, r) }, defaultMaxMessages)

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
	sub := newSubscriber(source, func(AuditRecord) {}, defaultMaxMessages)

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
	sub := newSubscriber(source, func(AuditRecord) {}, defaultMaxMessages)

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

func TestRunStopsOnContextCancel(t *testing.T) {
	source := &fakeSource{batches: [][]receivedMessage{
		{{AckID: "ack-1", Data: []byte(humanPatchEntry)}},
	}}
	sub := newSubscriber(source, func(AuditRecord) {}, defaultMaxMessages)
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
	sub := newSubscriber(source, func(AuditRecord) {}, defaultMaxMessages)

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

func TestNewSubscriberDefaultsMaxMessages(t *testing.T) {
	sub := newSubscriber(&fakeSource{}, func(AuditRecord) {}, 0)
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
