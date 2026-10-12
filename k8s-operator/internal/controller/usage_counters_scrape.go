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
	"slices"
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
	// usageScrapeMaxLineBytes is the one bound on how much of a body is held at
	// once: no more than this is buffered for a single line, and the only memory
	// a scrape holds besides it, since the body is folded as it is read. A
	// wanted series' line past the bound is a failed scrape; a line of any other
	// family past it is skipped rather than failing the body (foldUsageBody),
	// because the watcher's events_seen_total carries a reason label copied
	// verbatim from whatever posted the Event, which nothing bounds -- so no
	// bound sized for the listeners' own lines can also hold a hostile one. A
	// bound on the number of lines would be a bound on the watcher family's
	// cardinality, which grows with the fleet for the life of the process and
	// cannot be sized.
	usageScrapeMaxLineBytes = 64 * 1024
	// usageScrapeLineBuffer is the reader's buffer; a line longer than it is
	// accumulated across reads up to the line bound.
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
	// clusterUpSeries is the watcher's per-cluster gauge, one series per
	// cluster it built a client for, 1 once that cluster's informer has
	// synced and is delivering events. Read from the same body as the
	// injected counter: the series present are the clusters registered, those
	// at clusterUpMonitored the clusters monitored.
	clusterUpSeries    = "k8s_event_watcher_cluster_up"
	clusterUpMonitored = 1
	// usageClusterGaugeCeiling bounds the series counted into the gauges in one
	// body. The gauges are a reading, not an accumulation, so the per-poll delta
	// ceiling does not reach them; this is what bounds what a body on the
	// watcher's port can set them to. Sized past any fleet one install watches
	// (GKE's own per-project quotas keep a fleet far below it) and refused, not
	// clamped, so a body past it reads as the hostile or broken one it is.
	usageClusterGaugeCeiling int64 = 10000
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
	usageScrapeKindBodyTimeout = "timeout reading body"
	usageScrapeKindLine        = "line too long"
	usageScrapeKindParse       = "unparsable line"
	usageScrapeKindSample      = "sample out of range"
	// usageScrapeKindOther is the kind for an error that is not a
	// usageScrapeError, which the pod source never returns and a stub might.
	usageScrapeKindOther = "error"
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
// sets, the start time the body carried, nil when it carried none, and, for
// the watcher's body alone, its cluster gauges; nil for any other body, so
// that a poll can tell a watcher reporting no clusters from no watcher read.
type usageReading struct {
	Sample    int64
	StartTime *float64
	Clusters  *usageClusterGauges
}

// usageClusterGauges is the watcher's view of the fleet in one body: the
// clusterUpSeries series it exports and how many of them are at
// clusterUpMonitored. Gauges, projected to status.usage as the latest poll's
// reading rather than folded into the document.
type usageClusterGauges struct {
	Registered int64
	Monitored  int64
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
// where it surfaced: a timeout, a dial that was refused, unreachable, a
// connection the peer accepted but never answered on, or -- only when the peer
// did send a status line the client could not parse -- a malformed response, so
// the event points the reader at the layer that failed rather than at a policy.
// Never the text net/http builds from the bytes it read.
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
	// A reset, a broken pipe, any other net.OpError, or a clean EOF before a
	// byte was read is a connection the peer did not answer on, whatever the
	// Op: a listener down between accept and its first write, or a mesh
	// sidecar resetting the operator's plaintext GET. The connect guidance --
	// check the NetworkPolicy and that the listener is up -- fits those, not
	// the response guidance that says the listener answered.
	var opErr *net.OpError
	if errors.As(err, &opErr) ||
		errors.Is(err, syscall.ECONNRESET) ||
		errors.Is(err, syscall.EPIPE) ||
		errors.Is(err, io.EOF) {
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

// foldUsageBody scans body line by line and keeps none of it: a line that could
// be a wanted family -- counter's own, the start-time gauge, and for the
// watcher's body the per-cluster up gauge, one that contains a wanted name as a
// substring -- is parsed on its own with expfmt, and a metric expfmt names as
// one of them is folded as it is read, the counter into the running sum and the
// up gauge into the cluster counts; every other line is skipped unread. The substring is only a prefilter: the parser,
// not a hand-read of the line, names the metric, so the name in a comment or a
// label value adds nothing and a UTF-8 name the parser accepts is not dropped by
// a stricter hand-read. A candidate line that does not parse, or a sample that
// is negative or not finite, is a failed scrape; a wanted line past the bound is
// too, but a line of any other family past the bound is skipped, not a failure
// (usageReadScrapeLine).
func foldUsageBody(body io.Reader, counter string) (usageReading, error) {
	family, statuses := usageSeriesFor(counter)
	parser := expfmt.NewTextParser(model.UTF8Validation)
	var sum float64
	var start *float64
	// The watcher's body is also read for its cluster gauge; wanted is every
	// family the parser is asked to name in this body.
	var clusters *usageClusterGauges
	wanted := []string{family, processStartTimeSeries}
	if family == eventsInjectedSeries {
		clusters = &usageClusterGauges{}
		wanted = append(wanted, clusterUpSeries)
	}
	reader := bufio.NewReaderSize(body, usageScrapeLineBuffer)
	for {
		line, overLong, err := usageReadScrapeLine(reader)
		if err != nil && !errors.Is(err, io.EOF) {
			if usageTimedOut(err) {
				// The client's timeout firing mid-body: the listener answered and
				// the read stalled, a slow listener rather than a broken one, so
				// this is its own kind with response guidance -- not the dial
				// timeout's connect guidance, which would point at the
				// NetworkPolicy.
				return usageReading{}, &usageScrapeError{Kind: usageScrapeKindBodyTimeout}
			}
			// The body's read error can quote a trailer line; the kind is enough.
			// The line in hand may be a partial one, so it is not folded.
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindRead}
		}
		switch {
		case overLong:
			// No parser runs on an over-long line, so the substring prefilter the
			// other case uses cannot be trusted to name it: the watcher copies
			// events_seen_total's reason label verbatim from whatever posted the
			// Event, so a hostile label value can spell a wanted series' name
			// inside a line of another family. Decide on the metric name alone --
			// the leading token, before any `{` or the value, which a label value
			// sits after and so cannot reach. A genuinely over-long wanted line
			// still fails the scrape, as before; any other family's is skipped, so
			// one hostile line does not freeze the counter.
			if name := usageLeadingName(line); slices.Contains(wanted, name) {
				return usageReading{}, &usageScrapeError{Kind: usageScrapeKindLine}
			}
		case slices.ContainsFunc(wanted, func(w string) bool { return strings.Contains(line, w) }):
			// A cheap substring prefilter before the parser is handed the line: a
			// line mentioning neither name cannot be a wanted series, whatever the
			// parser would make of it. A line that mentions one still has the
			// parser, not this read, decide what it is.
			families, perr := parser.TextToMetricFamilies(strings.NewReader(line + "\n"))
			if perr != nil {
				return usageReading{}, &usageScrapeError{Kind: usageScrapeKindParse}
			}
			for _, name := range wanted {
				mf := families[name]
				if mf == nil {
					continue
				}
				for _, metric := range mf.GetMetric() {
					value, ok := usageSampleValue(metric)
					if !ok || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
						return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
					}
					if name == processStartTimeSeries {
						captured := value
						start = &captured
						continue
					}
					if name == clusterUpSeries {
						clusters.Registered++
						if clusters.Registered > usageClusterGaugeCeiling {
							return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
						}
						if value == clusterUpMonitored {
							clusters.Monitored++
						}
						continue
					}
					if statuses != nil && !statuses[usageLabelValue(metric, toolInvocationsStatusLabel)] {
						continue
					}
					sum += value
				}
			}
		}
		if errors.Is(err, io.EOF) {
			break
		}
	}
	if sum >= float64(math.MaxInt64) {
		return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
	}
	return usageReading{Sample: int64(sum), StartTime: start, Clusters: clusters}, nil
}

