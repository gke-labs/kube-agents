package proxyserver

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testLimitedCaller = "system:serviceaccount:agents:worker"
	testOtherCaller   = "system:serviceaccount:agents:shell"
	testRetryAfter    = "Retry-After"
)

func limitsOf(t *testing.T, cfg proxyConfig, username string) k8sapiproxy.CallerLimits {
	t.Helper()
	limits, err := callerLimitsFunc(cfg)
	if err != nil {
		t.Fatalf("callerLimitsFunc: %v", err)
	}
	if limits == nil {
		return k8sapiproxy.CallerLimits{}
	}
	return limits(&k8sapiproxy.CallerIdentity{Username: username})
}

func TestCallerLimitsFunc(t *testing.T) {
	clearProxyEnv(t)

	stock := parseTestFlags(t)
	if limits, err := callerLimitsFunc(stock); err != nil || limits != nil {
		t.Errorf("no flag and no embedder: callerLimitsFunc = (set %t, %v), want the stock handler's nil", limits != nil, err)
	}

	flagged := parseTestFlags(t, "--max-in-flight-per-caller=6", "--max-long-running-per-caller=3")
	if got, want := limitsOf(t, flagged, testOtherCaller), (k8sapiproxy.CallerLimits{MaxInFlight: 6, MaxLongRunning: 3}); got != want {
		t.Errorf("flags alone: limits = %+v, want %+v for every caller", got, want)
	}

	embedder := func(caller *k8sapiproxy.CallerIdentity) k8sapiproxy.CallerLimits {
		if caller.Username == testLimitedCaller {
			return k8sapiproxy.CallerLimits{MaxInFlight: 2, MaxLongRunning: -1}
		}
		return k8sapiproxy.CallerLimits{}
	}
	stock.embedder.CallerLimits = embedder
	if got, want := limitsOf(t, stock, testLimitedCaller), (k8sapiproxy.CallerLimits{MaxInFlight: 2, MaxLongRunning: -1}); got != want {
		t.Errorf("embedder alone: limits = %+v, want its own %+v", got, want)
	}
	if got := limitsOf(t, stock, testOtherCaller); got != (k8sapiproxy.CallerLimits{}) {
		t.Errorf("embedder alone: limits = %+v for a caller it does not limit, want none", got)
	}

	flagged.embedder.CallerLimits = embedder
	if got, want := limitsOf(t, flagged, testLimitedCaller), (k8sapiproxy.CallerLimits{MaxInFlight: 2, MaxLongRunning: -1}); got != want {
		t.Errorf("embedder and flags: limits = %+v, want the embedder's %+v, its negative field kept as no limit", got, want)
	}
	if got, want := limitsOf(t, flagged, testOtherCaller), (k8sapiproxy.CallerLimits{MaxInFlight: 6, MaxLongRunning: 3}); got != want {
		t.Errorf("embedder and flags: limits = %+v for a caller the embedder does not limit, want the flags' %+v", got, want)
	}

	for _, arg := range []string{"--max-in-flight-per-caller=-1", "--max-long-running-per-caller=-1"} {
		if _, err := callerLimitsFunc(parseTestFlags(t, arg)); err == nil || !strings.Contains(err.Error(), "negative") {
			t.Errorf("%s: err = %v, want a negative-value error", arg, err)
		}
	}
}

func TestCallerLimitFlagsReadTheEnvironment(t *testing.T) {
	clearProxyEnv(t)
	t.Setenv(maxInFlightPerCallerEnv, "5")
	t.Setenv(maxLongRunningPerCallerEnv, "4")
	cfg := parseTestFlags(t)
	if cfg.maxInFlightPerCaller != 5 || cfg.maxLongRunningPerCaller != 4 {
		t.Errorf("limits = (%d, %d), want (5, 4) from the environment", cfg.maxInFlightPerCaller, cfg.maxLongRunningPerCaller)
	}
	cfg = parseTestFlags(t, "--max-in-flight-per-caller=1")
	if cfg.maxInFlightPerCaller != 1 || cfg.maxLongRunningPerCaller != 4 {
		t.Errorf("limits = (%d, %d), want the flag's 1 over the environment's 5", cfg.maxInFlightPerCaller, cfg.maxLongRunningPerCaller)
	}
}

// A caller at its limit is answered 429 with Retry-After by the handler the server builds,
// from Options.CallerLimits and from the flag alike, and served again once its request ends.
func TestCallerLimitsThroughTheBuiltHandler(t *testing.T) {
	logger := slog.New(slog.DiscardHandler)
	for name, configure := range map[string]func(t *testing.T, upstreamURL string) proxyConfig{
		"Options.CallerLimits": func(t *testing.T, upstreamURL string) proxyConfig {
			return embedderConfig(t, upstreamURL, Options{CallerLimits: func(*k8sapiproxy.CallerIdentity) k8sapiproxy.CallerLimits {
				return k8sapiproxy.CallerLimits{MaxInFlight: 1}
			}})
		},
		"--max-in-flight-per-caller": func(t *testing.T, upstreamURL string) proxyConfig {
			cfg := embedderConfig(t, upstreamURL, Options{})
			cfg.maxInFlightPerCaller = 1
			return cfg
		},
	} {
		t.Run(name, func(t *testing.T) {
			entered, gate := make(chan struct{}, 1), make(chan struct{})
			release := sync.OnceFunc(func() { close(gate) })
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				select {
				case entered <- struct{}{}:
					<-gate
				default:
				}
				w.Header().Set("Content-Type", testContentJSON)
				_, _ = io.WriteString(w, `{"kind":"PodList","apiVersion":"v1","items":[]}`)
			}))
			t.Cleanup(upstream.Close)
			t.Cleanup(release)

			h := buildTestHandler(t, configure(t, upstream.URL), logger)
			held := make(chan *httptest.ResponseRecorder, 1)
			go func() { held <- getPods(h, testCallerToken) }()
			<-entered

			rec := getPods(h, testCallerToken)
			if rec.Code != http.StatusTooManyRequests || rec.Header().Get(testRetryAfter) == "" {
				t.Fatalf("at the limit: code = %d, Retry-After = %q, want 429 with the header; body = %s", rec.Code, rec.Header().Get(testRetryAfter), rec.Body.String())
			}
			release()
			if rec := <-held; rec.Code != http.StatusOK {
				t.Fatalf("held request: code = %d, want 200", rec.Code)
			}
			if rec := getPods(h, testCallerToken); rec.Code != http.StatusOK {
				t.Errorf("after the held request finished: code = %d, want 200", rec.Code)
			}
		})
	}
}
