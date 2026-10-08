package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// counterValue reads one counter (client_golang's testutil would add a
// module to go.mod for two helpers).
func counterValue(t *testing.T, c prometheus.Counter) float64 {
	t.Helper()
	var out dto.Metric
	if err := c.Write(&out); err != nil {
		t.Fatal(err)
	}
	return out.GetCounter().GetValue()
}

// seriesCount is how many series the registry serves under one name.
func seriesCount(t *testing.T, m *Metrics, name string) int {
	t.Helper()
	families, err := m.registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range families {
		if f.GetName() == name {
			return len(f.GetMetric())
		}
	}
	return 0
}

// metricsTerminalCount reads one task-terminal series.
func metricsTerminalCount(t *testing.T, m *Metrics, state, source string) float64 {
	t.Helper()
	return counterValue(t, m.taskTerminals.WithLabelValues(state, source))
}

// metricsPullCount reads one gchat pull-outcome series.
func metricsPullCount(t *testing.T, m *Metrics, outcome string) float64 {
	t.Helper()
	return counterValue(t, m.gchatPulls.WithLabelValues(outcome))
}

// publishMetricsTerminal ends origin's task with a final status in state,
// from the executor on its events subject or from the gateway's supervisor
// party on the supervisor subject: the two subjects the relay's durable
// reads and terminalSourceOf tells apart.
func publishMetricsTerminal(t *testing.T, r *rig, origin *lib.Envelope, source TerminalSource, state lib.TaskState) {
	t.Helper()
	ctx := context.Background()
	payload, err := json.Marshal(lib.StatusUpdate{
		TaskID: origin.TaskID, ContextID: origin.ContextID,
		Status: lib.TaskStatus{State: state, Message: &lib.Message{
			Role: "agent", MessageID: "msg-metrics", Parts: []lib.Part{{Kind: "text", Text: "reason: metrics-test - detail"}},
		}},
		Final: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	from, subject := lib.Party{Session: "platform", AgentType: "test-executor"}, lib.TaskEventsSubject("platform", origin.TaskID)
	if source == TerminalFromSupervisor {
		from, subject = gatewayParty, lib.TaskSupervisorSubject("platform", origin.TaskID)
	}
	env, err := lib.NewStatusUpdateEnvelope(from, origin.TaskID, origin.ContextID, origin.CorrelationID, payload)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.bus.Publish(ctx, subject, env); err != nil {
		t.Fatal(err)
	}
}

// scrape GETs url and returns the status and body.
func scrape(t *testing.T, method, url string) (int, string) {
	t.Helper()
	req, err := http.NewRequest(method, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("%s %s: %v", method, url, err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, string(body)
}

// startMetricsServer serves m on an ephemeral loopback port and returns its
// base URL.
func startMetricsServer(t *testing.T, m *Metrics) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv, err := NewMetricsServer(metricsPortMax, m, nil)
	if err != nil {
		t.Fatal(err)
	}
	srv.listener = ln
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-done; err != nil {
			t.Errorf("metrics server: %v", err)
		}
	})
	return "http://" + ln.Addr().String()
}

// TestTaskTerminalsAreCountedByStateAndSource drives one task to each final
// state from each source over the embedded bus, and reads each series move
// by exactly one, then reads the same counts off /metrics.
func TestTaskTerminalsAreCountedByStateAndSource(t *testing.T) {
	r := startRig(t)
	m := r.g.Metrics()
	i := 0
	for _, source := range []TerminalSource{TerminalFromExecutor, TerminalFromSupervisor} {
		for _, state := range []lib.TaskState{lib.StateCompleted, lib.StateFailed, lib.StateCanceled, lib.StateRejected} {
			i++
			conv := fmt.Sprintf("discord:g1/metrics-%s-%s", source, state)
			r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: fmt.Sprintf("m-%d", i), Text: "count me"}
			var origin *lib.Envelope
			waitFor(t, "the task for "+conv, func() bool {
				for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
					if e.Kind == lib.KindMessage && e.ContextID != "" {
						rec, err := r.g.reg.Get(context.Background(), conv)
						if err == nil && rec != nil && rec.ActiveTask != nil && rec.ActiveTask.TaskID == e.TaskID {
							origin = e
							return true
						}
					}
				}
				return false
			})
			publishMetricsTerminal(t, r, origin, source, state)
			waitFor(t, fmt.Sprintf("%s/%s counted", state, source), func() bool {
				return metricsTerminalCount(t, m, string(state), string(source)) == 1
			})
		}
	}
	// Eight terminals, one per series, and nothing anywhere else.
	var total float64
	for _, state := range []string{"completed", "failed", "canceled", "rejected", "other"} {
		for _, source := range []string{"executor", "supervisor", "other"} {
			total += metricsTerminalCount(t, m, state, source)
		}
	}
	if total != 8 {
		t.Errorf("the counter holds %v terminals across its series, want 8", total)
	}

	status, body := scrape(t, http.MethodGet, startMetricsServer(t, m)+MetricsPath)
	if status != http.StatusOK {
		t.Fatalf("GET %s = %d", MetricsPath, status)
	}
	for _, want := range []string{
		`kubeagents_a2a_gateway_task_terminals_total{source="executor",state="completed"} 1`,
		`kubeagents_a2a_gateway_task_terminals_total{source="supervisor",state="rejected"} 1`,
		`kubeagents_a2a_gateway_task_terminals_total{source="other",state="other"} 0`,
		`kubeagents_a2a_gateway_gchat_events_received_total 0`,
		`kubeagents_a2a_gateway_gchat_pulls_total{outcome="empty"} 0`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics does not carry %q:\n%s", want, body)
		}
	}
}

