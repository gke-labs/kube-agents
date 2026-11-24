package main

import (
	"bytes"
	"context"
	"errors"
	"flag"
	"io"
	"log"
	"log/slog"
	"net"
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
	testIntKey       = "KUBE_PROXY_TEST_INT"
	testDrainTimeout = 5 * time.Second
	testEarlyReturn  = 200 * time.Millisecond
	testWaitLimit    = 10 * time.Second
)

// receive fails the test instead of hanging when ch stays empty.
func receive[T any](t *testing.T, ch <-chan T, what string) T {
	t.Helper()
	select {
	case v := <-ch:
		return v
	case <-time.After(testWaitLimit):
		t.Fatalf("no %s within %s", what, testWaitLimit)
		var zero T
		return zero
	}
}

// A request in flight when SIGTERM lands finishes before the process exits:
// the replica's preStop sleep only covers new connections, and a kubectl
// call already inside the proxy must not be cut.
func TestServeUntilDrainedWaitsForTheInFlightRequest(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	arrived, release := make(chan struct{}), make(chan struct{})
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		close(arrived)
		<-release
		_, _ = io.WriteString(w, "done")
	})}

	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	served := make(chan error, 1)
	go func() {
		drainErr, err := serveUntilDrained(ctx, srv, func() error { return srv.Serve(ln) }, testDrainTimeout)
		served <- errors.Join(drainErr, err)
	}()

	answered := make(chan string, 1)
	go func() {
		resp, err := http.Get("http://" + ln.Addr().String())
		if err != nil {
			answered <- "error: " + err.Error()
			return
		}
		defer func() { _ = resp.Body.Close() }()
		body, _ := io.ReadAll(resp.Body)
		answered <- string(body)
	}()
	receive(t, arrived, "request at the server")

	stop()
	select {
	case err := <-served:
		t.Fatalf("returned with a request in flight (err=%v)", err)
	case <-time.After(testEarlyReturn):
	}

	close(release)
	if got := receive(t, answered, "response"); got != "done" {
		t.Errorf("in-flight request got %q, want done", got)
	}
	if err := receive(t, served, "return from serveUntilDrained"); err != nil {
		t.Errorf("serveUntilDrained = %v, want nil", err)
	}
}

func TestServeUntilDrainedReturnsAListenFailureWithoutWaiting(t *testing.T) {
	bound := errors.New("address already in use")
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	drainErr, err := serveUntilDrained(ctx, &http.Server{}, func() error { return bound }, testDrainTimeout)
	if !errors.Is(err, bound) || drainErr != nil {
		t.Errorf("got (drainErr=%v, err=%v), want (nil, %v)", drainErr, err, bound)
	}
}

func TestEnvIntOrDefault(t *testing.T) {
	cases := []struct {
		name, raw string
		want      int
		alert     bool
	}{
		{name: "unset", raw: "", want: 7},
		{name: "integer", raw: " 9100 ", want: 9100},
		{name: "not an integer", raw: "80x", want: 7, alert: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(testIntKey, tc.raw)
			var buf bytes.Buffer
			prev := log.Writer()
			log.SetOutput(&buf)
			t.Cleanup(func() { log.SetOutput(prev) })

			if got := envIntOrDefault(testIntKey, 7); got != tc.want {
				t.Errorf("envIntOrDefault = %d, want %d", got, tc.want)
			}
			out := buf.String()
			if !tc.alert {
				if out != "" {
					t.Errorf("logged %q, want nothing", out)
				}
				return
			}
			// The ALERT names the key it was given and the fallback it used,
			// not the metrics port's variable or 0.
			for _, want := range []string{"ALERT", testIntKey + `="80x"`, "using 7"} {
				if !strings.Contains(out, want) {
					t.Errorf("logged %q, want it to contain %q", out, want)
				}
			}
		})
	}
}

func TestVersionStringNamesTheBuild(t *testing.T) {
	if got, want := versionString(), "k8s-api-proxy "+devVersion; got != want {
		t.Errorf("versionString() = %q, want %q for a build without the release ldflags", got, want)
	}
	defer func(old string) { version = old }(version)
	version = "0.34.0"
	if got, want := versionString(), "k8s-api-proxy 0.34.0"; got != want {
		t.Errorf("versionString() = %q, want %q", got, want)
	}
}

