package proxyserver

import (
	"context"
	"errors"
	"flag"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testEmbedderHeader      = "X-Embedder-Credential"
	testEmbedderHeaderValue = "embedder-credential-value"
	testStockHeader         = "X-Stock-Header"
	testStockHeaderValue    = "stock-value"
	testCallerToken         = "caller-token"
)

// headerUpstream is an API server that counts its requests and keeps the headers of the
// first one it has not handed out.
type headerUpstream struct {
	srv   *httptest.Server
	calls atomic.Int32
	seen  chan http.Header
}

func newHeaderUpstream(t *testing.T) *headerUpstream {
	t.Helper()
	u := &headerUpstream{seen: make(chan http.Header, 1)}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u.calls.Add(1)
		select {
		case u.seen <- r.Header.Clone():
		default:
		}
		w.Header().Set("Content-Type", testContentJSON)
		_, _ = io.WriteString(w, `{"kind":"PodList","apiVersion":"v1","items":[]}`)
	}))
	t.Cleanup(u.srv.Close)
	return u
}

// testFlagSet is a flag set that returns a parse error, as flag.CommandLine does once an
// embedder has given it flag.ContinueOnError, and prints nothing.
func testFlagSet() *flag.FlagSet {
	fs := flag.NewFlagSet(binaryName, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	return fs
}

// embedderConfig is the configuration Run assembles for a loopback bridge in front of
// upstreamURL in a binary that passes opts.
func embedderConfig(t *testing.T, upstreamURL string, opts Options) proxyConfig {
	t.Helper()
	clearProxyEnv(t)
	args := []string{"--auth-mode=none", "--listen-addr=127.0.0.1:18443", "--upstream-url=" + upstreamURL}
	cfg, mode, versionOnly, err := assemble(testFlagSet(), args, opts)
	if err != nil || versionOnly {
		t.Fatalf("assemble(%q) = versionOnly %v, err %v; want a configuration", args, versionOnly, err)
	}
	if mode != k8sapiproxy.ModeReadOnlyWithDryRun {
		t.Fatalf("assemble(%q) mode = %q, want the default %q", args, mode, k8sapiproxy.ModeReadOnlyWithDryRun)
	}
	return cfg
}

func getPodsWithHeaders(h http.Handler, headers map[string]string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
	req.Header.Set("Authorization", "Bearer "+testCallerToken)
	for name, value := range headers {
		req.Header.Set(name, value)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestOptionsAuthorizeRefusesThroughTheBuiltHandler(t *testing.T) {
	up := newHeaderUpstream(t)
	logger := slog.New(slog.DiscardHandler)

	var asked atomic.Int32
	var sawHeader atomic.Value
	refuse := func(attrs k8sapiproxy.AuthorizeAttributes) error {
		asked.Add(1)
		sawHeader.Store(attrs.Request.Header.Get(testEmbedderHeader))
		return errors.New("the embedder's rule refuses this caller")
	}

	// Without Options the handler is the stock binary's: nothing asks the hook.
	stock := buildTestHandler(t, embedderConfig(t, up.srv.URL, Options{}), logger)
	if rec := getPods(stock, testCallerToken); rec.Code != http.StatusOK {
		t.Fatalf("stock handler: code = %d, want 200; body = %s", rec.Code, rec.Body.String())
	}
	if got := asked.Load(); got != 0 {
		t.Fatalf("the hook was called %d times by a handler built without it", got)
	}
	stockCalls := up.calls.Load()

	embedded := buildTestHandler(t, embedderConfig(t, up.srv.URL, Options{Authorize: refuse}), logger)
	rec := getPodsWithHeaders(embedded, map[string]string{testEmbedderHeader: testEmbedderHeaderValue})
	if rec.Code != http.StatusForbidden {
		t.Fatalf("handler with Authorize: code = %d, want 403; body = %s", rec.Code, rec.Body.String())
	}
	if got := asked.Load(); got != 1 {
		t.Errorf("the hook was called %d times, want 1", got)
	}
	if got, _ := sawHeader.Load().(string); got != testEmbedderHeaderValue {
		t.Errorf("the hook read %s = %q, want %q", testEmbedderHeader, got, testEmbedderHeaderValue)
	}
	if got := up.calls.Load(); got != stockCalls {
		t.Errorf("the upstream got %d requests for a refused caller", got-stockCalls)
	}
}

func TestOptionsExtraCallerHeadersAreAbsentUpstream(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	sent := map[string]string{testEmbedderHeader: testEmbedderHeaderValue, testStockHeader: testStockHeaderValue}

	for _, tc := range []struct {
		name string
		opts Options
		want string
	}{
		{name: "unset forwards the header as the stock binary does", want: testEmbedderHeaderValue},
		{name: "named header is stripped", opts: Options{ExtraCallerHeaders: []string{strings.ToLower(testEmbedderHeader)}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			up := newHeaderUpstream(t)
			h := buildTestHandler(t, embedderConfig(t, up.srv.URL, tc.opts), logger)
			if rec := getPodsWithHeaders(h, sent); rec.Code != http.StatusOK {
				t.Fatalf("code = %d, want 200; body = %s", rec.Code, rec.Body.String())
			}
			var got http.Header
			select {
			case got = <-up.seen:
			default:
				t.Fatal("the upstream got no request")
			}
			if v := got.Get(testEmbedderHeader); v != tc.want {
				t.Errorf("upstream %s = %q, want %q", testEmbedderHeader, v, tc.want)
			}
			if v := got.Get(testStockHeader); v != testStockHeaderValue {
				t.Errorf("upstream %s = %q, want %q: a header nothing names is forwarded", testStockHeader, v, testStockHeaderValue)
			}
		})
	}
}

func TestOptionsInvalidExtraCallerHeaderFailsStartup(t *testing.T) {
	up := newHeaderUpstream(t)
	logger := slog.New(slog.DiscardHandler)
	for _, name := range []string{"not a header", "Host"} {
		cfg := embedderConfig(t, up.srv.URL, Options{ExtraCallerHeaders: []string{name}})
		_, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, k8sapiproxy.NewMetrics(), nil)
		if err == nil {
			t.Fatalf("buildHandler with ExtraCallerHeaders %q succeeded, want the handler's error", name)
		}
		for _, want := range []string{"failed to initialize k8s-api-proxy handler", "Config.ExtraCallerHeaders", name} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("err = %q, want it to contain %q", err, want)
			}
		}
	}
}

