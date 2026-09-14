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
	"sync"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sruntime "k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/cache"
)

const (
	// forbiddenRetryInterval is how long an informer whose Event list or watch
	// the API server refused with 403 Forbidden waits before trying again. A
	// 403 is a permission the identity does not hold, and permissions change
	// on the order of minutes when someone edits an IAM binding or a
	// RoleBinding — not on the cadence the reflector's default backoff
	// assumes for a flapping connection. That backoff settles at 30 to 60
	// seconds between attempts (a 30-second cap with full jitter), so a fleet
	// where every cluster refuses the list is a refused request, logged twice,
	// from one cluster or another every second or two, for the life of the
	// process. The informer is kept, not stopped: a cluster whose permission
	// is granted or restored during the hold is picked up on the next
	// attempt, with no restart. One length before and after the initial sync:
	// a permission revoked from a running fleet is the same refused request
	// on the same clock, and cluster_up reports the held cluster as down for
	// the whole interval (see handleWatchError).
	forbiddenRetryInterval = 10 * time.Minute
)

// eventDispatcher represents the callback target for processed events.
// Decoupled into an interface to allow injecting mock implementations in tests.
type eventDispatcher interface {
	Dispatch(ctx context.Context, ev TriageEvent)
}

// errorHandlerOnce guards registration of the client-go error handler.
// runtime.ErrorHandlers is a process-global slice that client-go reads while
// informers are running, so registering from each Run call would both append
// duplicates — logging every informer error once per call — and, if Run is
// ever entered concurrently, race on the slice header.
var errorHandlerOnce sync.Once

// watcher manages the client-go event informer loop. It registers handlers
// for event creation (Add) and repeats (Update), converts raw Events to
// TriageEvent payloads, and forwards them to the eventDispatcher.
type watcher struct {
	client       kubernetes.Interface
	dispatcher   eventDispatcher
	cluster      targetCluster
	resyncPeriod time.Duration
	// forbiddenHold is the wait applied by handleWatchError after a 403; it is
	// forbiddenRetryInterval everywhere except tests, which shorten it.
	forbiddenHold time.Duration

	// onWatching is Run's callback, kept on the watcher so that
	// handleWatchError and the watch func of the ListWatch can report a
	// transition from their own goroutines. Nil until Run is entered.
	onWatching func(watching bool)
	// The watch state, guarded by stateMu: synced is set once Run's
	// WaitForCacheSync has returned, held while a 403 has the reflector
	// waiting and no watch has succeeded since, and watching is the last
	// value reported through onWatching. Two flags rather than one because
	// the reflector and Run observe the initial sync on different goroutines:
	// the reflector can have its first watch refused before Run has seen the
	// list complete, and the caller has to hear the same sequence either way
	// (see markSynced). stateMu is held across the onWatching call so that
	// transitions reach the callback in the order they happened; a gauge set
	// from them must end on the latest state, and an atomic flag alone would
	// not order the calls.
	stateMu  sync.Mutex
	synced   bool
	held     bool
	watching bool
}

// newWatcher constructs a watcher. resyncPeriod == 0 disables the
// periodic resync (informer only fires on real API events); non-zero
// values re-fire every registered event through the handler at that
// cadence — usually not what you want, so default 0 in main.go.
func newWatcher(client kubernetes.Interface, dispatcher eventDispatcher, cluster targetCluster, resyncPeriod time.Duration) *watcher {
	return &watcher{
		client:        client,
		dispatcher:    dispatcher,
		cluster:       cluster,
		resyncPeriod:  resyncPeriod,
		forbiddenHold: forbiddenRetryInterval,
	}
}

