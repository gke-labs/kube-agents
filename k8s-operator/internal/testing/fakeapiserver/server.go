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

// Package fakeapiserver is a Kubernetes API server for tests at fleet scale:
// one httptest.Server per fake cluster, serving core Nodes, Pods and Events
// from a seeded generator rather than a stored dataset, over the wire a real
// client-go sees. It paginates with limit and continue, streams watches with
// bookmarks and watch-list initial events, answers in JSON or protobuf as the
// Accept header asks, keeps a compaction floor below which a resource version
// is 410 Gone, and injects the faults a reflector has to live through: 429s
// with Retry-After on the first requests, and the expiry of every open watch.
//
// The event watcher reads Events only; Nodes and Pods are served, at the
// sizes the issue of fleet scale names, for the collectors that page through
// them, which have no Go consumer here yet.
//
// What it does not do: selectors beyond `reason=<value>` on Events (every other request sees every object), namespaced
// paths (the three resources are served at the cluster scope only), writes
// (AddEvent is the one way an object appears after start), and any resource
// beyond the three and the SelfSubjectAccessReview the event watcher's
// preflight asks for, which is always allowed. Unlike the API server, `limit`
// is honoured for every request, resourceVersion="0" included, so a paging
// client pages.
package fakeapiserver

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	authorizationv1 "k8s.io/api/authorization/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/selection"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/kubernetes/scheme"
	"k8s.io/client-go/rest"
)

const (
	nodesPath                   = "/api/v1/nodes"
	podsPath                    = "/api/v1/pods"
	eventsPath                  = "/api/v1/events"
	selfSubjectAccessReviewPath = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"

	paramWatch                = "watch"
	paramLimit                = "limit"
	paramContinue             = "continue"
	paramResourceVersion      = "resourceVersion"
	paramResourceVersionMatch = "resourceVersionMatch"
	paramSendInitialEvents    = "sendInitialEvents"
	paramAllowWatchBookmarks  = "allowWatchBookmarks"
	paramTimeoutSeconds       = "timeoutSeconds"
	paramTrue                 = "true"
	paramFieldSelector        = "fieldSelector"
	// fieldSelectorReason is the one field the fake's Events list selects on,
	// `reason`, with `=` or `==`, which the API server serves for Events and
	// the watcher uses to put the autoscaler marks on record ahead of a paged
	// list. Any other field, operator or compound selector is refused as the
	// API server refuses an unsupported field label.
	fieldSelectorReason  = "reason"
	retryAfterHeader     = "Retry-After"
	resourceVersionBase  = 10
	resourceVersionBits  = 64
	continueTokenVersion = 1

	// initialFlushEvery is how many initial events a watch stream writes
	// between flushes: the frames are small and the response writer buffers
	// them, so a flush per event would cost a syscall per Event.
	initialFlushEvery = 256
	// firstResourceVersion is the floor at start: every generated object is at
	// or above it, so nothing is expired until Expire raises it.
	firstResourceVersion = 1

	// The messages, in the API server's words where it has them.
	expiredMessageFormat         = "too old resource version: %d (%d)"
	continueWithVersionMessage   = "specifying resource version is not allowed when using continue"
	invalidContinueMessage       = "The provided continue parameter is too old to display a consistent list result"
	invalidResourceVersionFormat = "resourceVersion: Invalid value: %q: %v"
	resourceVersionParseFunc     = "ParseUint"
	invalidLimitFormat           = "limit: Invalid value: %q: must be a non-negative integer"
	invalidTimeoutFormat         = "timeoutSeconds: Invalid value: %q: must be a non-negative integer"
	initialEventsBookmarkMessage = "sendInitialEvents requires watch bookmarks"
	// watchListUnsupportedMessage is what a kube-apiserver without the
	// WatchList feature gate (off by default before 1.32 and at 1.33) answers
	// a watch opened with sendInitialEvents; client-go falls back to a list.
	watchListUnsupportedMessage    = "sendInitialEvents is forbidden for watch unless the WatchList feature gate is enabled"
	unsupportedFieldSelectorFormat = "field label not supported: %s"
	throttledMessage               = "the server has received too many requests and has asked the client to try again later"
	notFoundMessage                = "the server could not find the requested resource"
	reviewDecodeMessageFormat      = "the request body is not a SelfSubjectAccessReview: %v"
	initialEventsEndValue          = "true"
)

