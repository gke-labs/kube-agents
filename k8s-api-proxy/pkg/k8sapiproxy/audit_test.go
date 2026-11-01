package k8sapiproxy

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

type recordingSink struct {
	mu     sync.Mutex
	events []AuditEvent
}

func (s *recordingSink) Record(_ context.Context, e AuditEvent) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, e)
}

// only returns the one event recorded, failing the test if there is not exactly one.
func (s *recordingSink) only(t *testing.T) AuditEvent {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.events) != 1 {
		t.Fatalf("recorded %d events, want 1: %+v", len(s.events), s.events)
	}
	return s.events[0]
}

type transportFunc func(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error)

func (f transportFunc) RoundTripCluster(ctx context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	return f(ctx, clusterID, req)
}

func respondWith(code int, body io.Reader) UpstreamTransport {
	return transportFunc(func(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: code,
			Header:     http.Header{headerContentType: []string{contentTypeJSON}},
			Body:       io.NopCloser(body),
			Request:    req,
		}, nil
	})
}

// failingBody returns its content, then err.
type failingBody struct {
	content io.Reader
	err     error
}

func (b failingBody) Read(p []byte) (int, error) {
	if n, err := b.content.Read(p); err != io.EOF {
		return n, err
	}
	return 0, b.err
}

func TestHandlerAuditEvents(t *testing.T) {
	const transportErr = "dial tcp 10.0.0.1:443: i/o timeout"
	tests := []struct {
		name     string
		cfg      Config
		method   string
		target   string
		want     AuditEvent
		wantErr  string
		noCaller bool
	}{
		{
			name: "forwarded", method: http.MethodGet, target: "/clusters/" + gkeFleet + "/api/v1/namespaces/kube-system/pods?limit=5",
			want: AuditEvent{ClusterID: gkeFleet, Path: "/api/v1/namespaces/kube-system/pods", Verb: "list", APIVersion: "v1",
				Resource: "pods", Namespace: "kube-system", Outcome: OutcomeForwarded, StatusCode: http.StatusOK},
		},
		{
			name: "dry-run write", method: http.MethodPost, target: "/apis/apps/v1/namespaces/default/deployments?dryRun=All",
			want: AuditEvent{ClusterID: InClusterID, Path: "/apis/apps/v1/namespaces/default/deployments", Verb: "create",
				APIGroup: "apps", APIVersion: "v1", Resource: "deployments", Namespace: "default", DryRun: true,
				Outcome: OutcomeForwarded, StatusCode: http.StatusOK},
		},
		{
			name: "upstream 5xx", method: http.MethodGet, target: "/api/v1/nodes/n1",
			cfg: Config{Upstream: respondWith(http.StatusServiceUnavailable, strings.NewReader("{}"))},
			want: AuditEvent{ClusterID: InClusterID, Path: "/api/v1/nodes/n1", Verb: "get", APIVersion: "v1", Resource: "nodes",
				Name: "n1", Outcome: OutcomeForwarded, StatusCode: http.StatusServiceUnavailable},
		},
		{
			name: "unauthenticated", method: http.MethodGet, target: "/clusters/" + gkeFleet + "/api/v1/pods",
			cfg: Config{Authenticator: staticAuthenticator{err: errors.New("token expired")}},
			want: AuditEvent{Path: "/clusters/" + gkeFleet + "/api/v1/pods", Outcome: OutcomeUnauthenticated,
				StatusCode: http.StatusUnauthorized},
			wantErr: "token expired", noCaller: true,
		},
		{
			name: "invalid cluster", method: http.MethodGet, target: "/clusters/bad$id/api/v1/pods",
			want: AuditEvent{Path: "/clusters/bad$id/api/v1/pods", Outcome: OutcomeRefused, Reason: ReasonInvalidRoute,
				StatusCode: http.StatusBadRequest},
		},
		{
			name: "unclassifiable", method: http.MethodGet, target: "/api/v1/watch",
			want: AuditEvent{ClusterID: InClusterID, Path: "/api/v1/watch", Outcome: OutcomeRefused, Reason: ReasonInvalidRoute,
				StatusCode: http.StatusBadRequest},
		},
		{
			name: "policy refusal", method: http.MethodPost, target: "/api/v1/namespaces/default/pods/p/exec?command=sh",
			want: AuditEvent{ClusterID: InClusterID, Path: "/api/v1/namespaces/default/pods/p/exec", Verb: "create",
				APIVersion: "v1", Resource: "pods", Subresource: "exec", Namespace: "default", Name: "p",
				Outcome: OutcomeRefused, Reason: ReasonSubresourceBlocked, StatusCode: http.StatusForbidden},
		},
		{
			name: "secret refusal", method: http.MethodPatch, target: "/api/v1/namespaces/default/secrets/s?dryRun=All",
			want: AuditEvent{ClusterID: InClusterID, Path: "/api/v1/namespaces/default/secrets/s", Verb: "patch",
				APIVersion: "v1", Resource: "secrets", Namespace: "default", Name: "s", DryRun: true,
				Outcome: OutcomeRefused, Reason: ReasonSecretWriteBlocked, StatusCode: http.StatusForbidden},
		},
		{
			name: "upstream error", method: http.MethodGet, target: "/api/v1/pods",
			cfg: Config{Upstream: &recordingTransport{err: errors.New(transportErr)}, DefaultClusterID: gkeHost},
			want: AuditEvent{ClusterID: gkeHost, Path: "/api/v1/pods", Verb: "list", APIVersion: "v1", Resource: "pods",
				Outcome: OutcomeUpstreamError, StatusCode: http.StatusBadGateway},
			wantErr: transportErr,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			sink := &recordingSink{}
			tc.cfg.Audit = sink
			h, _ := newTestHandler(t, tc.cfg)
			req := httptest.NewRequest(tc.method, tc.target, nil)
			req.Header.Set("User-Agent", "kubectl/v1.37.0")
			rec := serve(h, req)

			got := sink.only(t)
			if got.StatusCode != rec.Code {
				t.Errorf("StatusCode = %d, response code %d", got.StatusCode, rec.Code)
			}
			if (got.Caller == nil) != tc.noCaller {
				t.Errorf("Caller = %+v", got.Caller)
			}
			if tc.wantErr == "" && got.Err != nil || tc.wantErr != "" && (got.Err == nil || got.Err.Error() != tc.wantErr) {
				t.Errorf("Err = %v, want %q", got.Err, tc.wantErr)
			}
			if got.Duration <= 0 {
				t.Errorf("Duration = %v", got.Duration)
			}
			tc.want.HTTPMethod, tc.want.UserAgent = tc.method, "kubectl/v1.37.0"
			got.Caller, got.Err, got.Duration = nil, nil, 0
			if got != tc.want {
				t.Errorf("event:\n got %+v\nwant %+v", got, tc.want)
			}
		})
	}
}

