package gateway

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/nats-io/nats.go"
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
	g, url, ctx := newUnprovisionedGateway(t)
	g.feedbackRetry = 10 * time.Millisecond // 30 retries: 300ms
	runGateway(t, ctx, g.Run)

	// Longer than every retry put together, with no TASKS.
	time.Sleep(time.Duration(feedbackRetries+20) * g.feedbackRetry)
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
	g, url, ctx := newUnprovisionedGateway(t)
	g.peopleRetry = 10 * time.Millisecond // 30 retries: 300ms
	runGateway(t, ctx, g.Run)

	time.Sleep(time.Duration(peopleRetries+20) * g.peopleRetry)
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
	runGateway(t, ctx, func(ctx context.Context) error { return g.RunWith(ctx, RunOptions{RelayBindDelay: 2 * time.Second}) })
	waitFor(t, "the bind to be held back", func() bool { return g.relayHeldBack.Load() })
	ready, reason := g.Readiness()
	if ready || !strings.Contains(reason, "held back after a crash start") || strings.Contains(reason, "waiting for stream") {
		t.Fatalf("Readiness while held back = %v %q", ready, reason)
	}
	waitFor(t, "the held-back bind to land", func() bool { r, _ := g.Readiness(); return r })
}

// runGateway runs g until the test ends, then stops it and waits for Run to
// return before the cleanups registered earlier (a restored retry interval,
// say) run, so nothing the run started reads them concurrently.
func runGateway(t *testing.T, ctx context.Context, run func(context.Context) error) {
	t.Helper()
	ctx, cancel := context.WithCancel(ctx)
	ran := make(chan struct{})
	go func() { defer close(ran); _ = run(ctx) }()
	t.Cleanup(func() { cancel(); <-ran })
}

// While the bus is unreachable the relay's wait cannot tell whether TASKS
// exists, so the reason names the bus, not the stream.
func TestReadinessNamesABusOutage(t *testing.T) {
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
	if ready, why := g.Readiness(); ready || !strings.Contains(why, "waiting for stream TASKS") {
		t.Fatalf("Readiness on a reachable bus = %v %q, want the stream wait", ready, why)
	}
	s.Shutdown()
	s.WaitForShutdown()
	deadline := time.Now().Add(10 * time.Second)
	for client.Connected() && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if ready, why := g.Readiness(); ready || !strings.Contains(why, "waiting for the bus") {
		t.Fatalf("Readiness with the bus down = %v %q, want the bus named", ready, why)
	}
}

// TestATermStartWaitsForTheSessionStateBucket: an elected gateway that wins
// the lease before the provision Job has created session-state waits for
// the bucket, reporting the wait through Readiness, rather than ending the
// term before it starts.
func TestATermStartWaitsForTheSessionStateBucket(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	js := r.bus.JetStream()
	if err := js.DeleteKeyValue(ctx, lib.SessionStateBucket); err != nil {
		t.Fatal(err)
	}
	type result struct {
		term uint64
		err  error
	}
	done := make(chan result, 1)
	go func() {
		term, err := r.g.BeginTerm(ctx)
		done <- result{term, err}
	}()
	waitFor(t, "the term wait in Readiness", func() bool {
		_, why := r.g.Readiness()
		return strings.Contains(why, "waiting for bucket "+lib.SessionStateBucket)
	})
	if _, err := js.CreateKeyValue(ctx, jetstream.KeyValueConfig{Bucket: lib.SessionStateBucket}); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-done:
		if got.err != nil || got.term != 1 {
			t.Fatalf("BeginTerm = %d, %v, want term 1", got.term, got.err)
		}
	case <-time.After(15 * time.Second):
		t.Fatal("BeginTerm didn't return once the bucket existed")
	}
	if _, why := r.g.Readiness(); strings.Contains(why, "waiting for bucket") {
		t.Fatalf("Readiness still names the term wait: %q", why)
	}
}

// TestATermStartWaitsOutAJetStreamOutage: with the connection up but
// JetStream not answering (no responders, an API timeout, the meta leader
// moving), a term start waits; any other error still ends it.
func TestATermStartWaitsOutAJetStreamOutage(t *testing.T) {
	g := &Gateway{}
	for name, err := range map[string]error{
		"no responders":           fmt.Errorf("stream info: %w", nats.ErrNoResponders),
		"an api timeout":          fmt.Errorf("stream info: %w", context.DeadlineExceeded),
		"a request timeout":       nats.ErrTimeout,
		"meta leader moving":      &jetstream.APIError{Code: 503, ErrorCode: jetstreamTemporarilyUnavailable, Description: "JetStream system temporarily unavailable"},
		"the bucket not there":    jetstream.ErrBucketNotFound,
		"jetstream not available": jetstream.ErrJetStreamNotEnabled,
	} {
		if !g.termCanWait(err) {
			t.Errorf("%s: a term start gave up on %v", name, err)
		}
	}
	for name, err := range map[string]error{
		"a wrong-type marker": errors.New("term marker: not a number"),
		"another api error":   &jetstream.APIError{Code: 400, ErrorCode: 10052, Description: "invalid request"},
	} {
		if g.termCanWait(err) {
			t.Errorf("%s: a term start waited on %v", name, err)
		}
	}
}

// TestATermStartGivesUpOnItsOwnBus: a term start that can't reach the bus
// waits, naming the bus in Readiness, but only so long: a fault local to
// this pod would otherwise hold the Lease while a standby with a working bus
// stands idle.
func TestATermStartGivesUpOnItsOwnBus(t *testing.T) {
	old := termWaitBusMax
	termWaitBusMax = 3 * time.Second
	t.Cleanup(func() { termWaitBusMax = old })
	r := startRig(t)
	r.client.Close()
	done := make(chan error, 1)
	go func() {
		_, err := r.g.BeginTerm(context.Background())
		done <- err
	}()
	waitFor(t, "the bus wait in Readiness", func() bool {
		_, why := r.g.Readiness()
		return strings.HasPrefix(why, "waiting for the bus: the gateway's term")
	})
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a term start with no bus succeeded")
		}
	case <-time.After(15 * time.Second):
		t.Fatal("the term start never gave up on the bus")
	}
}
