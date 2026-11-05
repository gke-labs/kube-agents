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

package fakeapiserver

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sruntime "k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/cache"
	"k8s.io/utils/ptr"
)

// small is the dataset the fake's own tests use: enough for several pages
// and for the cross-references to spread, small enough to list in full.
var small = Sizes{Nodes: 7, Pods: 23, Events: 61}

func startSmall(t *testing.T, seed int64) *Server {
	t.Helper()
	s := Start(Options{Seed: seed, Sizes: small})
	t.Cleanup(s.Close)
	return s
}

// clientFor builds a clientset asking for the given wire format, the way
// the watcher's newClusterClient asks for protobuf first.
func clientFor(t *testing.T, s *Server, contentType string) kubernetes.Interface {
	t.Helper()
	cfg := s.Config()
	cfg.ContentType = contentType
	cfg.AcceptContentTypes = contentType
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func wireFormats() map[string]string {
	return map[string]string{"json": k8sruntime.ContentTypeJSON, "protobuf": k8sruntime.ContentTypeProtobuf}
}

func receive(t *testing.T, w watch.Interface, timeout time.Duration) watch.Event {
	t.Helper()
	select {
	case ev, ok := <-w.ResultChan():
		if !ok {
			t.Fatal("watch closed")
		}
		return ev
	case <-time.After(timeout):
		t.Fatal("no watch event within the timeout")
	}
	return watch.Event{}
}

func eventually(t *testing.T, timeout time.Duration, cond func() bool, what string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// Every page of a paged Event list, in both formats, through a real
// clientset: the pages join to the generator's Events in order, with the
// versions the server assigned, and the list reports what remains.
func TestList_PaginatesInBothFormats(t *testing.T) {
	for name, ct := range wireFormats() {
		t.Run(name, func(t *testing.T) {
			s := startSmall(t, 1)
			client := clientFor(t, s, ct)
			var items []corev1.Event
			var pages int
			opts := metav1.ListOptions{Limit: 10}
			for {
				list, err := client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), opts)
				if err != nil {
					t.Fatal(err)
				}
				pages++
				items = append(items, list.Items...)
				if list.Continue == "" {
					if list.RemainingItemCount != nil {
						t.Errorf("last page reports %d remaining", *list.RemainingItemCount)
					}
					break
				}
				if list.RemainingItemCount == nil || *list.RemainingItemCount != int64(small.Events-len(items)) {
					t.Errorf("page %d: remainingItemCount = %v, want %d", pages, list.RemainingItemCount, small.Events-len(items))
				}
				if list.ResourceVersion != s.ResourceVersion() {
					t.Errorf("page %d served at %s, want %s", pages, list.ResourceVersion, s.ResourceVersion())
				}
				opts = metav1.ListOptions{Limit: 10, Continue: list.Continue}
			}
			if pages != 7 || len(items) != small.Events {
				t.Fatalf("%d pages, %d items; want 7 pages of %d items", pages, len(items), small.Events)
			}
			gen := NewGenerator(1, small)
			for i, got := range items {
				want := gen.Event(i)
				if got.Name != want.Name || got.UID != want.UID || got.Message != want.Message {
					t.Fatalf("item %d = %s/%s, want %s/%s", i, got.Namespace, got.Name, want.Namespace, want.Name)
				}
				if got.ResourceVersion != strconv.Itoa(small.Nodes+small.Pods+i+1) {
					t.Fatalf("item %d at version %s, want %d", i, got.ResourceVersion, small.Nodes+small.Pods+i+1)
				}
				if len(got.ManagedFields) != 1 {
					t.Fatalf("item %d carries %d managedFields entries, want 1", i, len(got.ManagedFields))
				}
			}
			if s.Counts().Lists != int64(pages) {
				t.Errorf("Lists = %d, want %d", s.Counts().Lists, pages)
			}
		})
	}
}

