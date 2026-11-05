package k8sapiproxy

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type staticAuthenticator struct {
	caller *CallerIdentity
	err    error
}

func (a staticAuthenticator) AuthenticateRequest(*http.Request) (*CallerIdentity, error) {
	return a.caller, a.err
}

var shellCaller = staticAuthenticator{caller: &CallerIdentity{Username: "system:serviceaccount:agents:shell"}}

type recordingTransport struct {
	calls     int
	clusterID string
	req       *http.Request
	body      string
	err       error
	// response, when set, builds the upstream response instead of an empty PodList.
	response func(req *http.Request) *http.Response
}

func (rt *recordingTransport) RoundTripCluster(_ context.Context, clusterID string, req *http.Request) (*http.Response, error) {
	rt.calls++
	rt.clusterID = clusterID
	rt.req = req.Clone(context.Background())
	if req.Body != nil {
		b, _ := io.ReadAll(req.Body)
		rt.body = string(b)
	}
	if rt.err != nil {
		return nil, rt.err
	}
	if rt.response != nil {
		return rt.response(req), nil
	}
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{contentTypeJSON}},
		Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","items":[]}`)),
		Request:    req,
	}, nil
}

func newTestHandler(t *testing.T, cfg Config) (http.Handler, *recordingTransport) {
	t.Helper()
	upstream := &recordingTransport{}
	if cfg.Authenticator == nil {
		cfg.Authenticator = shellCaller
	}
	if cfg.Upstream == nil {
		cfg.Upstream = upstream
	}
	if cfg.Policy.Mode == "" {
		cfg.Policy = Policy{Mode: ModeReadOnlyWithDryRun, RedactSecrets: true}
	}
	if cfg.Logger == nil {
		cfg.Logger = slog.New(slog.DiscardHandler)
	}
	h, err := NewHandler(cfg)
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	return h, upstream
}

func serve(h http.Handler, req *http.Request) *httptest.ResponseRecorder {
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decodeStatus(t *testing.T, rec *httptest.ResponseRecorder) metav1.Status {
	t.Helper()
	if ct := rec.Header().Get(headerContentType); ct != contentTypeJSON {
		t.Errorf("Content-Type = %q, want %q", ct, contentTypeJSON)
	}
	var s metav1.Status
	if err := json.Unmarshal(rec.Body.Bytes(), &s); err != nil {
		t.Fatalf("body is not a Status: %v: %s", err, rec.Body.String())
	}
	if s.Kind != statusKind || s.APIVersion != statusAPIVersion || s.Status != metav1.StatusFailure {
		t.Errorf("Status envelope = %+v", s)
	}
	if int(s.Code) != rec.Code {
		t.Errorf("Status.Code = %d, response code %d", s.Code, rec.Code)
	}
	return s
}

func TestNewHandlerValidatesConfig(t *testing.T) {
	ok := Config{Authenticator: shellCaller, Upstream: &recordingTransport{}, Policy: Policy{Mode: ModeReadOnly}}
	if _, err := NewHandler(ok); err != nil {
		t.Fatalf("NewHandler(valid) = %v", err)
	}
	for name, mutate := range map[string]func(*Config){
		"no authenticator":      func(c *Config) { c.Authenticator = nil },
		"no upstream":           func(c *Config) { c.Upstream = nil },
		"no mode":               func(c *Config) { c.Policy.Mode = "" },
		"unknown mode":          func(c *Config) { c.Policy.Mode = "READ-ONLY" },
		"invalid default ID":    func(c *Config) { c.DefaultClusterID = "bad$cluster" },
		"upper-case default ID": func(c *Config) { c.DefaultClusterID = "kind-Dev" },
	} {
		cfg := ok
		mutate(&cfg)
		if _, err := NewHandler(cfg); err == nil {
			t.Errorf("%s: NewHandler accepted the config", name)
		}
	}
}

func TestHandlerAuthentication(t *testing.T) {
	for name, auth := range map[string]staticAuthenticator{
		"error":     {err: errors.New("token expired")},
		"no caller": {},
	} {
		t.Run(name, func(t *testing.T) {
			h, upstream := newTestHandler(t, Config{Authenticator: auth})
			rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil))
			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("code = %d, want 401", rec.Code)
			}
			if s := decodeStatus(t, rec); s.Reason != metav1.StatusReasonUnauthorized || s.Message != authFailedMessage {
				t.Errorf("Status = %q %q, want the fixed message", s.Reason, s.Message)
			}
			if upstream.calls != 0 {
				t.Error("an unauthenticated request reached the upstream")
			}
		})
	}
}

