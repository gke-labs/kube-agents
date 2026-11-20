package gateway

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// Each Chat API call through the relay is timed under its method and
// outcome; a 429 the relay carries from the Chat API behind its own 502 is
// counted under its method, and a method outside the closed list is other.
func TestGchatAPICallsAreTimedByMethodAndOutcome(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1"}
	f.responses["spaces.messages/patch"] = fakeChatAPIError{status: http.StatusTooManyRequests, reason: "Too Many Requests"}
	f.responses["spaces.members/list"] = fakeChatAPIError{status: http.StatusForbidden, reason: "Forbidden"}
	a := newTestGchatAdapterWithRelay(t, f)
	m := NewMetrics()
	a.SetMetrics(m)

	if err := a.apiCall([]string{"spaces", "messages"}, "create", nil, nil); err != nil {
		t.Fatal(err)
	}
	for _, c := range []struct {
		resource []string
		method   string
	}{
		{[]string{"spaces", "messages"}, "patch"},
		{[]string{"spaces", "members"}, "list"},
		// No response is set for it, so the fake relay answers 502 with no
		// Chat status.
		{[]string{"spaces", "made"}, "up"},
	} {
		if err := a.apiCall(c.resource, c.method, nil, nil); err == nil {
			t.Fatalf("%v.%s succeeded, want a refusal", c.resource, c.method)
		}
	}

	for _, c := range []struct{ method, outcome string }{
		{"spaces.messages.create", gchatCallOK},
		{"spaces.messages.patch", gchatCallRateLimited},
		{"spaces.members.list", gchatCallError},
		{metricsLabelOther, gchatCallError},
	} {
		if got := histogramCount(t, m.gchatAPIDuration.WithLabelValues(c.method, c.outcome)); got != 1 {
			t.Errorf("%s/%s observations = %d, want 1", c.method, c.outcome, got)
		}
	}
	if got := counterValue(t, m.gchatRateLimited.WithLabelValues("spaces.messages.patch")); got != 1 {
		t.Errorf("rate-limited spaces.messages.patch = %v, want 1", got)
	}
	if got := counterValue(t, m.gchatRateLimited.WithLabelValues("spaces.messages.create")); got != 0 {
		t.Errorf("rate-limited spaces.messages.create = %v, want 0", got)
	}
	if got := seriesCount(t, m, "kubeagents_a2a_gateway_gchat_api_request_duration_seconds"); got != 4 {
		t.Errorf("the duration histogram carries %d series, want the 4 observed", got)
	}

	// A call that gets no response is an error.
	f.srv.Close()
	if err := a.apiCall([]string{"spaces"}, "setup", nil, nil); err == nil {
		t.Fatal("a call to a closed relay succeeded")
	}
	if got := histogramCount(t, m.gchatAPIDuration.WithLabelValues("spaces.setup", gchatCallError)); got != 1 {
		t.Errorf("spaces.setup/error observations = %d, want 1", got)
	}
}

// A 429 the relay answers on its own, with no Chat status in the body, says
// nothing of Chat's quota: it is an error, and the rate-limit count stays.
func TestGchatAPICallReadsTheRelaysOwn429AsAnError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusTooManyRequests)
	}))
	t.Cleanup(srv.Close)
	a := newTestGchatAdapter(t)
	a.relayURL = srv.URL
	m := NewMetrics()
	a.SetMetrics(m)
	if err := a.apiCall([]string{"spaces", "messages"}, "get", nil, nil); err == nil {
		t.Fatal("a call the relay answered 429 succeeded")
	}
	if got := counterValue(t, m.gchatRateLimited.WithLabelValues("spaces.messages.get")); got != 0 {
		t.Errorf("rate-limited spaces.messages.get = %v, want 0", got)
	}
	if got := histogramCount(t, m.gchatAPIDuration.WithLabelValues("spaces.messages.get", gchatCallError)); got != 1 {
		t.Errorf("spaces.messages.get/error observations = %d, want 1", got)
	}
}

// Every method the adapter calls has a name in the closed list, and anything
// else, whatever its text, is other.
func TestGchatAPIMethodLabelIsAClosedList(t *testing.T) {
	for _, c := range []struct {
		resource []string
		method   string
		want     string
	}{
		{[]string{"spaces", "messages"}, "create", "spaces.messages.create"},
		{[]string{gchatSpacesResource, gchatMessagesResource}, gchatPatchMethod, "spaces.messages.patch"},
		{[]string{gchatSpacesResource, gchatMessagesResource}, gchatGetMethod, "spaces.messages.get"},
		{[]string{"spaces", "members"}, "list", "spaces.members.list"},
		{[]string{"spaces"}, "findDirectMessage", "spaces.findDirectMessage"},
		{[]string{"spaces"}, "setup", "spaces.setup"},
		{[]string{"spaces", "messages"}, "delete", metricsLabelOther},
		{[]string{"spaces/S1/messages/M1"}, "create", metricsLabelOther},
		{nil, "", metricsLabelOther},
	} {
		if got := gchatAPIMethodLabel(c.resource, c.method); got != c.want {
			t.Errorf("gchatAPIMethodLabel(%v, %q) = %q, want %q", c.resource, c.method, got, c.want)
		}
	}
}

// An adapter never given the metrics, and the event pull's ack, observe
// nothing.
func TestGchatAPIMetricsCountNothingUnsetOrOffTheAPIRoute(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1"}
	a := newTestGchatAdapterWithRelay(t, f)
	if err := a.apiCall([]string{"spaces", "messages"}, "create", nil, nil); err != nil {
		t.Fatal(err)
	}
	var none *Metrics
	none.gchatAPICall("spaces.messages.create", http.StatusOK, 0, 0)

	m := NewMetrics()
	a.SetMetrics(m)
	a.settle("receipt-1")
	if got := seriesCount(t, m, "kubeagents_a2a_gateway_gchat_api_request_duration_seconds"); got != 0 {
		t.Errorf("the duration histogram carries %d series after an ack alone, want 0", got)
	}
	if got := seriesCount(t, m, "kubeagents_a2a_gateway_gchat_api_rate_limited_total"); got != 0 {
		t.Errorf("the rate-limited counter carries %d series after an ack alone, want 0", got)
	}
}