// The wire really is protobuf when asked for: the raw response carries the
// protobuf media type and the serializer's magic prefix, and a watch stream
// the stream parameter.
func TestWire_ProtobufWhenAccepted(t *testing.T) {
	s := startSmall(t, 1)
	get := func(url, accept string) *http.Response {
		t.Helper()
		req, _ := http.NewRequest(http.MethodGet, url, nil)
		req.Header.Set(acceptHeader, accept)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	resp := get(s.URL+nodesPath+"?limit=1", k8sruntime.ContentTypeProtobuf+","+k8sruntime.ContentTypeJSON)
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if ct := resp.Header.Get(contentTypeHeader); ct != k8sruntime.ContentTypeProtobuf {
		t.Errorf("list Content-Type = %q", ct)
	}
	if !bytes.HasPrefix(body, []byte("k8s\x00")) {
		t.Errorf("list body does not start with the protobuf magic: %q", body[:min(8, len(body))])
	}
	resp = get(s.URL+nodesPath+"?limit=1", k8sruntime.ContentTypeJSON)
	body, _ = io.ReadAll(resp.Body)
	resp.Body.Close()
	if ct := resp.Header.Get(contentTypeHeader); ct != k8sruntime.ContentTypeJSON || !bytes.HasPrefix(body, []byte("{")) {
		t.Errorf("JSON list: Content-Type %q, body %q", ct, body[:min(8, len(body))])
	}
	resp = get(s.URL+eventsPath+"?watch=true&resourceVersion="+s.ResourceVersion()+"&timeoutSeconds=1", k8sruntime.ContentTypeProtobuf)
	resp.Body.Close()
	if ct := resp.Header.Get(contentTypeHeader); ct != k8sruntime.ContentTypeProtobuf+watchStreamSuffix {
		t.Errorf("watch Content-Type = %q", ct)
	}
}

// Nodes and Pods come back whole, with the payloads a real cluster's carry.
func TestList_NodesAndPods(t *testing.T) {
	s := startSmall(t, 3)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	nodes, err := client.CoreV1().Nodes().List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(nodes.Items) != small.Nodes || len(nodes.Items[0].Status.Conditions) == 0 || len(nodes.Items[0].ManagedFields) != 3 {
		t.Fatalf("%d nodes, first with %d conditions and %d managedFields entries", len(nodes.Items), len(nodes.Items[0].Status.Conditions), len(nodes.Items[0].ManagedFields))
	}
	pods, err := client.CoreV1().Pods(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(pods.Items) != small.Pods {
		t.Fatalf("%d pods", len(pods.Items))
	}
	nodeNames := map[string]bool{}
	for _, n := range nodes.Items {
		nodeNames[n.Name] = true
	}
	for _, p := range pods.Items {
		if len(p.ManagedFields) != 3 || p.ManagedFields[2].Subresource != subresourceStatus {
			t.Fatalf("pod %s carries managedFields %+v", p.Name, p.ManagedFields)
		}
		if !nodeNames[p.Spec.NodeName] {
			t.Fatalf("pod %s runs on %q, not one of the cluster's nodes", p.Name, p.Spec.NodeName)
		}
	}
	if s.Counts().Expired != 0 || s.Counts().Throttled != 0 {
		t.Errorf("counts after two plain lists: %+v", s.Counts())
	}
}

// The first k Event requests are 429 with Retry-After; client-go retries
// them on its own, so the caller sees the throttle as latency, and the
// preflight's review is never throttled.
func TestThrottleFirst_RetriedByClientGo(t *testing.T) {
	s := startSmall(t, 1)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	s.ThrottleFirst(2, time.Second)
	review, err := client.AuthorizationV1().SelfSubjectAccessReviews().Create(context.Background(), &authorizationv1.SelfSubjectAccessReview{
		Spec: authorizationv1.SelfSubjectAccessReviewSpec{ResourceAttributes: &authorizationv1.ResourceAttributes{Resource: "events", Verb: "list"}},
	}, metav1.CreateOptions{})
	if err != nil || !review.Status.Allowed || review.Spec.ResourceAttributes.Verb != "list" {
		t.Fatalf("review while throttled: %+v, %v", review, err)
	}
	start := time.Now()
	list, err := client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if len(list.Items) != small.Events {
		t.Fatalf("%d items after the throttle", len(list.Items))
	}
	if elapsed := time.Since(start); elapsed < 2*time.Second {
		t.Errorf("the list took %s; two Retry-After of 1s should have held it two seconds", elapsed)
	}
	if c := s.Counts(); c.Throttled != 2 || c.Lists != 1 || c.Requests != 4 {
		t.Errorf("counts = %+v; want 2 throttled, 1 list, 4 requests", c)
	}
}

// A 429 the client does not retry (a request with a body is never retried)
// is still what the API server sends: the status, the header and the Status
// body.
func TestThrottleFirst_Response(t *testing.T) {
	s := startSmall(t, 1)
	s.ThrottleFirst(1, 1500*time.Millisecond)
	resp, err := http.Get(s.URL + eventsPath)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusTooManyRequests || resp.Header.Get(retryAfterHeader) != "2" {
		t.Fatalf("status %d, Retry-After %q", resp.StatusCode, resp.Header.Get(retryAfterHeader))
	}
	resp, err = http.Get(s.URL + eventsPath + "?limit=1")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("second request: status %d", resp.StatusCode)
	}
}

// A version below the floor is 410, on a fresh list and on a continue token
// taken before Expire; the newest state is served again from no version.
func TestExpire_ListsBelowTheFloorAre410(t *testing.T) {
	s := startSmall(t, 1)
	client := clientFor(t, s, k8sruntime.ContentTypeJSON)
	first, err := client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{Limit: 5})
	if err != nil {
		t.Fatal(err)
	}
	before := first.ResourceVersion
	s.Expire()
	_, err = client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{ResourceVersion: before})
	if !apierrors.IsResourceExpired(err) {
		t.Fatalf("list at the pre-expiry version: %v, want 410 Expired", err)
	}
	_, err = client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{Continue: first.Continue})
	if !apierrors.IsResourceExpired(err) {
		t.Fatalf("continue from before the expiry: %v, want 410 Expired", err)
	}
	after, err := client.CoreV1().Events(metav1.NamespaceAll).List(context.Background(), metav1.ListOptions{})
	if err != nil || len(after.Items) != small.Events {
		t.Fatalf("list after the expiry: %d items, %v", len(after.Items), err)
	}
	if after.ResourceVersion != s.ResourceVersion() || after.ResourceVersion == before {
		t.Errorf("post-expiry list at %s (was %s, server at %s)", after.ResourceVersion, before, s.ResourceVersion())
	}
	if s.Counts().Expired != 2 {
		t.Errorf("Expired = %d, want 2", s.Counts().Expired)
	}
}