func TestRegisterFlagsReadsBridgeEnvVars(t *testing.T) {
	t.Setenv(listenAddrEnv, "127.0.0.1:18443")
	t.Setenv(authModeEnv, authModeNone)
	t.Setenv(upstreamURLEnv, "http://127.0.0.1:18080/k8s")
	t.Setenv(denyStreamingEnv, "true")

	var cfg proxyConfig
	fs := flag.NewFlagSet("test", flag.ContinueOnError)
	if err := parseFlags(fs, nil, &cfg); err != nil {
		t.Fatalf("parseFlags: %v", err)
	}
	if !cfg.listenAddrSet || cfg.listenAddr != "127.0.0.1:18443" {
		t.Errorf("listenAddr = %q (set=%v), want 127.0.0.1:18443 (set=true)", cfg.listenAddr, cfg.listenAddrSet)
	}
	if cfg.authMode != authModeNone {
		t.Errorf("authMode = %q, want %q", cfg.authMode, authModeNone)
	}
	if cfg.upstreamURL != "http://127.0.0.1:18080/k8s" {
		t.Errorf("upstreamURL = %q", cfg.upstreamURL)
	}
	if !cfg.denyStreaming {
		t.Error("denyStreaming = false, want true")
	}
}

func TestParseFlagsTracksExplicitListenAddrForAuthModeNone(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)

	t.Run("default listen-addr without env or flag", func(t *testing.T) {
		t.Setenv(listenAddrEnv, "")
		t.Setenv(authModeEnv, "")
		t.Setenv(upstreamURLEnv, "http://127.0.0.1:18080")

		var cfg proxyConfig
		fs := flag.NewFlagSet("test", flag.ContinueOnError)
		if err := parseFlags(fs, nil, &cfg); err != nil {
			t.Fatalf("parseFlags: %v", err)
		}
		if cfg.listenAddr != defaultListenAddr || cfg.listenAddrSet {
			t.Fatalf("listenAddr = %q (set=%v), want %q (set=false)", cfg.listenAddr, cfg.listenAddrSet, defaultListenAddr)
		}

		tokenCfg := cfg
		tokenCfg.allowedCallers = "system:serviceaccount:ns:sa"
		tokenCfg.kubeAPIServer = "http://127.0.0.1:18080"
		tokenCfg.kubeCAFile = ""
		tokenCfg.kubeTokenFile = ""
		if _, err := buildHandler(context.Background(), tokenCfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil); err != nil {
			t.Fatalf("buildHandler(token-review) with default listen-addr failed: %v", err)
		}

		noneCfg := cfg
		noneCfg.authMode = authModeNone
		if _, err := buildHandler(context.Background(), noneCfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil); err == nil {
			t.Fatal("buildHandler(none) with implicit default listen-addr succeeded, want error")
		} else if !strings.Contains(err.Error(), "--"+listenAddrFlag) {
			t.Errorf("err = %q, want substring %q", err.Error(), "--"+listenAddrFlag)
		}
	})

	t.Run("listen-addr set via env", func(t *testing.T) {
		t.Setenv(listenAddrEnv, "127.0.0.1:18443")
		t.Setenv(authModeEnv, authModeNone)
		t.Setenv(upstreamURLEnv, "http://127.0.0.1:18080")

		var cfg proxyConfig
		fs := flag.NewFlagSet("test", flag.ContinueOnError)
		if err := parseFlags(fs, nil, &cfg); err != nil {
			t.Fatalf("parseFlags: %v", err)
		}
		if !cfg.listenAddrSet {
			t.Fatal("listenAddrSet = false, want true when KUBE_PROXY_LISTEN_ADDR is set")
		}
		if _, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil); err != nil {
			t.Fatalf("buildHandler: %v", err)
		}
	})

	t.Run("explicit wildcard listen-addr via CLI flag", func(t *testing.T) {
		t.Setenv(listenAddrEnv, "")
		t.Setenv(authModeEnv, "")
		t.Setenv(upstreamURLEnv, "")

		var cfg proxyConfig
		fs := flag.NewFlagSet("test", flag.ContinueOnError)
		args := []string{
			"--listen-addr=:8443",
			"--auth-mode=none",
			"--upstream-url=http://127.0.0.1:18080",
		}
		if err := parseFlags(fs, args, &cfg); err != nil {
			t.Fatalf("parseFlags: %v", err)
		}
		if !cfg.listenAddrSet || cfg.listenAddr != ":8443" {
			t.Fatalf("listenAddr = %q (set=%v), want :8443 (set=true)", cfg.listenAddr, cfg.listenAddrSet)
		}
		if _, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil); err != nil {
			t.Fatalf("buildHandler with explicit --listen-addr=:8443 failed: %v", err)
		}
	})
}

