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
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func TestToTriageEvent(t *testing.T) {
	now := time.Now()

	tests := []struct {
		name          string
		inputEvent    *corev1.Event
		wantFirstSeen time.Time
		wantLastSeen  time.Time
		wantMessage   string
	}{
		{
			name: "standard event with all timestamps",
			inputEvent: &corev1.Event{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-event",
					Namespace: "default",
				},
				InvolvedObject: corev1.ObjectReference{
					Kind:      "Pod",
					Name:      "pod-xyz",
					Namespace: "default",
					UID:       types.UID("uid-123"),
				},
				Reason:         "FailedScheduling",
				Message:        "pod failed to schedule",
				FirstTimestamp: metav1.Time{Time: now.Add(-10 * time.Minute)},
				LastTimestamp:  metav1.Time{Time: now},
				Count:          5,
			},
			wantFirstSeen: now.Add(-10 * time.Minute),
			wantLastSeen:  now,
			wantMessage:   "pod failed to schedule",
		},
		{
			name: "fallback to EventTime when timestamps are zero",
			inputEvent: &corev1.Event{
				InvolvedObject: corev1.ObjectReference{
					UID: types.UID("uid-123"),
				},
				EventTime: metav1.MicroTime{Time: now},
			},
			wantFirstSeen: now,
			wantLastSeen:  now,
			wantMessage:   "",
		},
		{
			name: "message truncation above limit",
			inputEvent: &corev1.Event{
				InvolvedObject: corev1.ObjectReference{
					UID: types.UID("uid-123"),
				},
				Message: strings.Repeat("A", 3000),
			},
			wantFirstSeen: time.Time{},
			wantLastSeen:  time.Time{},
			wantMessage:   strings.Repeat("A", 2048) + "... [truncated by k8s-event-watcher]",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := toTriageEvent(tc.inputEvent, targetCluster{Name: "test-cluster", ProjectID: "test-proj", Location: "us-central1"})
			if !got.FirstSeen.Equal(tc.wantFirstSeen) {
				t.Errorf("FirstSeen = %v; want %v", got.FirstSeen, tc.wantFirstSeen)
			}
			if !got.LastSeen.Equal(tc.wantLastSeen) {
				t.Errorf("LastSeen = %v; want %v", got.LastSeen, tc.wantLastSeen)
			}
			if got.Message != tc.wantMessage {
				t.Errorf("Message length = %d; want %d", len(got.Message), len(tc.wantMessage))
			}
			if got.Cluster != "test-cluster" {
				t.Errorf("Cluster = %q; want %q", got.Cluster, "test-cluster")
			}
		})
	}
}

// captureLog routes the package logger into a buffer for the duration of the
// test. The informer's goroutines write concurrently, so the buffer is locked.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func captureLog(t *testing.T) *lockedBuffer {
	t.Helper()
	buf := &lockedBuffer{}
	prev := log.Writer()
	log.SetOutput(buf)
	t.Cleanup(func() { log.SetOutput(prev) })
	return buf
}

// nopDispatcher satisfies eventDispatcher for informers that never sync.
type nopDispatcher struct{}

func (nopDispatcher) Dispatch(context.Context, TriageEvent) {}

// preflightStub answers the preflight's SelfSubjectAccessReviews on a fake
// clientset, which has no answer of its own for the resource. deny names the
// verb to refuse ("" refuses nothing) with reason as the review's
// status.reason; err, when set, fails every review instead. Both are read
// under a lock because the grant test changes them while Run is holding.
type preflightStub struct {
	mu      sync.Mutex
	deny    string
	reason  string
	err     error
	reviews atomic.Int64
	// evaluationError, when set, is put on every refused review's status: the
	// authorizer's "I could not decide" alongside allowed=false.
	evaluationError string
}

func (s *preflightStub) set(deny, reason string, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.deny, s.reason, s.err = deny, reason, err
}

