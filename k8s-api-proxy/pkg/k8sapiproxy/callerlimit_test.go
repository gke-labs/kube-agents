package k8sapiproxy

import (
	"bufio"
	"bytes"
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	// testCallerHeader names the caller of a test request, and testHoldHeader marks one the
	// upstream keeps open. Neither is a header the handler strips.
	testCallerHeader = "X-Test-Caller"
	testHoldHeader   = "X-Test-Hold"

	limitedCaller   = "system:serviceaccount:agents:worker"
	unlimitedCaller = "system:serviceaccount:agents:shell"

	limitTestCluster = "/clusters/in-cluster"
	limitPodsPath    = "/api/v1/pods"
	limitNSPath      = "/api/v1/namespaces"
	limitWatchPath   = "/api/v1/namespaces?watch=true"
	limitLogPath     = "/api/v1/namespaces/default/pods/web/log?follow=true"
	limitExecPath    = "/api/v1/namespaces/default/pods/web/exec?command=sh"
	limitWritePath   = "/api/v1/namespaces/default/configmaps"

	// testPodExtra is the CallerIdentity.Extra key a test's limits func reads a pod from.
	testPodExtra = "test/pod-name"

	limitTestBody = `{"kind":"PodList","apiVersion":"v1","items":[]}`
)

// headerAuthenticator admits every request as the caller its testCallerHeader names.
type headerAuthenticator struct{}

func (headerAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	return &CallerIdentity{Username: r.Header.Get(testCallerHeader)}, nil
}

// limitsFor limits limitedCaller and nobody else.
func limitsFor(limits CallerLimits) func(*CallerIdentity) CallerLimits {
	return func(caller *CallerIdentity) CallerLimits {
		if caller.Username == limitedCaller {
			return limits
		}
		return CallerLimits{}
	}
}

// holdingUpstream answers at once, except a request marked with testHoldHeader: its
// response body stays open until release is called or the request's context ends, as a
// watch's does, and entered gets one value when the request has reached the upstream.
type holdingUpstream struct {
	entered chan struct{}
	gate    chan struct{}
	release func()
}

func newHoldingUpstream(t *testing.T) *holdingUpstream {
	t.Helper()
	u := &holdingUpstream{entered: make(chan struct{}, 16), gate: make(chan struct{})}
	u.release = sync.OnceFunc(func() { close(u.gate) })
	// On a failed assertion too, so no held request outlives the test.
	t.Cleanup(u.release)
	return u
}

func (u *holdingUpstream) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	var body io.ReadCloser = io.NopCloser(strings.NewReader(limitTestBody))
	if req.Header.Get(testHoldHeader) != "" {
		pr, pw := io.Pipe()
		ctx := req.Context()
		go func() {
			select {
			case <-u.gate:
				_ = pw.Close()
			case <-ctx.Done():
				_ = pw.CloseWithError(ctx.Err())
			}
		}()
		body = pr
		u.entered <- struct{}{}
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{contentTypeJSON}},
		Body:       body,
		Request:    req,
	}, nil
}

func callerRequest(ctx context.Context, caller, method, path string) *http.Request {
	req := httptest.NewRequestWithContext(ctx, method, limitTestCluster+path, nil)
	req.Header.Set(testCallerHeader, caller)
	return req
}

func getAs(h http.Handler, caller, path string) *httptest.ResponseRecorder {
	return serve(h, callerRequest(context.Background(), caller, http.MethodGet, path))
}

// hold starts a request the upstream keeps open and returns once it is there. The channel
// yields its response when the handler has returned.
func (u *holdingUpstream) hold(ctx context.Context, h http.Handler, caller, path string) <-chan *httptest.ResponseRecorder {
	req := callerRequest(ctx, caller, http.MethodGet, path)
	req.Header.Set(testHoldHeader, "1")
	done := make(chan *httptest.ResponseRecorder, 1)
	go func() { done <- serve(h, req) }()
	<-u.entered
	return done
}

func lastEvent(t *testing.T, sink *recordingSink) AuditEvent {
	t.Helper()
	sink.mu.Lock()
	defer sink.mu.Unlock()
	if len(sink.events) == 0 {
		t.Fatal("no audit event recorded")
	}
	return sink.events[len(sink.events)-1]
}