func TestBuildHandlerStandaloneBridgeEndToEnd(t *testing.T) {
	var (
		mu      sync.Mutex
		seenURI []string
		seenHdr []string
	)
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		seenURI = append(seenURI, r.URL.RequestURI())
		seenHdr = append(seenHdr, r.Header.Get("Authorization"))
		mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"kind":"PodList","apiVersion":"v1","items":[]}`)
	}))
	t.Cleanup(up.Close)

	var logBuf bytes.Buffer
	logger := slog.New(slog.NewJSONHandler(&logBuf, nil))
	cfg := proxyConfig{
		listenAddr:         "127.0.0.1:18443",
		listenAddrSet:      true,
		authMode:           authModeNone,
		upstreamURL:        up.URL + "/bridge",
		denyStreaming:      true,
		redactSecrets:      true,
		cacheMaxBytes:      k8sapiproxy.DefaultCacheMaxBytes,
		cacheMaxEntryBytes: k8sapiproxy.DefaultCacheMaxEntryBytes,
		cacheResourceTTL:   time.Minute,
		cacheDiscoveryTTL:  k8sapiproxy.DefaultDiscoveryCacheTTL,
		maxInFlight:        defaultMaxInFlight,
	}
	h, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, k8sapiproxy.NewMetrics(), nil)
	if err != nil {
		t.Fatalf("buildHandler: %v", err)
	}
	for _, want := range []string{`"level":"WARN"`, `"auth_mode":"none"`, `"listen_addr":"127.0.0.1:18443"`, `"tls_enabled":false`} {
		if !strings.Contains(logBuf.String(), want) {
			t.Errorf("expected WARN startup log to contain %s, got: %s", want, logBuf.String())
		}
	}

	// Bare /api/... populates the cache, and /clusters/in-cluster/api/... hits the same entry.
	first := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods?limit=5", nil)
	first.Header.Set("Authorization", "Bearer caller-token")
	rec1 := httptest.NewRecorder()
	h.ServeHTTP(rec1, first)
	if rec1.Code != http.StatusOK {
		t.Fatalf("GET bare pods: code = %d, body = %s", rec1.Code, rec1.Body.String())
	}
	second := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/pods?limit=5", nil)
	rec2 := httptest.NewRecorder()
	h.ServeHTTP(rec2, second)
	if rec2.Code != http.StatusOK || rec2.Header().Get("Cache-Status") != "k8s-api-proxy; hit" {
		t.Fatalf("GET clustered pods: code = %d, Cache-Status = %q, want 200 hit", rec2.Code, rec2.Header().Get("Cache-Status"))
	}
	secondUncached := httptest.NewRequest(http.MethodGet, "/clusters/in-cluster/api/v1/namespaces/default/pods?limit=5", nil)
	secondUncached.Header.Set("Cache-Control", "no-cache")
	secondUncached.Header.Set("Authorization", "Bearer caller-token-2")
	rec2u := httptest.NewRecorder()
	h.ServeHTTP(rec2u, secondUncached)
	if rec2u.Code != http.StatusOK {
		t.Fatalf("GET uncached clustered pods: code = %d", rec2u.Code)
	}

	logReq := httptest.NewRequest(http.MethodGet, "/api/v1/namespaces/default/pods/web/log?tailLines=10", nil)
	logReq.Header.Set("Authorization", "Bearer caller-token")
	logRec := httptest.NewRecorder()
	h.ServeHTTP(logRec, logReq)
	if logRec.Code != http.StatusOK {
		t.Fatalf("GET finite log: code = %d", logRec.Code)
	}

	// Non-in-cluster fleet prefix is refused with 502 instead of silently serving the static upstream.
	wrongCluster := httptest.NewRecorder()
	h.ServeHTTP(wrongCluster, httptest.NewRequest(http.MethodGet, "/clusters/gke_other_us-central1_c/api/v1/namespaces/default/pods", nil))
	if wrongCluster.Code != http.StatusBadGateway {
		t.Errorf("GET wrong cluster: code = %d, want 502", wrongCluster.Code)
	}

	for _, blocked := range []string{
		"/api/v1/namespaces/default/pods?watch=true",
		"/api/v1/watch/namespaces/default/pods",
		"/api/v1/namespaces/default/pods/web/log?follow=true",
	} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, blocked, nil))
		if rec.Code != http.StatusForbidden {
			t.Errorf("GET %s: code = %d, want 403", blocked, rec.Code)
		}
	}

	mu.Lock()
	defer mu.Unlock()
	wantURIs := []string{
		"/bridge/api/v1/namespaces/default/pods?limit=5",
		"/bridge/api/v1/namespaces/default/pods?limit=5",
		"/bridge/api/v1/namespaces/default/pods/web/log?tailLines=10",
	}
	if strings.Join(seenURI, "|") != strings.Join(wantURIs, "|") {
		t.Errorf("upstream URIs = %v, want %v", seenURI, wantURIs)
	}
	for i, auth := range seenHdr {
		if auth != "" {
			t.Errorf("request %d leaked Authorization %q", i, auth)
		}
	}
}

func TestBuildHandlerRejectsConflictingOrInvalidFlags(t *testing.T) {
	dir := t.TempDir()
	tokenFile, emptyFile, absentFile := filepath.Join(dir, "token"), filepath.Join(dir, "empty"), filepath.Join(dir, "absent")
	if err := os.WriteFile(tokenFile, []byte("upstream-token\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(emptyFile, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	base := proxyConfig{
		listenAddr:    "127.0.0.1:18443",
		listenAddrSet: true,
		authMode:      authModeNone,
		upstreamURL:   "http://127.0.0.1:18080",
		redactSecrets: true,
		maxInFlight:   defaultMaxInFlight,
	}
	cases := []struct {
		name    string
		mutate  func(*proxyConfig)
		wantMsg string
	}{
		{
			name:    "unknown auth-mode",
			mutate:  func(c *proxyConfig) { c.authMode = "NONE" },
			wantMsg: "invalid --auth-mode",
		},
		{
			name: "auth-mode none without explicit listen-addr",
			mutate: func(c *proxyConfig) {
				c.listenAddr = defaultListenAddr
				c.listenAddrSet = false
			},
			wantMsg: "--" + listenAddrFlag + " (or " + listenAddrEnv + ") must be set explicitly when --auth-mode=none",
		},
		{
			name: "auth-mode none with empty listen-addr",
			mutate: func(c *proxyConfig) {
				c.listenAddr = "   "
				c.listenAddrSet = true
			},
			wantMsg: "--" + listenAddrFlag + " (or " + listenAddrEnv + ") must be set explicitly when --auth-mode=none",
		},
		{
			name:    "token-review without allowed-callers",
			mutate:  func(c *proxyConfig) { c.authMode = authModeTokenReview },
			wantMsg: "--allowed-callers",
		},
		{
			name:    "auth-mode none with allowed-callers",
			mutate:  func(c *proxyConfig) { c.allowedCallers = "system:serviceaccount:ns:sa" },
			wantMsg: "--auth-mode=none cannot be combined with --allowed-callers",
		},
		{
			name:    "auth-mode none with session audience",
			mutate:  func(c *proxyConfig) { c.sessionAudience = "session-aud" },
			wantMsg: sessionAudienceEnv,
		},
		{
			name:    "auth-mode none with session callers",
			mutate:  func(c *proxyConfig) { c.sessionCallers = "system:serviceaccount:ns:sa" },
			wantMsg: sessionCallersEnv,
		},
		{
			name: "deny-streaming with allow-upgrade-subresources",
			mutate: func(c *proxyConfig) {
				c.denyStreaming = true
				c.allowUpgradeSubresources = true
			},
			wantMsg: "--deny-streaming cannot be combined with --allow-upgrade-subresources",
		},
		{
			name:    "upstream-url with host-cluster-id",
			mutate:  func(c *proxyConfig) { c.hostClusterID = "gke_proj_us-central1_c" },
			wantMsg: "--upstream-url cannot be combined with --host-cluster-id",
		},
		{
			name:    "upstream-url with scoped SA pool",
			mutate:  func(c *proxyConfig) { c.scopedSAPool = "true" },
			wantMsg: "--upstream-url cannot be combined with " + scopedPoolFlagEnv,
		},
		{
			name:    "upstream-url with container-api-url",
			mutate:  func(c *proxyConfig) { c.containerAPIURLOverride = "https://container.example" },
			wantMsg: "--upstream-url cannot be combined with --container-api-url",
		},
		{
			name:    "upstream-url with scope-file",
			mutate:  func(c *proxyConfig) { c.scopeFile = "/etc/scope.json" },
			wantMsg: "--upstream-url cannot be combined with --scope-file",
		},
		{
			name:    "upstream-url with exclude-host-cluster-id",
			mutate:  func(c *proxyConfig) { c.excludeHostCluster = "gke_proj_us-central1_c" },
			wantMsg: "--upstream-url cannot be combined with --scope-file, --hold-file or --exclude-host-cluster-id",
		},
		{
			name:    "upstream-url with hold-file",
			mutate:  func(c *proxyConfig) { c.holdFile = "/etc/hold.json" },
			wantMsg: "--upstream-url cannot be combined with --scope-file, --hold-file",
		},
		{
			name:    "invalid upstream-url",
			mutate:  func(c *proxyConfig) { c.upstreamURL = "ftp://127.0.0.1:21" },
			wantMsg: "static upstream transport",
		},
		{
			name:    "upstream-token-file over plain http",
			mutate:  func(c *proxyConfig) { c.upstreamTokenFile = tokenFile },
			wantMsg: "with a bearer token file",
		},
		{
			name:    "upstream-ca-file over plain http",
			mutate:  func(c *proxyConfig) { c.upstreamCAFile = tokenFile },
			wantMsg: "with a CA file",
		},
		{
			name: "upstream-token-file without upstream-url",
			mutate: func(c *proxyConfig) {
				c.upstreamURL = ""
				c.upstreamTokenFile = tokenFile
			},
			wantMsg: "--" + upstreamTokenFileFlag + " and --" + upstreamCAFileFlag + " require --" + upstreamURLFlag,
		},
		{
			name: "upstream-ca-file without upstream-url",
			mutate: func(c *proxyConfig) {
				c.upstreamURL = ""
				c.upstreamCAFile = tokenFile
			},
			wantMsg: "--" + upstreamTokenFileFlag + " and --" + upstreamCAFileFlag + " require --" + upstreamURLFlag,
		},
		{
			name: "upstream-token-file that cannot be read at startup",
			mutate: func(c *proxyConfig) {
				c.upstreamURL = "https://127.0.0.1:18443"
				c.upstreamTokenFile = absentFile
			},
			wantMsg: "bearer token file",
		},
		{
			name: "upstream-token-file that is empty at startup",
			mutate: func(c *proxyConfig) {
				c.upstreamURL = "https://127.0.0.1:18443"
				c.upstreamTokenFile = emptyFile
			},
			wantMsg: "is empty",
		},
		{
			name: "upstream-ca-file that cannot be read at startup",
			mutate: func(c *proxyConfig) {
				c.upstreamURL = "https://127.0.0.1:18443"
				c.upstreamCAFile = absentFile
			},
			wantMsg: "static upstream CA file",
		},
		{
			name: "upstream-ca-file without a certificate",
			mutate: func(c *proxyConfig) {
				c.upstreamURL = "https://127.0.0.1:18443"
				c.upstreamCAFile = tokenFile
			},
			wantMsg: "static upstream CA file",
		},
	}

	logger := slog.New(slog.DiscardHandler)
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := base
			tc.mutate(&cfg)
			_, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil)
			if err == nil {
				t.Fatal("buildHandler succeeded, want error")
			}
			if !strings.Contains(err.Error(), tc.wantMsg) {
				t.Errorf("err = %q, want substring %q", err.Error(), tc.wantMsg)
			}
		})
	}
}