func TestHandlerAuditsAbortedResponse(t *testing.T) {
	const readErr = "connection reset by peer"
	sink := &recordingSink{}
	var logs bytes.Buffer
	h, _ := newTestHandler(t, Config{
		Audit:    sink,
		Logger:   slog.New(slog.NewTextHandler(&logs, nil)),
		Upstream: respondWith(http.StatusOK, failingBody{content: strings.NewReader(`{"kind":"PodList"`), err: errors.New(readErr)}),
	})
	// Under an HTTP server the reverse proxy aborts a response that fails mid-copy.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil)
	req = req.WithContext(context.WithValue(req.Context(), http.ServerContextKey, &http.Server{}))

	func() {
		defer func() {
			if p := recover(); p != http.ErrAbortHandler {
				t.Errorf("recovered %v, want http.ErrAbortHandler", p)
			}
		}()
		serve(h, req)
	}()

	if got := sink.only(t); got.Outcome != OutcomeAborted || got.StatusCode != http.StatusOK {
		t.Errorf("event = %+v, want an aborted 200", got)
	}
	if !strings.Contains(logs.String(), readErr) || !strings.Contains(logs.String(), "level=ERROR") {
		t.Errorf("Logger got %q, want the copy error at error level", logs.String())
	}
}

func TestHandlerDefaultAuditSink(t *testing.T) {
	const token = "sandbox-token"
	var logs bytes.Buffer
	h, _ := newTestHandler(t, Config{Logger: slog.New(slog.NewJSONHandler(&logs, nil))})
	req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods?labelSelector=app", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	if rec := serve(h, req); rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	var record map[string]any
	if err := json.Unmarshal(logs.Bytes(), &record); err != nil {
		t.Fatalf("Logger got %q, want one JSON record: %v", logs.String(), err)
	}
	if record[auditKeyOutcome] != string(OutcomeForwarded) || record[auditKeyUsername] != shellCaller.caller.Username {
		t.Errorf("record = %v", record)
	}
	if strings.Contains(logs.String(), token) || strings.Contains(logs.String(), "labelSelector") {
		t.Errorf("record %q carries the token or the query", logs.String())
	}
}

