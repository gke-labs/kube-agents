package hermesbridge

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// freeLoopbackAddr is a loopback address nothing listens on: the kernel picks
// a port and the listener is closed, so a connection to it is refused until a
// test starts a server there.
func freeLoopbackAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	if err := ln.Close(); err != nil {
		t.Fatal(err)
	}
	return addr
}

// startRouteStoreAt serves store's handler at addr after delay, as a
// session-kv server that comes up after the bridge does. Nothing starts once
// the test is over.
func startRouteStoreAt(t *testing.T, store *routeStore, addr string, delay time.Duration) {
	t.Helper()
	done := make(chan struct{})
	started := make(chan *httptest.Server, 1)
	t.Cleanup(func() {
		close(done)
		select {
		case srv := <-started:
			srv.Close()
		default:
		}
	})
	go func() {
		select {
		case <-done:
			return
		case <-time.After(delay):
		}
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			return // the test sees the refusals and fails on its own terms
		}
		srv := &httptest.Server{Listener: ln, Config: &http.Server{Handler: store.handler()}}
		srv.Start()
		started <- srv
	}()
}

// The incident (#7226): a task queued while the pod rolled is taken as soon
// as the new bridge binds, before the session-kv store in the agent container
// is listening. The route write retries the refusal, so the route is recorded
// once the store is up and the answer carries no route-lost note.
func TestAQueuedTaskRecordsItsRouteWithALateSessionStore(t *testing.T) {
	_, url := startServer(t)
	c := gatewayClient(t, url)
	// Queued before the bridge exists, as a message sent mid-roll is.
	submitFrom(t, c, "task-queued", "ctx-queued", "slack:dm/D1", "hello")

	addr := freeLoopbackAddr(t)
	store := &routeStore{status: http.StatusOK}
	// Past routeTimeout, as the incident's six seconds were: a retry cut off
	// at one request's timeout would still lose the route.
	startRouteStoreAt(t, store, addr, routeTimeout+time.Second)
	stub := newAPIStub(t, nil)
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.RouteURL, cfg.RouteKey = "http://"+addr, "kv-key" })

	task := waitTerminal(t, c, "task-queued")
	if task.State != lib.StateCompleted {
		t.Fatalf("task state = %s, want completed", task.State)
	}
	if got := task.Artifact(lib.ArtifactResult).Parts[0].Text; strings.Contains(got, routeLostNote) {
		t.Fatalf("answer = %q carries the route-lost note: the route write gave up on a store still starting", got)
	}
	if puts := store.seen(); len(puts) != 1 || puts[0].body.Conversation != "slack:dm/D1" {
		t.Fatalf("route PUTs = %+v, want the queued task's route", puts)
	}
}

func retryBridge(addr string, retry time.Duration) *Bridge {
	return &Bridge{cfg: Config{RouteURL: "http://" + addr, RouteKey: "kv-key", APIConnectRetry: retry, Logger: discardLogger()},
		routeClient: newAPIClient()}
}

// A store that never comes up is given up on after the retry window, with
// the refusal as the error.
func TestASessionStoreWriteGivesUpAfterTheRetryWindow(t *testing.T) {
	const window = 1500 * time.Millisecond
	b := retryBridge(freeLoopbackAddr(t), window)
	start := time.Now()
	err := b.putRoute(testCtx(t), "a2a-ctx-gone", conversationRoute{Platform: lib.NotifyPlatformSlack, Conversation: "slack:dm/D1"})
	if !errors.Is(err, syscall.ECONNREFUSED) {
		t.Fatalf("putRoute = %v, want the refusal", err)
	}
	if took := time.Since(start); took < window || took > window+2*apiConnectRetryInterval {
		t.Fatalf("gave up after %s, want about the %s window", took, window)
	}
}

// Only a refusal is retried: a store that answers with an error is not
// written to twice.
func TestASessionStoreThatAnswersIsNotRetried(t *testing.T) {
	store := newRouteStore(t, http.StatusInternalServerError)
	b := &Bridge{cfg: Config{RouteURL: store.srv.URL, RouteKey: "kv-key", APIConnectRetry: time.Minute, Logger: discardLogger()},
		routeClient: newAPIClient()}
	if err := b.putRoute(testCtx(t), "a2a-ctx-500", conversationRoute{Platform: lib.NotifyPlatformSlack, Conversation: "slack:dm/D1"}); err == nil {
		t.Fatal("putRoute = nil against a store answering 500")
	}
	if puts := store.seen(); len(puts) != 1 {
		t.Fatalf("route PUTs = %d, want 1", len(puts))
	}
}

