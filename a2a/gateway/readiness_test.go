package gateway

import (
	"context"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go/jetstream"
)

// ReadyzPath answers the readiness it is given, both ways, with the reason
// as the body, and only to GET.
func TestReadyzAnswersTheReadinessWithItsReason(t *testing.T) {
	var ready atomic.Bool
	base := startMetricsServerWith(t, NewMetrics(), func() (bool, string) {
		if ready.Load() {
			return true, "ready"
		}
		return false, "waiting for stream TASKS: the event relay durable gateway-relay has not bound yet"
	})
	if status, body := scrape(t, http.MethodGet, base+ReadyzPath); status != http.StatusServiceUnavailable ||
		!strings.Contains(body, "waiting for stream TASKS") {
		t.Fatalf("GET %s while waiting = %d %q, want 503 with the reason", ReadyzPath, status, body)
	}
	ready.Store(true)
	if status, body := scrape(t, http.MethodGet, base+ReadyzPath); status != http.StatusOK || strings.TrimSpace(body) != "ready" {
		t.Fatalf("GET %s once ready = %d %q, want 200 ready", ReadyzPath, status, body)
	}
	if status, _ := scrape(t, http.MethodPost, base+ReadyzPath); status != http.StatusMethodNotAllowed {
		t.Errorf("POST %s = %d, want 405", ReadyzPath, status)
	}
}