// wantCallerThrottled checks a caller-limit 429: the status, header and Status body the
// shared cap answers with, and the audit event's outcome and reason.
func wantCallerThrottled(t *testing.T, rec *httptest.ResponseRecorder, sink *recordingSink, reason RejectionReason, message string) {
	t.Helper()
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("code = %d, want 429: %s", rec.Code, rec.Body.String())
	}
	if got := rec.Header().Get(headerRetryAfter); got != retryAfterSeconds {
		t.Errorf("Retry-After = %q, want %q", got, retryAfterSeconds)
	}
	if s := decodeStatus(t, rec); s.Reason != metav1.StatusReasonTooManyRequests || s.Message != message {
		t.Errorf("Status = (%q, %q), want (TooManyRequests, %q)", s.Reason, s.Message, message)
	}
	ev := lastEvent(t, sink)
	if ev.Outcome != OutcomeThrottled || ev.Reason != reason || ev.Caller == nil || ev.Caller.Username != limitedCaller {
		t.Errorf("audit = (%q, %q, %+v), want (%q, %q) for %s", ev.Outcome, ev.Reason, ev.Caller, OutcomeThrottled, reason, limitedCaller)
	}
}

func newLimitedHandler(t *testing.T, cfg Config) (http.Handler, *handler, *holdingUpstream, *recordingSink) {
	t.Helper()
	up, sink := newHoldingUpstream(t), &recordingSink{}
	cfg.Authenticator, cfg.Upstream, cfg.Audit = headerAuthenticator{}, up, sink
	h, _ := newTestHandler(t, cfg)
	return h, h.(*handler), up, sink
}

func TestCallerInFlightLimit(t *testing.T) {
	metrics := NewMetrics()
	h, hh, up, sink := newLimitedHandler(t, Config{
		Metrics:      metrics,
		MaxInFlight:  2,
		CallerLimits: limitsFor(CallerLimits{MaxInFlight: 1, MaxLongRunning: 1}),
	})

	held := up.hold(context.Background(), h, limitedCaller, limitPodsPath)
	for range 3 {
		wantCallerThrottled(t, getAs(h, limitedCaller, limitNSPath), sink, ReasonCallerInFlightLimit, callerThrottledMessage)
	}
	if got := len(hh.slots); got != 1 {
		t.Errorf("%d shared slots taken, want 1: a caller refused at its own limit takes none", got)
	}
	scraped := scrapeMetricsText(t, metrics)
	for _, want := range []string{
		`kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",reason="caller_in_flight_limit",resource="namespaces",subresource="none",verb="list"} 3`,
		`outcome="throttled",resource="namespaces",status_code="429"`,
	} {
		if !strings.Contains(scraped, want) {
			t.Errorf("metrics lack %q", want)
		}
	}

	if rec := getAs(h, unlimitedCaller, limitNSPath); rec.Code != http.StatusOK {
		t.Errorf("another caller: code = %d, want 200: the limited caller left it the second shared slot", rec.Code)
	}
	if rec := getAs(h, limitedCaller, limitWatchPath); rec.Code != http.StatusOK {
		t.Errorf("the limited caller's watch: code = %d, want 200: long-running requests are counted apart", rec.Code)
	}
	write := callerRequest(context.Background(), limitedCaller, http.MethodPost, limitWritePath)
	write.Body = io.NopCloser(strings.NewReader(`{}`))
	if rec := serve(h, write); rec.Code != http.StatusForbidden {
		t.Errorf("refused write at the limit: code = %d, want 403: the policy answers before the limit", rec.Code)
	}

	up.release()
	if rec := <-held; rec.Code != http.StatusOK {
		t.Fatalf("held request: code = %d, want 200", rec.Code)
	}
	if got := hh.callers.entries(); got != 0 {
		t.Errorf("%d callers still counted after the last request finished, want 0", got)
	}
	if got := len(hh.slots); got != 0 {
		t.Errorf("%d shared slots still taken, want 0", got)
	}
	if rec := getAs(h, limitedCaller, limitNSPath); rec.Code != http.StatusOK {
		t.Errorf("after the held request finished: code = %d, want 200", rec.Code)
	}
}

// A request Authorize or the policy refuses is not counted, whether or not its caller is
// at the limit.
func TestCallerLimitsDoNotCountRefusedRequests(t *testing.T) {
	asked := 0
	limits := func(*CallerIdentity) CallerLimits {
		asked++
		return CallerLimits{MaxInFlight: 1, MaxLongRunning: 1}
	}
	h, hh, _, _ := newLimitedHandler(t, Config{
		CallerLimits: limits,
		Authorize: func(attrs AuthorizeAttributes) error {
			if attrs.Caller.Username == unlimitedCaller {
				return &AuthorizeError{}
			}
			return nil
		},
	})
	write := callerRequest(context.Background(), limitedCaller, http.MethodPost, limitWritePath)
	write.Body = io.NopCloser(strings.NewReader(`{}`))
	if rec := serve(h, write); rec.Code != http.StatusForbidden {
		t.Fatalf("write: code = %d, want the policy's 403", rec.Code)
	}
	if rec := getAs(h, unlimitedCaller, limitPodsPath); rec.Code != http.StatusForbidden {
		t.Fatalf("caller Authorize refuses: code = %d, want 403", rec.Code)
	}
	if asked != 0 || hh.callers.entries() != 0 {
		t.Errorf("limits asked %d times and %d callers counted for refused requests, want neither", asked, hh.callers.entries())
	}
}

