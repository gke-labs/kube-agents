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

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"
	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"
	"github.com/slack-go/slack/socketmode"
)

// histogramCount is how many observations one histogram series holds.
func histogramCount(t *testing.T, h prometheus.Observer) uint64 {
	t.Helper()
	var out dto.Metric
	if err := h.(prometheus.Metric).Write(&out); err != nil {
		t.Fatal(err)
	}
	return out.GetHistogram().GetSampleCount()
}

// The adapter's Web API client times each call under its method and outcome,
// counts a 429 under its method, and names a method outside the closed list
// other.
func TestSlackWebAPICallsAreTimedByMethodAndOutcome(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/chat.update"):
			w.Header().Set("Retry-After", "1")
			w.WriteHeader(http.StatusTooManyRequests)
		case strings.HasSuffix(r.URL.Path, "/made.up"):
			w.WriteHeader(http.StatusBadGateway)
		default:
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"ok":true,"channel":"C1","ts":"1.0"}`))
		}
	}))
	t.Cleanup(srv.Close)

	m := NewMetrics()
	held := &atomic.Pointer[Metrics]{}
	held.Store(m)
	client := slackObservedHTTPClient(held)
	for _, method := range []string{"chat.postMessage", "chat.update", "made.up"} {
		resp, err := client.Post(srv.URL+"/api/"+method, "application/json", nil)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
	}

	for _, c := range []struct{ method, outcome string }{
		{"chat.postMessage", slackAPIOK},
		{"chat.update", slackAPIRateLimited},
		{metricsLabelOther, slackAPIError},
	} {
		if got := histogramCount(t, m.slackAPIDuration.WithLabelValues(c.method, c.outcome)); got != 1 {
			t.Errorf("%s/%s observations = %d, want 1", c.method, c.outcome, got)
		}
	}
	if got := counterValue(t, m.slackRateLimited.WithLabelValues("chat.update")); got != 1 {
		t.Errorf("rate-limited chat.update = %v, want 1", got)
	}
	if got := counterValue(t, m.slackRateLimited.WithLabelValues("chat.postMessage")); got != 0 {
		t.Errorf("rate-limited chat.postMessage = %v, want 0", got)
	}

	// A request that gets no response is an error.
	srv.Close()
	if _, err := client.Post(srv.URL+"/api/reactions.add", "application/json", nil); err == nil {
		t.Fatal("a post to a closed server succeeded")
	}
	if got := histogramCount(t, m.slackAPIDuration.WithLabelValues("reactions.add", slackAPIError)); got != 1 {
		t.Errorf("reactions.add/error observations = %d, want 1", got)
	}
}

// The real constructor's client is the observed one, counting once
// SetMetrics hands the counters over and not before.
func TestTheSlackAdaptersClientCountsOnceItHasTheMetrics(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"channel":"C1","ts":"1.0"}`))
	}))
	t.Cleanup(srv.Close)
	a := newSlackAdapter("xoxb-stub", "xapp-stub", slog.Default(), slack.OptionAPIURL(srv.URL+"/"))
	if _, _, err := a.api.PostMessage("C1", slack.MsgOptionText("before", false)); err != nil {
		t.Fatal(err)
	}
	m := NewMetrics()
	a.SetMetrics(m)
	if _, _, err := a.api.PostMessage("C1", slack.MsgOptionText("after", false)); err != nil {
		t.Fatal(err)
	}
	if got := histogramCount(t, m.slackAPIDuration.WithLabelValues("chat.postMessage", slackAPIOK)); got != 1 {
		t.Errorf("chat.postMessage observations = %d, want the one made after SetMetrics", got)
	}
}

