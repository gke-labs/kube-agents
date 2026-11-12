package gateway

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	"github.com/slack-go/slack/socketmode"
)

// gaugeValue reads one unlabelled gauge from the registry, and whether it
// served a sample at all.
func gaugeValue(t *testing.T, m *Metrics, name string) (float64, bool) {
	t.Helper()
	families, err := m.registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range families {
		if f.GetName() == name && len(f.GetMetric()) == 1 {
			return f.GetMetric()[0].GetGauge().GetValue(), true
		}
	}
	return 0, false
}

const (
	slackConnectedSeries = "kubeagents_a2a_gateway_slack_connected"
	slackLastEventSeries = "kubeagents_a2a_gateway_slack_last_event_timestamp_seconds"
)

// The Slack gauges serve nothing until a Slack adapter runs, so a gateway on
// another backend adds no series; then 0 while the socket is down, 1 once it
// is up, the last event's time once one arrives, and nothing again once the
// adapter has stopped with its context.
func TestTheSlackGaugesServeNoSampleUnlessAnAdapterRuns(t *testing.T) {
	var none *Metrics
	none.slackDown()
	none.slackUp()
	none.slackEvent(time.Now())
	none.slackStopped()

	m := NewMetrics()
	url := startMetricsServer(t, m)
	_, body := scrape(t, http.MethodGet, url+MetricsPath)
	if strings.Contains(body, "slack_") {
		t.Fatalf("a Slack sample with no Slack adapter:\n%s", body)
	}

	m.slackDown()
	if v, ok := gaugeValue(t, m, slackConnectedSeries); !ok || v != 0 {
		t.Fatalf("slack_connected after a drop = %v, %v; want 0", v, ok)
	}
	if _, ok := gaugeValue(t, m, slackLastEventSeries); ok {
		t.Fatal("a last-event sample before any event")
	}

	m.slackUp()
	if v, _ := gaugeValue(t, m, slackConnectedSeries); v != 1 {
		t.Fatalf("slack_connected once up = %v; want 1", v)
	}
	m.slackEvent(time.Unix(1_800_000_000, 0))
	if v, ok := gaugeValue(t, m, slackLastEventSeries); !ok || v != 1_800_000_000 {
		t.Fatalf("last event = %v, %v; want 1800000000", v, ok)
	}
	_, body = scrape(t, http.MethodGet, url+MetricsPath)
	for _, want := range []string{
		"# TYPE " + slackConnectedSeries + " gauge",
		slackConnectedSeries + " 1",
		"# TYPE " + slackLastEventSeries + " gauge",
	} {
		if !strings.Contains(body, want) {
			t.Errorf("body lacks %q:\n%s", want, body)
		}
	}

	m.slackStopped()
	_, body = scrape(t, http.MethodGet, url+MetricsPath)
	if strings.Contains(body, "slack_") {
		t.Fatalf("a Slack sample after the adapter stopped:\n%s", body)
	}
	m.slackDown()
	if v, ok := gaugeValue(t, m, slackConnectedSeries); !ok || v != 0 {
		t.Fatalf("slack_connected on a fresh run = %v, %v; want 0", v, ok)
	}
	if _, ok := gaugeValue(t, m, slackLastEventSeries); ok {
		t.Fatal("a fresh run serves the previous run's last event")
	}
}

// The pump turns socketmode's connection events into the gauge: down from
// Run's start, up on connected, an event's time on an Events API delivery,
// down on the reconnect's connecting, and still down once Run has returned
// on a failure.
func TestTheSlackPumpReportsTheConnection(t *testing.T) {
	srv, unblock := stalledSlackStub(t)
	a := newTestSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	a.SetMetrics(m)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))

	// Queued ahead of anything RunContext sends, so when the handler sees
	// the message the pump has read exactly these two.
	a.sm.Events <- socketmode.Event{Type: socketmode.EventTypeConnected}
	a.sm.Events <- socketmode.Event{
		Type: socketmode.EventTypeEventsAPI,
		Data: slackevents.EventsAPIEvent{
			Type:       slackevents.CallbackEvent,
			InnerEvent: slackevents.EventsAPIInnerEvent{Data: slackMsg("im", "D1", "U1", "hello", "500.0", "")},
		},
	}
	type reading struct {
		up, event bool
	}
	seen := make(chan reading, 1)
	handler := func(InboundMessage) {
		up, _ := gaugeValue(t, m, slackConnectedSeries)
		_, event := gaugeValue(t, m, slackLastEventSeries)
		seen <- reading{up: up == 1, event: event}
	}

	returned := make(chan error, 1)
	go func() { returned <- a.Run(context.Background(), handler) }()
	select {
	case got := <-seen:
		if !got.up || !got.event {
			t.Fatalf("at the message: connected=%v event=%v; want both", got.up, got.event)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the pump never reached the handler")
	}
	// RunContext's own connecting, behind the two above, reads as a drop.
	waitFor(t, "the reconnect to read as down", func() bool {
		v, _ := gaugeValue(t, m, slackConnectedSeries)
		return v == 0
	})

	// invalid_auth: Run fails with its caller's context still live.
	unblock()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned")
	}
	if v, ok := gaugeValue(t, m, slackConnectedSeries); !ok || v != 0 {
		t.Fatalf("slack_connected after Run failed = %v, %v; want 0", v, ok)
	}
}

