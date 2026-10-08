package main

import (
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
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/gateway"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// startTestServerReadyTimeout bounds how long a test waits for the embedded
// nats-server to accept connections before failing loudly instead of hanging.
const startTestServerReadyTimeout = 10 * time.Second

// unreachableNATSURL is a loopback port nothing listens on. lib.Connect
// dials once with no retry-on-failed-connect, so a refused port fails the
// dial immediately rather than waiting out a timeout.
const unreachableNATSURL = "nats://127.0.0.1:1"

// TestMain clears every door knob for the package: the cases below pin what
// changes which FromEnv error fires, and a developer's exported door pair
// (what running the gateway locally with a door needs) would otherwise skip
// the "no chat backend" refusal or add a token refusal before the dial. One
// place, so the next knob cannot reopen this per test.
func TestMain(m *testing.M) {
	for _, k := range []string{
		"A2A_DOOR_LISTEN", "A2A_DOOR_TOKEN", "A2A_DOOR_PRINCIPAL_MAP", "A2A_DOOR_PUBLIC_URL",
		"A2A_INJECT_LISTEN", "A2A_INJECT_TOKEN", "A2A_INJECT_PRINCIPAL_MAP",
		"A2A_SPAWN_SESSIONS", "A2A_SESSION_SERVICE_ACCOUNT", "A2A_DEFAULT_ADDRESSEE",
	} {
		os.Unsetenv(k)
	}
	os.Exit(m.Run())
}

// realMain's first call is gateway.FromEnv, and every case below is refused
// there, so none of them dials NATS. Each case pins A2A_CHAT_DISPLAY_MODE to
// empty because FromEnv validates it before NATS_URL: a CI environment with
// a stray value there would otherwise change which error fires. The Slack
// pair is cleared for the same reason — it arms a third backend, so a stray
// SLACK_BOT_TOKEN turns a one-backend case into a two-backend refusal.
func TestRealMainRefusesBadConfigBeforeDialing(t *testing.T) {
	cases := []struct {
		name string
		env  map[string]string
		want string
	}{
		{
			name: "NATS_URL empty",
			env:  map[string]string{"NATS_URL": "", "DISCORD_TOKEN": "tok"},
			want: "NATS_URL",
		},
		{
			name: "two backends set",
			env: map[string]string{
				"NATS_URL":            "nats://127.0.0.1:1",
				"DISCORD_TOKEN":       "tok",
				"A2A_GCHAT_RELAY_URL": "http://relay",
			},
			// The refusal names what is armed, so an operator reading it
			// knows which variable to unset.
			want: "more than one chat backend is configured",
		},
		{
			name: "slack and discord set",
			env: map[string]string{
				"NATS_URL":        "nats://127.0.0.1:1",
				"DISCORD_TOKEN":   "tok",
				"SLACK_BOT_TOKEN": "xoxb-tok",
				"SLACK_APP_TOKEN": "xapp-tok",
			},
			want: "more than one chat backend is configured (the SLACK_BOT_TOKEN+SLACK_APP_TOKEN pair, DISCORD_TOKEN)",
		},
		{
			name: "half a slack pair",
			env: map[string]string{
				"NATS_URL":        "nats://127.0.0.1:1",
				"SLACK_BOT_TOKEN": "xoxb-tok",
			},
			want: "SLACK_BOT_TOKEN and SLACK_APP_TOKEN arm Slack together",
		},
		{
			// The door is a side door: beside one real backend it is
			// accepted, so the refusal here is the two BACKENDS, and the
			// message must not send the reader to unset the door.
			name: "two backends with the door open as well",
			env: map[string]string{
				"NATS_URL":            "nats://127.0.0.1:1",
				"DISCORD_TOKEN":       "tok",
				"A2A_GCHAT_RELAY_URL": "http://relay",
				"A2A_INJECT_LISTEN":   ":8099",
				"A2A_INJECT_TOKEN":    "s3cret",
			},
			want: "more than one chat backend is configured",
		},
		{
			// A door with no token never arms. The fence in front of it does
			// not govern the port-forward its caller uses, so there is no
			// unauthenticated mode to fall back to.
			name: "the door armed without a token",
			env: map[string]string{
				"NATS_URL":          "nats://127.0.0.1:1",
				"DISCORD_TOKEN":     "tok",
				"A2A_INJECT_LISTEN": ":8099",
			},
			want: "A2A_INJECT_TOKEN",
		},
		{
			name: "no backend set",
			env: map[string]string{
				"NATS_URL":            "nats://127.0.0.1:1",
				"DISCORD_TOKEN":       "",
				"A2A_GCHAT_RELAY_URL": "",
			},
			want: "no chat backend",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
			t.Setenv("NATS_URL", "")
			t.Setenv("DISCORD_TOKEN", "")
			t.Setenv("A2A_GCHAT_RELAY_URL", "")
			t.Setenv("SLACK_BOT_TOKEN", "")
			t.Setenv("SLACK_APP_TOKEN", "")
			t.Setenv("A2A_INJECT_LISTEN", "")
			t.Setenv("A2A_INJECT_TOKEN", "")
			for k, v := range tc.env {
				t.Setenv(k, v)
			}
			log := slog.New(slog.NewJSONHandler(io.Discard, nil))
			err := realMain(context.Background(), log)
			if err == nil {
				t.Fatalf("realMain returned nil, want an error naming %q", tc.want)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("realMain error %q, want it to name %q", err, tc.want)
			}
		})
	}
}