// A caller under its own limit that the shared cap turns away is throttled as before, with
// no reason, and gives its own count back.
func TestCallerLimitReleasedWhenTheSharedCapRefuses(t *testing.T) {
	h, hh, up, sink := newLimitedHandler(t, Config{
		MaxInFlight:  1,
		CallerLimits: limitsFor(CallerLimits{MaxInFlight: 2}),
	})
	held := up.hold(context.Background(), h, unlimitedCaller, limitPodsPath)
	rec := getAs(h, limitedCaller, limitNSPath)
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("over the shared cap: code = %d, want 429", rec.Code)
	}
	if s := decodeStatus(t, rec); s.Message != throttledMessage {
		t.Errorf("message = %q, want the shared cap's %q", s.Message, throttledMessage)
	}
	if ev := lastEvent(t, sink); ev.Outcome != OutcomeThrottled || ev.Reason != ReasonNone {
		t.Errorf("audit = (%q, %q), want throttled with no reason", ev.Outcome, ev.Reason)
	}
	if got := hh.callers.entries(); got != 0 {
		t.Errorf("%d callers counted after the shared cap refused the request, want 0", got)
	}
	up.release()
	<-held
}

func TestCallerLongRunningLimit(t *testing.T) {
	limits := limitsFor(CallerLimits{MaxInFlight: 1, MaxLongRunning: 1})

	t.Run("released when the stream ends", func(t *testing.T) {
		metrics := NewMetrics()
		h, hh, up, sink := newLimitedHandler(t, Config{Metrics: metrics, MaxInFlight: 1, CallerLimits: limits})
		held := up.hold(context.Background(), h, limitedCaller, limitWatchPath)

		wantCallerThrottled(t, getAs(h, limitedCaller, limitWatchPath), sink, ReasonCallerLongRunningLimit, callerLongRunningMessage)
		wantCallerThrottled(t, getAs(h, limitedCaller, limitLogPath), sink, ReasonCallerLongRunningLimit, callerLongRunningMessage)
		want := `kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",reason="caller_long_running_limit",resource="namespaces",subresource="none",verb="watch"} 1`
		if !strings.Contains(scrapeMetricsText(t, metrics), want) {
			t.Errorf("metrics lack %q", want)
		}
		if rec := getAs(h, limitedCaller, limitNSPath); rec.Code != http.StatusOK {
			t.Errorf("the limited caller's list: code = %d, want 200: a watch takes none of its in-flight limit", rec.Code)
		}
		if rec := getAs(h, unlimitedCaller, limitWatchPath); rec.Code != http.StatusOK {
			t.Errorf("another caller's watch: code = %d, want 200", rec.Code)
		}
		if got := len(hh.slots); got != 0 {
			t.Errorf("%d shared slots taken by a watch, want 0", got)
		}

		up.release()
		if rec := <-held; rec.Code != http.StatusOK {
			t.Fatalf("held watch: code = %d, want 200", rec.Code)
		}
		if got := hh.callers.entries(); got != 0 {
			t.Errorf("%d callers counted after the watch ended, want 0", got)
		}
		if rec := getAs(h, limitedCaller, limitWatchPath); rec.Code != http.StatusOK {
			t.Errorf("a watch after the first ended: code = %d, want 200", rec.Code)
		}
	})

	t.Run("released when the caller disconnects", func(t *testing.T) {
		h, hh, up, sink := newLimitedHandler(t, Config{CallerLimits: limits})
		ctx, disconnect := context.WithCancel(context.Background())
		defer disconnect()
		held := up.hold(ctx, h, limitedCaller, limitLogPath)
		wantCallerThrottled(t, getAs(h, limitedCaller, limitWatchPath), sink, ReasonCallerLongRunningLimit, callerLongRunningMessage)

		disconnect()
		<-held
		if got := hh.callers.entries(); got != 0 {
			t.Errorf("%d callers counted after the caller disconnected, want 0", got)
		}
		if rec := getAs(h, limitedCaller, limitWatchPath); rec.Code != http.StatusOK {
			t.Errorf("a watch after the disconnect: code = %d, want 200", rec.Code)
		}
	})
}