// stubPreflight installs a preflightStub with the given first answer on client.
func stubPreflight(client *fake.Clientset, deny, reason string, err error) *preflightStub {
	s := &preflightStub{deny: deny, reason: reason, err: err}
	client.PrependReactor("create", "selfsubjectaccessreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		s.reviews.Add(1)
		s.mu.Lock()
		deny, reason, err, evaluationError := s.deny, s.reason, s.err, s.evaluationError
		s.mu.Unlock()
		if err != nil {
			return true, nil, err
		}
		review := action.(k8stesting.CreateAction).GetObject().(*authorizationv1.SelfSubjectAccessReview)
		attrs := review.Spec.ResourceAttributes
		if attrs.Group != "" || attrs.Resource != "events" || attrs.Namespace != "" {
			return true, nil, fmt.Errorf("unexpected preflight review %+v; want core events at the cluster scope", attrs)
		}
		review.Status.Allowed = attrs.Verb != deny
		if !review.Status.Allowed {
			review.Status.Reason = reason
			review.Status.EvaluationError = evaluationError
		}
		return true, review, nil
	})
	return s
}

// stubIdentity answers whoAmI's SelfSubjectReview on a fake clientset with
// username, or fails it with err when set.
func stubIdentity(client *fake.Clientset, username string, err error) {
	client.PrependReactor("create", "selfsubjectreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if err != nil {
			return true, nil, err
		}
		review := action.(k8stesting.CreateAction).GetObject().(*authenticationv1.SelfSubjectReview)
		review.Status.UserInfo.Username = username
		return true, review, nil
	})
}

// allowPreflight is what every test that is not about the preflight wants:
// both verbs allowed, so Run builds the informer at once and the test observes
// the reflector, not a hold.
func allowPreflight(client *fake.Clientset) *preflightStub {
	return stubPreflight(client, "", "", nil)
}

// listFailingClient returns a fake clientset whose every Event list fails with
// listErr, and a counter of how many lists were attempted. The preflight is
// allowed: these tests are about what the reflector does with the refusal.
func listFailingClient(listErr error) (*fake.Clientset, *atomic.Int64) {
	client := fake.NewClientset()
	allowPreflight(client)
	var attempts atomic.Int64
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		attempts.Add(1)
		return true, nil, listErr
	})
	return client, &attempts
}

// forbiddenListErr is what the API server returns when the identity cannot
// list Events; the fake hands it back unwrapped and the reflector wraps it.
var forbiddenListErr = apierrors.NewForbidden(
	schema.GroupResource{Resource: "events"}, "",
	errors.New(`User "sa" cannot list resource "events" in API group "" at the cluster scope`),
)

// A 403 holds the reflector for the whole interval instead of the default
// backoff. Over a window in which the default backoff (800ms initial, doubling)
// makes at least two attempts, a held informer makes exactly one and logs it
// once; the informer stays alive, so cancelling the context still ends Run
// promptly from inside the hold.
func TestRun_ForbiddenListIsHeldForTheInterval(t *testing.T) {
	logs := captureLog(t)
	client, attempts := listFailingClient(forbiddenListErr)
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "held"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	var synced atomic.Bool
	go func() { done <- w.Run(ctx, func(watching bool) { synced.Store(watching) }) }()

	// Long enough for the default backoff to have retried at least once more
	// (first retry lands between 0.8s and 1.6s after the initial attempt).
	time.Sleep(2500 * time.Millisecond)

	if got := attempts.Load(); got != 1 {
		t.Errorf("want exactly one list attempt during the hold, got %d", got)
	}
	if got := strings.Count(logs.String(), "events forbidden, holding 1h0m0s"); got != 1 {
		t.Errorf("want exactly one hold log line, got %d in:\n%s", got, logs.String())
	}
	if synced.Load() {
		t.Error("a forbidden informer must not report itself synced")
	}

	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Error("Run should report the sync failure when stopped before the initial list completed")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation; the hold is not selecting on the context")
	}
}

// Every other error keeps client-go's own backoff: the reflector retries within
// seconds, and nothing is logged as a hold.
func TestRun_OtherListErrorsKeepTheDefaultBackoff(t *testing.T) {
	logs := captureLog(t)
	client, attempts := listFailingClient(apierrors.NewInternalError(errors.New("etcd unavailable")))
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "flapping"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- w.Run(ctx, nil) }()

	deadline := time.Now().Add(10 * time.Second)
	for attempts.Load() < 2 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if got := attempts.Load(); got < 2 {
		t.Errorf("want the default backoff to retry a non-403 list within 10s, got %d attempt(s)", got)
	}
	if strings.Contains(logs.String(), "forbidden, holding") {
		t.Errorf("a non-403 error must not be held:\n%s", logs.String())
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}

