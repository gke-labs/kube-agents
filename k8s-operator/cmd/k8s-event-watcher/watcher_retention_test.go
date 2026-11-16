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
	goruntime "runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const (
	// retentionListSize is how many events the fake cluster lists. At
	// retentionFieldsV1Bytes of managedFields apiece the list is over 20 MiB
	// of event objects before the rest of each object is counted.
	retentionListSize = 20_000
	// retentionFieldsV1Bytes approximates the managedFields entry the API
	// server writes on every event a kubelet records: one manager, one
	// FieldsV1 blob naming each field it set.
	retentionFieldsV1Bytes = 1024
	// retentionMessageBytes is a typical kubelet event message.
	retentionMessageBytes = 200
	// retentionCeilingBytesPerEvent bounds what the watcher may still hold
	// per listed event once the whole list has been delivered. A kept Event
	// of this shape is about 2.3 KiB on the heap; what remains after delivery
	// is the reflector's queue bookkeeping, whose map keeps its buckets at
	// the size of the largest list it has seen, at roughly a tenth of that.
	retentionCeilingBytesPerEvent = 256
	// heldCeilingBytesPerEvent bounds what the queue holds per listed event
	// while the initial batch waits for delivery. The Event of this shape
	// costs about 2.3 KiB with its managedFields and about 1.2 KiB without
	// them, so the ceiling sits between the two: the stripped batch fits, the
	// batch queued as listed does not.
	heldCeilingBytesPerEvent = 1700
)

// countingDispatcher counts Dispatch calls and keeps nothing else, so what
// the watcher retains is the only thing left to measure.
type countingDispatcher struct{ n atomic.Int64 }

func (d *countingDispatcher) Dispatch(context.Context, TriageEvent) { d.n.Add(1) }

func (d *countingDispatcher) RecordScaleUpMark(TriageEvent) bool { return false }

// kubeletShapedEvent is one listed event with the fields a kubelet-recorded
// core/v1 Event carries on a real cluster, managedFields included.
func kubeletShapedEvent(i int) corev1.Event {
	name := fmt.Sprintf("api-%d.18%08x", i, i)
	return corev1.Event{
		ObjectMeta: metav1.ObjectMeta{
			Name:              name,
			Namespace:         "default",
			UID:               types.UID(fmt.Sprintf("00000000-0000-0000-0000-%012d", i)),
			ResourceVersion:   fmt.Sprintf("%d", 1000+i),
			CreationTimestamp: metav1.Time{Time: time.Now()},
			ManagedFields: []metav1.ManagedFieldsEntry{{
				Manager:    "kubelet",
				Operation:  metav1.ManagedFieldsOperationUpdate,
				APIVersion: "v1",
				Time:       &metav1.Time{Time: time.Now()},
				FieldsType: "FieldsV1",
				FieldsV1:   &metav1.FieldsV1{Raw: []byte(strings.Repeat("f", retentionFieldsV1Bytes))},
			}},
		},
		InvolvedObject: corev1.ObjectReference{
			Kind:            "Pod",
			Namespace:       "default",
			Name:            fmt.Sprintf("api-%d", i),
			UID:             types.UID(fmt.Sprintf("10000000-0000-0000-0000-%012d", i)),
			APIVersion:      "v1",
			ResourceVersion: fmt.Sprintf("%d", 500+i),
			FieldPath:       "spec.containers{api}",
		},
		Reason:              "BackOff",
		Message:             strings.Repeat("m", retentionMessageBytes),
		Source:              corev1.EventSource{Component: "kubelet", Host: fmt.Sprintf("gke-node-%d", i%1900)},
		FirstTimestamp:      metav1.Time{Time: time.Now()},
		LastTimestamp:       metav1.Time{Time: time.Now()},
		Count:               3,
		Type:                "Normal",
		ReportingController: "kubelet",
		ReportingInstance:   fmt.Sprintf("gke-node-%d", i%1900),
	}
}

// liveHeapBytes is the heap in use after a full collection, so what it
// measures is what is still referenced, not what has merely not been swept.
func liveHeapBytes() uint64 {
	goruntime.GC()
	var ms goruntime.MemStats
	goruntime.ReadMemStats(&ms)
	return ms.HeapAlloc
}