// A store that accepts the connection and then hangs costs one attempt's
// timeout, not a retry window: only a refusal is retried. The outer context
// outlives the attempt timeout, so a retry would show as a second request.
func TestAHangingSessionStoreIsNotRetried(t *testing.T) {
	var hits atomic.Int32
	release := make(chan struct{})
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		<-release
	}))
	t.Cleanup(func() { close(release); srv.Close() })
	b := &Bridge{cfg: Config{RouteURL: srv.URL, APIConnectRetry: time.Minute, Logger: discardLogger()}, routeClient: newAPIClient()}
	const attempt = 200 * time.Millisecond
	ctx, cancel := context.WithTimeout(testCtx(t), 10*time.Second)
	defer cancel()
	start := time.Now()
	_, _, err := b.storeDo(ctx, attempt, func(ctx context.Context) (*http.Request, error) {
		return http.NewRequestWithContext(ctx, http.MethodGet, srv.URL+"/hang", nil)
	})
	if err == nil {
		t.Fatal("storeDo = nil against a hanging store")
	}
	if took := time.Since(start); took > attempt+2*apiConnectRetryInterval {
		t.Fatalf("storeDo took %s, want about one %s attempt", took, attempt)
	}
	if n := hits.Load(); n != 1 {
		t.Fatalf("requests = %d, want 1", n)
	}
}

// A store refused for a whole window is marked down: the next request tries
// once instead of waiting out another window, and the first request that
// reaches the store clears the mark.
func TestADeadSessionStoreCostsOneWindowNotOnePerRequest(t *testing.T) {
	const window = 1500 * time.Millisecond
	addr := freeLoopbackAddr(t)
	b := retryBridge(addr, window)
	route := conversationRoute{Platform: lib.NotifyPlatformSlack, Conversation: "slack:dm/D1"}

	start := time.Now()
	if err := b.putRoute(testCtx(t), "a2a-ctx-1", route); err == nil {
		t.Fatal("first putRoute = nil with nothing listening")
	}
	if took := time.Since(start); took < window {
		t.Fatalf("first putRoute gave up after %s, want the %s window", took, window)
	}
	if !b.storeDown.Load() {
		t.Fatal("store not marked down after a whole window of refusals")
	}
	start = time.Now()
	if err := b.putRoute(testCtx(t), "a2a-ctx-2", route); err == nil {
		t.Fatal("second putRoute = nil with nothing listening")
	}
	if took := time.Since(start); took > apiConnectRetryInterval {
		t.Fatalf("second putRoute took %s, want one try while the store is marked down", took)
	}

	store := &routeStore{status: http.StatusOK}
	startRouteStoreAt(t, store, addr, 0)
	waitFor(t, 5*time.Second, "late store listening", func() bool {
		c, err := net.Dial("tcp", addr)
		if err == nil {
			_ = c.Close()
		}
		return err == nil
	})
	if err := b.putRoute(testCtx(t), "a2a-ctx-3", route); err != nil {
		t.Fatalf("putRoute once the store listens = %v", err)
	}
	if b.storeDown.Load() {
		t.Fatal("store still marked down after a request reached it")
	}
}

// The pushed-report lookup retries a refusal the same way.
func TestThePushedReportLookupRetriesALateSessionStore(t *testing.T) {
	addr := freeLoopbackAddr(t)
	var hits atomic.Int32
	go func() {
		time.Sleep(1500 * time.Millisecond)
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			return
		}
		srv := &httptest.Server{Listener: ln, Config: &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			hits.Add(1)
			w.WriteHeader(http.StatusNotFound)
		})}}
		srv.Start()
		t.Cleanup(srv.Close)
	}()
	b := retryBridge(addr, time.Minute)
	_, err := b.getIncident(testCtx(t), incidentAddress{chatID: "slack:dm", threadID: "D1"})
	if !errors.Is(err, errNoIncident) {
		t.Fatalf("getIncident = %v, want errNoIncident from the store once it listens", err)
	}
	if hits.Load() != 1 {
		t.Fatalf("lookups that reached the store = %d, want 1", hits.Load())
	}
}