// listOptionsKind is the kind the API server names when it refuses a list or
// watch option with a 422 Invalid, as it does sendInitialEvents without the
// WatchList feature gate.
var listOptionsKind = schema.GroupKind{Group: metav1.GroupName, Kind: "ListOptions"}

// reasonSelector parses a list's field selector the way the API server does
// and admits the one form the fake serves: a single requirement on the
// Events `reason` field with `=` or `==`. It returns the value and true for
// that form, false for no selector, and an error for anything else.
func reasonSelector(res *kind, selector string) (string, bool, error) {
	if selector == "" {
		return "", false, nil
	}
	parsed, err := fields.ParseSelector(selector)
	if err != nil {
		return "", false, err
	}
	reqs := parsed.Requirements()
	if res.path != eventsPath || len(reqs) != 1 || reqs[0].Field != fieldSelectorReason ||
		(reqs[0].Operator != selection.Equals && reqs[0].Operator != selection.DoubleEquals) {
		return "", false, fmt.Errorf(unsupportedFieldSelectorFormat, selector)
	}
	return reqs[0].Value, true, nil
}

// Options configures a fake cluster.
type Options struct {
	// Seed drives the generator; two servers with one seed hold the same
	// objects. Give each cluster of a fleet its own.
	Seed int64
	// Sizes is how many Nodes, Pods and Events the cluster starts with.
	Sizes Sizes
	// OnBacklog, when set, is called with true as a request starts handing
	// out a backlog (a list page's body, or a watch-list stream's initial
	// events) and with false when that transfer has been written: after a
	// page's body, and before the bookmark that ends a stream's initial
	// events, since a client releases its gate slot on the bookmark. A test
	// of a fleet can so measure how many backlogs the servers had in flight
	// at once. Called from request goroutines; it must not block.
	OnBacklog func(inFlight bool)
	// WatchListUnsupported makes the server behave as a control plane whose
	// WatchList feature gate is off: a watch opened with sendInitialEvents is
	// refused with the API server's 422 Invalid, so a client-go reflector
	// falls back to a paged list, as it does against a 1.31 or 1.33 cluster.
	WatchListUnsupported bool
}

// Counts is a snapshot of what a server has been asked.
type Counts struct {
	// Requests is every request received, the throttled ones included.
	Requests int64
	// Lists is every list request served, a page each.
	Lists int64
	// Watches is every watch request served, watch-lists included.
	Watches int64
	// WatchLists is every watch-list stream that reached the bookmark ending
	// its initial events.
	WatchLists int64
	// WatchListsRefused is every watch opened with sendInitialEvents that
	// Options.WatchListUnsupported refused, so a test can tell a reflector
	// that asked for a stream and fell back from one that never asked.
	WatchListsRefused int64
	// Throttled is every request answered 429.
	Throttled int64
	// Expired is every 410 served: a list below the floor, a watch opened
	// below it, or an open watch ended by Expire.
	Expired int64
	// Connections is every TCP connection the server has accepted. A client
	// built from Config opens one per request, so it tracks Requests; a
	// client that pools connections falls behind it.
	Connections int64
}