// metricsTerminalTotal is every task-terminal series summed.
func metricsTerminalTotal(t *testing.T, m *Metrics) float64 {
	t.Helper()
	var total float64
	for _, state := range []string{"completed", "failed", "canceled", "rejected", "other"} {
		for _, source := range []string{"executor", "supervisor", "gateway", "gateway-never-started", "other"} {
			total += metricsTerminalCount(t, m, state, source)
		}
	}
	return total
}

// metricsSettle is how long a test waits after a terminal is counted before
// reading the total again, so a second count for the same terminal -- at the
// top of relayTerminal and again where it hands the adapter the end -- has
// time to land and be caught.
const metricsSettle = 300 * time.Millisecond

// TestRelayedTerminalIsCountedOnce: a terminal the relay delivers is counted
// once, though it passes through both relayTerminal and observeTaskTerminal.
func TestRelayedTerminalIsCountedOnce(t *testing.T) {
	r := startRig(t)
	m := r.g.Metrics()
	conv := "discord:g1/metrics-relayed-once"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-once", Text: "count me once"}
	origin := r.awaitTask(t, "platform")
	publishMetricsTerminal(t, r, origin, TerminalFromExecutor, lib.StateFailed)
	waitFor(t, "failed/executor counted", func() bool {
		return metricsTerminalCount(t, m, "failed", "executor") >= 1
	})
	time.Sleep(metricsSettle)
	if got := metricsTerminalTotal(t, m); got != 1 {
		t.Errorf("one relayed terminal counted %v times, want 1", got)
	}
}

// TestHealedStaleTerminalDeliversOnceEvenIfRelayQueued: if the stale-terminal
// heal fires while the relay still has the same terminal queued (the two
// share the session lock), the heal delivers the terminal and drops the
// routing entries; the relay's later delivery must be a no-op so the user
// does not see the answer posted twice and task_terminals_total is not
// incremented twice.
func TestHealedStaleTerminalDeliversOnceEvenIfRelayQueued(t *testing.T) {
	r := startRig(t)
	m := r.g.Metrics()
	conv := "discord:g1/metrics-heal-queued"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-1", Text: "do task"}
	origin := r.awaitTask(t, "platform")

	ctx := context.Background()
	// Hold the session lock to simulate the window where the relay has
	// received the terminal and queued it for this conversation, but
	// handleInbound runs its heal before the relay can process the batch.
	l := r.g.lockSession(conv)
	l.Lock()

	// Executor completes the task: the terminal lands on the TASKS stream
	// and relayEvent enqueues it for the session worker.
	publishMetricsTerminal(t, r, origin, TerminalFromExecutor, lib.StateCompleted)
	// Wait until the relay worker has picked up the batch and is blocked on the session lock.
	waitFor(t, "relay blocked on session lock", func() bool {
		r.g.mu.Lock()
		defer r.g.mu.Unlock()
		entry := r.g.sessionLocks[conv]
		return entry != nil && entry.refcount >= 2
	})

	// While the session lock is still held, the heal runs (e.g. from handleInbound).
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatalf("session record: %v", err)
	}
	r.g.healActiveTask(ctx, rec)
	l.Unlock()

	// Wait for the relay worker to finish.
	waitFor(t, "relay worker finished", func() bool {
		r.g.mu.Lock()
		defer r.g.mu.Unlock()
		return r.g.sessionLocks[conv] == nil
	})
	time.Sleep(metricsSettle)

	if got := metricsTerminalCount(t, m, "completed", "executor"); got != 1 {
		t.Errorf("completed/executor counted %v times, want 1", got)
	}
	if got := metricsTerminalTotal(t, m); got != 1 {
		t.Errorf("total terminals counted = %v, want 1", got)
	}
	posts := r.adapter.postTexts()
	if len(posts) != 2 {
		t.Errorf("adapter saw %d posts %v, want 2 (submission placeholder and one terminal post)", len(posts), posts)
	}
}