// The watch-list protocol: a watch with sendInitialEvents streams the state
// as Added events and ends them with the annotated bookmark at the list's
// version; an Event added afterwards follows on the same stream.
func TestWatchList_BookmarkThenLive(t *testing.T) {
	for name, ct := range wireFormats() {
		t.Run(name, func(t *testing.T) {
			s := startSmall(t, 2)
			client := clientFor(t, s, ct)
			w, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{
				SendInitialEvents: ptr.To(true), AllowWatchBookmarks: true, ResourceVersionMatch: metav1.ResourceVersionMatchNotOlderThan,
			})
			if err != nil {
				t.Fatal(err)
			}
			defer w.Stop()
			for i := 0; i < small.Events; i++ {
				ev := receive(t, w, 5*time.Second)
				if ev.Type != watch.Added {
					t.Fatalf("event %d is %s", i, ev.Type)
				}
			}
			bookmark := receive(t, w, 5*time.Second)
			obj, ok := bookmark.Object.(*corev1.Event)
			if bookmark.Type != watch.Bookmark || !ok || obj.Annotations[metav1.InitialEventsAnnotationKey] != initialEventsEndValue || obj.ResourceVersion != s.ResourceVersion() {
				t.Fatalf("after the initial events: %s %+v", bookmark.Type, bookmark.Object)
			}
			if s.Counts().WatchLists != 1 {
				t.Errorf("WatchLists = %d", s.Counts().WatchLists)
			}
			rv := s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "live-one", Namespace: "team-0"}, Reason: "Live"})
			live := receive(t, w, 5*time.Second)
			got, ok := live.Object.(*corev1.Event)
			if live.Type != watch.Added || !ok || got.Name != "live-one" || got.ResourceVersion != rv {
				t.Fatalf("live event: %s %+v", live.Type, live.Object)
			}
		})
	}
}

// Without sendInitialEvents a watch from a version carries only what was
// added after it, and asking for initial events without bookmarks is refused
// as the API server refuses it.
func TestWatch_FromVersionCatchesUp(t *testing.T) {
	s := startSmall(t, 2)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	firstRV := s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "first", Namespace: "team-0"}})
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "second", Namespace: "team-0"}})
	w, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{ResourceVersion: firstRV})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Stop()
	ev := receive(t, w, 5*time.Second)
	if got := ev.Object.(*corev1.Event); ev.Type != watch.Added || got.Name != "second" {
		t.Fatalf("first event from %s: %s %s", firstRV, ev.Type, got.Name)
	}
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "third", Namespace: "team-0"}})
	if ev := receive(t, w, 5*time.Second); ev.Object.(*corev1.Event).Name != "third" {
		t.Fatalf("live event: %+v", ev.Object)
	}
	_, err = client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{SendInitialEvents: ptr.To(true)})
	if !apierrors.IsBadRequest(err) {
		t.Fatalf("sendInitialEvents without bookmarks: %v, want 400", err)
	}
}