// --version is decided from the flags alone: nothing else is read, so a variable or a mode
// the server would refuse does not stop a binary from naming its build.
func TestAssembleVersionOnlyReadsNoEnvironment(t *testing.T) {
	clearProxyEnv(t)
	t.Setenv(sessionAudienceEnv, testReviewAudience)
	cfg, _, versionOnly, err := assemble(testFlagSet(), []string{"--version", "--mode=no-such-mode"}, Options{Authorize: func(k8sapiproxy.AuthorizeAttributes) error { return nil }})
	if err != nil || !versionOnly {
		t.Fatalf("assemble(--version) = versionOnly %v, err %v; want true, nil", versionOnly, err)
	}
	if cfg.sessionAudience != "" {
		t.Errorf("sessionAudience = %q after --version, want the environment unread", cfg.sessionAudience)
	}

	// Without --version the same environment is read.
	cfg, _, versionOnly, err = assemble(testFlagSet(), nil, Options{})
	if err != nil || versionOnly {
		t.Fatalf("assemble() = versionOnly %v, err %v; want false, nil", versionOnly, err)
	}
	if cfg.sessionAudience != testReviewAudience {
		t.Errorf("sessionAudience = %q, want %q from %s", cfg.sessionAudience, testReviewAudience, sessionAudienceEnv)
	}
}

func TestAssembleRefusesAnInvalidMode(t *testing.T) {
	clearProxyEnv(t)
	_, parseErr := k8sapiproxy.ParseMode("no-such-mode")
	if parseErr == nil {
		t.Fatal("ParseMode accepted no-such-mode")
	}
	_, _, versionOnly, err := assemble(testFlagSet(), []string{"--mode=no-such-mode"}, Options{})
	if err == nil || versionOnly {
		t.Fatalf("assemble(--mode=no-such-mode) = versionOnly %v, err %v; want an error", versionOnly, err)
	}
	if got, want := err.Error(), "invalid mode: "+parseErr.Error(); got != want {
		t.Errorf("err = %q, want %q, the line the binary exits on", got, want)
	}
}

// A flag set that does not exit on a parse error gets the error back, and no configuration
// to start a server on.
func TestAssembleReturnsAFlagParseError(t *testing.T) {
	clearProxyEnv(t)
	for _, args := range [][]string{
		{"--no-such-flag"},
		{"--auth-mode=none", "--max-in-flight=many"},
	} {
		cfg, _, versionOnly, err := assemble(testFlagSet(), args, Options{ExtraCallerHeaders: []string{testEmbedderHeader}})
		if err == nil || versionOnly {
			t.Fatalf("assemble(%q) = versionOnly %v, err %v; want the parse error", args, versionOnly, err)
		}
		if cfg.authMode != "" || cfg.embedder.ExtraCallerHeaders != nil {
			t.Errorf("assemble(%q) returned a configuration with its error: authMode %q, embedder %v", args, cfg.authMode, cfg.embedder.ExtraCallerHeaders)
		}
	}
}