func TestHandlerRoutes(t *testing.T) {
	tests := []struct {
		name, defaultID, target, wantCluster, wantPath string
	}{
		{"prefix", gkeHost, "/clusters/" + gkeFleet + "/api/v1/namespaces/kube-system/pods?limit=5", gkeFleet, "/api/v1/namespaces/kube-system/pods"},
		{"default cluster", gkeHost, "/apis/apps/v1/deployments?limit=5", gkeHost, "/apis/apps/v1/deployments"},
		{"no default", "", "/api/v1/pods?limit=5", InClusterID, "/api/v1/pods"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			h, upstream := newTestHandler(t, Config{DefaultClusterID: tc.defaultID})
			rec := serve(h, httptest.NewRequest(http.MethodGet, tc.target, nil))
			if rec.Code != http.StatusOK {
				t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
			}
			if upstream.clusterID != tc.wantCluster {
				t.Errorf("cluster = %q, want %q", upstream.clusterID, tc.wantCluster)
			}
			if upstream.req.URL.Path != tc.wantPath || upstream.req.URL.RawQuery != "limit=5" {
				t.Errorf("upstream URL = %q, want %q?limit=5", upstream.req.URL.String(), tc.wantPath)
			}
			if upstream.req.Host != "" {
				t.Errorf("upstream Host = %q, want it left to the transport", upstream.req.Host)
			}
		})
	}
}

func TestHandlerRefusals(t *testing.T) {
	tests := []struct {
		name, method, target string
		wantCode             int
		wantReason           metav1.StatusReason
	}{
		{"invalid path", http.MethodGet, "/api/v1/namespaces/%2e%2e/secrets", http.StatusBadRequest, metav1.StatusReasonBadRequest},
		{"invalid cluster", http.MethodGet, "/clusters/bad$id/api/v1/pods", http.StatusBadRequest, metav1.StatusReasonBadRequest},
		{"write without dry run", http.MethodPost, "/api/v1/namespaces/default/configmaps", http.StatusForbidden, metav1.StatusReasonForbidden},
		{"exec", http.MethodPost, "/api/v1/namespaces/default/pods/p/exec?command=sh", http.StatusForbidden, metav1.StatusReasonForbidden},
		{"non-discovery path", http.MethodGet, "/metrics", http.StatusForbidden, metav1.StatusReasonForbidden},
		{"unclassifiable path", http.MethodGet, "/api/v1/watch", http.StatusBadRequest, metav1.StatusReasonBadRequest},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			h, upstream := newTestHandler(t, Config{})
			rec := serve(h, httptest.NewRequest(tc.method, tc.target, nil))
			if rec.Code != tc.wantCode {
				t.Fatalf("code = %d, want %d: %s", rec.Code, tc.wantCode, rec.Body.String())
			}
			s := decodeStatus(t, rec)
			if s.Reason != tc.wantReason || !strings.HasPrefix(s.Message, "kube-proxy ") {
				t.Errorf("Status = %q %q", s.Reason, s.Message)
			}
			if upstream.calls != 0 {
				t.Error("a refused request reached the upstream")
			}
		})
	}
}

