/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package controller

import (
	"context"
	"testing"
	"time"
)

// The schedule the constructor hands Start: the first poll 30 seconds after
// election, the rest every five minutes.
func TestUsagePollerStartScheduleConstants(t *testing.T) {
	t.Parallel()
	p := NewUsageCounterPoller(&PlatformAgentReconciler{})
	if p.firstPollDelay != 30*time.Second {
		t.Errorf("firstPollDelay = %s, want 30s", p.firstPollDelay)
	}
	if p.pollInterval != 5*time.Minute {
		t.Errorf("pollInterval = %s, want 5m", p.pollInterval)
	}
}

// Start's first poll runs once firstPollDelay has elapsed, not a full
// interval later: with the interval set far out, the one scrape observed is
// the first poll's, and it lands no earlier than the delay.
func TestUsagePollerStartPollsFirstAfterTheDelay(t *testing.T) {
	t.Parallel()
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.clock = usageClock(5)
	h.p.firstPollDelay = 50 * time.Millisecond
	h.p.pollInterval = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	started := time.Now()
	go func() { done <- h.p.Start(ctx) }()

	deadline := time.Now().Add(10 * time.Second)
	for !h.stub.scraped(gatewayAddr()) {
		if time.Now().After(deadline) {
			cancel()
			t.Fatal("no poll within ten seconds of Start")
		}
		time.Sleep(5 * time.Millisecond)
	}
	if elapsed := time.Since(started); elapsed < h.p.firstPollDelay {
		t.Errorf("the first poll ran after %s, before the %s delay", elapsed, h.p.firstPollDelay)
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("Start returned %v", err)
	}
}

// A context cancelled before the first delay elapses ends Start without a poll.
func TestUsagePollerStartCancelledBeforeTheFirstPoll(t *testing.T) {
	t.Parallel()
	created := usageClock(0).Add(-time.Hour)
	h := newUsageHarness(t, usageTestAgent(created), usageDefaultObjects(created)...)
	h.clock = usageClock(5)
	h.p.firstPollDelay = time.Hour
	h.p.pollInterval = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := h.p.Start(ctx); err != nil {
		t.Fatalf("Start returned %v", err)
	}
	if h.stub.scraped(gatewayAddr()) {
		t.Error("a cancelled Start polled")
	}
}
