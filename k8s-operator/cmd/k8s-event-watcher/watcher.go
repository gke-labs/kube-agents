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
	"log"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/client-go/informers"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
)

// eventDispatcher represents the callback target for processed events.
// Decoupled into an interface to allow injecting mock implementations in tests.
type eventDispatcher interface {
	Dispatch(ctx context.Context, ev TriageEvent)
}

// watcher manages the client-go event informer loop. It registers handlers
// for event creation (Add) and repeats (Update), converts raw Events to
// TriageEvent payloads, and forwards them to the eventDispatcher.
type watcher struct {
	client       kubernetes.Interface
	dispatcher   eventDispatcher
	clusterName  string
	resyncPeriod time.Duration
}

// newWatcher constructs a watcher. resyncPeriod == 0 disables the
// periodic resync (informer only fires on real API events); non-zero
// values re-fire every registered event through the handler at that
// cadence — usually not what you want, so default 0 in main.go.
func newWatcher(client kubernetes.Interface, dispatcher eventDispatcher, clusterName string, resyncPeriod time.Duration) *watcher {
	return &watcher{
		client:       client,
		dispatcher:   dispatcher,
		clusterName:  clusterName,
		resyncPeriod: resyncPeriod,
	}
}

// Run starts the informer + handler goroutines and blocks until ctx
// is cancelled. Returns any startup error (e.g., initial list
// failure); shutdown-path errors are logged but not returned so
// callers can distinguish "startup failed, restart me" from "clean
// shutdown."
func (w *watcher) Run(ctx context.Context) error {
	factory := informers.NewSharedInformerFactory(w.client, w.resyncPeriod)
	eventInformer := factory.Core().V1().Events().Informer()

	handler, err := eventInformer.AddEventHandler(cache.ResourceEventHandlerFuncs{
		AddFunc: func(obj any) {
			ev, ok := obj.(*corev1.Event)
			if !ok {
				log.Printf("watcher: unexpected object type on Add: %T", obj)
				return
			}
			w.dispatch(ctx, ev)
		},
		UpdateFunc: func(_, newObj any) {
			// Update fires when the k8s API bumps the Event's
			// Count / LastTimestamp (kubelet reports a repeat).
			// We treat each update as another observation so
			// persistent failures continue to feed the dedup
			// window's LastSeen bump.
			ev, ok := newObj.(*corev1.Event)
			if !ok {
				log.Printf("watcher: unexpected object type on Update: %T", newObj)
				return
			}
			w.dispatch(ctx, ev)
		},
		// No DeleteFunc — event deletion is not a signal we care
		// about; the underlying incident may or may not be
		// resolved and we don't want to trigger investigations
		// on tombstones.
	})
	if err != nil {
		return fmt.Errorf("watcher: register event handler: %w", err)
	}
	// Silence the client-go internal error log ("unknown object
	// type in cache") on shutdown — cache.HandleCrash trips over
	// ctx.Done races otherwise. The default panic handler still
	// fires for real crashes.
	runtime.ErrorHandlers = append(runtime.ErrorHandlers, func(_ context.Context, err error, _ string, _ ...any) {
		log.Printf("watcher: informer error: %v", err)
	})

	factory.Start(ctx.Done())
	// WaitForCacheSync blocks until the initial list is done —
	// without this, the first N events after startup would
	// arrive without their prior Count/LastTimestamp, breaking
	// the dedup logic.
	if !cache.WaitForCacheSync(ctx.Done(), handler.HasSynced) {
		return fmt.Errorf("watcher: cache sync failed (informer stopped before initial list completed)")
	}
	<-ctx.Done()
	return nil
}

// dispatch converts a *corev1.Event to the internal TriageEvent
// shape and hands it to the dispatcher. Extracted so both AddFunc
// and UpdateFunc share one code path. The cluster name is added
// downstream (dispatcher.Dispatch stamps it onto InjectPayload)
// rather than TriageEvent so tests don't have to thread it through.
func (w *watcher) dispatch(ctx context.Context, ev *corev1.Event) {
	triage := toTriageEvent(ev)
	w.dispatcher.Dispatch(ctx, triage)
}

// toTriageEvent flattens a *corev1.Event to the internal payload
// shape. Timestamps prefer LastTimestamp (kubelet-set); fall back
// to EventTime / CreationTimestamp per k8s API convention.
func toTriageEvent(ev *corev1.Event) TriageEvent {
	first := ev.FirstTimestamp.Time
	if first.IsZero() {
		first = ev.EventTime.Time
	}
	if first.IsZero() {
		first = ev.CreationTimestamp.Time
	}
	last := ev.LastTimestamp.Time
	if last.IsZero() {
		last = ev.EventTime.Time
	}
	if last.IsZero() {
		last = ev.CreationTimestamp.Time
	}

	// The event references its target via InvolvedObject.
	// InvolvedObject.UID is what we key dedup on.
	uid := string(ev.InvolvedObject.UID)

	// ControllerRef: for a Pod, the parent ReplicaSet /
	// Deployment / StatefulSet is on OwnerReferences. Populating
	// this requires an additional Pod GET which we don't have
	// in-hand here. Left empty; the recipe includes RBAC for
	// pod GET so the agent can enrich via MCP if needed.
	controllerRef := ""

	return TriageEvent{
		Key: EventKey{
			UID:    uid,
			Reason: ev.Reason,
		},
		Namespace:     ev.InvolvedObject.Namespace,
		KindOfObject:  ev.InvolvedObject.Kind,
		Name:          ev.InvolvedObject.Name,
		Container:     ev.InvolvedObject.FieldPath,
		Message:       truncateMessage(ev.Message),
		FirstSeen:     first,
		LastSeen:      last,
		ControllerRef: controllerRef,
		Node:          nodeFromSource(ev),
		Labels:        labelsFromMeta(ev.ObjectMeta),
		Count:         int(ev.Count),
		Type:          ev.Type,
	}
}

// truncateMessage caps the payload's message field. K8s event
// messages are supposed to be small but we've seen kubelet emit
// multi-KB stack traces; playbook skills don't need more than a
// few hundred bytes to categorize.
func truncateMessage(msg string) string {
	const max = 2048
	if len(msg) <= max {
		return msg
	}
	return msg[:max] + "... [truncated by k8s-event-watcher]"
}

// nodeFromSource pulls the node name out of an Event's Source or
// ReportingController fields, whichever the API server populated.
func nodeFromSource(ev *corev1.Event) string {
	if ev.Source.Host != "" {
		return ev.Source.Host
	}
	if ev.ReportingInstance != "" {
		return ev.ReportingInstance
	}
	return ""
}

// labelsFromMeta returns a shallow copy of the event's own labels
// (not the involved object's — that would require an extra API
// call). Empty when no labels are set.
func labelsFromMeta(m metav1.ObjectMeta) map[string]string {
	if len(m.Labels) == 0 {
		return nil
	}
	out := make(map[string]string, len(m.Labels))
	for k, v := range m.Labels {
		out[k] = v
	}
	return out
}
