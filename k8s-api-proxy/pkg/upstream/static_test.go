package upstream

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

func TestNewStaticRejectsInvalidURLsAndClusterIDs(t *testing.T) {
	bad := []string{
		"",
		"   ",
		"127.0.0.1:8080",
		"ftp://127.0.0.1:8080",
		"http://",
		"http://:8080",
		"https://:8443/k8s",
		"https://user:secret@127.0.0.1:8443",
		"https://user@127.0.0.1:8443",
		"http://127.0.0.1:8080?x=1",
		"http://127.0.0.1:8080?",
		"http://127.0.0.1:8080#frag",
		"http://127.0.0.1:8080/a//b",
		"http://127.0.0.1:8080/a/../b",
		"http://127.0.0.1:8080/a/./b",
		"http://127.0.0.1:8080/a/%2e%2e/b",
		"http://127.0.0.1:8080/a/%2f/b",
		"http://127.0.0.1:8080/a\\b",
		"http://127.0.0.1:8080/a\x00b",
	}
	for _, raw := range bad {
		if _, err := NewStatic(StaticConfig{URL: raw}); err == nil {
			t.Errorf("NewStatic(%q) succeeded, want an error", raw)
		}
	}
	if _, err := NewStatic(StaticConfig{URL: "http://127.0.0.1:8080", ClusterID: "bad$cluster"}); err == nil {
		t.Error("NewStatic accepted an invalid ClusterID")
	}
}

func TestStaticTransportForwardsHTTPAndJoinsBasePath(t *testing.T) {
	var (
		mu      sync.Mutex
		gotURI  string
		gotAuth string
		gotHost string
		srvHost string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		gotURI = r.URL.RequestURI()
		gotAuth = r.Header.Get("Authorization")
		gotHost = r.Host
		mu.Unlock()
		_, _ = io.WriteString(w, `{"kind":"PodList","apiVersion":"v1","items":[]}`)
	}))
	t.Cleanup(srv.Close)
	u, _ := url.Parse(srv.URL)
	srvHost = u.Host

	for _, tc := range []struct {
		name     string
		suffix   string
		reqPath  string
		rawPath  string
		rawQuery string
		wantURI  string
	}{
		{
			name:     "no base path",
			suffix:   "",
			reqPath:  "/api/v1/namespaces/default/pods",
			rawQuery: "limit=5",
			wantURI:  "/api/v1/namespaces/default/pods?limit=5",
		},
		{
			name:    "slash base path",
			suffix:  "/",
			reqPath: "/version",
			wantURI: "/version",
		},
		{
			name:     "prefix without trailing slash",
			suffix:   "/k8s/bridge",
			reqPath:  "/api/v1/pods",
			rawQuery: "watch=false",
			wantURI:  "/k8s/bridge/api/v1/pods?watch=false",
		},
		{
			name:    "prefix with trailing slash",
			suffix:  "/k8s/bridge/",
			reqPath: "/api/v1/pods",
			wantURI: "/k8s/bridge/api/v1/pods",
		},
		{
			name:    "prefix with root request path",
			suffix:  "/k8s",
			reqPath: "/",
			wantURI: "/k8s/",
		},
		{
			name:    "preserves non-canonical RawPath encoding",
			suffix:  "/k8s",
			reqPath: "/api/v1/namespaces/default/pods/a:b",
			rawPath: "/api/v1/namespaces/default/pods/a%3Ab",
			wantURI: "/k8s/api/v1/namespaces/default/pods/a%3Ab",
		},
		{
			name:    "preserves escaped base path when reqPath equals baseEscapedPath",
			suffix:  "/a%3Ab",
			reqPath: "/a%3Ab",
			wantURI: "/a%3Ab/a%253Ab",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			st, err := NewStatic(StaticConfig{URL: srv.URL + tc.suffix})
			if err != nil {
				t.Fatalf("NewStatic: %v", err)
			}
			req := httptest.NewRequest(http.MethodGet, "http://caller.invalid/ignored", nil)
			req.URL.Path = tc.reqPath
			req.URL.RawPath = tc.rawPath
			req.URL.RawQuery = tc.rawQuery
			req.Host = "attacker.example"
			req.Header["authorization"] = []string{"Bearer caller-secret"}
			req.Header.Set("Authorization", "Bearer caller-secret-2")

			resp, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, req)
			if err != nil {
				t.Fatalf("RoundTripCluster: %v", err)
			}
			_ = resp.Body.Close()

			mu.Lock()
			uri, auth, host := gotURI, gotAuth, gotHost
			mu.Unlock()
			if uri != tc.wantURI {
				t.Errorf("upstream URI = %q, want %q", uri, tc.wantURI)
			}
			if auth != "" {
				t.Errorf("caller Authorization leaked to upstream: %q", auth)
			}
			if host != srvHost {
				t.Errorf("upstream Host = %q, want %q", host, srvHost)
			}
		})
	}
}

