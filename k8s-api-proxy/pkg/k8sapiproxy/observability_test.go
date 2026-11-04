package k8sapiproxy

import (
	"bytes"
	"context"
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

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

type recordingSpanExporter struct {
	mu    sync.Mutex
	spans []sdktrace.ReadOnlySpan
}

type fakeUpstream struct {
	handler func(clusterID string, req *http.Request) (*http.Response, error)
}

func (f *fakeUpstream) RoundTripCluster(_ context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	resp, err := f.handler(clusterID, req)
	if resp != nil && resp.Request == nil {
		resp.Request = req
	}
	return resp, err
}

type tokenAuthenticator struct {
	expected string
}

func (a tokenAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	if r.Header.Get("Authorization") != "Bearer "+a.expected {
		return nil, errors.New("unauthorized")
	}
	return &CallerIdentity{Username: "system:serviceaccount:default:shell"}, nil
}

func (e *recordingSpanExporter) ExportSpans(_ context.Context, spans []sdktrace.ReadOnlySpan) error {
	e.mu.Lock()
	defer e.mu.Unlock()
	e.spans = append(e.spans, spans...)
	return nil
}

func (e *recordingSpanExporter) Shutdown(_ context.Context) error {
	return nil
}

func (e *recordingSpanExporter) snapshot() []sdktrace.ReadOnlySpan {
	e.mu.Lock()
	defer e.mu.Unlock()
	out := make([]sdktrace.ReadOnlySpan, len(e.spans))
	copy(out, e.spans)
	return out
}

func scrapeMetricsText(t *testing.T, m *Metrics) string {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, MetricsPath, nil)
	rec := httptest.NewRecorder()
	m.Handler().ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET /metrics status = %d, want 200", rec.Code)
	}
	return rec.Body.String()
}