// A token Slack rejects does not take the gateway down: behind the mux the
// Slack backend is contained and run again after a backoff, and between those
// runs the gauge stays armed at 0, so the slack-disconnected alert has a
// sample to read for as long as the token is bad.
func TestARejectedTokenKeepsTheGaugeAtZeroAcrossTheMuxsRestarts(t *testing.T) {
	var opens atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		opens.Add(1)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":false,"error":"invalid_auth"}`))
	}))
	t.Cleanup(srv.Close)
	sl := newTestSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	sl.SetMetrics(m)
	sl.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))
	mux, err := NewMultiAdapter(slackBackend, consoleBackend, map[string]Adapter{slackBackend: sl, consoleBackend: newFakeAdapter()}, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	mux.restartBase, mux.restartMax = time.Millisecond, 5*time.Millisecond

	ctx, cancel := context.WithCancel(context.Background())
	returned := make(chan error, 1)
	go func() { returned <- mux.Run(ctx, func(InboundMessage) {}) }()
	waitFor(t, "several rejected dials", func() bool { return opens.Load() >= 5 })
	for range 20 {
		if v, ok := gaugeValue(t, m, slackConnectedSeries); !ok || v != 0 {
			t.Fatalf("slack_connected between restarts = %v, %v; want an armed 0", v, ok)
		}
		time.Sleep(time.Millisecond)
	}
	select {
	case err := <-returned:
		t.Fatalf("the mux returned on a rejected Slack token: %v", err)
	default:
	}
	cancel()
	<-returned
}

// A Run ended by its caller's context, as a replica's is when it loses the
// lease, disarms the gauges, so a standby serves no Slack sample.
func TestASlackRunEndedByItsContextDisarmsTheGauges(t *testing.T) {
	srv, _ := stalledSlackStub(t)
	a := newTestSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	a.SetMetrics(m)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))
	a.sm.Events <- socketmode.Event{Type: socketmode.EventTypeConnected}

	ctx, cancel := context.WithCancel(context.Background())
	returned := make(chan error, 1)
	go func() { returned <- a.Run(ctx, func(InboundMessage) {}) }()
	waitFor(t, "the gauge to be served", func() bool {
		_, ok := gaugeValue(t, m, slackConnectedSeries)
		return ok
	})
	cancel()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned")
	}
	if v, ok := gaugeValue(t, m, slackConnectedSeries); ok {
		t.Fatalf("slack_connected after the lease was lost = %v; want no sample", v)
	}
}

// Before the socket's first event, while auth.test is still out, the gauge is
// already armed at 0: a stalled auth.test reads as down, not as no sample.
func TestAStalledAuthTestReadsAsDown(t *testing.T) {
	srv, unblock := stalledSlackStub(t)
	a := newTestSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	a.SetMetrics(m)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))

	returned := make(chan error, 1)
	go func() { returned <- a.Run(context.Background(), func(InboundMessage) {}) }()
	waitFor(t, "the gauge to be armed while auth.test is parked", func() bool {
		_, ok := gaugeValue(t, m, slackConnectedSeries)
		return ok
	})
	if v, _ := gaugeValue(t, m, slackConnectedSeries); v != 0 {
		t.Fatalf("slack_connected while auth.test is parked = %v; want 0", v)
	}
	unblock()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned")
	}
}

// An interactive envelope (a button tap) is Slack traffic as much as a
// message is, so it stamps the last event's time on its own.
func TestAnInteractiveEnvelopeStampsTheLastEvent(t *testing.T) {
	srv, _ := stalledSlackStub(t)
	a := newTestSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	a.SetMetrics(m)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))
	a.sm.Events <- socketmode.Event{Type: socketmode.EventTypeInteractive}

	ctx, cancel := context.WithCancel(context.Background())
	returned := make(chan error, 1)
	go func() { returned <- a.Run(ctx, func(InboundMessage) {}) }()
	waitFor(t, "the interactive envelope to stamp the last event", func() bool {
		v, ok := gaugeValue(t, m, slackLastEventSeries)
		return ok && v > 0
	})
	cancel()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned")
	}
}
