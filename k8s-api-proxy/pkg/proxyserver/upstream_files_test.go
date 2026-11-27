package proxyserver

import (
	"bytes"
	"encoding/pem"
	"errors"
	"flag"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testUpstreamToken = "upstream-file-token"
	// testTokenRereadWait is longer than the second pkg/upstream waits between two reads
	// of the bearer token file.
	testTokenRereadWait = 1500 * time.Millisecond
	testDiscoveryPath   = "/api"
)

// hostEnvVars are what a Kubernetes pod or a Google Cloud login would set; a standalone
// proxy must start without any of them.
var hostEnvVars = []string{
	"HOME", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG", "APPDATA",
	"KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBECONFIG", googleCloudUniverseEnv,
}

// tlsUpstream is an HTTPS API server that records the Authorization header of each request.
type tlsUpstream struct {
	srv    *httptest.Server
	caFile string
	mu     sync.Mutex
	authz  []string
}

func newTLSUpstream(t *testing.T) *tlsUpstream {
	t.Helper()
	u := &tlsUpstream{}
	u.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.mu.Lock()
		u.authz = append(u.authz, r.Header.Get("Authorization"))
		u.mu.Unlock()
		w.Header().Set("Content-Type", testContentJSON)
		_, _ = io.WriteString(w, `{"kind":"PodList","apiVersion":"v1","items":[]}`)
	}))
	t.Cleanup(u.srv.Close)
	u.caFile = filepath.Join(t.TempDir(), "upstream-ca.crt")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: u.srv.Certificate().Raw}
	if err := os.WriteFile(u.caFile, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	return u
}

func (u *tlsUpstream) seen() []string {
	u.mu.Lock()
	defer u.mu.Unlock()
	return append([]string(nil), u.authz...)
}

// standaloneArgs are the flags of a proxy that holds the upstream's credential itself. The
// host cluster's ServiceAccount files are pointed at paths that do not exist, as they do
// not outside a pod.
func standaloneArgs(t *testing.T, u *tlsUpstream, tokenFile string) []string {
	t.Helper()
	absent := filepath.Join(t.TempDir(), "serviceaccount")
	return []string{
		"--upstream-token-file=" + tokenFile,
		"--upstream-ca-file=" + u.caFile,
		"--kube-ca-file=" + filepath.Join(absent, "ca.crt"),
		"--kube-token-file=" + filepath.Join(absent, "token"),
	}
}