// Server is one fake cluster. Its embedded httptest.Server's URL is the API
// server's address; Config returns it as a rest.Config.
type Server struct {
	*httptest.Server
	gen       *Generator
	onBacklog func(bool)
	resources map[string]*kind
	// watchListUnsupported is Options.WatchListUnsupported.
	watchListUnsupported bool

	mu sync.Mutex
	// rv is the newest resource version; floor the oldest still resource.
	// Every generated object has a version in [firstResourceVersion, rv0];
	// AddEvent and Expire move rv on from there.
	rv, floor int64
	// added holds the Events added after start, in version order.
	added []*corev1.Event
	// streams is every open watch.
	streams map[*stream]struct{}
	// throttleLeft is how many more Event requests get a 429, with the
	// Retry-After they carry.
	throttleLeft      int
	retryAfterSeconds int

	requests, lists, watches, watchLists, throttled, expired atomic.Int64
	watchListsRefused, connections                           atomic.Int64
}

// kind is one of the three served resources: its path, the generator for its
// i-th object, the versions it occupies and the typed list and bookmark
// objects client-go decodes it into.
type kind struct {
	path string
	gv   schema.GroupVersion
	// base is the resource version before the kind's first object: the i-th
	// generated object is at base+i+1.
	base  int64
	count int
	// live says whether the kind receives AddEvent objects.
	live     bool
	generate func(i int) runtime.Object
	newList  func(items []runtime.Object, lm metav1.ListMeta) runtime.Object
	newEmpty func() runtime.Object
}

// stream is one open watch: the live objects waiting to be written to it and
// whether Expire has ended it, both guarded by Server.mu; wake is signalled
// after either changes.
type stream struct {
	kind    *kind
	pending []*corev1.Event
	expired bool
	wake    chan struct{}
}

// continueToken is what the fake puts in a list's continue field: the
// snapshot the page was served at and the index the next page starts from.
type continueToken struct {
	Version         int   `json:"v"`
	ResourceVersion int64 `json:"rv"`
	Start           int64 `json:"start"`
}

// Start runs a fake cluster of opts and returns it listening. Close stops it.
func Start(opts Options) *Server {
	gen := NewGenerator(opts.Seed, opts.Sizes)
	s := &Server{
		gen:                  gen,
		onBacklog:            opts.OnBacklog,
		watchListUnsupported: opts.WatchListUnsupported,
		floor:                firstResourceVersion,
		streams:              map[*stream]struct{}{},
		resources:            map[string]*kind{},
	}
	var base int64
	for _, r := range []*kind{
		{
			path: nodesPath, gv: corev1.SchemeGroupVersion, count: opts.Sizes.Nodes,
			generate: func(i int) runtime.Object { return gen.Node(i) },
			newList: func(items []runtime.Object, lm metav1.ListMeta) runtime.Object {
				list := &corev1.NodeList{ListMeta: lm, Items: make([]corev1.Node, len(items))}
				for i, o := range items {
					list.Items[i] = *o.(*corev1.Node)
				}
				return list
			},
			newEmpty: func() runtime.Object { return &corev1.Node{} },
		},
		{
			path: podsPath, gv: corev1.SchemeGroupVersion, count: opts.Sizes.Pods,
			generate: func(i int) runtime.Object { return gen.Pod(i) },
			newList: func(items []runtime.Object, lm metav1.ListMeta) runtime.Object {
				list := &corev1.PodList{ListMeta: lm, Items: make([]corev1.Pod, len(items))}
				for i, o := range items {
					list.Items[i] = *o.(*corev1.Pod)
				}
				return list
			},
			newEmpty: func() runtime.Object { return &corev1.Pod{} },
		},
		{
			path: eventsPath, gv: corev1.SchemeGroupVersion, count: opts.Sizes.Events, live: true,
			generate: func(i int) runtime.Object { return gen.Event(i) },
			newList: func(items []runtime.Object, lm metav1.ListMeta) runtime.Object {
				list := &corev1.EventList{ListMeta: lm, Items: make([]corev1.Event, len(items))}
				for i, o := range items {
					list.Items[i] = *o.(*corev1.Event)
				}
				return list
			},
			newEmpty: func() runtime.Object { return &corev1.Event{} },
		},
	} {
		r.base = base
		base += int64(r.count)
		s.resources[r.path] = r
	}
	s.rv = base
	if s.rv < firstResourceVersion {
		s.rv = firstResourceVersion
	}
	s.Server = httptest.NewUnstartedServer(http.HandlerFunc(s.serveHTTP))
	s.Server.Config.ConnState = func(_ net.Conn, state http.ConnState) {
		if state == http.StateNew {
			s.connections.Add(1)
		}
	}
	s.Server.Start()
	return s
}

