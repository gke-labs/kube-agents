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
	"net/http"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

func TestListGate_AdmitsNAndBlocksTheNextUntilARelease(t *testing.T) {
	g := newListGate(2)
	ctx := context.Background()
	rel1, err := g.acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	rel2, err := g.acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if got := g.inFlight(); got != 2 {
		t.Fatalf("inFlight = %d; want 2", got)
	}

	third := make(chan error, 1)
	go func() {
		rel, err := g.acquire(ctx)
		if err == nil {
			rel()
		}
		third <- err
	}()
	select {
	case <-third:
		t.Fatal("a third acquire on a gate of two returned without a release")
	case <-time.After(200 * time.Millisecond):
	}
	rel1()
	// Idempotent: a second release of the same slot frees nothing more.
	rel1()
	select {
	case err := <-third:
		if err != nil {
			t.Fatalf("third acquire: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the third acquire did not proceed after a release")
	}
	rel2()
	if got := g.inFlight(); got != 0 {
		t.Fatalf("inFlight after every release = %d; want 0 (a double release freed a slot twice?)", got)
	}
}

func TestListGate_CancelledContextReturnsWithoutASlot(t *testing.T) {
	g := newListGate(1)
	rel, err := g.acquire(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer rel()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if _, err := g.acquire(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("acquire on a full gate with an expiring ctx = %v; want DeadlineExceeded", err)
	}
	if got := g.inFlight(); got != 1 {
		t.Fatalf("inFlight = %d; want 1 (the refused acquire must hold nothing)", got)
	}
}

func TestListGate_NilGateAdmitsEverything(t *testing.T) {
	var g *listGate
	for i := 0; i < 10; i++ {
		rel, err := g.acquire(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		rel()
	}
	if newListGate(0) != nil || newListGate(-1) != nil {
		t.Fatal("a slot count of 0 or less must build the nil (ungated) gate")
	}
	if got := g.size(); got != 0 {
		t.Fatalf("nil gate size = %d; want 0", got)
	}
}

// blockingListClient is a fake clientset whose Event list blocks until the
// returned channel is closed, counting how many lists are in flight at once
// and the highest that number reached.
func blockingListClient(inFlight, peak *atomic.Int64, release <-chan struct{}) *fake.Clientset {
	client := fake.NewClientset()
	allowPreflight(client)
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		now := inFlight.Add(1)
		for {
			prev := peak.Load()
			if now <= prev || peak.CompareAndSwap(prev, now) {
				break
			}
		}
		<-release
		inFlight.Add(-1)
		return true, &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: "1"}}, nil
	})
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		return true, watch.NewFakeWithChanSize(1, false), nil
	})
	return client
}

// Five clusters start through a gate of two: at most two lists are ever in
// flight, and once the lists are let through every cluster syncs.
func TestRun_GateBoundsConcurrentInitialLists(t *testing.T) {
	captureLog(t)
	const clusters, slots = 5, 2
	gate := newListGate(slots)
	var inFlight, peak atomic.Int64
	release := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var synced atomic.Int64
	done := make(chan error, clusters)
	for i := 0; i < clusters; i++ {
		w := newWatcher(blockingListClient(&inFlight, &peak, release), nopDispatcher{}, targetCluster{Name: "c"})
		w.gate = gate
		go func() {
			done <- w.Run(ctx, func(watching bool) {
				if watching {
					synced.Add(1)
				}
			})
		}()
	}
	eventually(t, 10*time.Second, func() bool { return inFlight.Load() == slots }, "the gate's slots to fill")
	// Give the other three every chance to list if the gate were not holding
	// them.
	time.Sleep(300 * time.Millisecond)
	if got := peak.Load(); got != slots {
		t.Fatalf("peak concurrent lists = %d; want exactly %d", got, slots)
	}
	if got := gate.inFlight(); got != slots {
		t.Fatalf("gate.inFlight = %d; want %d", got, slots)
	}
	close(release)
	eventually(t, 10*time.Second, func() bool { return synced.Load() == clusters }, "every cluster to sync")
	if got := peak.Load(); got > slots {
		t.Fatalf("peak concurrent lists = %d; want at most %d", got, slots)
	}
	eventually(t, 5*time.Second, func() bool { return gate.inFlight() == 0 }, "every slot to be released")
	cancel()
	for i := 0; i < clusters; i++ {
		if err := <-done; err != nil {
			t.Errorf("Run returned %v; want nil on shutdown", err)
		}
	}
}