// The hold ends the moment the informer's context does, so shutdown is never
// delayed by a cluster that is being held.
func TestHandleWatchError_CancelledContextEndsTheHold(t *testing.T) {
	captureLog(t)
	w := newWatcher(fake.NewClientset(), nopDispatcher{}, targetCluster{Name: "held"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	start := time.Now()
	w.handleWatchError(ctx, nil, fmt.Errorf("failed to list *v1.Event: %w", forbiddenListErr))
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Errorf("hold took %s with a cancelled context; want an immediate return", elapsed)
	}
}

// The reflector wraps the list error before it reaches the handler; the 403 has
// to be recognised through that wrapping or the hold never applies in practice.
func TestHandleWatchError_RecognisesForbiddenThroughWrapping(t *testing.T) {
	logs := captureLog(t)
	w := newWatcher(fake.NewClientset(), nopDispatcher{}, targetCluster{Name: "held"}, 0)
	w.forbiddenHold = time.Millisecond

	w.handleWatchError(context.Background(), nil, fmt.Errorf("failed to list *v1.Event: %w", forbiddenListErr))
	if !strings.Contains(logs.String(), "[held] events forbidden, holding 1ms") {
		t.Errorf("wrapped 403 was not recognised:\n%s", logs.String())
	}
}

// forbiddenWatchErr is the watch-side twin of forbiddenListErr: what the API
// server returns when the identity may no longer watch Events. The reflector
// passes a watch error through unwrapped.
var forbiddenWatchErr = apierrors.NewForbidden(
	schema.GroupResource{Resource: "events"}, "",
	errors.New(`User "sa" cannot watch resource "events" in API group "" at the cluster scope`),
)

// transitionRecorder collects the values Run's onWatching callback receives,
// in order, from whichever goroutine reports them.
type transitionRecorder struct {
	mu     sync.Mutex
	values []bool
}

func (r *transitionRecorder) record(watching bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.values = append(r.values, watching)
}

func (r *transitionRecorder) snapshot() []bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]bool(nil), r.values...)
}

func (r *transitionRecorder) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.values)
}

// eventually polls cond until it holds or timeout passes, and fails the test
// with what if it never does.
func eventually(t *testing.T, timeout time.Duration, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out after %s waiting for %s", timeout, what)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func equalBools(a, b []bool) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// A 403 that arrives after the initial list succeeded — the watch refused for an
// identity that may list but not watch, or a permission revoked mid-run — is
// held exactly as one before it: over a window in which the default backoff
// would have relisted and rewatched at least once more, a held informer makes
// exactly one watch attempt and logs one hold line. The caller hears the sync
// and then the drop, so cluster_up can read 0 for the held cluster.
func TestRun_ForbiddenWatchAfterSyncIsHeld(t *testing.T) {
	logs := captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	var watchAttempts atomic.Int64
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		watchAttempts.Add(1)
		return true, nil, forbiddenWatchErr
	})
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "list-only"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	eventually(t, 10*time.Second, func() bool { return rec.count() >= 2 }, "the sync and the drop to be reported")
	// Long enough for the default backoff to have relisted at least once more
	// (first retry lands between 0.8s and 1.6s after the refused watch).
	time.Sleep(2500 * time.Millisecond)

	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Errorf("transitions = %v; want %v", got, want)
	}
	if got := watchAttempts.Load(); got != 1 {
		t.Errorf("want exactly one watch attempt during the hold, got %d", got)
	}
	if got := strings.Count(logs.String(), "[list-only] events forbidden, holding 1h0m0s"); got != 1 {
		t.Errorf("want exactly one hold log line, got %d in:\n%s", got, logs.String())
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation; the hold is not selecting on the context")
	}
}

