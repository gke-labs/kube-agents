package gateway

import (
	"errors"
	"net/http"
	"slices"
	"strings"
	"time"
)

// gchatAPIMethodSeparator joins a Chat API call's resource path and method
// into the name the Chat API reference gives it (spaces.messages.create).
const gchatAPIMethodSeparator = "."

// gchatAPIMethodLabel is the method label for one Chat API call: its
// resource path and method joined by dots when metricsGchatAPIMethods names
// it, other otherwise.
func gchatAPIMethodLabel(resource []string, method string) string {
	name := strings.Join(append(slices.Clone(resource), method), gchatAPIMethodSeparator)
	if slices.Contains(metricsGchatAPIMethods, name) {
		return name
	}
	return metricsLabelOther
}

// gchatCarriedStatus is the Chat API's own status for one call, read from
// the error the relay's answer became (the relay answers such a refusal 502
// with the Chat status in its body), or 0 when the answer carried none.
func gchatCarriedStatus(err error) int {
	var api *gchatAPIError
	if errors.As(err, &api) {
		return api.Status
	}
	return 0
}

// gchatAPICall records one Chat API call made through the relay.
// relayStatus is the relay's own HTTP status, 0 for a call that got no
// response; chatStatus is the Chat API's as the relay carried it, 0 when it
// carried none. A rate limit is the Chat API's 429 alone: a 429 the relay
// answered itself says nothing of Chat's quota and reads error. The duration
// runs to the end of the relay's response body.
func (m *Metrics) gchatAPICall(method string, relayStatus, chatStatus int, took time.Duration) {
	if m == nil {
		return
	}
	outcome := gchatCallError
	switch {
	case chatStatus == http.StatusTooManyRequests:
		outcome = gchatCallRateLimited
		m.gchatRateLimited.WithLabelValues(method).Inc()
	case chatStatus == 0 && relayStatus == http.StatusOK:
		outcome = gchatCallOK
	}
	m.gchatAPIDuration.WithLabelValues(method, outcome).Observe(took.Seconds())
}
