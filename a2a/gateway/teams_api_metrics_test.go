package gateway

import (
	"errors"
	"net/http"
	"testing"
	"time"
)

func TestTeamsAPIMethodLabelNamesTheOperationFromTheVerbAndEndpoint(t *testing.T) {
	conv := teamsConversationsPath + "19:abc@thread.tacv2" + teamsActivitiesSuffix
	for _, c := range []struct {
		method, endpoint, want string
	}{
		{http.MethodPost, conv, teamsAPIMethodCreate},
		{http.MethodPut, conv + "/1794603936403", teamsAPIMethodUpdate},
		{http.MethodDelete, conv + "/1794603936403", teamsAPIMethodDelete},
		{http.MethodPost, conv + "/1794603936403", metricsLabelOther}, // a POST under an activity is not a create
		{http.MethodPut, conv, metricsLabelOther},                     // a PUT on the collection is nothing
		{http.MethodGet, conv, metricsLabelOther},
		{http.MethodPost, "v3/conversations", metricsLabelOther},
		{http.MethodPost, "v3/teams/x/activities", metricsLabelOther},
	} {
		if got := teamsAPIMethodLabel(c.method, c.endpoint); got != c.want {
			t.Errorf("%s %s: got %q, want %q", c.method, c.endpoint, got, c.want)
		}
	}
}

func TestTeamsAPICallOutcomesAndTheRateLimitCounter(t *testing.T) {
	m := NewMetrics()
	// The broker returned Microsoft's response: ok.
	m.teamsAPICall(teamsAPIMethodCreate, http.StatusOK, 0, 40*time.Millisecond)
	// The broker answered 502 carrying Microsoft's 429: rate limited, and counted.
	m.teamsAPICall(teamsAPIMethodUpdate, http.StatusBadGateway, http.StatusTooManyRequests, 120*time.Millisecond)
	// The broker answered 502 carrying a 404: error, not rate limited.
	m.teamsAPICall(teamsAPIMethodUpdate, http.StatusBadGateway, http.StatusNotFound, 30*time.Millisecond)
	// No response at all: error.
	m.teamsAPICall(teamsAPIMethodCreate, 0, 0, 5*time.Second)
	// A 429 the broker answered itself says nothing of Microsoft's quota: error.
	m.teamsAPICall(teamsAPIMethodCreate, http.StatusTooManyRequests, 0, time.Millisecond)
	for _, c := range []struct {
		method, outcome string
		want            uint64
	}{
		{teamsAPIMethodCreate, teamsCallOK, 1},
		{teamsAPIMethodUpdate, teamsCallRateLimited, 1},
		{teamsAPIMethodUpdate, teamsCallError, 1},
		{teamsAPIMethodCreate, teamsCallError, 2},
	} {
		if got := histogramCount(t, m.teamsAPIDuration.WithLabelValues(c.method, c.outcome)); got != c.want {
			t.Errorf("%s/%s: %d observations, want %d", c.method, c.outcome, got, c.want)
		}
	}
	if got := counterValue(t, m.teamsRateLimited.WithLabelValues(teamsAPIMethodUpdate)); got != 1 {
		t.Errorf("rate limited update: %v, want 1", got)
	}
	if got := counterValue(t, m.teamsRateLimited.WithLabelValues(teamsAPIMethodCreate)); got != 0 {
		t.Errorf("rate limited create: %v, want 0", got)
	}
	// A nil Metrics counts nothing and does not panic.
	var none *Metrics
	none.teamsAPICall(teamsAPIMethodCreate, http.StatusOK, 0, time.Millisecond)
	none.teamsPull(teamsPullEvents)
	none.teamsIngress(time.Now(), time.Now().Format(time.RFC3339Nano))
}

func TestTeamsRelayErrorCarriesMicrosoftsStatus(t *testing.T) {
	err := teamsRelayError("teams: relay /v1/chat/teams/api answered 502",
		[]byte(`{"error":"Teams operation failed","detail":"HTTP 429: TooManyRequests","teams":{"status":429,"code":"TooManyRequests"}}`))
	if got := teamsCarriedStatus(err); got != http.StatusTooManyRequests {
		t.Fatalf("carried status %d, want 429 from %v", got, err)
	}
	if err.Error() != "teams: relay /v1/chat/teams/api answered 502: Teams operation failed HTTP 429: TooManyRequests" {
		t.Fatalf("text: %q", err.Error())
	}
	plain := teamsRelayError("teams: relay answered 503", []byte(`{"error":"Teams relay unavailable"}`))
	if teamsCarriedStatus(plain) != 0 {
		t.Fatalf("a broker-side refusal carried a status: %v", plain)
	}
	var up *teamsUpstreamError
	if errors.As(plain, &up) {
		t.Fatal("a broker-side refusal became an upstream error")
	}
	if teamsCarriedStatus(teamsRelayError("teams: relay answered 500", nil)) != 0 {
		t.Fatal("an empty body carried a status")
	}
}

func TestTeamsIngressObservesTheActivitysTimestampAndClampsSkew(t *testing.T) {
	m := NewMetrics()
	now := time.Date(2026, 11, 23, 15, 0, 10, 0, time.UTC)
	m.teamsIngress(now, now.Add(-2*time.Second).Format(time.RFC3339Nano))
	m.teamsIngress(now, now.Add(30*time.Second).Format(time.RFC3339Nano)) // skew: observed as 0
	m.teamsIngress(now, "")                                               // nothing
	m.teamsIngress(now, "yesterday")                                      // nothing
	if got := histogramCount(t, m.teamsIngressLag.WithLabelValues()); got != 2 {
		t.Fatalf("%d observations, want 2", got)
	}
}