// TestBusUnreachableTerminalIsCounted (jayantid's review of #2473): a task
// whose submission never reached the bus ends in the gateway's own failed
// terminal, which bypasses the relay. The user reads "could not reach the
// bus"; the counter must read one failed task, under source gateway, or a
// NATS outage shows zero failures.
func TestBusUnreachableTerminalIsCounted(t *testing.T) {
	r := startRig(t)
	m := r.g.Metrics()
	deleteTasksStream(t, r.url)
	r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/metrics-no-bus", Kind: "group", AuthorID: "1001", MessageID: "m-no-bus", Text: "count me"}
	waitFor(t, "the bus failure posted", func() bool {
		return strings.Contains(strings.Join(r.adapter.editTexts(), "\n"), "could not reach the bus")
	})
	waitFor(t, "failed/gateway counted", func() bool {
		return metricsTerminalCount(t, m, "failed", string(TerminalFromGateway)) >= 1
	})
	time.Sleep(metricsSettle)
	if got := metricsTerminalTotal(t, m); got != 1 {
		t.Errorf("one bus-unreachable terminal counted %v times, want 1", got)
	}
}

// TestNeverStartedHealIsCounted (jayantid's review of #2473): the heal that
// releases a task with no first event inside the grace declares a failed
// terminal that bypasses the relay. It counts once, under
// gateway-never-started.
func TestNeverStartedHealIsCounted(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	m := r.g.Metrics()
	conv := "discord:g1/metrics-never-started"
	seedTasklessDelegate(t, r, conv, defaultFirstEventGrace+time.Minute)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "m-never", Text: "Delegate: write a haiku about otters"}
	waitFor(t, "a fresh delegation spawned", func() bool { return len(spawn.calls()) == 1 })
	waitFor(t, "failed/gateway-never-started counted", func() bool {
		return metricsTerminalCount(t, m, "failed", string(TerminalNeverStarted)) >= 1
	})
	time.Sleep(metricsSettle)
	if got := metricsTerminalTotal(t, m); got != 1 {
		t.Errorf("one never-started heal counted %v times, want 1", got)
	}
}

// TestTaskTerminalLabelsAreClosed: a state or source outside the closed lists
// counts under other, and the series set is the same fixed size whatever is
// counted, so nothing a bus message carries can mint a series.
func TestTaskTerminalLabelsAreClosed(t *testing.T) {
	m := NewMetrics()
	before := seriesCount(t, m, "kubeagents_a2a_gateway_task_terminals_total")
	m.taskTerminal(lib.StateAuthRequired, TerminalSource("task-1234 conversation discord:g1/x"))
	m.taskTerminal(lib.TaskState("failed\nlevel=ERROR"), TerminalFromExecutor)
	m.taskTerminal(lib.StateFailed, TerminalSource("reaper"))
	if got := metricsTerminalCount(t, m, "other", "other"); got != 1 {
		t.Errorf("other/other = %v, want 1", got)
	}
	if got := metricsTerminalCount(t, m, "other", "executor"); got != 1 {
		t.Errorf("other/executor = %v, want 1", got)
	}
	if got := metricsTerminalCount(t, m, "failed", "other"); got != 1 {
		t.Errorf("failed/other = %v, want 1", got)
	}
	if after := seriesCount(t, m, "kubeagents_a2a_gateway_task_terminals_total"); after != before || before != 25 {
		t.Errorf("series before=%d after=%d, want 25 both times (5 states x 5 sources)", before, after)
	}
	// A nil set counts nothing and does not panic: an adapter built without
	// one, as every gchat test builds it.
	var none *Metrics
	none.taskTerminal(lib.StateFailed, TerminalFromExecutor)
	none.gchatPull(gchatPullEvents)
}

