package main

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"slices"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// fakeHarnessEnv makes this test binary act as the core-agent harness for
// one model call (TestFakeCoreAgentHarness): it reads its endpoint from
// GOOGLE_GEMINI_BASE_URL and POSTs one streamGenerateContent there, with
// the placeholder key in x-goog-api-key, as the genai client does.
const fakeHarnessEnv = "WORKER_ADAPTER_TEST_FAKE_CORE_AGENT"

func TestFakeCoreAgentHarness(t *testing.T) {
	if os.Getenv(fakeHarnessEnv) != "1" {
		t.Skip("helper for TestACoreAgentWorkerCallsTheGatewayThroughOneProxy")
	}
	for _, kv := range os.Environ() {
		k, _, _ := strings.Cut(kv, "=")
		if k != coreAgentGatewayURLEnv && coreAgentWithheld(k) {
			fmt.Fprintf(os.Stderr, "the harness's env has %s\n", k)
			os.Exit(5)
		}
	}
	req, _ := http.NewRequest(http.MethodPost, os.Getenv(coreAgentGatewayURLEnv)+"/v1beta/models/gemini-default:streamGenerateContent?alt=sse", strings.NewReader(`{"contents":[]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-goog-api-key", "gateway-proxy-owns-the-key")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(6)
	}
	body, _ := io.ReadAll(resp.Body)
	fmt.Print(string(body))
	if resp.StatusCode != http.StatusOK {
		os.Exit(7)
	}
	os.Exit(0)
}

// recordingGateway is the install's gateway: it records each call and
// answers one finished SSE chunk.
type recordingGateway struct {
	mu    sync.Mutex
	calls []*http.Request
}

func (g *recordingGateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	g.mu.Lock()
	g.calls = append(g.calls, r.Clone(r.Context()))
	g.mu.Unlock()
	w.Header().Set("Content-Type", "text/event-stream")
	_, _ = io.WriteString(w, "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"pong\"}]},\"finishReason\":\"STOP\"}]}\n\n")
}

func (g *recordingGateway) seen() []*http.Request {
	g.mu.Lock()
	defer g.mu.Unlock()
	return slices.Clone(g.calls)
}

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// podModelEnv is a pod that names every kind of model route core-agent's
// library could take: another Gemini endpoint, keys, a Vertex project and
// credentials file, the Vertex switch, and other providers' keys.
func podModelEnv(gateway, stray string) map[string]string {
	return map[string]string{
		modelBaseURLEnv:                  gateway,
		modelAPIKeyEnv:                   "sk-pod",
		coreAgentGatewayURLEnv:           stray,
		"GOOGLE_API_KEY":                 "AIza-pod",
		"GEMINI_API_KEY":                 "gem-pod",
		"GOOGLE_VERTEX_BASE_URL":         stray,
		"GOOGLE_GENAI_USE_VERTEXAI":      "true",
		"GOOGLE_CLOUD_PROJECT":           "proj",
		"GOOGLE_CLOUD_LOCATION":          "us-central1",
		"GOOGLE_APPLICATION_CREDENTIALS": "/var/run/sa.json",
		"CLOUD_ML_REGION":                "us-east5",
		"OPENAI_API_KEY":                 "sk-oai",
		"XAI_API_KEY":                    "xai",
		"A2A_MODEL":                      "gemini-default",
	}
}

// TestACoreAgentWorkerCallsTheGatewayThroughOneProxy: the pod points the
// harness at another endpoint and carries keys and a Vertex project. The
// env the adapter builds (builder, then startHarnessGateway) points the
// harness at the loopback proxy, once, and carries none of the rest. Two
// harness processes in a row on that one env (one per turn) both reach the
// install's gateway through the one proxy on /v1beta/..., the placeholder
// key isn't forwarded, the pod's endpoint is never called, a path off the
// allowlist is refused, and after Close the proxy is gone.
func TestACoreAgentWorkerCallsTheGatewayThroughOneProxy(t *testing.T) {
	gw := &recordingGateway{}
	up := httptest.NewServer(gw)
	t.Cleanup(up.Close)
	stray := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("the pod's own endpoint was called: %s", r.URL.Path)
	}))
	t.Cleanup(stray.Close)

	pod := podModelEnv(up.URL, stray.URL)
	var podEnv []string
	for k, v := range pod {
		podEnv = append(podEnv, k+"="+v)
	}
	podEnv = append(podEnv, "PATH="+os.Getenv("PATH"), "HOME="+t.TempDir())
	started := 0
	start := func(gateway string, log *slog.Logger) (*workeradapter.GatewayProxy, error) {
		started++
		return workeradapter.StartGatewayProxy(gateway, log)
	}
	env, closer, err := startHarnessGateway(persona.CoreAgent, coreAgentBuilder{}.env(podEnv, nil), func(k string) string { return pod[k] }, start, quietLog())
	if err != nil {
		t.Fatal(err)
	}
	proxy := closer.(*workeradapter.GatewayProxy)
	if started != 1 {
		t.Fatalf("started %d proxies, want 1", started)
	}
	if got := envValue(env, coreAgentGatewayURLEnv); got != proxy.URL || !strings.HasPrefix(got, "http://127.0.0.1:") || envCount(env, coreAgentGatewayURLEnv) != 1 {
		t.Errorf("%s=%q (x%d), want the proxy's loopback URL %q once", coreAgentGatewayURLEnv, got, envCount(env, coreAgentGatewayURLEnv), proxy.URL)
	}
	for k := range pod {
		if k != "A2A_MODEL" && k != coreAgentGatewayURLEnv && envCount(env, k) != 0 {
			t.Errorf("the harness's env carries %s", k)
		}
	}

	for turn := 1; turn <= 2; turn++ {
		cmd := exec.Command(os.Args[0], "-test.run=^TestFakeCoreAgentHarness$")
		cmd.Env = append(slices.Clone(env), fakeHarnessEnv+"=1")
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("harness process %d: %v: %s", turn, err, out)
		}
		if !strings.Contains(string(out), "pong") {
			t.Errorf("harness process %d got %q, want the gateway's answer", turn, out)
		}
	}
	calls := gw.seen()
	if len(calls) != 2 {
		t.Fatalf("the gateway saw %d calls, want 2", len(calls))
	}
	for _, r := range calls {
		if r.URL.Path != "/v1beta/models/gemini-default:streamGenerateContent" {
			t.Errorf("gateway path %q", r.URL.Path)
		}
		if k := r.Header.Get("x-goog-api-key"); k != "" {
			t.Errorf("the harness's placeholder key reached the gateway: %q", k)
		}
	}

	resp, err := http.Get(proxy.URL + "/v1/models")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound || len(gw.seen()) != 2 {
		t.Errorf("a path off the allowlist: %d, gateway calls %d; want 404 and none", resp.StatusCode, len(gw.seen()))
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}
	if resp, err := http.Post(proxy.URL+"/v1beta/models/m:generateContent", "application/json", strings.NewReader("{}")); err == nil {
		resp.Body.Close()
		t.Errorf("the proxy still answers after Close: %d", resp.StatusCode)
	}
}

// TestCoreAgentsEnvWithholdsEveryModelRoute: the builder drops every
// variable under the withheld prefixes and keeps the rest, near misses
// included.
func TestCoreAgentsEnvWithholdsEveryModelRoute(t *testing.T) {
	base := []string{"HOME=/h", "A2A_MODEL=m", "GOOGLE_API_KEY=k", "GOOGLE_GEMINI_BASE_URL=http://x", "GEMINI_API_KEY=g",
		"GOOGLE_GENAI_USE_VERTEXAI=1", "CLOUD_ML_REGION=r", "ANTHROPIC_BASE_URL=http://g", "AICORE_CLIENT_SECRET=s",
		"NOT_GOOGLE_X=kept", "GOOGLEX=kept", "PATH=/bin"}
	got := coreAgentBuilder{}.env(slices.Clone(base), nil)
	want := []string{"HOME=/h", "A2A_MODEL=m", "NOT_GOOGLE_X=kept", "GOOGLEX=kept", "PATH=/bin"}
	if !slices.Equal(got, want) {
		t.Errorf("env %q, want %q", got, want)
	}
}

// TestTheHarnessGatewayFollowsClaudesRule: with no ANTHROPIC_BASE_URL the
// proxy forwards to the install's inference-gateway.
func TestTheHarnessGatewayFollowsClaudesRule(t *testing.T) {
	var gotGateway string
	start := func(gateway string, log *slog.Logger) (*workeradapter.GatewayProxy, error) {
		gotGateway = gateway
		return &workeradapter.GatewayProxy{URL: "http://127.0.0.1:9"}, nil
	}
	if _, _, err := startHarnessGateway(persona.CoreAgent, nil, func(string) string { return "" }, start, quietLog()); err != nil {
		t.Fatal(err)
	}
	if gotGateway != defaultModelBaseURL {
		t.Errorf("gateway %q, want %q", gotGateway, defaultModelBaseURL)
	}
}

// TestOtherHarnessesStartNoGatewayProxy: Claude Code reaches the gateway
// itself, and agy has no wiring here; their env comes back as it went in.
func TestOtherHarnessesStartNoGatewayProxy(t *testing.T) {
	start := func(string, *slog.Logger) (*workeradapter.GatewayProxy, error) {
		t.Fatal("a proxy was started")
		return nil, nil
	}
	in := []string{"ANTHROPIC_API_KEY=k", "ANTHROPIC_BASE_URL=http://g"}
	for _, h := range []persona.Harness{persona.ClaudeCode, persona.Antigravity} {
		env, closer, err := startHarnessGateway(h, slices.Clone(in), func(string) string { return "" }, start, quietLog())
		if err != nil || closer != nil || !slices.Equal(env, in) {
			t.Errorf("%s: env %q closer %v err %v, want the env unchanged and no proxy", h, env, closer, err)
		}
	}
}

// TestAProxyThatCannotStartRefusesTheCoreAgentWorker: the error comes back
// with no env.
func TestAProxyThatCannotStartRefusesTheCoreAgentWorker(t *testing.T) {
	start := func(string, *slog.Logger) (*workeradapter.GatewayProxy, error) {
		return nil, errors.New("listen: no ports")
	}
	env, closer, err := startHarnessGateway(persona.CoreAgent, []string{"HOME=/h"}, func(string) string { return "" }, start, quietLog())
	if err == nil || env != nil || closer != nil {
		t.Errorf("env %q closer %v err %v, want a refusal", env, closer, err)
	}
}

// coreAgentWorkerEnv selects the core-agent harness under the waiver.
func coreAgentWorkerEnv(t *testing.T) []string {
	return []string{"TASK_ID=task-wiring", "PROFILE=p", "NATS_URL=nats://127.0.0.1:1", "A2A_WORKDIR=" + t.TempDir(),
		lib.EnvHarness + "=" + string(persona.CoreAgent), lib.EnvHarnessUnisolated + "=" + lib.HarnessUnisolatedAllow, "A2A_HARNESS_PATH=/bin/false"}
}

// TestRunRefusesACoreAgentWorkerWhoseProxyCannotStart: run() exits 1 when
// the gateway URL is one the proxy won't forward to, before it dials the bus.
func TestRunRefusesACoreAgentWorkerWhoseProxyCannotStart(t *testing.T) {
	code, stderr := runChild(t, "", append(coreAgentWorkerEnv(t), modelBaseURLEnv+"=ftp://inference-gateway")...)
	if code != 1 || !strings.Contains(stderr, "the harness's gateway proxy") {
		t.Errorf("exit %d, stderr %q; want 1 and the proxy's refusal", code, stderr)
	}
	if strings.Contains(stderr, "adapter run failed") || strings.Contains(stderr, "nats") {
		t.Errorf("run() went on past the refusal: %q", stderr)
	}
}

// TestRunsConfigCarriesTheProxyToEveryTurn: the Config run() hands to
// workeradapter.Run, built by configFromEnv (harnessEnv, core-agent's
// builder) and then applyHarnessGateway, carries the proxy's URL once and
// none of the pod's model routes.
func TestRunsConfigCarriesTheProxyToEveryTurn(t *testing.T) {
	for _, kv := range coreAgentWorkerEnv(t) {
		k, v, _ := strings.Cut(kv, "=")
		t.Setenv(k, v)
	}
	t.Setenv(lib.EnvProfileWorker, "")
	pod := podModelEnv("http://inference-gateway.ns.svc", "http://elsewhere:1")
	for k, v := range pod {
		t.Setenv(k, v)
	}
	was := activeHarness
	t.Cleanup(func() { activeHarness = was })
	cfg, ok := configFromEnv(quietLog())
	if !ok {
		t.Fatal("configFromEnv refused the core-agent worker")
	}
	var gotGateway string
	start := func(gateway string, log *slog.Logger) (*workeradapter.GatewayProxy, error) {
		gotGateway = gateway
		return &workeradapter.GatewayProxy{URL: "http://127.0.0.1:7"}, nil
	}
	closer, err := applyHarnessGateway(&cfg, os.Getenv, start, quietLog())
	if err != nil || closer == nil {
		t.Fatalf("closer %v err %v", closer, err)
	}
	if gotGateway != "http://inference-gateway.ns.svc" {
		t.Errorf("the proxy forwards to %q, want the pod's base URL", gotGateway)
	}
	if got := envValue(cfg.HarnessEnv, coreAgentGatewayURLEnv); got != "http://127.0.0.1:7" || envCount(cfg.HarnessEnv, coreAgentGatewayURLEnv) != 1 {
		t.Errorf("%s=%q (x%d), want the proxy's URL once", coreAgentGatewayURLEnv, got, envCount(cfg.HarnessEnv, coreAgentGatewayURLEnv))
	}
	for k := range pod {
		if k != "A2A_MODEL" && k != coreAgentGatewayURLEnv && envCount(cfg.HarnessEnv, k) != 0 {
			t.Errorf("the turn's env carries %s", k)
		}
	}
	if envCount(cfg.HarnessEnv, "NATS_URL") != 0 {
		t.Errorf("the turn's env carries NATS_URL")
	}
}

func envValue(env []string, key string) string {
	v := ""
	for _, kv := range env {
		if k, val, ok := strings.Cut(kv, "="); ok && k == key {
			v = val
		}
	}
	return v
}

func envCount(env []string, key string) int {
	n := 0
	for _, kv := range env {
		if k, _, ok := strings.Cut(kv, "="); ok && k == key {
			n++
		}
	}
	return n
}