// The dial is the first thing after FromEnv that can fail, and its error
// has to come back out of realMain as the failure exit rather than a clean
// zero. SESSION_KV_SALT is set because FromEnv refuses an empty
// NATS_PASSWORD without a salt, which would stop the case at config.
func TestRealMainReturnsDialFailure(t *testing.T) {
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
	t.Setenv("NATS_URL", unreachableNATSURL)
	t.Setenv("NATS_USER", "")
	t.Setenv("NATS_PASSWORD", "")
	t.Setenv("SESSION_KV_SALT", "test-salt")
	t.Setenv("DISCORD_TOKEN", "tok")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	t.Setenv("SLACK_BOT_TOKEN", "")
	t.Setenv("SLACK_APP_TOKEN", "")
	log := slog.New(slog.NewJSONHandler(io.Discard, nil))
	err := realMain(context.Background(), log)
	if !errors.Is(err, nats.ErrNoServers) {
		t.Fatalf("realMain against %s returned %v, want a wrapped nats.ErrNoServers", unreachableNATSURL, err)
	}
	if got := run(); got != exitFailure {
		t.Errorf("run() = %d, want %d", got, exitFailure)
	}
}

// A config error is the one path a test can reach without a bus, and run
// must report it as the failure exit rather than a clean zero.
func TestRunExitsNonZeroOnConfigError(t *testing.T) {
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
	t.Setenv("NATS_URL", "")
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("A2A_GCHAT_RELAY_URL", "")
	t.Setenv("SLACK_BOT_TOKEN", "")
	t.Setenv("SLACK_APP_TOKEN", "")
	if got := run(); got != exitFailure {
		t.Errorf("run() = %d, want %d", got, exitFailure)
	}
}

// startTestServer starts an embedded, no-auth nats-server on a random port
// for buildAdapters tests that need a real bus but no gateway config.
func startTestServer(t *testing.T) *natsserver.Server {
	return startTestServerWithOptions(t, false)
}

func startJetStreamTestServer(t *testing.T) *natsserver.Server {
	return startTestServerWithOptions(t, true)
}

func startTestServerWithOptions(t *testing.T, jetStream bool) *natsserver.Server {
	t.Helper()
	opts := &natsserver.Options{
		Host:      "127.0.0.1",
		Port:      -1,
		JetStream: jetStream,
		NoLog:     true,
		NoSigs:    true,
		StoreDir:  t.TempDir(),
	}
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go s.Start()
	if !s.ReadyForConnections(startTestServerReadyTimeout) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(s.Shutdown)
	return s
}