// A Node or Pod watch resumed from a version, the re-watch a reflector
// issues after its list, carries none of the Events added since: those
// belong to Event streams, as they do in a snapshot and in a live delivery.
// A Node reflector handed an Event logs a type mismatch and drops it, and a
// typed caller asserting *corev1.Node panics.
func TestWatch_FromVersionKeepsAddedEventsOffNodesAndPods(t *testing.T) {
	s := startSmall(t, 2)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	listed := s.ResourceVersion()
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "before-the-watch", Namespace: "team-0"}})
	nodes, err := client.CoreV1().Nodes().Watch(context.Background(), metav1.ListOptions{ResourceVersion: listed})
	if err != nil {
		t.Fatal(err)
	}
	defer nodes.Stop()
	pods, err := client.CoreV1().Pods(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{ResourceVersion: listed})
	if err != nil {
		t.Fatal(err)
	}
	defer pods.Stop()
	events, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{ResourceVersion: listed})
	if err != nil {
		t.Fatal(err)
	}
	defer events.Stop()
	// The Event stream delivers the catch-up and then a live one; by the time
	// both have crossed the wire, anything the Node and Pod streams were
	// handed at open has too.
	if ev := receive(t, events, 5*time.Second); ev.Object.(*corev1.Event).Name != "before-the-watch" {
		t.Fatalf("catch-up on the Event stream: %+v", ev.Object)
	}
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "while-watching", Namespace: "team-0"}})
	if ev := receive(t, events, 5*time.Second); ev.Object.(*corev1.Event).Name != "while-watching" {
		t.Fatalf("live Event on the Event stream: %+v", ev.Object)
	}
	for name, w := range map[string]watch.Interface{"nodes": nodes, "pods": pods} {
		select {
		case ev, ok := <-w.ResultChan():
			t.Errorf("the %s watch from %s received %s %T (open %v); added Events belong to Event streams", name, listed, ev.Type, ev.Object, ok)
		case <-time.After(200 * time.Millisecond):
		}
	}
}

// Expire ends an open watch with a 410 Error event, a watch resumed at the
// version the client last saw is answered with the same, and one from no
// version streams the state again.
func TestExpire_EndsOpenWatchesWith410(t *testing.T) {
	s := startSmall(t, 2)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	w, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{ResourceVersion: s.ResourceVersion()})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Stop()
	last := s.ResourceVersion()
	s.Expire()
	ev := receive(t, w, 5*time.Second)
	status, ok := ev.Object.(*metav1.Status)
	if ev.Type != watch.Error || !ok || status.Code != http.StatusGone || status.Reason != metav1.StatusReasonExpired {
		t.Fatalf("after Expire: %s %+v", ev.Type, ev.Object)
	}
	if _, open := <-w.ResultChan(); open {
		t.Fatal("the expired watch stayed open")
	}
	stale, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{
		ResourceVersion: last, SendInitialEvents: ptr.To(true), AllowWatchBookmarks: true, ResourceVersionMatch: metav1.ResourceVersionMatchNotOlderThan,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer stale.Stop()
	if ev := receive(t, stale, 5*time.Second); ev.Type != watch.Error || !apierrors.IsResourceExpired(apierrors.FromObject(ev.Object)) {
		t.Fatalf("watch-list at the stale version: %s %+v", ev.Type, ev.Object)
	}
	fresh, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{
		SendInitialEvents: ptr.To(true), AllowWatchBookmarks: true, ResourceVersionMatch: metav1.ResourceVersionMatchNotOlderThan,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Stop()
	n := 0
	for ev := receive(t, fresh, 5*time.Second); ev.Type != watch.Bookmark; ev = receive(t, fresh, 5*time.Second) {
		n++
	}
	if n != small.Events {
		t.Fatalf("%d initial events after the expiry, want %d", n, small.Events)
	}
	if c := s.Counts(); c.Expired != 2 || c.Watches != 3 || c.WatchLists != 1 {
		t.Errorf("counts = %+v; want 2 expired, 3 watches, 1 watch-list", c)
	}
	if s.ResourceVersion() != strconv.Itoa(small.Nodes+small.Pods+small.Events+1) {
		t.Errorf("version after Expire = %s", s.ResourceVersion())
	}
}

// A client-go reflector syncs from the fake in watch-list mode, holds the
// stream, survives Expire by starting again from the newest state, and
// sees an Event added afterwards: the path the event watcher takes, with
// nothing of the watcher in between.
func TestReflector_SyncsAndRecoversFromExpiry(t *testing.T) {
	var inFlight, peak atomic.Int64
	s := Start(Options{Seed: 5, Sizes: small, OnBacklog: func(started bool) {
		if !started {
			inFlight.Add(-1)
			return
		}
		if now := inFlight.Add(1); now > peak.Load() {
			peak.Store(now)
		}
	}})
	t.Cleanup(s.Close)
	cfg := s.Config()
	cfg.ContentType = k8sruntime.ContentTypeProtobuf
	cfg.AcceptContentTypes = k8sruntime.ContentTypeProtobuf + "," + k8sruntime.ContentTypeJSON
	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		t.Fatal(err)
	}
	s.ThrottleFirst(1, time.Second)
	lw := cache.ToListWatcherWithWatchListSemantics(eventsListWatch(client), client)
	store := cache.NewStore(cache.MetaNamespaceKeyFunc)
	reflector := cache.NewReflector(lw, &corev1.Event{}, store, 0)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go reflector.RunWithContext(ctx)

	eventually(t, 20*time.Second, func() bool { return len(store.List()) == small.Events }, "the reflector's initial sync")
	if c := s.Counts(); c.Throttled != 1 || c.WatchLists != 1 || c.Lists != 0 {
		t.Fatalf("after the sync: %+v; want one throttle, one watch-list and no list", c)
	}
	s.Expire()
	eventually(t, 20*time.Second, func() bool { return s.Counts().WatchLists == 2 }, "the reflector to stream the state again")
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "after-expiry", Namespace: "team-1"}})
	eventually(t, 20*time.Second, func() bool {
		_, ok, _ := store.GetByKey("team-1/after-expiry")
		return ok
	}, "the post-expiry Event to reach the store")
	// How many stale resumes follow the stream's 410 is client-go's recovery
	// sequence, so only its floor is asserted.
	if c := s.Counts(); c.Expired < 1 || c.Lists != 0 {
		t.Errorf("after the recovery: %+v; want at least the stream's 410 and no list", c)
	}
	if peak.Load() != 1 {
		t.Errorf("peak backlogs in flight = %d, want 1 for one reflector", peak.Load())
	}
}