// A held cluster comes back on its own: once the watch is permitted again the
// next attempt succeeds and the caller hears true once more, so cluster_up
// returns to 1 without a restart. The report is per transition, not per
// request — a watch the reflector re-opens after that recovery is the same
// state and is not reported again.
func TestRun_ForbiddenWatchAfterSyncReportsDownThenUp(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	var refuse atomic.Bool
	refuse.Store(true)
	var watchAttempts atomic.Int64
	var openWatch atomic.Pointer[watch.FakeWatcher]
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		watchAttempts.Add(1)
		if refuse.Load() {
			return true, nil, forbiddenWatchErr
		}
		fw := watch.NewFake()
		openWatch.Store(fw)
		return true, fw, nil
	})
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "revoked"}, 0)
	w.forbiddenHold = 200 * time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	eventually(t, 10*time.Second, func() bool { return rec.count() >= 2 }, "the sync and the drop to be reported")
	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Fatalf("transitions before the grant = %v; want %v", got, want)
	}

	refuse.Store(false)
	eventually(t, 10*time.Second, func() bool { return rec.count() >= 3 }, "the recovery to be reported")
	if got, want := rec.snapshot(), []bool{true, false, true}; !equalBools(got, want) {
		t.Fatalf("transitions after the grant = %v; want %v", got, want)
	}

	// Close the recovered watch so the reflector opens another one. That is a
	// second successful watch call in the same watching state, and must not
	// be a fourth transition.
	attemptsBeforeClose := watchAttempts.Load()
	eventually(t, 2*time.Second, func() bool { return openWatch.Load() != nil }, "the recovered watch to be handed to the reflector")
	openWatch.Swap(nil).Stop()
	eventually(t, 10*time.Second, func() bool { return watchAttempts.Load() > attemptsBeforeClose && openWatch.Load() != nil }, "the reflector to re-open the watch")
	if got, want := rec.snapshot(), []bool{true, false, true}; !equalBools(got, want) {
		t.Errorf("transitions after a re-opened watch = %v; want %v (one report per transition, not per watch)", got, want)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}

// Reflector-independent view of the post-sync path: a wrapped 403 on a watcher
// that has synced is held for the interval, the callback sees false before the
// hold begins rather than after it, a second refused attempt in the same hold
// is not reported again, and the next successful watch call reports true.
func TestHandleWatchError_ForbiddenAfterSyncIsHeld(t *testing.T) {
	logs := captureLog(t)
	w := newWatcher(fake.NewClientset(), nopDispatcher{}, targetCluster{Name: "synced"}, 0)
	w.forbiddenHold = 300 * time.Millisecond
	rec := &transitionRecorder{}
	var reportedAt atomic.Pointer[time.Time]
	w.onWatching = func(watching bool) {
		now := time.Now()
		reportedAt.Store(&now)
		rec.record(watching)
	}
	w.markSynced()
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Fatalf("transitions after the sync = %v; want %v", got, want)
	}

	start := time.Now()
	w.handleWatchError(context.Background(), nil, fmt.Errorf("failed to list *v1.Event: %w", forbiddenWatchErr))
	held := time.Since(start)
	if held < w.forbiddenHold {
		t.Errorf("handler returned after %s; want at least the %s hold", held, w.forbiddenHold)
	}
	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Fatalf("transitions after the 403 = %v; want %v", got, want)
	}
	if at := reportedAt.Load(); at == nil || at.Sub(start) >= w.forbiddenHold {
		t.Errorf("the drop was reported %s after the 403; want before the hold, not after it", at.Sub(start))
	}
	if got := strings.Count(logs.String(), "[synced] events forbidden, holding 300ms"); got != 1 {
		t.Errorf("want exactly one hold log line, got %d in:\n%s", got, logs.String())
	}

	w.handleWatchError(context.Background(), nil, forbiddenWatchErr)
	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Errorf("transitions after a second 403 in the same hold = %v; want %v", got, want)
	}

	w.watchEstablished()
	if got, want := rec.snapshot(), []bool{true, false, true}; !equalBools(got, want) {
		t.Errorf("transitions after the watch succeeded again = %v; want %v", got, want)
	}
}

