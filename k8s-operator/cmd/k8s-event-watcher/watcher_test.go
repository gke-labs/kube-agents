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

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
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

// listFailingClient returns a fake clientset whose every Event list fails with
// listErr, and a counter of how many lists were attempted.
func listFailingClient(listErr error) (*fake.Clientset, *atomic.Int64) {
	client := fake.NewClientset()
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
	go func() { done <- w.Run(ctx, func() { synced.Store(true) }) }()

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
