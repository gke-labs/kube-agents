package gateway

import (
	"net/http"
	"path"
	"slices"
	"strconv"
	"sync/atomic"
	"time"
)

// slackAPITransport times every request the Slack adapter's Web API client
// sends, and counts the ones Slack answered 429, under the method the URL
// names (https://slack.com/api/<method>). metrics is the adapter's, read on
// each request, so the client can be built before SetMetrics hands them
// over; unset, nothing is counted.
type slackAPITransport struct {
	base    http.RoundTripper
	metrics *atomic.Pointer[Metrics]
}

// RoundTrip sends req through base and records how it went.
func (t slackAPITransport) RoundTrip(req *http.Request) (*http.Response, error) {
	start := time.Now()
	resp, err := t.base.RoundTrip(req)
	status := 0
	if err == nil {
		status = resp.StatusCode
	}
	t.metrics.Load().slackAPICall(slackAPIMethodLabel(req.URL.Path), status, time.Since(start))
	return resp, err
}

// slackObservedHTTPClient is slackHTTPClient with every request recorded
// under metrics.
func slackObservedHTTPClient(metrics *atomic.Pointer[Metrics]) *http.Client {
	c := slackHTTPClient()
	c.Transport = slackAPITransport{base: http.DefaultTransport, metrics: metrics}
	return c
}

// slackAPIMethodLabel is the method label for a request path: its last
// segment when metricsSlackAPIMethods names it, other otherwise.
func slackAPIMethodLabel(urlPath string) string {
	if method := path.Base(urlPath); slices.Contains(metricsSlackAPIMethods, method) {
		return method
	}
	return metricsLabelOther
}

// slackAPICall records one Web API call: status 0 is a request that got no
// response.
func (m *Metrics) slackAPICall(method string, status int, took time.Duration) {
	if m == nil {
		return
	}
	outcome := slackAPIError
	switch {
	case status == http.StatusTooManyRequests:
		outcome = slackAPIRateLimited
		m.slackRateLimited.WithLabelValues(method).Inc()
	case status >= http.StatusOK && status < http.StatusMultipleChoices:
		outcome = slackAPIOK
	}
	m.slackAPIDuration.WithLabelValues(method, outcome).Observe(took.Seconds())
}

// slackIngress records how long the event stamped ts (a Slack timestamp,
// "1700000000.000100") took to arrive at now. A ts that does not parse is
// not observed; a negative lag is clock skew between Slack and this node,
// observed as 0.
func (m *Metrics) slackIngress(now time.Time, ts string) {
	if m == nil || ts == "" {
		return
	}
	secs, err := strconv.ParseFloat(ts, 64)
	if err != nil || secs <= 0 {
		return
	}
	lag := float64(now.UnixNano())/float64(time.Second) - secs
	m.slackIngressLag.WithLabelValues().Observe(max(lag, 0))
}