func TestSlogAuditSink(t *testing.T) {
	tests := []struct {
		name      string
		event     AuditEvent
		wantLevel string
		want      map[string]any
		absent    []string
	}{
		{
			name: "forwarded",
			event: AuditEvent{
				Caller:    &CallerIdentity{Username: "system:serviceaccount:agents:shell", UID: "u1", Groups: []string{"system:serviceaccounts"}},
				ClusterID: gkeHost, HTTPMethod: http.MethodGet, Path: "/api/v1/pods", Verb: "list", APIVersion: "v1",
				Resource: "pods", Outcome: OutcomeForwarded, StatusCode: http.StatusOK, Duration: 1500 * time.Microsecond,
			},
			wantLevel: "INFO",
			want: map[string]any{
				auditKeyOutcome: "forwarded", auditKeyStatus: 200.0, auditKeyMethod: "GET", auditKeyPath: "/api/v1/pods",
				auditKeyDurationMS: 1.5, auditKeyCluster: gkeHost, auditKeyVerb: "list", auditKeyResource: "pods",
				auditKeyUsername: "system:serviceaccount:agents:shell", auditKeyUID: "u1",
				auditKeyGroups: []any{"system:serviceaccounts"},
			},
			absent: []string{auditKeyAPIGroup, auditKeyNamespace, auditKeyName, auditKeyReason, auditKeyDryRun, auditKeyError},
		},
		{
			name:      "refused",
			event:     AuditEvent{Outcome: OutcomeRefused, Reason: ReasonDryRunRequired, StatusCode: http.StatusForbidden},
			wantLevel: "WARN",
			want:      map[string]any{auditKeyReason: "dry_run_required", auditKeyStatus: 403.0},
			absent:    []string{auditKeyUsername, auditKeyCluster},
		},
		{
			name:      "unauthenticated",
			event:     AuditEvent{Outcome: OutcomeUnauthenticated, Err: errors.New("token expired"), StatusCode: http.StatusUnauthorized},
			wantLevel: "WARN",
			want:      map[string]any{auditKeyError: "token expired"},
		},
		{
			name:      "upstream error",
			event:     AuditEvent{Outcome: OutcomeUpstreamError, Err: errors.New("dial tcp: i/o timeout"), DryRun: true},
			wantLevel: "ERROR",
			want:      map[string]any{auditKeyError: "dial tcp: i/o timeout", auditKeyDryRun: true},
		},
		{name: "aborted", event: AuditEvent{Outcome: OutcomeAborted}, wantLevel: "ERROR"},
		{name: "canceled", event: AuditEvent{Outcome: OutcomeCanceled, Err: context.Canceled}, wantLevel: "INFO"},
		{name: "panicked", event: AuditEvent{Err: errors.New("panic: boom")}, wantLevel: "ERROR"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			NewSlogAuditSink(slog.New(slog.NewJSONHandler(&buf, nil))).Record(context.Background(), tc.event)
			var record map[string]any
			if err := json.Unmarshal(buf.Bytes(), &record); err != nil {
				t.Fatalf("not one JSON record: %v: %s", err, buf.String())
			}
			if record[slog.LevelKey] != tc.wantLevel || record[slog.MessageKey] != auditMessage {
				t.Errorf("level %v, msg %v; want %s %q", record[slog.LevelKey], record[slog.MessageKey], tc.wantLevel, auditMessage)
			}
			for k, v := range tc.want {
				if got, _ := json.Marshal(record[k]); string(got) != mustJSON(t, v) {
					t.Errorf("%s = %s, want %s", k, got, mustJSON(t, v))
				}
			}
			for _, k := range tc.absent {
				if v, ok := record[k]; ok {
					t.Errorf("%s = %v, want it absent", k, v)
				}
			}
		})
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// hijackableRecorder is a ResponseRecorder that can be hijacked, as a server's writer can.
type hijackableRecorder struct {
	*httptest.ResponseRecorder
	conn     net.Conn
	hijacked bool
}

func (h *hijackableRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	h.hijacked = true
	return h.conn, bufio.NewReadWriter(bufio.NewReader(h.conn), bufio.NewWriter(h.conn)), nil
}

// upgradedStream is the body of an upstream 101: the upgraded connection.
type upgradedStream struct {
	io.Reader
	io.Writer
}

func (upgradedStream) Close() error { return nil }

func TestHandlerAuditsUpgrade(t *testing.T) {
	sink := &recordingSink{}
	upstream := transportFunc(func(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusSwitchingProtocols,
			ProtoMajor: 1,
			ProtoMinor: 1,
			Header:     http.Header{"Upgrade": {"SPDY/3.1"}, "Connection": {"Upgrade"}},
			Body:       upgradedStream{Reader: strings.NewReader(""), Writer: io.Discard},
			Request:    req,
		}, nil
	})
	h, _ := newTestHandler(t, Config{Audit: sink, Upstream: upstream,
		Policy: Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true}})

	proxySide, callerSide := net.Pipe()
	t.Cleanup(func() { _ = callerSide.Close() })
	received := make(chan string, 1)
	go func() {
		b, _ := io.ReadAll(callerSide)
		received <- string(b)
	}()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods/p/exec?command=sh", nil)
	req.Header.Set("Upgrade", "SPDY/3.1")
	req.Header.Set("Connection", "Upgrade")
	h.ServeHTTP(&hijackableRecorder{ResponseRecorder: httptest.NewRecorder(), conn: proxySide}, req)
	_ = proxySide.Close()

	if got := <-received; !strings.HasPrefix(got, "HTTP/1.1 101") {
		t.Fatalf("caller received %q, want a 101", got)
	}
	if got := sink.only(t); got.Outcome != OutcomeForwarded || got.StatusCode != http.StatusSwitchingProtocols || got.Err != nil {
		t.Errorf("event = %+v, want a forwarded 101", got)
	}
}