// An upgraded connection is hijacked from the server, and the reverse proxy keeps the
// handler in the copy until it closes, so its count is held for as long as it is open.
func TestCallerLongRunningLimitHoldsAnUpgradedConnection(t *testing.T) {
	backendRead, backendWrite := io.Pipe()
	t.Cleanup(func() { _ = backendWrite.Close() })
	upstream := transportFunc(func(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusSwitchingProtocols,
			ProtoMajor: 1,
			ProtoMinor: 1,
			Header:     http.Header{"Upgrade": {"SPDY/3.1"}, "Connection": {"Upgrade"}},
			// The backend sends nothing, so the session lasts until the caller closes.
			Body:    upgradedStream{Reader: backendRead, Writer: io.Discard},
			Request: req,
		}, nil
	})
	sink := &recordingSink{}
	h, _ := newTestHandler(t, Config{
		Authenticator: headerAuthenticator{},
		Upstream:      upstream,
		Audit:         sink,
		Policy:        Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true},
		CallerLimits:  limitsFor(CallerLimits{MaxLongRunning: 1}),
	})
	hh := h.(*handler)
	exec := func() *http.Request {
		req := callerRequest(context.Background(), limitedCaller, http.MethodPost, limitExecPath)
		req.Header.Set("Upgrade", "SPDY/3.1")
		req.Header.Set("Connection", "Upgrade")
		return req
	}

	// Twice: the second session is admitted only because the first gave its count back.
	for round := range 2 {
		proxySide, callerSide := net.Pipe()
		done := make(chan struct{})
		go func() {
			defer close(done)
			h.ServeHTTP(&hijackableRecorder{ResponseRecorder: httptest.NewRecorder(), conn: proxySide}, exec())
		}()
		resp, err := http.ReadResponse(bufio.NewReader(callerSide), nil)
		if err != nil || resp.StatusCode != http.StatusSwitchingProtocols {
			_ = callerSide.Close()
			t.Fatalf("round %d: the caller read (%v, %v), want a 101", round, resp, err)
		}

		wantCallerThrottled(t, serve(h, exec()), sink, ReasonCallerLongRunningLimit, callerLongRunningMessage)
		select {
		case <-done:
			t.Fatalf("round %d: the handler returned with the upgraded connection open", round)
		default:
		}

		_ = callerSide.Close()
		<-done
		_ = proxySide.Close()
		if got := hh.callers.entries(); got != 0 {
			t.Fatalf("round %d: %d callers counted after the upgraded connection closed, want 0", round, got)
		}
	}
}

// Many goroutines at once against an upstream that keeps every admitted request open: the
// limiter admits exactly as many as the limit and refuses every other, for a caller's own
// limit and for a group's, for requests in flight and for long-running ones, and when the
// held requests end nothing is left counted. Three rounds through one handler and one
// limiter, so the releases of a round are what admit the next.
func TestCallerLimitsAdmitExactlyTheLimitUnderConcurrency(t *testing.T) {
	const (
		limit      = 5
		goroutines = 64
		rounds     = 3
		// roomy is a limit no round reaches, for the count that is not under test.
		roomy = goroutines * 2
		group = "workers"
	)
	// The caller's own limit: every goroutine is the same caller. The group's: every
	// goroutine is a caller of its own, with room of its own, in one group.
	ownLimit := func(*CallerIdentity) CallerLimits {
		return CallerLimits{MaxInFlight: limit, MaxLongRunning: limit, Group: group, GroupMaxInFlight: roomy, GroupMaxLongRunning: roomy}
	}
	groupLimit := func(*CallerIdentity) CallerLimits {
		return CallerLimits{MaxInFlight: roomy, MaxLongRunning: roomy, Group: group, GroupMaxInFlight: limit, GroupMaxLongRunning: limit}
	}
	sameCaller := func(int) string { return limitedCaller }
	ownCaller := func(g int) string { return limitedCaller + "-" + strconv.Itoa(g) }

	for _, tc := range []struct {
		name   string
		limits func(*CallerIdentity) CallerLimits
		caller func(int) string
		path   string
		reason RejectionReason
	}{
		{"a caller's requests in flight", ownLimit, sameCaller, limitPodsPath, ReasonCallerInFlightLimit},
		{"a caller's long-running requests", ownLimit, sameCaller, limitWatchPath, ReasonCallerLongRunningLimit},
		{"a group's requests in flight", groupLimit, ownCaller, limitPodsPath, ReasonCallerGroupInFlightLimit},
		{"a group's long-running requests", groupLimit, ownCaller, limitWatchPath, ReasonCallerGroupLongRunningLimit},
	} {
		t.Run(tc.name, func(t *testing.T) {
			// One handler for every round; each round swaps in an upstream of its own to hold
			// and release.
			var current atomic.Pointer[holdingUpstream]
			upstream := transportFunc(func(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
				return current.Load().RoundTripCluster(ctx, clusterID, req)
			})
			sink := &recordingSink{}
			h, _ := newTestHandler(t, Config{Authenticator: headerAuthenticator{}, Upstream: upstream, Audit: sink, CallerLimits: tc.limits})
			hh := h.(*handler)
			for round := range rounds {
				up := &holdingUpstream{entered: make(chan struct{}, goroutines), gate: make(chan struct{})}
				up.release = sync.OnceFunc(func() { close(up.gate) })
				t.Cleanup(up.release)
				current.Store(up)
				sink.mu.Lock()
				sink.events = nil
				sink.mu.Unlock()

				answers := make(chan int, goroutines)
				start := make(chan struct{})
				for g := range goroutines {
					req := callerRequest(context.Background(), tc.caller(g), http.MethodGet, tc.path)
					req.Header.Set(testHoldHeader, "1")
					go func() {
						<-start
						answers <- serve(h, req).Code
					}()
				}
				close(start)

				// Every request either reaches the upstream, where it is held, or is answered.
				admitted, refused := 0, 0
				for range goroutines {
					select {
					case <-up.entered:
						admitted++
					case code := <-answers:
						if code != http.StatusTooManyRequests {
							t.Errorf("round %d: a request not admitted was answered %d, want 429", round, code)
						}
						refused++
					}
				}
				if admitted != limit || refused != goroutines-limit {
					t.Fatalf("round %d: %d admitted and %d refused, want exactly %d and %d", round, admitted, refused, limit, goroutines-limit)
				}
				sink.mu.Lock()
				for _, ev := range sink.events {
					if ev.Outcome != OutcomeThrottled || ev.Reason != tc.reason {
						t.Errorf("round %d: audit = (%q, %q), want (%q, %q)", round, ev.Outcome, ev.Reason, OutcomeThrottled, tc.reason)
					}
				}
				sink.mu.Unlock()

				up.release()
				for range admitted {
					if code := <-answers; code != http.StatusOK {
						t.Errorf("round %d: an admitted request was answered %d, want 200", round, code)
					}
				}
				if got := hh.callers.entries(); got != 0 {
					t.Fatalf("round %d: %d keys and groups still counted with nothing open, want 0", round, got)
				}
			}
		})
	}
}