// usageReadScrapeLine reads one line from r, bounded at usageScrapeMaxLineBytes,
// returning it without its trailing newline. overLong is true when the line's
// content ran past the bound: the returned string is its first
// usageScrapeMaxLineBytes, the rest of the line through the next newline has been
// discarded, and the next call resumes at the following line. A bufio.Scanner
// cannot resume past an over-long token, which is why this reads with a
// bufio.Reader instead. err is io.EOF once the body is exhausted, carrying any
// final unterminated line alongside it, and the read's error otherwise.
func usageReadScrapeLine(r *bufio.Reader) (string, bool, error) {
	var buf []byte
	for {
		chunk, err := r.ReadSlice('\n')
		buf = append(buf, chunk...)
		full := errors.Is(err, bufio.ErrBufferFull)
		// The trailing newline, present only when ReadSlice found it, is not line
		// content and so does not count against the bound.
		content := buf
		if !full && len(content) > 0 && content[len(content)-1] == '\n' {
			content = content[:len(content)-1]
		}
		if len(content) > usageScrapeMaxLineBytes {
			over := string(content[:usageScrapeMaxLineBytes])
			if full {
				return over, true, usageDiscardScrapeLine(r)
			}
			return over, true, err
		}
		if full {
			continue
		}
		return usageTrimLineEnd(buf), false, err
	}
}

// usageDiscardScrapeLine reads and drops bytes through the next newline, so a
// line past the bound is not held. It returns nil once the newline is consumed,
// io.EOF if the body ends first, and the read's error otherwise.
func usageDiscardScrapeLine(r *bufio.Reader) error {
	for {
		_, err := r.ReadSlice('\n')
		if errors.Is(err, bufio.ErrBufferFull) {
			continue
		}
		return err
	}
}

// usageTrimLineEnd drops the trailing newline, and a single carriage return
// before it, that bufio.Scanner's line split would have, so a folded line
// matches what the previous scanner handed the parser.
func usageTrimLineEnd(b []byte) string {
	if n := len(b); n > 0 && b[n-1] == '\n' {
		b = b[:n-1]
	}
	if n := len(b); n > 0 && b[n-1] == '\r' {
		b = b[:n-1]
	}
	return string(b)
}

// usageLeadingName is the metric name at the start of a sample line: the leading
// token before the first '{', space or tab. A label value sits after the '{', so
// it cannot be mistaken for the name -- which is how an over-long line, with no
// parser to run on it, is judged a wanted series or not.
func usageLeadingName(line string) string {
	if i := strings.IndexAny(line, "{ \t"); i >= 0 {
		return line[:i]
	}
	return line
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