// In watch-list mode the initial state arrives through the watch call. Two
// clusters share a gate of one: the second's watch is not opened until the
// first stream's initial-events bookmark arrives, and a stream that ends before
// its bookmark (the reflector gives up on it) hands the slot on too.
func TestRun_GateHoldsAWatchListSlotUntilTheBookmark(t *testing.T) {
	captureLog(t)
	gate := newListGate(1)
	var watches atomic.Int64
	var first atomic.Pointer[watch.FakeWatcher]
	newClient := func() *fake.Clientset {
		underlying := fake.NewClientset()
		allowPreflight(underlying)
		underlying.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
			t.Error("the reflector listed in watch-list mode")
			return false, nil, nil
		})
		underlying.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
			fw := watch.NewFakeWithChanSize(2, false)
			if watches.Add(1) == 1 {
				first.Store(fw)
			} else {
				fw.Action(watch.Bookmark, initialEventsEndBookmark())
			}
			return true, fw, nil
		})
		return underlying
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var synced atomic.Int64
	done := make(chan error, 2)
	start := func() {
		w := newWatcher(watchListCapableClient{newClient()}, nopDispatcher{}, targetCluster{Name: "streamed"})
		w.gate = gate
		go func() {
			done <- w.Run(ctx, func(watching bool) {
				if watching {
					synced.Add(1)
				}
			})
		}()
	}
	start()
	eventually(t, 10*time.Second, func() bool { return first.Load() != nil }, "the first stream to open")
	start()
	time.Sleep(300 * time.Millisecond)
	if got := watches.Load(); got != 1 {
		t.Fatalf("%d watches opened while the first stream held the only slot; want 1", got)
	}
	// The first stream's initial events end: its slot is released, and the
	// second cluster's stream opens and syncs on its own bookmark.
	first.Load().Action(watch.Bookmark, initialEventsEndBookmark())
	eventually(t, 10*time.Second, func() bool { return synced.Load() == 2 }, "both clusters to sync")
	if got := watches.Load(); got < 2 {
		t.Fatalf("%d watches opened after the bookmark; want the second cluster's", got)
	}
	eventually(t, 5*time.Second, func() bool { return gate.inFlight() == 0 }, "the slot to be free once both streams passed their bookmark")
	cancel()
	for i := 0; i < 2; i++ {
		if err := <-done; err != nil {
			t.Errorf("Run returned %v; want nil on shutdown", err)
		}
	}
}

func TestGatedWatch_ReleasesOnStopAndOnUpstreamClose(t *testing.T) {
	for _, tc := range []struct {
		name string
		end  func(fw *watch.FakeWatcher, gw watch.Interface)
	}{
		{"stop", func(_ *watch.FakeWatcher, gw watch.Interface) { gw.Stop() }},
		{"upstream close", func(fw *watch.FakeWatcher, _ watch.Interface) { fw.Stop() }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			gate := newListGate(1)
			release, err := gate.acquire(context.Background())
			if err != nil {
				t.Fatal(err)
			}
			fw := watch.NewFakeWithChanSize(1, false)
			gw := gatedWatch(fw, func(watch.Event) {}, release)
			if got := gate.inFlight(); got != 1 {
				t.Fatalf("inFlight before the end = %d; want 1", got)
			}
			tc.end(fw, gw)
			eventually(t, 5*time.Second, func() bool { return gate.inFlight() == 0 }, "the slot to be released")
			gw.Stop() // a second Stop is harmless
		})
	}
}

// A relist after a 410 Gone goes through the gate like the first list: with
// the only slot held elsewhere the reflector's second list waits, and proceeds
// once the slot is released.
func TestRun_RelistAfterGoneIsGated(t *testing.T) {
	captureLog(t)
	gate := newListGate(1)
	client := fake.NewClientset()
	allowPreflight(client)
	var lists atomic.Int64
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		lists.Add(1)
		return true, &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: "10"}}, nil
	})
	var openWatch atomic.Pointer[watch.FakeWatcher]
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		fw := watch.NewFakeWithChanSize(1, false)
		openWatch.Store(fw)
		return true, fw, nil
	})
	w := newWatcher(client, nopDispatcher{}, targetCluster{Name: "relisted"})
	w.gate = gate
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- w.Run(ctx, func(bool) {}) }()
	eventually(t, 10*time.Second, func() bool { return lists.Load() == 1 && openWatch.Load() != nil }, "the first list and watch")

	// Take the only slot, then make the watch unresumable.
	release, err := gate.acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	openWatch.Load().Error(&metav1.Status{
		Status:  metav1.StatusFailure,
		Code:    http.StatusGone,
		Reason:  metav1.StatusReasonExpired,
		Message: "too old resource version",
	})
	time.Sleep(300 * time.Millisecond)
	if got := lists.Load(); got != 1 {
		t.Fatalf("the reflector relisted (%d lists) while the only slot was held; want it to wait", got)
	}
	release()
	eventually(t, 10*time.Second, func() bool { return lists.Load() >= 2 }, "the relist once the slot was released")

	cancel()
	if err := <-done; err != nil {
		t.Errorf("Run returned %v; want nil on shutdown", err)
	}
}