// The reflector refuses the watch that follows a successful list on its own
// goroutine, and can do so before Run has seen the list complete. The caller
// hears the same sequence in that order as in the other: true for the list,
// false for the hold, and true again only when a watch succeeds.
func TestHandleWatchError_ForbiddenBeforeRunSeesTheSyncIsReportedAtTheSync(t *testing.T) {
	captureLog(t)
	w := newWatcher(fake.NewClientset(), nopDispatcher{}, targetCluster{Name: "early"}, 0)
	w.forbiddenHold = time.Hour
	rec := &transitionRecorder{}
	w.onWatching = rec.record

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	w.handleWatchError(cancelled, nil, forbiddenWatchErr)
	if got := rec.snapshot(); len(got) != 0 {
		t.Fatalf("a 403 before the initial list completed reported %v; want nothing", got)
	}

	w.markSynced()
	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Fatalf("transitions once Run sees the sync = %v; want %v", got, want)
	}

	w.watchEstablished()
	if got, want := rec.snapshot(), []bool{true, false, true}; !equalBools(got, want) {
		t.Errorf("transitions after the watch succeeded = %v; want %v", got, want)
	}
}

// A list that succeeds ends the hold its refusal began but is not the recovery
// signal: a cluster held from its first list and then granted both permissions
// is reported synced once, not synced, held and recovered in the space of the
// watch call, whichever of Run and the reflector sees the list complete first.
func TestWatcher_SuccessfulListEndsTheHoldWithoutReporting(t *testing.T) {
	captureLog(t)
	w := newWatcher(fake.NewClientset(), nopDispatcher{}, targetCluster{Name: "granted"}, 0)
	w.forbiddenHold = time.Hour
	rec := &transitionRecorder{}
	w.onWatching = rec.record

	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	w.handleWatchError(cancelled, nil, fmt.Errorf("failed to list *v1.Event: %w", forbiddenListErr))
	w.listCompleted()
	w.markSynced()
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Fatalf("transitions after a granted list = %v; want %v", got, want)
	}
	w.watchEstablished()
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Errorf("transitions after the first watch = %v; want %v (the first watch is not a recovery)", got, want)
	}

	// The reverse: a list that succeeds for an identity that may list but not
	// watch clears nothing the caller can see. The refused watch that follows
	// puts the hold back, and the caller hears false once.
	w.listCompleted()
	w.handleWatchError(cancelled, nil, forbiddenWatchErr)
	w.listCompleted()
	w.handleWatchError(cancelled, nil, forbiddenWatchErr)
	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Errorf("transitions across two list-then-refused-watch cycles = %v; want %v", got, want)
	}
}

// watchListCapableClient hides the fake clientset's
// IsWatchListSemanticsUnSupported marker. The reflector reads that marker
// through ToListWatcherWithWatchListSemantics and, when it is present, runs in
// the classic list-then-watch mode; without it the reflector takes the
// watch-list path a real client gets by default, streaming the initial state
// through the watch call and never calling List while the watch is permitted.
type watchListCapableClient struct{ kubernetes.Interface }

// initialEventsEndBookmark is the bookmark an API server sends once the
// watch-list stream has delivered the initial state; the reflector completes
// its sync on it.
func initialEventsEndBookmark() *corev1.Event {
	return &corev1.Event{ObjectMeta: metav1.ObjectMeta{
		ResourceVersion: "1",
		Annotations:     map[string]string{metav1.InitialEventsAnnotationKey: "true"},
	}}
}

