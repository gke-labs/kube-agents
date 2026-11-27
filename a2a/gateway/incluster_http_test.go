package gateway

import (
	"context"
	"encoding/pem"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"k8s.io/client-go/rest"
)

// envGatewayProxyChild marks the re-executed test binary that runs with
// HTTP_PROXY and HTTPS_PROXY set from process startup: Go's
// http.ProxyFromEnvironment caches proxy variables behind a package-level
// sync.Once, so t.Setenv inside a shared test process is ignored once any
// earlier test has used http.DefaultTransport.
const envGatewayProxyChild = "GATEWAY_TEST_PROXY_CHILD_URL"

func runWithProxyEnv(t *testing.T, testName string, childFn func(proxyURL string)) {
	t.Helper()
	if proxyURL := os.Getenv(envGatewayProxyChild); proxyURL != "" {
		childFn(proxyURL)
		return
	}
	var proxyHits atomic.Int32
	proxySrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		proxyHits.Add(1)
		http.Error(w, "in-cluster request must not route through proxy", http.StatusBadGateway)
	}))
	defer proxySrv.Close()

	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, exe, "-test.run=^"+testName+"$", "-test.count=1") // #nosec G204 -- re-execs this test binary
	env := make([]string, 0, len(os.Environ())+7)
	for _, kv := range os.Environ() {
		key, _, _ := strings.Cut(kv, "=")
		switch strings.ToUpper(key) {
		case "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", envGatewayProxyChild:
			continue
		}
		env = append(env, kv)
	}
	cmd.Env = append(env,
		envGatewayProxyChild+"="+proxySrv.URL,
		"HTTP_PROXY="+proxySrv.URL,
		"HTTPS_PROXY="+proxySrv.URL,
		"http_proxy="+proxySrv.URL,
		"https_proxy="+proxySrv.URL,
		"NO_PROXY=",
		"no_proxy=",
	)
	out, runErr := cmd.CombinedOutput()
	if got := proxyHits.Load(); got != 0 {
		t.Fatalf("trap proxy received %d request(s), want 0\nchild output:\n%s", got, out)
	}
	if runErr != nil {
		t.Fatalf("child test %s failed: %v\n%s", testName, runErr, out)
	}
}

func assertEnvProxyActive(t *testing.T, targetURL, wantProxyURL string) {
	t.Helper()
	probeReq, err := http.NewRequestWithContext(context.Background(), http.MethodGet, targetURL, nil)
	if err != nil {
		t.Fatalf("NewRequest(%q): %v", targetURL, err)
	}
	gotProxy, err := http.ProxyFromEnvironment(probeReq)
	if err != nil {
		t.Fatalf("http.ProxyFromEnvironment(%q): %v", targetURL, err)
	}
	if gotProxy == nil || gotProxy.String() != wantProxyURL {
		t.Fatalf("http.ProxyFromEnvironment(%q) = %v, want %q (envProxyOnce must see the trap proxy)", targetURL, gotProxy, wantProxyURL)
	}
}

func TestInClusterRESTConfigBypassesHTTPSProxy(t *testing.T) {
	runWithProxyEnv(t, "TestInClusterRESTConfigBypassesHTTPSProxy", func(proxyURL string) {
		// Use example.com (in httptest.NewTLSServer's default cert SANs and non-loopback
		// so http.ProxyFromEnvironment routes it through HTTPS_PROXY unless bypassed).
		const apiURL = "https://example.com:443/version"
		assertEnvProxyActive(t, apiURL, proxyURL)

		var apiHits atomic.Int32
		apiSrv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			apiHits.Add(1)
			w.WriteHeader(http.StatusOK)
			_, _ = io.WriteString(w, `{"ok":true}`)
		}))
		defer apiSrv.Close()

		caPEM := pem.EncodeToMemory(&pem.Block{
			Type:  "CERTIFICATE",
			Bytes: apiSrv.Certificate().Raw,
		})
		apiAddr := apiSrv.Listener.Addr().String()
		dialer := &net.Dialer{Timeout: 5 * time.Second}

		rc := disableRESTConfigProxy(&rest.Config{
			Host: "https://example.com:443",
			TLSClientConfig: rest.TLSClientConfig{
				CAData: caPEM,
			},
			Dial: func(ctx context.Context, network, addr string) (net.Conn, error) {
				if addr == "example.com:443" {
					return dialer.DialContext(ctx, network, apiAddr)
				}
				return dialer.DialContext(ctx, network, addr)
			},
		})
		if rc.Proxy == nil {
			t.Fatal("rc.Proxy == nil; client-go defaults nil Proxy to http.ProxyFromEnvironment")
		}
		req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, apiURL, nil)
		if err != nil {
			t.Fatalf("NewRequest: %v", err)
		}
		if p, err := rc.Proxy(req); err != nil || p != nil {
			t.Fatalf("rc.Proxy(req) = (%v, %v), want (nil, nil)", p, err)
		}

		rt, err := rest.TransportFor(rc)
		if err != nil {
			t.Fatalf("rest.TransportFor: %v", err)
		}
		resp, err := (&http.Client{Transport: rt, Timeout: 5 * time.Second}).Do(req)
		if err != nil {
			t.Fatalf("in-cluster Kubernetes request failed under HTTPS_PROXY: %v", err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("status = %d, want 200", resp.StatusCode)
		}
		if got := apiHits.Load(); got != 1 {
			t.Fatalf("api server received %d request(s), want 1", got)
		}
	})
}
