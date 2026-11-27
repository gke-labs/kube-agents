package k8sapiproxy

import (
	"bufio"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"runtime"
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
	if got := hh.callers.callers(); got != 0 {
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
	if asked != 0 || hh.callers.callers() != 0 {
		t.Errorf("limits asked %d times and %d callers counted for refused requests, want neither", asked, hh.callers.callers())
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
	if got := hh.callers.callers(); got != 0 {
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
		if got := hh.callers.callers(); got != 0 {
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
		if got := hh.callers.callers(); got != 0 {
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
		if got := hh.callers.callers(); got != 0 {
			t.Fatalf("round %d: %d callers counted after the upgraded connection closed, want 0", round, got)
		}
	}
}

// Many goroutines for one limited caller: the upstream never sees more of its requests at
// once than its limits allow, of either kind, every answer is a 200 or a 429, an unlimited
// caller is never throttled, and nothing is left counted.
func TestCallerLimitsUnderConcurrency(t *testing.T) {
	const (
		maxInFlight    = 3
		maxLongRunning = 2
		goroutines     = 48
		requestsEach   = 40
	)
	var inFlight, longRunning, peakInFlight, peakLongRunning atomic.Int64
	peak := func(p *atomic.Int64, n int64) {
		for {
			old := p.Load()
			if n <= old || p.CompareAndSwap(old, n) {
				return
			}
		}
	}
	upstream := &fakeUpstream{handler: func(_ string, req *http.Request) (*http.Response, error) {
		if req.Header.Get(testCallerHeader) == limitedCaller {
			count, top := &inFlight, &peakInFlight
			if req.URL.Query().Has("watch") {
				count, top = &longRunning, &peakLongRunning
			}
			peak(top, count.Add(1))
			runtime.Gosched()
			count.Add(-1)
		}
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{contentTypeJSON}},
			Body:       io.NopCloser(strings.NewReader(limitTestBody)),
		}, nil
	}}
	h, _ := newTestHandler(t, Config{
		Authenticator: headerAuthenticator{},
		Upstream:      upstream,
		CallerLimits:  limitsFor(CallerLimits{MaxInFlight: maxInFlight, MaxLongRunning: maxLongRunning}),
	})
	hh := h.(*handler)

	var served, throttled, unexpected, unlimitedThrottled atomic.Int64
	var wg sync.WaitGroup
	for g := range goroutines {
		wg.Go(func() {
			caller, path := limitedCaller, limitNSPath
			if g%2 == 1 {
				path = limitWatchPath
			}
			if g%8 == 0 {
				caller = unlimitedCaller
			}
			for range requestsEach {
				switch code := getAs(h, caller, path).Code; {
				case code == http.StatusOK:
					served.Add(1)
				case code == http.StatusTooManyRequests && caller == limitedCaller:
					throttled.Add(1)
				case code == http.StatusTooManyRequests:
					unlimitedThrottled.Add(1)
				default:
					unexpected.Add(1)
				}
			}
		})
	}
	wg.Wait()

	if got := peakInFlight.Load(); got > maxInFlight {
		t.Errorf("the upstream saw %d of the caller's requests at once, want at most %d", got, maxInFlight)
	}
	if got := peakLongRunning.Load(); got > maxLongRunning {
		t.Errorf("the upstream saw %d of the caller's watches at once, want at most %d", got, maxLongRunning)
	}
	if served.Load() == 0 || unexpected.Load() != 0 || unlimitedThrottled.Load() != 0 {
		t.Errorf("served %d, throttled %d, other answers %d, unlimited caller throttled %d; want some served and only 200s and the limited caller's 429s",
			served.Load(), throttled.Load(), unexpected.Load(), unlimitedThrottled.Load())
	}
	if got := served.Load() + throttled.Load(); got != goroutines*requestsEach {
		t.Errorf("%d requests answered, want %d", got, goroutines*requestsEach)
	}
	if got := hh.callers.callers(); got != 0 {
		t.Errorf("%d callers still counted with nothing in flight, want 0", got)
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
			if got := hh.callers.callers(); got != 0 {
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

	l := newCallerLimiter(limitsFor(CallerLimits{MaxInFlight: 1, MaxLongRunning: 1}))
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
	if got := l.callers(); got != 1 {
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
	if got := l.callers(); got != 0 {
		t.Errorf("%d callers counted with nothing open, want 0", got)
	}
}