// A reflector whose client does not do watch-list pages the list through
// the same fake.
func TestReflector_ClassicListPaginates(t *testing.T) {
	s := startSmall(t, 6)
	client := clientFor(t, s, k8sruntime.ContentTypeJSON)
	lw := classicListWatch{eventsListWatch(client)}
	store := cache.NewStore(cache.MetaNamespaceKeyFunc)
	reflector := cache.NewReflector(lw, &corev1.Event{}, store, 0)
	reflector.WatchListPageSize = 25
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go reflector.RunWithContext(ctx)
	eventually(t, 20*time.Second, func() bool { return len(store.List()) == small.Events }, "the classic reflector's sync")
	if c := s.Counts(); c.Lists != 3 || c.WatchLists != 0 {
		t.Errorf("counts = %+v; want three pages and no watch-list", c)
	}
	s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "live", Namespace: "team-2"}})
	eventually(t, 20*time.Second, func() bool {
		_, ok, _ := store.GetByKey("team-2/live")
		return ok
	}, "the live Event to reach the store")
}

// classicListWatch tells the reflector its client has no watch-list, which
// is how a reflector behind an older API server lists: pages, then a watch.
type classicListWatch struct{ *cache.ListWatch }

func (classicListWatch) IsWatchListSemanticsUnSupported() bool { return true }

// eventsListWatch is the reflector's view of a client's Events: the one
// list-and-watch pair every reflector test here builds.
func eventsListWatch(client kubernetes.Interface) *cache.ListWatch {
	return &cache.ListWatch{
		ListWithContextFunc: func(ctx context.Context, opts metav1.ListOptions) (k8sruntime.Object, error) {
			return client.CoreV1().Events(metav1.NamespaceAll).List(ctx, opts)
		},
		WatchFuncWithContext: func(ctx context.Context, opts metav1.ListOptions) (watch.Interface, error) {
			return client.CoreV1().Events(metav1.NamespaceAll).Watch(ctx, opts)
		},
	}
}

// Two servers with one seed hold the same objects, byte for byte on the
// wire; a different seed holds different ones.
func TestGenerator_Deterministic(t *testing.T) {
	a, b, c := startSmall(t, 9), startSmall(t, 9), startSmall(t, 10)
	page := func(s *Server, path string) []byte {
		t.Helper()
		resp, err := http.Get(s.URL + path + "?limit=50")
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		body, _ := io.ReadAll(resp.Body)
		return body
	}
	for _, path := range []string{nodesPath, podsPath, eventsPath} {
		if !bytes.Equal(page(a, path), page(b, path)) {
			t.Errorf("%s differs between two servers seeded alike", path)
		}
		if bytes.Equal(page(a, path), page(c, path)) {
			t.Errorf("%s is the same under two seeds", path)
		}
	}
	gen := NewGenerator(9, small)
	if gen.Event(3).UID == gen.Event(4).UID || gen.Pod(0).UID == gen.Pod(1).UID {
		t.Error("adjacent objects share a UID")
	}
}