// TestRun_RetainsNothingOfTheEventsItHasDelivered pins the memory contract
// behind the agent-api-auth container's limit: once an event has been
// converted and handed to the dispatcher, the watcher holds no reference to
// it. Nothing in this binary reads a cached Event back — the dedup and
// scale-up memos keep their own bounded entries — so a store that kept every
// listed Event would scale the process with the fleet's event backlog for no
// reader, which is how a 26-cluster fleet with 1,900-node clusters took the
// container past 2Gi within minutes of its informers syncing.
func TestRun_RetainsNothingOfTheEventsItHasDelivered(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	// Built inside the reactor so the test itself holds no reference to the
	// list once the reflector has consumed it.
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		list := &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: "10"}}
		list.Items = make([]corev1.Event, 0, retentionListSize)
		for i := 0; i < retentionListSize; i++ {
			list.Items = append(list.Items, kubeletShapedEvent(i))
		}
		return true, list, nil
	})
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		return true, watch.NewFakeWithChanSize(1, false), nil
	})
	rec := &countingDispatcher{}
	w := newWatcher(client, rec, targetCluster{Name: "large"})

	before := liveHeapBytes()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	synced := make(chan struct{})
	go func() {
		done <- w.Run(ctx, func(watching bool) {
			if watching {
				close(synced)
			}
		})
	}()
	select {
	case <-synced:
	case <-time.After(30 * time.Second):
		t.Fatal("the reflector never synced")
	}
	eventually(t, 30*time.Second, func() bool { return rec.n.Load() >= retentionListSize }, "the list to be delivered in full")

	retained := int64(liveHeapBytes()) - int64(before)
	ceiling := int64(retentionListSize * retentionCeilingBytesPerEvent)
	t.Logf("live heap after delivering %d events: %+d bytes over the baseline (%d per event)", retentionListSize, retained, retained/retentionListSize)
	if retained > ceiling {
		t.Errorf("the watcher retains %d bytes after delivering %d events; want under %d (it is holding the events it has already dispatched)",
			retained, retentionListSize, ceiling)
	}

	cancel()
	if err := <-done; err != nil {
		t.Errorf("Run returned %v; want nil on shutdown", err)
	}
}

// TestRun_QueueHoldsTheInitialListWithoutManagedFields pins the transformer
// on Run's queue: the initial batch sits in the DeltaFIFO until the single
// delivery goroutine has popped it, and that residency is what the startup
// burst costs per cluster, so an Event is held without its managedFields. The
// dispatcher stalls on the first delivery, which keeps the whole list in the
// queue and the delivery channel, and the live heap is measured while it
// waits there: under the ceiling with the transformer, over it with the batch
// queued as the API server listed it.
func TestRun_QueueHoldsTheInitialListWithoutManagedFields(t *testing.T) {
	captureLog(t)
	client := fake.NewClientset()
	allowPreflight(client)
	client.PrependReactor("list", "events", func(k8stesting.Action) (bool, runtime.Object, error) {
		list := &corev1.EventList{ListMeta: metav1.ListMeta{ResourceVersion: "10"}}
		list.Items = make([]corev1.Event, 0, retentionListSize)
		for i := 0; i < retentionListSize; i++ {
			list.Items = append(list.Items, kubeletShapedEvent(i))
		}
		return true, list, nil
	})
	client.PrependWatchReactor("events", func(k8stesting.Action) (bool, watch.Interface, error) {
		return true, watch.NewFakeWithChanSize(1, false), nil
	})
	rec := &stallingDispatcher{release: make(chan struct{})}
	w := newWatcher(client, rec, targetCluster{Name: "large"})

	before := liveHeapBytes()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	synced := make(chan struct{})
	go func() {
		done <- w.Run(ctx, func(watching bool) {
			if watching {
				close(synced)
			}
		})
	}()
	// The reflector queues the whole list in one Replace before the first
	// pop, so once a delivery has entered the dispatcher the batch is held.
	eventually(t, 30*time.Second, func() bool { return rec.entered.Load() >= 1 }, "the first delivery to enter the dispatcher")

	held := int64(liveHeapBytes()) - int64(before)
	ceiling := int64(retentionListSize * heldCeilingBytesPerEvent)
	t.Logf("live heap while the queue holds %d undelivered events: %+d bytes over the baseline (%d per event)", retentionListSize, held, held/retentionListSize)
	if held > ceiling {
		t.Errorf("the queue holds %d bytes for %d undelivered events; want under %d (the events are queued with their managedFields)",
			held, retentionListSize, ceiling)
	}

	close(rec.release)
	select {
	case <-synced:
	case <-time.After(30 * time.Second):
		t.Fatal("the reflector never synced once delivery was released")
	}
	cancel()
	if err := <-done; err != nil {
		t.Errorf("Run returned %v; want nil on shutdown", err)
	}
}