func TestHandlerAuditsFailedUpgradeWrite(t *testing.T) {
	sink := &recordingSink{}
	upstream := transportFunc(func(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusSwitchingProtocols,
			ProtoMajor: 1,
			ProtoMinor: 1,
			Header:     http.Header{"Upgrade": {"SPDY/3.1"}, "Connection": {"Upgrade"}},
			Body:       upgradedStream{Reader: strings.NewReader(""), Writer: io.Discard},
			Request:    req,
		}, nil
	})
	h, _ := newTestHandler(t, Config{Audit: sink, Upstream: upstream,
		Policy: Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true}})

	// The caller's side is gone, so writing the 101 onto the hijacked connection fails.
	proxySide, callerSide := net.Pipe()
	_ = callerSide.Close()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods/p/exec?command=sh", nil)
	req.Header.Set("Upgrade", "SPDY/3.1")
	req.Header.Set("Connection", "Upgrade")
	h.ServeHTTP(&hijackableRecorder{ResponseRecorder: httptest.NewRecorder(), conn: proxySide}, req)

	if got := sink.only(t); got.Outcome != OutcomeAborted || got.Err == nil {
		t.Errorf("event = %+v, want aborted with the write error", got)
	}
}

type panicAuthenticator struct{}

func (panicAuthenticator) AuthenticateRequest(*http.Request) (*CallerIdentity, error) {
	panic("authenticator bug")
}