func TestPrometheusMetricsAndCardinalitySafety(t *testing.T) {
	metrics := NewMetrics()
	upstream := &fakeUpstream{
		handler: func(_ string, req *http.Request) (*http.Response, error) {
			if strings.Contains(req.URL.Path, "/secrets/") {
				body := `{"apiVersion":"meta.k8s.io/v1","kind":"PartialObjectMetadata","metadata":{"name":"my-secret","namespace":"default"}}`
				return &http.Response{
					StatusCode: http.StatusOK,
					Header:     http.Header{"Content-Type": []string{"application/json"}},
					Body:       io.NopCloser(bytes.NewBufferString(body)),
				}, nil
			}
			status := http.StatusOK
			if req.Method == http.MethodPost {
				status = http.StatusCreated
			}
			return &http.Response{
				StatusCode: status,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(bytes.NewBufferString(`{"kind":"PodList","items":[]}`)),
			}, nil
		},
	}

	h, err := NewHandler(Config{
		Authenticator: tokenAuthenticator{expected: "valid-token"},
		Upstream:      upstream,
		Policy:        Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:        slog.New(slog.DiscardHandler),
		Metrics:       metrics,
	})
	if err != nil {
		t.Fatalf("NewHandler failed: %v", err)
	}

	// 1. Healthz request (unauthenticated).
	{
		req := httptest.NewRequest(http.MethodGet, "/_kube_proxy/healthz", nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("healthz status = %d, want 200", rec.Code)
		}
	}

	// 2. Allowed GET pods.
	{
		req := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/pods", nil)
		req.Header.Set("Authorization", "Bearer valid-token")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET pods status = %d, want 200", rec.Code)
		}
	}

	// 3. Allowed dry-run POST configmaps.
	{
		req := httptest.NewRequest(http.MethodPost, "/clusters/in-cluster/api/v1/namespaces/default/configmaps?dryRun=All", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer valid-token")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusCreated {
			t.Fatalf("POST configmaps dryRun=All status = %d, want 201", rec.Code)
		}
	}

	// 4. Allowed GET secret (triggers Accept header rewrite + payload scrub).
	{
		req := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/secrets/my-secret", nil)
		req.Header.Set("Authorization", "Bearer valid-token")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET secret status = %d, want 200", rec.Code)
		}
	}

	// 5. Rejected unauthenticated request.
	{
		req := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/pods", nil)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("unauthenticated status = %d, want 401", rec.Code)
		}
	}

	// 6. Rejected POST without dryRun=All.
	{
		req := httptest.NewRequest(http.MethodPost, "/clusters/in-cluster/api/v1/namespaces/default/configmaps", strings.NewReader(`{}`))
		req.Header.Set("Authorization", "Bearer valid-token")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("POST without dryRun status = %d, want 403", rec.Code)
		}
	}

	// 7. Rejected DELETE pod.
	{
		req := httptest.NewRequest(http.MethodDelete, "/clusters/in-cluster/api/v1/namespaces/default/pods/p1", nil)
		req.Header.Set("Authorization", "Bearer valid-token")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("DELETE pod status = %d, want 403", rec.Code)
		}
	}

	// 8. Rejected pod exec with Upgrade header records resource="pods" and subresource="exec".
	{
		req := httptest.NewRequest(http.MethodPost, "/clusters/in-cluster/api/v1/namespaces/default/pods/p1/exec?command=whoami", nil)
		req.Header.Set("Authorization", "Bearer valid-token")
		req.Header.Set("Connection", "Upgrade")
		req.Header.Set("Upgrade", "SPDY/3.1")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusForbidden {
			t.Fatalf("POST pods/exec status = %d, want 403", rec.Code)
		}
	}

	// 9. Arbitrary custom resource name to verify cardinality bounding.
	{
		req := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/apis/example.com/v1/namespaces/default/CallerSuppliedUnboundedResource42", nil)
		req.Header.Set("Authorization", "Bearer valid-token")
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("GET custom resource status = %d, want 200", rec.Code)
		}
	}

	exposition := scrapeMetricsText(t, metrics)

	expectedLines := []string{
		`kubeagents_kube_proxy_requests_total{dry_run="false",mode="read-only-dry-run",outcome="allowed",resource="healthz",status_code="200",subresource="none",verb="healthz"} 1`,
		`kubeagents_kube_proxy_requests_total{dry_run="false",mode="read-only-dry-run",outcome="allowed",resource="pods",status_code="200",subresource="none",verb="list"} 1`,
		`kubeagents_kube_proxy_requests_total{dry_run="true",mode="read-only-dry-run",outcome="allowed",resource="configmaps",status_code="201",subresource="none",verb="create"} 1`,
		`kubeagents_kube_proxy_requests_total{dry_run="false",mode="read-only-dry-run",outcome="allowed",resource="secrets",status_code="200",subresource="none",verb="get"} 1`,
		`kubeagents_kube_proxy_requests_total{dry_run="false",mode="read-only-dry-run",outcome="allowed",resource="other",status_code="200",subresource="none",verb="list"} 1`,
		`kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",reason="unauthenticated",resource="unknown",subresource="none",verb="get"} 1`,
		`kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",reason="dry_run_required",resource="configmaps",subresource="none",verb="create"} 1`,
		`kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",reason="verb_blocked",resource="pods",subresource="none",verb="delete"} 1`,
		`kubeagents_kube_proxy_rejected_requests_total{mode="read-only-dry-run",reason="subresource_blocked",resource="pods",subresource="exec",verb="create"} 1`,
		`kubeagents_kube_proxy_secrets_redacted_total{action="accept_header_rewritten"} 1`,
		`kubeagents_kube_proxy_secrets_redacted_total{action="payload_scrubbed"} 1`,
		`kubeagents_kube_proxy_in_flight_requests 0`,
		`# TYPE process_start_time_seconds gauge`,
	}

	for _, want := range expectedLines {
		if !strings.Contains(exposition, want) {
			t.Errorf("metrics exposition missing line %q\nFull exposition:\n%s", want, exposition)
		}
	}

	if strings.Contains(exposition, "CallerSuppliedUnboundedResource42") {
		t.Errorf("caller-supplied resource name leaked into Prometheus metrics:\n%s", exposition)
	}
}