// What the fake does not serve is a 404 Status, so a client reads the
// refusal rather than a broken body; a rest.Config from Config reaches it.
func TestUnknownPathIs404(t *testing.T) {
	s := startSmall(t, 1)
	client, err := rest.RESTClientFor(func() *rest.Config {
		cfg := s.Config()
		cfg.APIPath = "/api"
		cfg.GroupVersion = &corev1.SchemeGroupVersion
		cfg.NegotiatedSerializer = k8sruntime.NewSimpleNegotiatedSerializer(k8sruntime.SerializerInfo{MediaType: k8sruntime.ContentTypeJSON, Serializer: jsonFormat.info.Serializer})
		return cfg
	}())
	if err != nil {
		t.Fatal(err)
	}
	err = client.Get().AbsPath("/api/v1/namespaces/team-0/events").Do(context.Background()).Error()
	if !apierrors.IsNotFound(err) {
		t.Fatalf("namespaced path: %v, want 404", err)
	}
}

// The refusals are Status bodies a client decodes, not broken responses: a
// bad limit, continue token or timeout and an unsupported method on a
// resource or on the review route.
func TestRefusalsAreStatuses(t *testing.T) {
	s := startSmall(t, 1)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	ctx := context.Background()
	events := client.CoreV1().Events(metav1.NamespaceAll)
	for name, err := range map[string]error{
		"negative limit": func() error {
			_, err := events.List(ctx, metav1.ListOptions{Limit: -1})
			return err
		}(),
		"undecodable continue": func() error {
			_, err := events.List(ctx, metav1.ListOptions{Continue: "not-a-token"})
			return err
		}(),
		"continue token past the end": func() error {
			token := encodeContinue(continueToken{Version: continueTokenVersion, ResourceVersion: currentVersion(t, s), Start: 1000})
			_, err := events.List(ctx, metav1.ListOptions{Continue: token})
			return err
		}(),
		"bad resourceVersion": func() error {
			_, err := events.List(ctx, metav1.ListOptions{ResourceVersion: "latest"})
			return err
		}(),
		"list resourceVersion above int64": func() error {
			_, err := events.List(ctx, metav1.ListOptions{ResourceVersion: "18446744073709551615"})
			return err
		}(),
		"watch resourceVersion above int64": func() error {
			_, err := events.Watch(ctx, metav1.ListOptions{ResourceVersion: "9223372036854775808"})
			return err
		}(),
		"negative timeout": func() error {
			_, err := events.Watch(ctx, metav1.ListOptions{TimeoutSeconds: ptr.To[int64](-1)})
			return err
		}(),
	} {
		if !apierrors.IsBadRequest(err) {
			t.Errorf("%s: %v, want 400", name, err)
		}
	}
	if s.Counts().Expired != 0 || s.Counts().Lists != 0 {
		t.Errorf("counts after the refusals: %+v, want no 410 and no list served", s.Counts())
	}
	_, err := client.CoreV1().Events("team-0").Create(ctx, &corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: "x"}}, metav1.CreateOptions{})
	if !apierrors.IsNotFound(err) {
		t.Errorf("create on a namespaced path: %v, want 404", err)
	}
	for name, req := range map[string]*http.Request{
		"POST on a resource":          mustRequest(t, http.MethodPost, s.URL+eventsPath, "{}"),
		"GET on the review route":     mustRequest(t, http.MethodGet, s.URL+selfSubjectAccessReviewPath, ""),
		"undecodable review body":     mustRequest(t, http.MethodPost, s.URL+selfSubjectAccessReviewPath, "not a review"),
		"review body of another kind": mustRequest(t, http.MethodPost, s.URL+selfSubjectAccessReviewPath, `{"apiVersion":"v1","kind":"Pod"}`),
	} {
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		var status metav1.Status
		body, _ := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err := json.Unmarshal(body, &status); err != nil || status.Kind != "Status" || int(status.Code) != resp.StatusCode {
			t.Errorf("%s: status %d, body %q", name, resp.StatusCode, body)
			continue
		}
		wantCode := http.StatusMethodNotAllowed
		if name == "undecodable review body" || name == "review body of another kind" {
			wantCode = http.StatusBadRequest
		}
		if resp.StatusCode != wantCode {
			t.Errorf("%s: status %d, want %d", name, resp.StatusCode, wantCode)
		}
	}
}