// A request must have room under its caller's key and under its group: callers of one group
// share the group's count whatever their keys, the refusal names the limit that was hit,
// the key first, and each request is held to the group limits its own caller returned.
func TestCallerGroupLimit(t *testing.T) {
	const (
		group      = "workers"
		firstPod   = limitedCaller + "/pod-a"
		secondPod  = limitedCaller + "/pod-b"
		laxCaller  = "system:serviceaccount:agents:lax"
		groupLimit = 2
	)
	limits := func(caller *CallerIdentity) CallerLimits {
		switch caller.Username {
		case unlimitedCaller:
			return CallerLimits{}
		case laxCaller:
			// The same group with a higher limit of its own.
			return CallerLimits{Group: group, GroupMaxInFlight: groupLimit + 1, GroupMaxLongRunning: groupLimit + 1}
		}
		// One username, counted per pod: the key the embedder chose.
		return CallerLimits{Key: caller.Username + "/" + caller.Extra[testPodExtra][0], MaxInFlight: 1, MaxLongRunning: 1,
			Group: group, GroupMaxInFlight: groupLimit, GroupMaxLongRunning: groupLimit}
	}
	pod := func(name string) *CallerIdentity {
		return &CallerIdentity{Username: limitedCaller, Extra: map[string][]string{testPodExtra: {name}}}
	}
	for name, longRunning := range map[string]bool{"in flight": false, "long-running": true} {
		t.Run(name, func(t *testing.T) {
			keyReason, groupReason := ReasonCallerInFlightLimit, ReasonCallerGroupInFlightLimit
			if longRunning {
				keyReason, groupReason = ReasonCallerLongRunningLimit, ReasonCallerGroupLongRunningLimit
			}
			l := newCallerLimiter(limits, 0, slog.New(slog.DiscardHandler))
			first, _, ok := l.acquire(pod("pod-a"), longRunning)
			if !ok {
				t.Fatal("the first pod's request was refused")
			}
			if _, reason, ok := l.acquire(pod("pod-a"), longRunning); ok || reason != keyReason {
				t.Errorf("the first pod's second request = (ok %t, %q), want refused with %q", ok, reason, keyReason)
			}
			second, _, ok := l.acquire(pod("pod-b"), longRunning)
			if !ok {
				t.Fatal("the second pod's request was refused: pods of one username are counted apart")
			}
			// Both the key and the group are full: the key answers.
			if _, reason, ok := l.acquire(pod("pod-b"), longRunning); ok || reason != keyReason {
				t.Errorf("the second pod's second request = (ok %t, %q), want refused with %q", ok, reason, keyReason)
			}
			if _, reason, ok := l.acquire(pod("pod-c"), longRunning); ok || reason != groupReason {
				t.Errorf("a third pod's request = (ok %t, %q), want refused with %q: the group is full", ok, reason, groupReason)
			}
			if got, want := l.entries(), len([]string{firstPod, secondPod, group}); got != want {
				t.Errorf("%d keys and groups counted, want %d: a refused request leaves no entry", got, want)
			}
			// The other kind has room of its own, for the key and for the group.
			other, _, ok := l.acquire(pod("pod-a"), !longRunning)
			if !ok {
				t.Fatal("the first pod's request of the other kind was refused")
			}
			other()
			// The limits in force are the request's own caller's: a caller that returns a
			// higher limit for the group is admitted against the same count, once.
			lax, _, ok := l.acquire(&CallerIdentity{Username: laxCaller}, longRunning)
			if !ok {
				t.Fatal("a caller with a higher limit for the group was refused under it")
			}
			if _, reason, ok := l.acquire(&CallerIdentity{Username: laxCaller}, longRunning); ok || reason != groupReason {
				t.Errorf("that caller's second request = (ok %t, %q), want refused with %q", ok, reason, groupReason)
			}
			if release, _, ok := l.acquire(&CallerIdentity{Username: unlimitedCaller}, longRunning); !ok {
				t.Error("a caller outside the group was refused")
			} else {
				release()
			}
			lax()

			// One release gives back the key and the group together.
			first()
			first()
			third, _, ok := l.acquire(pod("pod-a"), longRunning)
			if !ok {
				t.Fatal("the first pod was refused after its request ended")
			}
			for _, release := range []func(){second, third} {
				release()
			}
			if got := l.entries(); got != 0 {
				t.Errorf("%d keys and groups counted with nothing open, want 0", got)
			}
		})
	}
}