func TestStaticTransportRefusesOtherClusterIDsBadInputsAndRedirects(t *testing.T) {
	var redirected int
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		redirected++
	}))
	t.Cleanup(target.Close)

	var upstreamCalls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalls++
		if r.URL.Path == "/prefix/not-modified" {
			w.WriteHeader(http.StatusNotModified)
			return
		}
		http.Redirect(w, r, target.URL+"/leaked", http.StatusFound)
	}))
	t.Cleanup(srv.Close)

	st, err := NewStatic(StaticConfig{URL: srv.URL + "/prefix"})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}

	for _, wrongCluster := range []string{"", "kind-other", testClusterID} {
		if _, err := st.RoundTripCluster(context.Background(), wrongCluster, httptest.NewRequest(http.MethodGet, "/api", nil)); err == nil {
			t.Errorf("accepted non-matching clusterID %q", wrongCluster)
		}
	}
	if upstreamCalls != 0 {
		t.Errorf("wrong-cluster requests reached upstream (%d calls)", upstreamCalls)
	}
	if _, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, nil); err == nil {
		t.Error("accepted nil request")
	}
	for _, badPath := range []string{"relative", "//evil.example/api", "/api/../secret", "/api/%2e%2e/secret", "/api/%2fsecret"} {
		req := httptest.NewRequest(http.MethodGet, "/api", nil)
		req.URL.Path = badPath
		if _, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, req); err == nil {
			t.Errorf("RoundTripCluster accepted unsafe path %q", badPath)
		}
	}

	// Upstream 302 is neither followed nor relayed to the caller.
	if resp, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, httptest.NewRequest(http.MethodGet, "/api", nil)); err == nil {
		_ = resp.Body.Close()
		t.Fatal("RoundTripCluster relayed a 302 redirect")
	}
	if redirected != 0 {
		t.Errorf("redirect target received %d requests, want 0", redirected)
	}

	// 304 Not Modified is allowed through.
	resp, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, httptest.NewRequest(http.MethodGet, "/not-modified", nil))
	if err != nil || resp.StatusCode != http.StatusNotModified {
		t.Fatalf("304 response = %v, %v; want 304", resp, err)
	}
	_ = resp.Body.Close()
}

func TestStaticTransportTLSAndHTTP1Upgrade(t *testing.T) {
	srv := newFakeAPIServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Upgrade") == "" {
			_, _ = io.WriteString(w, "ok")
			return
		}
		conn, rw, err := http.NewResponseController(w).Hijack()
		if err != nil {
			http.Error(w, err.Error(), http.StatusHTTPVersionNotSupported)
			return
		}
		defer func() { _ = conn.Close() }()
		_, _ = rw.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: SPDY/3.1\r\n\r\n")
		_ = rw.Flush()
		line, _ := rw.ReadString('\n')
		_, _ = rw.WriteString("echo " + line)
		_ = rw.Flush()
	})

	// Without trusting srv's CA, TLS verification rejects the server.
	untrusted, err := NewStatic(StaticConfig{URL: srv.URL})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if _, err := untrusted.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, httptest.NewRequest(http.MethodGet, "/api", nil)); err == nil {
		t.Fatal("trusted a self-signed certificate without RootCAs")
	}

	roots := x509.NewCertPool()
	roots.AddCert(srv.Certificate())
	st, err := NewStatic(StaticConfig{
		URL:             srv.URL,
		TLSClientConfig: &tls.Config{RootCAs: roots},
	})
	if err != nil {
		t.Fatalf("NewStatic(trusted): %v", err)
	}

	// Pool an HTTP/2 connection first, then verify upgrade still uses HTTP/1.1 and returns io.ReadWriteCloser.
	resp1, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, httptest.NewRequest(http.MethodGet, "/api", nil))
	if err != nil {
		t.Fatalf("HTTP/2 request failed: %v", err)
	}
	_, _ = io.Copy(io.Discard, resp1.Body)
	_ = resp1.Body.Close()

	upReq := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods/p/exec", nil)
	upReq.Header.Set("Connection", "Upgrade")
	upReq.Header.Set("Upgrade", "SPDY/3.1")
	resp2, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, upReq)
	if err != nil {
		t.Fatalf("upgrade failed: %v", err)
	}
	defer func() { _ = resp2.Body.Close() }()
	if resp2.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status = %d, want 101", resp2.StatusCode)
	}
	stream, ok := resp2.Body.(io.ReadWriteCloser)
	if !ok {
		t.Fatalf("101 body is %T, want io.ReadWriteCloser", resp2.Body)
	}
	if _, err := io.WriteString(stream, "ping\n"); err != nil {
		t.Fatalf("write: %v", err)
	}
	buf := make([]byte, len("echo ping\n"))
	if _, err := io.ReadFull(stream, buf); err != nil || string(buf) != "echo ping\n" {
		t.Errorf("read %q, %v", string(buf), err)
	}

	srv.mu.Lock()
	protos := append([]string(nil), srv.protos...)
	srv.mu.Unlock()
	if len(protos) != 2 || protos[0] != "HTTP/2.0" || protos[1] != "HTTP/1.1" {
		t.Errorf("protocols = %v, want [HTTP/2.0 HTTP/1.1]", protos)
	}
}
