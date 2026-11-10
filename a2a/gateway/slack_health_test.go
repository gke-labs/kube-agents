package gateway

import (
	"context"
	"net/http"
	"strings"
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
// is up, and the last event's time once one arrives.
func TestTheSlackGaugesServeNoSampleUntilAnAdapterRuns(t *testing.T) {
	var none *Metrics
	none.slackDown(time.Now())
	none.slackUp()
	none.slackEvent(time.Now())
	if got := none.slackDownFor(time.Now()); got != 0 {
		t.Fatalf("nil metrics slackDownFor = %v", got)
	}

	m := NewMetrics()
	url := startMetricsServer(t, m)
	_, body := scrape(t, http.MethodGet, url+MetricsPath)
	if strings.Contains(body, "slack_") {
		t.Fatalf("a Slack sample with no Slack adapter:\n%s", body)
	}

	m.slackDown(time.Now())
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
	at := time.Unix(1_800_000_000, 0)
	m.slackEvent(at)
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
}

// A drop keeps the time it began across the reconnect attempts that follow,
// so the readiness grace runs from the drop, not from the latest dial.
func TestASlackDropKeepsTheTimeItBegan(t *testing.T) {
	m := NewMetrics()
	start := time.Unix(1_800_000_000, 0)
	m.slackUp()
	m.slackDown(start)
	m.slackDown(start.Add(time.Minute))
	if got := m.slackDownFor(start.Add(3 * time.Minute)); got != 3*time.Minute {
		t.Fatalf("down for %v, want 3m", got)
	}
	m.slackUp()
	if got := m.slackDownFor(start.Add(4 * time.Minute)); got != 0 {
		t.Fatalf("down for %v once up, want 0", got)
	}
}

// The pump turns socketmode's connection events into the gauge: down from
// Run's start, up on connected, an event's time on an Events API delivery,
// down on the reconnect's connecting, and down once Run has returned.
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

	unblock()
	select {
	case <-returned:
	case <-time.After(10 * time.Second):
		t.Fatal("Run never returned")
	}
	if v, ok := gaugeValue(t, m, slackConnectedSeries); !ok || v != 0 {
		t.Fatalf("slack_connected after Run returned = %v, %v; want 0", v, ok)
	}
}

// Readiness ignores a drop inside slackReadyGrace, reports one past it with
// its length, and reads ready again once the socket is back.
func TestReadinessReportsASlackDropPastTheGrace(t *testing.T) {
	g := &Gateway{metrics: NewMetrics()}
	g.relayBound.Store(true)
	if ready, reason := g.Readiness(); !ready {
		t.Fatalf("no Slack adapter: not ready, %q", reason)
	}

	g.metrics.slackDown(time.Now())
	if ready, reason := g.Readiness(); !ready {
		t.Fatalf("a fresh drop: not ready, %q", reason)
	}

	g.metrics.slackUp()
	g.metrics.slackDown(time.Now().Add(-slackReadyGrace - time.Minute))
	ready, reason := g.Readiness()
	if ready || !strings.Contains(reason, "Slack Socket Mode connection has been down for 3m") {
		t.Fatalf("a drop past the grace: %v %q", ready, reason)
	}

	g.metrics.slackUp()
	if ready, reason := g.Readiness(); !ready {
		t.Fatalf("reconnected: not ready, %q", reason)
	}
}