func writeTokenFile(t *testing.T, token string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// clearHostEnv removes the variables of a pod and of a Google Cloud login for the test.
func clearHostEnv(t *testing.T) {
	t.Helper()
	clearProxyEnv(t)
	for _, name := range hostEnvVars {
		// Setenv registers the restore; the variable itself is then removed, not emptied.
		t.Setenv(name, "")
		if err := os.Unsetenv(name); err != nil {
			t.Fatal(err)
		}
	}
}

func TestRegisterFlagsReadsUpstreamFileEnvVars(t *testing.T) {
	clearProxyEnv(t)
	t.Setenv(upstreamTokenFileEnv, "/var/run/upstream/token")
	t.Setenv(upstreamCAFileEnv, "/var/run/upstream/ca.crt")

	var cfg proxyConfig
	if err := parseFlags(flag.NewFlagSet("test", flag.ContinueOnError), nil, &cfg); err != nil {
		t.Fatalf("parseFlags: %v", err)
	}
	if cfg.upstreamTokenFile != "/var/run/upstream/token" || cfg.upstreamCAFile != "/var/run/upstream/ca.crt" {
		t.Errorf("upstreamTokenFile = %q, upstreamCAFile = %q; want the environment's values", cfg.upstreamTokenFile, cfg.upstreamCAFile)
	}

	flagged := parseTestFlags(t, "--"+upstreamTokenFileFlag+"=/flag/token", "--"+upstreamCAFileFlag+"=/flag/ca.crt")
	if flagged.upstreamTokenFile != "/flag/token" || flagged.upstreamCAFile != "/flag/ca.crt" {
		t.Errorf("upstreamTokenFile = %q, upstreamCAFile = %q; want the flags' values", flagged.upstreamTokenFile, flagged.upstreamCAFile)
	}
}

func TestBuildHandlerStandaloneStartsWithoutAServiceAccountMountOrGoogleCredentials(t *testing.T) {
	for _, tc := range []struct {
		name string
		// args returns the authentication flags and the caller's bearer token.
		args func(t *testing.T, upstreamURL string) (flags []string, callerToken string)
	}{
		{
			name: "auth-mode none",
			args: func(*testing.T, string) ([]string, string) {
				return []string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443"}, "caller-token"
			},
		},
		{
			name: "auth-mode jwt",
			args: func(t *testing.T, upstreamURL string) ([]string, string) {
				is := newTestJWTIssuer(t)
				return is.jwtArgs(upstreamURL), is.sign("agent-1", testJWTAudience, nil)
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			clearHostEnv(t)
			up := newTLSUpstream(t)
			flags, callerToken := tc.args(t, up.srv.URL)
			args := append(flags, standaloneArgs(t, up, writeTokenFile(t, testUpstreamToken+"\n"))...)
			args = append(args, "--upstream-url="+up.srv.URL, "--cache-max-bytes=0")
			var logs bytes.Buffer
			h := buildTestHandler(t, parseTestFlags(t, args...), slog.New(slog.NewJSONHandler(&logs, nil)))

			rec := getPods(h, callerToken)
			if rec.Code != http.StatusOK {
				t.Fatalf("GET pods: code = %d, body = %s, logs = %s", rec.Code, rec.Body.String(), logs.String())
			}
			// The upstream is sent the file's token, never the caller's own credential.
			if got := up.seen(); len(got) != 1 || got[0] != "Bearer "+testUpstreamToken {
				t.Errorf("upstream saw Authorization %q, want only the token file's", got)
			}
			if strings.Contains(logs.String(), testUpstreamToken) {
				t.Errorf("the logs carry the upstream token: %s", logs.String())
			}
		})
	}
}

func TestBuildHandlerStandaloneRefusesRequestsWhileTheUpstreamTokenFileIsEmpty(t *testing.T) {
	clearHostEnv(t)
	up := newTLSUpstream(t)
	tokenFile := writeTokenFile(t, testUpstreamToken)
	args := append([]string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443", "--upstream-url=" + up.srv.URL, "--cache-max-bytes=0"},
		standaloneArgs(t, up, tokenFile)...)
	var logs bytes.Buffer
	h := buildTestHandler(t, parseTestFlags(t, args...), slog.New(slog.NewJSONHandler(&logs, nil)))
	if rec := getPods(h, ""); rec.Code != http.StatusOK {
		t.Fatalf("GET pods: code = %d, body = %s", rec.Code, rec.Body.String())
	}

	// Emptied in place: the next read of the file finds no token.
	if err := os.WriteFile(tokenFile, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	time.Sleep(testTokenRereadWait)
	rec := getPods(h, "")
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("GET pods with an empty token file: code = %d, want 502; body = %s", rec.Code, rec.Body.String())
	}
	if got := up.seen(); len(got) != 1 {
		t.Errorf("upstream saw %q, want only the first request: nothing is sent without the file's token", got)
	}
	// The audit record names the cause; neither it nor the response carries the token.
	if !strings.Contains(logs.String(), "is empty") || !strings.Contains(logs.String(), tokenFile) {
		t.Errorf("logs = %s, want the refusal to name the empty token file", logs.String())
	}
	if strings.Contains(logs.String(), testUpstreamToken) || strings.Contains(rec.Body.String(), testUpstreamToken) {
		t.Errorf("the token reached the logs or the response: logs = %s, body = %s", logs.String(), rec.Body.String())
	}
}

func TestBuildHandlerStandaloneServesCachedDiscoveryWhileTheUpstreamTokenFileIsEmpty(t *testing.T) {
	clearHostEnv(t)
	up := newTLSUpstream(t)
	tokenFile := writeTokenFile(t, testUpstreamToken)
	// The response cache is left at its defaults: discovery answers are kept for their TTL,
	// resource answers are not kept at all.
	args := append([]string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443", "--upstream-url=" + up.srv.URL},
		standaloneArgs(t, up, tokenFile)...)
	h := buildTestHandler(t, parseTestFlags(t, args...), slog.New(slog.DiscardHandler))

	get := func(path string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		return rec
	}
	if rec := get(testDiscoveryPath); rec.Code != http.StatusOK {
		t.Fatalf("GET %s: code = %d, body = %s", testDiscoveryPath, rec.Code, rec.Body.String())
	}

	if err := os.WriteFile(tokenFile, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	time.Sleep(testTokenRereadWait)

	// An answer fetched with the earlier token is served until it expires.
	cached := get(testDiscoveryPath)
	if cached.Code != http.StatusOK || cached.Header().Get("Cache-Status") != testCacheHit {
		t.Errorf("GET %s with an empty token file: code = %d, Cache-Status = %q; want 200 from the cache",
			testDiscoveryPath, cached.Code, cached.Header().Get("Cache-Status"))
	}
	// A request that has to reach the upstream fails.
	if rec := get(testPodsPath); rec.Code != http.StatusBadGateway {
		t.Errorf("GET %s with an empty token file: code = %d, want 502; body = %s", testPodsPath, rec.Code, rec.Body.String())
	}
	if got := up.seen(); len(got) != 1 {
		t.Errorf("upstream saw %q, want only the request made while the file held a token", got)
	}
}

func TestUpstreamCAReloadErrorWarnsAndCountsTheFailure(t *testing.T) {
	var logs bytes.Buffer
	metrics := k8sapiproxy.NewMetrics()
	upstreamCAReloadError(slog.New(slog.NewJSONHandler(&logs, nil)), metrics)(errors.New("bundle cut off mid-write"))

	for _, want := range []string{`"level":"WARN"`, "bundle cut off mid-write", "--" + upstreamCAFileFlag} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("log = %s, want it to contain %s", logs.String(), want)
		}
	}
	rec := httptest.NewRecorder()
	metrics.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, k8sapiproxy.MetricsPath, nil))
	if want := "kubeagents_kube_proxy_certificate_reload_failures_total 1"; !strings.Contains(rec.Body.String(), want) {
		t.Errorf("metrics exposition missing %q", want)
	}

	// Without metrics, as in a build that passes none, the hook still logs.
	upstreamCAReloadError(slog.New(slog.DiscardHandler), nil)(errors.New("bundle cut off mid-write"))
}