// Config is a rest.Config for the fake, as a kubeconfig entry for it would
// be, except that its transport keeps no connection alive between requests.
// Over a pooled HTTP/1.1 connection, net/http's transport can wake the wrong
// reader. When a body is closed while another goroutine's Read of it is
// returning EOF, which is how client-go's StreamWatcher stops a stream the
// API server ended with a 410, the transport drains what is left, puts the
// connection back in the pool, and leaves that Read's end-of-body callback
// parked on the connection's EOF channel (bodyEOFSignal and eofc in
// net/http/transport.go's readLoop: the channel is signalled once per
// response, and the early Close took the signal). The next response on the
// reused connection signals the channel once more, the parked reader takes
// it, and the reader of the new response parks in its place until the
// connection closes, by the idle timeout at the latest; a reflector's
// re-stream after an expiry delivers nothing for that long. The code is
// older than Go 1.27.1, where it was traced; this Transport can go once
// net/http hands the signal to the reader of the response it belongs to.
// A connection per request leaves nothing to reuse, at one dial per request,
// which the paged soaks absorb. It fixes the fake's clients only: the
// watcher's broker route reaches profile clusters over pooled plain HTTP/1.1
// (clusterprofiles.Broker.ClientConfig) and is open to the same stall. A
// test that sets a CA, a client certificate, a certificate callback or
// Insecure on this config fails at client construction, since client-go
// refuses those next to a Transport.
func (s *Server) Config() *rest.Config {
	return &rest.Config{Host: s.URL, Transport: &http.Transport{DisableKeepAlives: true}}
}

// Generator is the generator the server serves from, for a test that wants
// to know what the i-th object is.
func (s *Server) Generator() *Generator { return s.gen }

// Close ends every open watch and stops the server. httptest's Close waits
// for the handlers, and a watch handler returns when its connection does, so
// the connections go first.
func (s *Server) Close() {
	s.CloseClientConnections()
	s.Server.Close()
}

// Counts is what the server has been asked so far.
func (s *Server) Counts() Counts {
	return Counts{
		Requests:   s.requests.Load(),
		Lists:      s.lists.Load(),
		Watches:    s.watches.Load(),
		WatchLists: s.watchLists.Load(),

		WatchListsRefused: s.watchListsRefused.Load(),
		Throttled:         s.throttled.Load(),
		Expired:           s.expired.Load(),
		Connections:       s.connections.Load(),
	}
}

// ResourceVersion is the newest version the server holds, as a list would
// report it.
func (s *Server) ResourceVersion() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strconv.FormatInt(s.rv, resourceVersionBase)
}

// ThrottleFirst answers the next k Event list or watch requests 429 with
// Retry-After of retryAfter (rounded up to whole seconds, the header's unit,
// and at least one), then serves normally. Event routes only: the throttle
// lands on the reflector's requests and not on the preflight's reviews, and
// a client-go request without a body retries a 429 on its own after the
// Retry-After, so a reflector sees the throttle as latency.
func (s *Server) ThrottleFirst(k int, retryAfter time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.throttleLeft = k
	s.retryAfterSeconds = int(math.Ceil(retryAfter.Seconds()))
	if s.retryAfterSeconds < 1 {
		s.retryAfterSeconds = 1
	}
}

// AddEvent appends ev to the cluster at the next resource version and
// delivers it to every open Event watch. It returns the version assigned.
func (s *Server) AddEvent(ev corev1.Event) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rv++
	ev.ResourceVersion = strconv.FormatInt(s.rv, resourceVersionBase)
	stored := &ev
	s.added = append(s.added, stored)
	for st := range s.streams {
		if st.kind.live {
			st.pending = append(st.pending, stored)
			st.signal()
		}
	}
	return ev.ResourceVersion
}