// TestGchatPullsAreCountedByOutcome: events, then empty pulls, then refused
// ones, each read off the counter where the summary line counts them.
func TestGchatPullsAreCountedByOutcome(t *testing.T) {
	turn := gchatMsg("spaces/D1", "DIRECT_MESSAGE", "", "", "spaces/D1/messages/M1", "hi", "", "u1@example.com", "HUMAN")
	f := &gchatLegibilityRelay{
		subscription: "projects/p/subscriptions/s",
		events: []map[string]any{
			{"receipt": "r1", "data": b64GchatEvent(t, turn), "messageId": "1"},
			{"receipt": "r2", "data": "not!!!base64", "messageId": "2"},
		},
	}
	srv := f.start(t)
	a, _ := newLegibilityAdapter(t, srv.URL)
	m := NewMetrics()
	a.SetMetrics(m)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- a.Run(ctx, func(InboundMessage) {}) }()
	defer func() {
		cancel()
		<-done
	}()

	waitFor(t, "two events and an empty pull", func() bool {
		return metricsPullCount(t, m, gchatPullEvents) == 2 && metricsPullCount(t, m, gchatPullEmpty) >= 1
	})
	if got := counterValue(t, m.gchatEventsReceived); got != 2 {
		t.Errorf("events received = %v, want 2 (an unparsable payload is still an event received)", got)
	}
	if got := metricsPullCount(t, m, gchatPullFailed); got != 0 {
		t.Errorf("failed pulls = %v before any refusal", got)
	}

	f.mu.Lock()
	f.pullStatus = http.StatusServiceUnavailable
	f.pullBody = `{"error":"a2a chat event pull failed","pubsub":{"type":"PermissionDenied","code":403}}`
	f.mu.Unlock()
	waitFor(t, "a refused pull counted", func() bool { return metricsPullCount(t, m, gchatPullFailed) >= 1 })
	if got := counterValue(t, m.gchatEventsReceived); got != 2 {
		t.Errorf("events received moved to %v on refused pulls", got)
	}
	if got := seriesCount(t, m, "kubeagents_a2a_gateway_gchat_pulls_total"); got != 3 {
		t.Errorf("pull outcomes carry %d series, want 3", got)
	}
}

// TestMetricsAreServedOnTheMetricsPortAlone: the listener serves MetricsPath
// and nothing else, and neither door's listener serves the series at all.
func TestMetricsAreServedOnTheMetricsPortAlone(t *testing.T) {
	m := NewMetrics()
	base := startMetricsServer(t, m)
	if status, body := scrape(t, http.MethodGet, base+MetricsPath); status != http.StatusOK || !strings.Contains(body, "kubeagents_a2a_gateway_task_terminals_total") {
		t.Fatalf("GET %s = %d:\n%s", MetricsPath, status, body)
	}
	for _, path := range []string{"/", "/metrics/", "/healthz", injectPath, a2aCardPath} {
		if status, _ := scrape(t, http.MethodGet, base+path); status != http.StatusNotFound {
			t.Errorf("GET %s on the metrics port = %d, want 404", path, status)
		}
	}
	if status, _ := scrape(t, http.MethodPost, base+MetricsPath); status != http.StatusMethodNotAllowed {
		t.Errorf("POST %s = %d, want 405", MetricsPath, status)
	}

	inject := startInjectRig(t)
	a2a := startA2ARig(t)
	for name, doorBase := range map[string]string{"inject door": inject.base, "A2A door": a2a.base} {
		status, body := scrape(t, http.MethodGet, doorBase+MetricsPath)
		if status == http.StatusOK || strings.Contains(body, "kubeagents_a2a_gateway") {
			t.Errorf("the %s answers GET %s with %d:\n%s", name, MetricsPath, status, body)
		}
	}
}

// TestMetricsServerRefusesAPortOutOfRange: NewMetricsServer is also what an
// embedder reaches, so it holds the range FromEnv holds.
func TestMetricsServerRefusesAPortOutOfRange(t *testing.T) {
	for _, port := range []int{0, -1, metricsPortMax + 1} {
		if _, err := NewMetricsServer(port, NewMetrics(), nil); err == nil {
			t.Errorf("port %d accepted", port)
		}
	}
	if _, err := NewMetricsServer(metricsPortMin, nil, nil); err == nil {
		t.Error("a listener with no metrics accepted")
	}
}

// TestMetricsServerStopsWithItsContext: the listener releases its port when
// the gateway stops, inside the shutdown grace.
func TestMetricsServerStopsWithItsContext(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv, err := NewMetricsServer(metricsPortMax, NewMetrics(), nil)
	if err != nil {
		t.Fatal(err)
	}
	srv.listener = ln
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- srv.Run(ctx) }()
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Run returned %v on shutdown", err)
		}
	case <-time.After(metricsShutdownGrace + time.Second):
		t.Fatal("Run did not return after its context ended")
	}
}