// Run starts the informer + handler goroutines and blocks until ctx
// is cancelled. Returns any startup error (e.g., initial list
// failure); shutdown-path errors are logged but not returned so
// callers can distinguish "startup failed, restart me" from "clean
// shutdown."
//
// onWatching is called with true once the initial list has completed, with
// false when a 403 Forbidden takes the cluster out of that state (see
// handleWatchError), and with true again when a later attempt succeeds. It
// fires once per transition, never twice with the same value, and never
// before the initial list has completed: a cluster held from its first list
// never hears anything. The call is made from an informer goroutine, so it
// must not block.
func (w *watcher) Run(ctx context.Context, onWatching func(watching bool)) error {
	w.onWatching = onWatching
	eventInformer := cache.NewSharedIndexInformer(w.newListWatch(), &corev1.Event{}, w.resyncPeriod, cache.Indexers{})

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
	// Must be registered before RunWithContext: the informer refuses a handler
	// once it is running.
	if err := eventInformer.SetWatchErrorHandlerWithContext(w.handleWatchError); err != nil {
		return fmt.Errorf("watcher: register watch error handler: %w", err)
	}
	// Report client-go's internal errors ("unknown object type in
	// cache" on shutdown, where cache.HandleCrash trips over
	// ctx.Done races) through our logger too. Note this appends to
	// runtime.ErrorHandlers rather than replacing it, so klog's
	// default UnhandledError line still fires alongside ours: the
	// slice ships with logError already in it and handleError runs
	// every entry. apimachinery v0.36 has no SetErrorHandlers, and
	// assigning the slice directly would drop the rate-limiting
	// backoff handler that sits beside logError. The default panic
	// handler still fires for real crashes. Registered once per
	// process — see errorHandlerOnce.
	errorHandlerOnce.Do(func() {
		runtime.ErrorHandlers = append(runtime.ErrorHandlers, func(_ context.Context, err error, _ string, _ ...any) {
			log.Printf("watcher: informer error: %v", err)
		})
	})

	go eventInformer.RunWithContext(ctx)
	// WaitForCacheSync blocks until the initial list is done —
	// without this, the first N events after startup would
	// arrive without their prior Count/LastTimestamp, breaking
	// the dedup logic.
	if !cache.WaitForCacheSync(ctx.Done(), handler.HasSynced) {
		return fmt.Errorf("watcher: cache sync failed (informer stopped before initial list completed)")
	}
	// Only now is this cluster actually being watched. Everything before here
	// is a cluster we are *trying* to watch: WaitForCacheSync has no timeout
	// and the reflector retries a failed initial list forever, so an
	// unreachable API server, a bad CA, or a missing events permission blocks
	// on the line above indefinitely rather than returning an error. Callers
	// that want to know whether a cluster is live have to be told, because
	// they cannot infer it from Run having not returned.
	w.markSynced()
	<-ctx.Done()
	return nil
}

// newListWatch builds the informer's list and watch calls: the same
// Events(NamespaceAll).List and .Watch the informer factory would make,
// wrapped the same way so the reflector uses watch-list semantics against a
// real client and not against the fake one in tests, plus two hooks — a watch
// call that returns without error reports the cluster as watching again, and
// a list that does ends a hold without reporting anything.
//
// The watch call is the recovery signal rather than the list because it is
// the last request the reflector makes before events flow, whichever mode it
// is in. Under client-go's WatchListClient feature, on by default since 0.35,
// a recovered reflector streams its initial state through the watch call and
// may never call List at all; and in the classic mode an identity that may
// list but not watch would otherwise read as up for the instant between each
// relist and the refused watch that follows it.
func (w *watcher) newListWatch() cache.ListerWatcher {
	return cache.ToListWatcherWithWatchListSemantics(&cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, opts metav1.ListOptions) (k8sruntime.Object, error) {
			list, err := w.client.CoreV1().Events(metav1.NamespaceAll).List(ctx, opts)
			if err == nil {
				w.listCompleted()
			}
			return list, err
		},
		WatchFuncWithContext: func(ctx context.Context, opts metav1.ListOptions) (watch.Interface, error) {
			wi, err := w.client.CoreV1().Events(metav1.NamespaceAll).Watch(ctx, opts)
			if err == nil {
				w.watchEstablished()
			}
			return wi, err
		},
	}, w.client)
}

// markSynced records that the initial list has completed and reports it. Run
// calls it once WaitForCacheSync returns, which is on Run's goroutine and so
// may come after the reflector has already had the watch that follows the
// list refused: in that case the caller hears true and then false here, the
// same two reports in the same order as when the 403 lands after Run has seen
// the sync. The cluster did complete its list either way, so the caller's
// count of synced clusters — and the no-cluster-synced exit in main.go built
// on it — is the same whichever goroutine got there first.
func (w *watcher) markSynced() {
	w.stateMu.Lock()
	defer w.stateMu.Unlock()
	w.synced = true
	w.reportLocked(true)
	if w.held {
		w.reportLocked(false)
	}
}

// listCompleted is the list func's hook: a list that returned without error
// ends the hold a refused list began, but reports nothing, because the watch
// that follows is the signal (see newListWatch). Without it a cluster held
// from its first list and then granted both permissions would be reported as
// synced, held and recovered within the space of the watch call whenever Run
// saw the list complete before the reflector opened the watch.
func (w *watcher) listCompleted() {
	w.stateMu.Lock()
	defer w.stateMu.Unlock()
	w.held = false
}

