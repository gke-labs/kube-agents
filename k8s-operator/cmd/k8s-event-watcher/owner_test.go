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
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"k8s.io/client-go/kubernetes/fake"
	"k8s.io/client-go/rest"
)

// podMetaServer answers a named Pod GET with the metadata set for that path,
// and records each request's path and Accept header.
type podMetaServer struct {
	mu       sync.Mutex
	bodies   map[string]string
	status   int
	requests []string
	accepts  []string
}

func (s *podMetaServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.requests = append(s.requests, r.URL.Path)
	s.accepts = append(s.accepts, r.Header.Get("Accept"))
	status, body := s.status, s.bodies[r.URL.Path]
	s.mu.Unlock()
	if status != 0 {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure"}`))
		return
	}
	if body == "" {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"kind":"Status","apiVersion":"v1","status":"Failure","reason":"NotFound"}`))
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write([]byte(body))
}

func (s *podMetaServer) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.requests)
}

func newPodMetaResolver(t *testing.T, bodies map[string]string) (*podMetaServer, *ownerResolver) {
	t.Helper()
	s := &podMetaServer{bodies: bodies}
	srv := httptest.NewServer(s)
	t.Cleanup(srv.Close)
	client, err := newClusterClient(&rest.Config{Host: srv.URL})
	if err != nil {
		t.Fatal(err)
	}
	return s, newOwnerResolver(client)
}

func podMetaBody(labels, owners string) string {
	return `{"kind":"PartialObjectMetadata","apiVersion":"meta.k8s.io/v1","metadata":{"name":"p","namespace":"ns",` +
		`"labels":{` + labels + `},"ownerReferences":[` + owners + `]}}`
}

func TestOwnerResolver_NamesEachControllerKind(t *testing.T) {
	cases := []struct {
		name   string
		labels string
		owners string
		want   string
	}{
		{"statefulset", ``, `{"kind":"StatefulSet","name":"web","controller":true}`, "StatefulSet/web"},
		{"daemonset", ``, `{"kind":"DaemonSet","name":"node-exporter","controller":true}`, "DaemonSet/node-exporter"},
		{"job", ``, `{"kind":"Job","name":"db-migrate-29812345","controller":true}`, "Job/db-migrate-29812345"},
		// A ReplicaSet owner names its Deployment by the pod-template-hash suffix.
		{"deployment", `"pod-template-hash":"6cfdb6b98b"`, `{"kind":"ReplicaSet","name":"checkout-6cfdb6b98b","controller":true}`, "Deployment/checkout"},
		// A ReplicaSet whose name does not end in the Pod's hash, or with no
		// hash label (an Argo Rollout's labels rollouts-pod-template-hash), is
		// not a Deployment's: it reads as none, and the Pod folds by name.
		{"bare replicaset", `"pod-template-hash":"aaaa"`, `{"kind":"ReplicaSet","name":"standalone","controller":true}`, ""},
		{"replicaset without the hash label", `"rollouts-pod-template-hash":"5d8f7c"`, `{"kind":"ReplicaSet","name":"canary-5d8f7c","controller":true}`, ""},
		// Only the controller reference counts; a plain owner does not.
		{"owner that is not the controller", ``, `{"kind":"ConfigMap","name":"cm"},{"kind":"StatefulSet","name":"db","controller":true}`, "StatefulSet/db"},
		{"no controller", ``, `{"kind":"ConfigMap","name":"cm","controller":false}`, ""},
		// A static Pod's controller is its Node; folding on it would make every
		// static Pod on the node one incident, so it reads as none.
		{"static pod", ``, `{"kind":"Node","name":"node-a","controller":true}`, ""},
		{"other controller kind", ``, `{"kind":"Rollout","name":"r","controller":true}`, ""},
		{"bare pod", ``, ``, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s, o := newPodMetaResolver(t, map[string]string{"/api/v1/namespaces/ns/pods/p": podMetaBody(tc.labels, tc.owners)})
			if got := o.ControllerRef(context.Background(), "ns", "p", "uid-"+tc.name); got != tc.want {
				t.Fatalf("ControllerRef = %q; want %q", got, tc.want)
			}
			if s.accepts[0] != podMetadataAccept {
				t.Fatalf("Accept = %q; want the metadata-only %q, the one shape the broker's watcher route admits", s.accepts[0], podMetadataAccept)
			}
		})
	}
}

// An answer is remembered per Pod (namespace, name and UID), "no controller"
// included, so the Pod's later events cost no read.
func TestOwnerResolver_RemembersAnAnswerPerPod(t *testing.T) {
	s, o := newPodMetaResolver(t, map[string]string{
		"/api/v1/namespaces/ns/pods/p": podMetaBody(``, `{"kind":"StatefulSet","name":"web","controller":true}`),
		"/api/v1/namespaces/ns/pods/b": podMetaBody(``, ``),
	})
	for range 3 {
		if got := o.ControllerRef(context.Background(), "ns", "p", "uid-p"); got != "StatefulSet/web" {
			t.Fatalf("ControllerRef = %q", got)
		}
		if got := o.ControllerRef(context.Background(), "ns", "b", "uid-b"); got != "" {
			t.Fatalf("bare pod ControllerRef = %q", got)
		}
	}
	if n := s.count(); n != 2 {
		t.Fatalf("requests = %d; want 2, one per Pod", n)
	}
}

// A refused or failed lookup sends the event without controller_ref, and the
// cluster's lookups pause for ownerFailureBackoff, so a cluster whose reads
// fail does not hold every admitted Pod event for the timeout in turn. The
// failure is not remembered per Pod: after the backoff the Pod is asked again.
func TestOwnerResolver_AFailedLookupBacksOffTheCluster(t *testing.T) {
	for _, status := range []int{http.StatusForbidden, http.StatusInternalServerError} {
		s, o := newPodMetaResolver(t, nil)
		s.status = status
		now := time.Now()
		o.now = func() time.Time { return now }
		for _, pod := range []string{"p", "q", "p"} {
			if got := o.ControllerRef(context.Background(), "ns", pod, "uid-"+pod); got != "" {
				t.Fatalf("status %d: ControllerRef = %q; want empty", status, got)
			}
		}
		if n := s.count(); n != 1 {
			t.Fatalf("status %d: requests = %d; want 1, the rest skipped by the backoff", status, n)
		}
		now = now.Add(ownerFailureBackoff + time.Second)
		o.ControllerRef(context.Background(), "ns", "p", "uid-p")
		if n := s.count(); n != 2 {
			t.Fatalf("status %d: requests = %d after the backoff; want 2, the Pod is asked again", status, n)
		}
	}
}

// A 404 is a Pod deleted before its event was dispatched: that Pod reads as
// none, and the cluster's other Pods are still looked up.
func TestOwnerResolver_ADeletedPodDoesNotBackOffTheCluster(t *testing.T) {
	s, o := newPodMetaResolver(t, map[string]string{
		"/api/v1/namespaces/ns/pods/q": podMetaBody(``, `{"kind":"StatefulSet","name":"web","controller":true}`),
	})
	if got := o.ControllerRef(context.Background(), "ns", "gone", "uid-gone"); got != "" {
		t.Fatalf("deleted pod ControllerRef = %q; want empty", got)
	}
	if got := o.ControllerRef(context.Background(), "ns", "q", "uid-q"); got != "StatefulSet/web" {
		t.Fatalf("ControllerRef after a 404 = %q; want StatefulSet/web, no backoff", got)
	}
	if n := s.count(); n != 2 {
		t.Fatalf("requests = %d; want 2", n)
	}
}

// The memo is keyed on the Pod read, not on the event's UID alone: a UID an
// event in one namespace names does not set the answer for a Pod in another.
func TestOwnerResolver_TheMemoIsPerPodNotPerUID(t *testing.T) {
	s, o := newPodMetaResolver(t, map[string]string{
		"/api/v1/namespaces/a/pods/x": podMetaBody(``, `{"kind":"StatefulSet","name":"db","controller":true}`),
		"/api/v1/namespaces/b/pods/y": podMetaBody(``, `{"kind":"Job","name":"j","controller":true}`),
	})
	if got := o.ControllerRef(context.Background(), "a", "x", "uid-shared"); got != "StatefulSet/db" {
		t.Fatalf("a/x ControllerRef = %q", got)
	}
	if got := o.ControllerRef(context.Background(), "b", "y", "uid-shared"); got != "Job/j" {
		t.Fatalf("b/y ControllerRef = %q; want Job/j, read on its own", got)
	}
	if n := s.count(); n != 2 {
		t.Fatalf("requests = %d; want 2", n)
	}
}

// The namespace and name come from the event's author. One a Pod cannot have
// is not read at all, so it cannot steer the GET to another object or draw a
// refusal from the broker, and it starts no backoff.
func TestOwnerResolver_ANameAPodCannotHaveIsNotRead(t *testing.T) {
	s, o := newPodMetaResolver(t, map[string]string{
		"/api/v1/namespaces/b/pods/y":   podMetaBody(``, `{"kind":"StatefulSet","name":"other","controller":true}`),
		"/api/v1/namespaces/ns/pods/ok": podMetaBody(``, `{"kind":"Job","name":"j","controller":true}`),
	})
	for _, tc := range []struct{ namespace, name string }{
		{"ns", "x/../../../b/pods/y"},
		{"ns", "x/../../configmaps/y"},
		{"ns", ".."},
		{"ns", "."},
		{"ns", "a%2Fb"},
		{"..", "y"},
		{"ns/../b", "y"},
		{"ns", "web 0"},
		{"ns", "web#0"},
		{"ns", "Web-0"},
		{"ns", strings.Repeat("a", 254)},
		{strings.Repeat("n", 64), "y"},
		{"", "y"},
		{"ns", ""},
	} {
		if got := o.ControllerRef(context.Background(), tc.namespace, tc.name, "uid"); got != "" {
			t.Errorf("ControllerRef(%q, %q) = %q; want empty", tc.namespace, tc.name, got)
		}
	}
	if n := s.count(); n != 0 {
		t.Fatalf("requests = %d; want 0, no read for a name a Pod cannot have", n)
	}
	if got := o.ControllerRef(context.Background(), "ns", "ok", "uid-ok"); got != "Job/j" {
		t.Fatalf("ControllerRef after the refusals = %q; want Job/j, no backoff", got)
	}
}

func TestOwnerResolver_ASlowAPIServerIsEmptyAfterTheTimeout(t *testing.T) {
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		<-release
	}))
	t.Cleanup(func() { close(release); srv.Close() })
	client, err := newClusterClient(&rest.Config{Host: srv.URL})
	if err != nil {
		t.Fatal(err)
	}
	o := newOwnerResolver(client)
	o.timeout = 50 * time.Millisecond
	start := time.Now()
	if got := o.ControllerRef(context.Background(), "ns", "p", "uid"); got != "" {
		t.Fatalf("ControllerRef = %q; want empty", got)
	}
	if took := time.Since(start); took > 2*time.Second {
		t.Fatalf("lookup took %s; want it bounded by the timeout", took)
	}
}

func TestOwnerResolver_NilOrIncompleteIsEmpty(t *testing.T) {
	var nilResolver *ownerResolver
	if got := nilResolver.ControllerRef(context.Background(), "ns", "p", "uid"); got != "" {
		t.Fatalf("nil resolver = %q", got)
	}
	s, o := newPodMetaResolver(t, nil)
	if got := o.ControllerRef(context.Background(), "", "p", "uid"); got != "" {
		t.Fatalf("no namespace = %q", got)
	}
	if got := o.ControllerRef(context.Background(), "ns", "", "uid"); got != "" {
		t.Fatalf("no name = %q", got)
	}
	if n := s.count(); n != 0 {
		t.Fatalf("requests = %d; want none for an incomplete event", n)
	}
}

// The dispatcher fills controller_ref for an admitted Pod event and reads
// nothing for a duplicate or a non-Pod event.
func TestDispatcher_FillsControllerRefForAnAdmittedPodEvent(t *testing.T) {
	var payloads []InjectPayload
	var mu sync.Mutex
	daemon := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/sessions" {
			w.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(w).Encode(createSessionResponse{SessionID: "sid-" + strconv.Itoa(len(payloads))})
			return
		}
		var req injectMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Errorf("decode inject: %v", err)
		}
		var p InjectPayload
		if err := json.Unmarshal([]byte(req.Message), &p); err != nil {
			t.Errorf("decode payload: %v", err)
		}
		mu.Lock()
		payloads = append(payloads, p)
		mu.Unlock()
		w.WriteHeader(http.StatusOK)
	}))
	defer daemon.Close()
	inj, err := newInjector(injectorConfig{daemonURL: daemon.URL, bearerToken: "t", httpClient: daemon.Client()})
	if err != nil {
		t.Fatal(err)
	}
	dedup, err := newDedupCache(5*time.Minute, "")
	if err != nil {
		t.Fatal(err)
	}
	pods, owners := newPodMetaResolver(t, map[string]string{
		"/api/v1/namespaces/default/pods/web-0": podMetaBody(``, `{"kind":"StatefulSet","name":"web","controller":true}`),
	})
	disp := &dispatcher{
		filter:   newFilter(newFilterConfig(nil, nil, nil, filterThresholds{})),
		dedup:    dedup,
		injector: inj,
		metrics:  newMetrics(),
		mode:     "per-incident",
		owners:   owners,
	}
	pod := TriageEvent{
		Key:          EventKey{UID: "uid-web-0", Reason: "CrashLoopBackOff"},
		Namespace:    "default",
		KindOfObject: kindPod,
		Name:         "web-0",
		LastSeen:     time.Now(),
		Message:      "back-off restarting failed container",
		Count:        3,
	}
	disp.Dispatch(context.Background(), pod)
	disp.Dispatch(context.Background(), pod) // a duplicate: dedup stops it before any read
	node := TriageEvent{
		Key:          EventKey{UID: "uid-node", Reason: "NodeNotReady"},
		KindOfObject: "Node",
		Name:         "node-1",
		LastSeen:     time.Now(),
		Message:      "node is not ready",
		Count:        3,
	}
	disp.Dispatch(context.Background(), node)

	mu.Lock()
	defer mu.Unlock()
	if len(payloads) == 0 || payloads[0].Context.ControllerRef != "StatefulSet/web" {
		t.Fatalf("first payload controller_ref = %+v; want StatefulSet/web", payloads)
	}
	for _, p := range payloads[1:] {
		if p.Context.ControllerRef != "" {
			t.Errorf("payload for %s/%s carries controller_ref %q; want empty for a non-Pod event", p.KindOfObject, p.Name, p.Context.ControllerRef)
		}
	}
	if n := pods.count(); n != 1 {
		t.Fatalf("pod reads = %d; want 1, for the one admitted Pod event", n)
	}
}

// client-go's fake clientset has no REST transport: the lookup answers empty
// rather than panicking, so a test or a client that cannot read stays safe.
func TestOwnerResolver_AClientWithoutARESTTransportIsEmpty(t *testing.T) {
	o := newOwnerResolver(fake.NewSimpleClientset())
	if got := o.ControllerRef(context.Background(), "ns", "p", "uid"); got != "" {
		t.Fatalf("ControllerRef = %q; want empty", got)
	}
}

// Through the broker the answer is projected to the controller entry and,
// for a ReplicaSet, the hash label (watcher_policy.project_pod_metadata): no
// kind, apiVersion, name, namespace or uid. The lookup reads that shape.
func TestOwnerResolver_ReadsTheBrokersProjectedShape(t *testing.T) {
	_, o := newPodMetaResolver(t, map[string]string{
		"/api/v1/namespaces/ns/pods/d": `{"metadata":{"ownerReferences":[{"kind":"ReplicaSet","name":"checkout-6cfdb6b98b","controller":true}],"labels":{"pod-template-hash":"6cfdb6b98b"}}}`,
		"/api/v1/namespaces/ns/pods/s": `{"metadata":{"ownerReferences":[{"kind":"StatefulSet","name":"web","controller":true}]}}`,
		"/api/v1/namespaces/ns/pods/b": `{"metadata":{}}`,
	})
	for pod, want := range map[string]string{"d": "Deployment/checkout", "s": "StatefulSet/web", "b": ""} {
		if got := o.ControllerRef(context.Background(), "ns", pod, "uid-"+pod); got != want {
			t.Errorf("pod %s: ControllerRef = %q; want %q", pod, got, want)
		}
	}
}