// TestFromEnvReadsTheMetricsPort: unset is no listener, a port in range is
// read, and junk, out-of-range values and a door's own port, in any spelling
// net.Listen reads as that port, refuse the boot.
func TestFromEnvReadsTheMetricsPort(t *testing.T) {
	cases := []struct {
		name, value, inject, door string
		want                      int
		refused                   bool
	}{
		{name: "unset", value: "", want: 0},
		{name: "blank", value: "  ", want: 0},
		{name: "the operator's port", value: "9096", want: 9096},
		{name: "padded", value: " 9096 ", want: 9096},
		{name: "junk", value: "metrics", refused: true},
		{name: "zero", value: "0", refused: true},
		{name: "negative", value: "-1", refused: true},
		{name: "past the range", value: "65536", refused: true},
		{name: "the inject door's port", value: "8099", inject: "127.0.0.1:8099", refused: true},
		{name: "the A2A door's port", value: "8098", door: "127.0.0.1:8098", refused: true},
		{name: "beside both doors", value: "9096", inject: "127.0.0.1:8099", door: "127.0.0.1:8098", want: 9096},
		// net.Listen reads a door's port the way net.LookupPort does, not as
		// the literal string, so every spelling it binds to the metrics port
		// is a collision. Each was admitted when the check compared strings.
		{name: "the inject door's port zero-padded", value: "9096", inject: "127.0.0.1:09096", refused: true},
		{name: "the inject door's port signed", value: "9096", inject: "127.0.0.1:+9096", refused: true},
		{name: "the inject door's port signed and padded", value: "9096", inject: "127.0.0.1:+09096", refused: true},
		{name: "the A2A door's port zero-padded", value: "8098", door: "127.0.0.1:008098", refused: true},
		{name: "the A2A door's port as a service name", value: "80", door: "127.0.0.1:http", refused: true},
		// A port net.Listen cannot read either is the door's own boot failure
		// when it binds, not a collision with this listener.
		{name: "beside a door port that cannot bind", value: "9096", inject: "127.0.0.1:-9096", want: 9096},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			setBaseEnv(t)
			t.Setenv(metricsPortEnv, tc.value)
			if tc.inject != "" {
				t.Setenv("A2A_INJECT_LISTEN", tc.inject)
				t.Setenv("A2A_INJECT_TOKEN", "tok")
			}
			if tc.door != "" {
				t.Setenv("A2A_DOOR_LISTEN", tc.door)
				t.Setenv("A2A_DOOR_TOKEN", "tok")
			}
			cfg, err := FromEnv()
			if tc.refused {
				if err == nil || !strings.Contains(err.Error(), metricsPortEnv) {
					t.Fatalf("FromEnv = %v, %v; want a refusal naming %s", cfg, err, metricsPortEnv)
				}
				return
			}
			if err != nil {
				t.Fatalf("FromEnv: %v", err)
			}
			if cfg.MetricsPort != tc.want {
				t.Errorf("MetricsPort = %d, want %d", cfg.MetricsPort, tc.want)
			}
		})
	}
}

// TestMetricsListenerCapsItsConnections: with metricsMaxConnections idle
// connections open, a scrape waits rather than adding one more; once they
// close it is served. The cap is what keeps a peer on the pod network from
// spending the gateway's memory one idle connection at a time.
func TestMetricsListenerCapsItsConnections(t *testing.T) {
	base := startMetricsServer(t, NewMetrics())
	addr := strings.TrimPrefix(base, "http://")
	var held []net.Conn
	for range metricsMaxConnections {
		c, err := net.Dial("tcp", addr)
		if err != nil {
			t.Fatal(err)
		}
		held = append(held, c)
	}
	// Give the server time to accept every held connection, so the slots
	// are taken before the scrape below arrives.
	time.Sleep(200 * time.Millisecond)
	quick := &http.Client{Timeout: 500 * time.Millisecond}
	if resp, err := quick.Get(base + MetricsPath); err == nil {
		_ = resp.Body.Close()
		t.Fatalf("a scrape past %d open connections was served (%d); the listener has no cap", metricsMaxConnections, resp.StatusCode)
	}
	for _, c := range held {
		_ = c.Close()
	}
	waitFor(t, "a scrape once the held connections close", func() bool {
		resp, err := quick.Get(base + MetricsPath)
		if err != nil {
			return false
		}
		_ = resp.Body.Close()
		return resp.StatusCode == http.StatusOK
	})
}
