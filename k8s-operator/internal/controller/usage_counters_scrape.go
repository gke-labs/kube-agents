// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package controller

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"strings"
	"syscall"
	"time"

	dto "github.com/prometheus/client_model/go"
	"github.com/prometheus/common/expfmt"
	"github.com/prometheus/common/model"
)

// The scrape behind status.usage's counters: one GET of a pod's metrics
// listener over the pod network, read line by line and kept nowhere. The port
// is held by a listener in a pod that runs other containers, so what answers
// is input, not the operator's own data; every bound here is sized on that.

const (
	// usageScrapeTimeout bounds one scrape from connect to the last byte.
	usageScrapeTimeout = 15 * time.Second
	// usageScrapeMaxLineBytes is the one bound on a body: a line past it is a
	// failed scrape. Far above any line either listener writes, a name, five
	// short labels and a number, and the only memory a scrape holds, since
	// the body is folded as it is read. A bound on the number of lines would
	// be a bound on the watcher family's cardinality, which grows with the
	// fleet for the life of the process and cannot be sized.
	usageScrapeMaxLineBytes = 64 * 1024
	// usageScrapeLineBuffer is the scanner's initial buffer, grown up to the
	// line bound as needed.
	usageScrapeLineBuffer = 4 * 1024
	// usageScrapeMaxHeaderBytes bounds the response headers the transport
	// buffers before the body is read, for the same reason the body is read
	// a line at a time: what answers on the port is input, and Go's default
	// would hold ten mebibytes of headers from it.
	usageScrapeMaxHeaderBytes = 16 * 1024
	usageMetricsPath          = "/metrics"
	usageScrapeScheme         = "http://"

	// The series the poller reads, and the gauge both listeners export so that
	// a restart is seen whatever the sample did.
	toolInvocationsSeries  = "kubeagents_tool_invocations_total"
	eventsInjectedSeries   = "k8s_event_watcher_events_injected_total"
	processStartTimeSeries = "process_start_time_seconds"
	// toolInvocationsStatusLabel is the broker's outcome label. The outcomes
	// toolInvocationsCountedStatuses sums are the broker's success and error:
	// the commands it ran and the requests it rejected or failed on before
	// running. blocked and busy are refusals, and abandoned cannot say
	// whether the command had started.
	toolInvocationsStatusLabel = "status"

	// The kinds a failed scrape is logged as: a closed vocabulary, never a
	// byte the peer sent. The connection kinds are classified from the
	// dial error rather than copied from it, because net/http's own error
	// text quotes the status line and header lines it could not parse.
	usageScrapeKindConnect     = "connect"
	usageScrapeKindRefused     = "connection refused"
	usageScrapeKindTimeout     = "timeout"
	usageScrapeKindUnreachable = "unreachable"
	usageScrapeKindMalformed   = "malformed response"
	usageScrapeKindStatus      = "status"
	usageScrapeKindRead        = "read"
	usageScrapeKindLine        = "line too long"
	usageScrapeKindParse       = "unparsable line"
	usageScrapeKindSample      = "sample out of range"
	// usageScrapeKindOther is the kind for an error that is not a
	// usageScrapeError, which the pod source never returns and a stub might.
	usageScrapeKindOther = "error"
	// netOpDial is the Op a *net.OpError carries for a failure before the
	// connection existed; after it, the error is the peer's doing.
	netOpDial = "dial"
)

var toolInvocationsCountedStatuses = map[string]bool{"success": true, "error": true}

// usageSeriesFor is the family each counter is read from and, for the broker,
// the status values summed; nil statuses sums every label set.
func usageSeriesFor(counter string) (family string, statuses map[string]bool) {
	if counter == usageCounterEventsIngested {
		return eventsInjectedSeries, nil
	}
	return toolInvocationsSeries, toolInvocationsCountedStatuses
}

// usageReading is what one scrape yields: the counter summed over its label
// sets, and the start time the body carried, nil when it carried none.
type usageReading struct {
	Sample    int64
	StartTime *float64
}

// usageScrapeError is a scrape that produced no body to count, with a kind the
// log and the CR's event can name. Status is the HTTP status the listener
// answered, for usageScrapeKindStatus: an integer of ours, never the peer's
// text. Nothing the peer sent reaches Error().
type usageScrapeError struct {
	Kind   string
	Status int
}

func (e *usageScrapeError) Error() string {
	if e.Status != 0 {
		return fmt.Sprintf("%s: HTTP %d", e.Kind, e.Status)
	}
	return e.Kind
}

// usageConnectKind classifies a client.Do error by what happened rather than
// where it surfaced: a timeout, a dial that was refused, unreachable or failed
// otherwise, and for anything after the connection existed, a response net/http
// could not parse, so the event points the reader at the peer rather than at a
// policy. Never the text net/http builds from the bytes it read.
func usageConnectKind(err error) string {
	if usageTimedOut(err) {
		return usageScrapeKindTimeout
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return usageScrapeKindRefused
	}
	if errors.Is(err, syscall.EHOSTUNREACH) || errors.Is(err, syscall.ENETUNREACH) {
		return usageScrapeKindUnreachable
	}
	var opErr *net.OpError
	if errors.As(err, &opErr) && opErr.Op == netOpDial {
		return usageScrapeKindConnect
	}
	return usageScrapeKindMalformed
}