// The same sequence as TestRun_ForbiddenWatchAfterSyncReportsDownThenUp, in
// the watch-list reflector mode production runs in: the initial sync arrives
// through the watch call with no List at all, a refused watch after the sync
// is held and reported false, and the recovery is the next watch call that
// succeeds, again with no List between the hold and it. The last call before
// the recovering watch is the refused watch, which is what makes the watch
// call, not the list, the signal newListWatch hooks.
func TestRun_WatchListMode_ForbiddenWatchAfterSyncReportsDownThenUp(t *testing.T) {
	captureLog(t)
	underlying := fake.NewClientset()
	allowPreflight(underlying)
	var refuse atomic.Bool
	var listAttempts, watchAttempts atomic.Int64
	var lastCall atomic.Value
	var callBeforeRecovery atomic.Value
	var openWatch atomic.Pointer[watch.FakeWatcher]
	underlying.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		listAttempts.Add(1)
		lastCall.Store("list")
		return false, nil, nil
	})
	underlying.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		watchAttempts.Add(1)
		if refuse.Load() {
			lastCall.Store("refused watch")
			return true, nil, forbiddenWatchErr
		}
		if prev, ok := lastCall.Load().(string); ok && prev == "refused watch" {
			callBeforeRecovery.Store(prev)
		}
		lastCall.Store("watch")
		fw := watch.NewFakeWithChanSize(1, false)
		fw.Action(watch.Bookmark, initialEventsEndBookmark())
		openWatch.Store(fw)
		return true, fw, nil
	})
	w := newWatcher(watchListCapableClient{underlying}, nopDispatcher{}, targetCluster{Name: "streamed"}, 0)
	w.forbiddenHold = 200 * time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	eventually(t, 10*time.Second, func() bool { return rec.count() >= 1 }, "the sync to be reported")
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Fatalf("transitions after the sync = %v; want %v", got, want)
	}
	if got := listAttempts.Load(); got != 0 {
		t.Fatalf("the reflector listed %d time(s) before the sync; want 0 in watch-list mode (is the fake still reporting itself unsupported?)", got)
	}

	// Revoke: close the stream so the reflector re-opens the watch, and refuse
	// it from now on.
	refuse.Store(true)
	openWatch.Swap(nil).Stop()
	eventually(t, 10*time.Second, func() bool { return rec.count() >= 2 }, "the drop to be reported")
	if got, want := rec.snapshot(), []bool{true, false}; !equalBools(got, want) {
		t.Fatalf("transitions after the refused watch = %v; want %v", got, want)
	}

	refuse.Store(false)
	eventually(t, 10*time.Second, func() bool { return rec.count() >= 3 }, "the recovery to be reported")
	if got, want := rec.snapshot(), []bool{true, false, true}; !equalBools(got, want) {
		t.Fatalf("transitions after the grant = %v; want %v", got, want)
	}
	if got, _ := callBeforeRecovery.Load().(string); got != "refused watch" {
		t.Errorf("the call before the recovering watch was %q; want the refused watch, with no List between the hold and the recovery", got)
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}

// countingListClient returns a fake clientset that answers Event lists as the
// fake does by default, and a counter of how many lists were attempted.
func countingListClient() (*fake.Clientset, *atomic.Int64) {
	client := fake.NewClientset()
	var attempts atomic.Int64
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		attempts.Add(1)
		return false, nil, nil
	})
	return client, &attempts
}

// whoAmI names the identity the API server authenticated, and when it cannot,
// says which credential the cluster is reached with and why the name is
// missing, so the denial line never shows a bare blank where the identity
// should be.
func TestWhoAmI(t *testing.T) {
	cases := []struct {
		name     string
		profile  string
		username string
		err      error
		want     string
	}{
		{name: "google identity named", profile: "gke_p_l_c", username: "sa@p.iam.gserviceaccount.com", want: "sa@p.iam.gserviceaccount.com"},
		{name: "service account named", profile: "direct", username: "system:serviceaccount:kubeagents-system:kubeagents-platform-agent", want: "system:serviceaccount:kubeagents-system:kubeagents-platform-agent"},
		{name: "review fails on the direct cluster", profile: "direct", err: errors.New("the server could not find the requested resource"),
			want: "the process's own credential (in a pod, its service account) (not named: SelfSubjectReview the server could not find the requested resource)"},
		{name: "review fails on a profile cluster", profile: "gke_p_l_c", err: errors.New("forbidden"),
			want: "the pod's Google identity (not named: SelfSubjectReview forbidden)"},
		{name: "review returns no username", profile: "gke_p_l_c", username: "",
			want: "the pod's Google identity (not named: SelfSubjectReview returned no username)"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			client := fake.NewClientset()
			stubIdentity(client, tc.username, tc.err)
			w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "c", Profile: tc.profile}, 0)
			if got := w.whoAmI(context.Background()); got != tc.want {
				t.Errorf("whoAmI() = %q; want %q", got, tc.want)
			}
		})
	}
}