// A gateway started before the provision Job has created TASKS waits for it
// instead of exiting: not ready, with the reason on /readyz, Run still
// running, and ready once the stream exists, with no restart in between.
// newUnprovisionedGateway builds a gateway on an embedded server that has no
// TASKS stream and no buckets yet, as a gateway started before the provision
// Job has run sees it, and returns it with the server's URL.
func newUnprovisionedGateway(t *testing.T) (*Gateway, string, context.Context) {
	t.Helper()
	s := startServer(t)
	url := s.ClientURL()

	mapFile := filepath.Join(t.TempDir(), "principal-map")
	if err := os.WriteFile(mapFile, []byte("1001 test:bnaylor\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	client, err := lib.Connect(ctx, url, lib.WithName("gateway-test"), lib.WithAgreementPolicy(SupervisorAgreement(nil)))
	if err != nil {
		t.Fatalf("gateway client: %v", err)
	}
	t.Cleanup(client.Close)
	g, err := New(Options{Client: client, Adapter: newFakeAdapter(), Backend: "discord", Config: &Config{
		NATSURL: url, PrincipalMapPath: mapFile, DefaultAddressee: "platform",
		IdleTTL: 30 * time.Minute, AttributionSalt: []byte("test-salt"),
	}})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	return g, url, ctx
}

func TestGatewayWaitsForTheTasksStreamAndIsNotReadyUntilItBinds(t *testing.T) {
	g, url, ctx := newUnprovisionedGateway(t)
	base := startMetricsServerWith(t, g.Metrics(), g.Readiness)

	ran := make(chan error, 1)
	go func() { ran <- g.Run(ctx) }()

	// Several seconds of no stream: Run keeps waiting and the gateway says why.
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		select {
		case err := <-ran:
			t.Fatalf("Run returned while TASKS did not exist: %v", err)
		case <-time.After(250 * time.Millisecond):
		}
		if ready, _ := g.Readiness(); ready {
			t.Fatal("the gateway reported ready before its relay could bind")
		}
	}
	status, body := scrape(t, http.MethodGet, base+ReadyzPath)
	if status != http.StatusServiceUnavailable || !strings.Contains(body, "waiting for stream TASKS") || !strings.Contains(body, relayDurable) {
		t.Fatalf("GET %s while waiting = %d %q, want 503 naming the stream and the durable", ReadyzPath, status, body)
	}

	provision(t, url)
	waitFor(t, "the gateway to become ready once TASKS exists", func() bool {
		ready, _ := g.Readiness()
		return ready
	})
	if status, _ := scrape(t, http.MethodGet, base+ReadyzPath); status != http.StatusOK {
		t.Errorf("GET %s once bound = %d, want 200", ReadyzPath, status)
	}
	select {
	case err := <-ran:
		t.Fatalf("Run returned after binding: %v", err)
	default:
	}
}

// The relay is the subscription that waits for its stream. A relay without
// WaitForStream would pass the test above whenever TASKS appeared inside
// lib's 45s bind window, so the flag is held here directly.
func TestTheRelayWaitsForItsStream(t *testing.T) {
	g := &Gateway{relayDurable: relayDurable}
	cfg := g.relaySubscribeConfig()
	if !cfg.WaitForStream {
		t.Fatal("the event relay's subscription does not wait for TASKS; a slow provision Job restarts the gateway")
	}
	if cfg.Stream != lib.TasksStream || cfg.Durable != relayDurable {
		t.Errorf("relay subscription = %s/%s, want %s/%s", cfg.Stream, cfg.Durable, lib.TasksStream, relayDurable)
	}
}

// The feedback bucket comes from the same provision Job as TASKS, so a
// gateway that waited for TASKS longer than its feedback retries last must
// still look for the bucket once the relay binds, not have given up while it
// waited.
func TestFeedbackRetriesStartOnceTheRelayBinds(t *testing.T) {
	old := feedbackRetryInterval
	feedbackRetryInterval = 10 * time.Millisecond // 30 retries: 300ms
	t.Cleanup(func() { feedbackRetryInterval = old })
	g, url, ctx := newUnprovisionedGateway(t)
	go func() { _ = g.Run(ctx) }()

	// Longer than every retry put together, with no TASKS.
	time.Sleep(time.Duration(feedbackRetries+20) * feedbackRetryInterval)
	provision(t, url)
	waitFor(t, "the relay to bind", func() bool { ready, _ := g.Readiness(); return ready })
	if g.feedback.Load() != nil {
		t.Fatal("feedback is on before its bucket exists")
	}
	withFeedbackBucket(t, time.Hour)(&Config{NATSURL: url})
	waitFor(t, "answer feedback to turn on once its bucket exists", func() bool { return g.feedback.Load() != nil })
}

// The people bucket comes from the same provision Job as TASKS, so its
// retries start once the relay binds too, as feedback's do.
func TestPeopleRetriesStartOnceTheRelayBinds(t *testing.T) {
	old := peopleRetryInterval
	peopleRetryInterval = 10 * time.Millisecond // 30 retries: 300ms
	t.Cleanup(func() { peopleRetryInterval = old })
	g, url, ctx := newUnprovisionedGateway(t)
	go func() { _ = g.Run(ctx) }()

	time.Sleep(time.Duration(peopleRetries+20) * peopleRetryInterval)
	provision(t, url)
	waitFor(t, "the relay to bind", func() bool { ready, _ := g.Readiness(); return ready })
	if g.people.Load() != nil {
		t.Fatal("the people count is on before its bucket exists")
	}
	createPeopleBucket(t, url, jetstream.KeyValueConfig{History: 1, TTL: time.Hour})
	waitFor(t, "the people count to turn on once its bucket exists", func() bool { return g.people.Load() != nil })
}

// A start after a crash holds the bind back on purpose; readiness says so
// rather than blaming the stream, and turns ready once the held bind lands.
func TestReadinessNamesAHeldBackBind(t *testing.T) {
	g, url, ctx := newUnprovisionedGateway(t)
	provision(t, url)
	go func() { _ = g.RunWith(ctx, RunOptions{RelayBindDelay: 2 * time.Second}) }()
	waitFor(t, "the bind to be held back", func() bool { return g.relayHeldBack.Load() })
	ready, reason := g.Readiness()
	if ready || !strings.Contains(reason, "held back after a crash start") || strings.Contains(reason, "waiting for stream") {
		t.Fatalf("Readiness while held back = %v %q", ready, reason)
	}
	waitFor(t, "the held-back bind to land", func() bool { r, _ := g.Readiness(); return r })
}