// usageTimedOut reports whether err is a deadline or a network timeout, which
// the client's timeout raises before the connection and, as the body's read
// error, after it.
func usageTimedOut(err error) bool {
	var netErr net.Error
	return errors.Is(err, context.DeadlineExceeded) || (errors.As(err, &netErr) && netErr.Timeout())
}

// usageSource reads a listener. The pod scraper is its one implementation; a
// test supplies a stub, and a deployment that cannot admit operator-to-pod
// traffic could gain another without changing the accumulation or the writer.
type usageSource interface {
	Scrape(ctx context.Context, addr, counter string) (usageReading, error)
}

// podUsageSource reads /metrics at a pod IP and port over the pod network.
type podUsageSource struct {
	client *http.Client
}

func newPodUsageSource() *podUsageSource {
	transport := &http.Transport{
		// No proxy: a pod-network scrape never has one, and the default
		// transport would send the GET to an HTTP_PROXY the operator's
		// environment sets.
		Proxy:                  nil,
		DialContext:            (&net.Dialer{Timeout: usageScrapeTimeout}).DialContext,
		DisableKeepAlives:      true,
		MaxResponseHeaderBytes: usageScrapeMaxHeaderBytes,
	}
	return &podUsageSource{client: &http.Client{
		Timeout:   usageScrapeTimeout,
		Transport: transport,
		// No redirect: a body on the port cannot send the operator's GET,
		// made from a network position the pod's own egress policy does not
		// have, anywhere else.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}}
}

// Scrape GETs the listener at addr and folds the body for counter. Any status
// other than 200 is a failed scrape.
func (s *podUsageSource) Scrape(ctx context.Context, addr, counter string) (usageReading, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, usageScrapeScheme+addr+usageMetricsPath, nil)
	if err != nil {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindConnect}
	}
	resp, err := s.client.Do(req)
	if err != nil {
		return usageReading{}, &usageScrapeError{Kind: usageConnectKind(err)}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindStatus, Status: resp.StatusCode}
	}
	return foldUsageBody(resp.Body, counter)
}

// foldUsageBody scans body line by line and keeps none of it: a sample line of
// counter's family, or of the start-time gauge, is parsed on its own with
// expfmt and folded into the running sum as it is read; every other line is
// skipped unread. A line past the bound, a wanted line that does not parse, or
// a sample that is negative or not finite is a failed scrape.
func foldUsageBody(body io.Reader, counter string) (usageReading, error) {
	family, statuses := usageSeriesFor(counter)
	parser := expfmt.NewTextParser(model.UTF8Validation)
	var sum float64
	var start *float64
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 0, usageScrapeLineBuffer), usageScrapeMaxLineBytes)
	for scanner.Scan() {
		line := scanner.Text()
		name := usageLineName(line)
		if name != family && name != processStartTimeSeries {
			continue
		}
		families, err := parser.TextToMetricFamilies(strings.NewReader(line + "\n"))
		if err != nil || families[name] == nil {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindParse}
		}
		for _, metric := range families[name].GetMetric() {
			value, ok := usageSampleValue(metric)
			if !ok || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
				return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
			}
			if name == processStartTimeSeries {
				captured := value
				start = &captured
				continue
			}
			if statuses != nil && !statuses[usageLabelValue(metric, toolInvocationsStatusLabel)] {
				continue
			}
			sum += value
		}
	}
	if err := scanner.Err(); err != nil {
		if errors.Is(err, bufio.ErrTooLong) {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindLine}
		}
		if usageTimedOut(err) {
			// The client's timeout firing mid-body: a slow listener, not a broken one.
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindTimeout}
		}
		// The body's read error can quote a trailer line; the kind is enough.
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindRead}
	}
	if sum >= float64(math.MaxInt64) {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
	}
	return usageReading{Sample: int64(sum), StartTime: start}, nil
}

// usageLineName is the metric name a sample line starts with, read the way
// expfmt reads it: leading blanks skipped, then everything up to the label
// set or the value. "" for a comment or a blank line. A line with no
// separator is returned whole, so a bare wanted name reaches expfmt and
// fails there rather than being skipped as another family.
func usageLineName(line string) string {
	line = strings.TrimLeft(line, " \t")
	if line == "" || line[0] == '#' {
		return ""
	}
	end := strings.IndexAny(line, "{ \t")
	if end < 0 {
		return line
	}
	return line[:end]
}

// usageSampleValue is the sample of a metric parsed from a single line, which
// expfmt types as untyped: the line is parsed alone, never with its TYPE line.
func usageSampleValue(metric *dto.Metric) (float64, bool) {
	if metric.Untyped == nil {
		return 0, false
	}
	return metric.Untyped.GetValue(), true
}

func usageLabelValue(metric *dto.Metric, name string) string {
	for _, pair := range metric.GetLabel() {
		if pair.GetName() == name {
			return pair.GetValue()
		}
	}
	return ""
}

// usageScrapeDetail is what a failed scrape is logged and recorded as: the
// scrape error's closed vocabulary, or usageScrapeKindOther for an error of
// another type. usageScrapeKindOf is the kind alone.
func usageScrapeDetail(err error) string {
	var scrape *usageScrapeError
	if errors.As(err, &scrape) {
		return scrape.Error()
	}
	return usageScrapeKindOther
}

func usageScrapeKindOf(err error) string {
	var scrape *usageScrapeError
	if errors.As(err, &scrape) {
		return scrape.Kind
	}
	return usageScrapeKindOther
}