// A list the preflight says is denied never reaches the reflector: over a
// window in which the default backoff would have relisted, no Event list is
// attempted at all, one preflight line names the cluster, the verb and the
// authorizer's reason, the caller hears nothing, and the denied verb costs one
// review per check. Cancelling during the hold ends Run with an error, as a
// cancelled sync does.
func TestRun_PreflightDeniedListBuildsNoInformer(t *testing.T) {
	logs := captureLog(t)
	client, lists := countingListClient()
	stub := stubPreflight(client, "list", `requires one of ["container.events.list"] permission(s)`, nil)
	stubIdentity(client, "kubeagents-platform-agent@proj.iam.gserviceaccount.com", nil)
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "denied", Profile: "gke_proj_us-central1_denied"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	// Long enough for the default backoff to have retried at least once more
	// had an informer been built (first retry lands 0.8s to 1.6s after the
	// initial attempt).
	time.Sleep(2500 * time.Millisecond)

	if got := lists.Load(); got != 0 {
		t.Errorf("want no Event list for a cluster denied at preflight, got %d", got)
	}
	if got := stub.reviews.Load(); got != 1 {
		t.Errorf("want exactly one review per check for a denied list, got %d", got)
	}
	want := `[denied] events list denied at preflight for kubeagents-platform-agent@proj.iam.gserviceaccount.com, holding 1h0m0s before the next check: requires one of ["container.events.list"] permission(s)`
	if got := strings.Count(logs.String(), want); got != 1 {
		t.Errorf("want exactly one preflight line %q, got %d in:\n%s", want, got, logs.String())
	}
	if strings.Contains(logs.String(), "forbidden, holding") || strings.Contains(logs.String(), "informer error") {
		t.Errorf("a cluster held at preflight must not reach the reflector:\n%s", logs.String())
	}
	if got := rec.snapshot(); len(got) != 0 {
		t.Errorf("a cluster held at preflight reported %v; want nothing", got)
	}

	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Error("Run should report an error when stopped while held at preflight")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation; the preflight hold is not selecting on the context")
	}
}

// An identity that may list but not watch is held before its list: the line
// names watch, both verbs were asked, and no Event list is attempted. The
// denial carries no reason, as RBAC's do, so the line says what that means
// and, with the identity unnamed, which credential the cluster is reached by.
func TestRun_PreflightDeniedWatchBuildsNoInformer(t *testing.T) {
	logs := captureLog(t)
	client, lists := countingListClient()
	stub := stubPreflight(client, "watch", "", nil)
	stubIdentity(client, "", apierrors.NewNotFound(schema.GroupResource{Group: "authentication.k8s.io", Resource: "selfsubjectreviews"}, ""))
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "list-only", Profile: "direct"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	want := "[list-only] events watch denied at preflight for the process's own credential (in a pod, its service account) (not named: SelfSubjectReview selfsubjectreviews.authentication.k8s.io \"\" not found), holding 1h0m0s before the next check: no reason given (Kubernetes RBAC returns none; grant list and watch on events to this identity)"
	eventually(t, 10*time.Second, func() bool { return strings.Contains(logs.String(), want) }, "the preflight line naming watch")
	time.Sleep(500 * time.Millisecond)

	if got := lists.Load(); got != 0 {
		t.Errorf("want no Event list for a cluster whose watch is denied at preflight, got %d", got)
	}
	if got := stub.reviews.Load(); got != 2 {
		t.Errorf("want two reviews per check when the list is allowed and the watch denied, got %d", got)
	}
	if got := strings.Count(logs.String(), "denied at preflight"); got != 1 {
		t.Errorf("want exactly one preflight line, got %d in:\n%s", got, logs.String())
	}
	if got := rec.snapshot(); len(got) != 0 {
		t.Errorf("a cluster held at preflight reported %v; want nothing", got)
	}

	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Error("Run should report an error when stopped while held at preflight")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}