func TestBuildHandlerWarnsWhenAnUnauthenticatedListenerLendsTheUpstreamToken(t *testing.T) {
	clearHostEnv(t)
	up := newTLSUpstream(t)
	noneArgs := []string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443", "--upstream-url=" + up.srv.URL}

	var logs bytes.Buffer
	args := append(append([]string{}, noneArgs...), standaloneArgs(t, up, writeTokenFile(t, testUpstreamToken))...)
	buildTestHandler(t, parseTestFlags(t, args...), slog.New(slog.NewJSONHandler(&logs, nil)))
	if !strings.Contains(logs.String(), unauthenticatedTokenWarning) || !strings.Contains(logs.String(), `"listen_addr":"127.0.0.1:18443"`) {
		t.Errorf("logs = %s, want the warning that every peer acts with the token file's identity", logs.String())
	}

	// Without a token file the proxy attaches no credential, and with an authenticated
	// caller it lends the token to nobody unknown: neither is warned about.
	is := newTestJWTIssuer(t)
	for name, quiet := range map[string][]string{
		"none without a token file": append(append([]string{}, noneArgs...), "--upstream-ca-file="+up.caFile),
		"jwt with a token file":     is.jwtArgs(up.srv.URL, standaloneArgs(t, up, writeTokenFile(t, testUpstreamToken))...),
	} {
		logs.Reset()
		buildTestHandler(t, parseTestFlags(t, quiet...), slog.New(slog.NewJSONHandler(&logs, nil)))
		if strings.Contains(logs.String(), unauthenticatedTokenWarning) {
			t.Errorf("%s: logs = %s, want no warning about lending the upstream token", name, logs.String())
		}
	}
}