// Through the handler: a group refusal is the same 429, with the group's reason and message
// in the audit record, the Status and the rejection metric, and it takes no shared slot.
func TestCallerGroupLimitThroughTheHandler(t *testing.T) {
	metrics := NewMetrics()
	h, hh, up, sink := newLimitedHandler(t, Config{
		Metrics:     metrics,
		MaxInFlight: 4,
		CallerLimits: func(caller *CallerIdentity) CallerLimits {
			if caller.Username == unlimitedCaller {
				return CallerLimits{}
			}
			return CallerLimits{MaxInFlight: 4, MaxLongRunning: 4, Group: "workers", GroupMaxInFlight: 1, GroupMaxLongRunning: 1}
		},
	})
	const otherWorker = "system:serviceaccount:agents:other-worker"
	heldList := up.hold(context.Background(), h, otherWorker, limitPodsPath)
	heldWatch := up.hold(context.Background(), h, otherWorker, limitWatchPath)

	wantCallerThrottled(t, getAs(h, limitedCaller, limitNSPath), sink, ReasonCallerGroupInFlightLimit, callerGroupThrottledMessage)
	wantCallerThrottled(t, getAs(h, limitedCaller, limitWatchPath), sink, ReasonCallerGroupLongRunningLimit, callerGroupLongRunningMessage)
	if got := len(hh.slots); got != 1 {
		t.Errorf("%d shared slots taken, want 1: a caller refused for its group takes none", got)
	}
	scraped := scrapeMetricsText(t, metrics)
	for _, want := range []string{
		`reason="caller_group_in_flight_limit",resource="namespaces",subresource="none",verb="list"} 1`,
		`reason="caller_group_long_running_limit",resource="namespaces",subresource="none",verb="watch"} 1`,
	} {
		if !strings.Contains(scraped, want) {
			t.Errorf("metrics lack %q", want)
		}
	}
	if rec := getAs(h, unlimitedCaller, limitNSPath); rec.Code != http.StatusOK {
		t.Errorf("a caller outside the group: code = %d, want 200", rec.Code)
	}

	up.release()
	for _, held := range []<-chan *httptest.ResponseRecorder{heldList, heldWatch} {
		if rec := <-held; rec.Code != http.StatusOK {
			t.Errorf("held request: code = %d, want 200", rec.Code)
		}
	}
	if got := hh.callers.entries(); got != 0 {
		t.Errorf("%d keys and groups counted with nothing open, want 0", got)
	}
	if rec := getAs(h, limitedCaller, limitNSPath); rec.Code != http.StatusOK {
		t.Errorf("after the group emptied: code = %d, want 200", rec.Code)
	}
}