// Expire ends every open watch with a 410 Expired error event and compacts
// the history: the resource version advances by one and the floor rises to
// it, so a list or watch resumed at any earlier version, the one the client
// last saw included, is answered 410 too, and the client has to start again
// from the newest state. That is the sequence a reflector sees when the API
// server's watch cache drops its window.
func (s *Server) Expire() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.rv++
	s.floor = s.rv
	for st := range s.streams {
		st.expired = true
		st.signal()
	}
}

func (st *stream) signal() {
	select {
	case st.wake <- struct{}{}:
	default:
	}
}

func (s *Server) serveHTTP(w http.ResponseWriter, r *http.Request) {
	s.requests.Add(1)
	format := negotiate(r.Header.Get(acceptHeader))
	if r.URL.Path == selfSubjectAccessReviewPath {
		s.serveAccessReview(format, w, r)
		return
	}
	res, ok := s.resources[r.URL.Path]
	if !ok {
		format.writeStatus(w, &metav1.Status{Status: metav1.StatusFailure, Code: http.StatusNotFound, Reason: metav1.StatusReasonNotFound, Message: notFoundMessage})
		return
	}
	if r.Method != http.MethodGet {
		format.writeStatus(w, &apierrors.NewMethodNotSupported(schema.GroupResource{Resource: r.URL.Path}, r.Method).ErrStatus)
		return
	}
	if res.live && s.takeThrottle(w, format) {
		return
	}
	if r.URL.Query().Get(paramWatch) == paramTrue {
		s.serveWatch(res, format, w, r)
		return
	}
	s.serveList(res, format, w, r)
}

// takeThrottle answers the request 429 and reports true while a throttle is
// armed.
func (s *Server) takeThrottle(w http.ResponseWriter, format wireFormat) bool {
	s.mu.Lock()
	if s.throttleLeft <= 0 {
		s.mu.Unlock()
		return false
	}
	s.throttleLeft--
	seconds := s.retryAfterSeconds
	s.mu.Unlock()
	s.throttled.Add(1)
	w.Header().Set(retryAfterHeader, strconv.Itoa(seconds))
	format.writeStatus(w, &apierrors.NewTooManyRequests(throttledMessage, seconds).ErrStatus)
	return true
}

// serveAccessReview allows every SelfSubjectAccessReview, echoing the review
// it was sent with status.allowed set, as a cluster whose identity holds the
// permission does.
func (s *Server) serveAccessReview(format wireFormat, w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		format.writeStatus(w, &apierrors.NewMethodNotSupported(schema.GroupResource{Group: authorizationv1.GroupName, Resource: r.URL.Path}, r.Method).ErrStatus)
		return
	}
	body, err := io.ReadAll(r.Body)
	if err != nil {
		format.writeStatus(w, &apierrors.NewBadRequest(fmt.Sprintf(reviewDecodeMessageFormat, err)).ErrStatus)
		return
	}
	obj, _, err := scheme.Codecs.UniversalDeserializer().Decode(body, nil, &authorizationv1.SelfSubjectAccessReview{})
	review, ok := obj.(*authorizationv1.SelfSubjectAccessReview)
	if err != nil || !ok {
		format.writeStatus(w, &apierrors.NewBadRequest(fmt.Sprintf(reviewDecodeMessageFormat, err)).ErrStatus)
		return
	}
	review.Status = authorizationv1.SubjectAccessReviewStatus{Allowed: true}
	_ = format.writeObject(w, http.StatusCreated, authorizationv1.SchemeGroupVersion, review)
}