func TestHandlerAuditsCancelAndPanic(t *testing.T) {
	canceled := func(serverCtx bool) *http.Request {
		ctx, cancel := context.WithCancel(context.Background())
		cancel()
		if serverCtx {
			ctx = context.WithValue(ctx, http.ServerContextKey, &http.Server{})
		}
		return httptest.NewRequest(http.MethodGet, "/api/v1/pods?watch=1", nil).WithContext(ctx)
	}
	tests := []struct {
		name        string
		cfg         Config
		req         *http.Request
		wantPanic   any
		wantOutcome Outcome
		wantErr     string
	}{
		{
			name: "caller gone before the response",
			cfg: Config{Upstream: transportFunc(func(ctx context.Context, _ string, _ *http.Request) (*http.Response, error) {
				return nil, ctx.Err()
			})},
			req: canceled(false), wantOutcome: OutcomeCanceled, wantErr: context.Canceled.Error(),
		},
		{
			name:      "caller gone mid-stream",
			cfg:       Config{Upstream: respondWith(http.StatusOK, failingBody{content: strings.NewReader("{}"), err: context.Canceled})},
			req:       canceled(true),
			wantPanic: http.ErrAbortHandler, wantOutcome: OutcomeCanceled,
		},
		{
			name:      "panic",
			cfg:       Config{Authenticator: panicAuthenticator{}},
			req:       httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil),
			wantPanic: "authenticator bug", wantErr: "panic: authenticator bug",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			sink := &recordingSink{}
			tc.cfg.Audit = sink
			h, _ := newTestHandler(t, tc.cfg)
			func() {
				defer func() {
					if p := recover(); p != tc.wantPanic {
						t.Errorf("recovered %v, want %v", p, tc.wantPanic)
					}
				}()
				serve(h, tc.req)
			}()
			got := sink.only(t)
			if got.Outcome != tc.wantOutcome {
				t.Errorf("Outcome = %q, want %q", got.Outcome, tc.wantOutcome)
			}
			if gotErr := fmt.Sprint(got.Err); tc.wantErr != "" && gotErr != tc.wantErr {
				t.Errorf("Err = %s, want %s", gotErr, tc.wantErr)
			}
		})
	}
}

func TestStatusRecorder(t *testing.T) {
	for name, tc := range map[string]struct {
		write func(w http.ResponseWriter)
		want  int
	}{
		"interim then final":    {func(w http.ResponseWriter) { w.WriteHeader(http.StatusEarlyHints); w.WriteHeader(http.StatusOK) }, http.StatusOK},
		"write without header":  {func(w http.ResponseWriter) { _, _ = w.Write([]byte("{}")) }, http.StatusOK},
		"first final code wins": {func(w http.ResponseWriter) { w.WriteHeader(http.StatusNotFound); w.WriteHeader(http.StatusOK) }, http.StatusNotFound},
	} {
		t.Run(name, func(t *testing.T) {
			rec := &statusRecorder{ResponseWriter: httptest.NewRecorder()}
			tc.write(rec)
			if rec.code != tc.want {
				t.Errorf("code = %d, want %d", rec.code, tc.want)
			}
		})
	}

	// The reverse proxy flushes and hijacks through http.ResponseController.
	under := &hijackableRecorder{ResponseRecorder: httptest.NewRecorder()}
	rec := &statusRecorder{ResponseWriter: under}
	rc := http.NewResponseController(rec)
	if err := rc.Flush(); err != nil || !under.Flushed {
		t.Errorf("Flush = %v, flushed %t", err, under.Flushed)
	}
	if _, _, err := rc.Hijack(); err != nil || !under.hijacked || rec.code != http.StatusSwitchingProtocols {
		t.Errorf("Hijack = %v, hijacked %t, code %d", err, under.hijacked, rec.code)
	}
	// Nothing is written to a hijacked writer.
	rec.WriteHeader(http.StatusBadGateway)
	if _, err := rec.Write([]byte("{}")); err != http.ErrHijacked || under.Code == http.StatusBadGateway || rec.code != http.StatusSwitchingProtocols {
		t.Errorf("after hijack: Write err %v, underlying code %d, recorded %d", err, under.Code, rec.code)
	}
}