// markHeld records a 403 from the list or the watch. Before the initial list
// has completed there is nothing to report — the caller has not heard true
// yet — and markSynced picks the state up if the list completes meanwhile.
func (w *watcher) markHeld() {
	w.stateMu.Lock()
	defer w.stateMu.Unlock()
	w.held = true
	if w.synced {
		w.reportLocked(false)
	}
}

// watchEstablished is the watch func's hook: a watch call that returned
// without error ends any hold. Before the initial list has completed it only
// clears the flag — the first successful watch is opened before
// WaitForCacheSync returns, and Run reports that one — so the caller's first
// true still means "initial list complete" as it always has. After the sync
// it reports the transition back to watching that ends a hold.
func (w *watcher) watchEstablished() {
	w.stateMu.Lock()
	defer w.stateMu.Unlock()
	w.held = false
	if w.synced {
		w.reportLocked(true)
	}
}

// reportLocked reports a transition through onWatching, once per change. A
// repeat of the current state is dropped, so the informer's many watch calls
// and repeated holds cost the caller nothing. Caller holds stateMu.
func (w *watcher) reportLocked(watching bool) {
	if w.watching == watching {
		return
	}
	w.watching = watching
	if w.onWatching != nil {
		w.onWatching(watching)
	}
}

// handleWatchError is the informer's watch error handler. The reflector calls
// it synchronously from its retry loop, after a list or watch failed and
// before the backoff that precedes the next attempt, so time spent in here is
// added to the retry interval. That is the lever this uses: a 403 Forbidden,
// from the list or from the watch, before or after the initial sync, holds
// the reflector for forbiddenHold, or until the informer is stopped,
// whichever comes first. Every other error goes to the default handler
// unchanged and retries on the default backoff. The reflector wraps the list
// error with %w and passes the watch error through as is, so
// apierrors.IsForbidden sees the StatusError either way; a client-go that
// stopped wrapping would fall back to the default path, which is the pre-hold
// behaviour rather than a new failure.
//
// A cluster that has already synced is reported as not watching before the
// hold starts, and as watching again by the first watch call that succeeds
// afterwards (see newListWatch), so cluster_up reads 0 for the whole hold
// rather than 1 for a cluster whose events are up to forbiddenHold stale.
// That is what lets the hold apply after the sync at all: an identity whose
// permission is revoked mid-run, or that may list but not watch, gets the
// same treatment as one that never had it, because the operator who edited
// the binding did not make that distinction and cannot see it. A cluster
// whose list is refused never syncs, so nothing is reported for it: it stays
// at 0, WaitForCacheSync in Run stays blocked, and the no-cluster-synced exit
// in main.go still fires when every cluster is held from the start.
//
// One log line per attempt, and the default handler is skipped for the held
// 403 so klog's "Failed to watch" and the runtime.ErrorHandlers echo of it
// stay quiet too.
func (w *watcher) handleWatchError(ctx context.Context, r *cache.Reflector, err error) {
	if !apierrors.IsForbidden(err) {
		cache.DefaultWatchErrorHandler(ctx, r, err)
		return
	}
	w.markHeld()
	log.Printf("watcher: [%s] events forbidden, holding %s before the next attempt: %v", w.cluster.Name, w.forbiddenHold, err)
	hold := time.NewTimer(w.forbiddenHold)
	defer hold.Stop()
	select {
	case <-ctx.Done():
	case <-hold.C:
	}
}

// dispatch converts a *corev1.Event to the internal TriageEvent
// shape and hands it to the dispatcher. Extracted so both AddFunc
// and UpdateFunc share one code path. The watcher's own cluster name
// is stamped onto the event here, at the point where the source is
// unambiguous.
func (w *watcher) dispatch(ctx context.Context, ev *corev1.Event) {
	triage := toTriageEvent(ev, w.cluster)
	w.dispatcher.Dispatch(ctx, triage)
}

// toTriageEvent flattens a *corev1.Event to the internal payload
// shape. Timestamps prefer LastTimestamp (kubelet-set); fall back
// to EventTime / CreationTimestamp per k8s API convention.
// clusterName identifies the source cluster and is stamped onto the
// event so it reaches InjectPayload and the metric labels.
func toTriageEvent(ev *corev1.Event, cluster targetCluster) TriageEvent {
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
		Cluster:       cluster.Name,
		Project:       cluster.ProjectID,
		Location:      cluster.Location,
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