// fakePrimary is a gateway.Adapter stand-in for buildAdapters tests: it
// proves the primary backend is still reachable through the mux without
// standing up a real Discord or Google Chat adapter.
type fakePrimary struct{ posts []string }

func newFakePrimary() *fakePrimary { return &fakePrimary{} }

func (f *fakePrimary) Run(ctx context.Context, handler func(gateway.InboundMessage)) error {
	<-ctx.Done()
	return ctx.Err()
}

func (f *fakePrimary) Post(conversation, text string) (string, error) {
	f.posts = append(f.posts, conversation)
	return "1", nil
}

func (f *fakePrimary) Edit(conversation, messageID, text string) error {
	return nil
}

func (f *fakePrimary) Roster(conversation string) ([]string, bool, error) {
	return nil, true, nil
}

func (f *fakePrimary) OpenDirect(userID string) (string, error) {
	return "", nil
}

// TestBuildAdaptersIncludesTheConsole proves buildAdapters wires both the
// configured chat backend and the console adapter behind one mux: a post to
// either backend's prefix must reach it (spec-chatops-gateway.md, "The
// console adapter").
func TestBuildAdaptersIncludesTheConsole(t *testing.T) {
	s := startTestServer(t)
	cfg := &gateway.Config{NATSURL: s.ClientURL(), DiscordToken: "x"}
	primary := newFakePrimary()
	m, err := buildAdapters(cfg, primary, nil, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := m.Post("console:tab-1", "hi"); err != nil {
		t.Errorf("console not wired: %v", err)
	}
	if _, err := m.Post("discord:g/c", "hi"); err != nil {
		t.Errorf("primary not wired: %v", err)
	}
}

// TestComposeAdaptersKeepsTheDoorOnTop builds the stack realMain drives with
// both doors in it, the case nothing else exercises. The inject door has to
// sit above the mux: the gateway finds its probe and observers by type
// assertion on the top of the stack, and the mux has no key for an inject:
// conversation. Both shapes are covered, the door beside a real backend and
// the inject-only eval install, where the console is the only chat backend.
func TestComposeAdaptersKeepsTheDoorOnTop(t *testing.T) {
	s := startTestServer(t)
	for _, tc := range []struct {
		name    string
		cfg     *gateway.Config
		primary *fakePrimary
	}{
		{"beside discord", &gateway.Config{NATSURL: s.ClientURL(), DiscordToken: "x", InjectListen: "127.0.0.1:0", InjectToken: "token"}, newFakePrimary()},
		{"inject only", &gateway.Config{NATSURL: s.ClientURL(), InjectListen: "127.0.0.1:0", InjectToken: "token"}, nil},
		{"both doors beside discord", &gateway.Config{NATSURL: s.ClientURL(), DiscordToken: "x", InjectListen: "127.0.0.1:0", InjectToken: "token", A2ADoorListen: "127.0.0.1:0", A2ADoorToken: "token"}, newFakePrimary()},
		{"both doors alone", &gateway.Config{NATSURL: s.ClientURL(), InjectListen: "127.0.0.1:0", InjectToken: "token", A2ADoorListen: "127.0.0.1:0", A2ADoorToken: "token"}, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			door, err := gateway.NewInjectAdapter(tc.cfg.InjectListen, tc.cfg.InjectToken, time.Second, slog.Default())
			if err != nil {
				t.Fatal(err)
			}
			var primary gateway.Adapter
			if tc.primary != nil {
				primary = tc.primary
			}
			doors := []gateway.DoorSpec{gateway.InjectDoorSpec(door)}
			if tc.cfg.A2ADoorArmed() {
				a2a, err := gateway.NewA2ADoor(tc.cfg.A2ADoorListen, tc.cfg.A2ADoorToken, gateway.A2ADoorOptions{DefaultAddressee: "platform"})
				if err != nil {
					t.Fatal(err)
				}
				doors = append(doors, gateway.A2ADoorSpec(a2a))
			}
			a, err := composeAdapters(tc.cfg, primary, doors, nil, slog.Default())
			if err != nil {
				t.Fatal(err)
			}
			if _, ok := a.(gateway.ProbeSink); !ok {
				t.Error("the door's ProbeSink is hidden: the gateway cannot hand it the probe")
			}
			if _, ok := a.(gateway.TaskObserver); !ok {
				t.Error("the door's TaskObserver is hidden: its requests never see their task")
			}
			if _, ok := a.(gateway.InboundObserver); !ok {
				t.Error("the door's InboundObserver is hidden: its drops and turn ends go unreported")
			}
			id, err := a.Post("inject:case-1", "hi")
			if err != nil || !strings.HasPrefix(id, "inj-") {
				t.Errorf("inject post = %q, %v; want the door's own message id", id, err)
			}
			if _, err := a.Post("console:tab-1", "hi"); err != nil {
				t.Errorf("console not wired: %v", err)
			}
			if tc.cfg.A2ADoorArmed() {
				id, err := a.Post("a2a:caller:ctx-1", "hi")
				if err != nil || !strings.HasPrefix(id, "a2a-") {
					t.Errorf("a2a post = %q, %v; want the A2A door's own message id", id, err)
				}
			}
			if tc.primary != nil {
				if _, err := a.Post("discord:g/c", "hi"); err != nil {
					t.Errorf("primary not wired: %v", err)
				}
				if len(tc.primary.posts) != 1 || tc.primary.posts[0] != "discord:g/c" {
					t.Errorf("primary saw posts %v, want only discord:g/c", tc.primary.posts)
				}
			}
		})
	}
}