// A permission granted during the hold is picked up on the next check: the
// informer is built then, syncs, and the caller hears true, with no restart.
func TestRun_PreflightGrantDuringHoldStartsTheInformer(t *testing.T) {
	logs := captureLog(t)
	client, lists := countingListClient()
	stub := stubPreflight(client, "list", "", nil)
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "granted"}, 0)
	w.forbiddenHold = 200 * time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	eventually(t, 10*time.Second, func() bool { return stub.reviews.Load() >= 1 }, "the first check")
	if got := lists.Load(); got != 0 {
		t.Fatalf("want no Event list before the grant, got %d", got)
	}
	if got := rec.snapshot(); len(got) != 0 {
		t.Fatalf("a cluster held at preflight reported %v; want nothing", got)
	}

	stub.set("", "", nil)
	eventually(t, 10*time.Second, func() bool { return rec.count() >= 1 }, "the sync to be reported after the grant")
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Errorf("transitions after the grant = %v; want %v", got, want)
	}
	if got := lists.Load(); got < 1 {
		t.Errorf("want the informer's list once the preflight passes, got %d", got)
	}
	if got := strings.Count(logs.String(), "denied at preflight"); got < 1 {
		t.Errorf("want at least one preflight line before the grant in:\n%s", logs.String())
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}

// A review that fails is inconclusive, not a no: one line says so and the
// informer is built as it would have been without the preflight, so the list
// is attempted and the sync reported.
func TestRun_PreflightErrorFallsThroughToTheInformer(t *testing.T) {
	logs := captureLog(t)
	client, lists := countingListClient()
	stub := stubPreflight(client, "", "", apierrors.NewInternalError(errors.New("authorization webhook unavailable")))
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "unsure"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	eventually(t, 10*time.Second, func() bool { return rec.count() >= 1 }, "the sync to be reported")
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Errorf("transitions = %v; want %v", got, want)
	}
	if got := lists.Load(); got < 1 {
		t.Errorf("want the informer's list after an inconclusive preflight, got %d", got)
	}
	if got := stub.reviews.Load(); got != 1 {
		t.Errorf("want one review before falling through, got %d", got)
	}
	want := "[unsure] preflight inconclusive, starting the informer anyway: SelfSubjectAccessReview for list events: "
	if got := strings.Count(logs.String(), want); got != 1 {
		t.Errorf("want exactly one inconclusive line %q, got %d in:\n%s", want, got, logs.String())
	}
	if strings.Contains(logs.String(), "denied at preflight") {
		t.Errorf("an inconclusive preflight must not read as a denial:\n%s", logs.String())
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}

// A review the authorizer could not decide — allowed=false with an
// evaluationError, which is what a webhook authorizer that cannot reach its
// backend produces — is inconclusive, not a denial: the API server would have
// answered the list itself with a 500 the reflector retries within seconds,
// so holding here would withhold a cluster the reflector would have watched.
func TestRun_PreflightEvaluationErrorIsInconclusive(t *testing.T) {
	logs := captureLog(t)
	client, lists := countingListClient()
	stub := stubPreflight(client, "list", "", nil)
	stub.evaluationError = "webhook authorizer: connection refused"
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "undecided"}, 0)
	w.forbiddenHold = time.Hour

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	rec := &transitionRecorder{}
	go func() { done <- w.Run(ctx, rec.record) }()

	eventually(t, 10*time.Second, func() bool { return rec.count() >= 1 }, "the sync to be reported")
	if got, want := rec.snapshot(), []bool{true}; !equalBools(got, want) {
		t.Errorf("transitions = %v; want %v", got, want)
	}
	if got := lists.Load(); got < 1 {
		t.Errorf("want the informer's list after an undecided preflight, got %d", got)
	}
	want := "[undecided] preflight inconclusive, starting the informer anyway: SelfSubjectAccessReview for list events: authorizer could not decide: webhook authorizer: connection refused"
	if got := strings.Count(logs.String(), want); got != 1 {
		t.Errorf("want exactly one inconclusive line %q, got %d in:\n%s", want, got, logs.String())
	}
	if strings.Contains(logs.String(), "denied at preflight") {
		t.Errorf("an undecided review must not read as a denial:\n%s", logs.String())
	}

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run did not return within 2s of cancellation")
	}
}