// TestMetricsOutcomesThatAreNotTheUpstreams checks the 5xx answers the upstream did not cause:
// each keeps its own outcome rather than counting as upstream_error, and a Secret response
// replaced with a 502 is not counted as scrubbed.
func TestMetricsOutcomesThatAreNotTheUpstreams(t *testing.T) {
	podList := func(_ string, _ *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(`{"apiVersion":"v1","kind":"PodList","items":[]}`)),
		}, nil
	}
	canceled, cancel := context.WithCancel(context.Background())
	cancel()
	for _, tc := range []struct {
		name     string
		authn    CallerAuthenticator
		upstream func(string, *http.Request) (*http.Response, error)
		ctx      context.Context
		target   string
		wantCode int
		want     string
		notWant  string
	}{
		{
			name:     "authentication unavailable",
			authn:    staticAuthenticator{err: fmt.Errorf("%w: %w", errAuthenticationUnavailable, errors.New("connection refused"))},
			upstream: podList,
			target:   "/api/v1/namespaces/default/pods",
			wantCode: http.StatusServiceUnavailable,
			want:     `outcome="auth_unavailable",resource="unknown",status_code="503"`,
		},
		{
			name:  "caller gone",
			authn: staticAuthenticator{caller: &CallerIdentity{Username: "shell"}},
			upstream: func(_ string, req *http.Request) (*http.Response, error) {
				return nil, req.Context().Err()
			},
			ctx:      canceled,
			target:   "/api/v1/namespaces/default/pods",
			wantCode: http.StatusBadGateway,
			want:     `outcome="canceled",resource="pods",status_code="502"`,
		},
		{
			name:     "unredactable secret",
			authn:    staticAuthenticator{caller: &CallerIdentity{Username: "shell"}},
			upstream: podList,
			target:   "/api/v1/namespaces/default/secrets/db",
			wantCode: http.StatusBadGateway,
			want:     `outcome="upstream_error",resource="secrets",status_code="502"`,
			notWant:  `kubeagents_kube_proxy_secrets_redacted_total{action="payload_scrubbed"} 1`,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			metrics := NewMetrics()
			h, err := NewHandler(Config{
				Authenticator:    tc.authn,
				Upstream:         &fakeUpstream{handler: tc.upstream},
				DefaultClusterID: InClusterID,
				Policy:           Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true},
				Logger:           slog.New(slog.DiscardHandler),
				Metrics:          metrics,
			})
			if err != nil {
				t.Fatalf("NewHandler: %v", err)
			}
			req := httptest.NewRequest(http.MethodGet, tc.target, nil)
			if tc.ctx != nil {
				req = req.WithContext(tc.ctx)
			}
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, req)
			if rec.Code != tc.wantCode {
				t.Fatalf("status = %d, want %d", rec.Code, tc.wantCode)
			}
			exposition := scrapeMetricsText(t, metrics)
			if !strings.Contains(exposition, tc.want) {
				t.Errorf("metrics exposition lacks %q\n%s", tc.want, exposition)
			}
			if tc.notWant != "" && strings.Contains(exposition, tc.notWant) {
				t.Errorf("metrics exposition has %q\n%s", tc.notWant, exposition)
			}
		})
	}
}