// syncBuffer is an io.Writer a logger on another goroutine can write to
// while the test reads it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// alertErr returns the err attribute of the first metricsUnavailableAlert
// record in a JSON log, and whether there is one.
func alertErr(t *testing.T, logged string) (string, bool) {
	t.Helper()
	for _, line := range strings.Split(strings.TrimSpace(logged), "\n") {
		if line == "" {
			continue
		}
		var rec struct {
			Level string `json:"level"`
			Msg   string `json:"msg"`
			Err   string `json:"err"`
		}
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("log line %q is not JSON: %v", line, err)
		}
		if rec.Msg == metricsUnavailableAlert && rec.Level == "ERROR" {
			return rec.Err, true
		}
	}
	return "", false
}

// A port that will not bind costs the gateway its metrics, not the
// conversations: serve must log the ALERT line and still run the gateway,
// and return what the gateway returns rather than the listener's error.
// The port case holds the metrics port with a listener of the test's own on
// every interface, as NewMetricsServer binds, so Run's net.Listen fails with
// EADDRINUSE on its goroutine. The constructor case reaches the other
// swallowed error, a port NewMetricsServer refuses.
func TestServeKeepsTheGatewayWhenTheMetricsListenerCannotStart(t *testing.T) {
	held, err := net.Listen("tcp", ":0")
	if err != nil {
		t.Fatalf("holding a port: %v", err)
	}
	t.Cleanup(func() { held.Close() })
	heldPort := held.Addr().(*net.TCPAddr).Port

	for _, tc := range []struct {
		name    string
		port    int
		wantErr string
	}{
		{"port already bound", heldPort, fmt.Sprintf("metrics listener on :%d", heldPort)},
		{"port the constructor refuses", 70000, "metrics listener port 70000 is not in"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var logged syncBuffer
			log := slog.New(slog.NewJSONHandler(&logged, nil))
			cfg := &gateway.Config{MetricsPort: tc.port}

			// The stub gateway runs until the ALERT is logged (or 10s pass),
			// so the listener's failure lands while the gateway is running,
			// which is the production order; then it returns nil, as gw.Run
			// does on a clean shutdown, and the checks below say which half
			// is missing.
			reached := false
			run := func(context.Context) error {
				reached = true
				deadline := time.Now().Add(10 * time.Second)
				for time.Now().Before(deadline) {
					if _, ok := alertErr(t, logged.String()); ok {
						break
					}
					time.Sleep(10 * time.Millisecond)
				}
				return nil
			}

			if err := serve(ctx, cfg, gateway.NewMetrics(), log, run); err != nil {
				t.Fatalf("serve returned %v, want nil: a metrics listener that cannot start must not end the gateway", err)
			}
			if !reached {
				t.Fatal("serve returned without running the gateway")
			}
			got, ok := alertErr(t, logged.String())
			if !ok {
				t.Fatalf("no %q line logged; log:\n%s", metricsUnavailableAlert, logged.String())
			}
			if !strings.Contains(got, tc.wantErr) {
				t.Errorf("ALERT err = %q, want it to name %q", got, tc.wantErr)
			}
		})
	}
}