func TestHandlerDropsAbsoluteURLAuthority(t *testing.T) {
	h, upstream := newTestHandler(t, Config{})
	rec := serve(h, httptest.NewRequest(http.MethodGet, "http://user:pw@attacker.example/api/v1/pods?limit=5", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	u := upstream.req.URL
	if u.Scheme != "" || u.Host != "" || u.User != nil || u.Path != "/api/v1/pods" || u.RawQuery != "limit=5" {
		t.Errorf("upstream URL = %#v, want only the path and query", u)
	}
}

func TestHandlerForwardsDryRunWrite(t *testing.T) {
	h, upstream := newTestHandler(t, Config{})
	const body = `{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"c"}}`
	rec := serve(h, httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/configmaps?dryRun=All", strings.NewReader(body)))
	if rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	if upstream.req.Method != http.MethodPost || upstream.req.URL.RawQuery != "dryRun=All" || upstream.body != body {
		t.Errorf("upstream got %s %s with body %q", upstream.req.Method, upstream.req.URL, upstream.body)
	}
}

func TestHandlerStripsCallerHeaders(t *testing.T) {
	h, upstream := newTestHandler(t, Config{})
	req := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods", nil)
	for k, v := range map[string]string{
		"Authorization":          "Bearer sandbox-token",
		"Proxy-Authorization":    "Basic dXNlcjpwYXNz",
		"Cookie":                 "session=s",
		"Audit-ID":               "forged",
		"Sec-WebSocket-Protocol": "base64url.bearer.authorization.k8s.io.dG9rZW4, v5.channel.k8s.io",
		"Impersonate-User":       "cluster-admin",
		"Impersonate-Group":      "system:masters",
		"Impersonate-Uid":        "1",
		"Impersonate-Extra-Foo":  "bar",
		"X-Remote-User":          "admin",
		"X-Remote-Group":         "system:masters",
		"Forwarded":              "for=10.0.0.1",
		"X-Forwarded-For":        "10.0.0.1",
		"X-Forwarded-Host":       "attacker.example",
		"X-Forwarded-Proto":      "https",
		"X-Real-Ip":              "10.0.0.2",
		"Connection":             "X-Hop",
		"X-Hop":                  "1",
		"User-Agent":             "kubectl/v1.37.0",
		"Accept":                 contentTypeJSON,
	} {
		req.Header.Set(k, v)
	}
	if rec := serve(h, req); rec.Code != http.StatusOK {
		t.Fatalf("code = %d: %s", rec.Code, rec.Body.String())
	}
	// Only these reach the upstream, so a header added above without a decision fails here.
	want := map[string]bool{"User-Agent": true, "Accept": true, "Sec-Websocket-Protocol": true}
	for k := range upstream.req.Header {
		if !want[k] {
			t.Errorf("%s reached the upstream: %q", k, upstream.req.Header.Values(k))
		}
	}
	for k := range want {
		if upstream.req.Header.Get(k) == "" {
			t.Errorf("%s was dropped", k)
		}
	}
	if got := upstream.req.Header.Get("Sec-WebSocket-Protocol"); got != "v5.channel.k8s.io" {
		t.Errorf("upstream Sec-WebSocket-Protocol = %q, want the bearer entry dropped", got)
	}
}

func TestHandlerUpgrade(t *testing.T) {
	const target = "/api/v1/namespaces/default/pods/p/exec?command=sh"
	newReq := func() *http.Request {
		req := httptest.NewRequest(http.MethodPost, target, nil)
		req.Header.Set("Upgrade", "SPDY/3.1")
		req.Header.Set("Connection", "Upgrade")
		return req
	}

	h, upstream := newTestHandler(t, Config{Policy: Policy{Mode: ModeReadWrite, AllowUpgradeSubresources: true}})
	if rec := serve(h, newReq()); rec.Code != http.StatusOK {
		t.Fatalf("opted-in exec: code = %d: %s", rec.Code, rec.Body.String())
	}
	if got := upstream.req.Header.Get("Upgrade"); got != "SPDY/3.1" {
		t.Errorf("upstream Upgrade = %q, want SPDY/3.1", got)
	}
	if got := upstream.req.Header.Get("Connection"); !strings.EqualFold(got, "upgrade") {
		t.Errorf("upstream Connection = %q, want Upgrade", got)
	}

	h, upstream = newTestHandler(t, Config{Policy: Policy{Mode: ModeReadWrite}})
	if rec := serve(h, newReq()); rec.Code != http.StatusForbidden {
		t.Errorf("exec without the opt-in: code = %d, want 403", rec.Code)
	}
	if upstream.calls != 0 {
		t.Error("a refused upgrade reached the upstream")
	}
}

func TestHandlerUpstreamError(t *testing.T) {
	upstream := &recordingTransport{err: errors.New("dial tcp: i/o timeout")}
	h, _ := newTestHandler(t, Config{Upstream: upstream, DefaultClusterID: gkeHost})
	rec := serve(h, httptest.NewRequest(http.MethodGet, "/api/v1/pods", nil))
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("code = %d, want 502", rec.Code)
	}
	if s := decodeStatus(t, rec); !strings.Contains(s.Message, gkeHost) || strings.Contains(s.Message, "i/o timeout") {
		t.Errorf("message = %q, want the cluster without the transport error", s.Message)
	}
}

func TestSanitizeRequestHeaders(t *testing.T) {
	tests := []struct {
		name            string
		in              http.Header
		preserveUpgrade bool
		want            http.Header
	}{
		{
			name: "drops what Connection names, then Connection",
			in:   http.Header{"Connection": {"X-A, x-b", "X-C"}, "X-A": {"1"}, "X-B": {"1"}, "X-C": {"1"}, "Accept": {"a"}},
			want: http.Header{"Accept": {"a"}},
		},
		{
			name: "drops Upgrade by default",
			in:   http.Header{"Connection": {"Upgrade"}, "Upgrade": {"websocket"}},
			want: http.Header{},
		},
		{
			name:            "keeps an admitted upgrade and drops the rest Connection names",
			in:              http.Header{"Connection": {"Upgrade, X-A"}, "Upgrade": {"websocket"}, "X-A": {"1"}},
			preserveUpgrade: true,
			want:            http.Header{"Connection": {"Upgrade"}, "Upgrade": {"websocket"}},
		},
		{
			name:            "no Connection: Upgrade without an Upgrade header",
			in:              http.Header{"Connection": {"Upgrade"}},
			preserveUpgrade: true,
			want:            http.Header{},
		},
		{
			name: "drops bearer subprotocols and keeps the rest",
			in: http.Header{"Sec-WebSocket-Protocol": {
				"Base64url.Bearer.Authorization.K8s.Io.dG9rZW4, v5.channel.k8s.io",
				"base64url.bearer.authorization.k8s.io.eA, channel.k8s.io",
			}},
			want: http.Header{"Sec-WebSocket-Protocol": {"v5.channel.k8s.io, channel.k8s.io"}},
		},
		{
			name: "drops a header of only bearer subprotocols",
			in:   http.Header{"Sec-WebSocket-Protocol": {"base64url.bearer.authorization.k8s.io.dG9rZW4"}},
			want: http.Header{},
		},
		{
			name: "drops credentials and identity headers in any case",
			in:   http.Header{"Authorization": {"a"}, "IMPERSONATE-USER": {"u"}, "x-remote-extra-scopes": {"s"}, "User-Agent": {"k"}},
			want: http.Header{"User-Agent": {"k"}},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			sanitizeRequestHeaders(tc.in, tc.preserveUpgrade)
			if len(tc.in) != len(tc.want) {
				t.Fatalf("headers = %v, want %v", tc.in, tc.want)
			}
			for k, v := range tc.want {
				if got := strings.Join(tc.in.Values(k), ","); got != strings.Join(v, ",") {
					t.Errorf("%s = %q, want %q", k, got, strings.Join(v, ","))
				}
			}
		})
	}
}

func TestWriteStatusClampsCode(t *testing.T) {
	rec := httptest.NewRecorder()
	writeStatus(rec, 0, metav1.StatusReasonInternalError, "m")
	if rec.Code != http.StatusInternalServerError {
		t.Errorf("code = %d, want 500", rec.Code)
	}
	decodeStatus(t, rec)
}