// currentVersion is the server's resource version as the number a continue
// token carries.
func currentVersion(t *testing.T, s *Server) int64 {
	t.Helper()
	rv, err := strconv.ParseInt(s.ResourceVersion(), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	return rv
}

// A continuation page asked for with the largest limit there is serves the
// rest of the list: the page arithmetic does not wrap.
func TestList_ContinueWithLargestLimit(t *testing.T) {
	s := startSmall(t, 1)
	events := clientFor(t, s, k8sruntime.ContentTypeJSON).CoreV1().Events(metav1.NamespaceAll)
	first, err := events.List(context.Background(), metav1.ListOptions{Limit: 10})
	if err != nil || first.Continue == "" {
		t.Fatalf("first page: %d items, continue %q, %v", len(first.Items), first.Continue, err)
	}
	rest, err := events.List(context.Background(), metav1.ListOptions{Limit: math.MaxInt64, Continue: first.Continue})
	if err != nil {
		t.Fatal(err)
	}
	if len(rest.Items) != small.Events-10 || rest.Continue != "" {
		t.Fatalf("continuation page: %d items, continue %q", len(rest.Items), rest.Continue)
	}
}

// The server's backlog count is down by the time the bookmark ending a
// watch-list's initial events reaches the client, which is when the
// watcher's gate releases the slot: a fleet test reading both never counts a
// slot twice.
func TestWatchList_BacklogReleasedBeforeBookmark(t *testing.T) {
	var inFlight, peak atomic.Int64
	s := Start(Options{Seed: 3, Sizes: small, OnBacklog: func(started bool) {
		if !started {
			inFlight.Add(-1)
			return
		}
		if now := inFlight.Add(1); now > peak.Load() {
			peak.Store(now)
		}
	}})
	t.Cleanup(s.Close)
	w, err := clientFor(t, s, k8sruntime.ContentTypeProtobuf).CoreV1().Events(metav1.NamespaceAll).Watch(context.Background(), metav1.ListOptions{
		SendInitialEvents: ptr.To(true), AllowWatchBookmarks: true, ResourceVersionMatch: metav1.ResourceVersionMatchNotOlderThan,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer w.Stop()
	for i := 0; i < small.Events; i++ {
		receive(t, w, 5*time.Second)
	}
	if ev := receive(t, w, 5*time.Second); ev.Type != watch.Bookmark {
		t.Fatalf("after the initial events: %s", ev.Type)
	}
	if got := inFlight.Load(); got != 0 {
		t.Errorf("%d backlog in flight when the bookmark arrived; want the count released before it", got)
	}
	if peak.Load() != 1 {
		t.Errorf("peak backlogs %d, want the stream counted once", peak.Load())
	}
}

// Every Node and every Pod of a fleet-scale cluster has its own name: the
// per-object suffix is a function of the index, so a consumer keyed by name
// sees as many objects as the sizes say.
func TestGenerator_NamesAreUnique(t *testing.T) {
	fleetScale := Sizes{Nodes: 1_500, Pods: 20_000}
	for seed := int64(0); seed < 3; seed++ {
		gen := NewGenerator(seed, fleetScale)
		nodes := map[string]int{}
		for i := 0; i < fleetScale.Nodes; i++ {
			name := gen.NodeName(i)
			if j, dup := nodes[name]; dup {
				t.Fatalf("seed %d: nodes %d and %d are both %q", seed, j, i, name)
			}
			nodes[name] = i
		}
		pods := map[string]int{}
		for i := 0; i < fleetScale.Pods; i++ {
			name, namespace := gen.PodName(i)
			key := namespace + "/" + name
			if j, dup := pods[key]; dup {
				t.Fatalf("seed %d: pods %d and %d are both %q", seed, j, i, key)
			}
			pods[key] = i
			if suffix := name[len(name)-podSuffixLength-1:]; suffix[0] != '-' || len(suffix) != podSuffixLength+1 {
				t.Fatalf("seed %d: pod %d is %q, not a %d-character suffix", seed, i, name, podSuffixLength)
			}
		}
	}
	a, b := NewGenerator(1, fleetScale), NewGenerator(2, fleetScale)
	if a.NodeName(0) == b.NodeName(0) {
		t.Errorf("node 0 is %q under two seeds", a.NodeName(0))
	}
}

func mustRequest(t *testing.T, method, url, body string) *http.Request {
	t.Helper()
	req, err := http.NewRequest(method, url, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set(contentTypeHeader, k8sruntime.ContentTypeJSON)
	return req
}

// A server without the WatchList gate refuses sendInitialEvents the way the
// API server does, with a 422 Invalid, and a client-go reflector that asked
// for watch-list falls back to a paged list against it.
func TestWatchListUnsupported_RefusedAndTheReflectorFallsBackToPages(t *testing.T) {
	s := Start(Options{Seed: 1, Sizes: small, WatchListUnsupported: true})
	t.Cleanup(s.Close)
	client := clientFor(t, s, k8sruntime.ContentTypeProtobuf)
	ctx := context.Background()
	_, err := client.CoreV1().Events(metav1.NamespaceAll).Watch(ctx, metav1.ListOptions{
		SendInitialEvents:    ptr.To(true),
		AllowWatchBookmarks:  true,
		ResourceVersionMatch: metav1.ResourceVersionMatchNotOlderThan,
	})
	if !apierrors.IsInvalid(err) {
		t.Fatalf("watch with sendInitialEvents: %v, want 422 Invalid", err)
	}
	if c := s.Counts(); c.WatchLists != 0 {
		t.Errorf("counts after the refusal = %+v; want no watch-list stream counted", c)
	}

	lw := eventsListWatch(client)
	store := cache.NewStore(cache.MetaNamespaceKeyFunc)
	reflector := cache.NewReflector(lw, &corev1.Event{}, store, 0)
	reflector.WatchListPageSize = 25
	rctx, cancel := context.WithCancel(ctx)
	defer cancel()
	go reflector.RunWithContext(rctx)
	eventually(t, 20*time.Second, func() bool { return len(store.List()) == small.Events }, "the reflector's sync through the fallback list")
	if c := s.Counts(); c.WatchLists != 0 || c.WatchListsRefused < 2 || c.Lists < 3 {
		t.Errorf("counts = %+v; want no watch-list stream, a refused stream for the direct watch and for the reflector's attempt, and at least three list pages", c)
	}
}

// The one field selector the fake honours, reason=<value> on Events, pages
// the matches at the limit with a continue token and no remainingItemCount,
// and a selector on any other field is refused as the API server refuses an
// unsupported field label.
func TestList_FieldSelectorReasonPagesTheMatches(t *testing.T) {
	s := startSmall(t, 1)
	client := clientFor(t, s, k8sruntime.ContentTypeJSON)
	ctx := context.Background()
	const reason = "TriggeredScaleUp"
	for i := 0; i < 3; i++ {
		s.AddEvent(corev1.Event{ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("mark-%d", i), Namespace: "team-0"}, Reason: reason})
	}
	events := client.CoreV1().Events(metav1.NamespaceAll)
	var got []string
	var pages int
	opts := metav1.ListOptions{FieldSelector: "reason=" + reason, Limit: 2}
	for {
		list, err := events.List(ctx, opts)
		if err != nil {
			t.Fatal(err)
		}
		pages++
		if list.RemainingItemCount != nil {
			t.Errorf("page %d carries remainingItemCount %d; a filtered list omits it", pages, *list.RemainingItemCount)
		}
		for _, ev := range list.Items {
			if ev.Reason != reason {
				t.Errorf("page %d holds %s with reason %q", pages, ev.Name, ev.Reason)
			}
			got = append(got, ev.Name)
		}
		if list.Continue == "" {
			break
		}
		opts.Continue = list.Continue
	}
	if len(got) != 3 || pages < 2 {
		t.Errorf("selected %v over %d pages; want the three marks over at least two pages", got, pages)
	}
	served := s.Counts().Lists
	for _, selector := range []string{"involvedObject.kind=Pod", "reason!=" + reason, "reason=" + reason + ",involvedObject.kind=Pod", "reason"} {
		if _, err := events.List(ctx, metav1.ListOptions{FieldSelector: selector}); !apierrors.IsBadRequest(err) {
			t.Errorf("selector %q: %v, want 400", selector, err)
		}
	}
	if got := s.Counts().Lists; got != served {
		t.Errorf("Counts.Lists moved from %d to %d across four refused selectors; a refusal serves no page", served, got)
	}
	double, err := events.List(ctx, metav1.ListOptions{FieldSelector: "reason==" + reason})
	if err != nil || len(double.Items) != 3 {
		t.Errorf("reason== selector: %d items, %v; want the three marks, as the API server treats == as =", len(double.Items), err)
	}
}