func TestOpenTelemetryTracing(t *testing.T) {
	exporter := &recordingSpanExporter{}
	tp := sdktrace.NewTracerProvider(sdktrace.WithSyncer(exporter))
	defer func() { _ = tp.Shutdown(context.Background()) }()

	upstream := &fakeUpstream{
		handler: func(_ string, _ *http.Request) (*http.Response, error) {
			return &http.Response{
				StatusCode: http.StatusOK,
				Header:     http.Header{"Content-Type": []string{"application/json"}},
				Body:       io.NopCloser(bytes.NewBufferString(`{"kind":"PodList","items":[]}`)),
			}, nil
		},
	}

	h, err := NewHandler(Config{
		Authenticator:  tokenAuthenticator{expected: "valid-token"},
		Upstream:       upstream,
		Policy:         Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true},
		Logger:         slog.New(slog.DiscardHandler),
		TracerProvider: tp,
	})
	if err != nil {
		t.Fatalf("NewHandler failed: %v", err)
	}

	// 1. Allowed request produces both server span and upstream client span.
	req := httptest.NewRequest(http.MethodGet, "/clusters/gke_my-proj_us-central1_my-cluster/api/v1/namespaces/kube-system/pods", nil)
	req.Header.Set("Authorization", "Bearer valid-token")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET pods status = %d, want 200", rec.Code)
	}

	// 2. Rejected request produces error server span with rejection_reason.
	delReq := httptest.NewRequest(http.MethodDelete, "/clusters/in-cluster/api/v1/namespaces/default/pods/victim", nil)
	delReq.Header.Set("Authorization", "Bearer valid-token")
	delRec := httptest.NewRecorder()
	h.ServeHTTP(delRec, delReq)
	if delRec.Code != http.StatusForbidden {
		t.Fatalf("DELETE pod status = %d, want 403", delRec.Code)
	}

	spans := exporter.snapshot()
	if len(spans) != 3 {
		t.Fatalf("recorded %d spans, want 3 (1 upstream client + 1 allowed server + 1 rejected server)", len(spans))
	}

	var foundUpstream, foundAllowedServer, foundRejectedServer bool
	for _, s := range spans {
		attrs := make(map[attribute.Key]attribute.Value)
		for _, kv := range s.Attributes() {
			attrs[kv.Key] = kv.Value
		}
		switch s.Name() {
		case "k8s.api.upstream":
			foundUpstream = true
			if got := attrs["k8s.cluster.id"].AsString(); got != "gke_my-proj_us-central1_my-cluster" {
				t.Errorf("upstream span cluster.id = %q, want gke_my-proj_us-central1_my-cluster", got)
			}
		case "k8s.api.proxy list pods":
			foundAllowedServer = true
			if got := attrs["kube_proxy.outcome"].AsString(); got != OutcomeAllowed {
				t.Errorf("allowed server span outcome = %q, want %q", got, OutcomeAllowed)
			}
			if got := attrs["k8s.namespace"].AsString(); got != "kube-system" {
				t.Errorf("allowed server span namespace = %q, want kube-system", got)
			}
		case "k8s.api.proxy delete pods":
			foundRejectedServer = true
			if s.Status().Code != codes.Error {
				t.Errorf("rejected server span status = %v, want Error", s.Status().Code)
			}
			if got := attrs["kube_proxy.rejection_reason"].AsString(); got != string(ReasonVerbBlocked) {
				t.Errorf("rejected server span rejection_reason = %q, want %q", got, ReasonVerbBlocked)
			}
		}
	}

	if !foundUpstream || !foundAllowedServer || !foundRejectedServer {
		t.Errorf("missing expected spans: foundUpstream=%v foundAllowedServer=%v foundRejectedServer=%v", foundUpstream, foundAllowedServer, foundRejectedServer)
	}
}

func TestMetricsServerLifecycle(t *testing.T) {
	m := NewMetrics()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("net.Listen: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port

	srv, err := NewMetricsServer(port, m, nil)
	if err != nil {
		t.Fatalf("NewMetricsServer: %v", err)
	}
	srv.listener = ln

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	done := make(chan error, 1)
	go func() {
		done <- srv.Run(ctx)
	}()

	resp, err := http.Get("http://" + ln.Addr().String() + MetricsPath)
	if err != nil {
		t.Fatalf("GET /metrics failed: %v", err)
	}
	body, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("GET /metrics status = %d, want 200", resp.StatusCode)
	}
	if !strings.Contains(string(body), "process_start_time_seconds") {
		t.Errorf("/metrics missing process_start_time_seconds:\n%s", string(body))
	}

	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("MetricsServer.Run returned error: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("MetricsServer.Run did not shut down within 3s")
	}
}

func TestSetupOTelDisabledOrUnset(t *testing.T) {
	t.Setenv("OTEL_SDK_DISABLED", "true")
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
	shutdown, tp, err := SetupOTel(context.Background())
	if err != nil {
		t.Fatalf("SetupOTel when disabled returned error: %v", err)
	}
	if tp == nil {
		t.Fatal("expected non-nil TracerProvider")
	}
	_ = shutdown(context.Background())

	t.Setenv("OTEL_SDK_DISABLED", "false")
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	t.Setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "")
	shutdown2, tp2, err := SetupOTel(context.Background())
	if err != nil {
		t.Fatalf("SetupOTel when endpoint empty returned error: %v", err)
	}
	if tp2 == nil {
		t.Fatal("expected non-nil TracerProvider")
	}
	_ = shutdown2(context.Background())
}