func provisionTasksStream(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url)
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if _, err := js.CreateOrUpdateStream(ctx, jetstream.StreamConfig{
		Name:      lib.TasksStream,
		Subjects:  []string{"a2a.tasks.>"},
		Retention: jetstream.LimitsPolicy,
		MaxAge:    72 * time.Hour,
	}); err != nil {
		t.Fatalf("create TASKS: %v", err)
	}
}

func freePort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("free port: %v", err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// TestRealMainWiresMetrics proves realMain threads one Metrics instance across
// the adapter, the gateway, and the metrics server: pulls performed by the
// adapter are reflected on the /metrics listener served by realMain.
func TestRealMainWiresMetrics(t *testing.T) {
	s := startJetStreamTestServer(t)
	url := s.ClientURL()
	provisionTasksStream(t, url)

	relaySrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/v1/chat/a2a/events" && r.Method == http.MethodGet {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"subscription":"projects/p/subscriptions/s"}`))
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(relaySrv.Close)

	tokenFile := filepath.Join(t.TempDir(), "chat-token")
	if err := os.WriteFile(tokenFile, []byte("test-token"), 0o600); err != nil {
		t.Fatal(err)
	}

	metricsPort := freePort(t)

	t.Setenv("NATS_URL", url)
	t.Setenv("SESSION_KV_SALT", "test-salt")
	t.Setenv("A2A_GCHAT_RELAY_URL", relaySrv.URL)
	t.Setenv("A2A_GCHAT_TOKEN_PATH", tokenFile)
	t.Setenv("A2A_METRICS_PORT", fmt.Sprintf("%d", metricsPort))
	t.Setenv("A2A_CHAT_DISPLAY_MODE", "")
	t.Setenv("DISCORD_TOKEN", "")
	t.Setenv("SLACK_BOT_TOKEN", "")
	t.Setenv("SLACK_APP_TOKEN", "")
	t.Setenv("A2A_SPAWN_SESSIONS", "")
	t.Setenv("A2A_SESSION_SERVICE_ACCOUNT", "")
	t.Setenv("A2A_DEFAULT_ADDRESSEE", "")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	errCh := make(chan error, 1)
	go func() {
		errCh <- realMain(ctx, slog.Default())
	}()

	metricsURL := fmt.Sprintf("http://127.0.0.1:%d/metrics", metricsPort)
	client := &http.Client{Timeout: 1 * time.Second}

	deadline := time.Now().Add(10 * time.Second)
	found := false
	metricPrefix := "kubeagents_a2a_gateway_gchat_pulls_total{outcome=\"empty\"} "
	for time.Now().Before(deadline) {
		select {
		case err := <-errCh:
			t.Fatalf("realMain exited early while polling /metrics: %v", err)
		default:
		}
		resp, err := client.Get(metricsURL)
		if err == nil {
			body, _ := io.ReadAll(resp.Body)
			resp.Body.Close()
			bodyStr := string(body)
			if resp.StatusCode == http.StatusOK && strings.Contains(bodyStr, metricPrefix) && !strings.Contains(bodyStr, metricPrefix+"0\n") {
				found = true
				break
			}
		}
		select {
		case err := <-errCh:
			t.Fatalf("realMain exited early while polling /metrics: %v", err)
		case <-time.After(25 * time.Millisecond):
		}
	}

	cancel()
	select {
	case err := <-errCh:
		if err != nil && !errors.Is(err, context.Canceled) {
			t.Fatalf("realMain returned unexpected error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("realMain did not exit after context cancellation")
	}

	if !found {
		t.Fatal("timed out waiting for /metrics to reflect adapter pulls")
	}
}