// Without Config.CallerLimits the handler has no limiter, and a caller the func gives no
// limit is not counted: one caller may hold as many requests as the shared cap allows.
func TestCallerLimitsUnset(t *testing.T) {
	for name, limits := range map[string]func(*CallerIdentity) CallerLimits{
		"no func":           nil,
		"a zero answer":     limitsFor(CallerLimits{}),
		"a negative answer": limitsFor(CallerLimits{MaxInFlight: -1, MaxLongRunning: -1}),
	} {
		t.Run(name, func(t *testing.T) {
			metrics := NewMetrics()
			h, hh, up, sink := newLimitedHandler(t, Config{Metrics: metrics, MaxInFlight: 2, CallerLimits: limits})
			if limits == nil && hh.callers != nil {
				t.Error("a handler built without CallerLimits has a limiter")
			}
			first := up.hold(context.Background(), h, limitedCaller, limitPodsPath)
			second := up.hold(context.Background(), h, limitedCaller, limitPodsPath)
			watch := up.hold(context.Background(), h, limitedCaller, limitWatchPath)
			if got := hh.callers.entries(); got != 0 {
				t.Errorf("%d callers counted, want 0", got)
			}
			rec := getAs(h, limitedCaller, limitNSPath)
			if rec.Code != http.StatusTooManyRequests {
				t.Fatalf("over the shared cap: code = %d, want 429", rec.Code)
			}
			if ev := lastEvent(t, sink); ev.Outcome != OutcomeThrottled || ev.Reason != ReasonNone {
				t.Errorf("audit = (%q, %q), want throttled with no reason", ev.Outcome, ev.Reason)
			}
			if scraped := scrapeMetricsText(t, metrics); strings.Contains(scraped, rejectedRequestsTotalName+"{") {
				t.Errorf("the shared cap's 429 was counted as a rejection:\n%s", scraped)
			}
			up.release()
			for _, held := range []<-chan *httptest.ResponseRecorder{first, second, watch} {
				if rec := <-held; rec.Code != http.StatusOK {
					t.Errorf("held request: code = %d, want 200", rec.Code)
				}
			}
		})
	}
}

func TestCallerLimiter(t *testing.T) {
	var nilLimiter *callerLimiter
	if release, reason, ok := nilLimiter.acquire(&CallerIdentity{Username: limitedCaller}, false); !ok || reason != ReasonNone {
		t.Errorf("a nil limiter refused (%q)", reason)
	} else {
		release()
	}

	l := newCallerLimiter(limitsFor(CallerLimits{MaxInFlight: 1, MaxLongRunning: 1}), 0, slog.New(slog.DiscardHandler))
	caller := &CallerIdentity{Username: limitedCaller, Partition: "team-a"}
	release, _, ok := l.acquire(caller, false)
	if !ok {
		t.Fatal("the first request was refused")
	}
	// The same username in another partition is the same caller.
	if _, reason, ok := l.acquire(&CallerIdentity{Username: limitedCaller, Partition: "team-b"}, false); ok || reason != ReasonCallerInFlightLimit {
		t.Errorf("a second request = (ok %t, %q), want refused with %q", ok, reason, ReasonCallerInFlightLimit)
	}
	stream, _, ok := l.acquire(caller, true)
	if !ok {
		t.Fatal("a long-running request was refused by the in-flight count")
	}
	if _, reason, ok := l.acquire(caller, true); ok || reason != ReasonCallerLongRunningLimit {
		t.Errorf("a second long-running request = (ok %t, %q), want refused with %q", ok, reason, ReasonCallerLongRunningLimit)
	}
	// A release called twice gives back one count, not two.
	release()
	release()
	if got := l.entries(); got != 1 {
		t.Errorf("%d callers counted with a stream open, want 1", got)
	}
	again, _, ok := l.acquire(caller, false)
	if !ok {
		t.Fatal("a request after a release was refused")
	}
	if _, _, ok := l.acquire(caller, false); ok {
		t.Error("a double release freed two counts")
	}
	again()
	stream()
	if got := l.entries(); got != 0 {
		t.Errorf("%d callers counted with nothing open, want 0", got)
	}
}

// A key an embedder sets and a username are different counts even when the strings are
// equal: a caller whose username is another caller's key does not land in its bucket.
func TestCallerKeyAndUsernameDoNotCollide(t *testing.T) {
	const podKey = limitedCaller + "/pod-a"
	limits := func(caller *CallerIdentity) CallerLimits {
		if caller.Username == limitedCaller {
			return CallerLimits{Key: podKey, MaxInFlight: 1}
		}
		// As a per-caller flag would give a caller the embedder does not key.
		return CallerLimits{MaxInFlight: 1}
	}
	l := newCallerLimiter(limits, 0, slog.New(slog.DiscardHandler))
	keyed, _, ok := l.acquire(&CallerIdentity{Username: limitedCaller}, false)
	if !ok {
		t.Fatal("the keyed caller was refused")
	}
	named, _, ok := l.acquire(&CallerIdentity{Username: podKey}, false)
	if !ok {
		t.Fatal("a caller whose username equals another caller's key was counted under that key")
	}
	if _, reason, ok := l.acquire(&CallerIdentity{Username: podKey}, false); ok || reason != ReasonCallerInFlightLimit {
		t.Errorf("that caller's second request = (ok %t, %q), want refused under its own count", ok, reason)
	}
	if got := l.entries(); got != 2 {
		t.Errorf("%d counts kept, want 2: one for the key and one for the username", got)
	}
	keyed()
	if _, _, ok := l.acquire(&CallerIdentity{Username: podKey}, false); ok {
		t.Error("the key's release freed the username's count")
	}
	named()
	if got := l.entries(); got != 0 {
		t.Errorf("%d counts kept with nothing open, want 0", got)
	}
}