// parseResourceVersion reads a resourceVersion parameter: 0 for "" and "0",
// the number otherwise. A value above the signed 64-bit range the versions
// are compared in is refused as out of range rather than cast negative,
// where it would read as below the floor.
func parseResourceVersion(raw string) (int64, error) {
	if raw == "" {
		return 0, nil
	}
	rv, err := strconv.ParseUint(raw, resourceVersionBase, resourceVersionBits)
	if err != nil {
		return 0, apierrors.NewBadRequest(fmt.Sprintf(invalidResourceVersionFormat, raw, err))
	}
	if rv > math.MaxInt64 {
		err := &strconv.NumError{Func: resourceVersionParseFunc, Num: raw, Err: strconv.ErrRange}
		return 0, apierrors.NewBadRequest(fmt.Sprintf(invalidResourceVersionFormat, raw, err))
	}
	return int64(rv), nil
}

// expiredStatus is the 410 the API server returns for a version below its
// compaction floor, counted.
func (s *Server) expiredStatus(requested, floor int64) *metav1.Status {
	s.expired.Add(1)
	return &apierrors.NewResourceExpired(fmt.Sprintf(expiredMessageFormat, requested, floor)).ErrStatus
}

// snapshot is the server's state as of one resource version: how many
// generated objects of the resource it holds and which added Events, so a
// page or a stream served from it is consistent whatever is added meanwhile.
type snapshot struct {
	version   int64
	generated int64
	added     []*corev1.Event
}

// snapshotLocked takes the state at version for res. Caller holds s.mu.
func (s *Server) snapshotLocked(res *kind, version int64) snapshot {
	snap := snapshot{version: version}
	snap.generated = version - res.base
	if snap.generated < 0 {
		snap.generated = 0
	}
	if snap.generated > int64(res.count) {
		snap.generated = int64(res.count)
	}
	if res.live {
		for _, ev := range s.added {
			if rv, _ := strconv.ParseInt(ev.ResourceVersion, resourceVersionBase, resourceVersionBits); rv > version {
				break
			}
			snap.added = append(snap.added, ev)
		}
	}
	return snap
}

func (snap snapshot) total() int64 { return snap.generated + int64(len(snap.added)) }

// object is the j-th object of the snapshot, generated with its version set,
// or a copy of an added Event; a copy, because the encoder sets the kind on
// the object it is given and the added Events are shared between streams.
func (snap snapshot) object(res *kind, j int64) runtime.Object {
	if j < snap.generated {
		obj := res.generate(int(j))
		accessor, err := meta.Accessor(obj)
		if err != nil {
			panic(err)
		}
		accessor.SetResourceVersion(strconv.FormatInt(res.base+j+1, resourceVersionBase))
		return obj
	}
	return snap.added[j-snap.generated].DeepCopy()
}