// The ingress lag is the arrival time less the event's own Slack timestamp;
// clock skew reads 0 and a timestamp that does not parse is not observed.
func TestSlackIngressLagReadsTheEventsTimestamp(t *testing.T) {
	var none *Metrics
	none.slackIngress(time.Now(), "1.0")

	m := NewMetrics()
	now := time.Unix(1_800_000_010, 0)
	m.slackIngress(now, "1800000007.500000")
	m.slackIngress(now, "1800000020.000000")
	m.slackIngress(now, "not-a-ts")
	m.slackIngress(now, "NaN")
	m.slackIngress(now, "Inf")
	m.slackIngress(now, "")
	var out dto.Metric
	if err := m.slackIngressLag.WithLabelValues().(prometheus.Metric).Write(&out); err != nil {
		t.Fatal(err)
	}
	h := out.GetHistogram()
	if h.GetSampleCount() != 2 || h.GetSampleSum() != 2.5 {
		t.Fatalf("lag count=%d sum=%v, want 2 observations summing to 2.5 (a 2.5s lag and a skewed 0)", h.GetSampleCount(), h.GetSampleSum())
	}
}

// A click's action_ts, inside the action as Slack documents it, is observed
// whatever the click turns out to be.
func TestASlackClickObservesItsIngressLag(t *testing.T) {
	a, _ := newUXSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	a.SetMetrics(m)
	a.interactive(clickPayload(t, "U1", "T1", "C1", "40.0", "kage.link.0", "Docs", "", "1800000000.0"))
	var out dto.Metric
	if err := m.slackIngressLag.WithLabelValues().(prometheus.Metric).Write(&out); err != nil {
		t.Fatal(err)
	}
	if got := out.GetHistogram().GetSampleCount(); got != 1 {
		t.Fatalf("lag observations after a link click = %d, want 1", got)
	}
}

// A message that becomes a turn through the pump is observed once; the bot's
// own post, which the filter drops, is not.
func TestTheSlackPumpObservesATurnsIngressLag(t *testing.T) {
	srv, _ := stalledSlackStub(t)
	a := newTestSlackAdapter(&fakeSlackAPI{})
	m := NewMetrics()
	a.SetMetrics(m)
	a.sm = socketmode.New(slack.New("xoxb-stub", slack.OptionAPIURL(srv.URL+"/")))
	for _, msg := range []*slackevents.MessageEvent{
		slackMsg("im", "D1", "UBOT", "the bot's own post", "499.0", ""),
		slackMsg("im", "D1", "U1", "hello", "500.0", ""),
	} {
		a.sm.Events <- socketmode.Event{
			Type: socketmode.EventTypeEventsAPI,
			Data: slackevents.EventsAPIEvent{Type: slackevents.CallbackEvent, InnerEvent: slackevents.EventsAPIInnerEvent{Data: msg}},
		}
	}
	turned := make(chan struct{}, 1)
	ctx, cancel := context.WithCancel(context.Background())
	returned := make(chan error, 1)
	go func() { returned <- a.Run(ctx, func(InboundMessage) { turned <- struct{}{} }) }()
	select {
	case <-turned:
	case <-time.After(10 * time.Second):
		t.Fatal("the pump never reached the handler")
	}
	cancel()
	<-returned
	var out dto.Metric
	if err := m.slackIngressLag.WithLabelValues().(prometheus.Metric).Write(&out); err != nil {
		t.Fatal(err)
	}
	if got := out.GetHistogram().GetSampleCount(); got != 1 {
		t.Fatalf("lag observations = %d, want the one turn's", got)
	}
}

func TestSlackAPIMethodLabelIsClosed(t *testing.T) {
	for _, c := range []struct{ path, want string }{
		{"/api/chat.postMessage", "chat.postMessage"},
		{"/api/agents.sessions.setStatus", slackSessionsSetStatusMethod},
		{"/api/files.upload", metricsLabelOther},
		{"/actions/T1/123/abc", metricsLabelOther},
	} {
		if got := slackAPIMethodLabel(c.path); got != c.want {
			t.Errorf("slackAPIMethodLabel(%q) = %q, want %q", c.path, got, c.want)
		}
	}
}