// A group whose in-flight limit is not below a non-zero shared cap can take every shared
// slot: that is logged, at warning level, once for each such group name, and in no other
// case.
func TestGroupThatFillsTheSharedCapIsWarnedAboutOnce(t *testing.T) {
	const sharedCap = 4
	groupOf := func(name string, maxInFlight int) *CallerIdentity {
		return &CallerIdentity{Username: name, Extra: map[string][]string{testPodExtra: {strconv.Itoa(maxInFlight)}}}
	}
	// The username is the group, and the extra its in-flight limit; "" is no group.
	limits := func(caller *CallerIdentity) CallerLimits {
		maxInFlight, _ := strconv.Atoi(caller.Extra[testPodExtra][0])
		return CallerLimits{MaxInFlight: 1, MaxLongRunning: 1, Group: caller.Username, GroupMaxInFlight: maxInFlight, GroupMaxLongRunning: sharedCap * 2}
	}
	for _, tc := range []struct {
		name      string
		sharedCap int
		callers   []*CallerIdentity
		want      []string
	}{
		{"below the cap", sharedCap, []*CallerIdentity{groupOf("workers", sharedCap-1)}, nil},
		{"at the cap", sharedCap, []*CallerIdentity{groupOf("workers", sharedCap)}, []string{"workers"}},
		{"above the cap", sharedCap, []*CallerIdentity{groupOf("workers", sharedCap+1)}, []string{"workers"}},
		{"no shared cap", 0, []*CallerIdentity{groupOf("workers", sharedCap)}, nil},
		{"no group", sharedCap, []*CallerIdentity{groupOf("", sharedCap)}, nil},
		{"a group with no in-flight limit", sharedCap, []*CallerIdentity{groupOf("workers", 0)}, nil},
		{"two groups, one of them under the cap", sharedCap,
			[]*CallerIdentity{groupOf("workers", sharedCap), groupOf("sessions", 1), groupOf("batch", sharedCap)}, []string{"workers", "batch"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var logged bytes.Buffer
			l := newCallerLimiter(limits, tc.sharedCap, slog.New(slog.NewJSONHandler(&logged, nil)))
			// Each caller several times, of both kinds, refused requests included.
			for range 3 {
				for _, caller := range tc.callers {
					for _, longRunning := range []bool{false, true} {
						release, _, ok := l.acquire(caller, longRunning)
						if !ok {
							t.Fatalf("%s was refused", caller.Username)
						}
						_, _, _ = l.acquire(caller, longRunning)
						release()
					}
				}
			}
			lines := strings.Split(strings.TrimSpace(logged.String()), "\n")
			if logged.Len() == 0 {
				lines = nil
			}
			if len(lines) != len(tc.want) {
				t.Fatalf("%d records logged, want %d:\n%s", len(lines), len(tc.want), logged.String())
			}
			for i, group := range tc.want {
				for _, want := range []string{`"level":"WARN"`, groupFillsCapWarning, `"` + logKeyGroup + `":"` + group + `"`, `"` + logKeySharedCap + `":` + strconv.Itoa(tc.sharedCap)} {
					if !strings.Contains(lines[i], want) {
						t.Errorf("record %d lacks %q: %s", i, want, lines[i])
					}
				}
			}
		})
	}
}

// The handler compares a group's limit with its own Config.MaxInFlight and warns on its
// Config.Logger.
func TestHandlerWarnsAboutAGroupThatFillsMaxInFlight(t *testing.T) {
	limits := func(*CallerIdentity) CallerLimits {
		return CallerLimits{Group: "workers", GroupMaxInFlight: 2}
	}
	for name, tc := range map[string]struct {
		maxInFlight int
		want        int
	}{
		"a cap the group can fill": {2, 1},
		"a cap above the group":    {3, 0},
		"no cap":                   {0, 0},
	} {
		t.Run(name, func(t *testing.T) {
			var logged bytes.Buffer
			h, _ := newTestHandler(t, Config{
				Authenticator: headerAuthenticator{},
				Audit:         &recordingSink{},
				Logger:        slog.New(slog.NewJSONHandler(&logged, nil)),
				MaxInFlight:   tc.maxInFlight,
				CallerLimits:  limits,
			})
			for range 3 {
				if rec := getAs(h, limitedCaller, limitNSPath); rec.Code != http.StatusOK {
					t.Fatalf("code = %d, want 200", rec.Code)
				}
			}
			if got := strings.Count(logged.String(), groupFillsCapWarning); got != tc.want {
				t.Errorf("the warning was logged %d times, want %d:\n%s", got, tc.want, logged.String())
			}
		})
	}
}