func (s *Server) serveList(res *kind, format wireFormat, w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var limit int64
	if raw := q.Get(paramLimit); raw != "" {
		n, err := strconv.ParseInt(raw, resourceVersionBase, resourceVersionBits)
		if err != nil || n < 0 {
			format.writeStatus(w, &apierrors.NewBadRequest(fmt.Sprintf(invalidLimitFormat, raw)).ErrStatus)
			return
		}
		limit = n
	}
	var version, start int64
	if token := q.Get(paramContinue); token != "" {
		if q.Get(paramResourceVersion) != "" {
			format.writeStatus(w, &apierrors.NewBadRequest(continueWithVersionMessage).ErrStatus)
			return
		}
		c, err := decodeContinue(token)
		if err != nil {
			format.writeStatus(w, &apierrors.NewBadRequest(invalidContinueMessage).ErrStatus)
			return
		}
		version, start = c.ResourceVersion, c.Start
	} else {
		requested, err := parseResourceVersion(q.Get(paramResourceVersion))
		if err != nil {
			format.writeStatus(w, &err.(*apierrors.StatusError).ErrStatus)
			return
		}
		version = requested
	}
	s.mu.Lock()
	current, floor := s.rv, s.floor
	if version == 0 {
		version = current
	} else if version < floor {
		s.mu.Unlock()
		format.writeStatus(w, s.expiredStatus(version, floor))
		return
	} else if q.Get(paramResourceVersionMatch) != string(metav1.ResourceVersionMatchExact) && q.Get(paramContinue) == "" {
		// NotOlderThan, the default: the newest state satisfies it.
		version = current
	} else if version > current {
		// Exact at a version the server has not reached is served as the
		// newest; the API server would answer 504 and have the client wait.
		version = current
	}
	snap := s.snapshotLocked(res, version)
	s.mu.Unlock()
	if start > snap.total() {
		// A token past the snapshot it names, which the fake never minted:
		// the API server refuses one it cannot serve the same way.
		format.writeStatus(w, &apierrors.NewBadRequest(invalidContinueMessage).ErrStatus)
		return
	}

	selector := q.Get(paramFieldSelector)
	want, selected, err := reasonSelector(res, selector)
	if err != nil {
		// Refused before the count, as every other refusal is: Counts.Lists
		// is the pages served.
		format.writeStatus(w, &apierrors.NewBadRequest(fmt.Sprintf(unsupportedFieldSelectorFormat, selector)).ErrStatus)
		return
	}

	s.lists.Add(1)
	lm := metav1.ListMeta{ResourceVersion: strconv.FormatInt(snap.version, resourceVersionBase)}
	var items []runtime.Object
	if selected {
		// The page walks the snapshot from the token's start and keeps the
		// matches, up to the limit; the next token starts after the last
		// object examined, and a walk that reaches the end without filling
		// the page is the last page. No remainingItemCount: the API server
		// omits it on a filtered list too.
		next := snap.total()
		for j := start; j < snap.total(); j++ {
			obj := snap.object(res, j)
			if ev, ok := obj.(*corev1.Event); ok && ev.Reason == want {
				items = append(items, obj)
			}
			if limit > 0 && int64(len(items)) == limit {
				next = j + 1
				break
			}
		}
		if next < snap.total() {
			lm.Continue = encodeContinue(continueToken{Version: continueTokenVersion, ResourceVersion: snap.version, Start: next})
		}
	} else {
		end := snap.total()
		if limit > 0 && limit < end-start {
			end = start + limit
		}
		items = make([]runtime.Object, 0, end-start)
		for j := start; j < end; j++ {
			items = append(items, snap.object(res, j))
		}
		if end < snap.total() {
			lm.Continue = encodeContinue(continueToken{Version: continueTokenVersion, ResourceVersion: snap.version, Start: end})
			remaining := snap.total() - end
			lm.RemainingItemCount = &remaining
		}
	}
	s.backlog(true)
	defer s.backlog(false)
	_ = format.writeObject(w, http.StatusOK, res.gv, res.newList(items, lm))
}

func (s *Server) backlog(inFlight bool) {
	if s.onBacklog != nil {
		s.onBacklog(inFlight)
	}
}

func encodeContinue(c continueToken) string {
	raw, err := json.Marshal(c)
	if err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(raw)
}

func decodeContinue(token string) (continueToken, error) {
	var c continueToken
	raw, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(raw, &c); err != nil {
		return c, err
	}
	if c.Version != continueTokenVersion || c.Start < 0 {
		return c, fmt.Errorf("continue token version %d, start %d", c.Version, c.Start)
	}
	return c, nil
}

