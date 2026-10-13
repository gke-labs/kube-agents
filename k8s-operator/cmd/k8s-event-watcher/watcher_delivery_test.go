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
	"fmt"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
	"k8s.io/client-go/tools/cache"
)

// syncHoldProbe is how long a test watches for a sync report that must not
// come. WaitForCacheSync polls every 100 ms, so a sync wrongly reported at the
// list's arrival shows well inside it.
const syncHoldProbe = 1500 * time.Millisecond

// stallingDispatcher blocks every Dispatch until release is closed, and
// counts the calls that have entered.
type stallingDispatcher struct {
	entered atomic.Int64
	release chan struct{}
}

func (d *stallingDispatcher) Dispatch(ctx context.Context, _ TriageEvent) {
	d.entered.Add(1)
	select {
	case <-d.release:
	case <-ctx.Done():
	}
}

func (d *stallingDispatcher) RecordScaleUpMark(TriageEvent) bool { return false }

// listOf builds a fake Event list of n distinct pods.
func listOf(n int) *corev1.EventList {
	list := &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: "10"}}
	for i := 0; i < n; i++ {
		list.Items = append(list.Items, listedEvent(fmt.Sprintf("pod-%d", i), "BackOff", fmt.Sprintf("api-%d.1", i)))
	}
	return list
}

// TestRun_SyncWaitsForTheInitialListToBeDelivered pins the contract the
// shared informer kept and this pipeline has to keep: the sync — and so
// cluster_up and the no-cluster-synced exit in main.go — is reported once
// every Event of the initial list has been handed to the dispatcher and the
// dispatcher has returned, not when the list has merely been received. The
// gauge's 1 means events are flowing, and a daemon that is wedged during the
// initial list is exactly what the two-minute exit exists to expose.
func TestRun_SyncWaitsForTheInitialListToBeDelivered(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		return true, listOf(deliveryQueueDepth + 2), nil
	})
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		return true, watch.NewFakeWithChanSize(1, false), nil
	})
	rec := &stallingDispatcher{release: make(chan struct{})}
	w := newWatcher(client, rec, targetCluster{Name: "stalled-daemon"})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	synced := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- w.Run(ctx, func(watching bool) {
			if watching {
				close(synced)
			}
		})
	}()
	eventually(t, 10*time.Second, func() bool { return rec.entered.Load() >= 1 }, "delivery to begin")
	select {
	case <-synced:
		t.Fatalf("the sync was reported while the daemon stalled on the first of %d listed events", deliveryQueueDepth+2)
	case <-time.After(syncHoldProbe):
	}
	if got := rec.entered.Load(); got != 1 {
		t.Errorf("%d dispatches entered while the first was stalled; want 1 (one delivery goroutine per cluster)", got)
	}

	close(rec.release)
	select {
	case <-synced:
	case <-time.After(10 * time.Second):
		t.Fatal("the sync was not reported within 10s of the daemon recovering")
	}
	cancel()
	if err := <-done; err != nil {
		t.Errorf("Run returned %v; want nil on shutdown", err)
	}
}

// TestEventFromDelta_SkipsDeletions pins that an Event expiring is not an
// observation: eventFromDelta, which Run's Process loop calls for every delta
// the queue pops, delivers an Add, Update, Sync or Replaced and drops a
// Deleted and a tombstone. With no known-objects store the DeltaFIFO already
// drops a Deleted for a key it has popped, so this is the guard for the one
// Deleted that still reaches Process: one coalesced behind a queued Add of the
// same key.
func TestEventFromDelta_SkipsDeletions(t *testing.T) {
	ev := listedEvent("pod-1", "BackOff", "api.1")
	for _, tc := range []struct {
		name string
		dt   cache.DeltaType
		want bool
	}{
		{"added", cache.Added, true},
		{"updated", cache.Updated, true},
		{"sync", cache.Sync, true},
		{"replaced", cache.Replaced, true},
		{"deleted", cache.Deleted, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := eventFromDelta(cache.Delta{Type: tc.dt, Object: &ev})
			if ok != tc.want {
				t.Fatalf("eventFromDelta(%s) ok = %v; want %v", tc.dt, ok, tc.want)
			}
			if tc.want && got == nil {
				t.Fatalf("eventFromDelta(%s) returned a nil event with ok=true", tc.dt)
			}
		})
	}
	t.Run("tombstone", func(t *testing.T) {
		tomb := cache.DeletedFinalStateUnknown{Key: "default/api.1", Obj: &ev}
		if _, ok := eventFromDelta(cache.Delta{Type: cache.Deleted, Object: tomb}); ok {
			t.Error("a DeletedFinalStateUnknown tombstone was delivered; want it dropped")
		}
	})
}
