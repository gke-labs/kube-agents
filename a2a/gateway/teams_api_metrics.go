package gateway

import (
	"errors"
	"net/http"
	"strings"
	"time"
)

// The Bot Framework operations the Teams adapter makes through the broker's
// passthrough, as the method label names them.
const (
	teamsAPIMethodCreate = "conversations.activities.create"
	teamsAPIMethodUpdate = "conversations.activities.update"
	teamsAPIMethodDelete = "conversations.activities.delete"
)

// teamsAPIMethodLabel is the method label for one passthrough call: the
// Bot Framework operation the HTTP method and endpoint spell (a POST to a
// conversation's activities creates one, a PUT to an activity updates it, a
// DELETE removes it), other for anything else. The endpoint is the
// adapter's own, never a user's text, and the label set is closed either way.
func teamsAPIMethodLabel(httpMethod, endpoint string) string {
	path := strings.TrimSuffix(endpoint, "/")
	if !strings.HasPrefix(path, teamsConversationsPath) {
		return metricsLabelOther
	}
	onActivities := strings.HasSuffix(path, teamsActivitiesSuffix)
	underActivities := !onActivities && strings.Contains(path, teamsActivitiesSuffix+"/")
	switch {
	case httpMethod == http.MethodPost && onActivities:
		return teamsAPIMethodCreate
	case httpMethod == http.MethodPut && underActivities:
		return teamsAPIMethodUpdate
	case httpMethod == http.MethodDelete && underActivities:
		return teamsAPIMethodDelete
	}
	return metricsLabelOther
}

// teamsUpstreamError is a broker answer that carried Microsoft's own status
// for the call (the broker answers such a refusal 502 with the status under
// teams.status in its body), as distinct from the broker being down or
// refusing the caller. Its text is the relay error's.
type teamsUpstreamError struct {
	Status int
	Err    error
}

func (e *teamsUpstreamError) Error() string { return e.Err.Error() }
func (e *teamsUpstreamError) Unwrap() error { return e.Err }

// teamsCarriedStatus is Microsoft's status for one passthrough call, read
// from the error the broker's answer became, or 0 when it carried none.
func teamsCarriedStatus(err error) int {
	var up *teamsUpstreamError
	if errors.As(err, &up) {
		return up.Status
	}
	return 0
}

// teamsAPICall records one Bot Framework call made through the broker's
// passthrough. relayStatus is the broker's own HTTP status, 0 for a call
// that got no response; msStatus is Microsoft's as the broker carried it, 0
// when it carried none. A rate limit is Microsoft's 429 alone: a 429 the
// broker answered itself says nothing of Microsoft's quota and reads error.
func (m *Metrics) teamsAPICall(method string, relayStatus, msStatus int, took time.Duration) {
	if m == nil {
		return
	}
	outcome := teamsCallError
	switch {
	case msStatus == http.StatusTooManyRequests:
		outcome = teamsCallRateLimited
		m.teamsRateLimited.WithLabelValues(method).Inc()
	case msStatus == 0 && relayStatus == http.StatusOK:
		outcome = teamsCallOK
	}
	m.teamsAPIDuration.WithLabelValues(method, outcome).Observe(took.Seconds())
}

// teamsIngress records how long the activity stamped ts (Bot Framework's
// RFC 3339 timestamp) took to arrive at now. A ts that is empty or does not
// parse is not observed; a negative lag is clock skew between Microsoft and
// this node, observed as 0.
func (m *Metrics) teamsIngress(now time.Time, ts string) {
	if m == nil || strings.TrimSpace(ts) == "" {
		return
	}
	stamped, err := time.Parse(time.RFC3339Nano, strings.TrimSpace(ts))
	if err != nil {
		return
	}
	m.teamsIngressLag.WithLabelValues().Observe(max(now.Sub(stamped).Seconds(), 0))
}