// serveWatch streams a watch. With sendInitialEvents, or from no resource
// version, it begins with the state as synthetic Added events, ends them
// with the bookmark the watch-list protocol defines when asked for one, and
// goes on with whatever is added, until the client leaves, the timeout
// passes or Expire ends it. From a resource version, it begins with the
// Events added since that version.
func (s *Server) serveWatch(res *kind, format wireFormat, w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	requested, err := parseResourceVersion(q.Get(paramResourceVersion))
	if err != nil {
		format.writeStatus(w, &err.(*apierrors.StatusError).ErrStatus)
		return
	}
	sendInitial := q.Get(paramSendInitialEvents) == paramTrue
	if sendInitial && s.watchListUnsupported {
		s.watchListsRefused.Add(1)
		format.writeStatus(w, &apierrors.NewInvalid(listOptionsKind, "", field.ErrorList{field.Forbidden(field.NewPath(paramSendInitialEvents), watchListUnsupportedMessage)}).ErrStatus)
		return
	}
	if sendInitial && q.Get(paramAllowWatchBookmarks) != paramTrue {
		format.writeStatus(w, &apierrors.NewBadRequest(initialEventsBookmarkMessage).ErrStatus)
		return
	}
	var timeout <-chan time.Time
	if raw := q.Get(paramTimeoutSeconds); raw != "" {
		seconds, err := strconv.ParseInt(raw, resourceVersionBase, resourceVersionBits)
		if err != nil || seconds < 0 {
			format.writeStatus(w, &apierrors.NewBadRequest(fmt.Sprintf(invalidTimeoutFormat, raw)).ErrStatus)
			return
		}
		if seconds > 0 {
			timer := time.NewTimer(time.Duration(seconds) * time.Second)
			defer timer.Stop()
			timeout = timer.C
		}
	}
	s.watches.Add(1)
	ww := format.startWatch(w, res.gv)

	s.mu.Lock()
	if requested != 0 && requested < s.floor {
		status := s.expiredStatus(requested, s.floor)
		s.mu.Unlock()
		_ = ww.send(watch.Error, status)
		ww.flush()
		return
	}
	initial := sendInitial || requested == 0
	snap := s.snapshotLocked(res, s.rv)
	st := &stream{kind: res, wake: make(chan struct{}, 1)}
	if !initial && res.live {
		// Catch up: everything added after the requested version is the
		// stream's first content, then what arrives from here on. Added
		// objects are Events, so a Node or Pod watch resumed from a version
		// has nothing to catch up on and begins with what arrives, as the
		// snapshot and AddEvent keep Events off those streams too.
		for _, ev := range s.added {
			if rv, _ := strconv.ParseInt(ev.ResourceVersion, resourceVersionBase, resourceVersionBits); rv > requested {
				st.pending = append(st.pending, ev)
			}
		}
		if len(st.pending) > 0 {
			st.signal()
		}
	}
	s.streams[st] = struct{}{}
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		delete(s.streams, st)
		s.mu.Unlock()
	}()

	if initial {
		s.backlog(true)
		for j := int64(0); j < snap.total(); j++ {
			if err := ww.send(watch.Added, snap.object(res, j)); err != nil {
				s.backlog(false)
				return
			}
			if j%initialFlushEvery == initialFlushEvery-1 {
				ww.flush()
			}
		}
		// Released before the bookmark goes out: the client's gate frees its
		// slot on the bookmark and the next watcher's request can reach a
		// server before this goroutine runs another statement, so a count
		// still up here would read one backlog more than the gate allows.
		s.backlog(false)
		if sendInitial {
			bookmark := res.newEmpty()
			accessor, err := meta.Accessor(bookmark)
			if err != nil {
				panic(err)
			}
			accessor.SetResourceVersion(strconv.FormatInt(snap.version, resourceVersionBase))
			accessor.SetAnnotations(map[string]string{metav1.InitialEventsAnnotationKey: initialEventsEndValue})
			if err := ww.send(watch.Bookmark, bookmark); err != nil {
				return
			}
			s.watchLists.Add(1)
		}
		ww.flush()
	}

	for {
		select {
		case <-r.Context().Done():
			return
		case <-timeout:
			return
		case <-st.wake:
		}
		s.mu.Lock()
		pending, expired := st.pending, st.expired
		st.pending = nil
		s.mu.Unlock()
		for _, ev := range pending {
			if err := ww.send(watch.Added, ev.DeepCopy()); err != nil {
				return
			}
		}
		if expired {
			s.mu.Lock()
			status := s.expiredStatus(snap.version, s.floor)
			s.mu.Unlock()
			_ = ww.send(watch.Error, status)
			ww.flush()
			return
		}
		ww.flush()
	}
}
