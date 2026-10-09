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
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// scrapeTestFleetClusters and scrapeTestFleetNamespaces size the fleet-sized
// body: clusters times namespaces times reasons, per family, is the product
// the design says cannot be bounded, and the body is folded within a memory
// bound of one line whatever its size.
const (
	scrapeTestFleetClusters   = 40
	scrapeTestFleetNamespaces = 30
	scrapeTestStartTime       = 1759400000.25
	// scrapeTestStallTimeout is the client timeout the stalled-body case
	// shortens the scrape to, so the test does not wait the real one.
	scrapeTestStallTimeout = 300 * time.Millisecond
)

var scrapeTestReasons = []string{"BackOff", "Failed", "Unhealthy", "FailedScheduling", "OOMKilling"}

// fleetBody renders a watcher exposition the size of a fleet: every family
// the watcher exports over the label product, with the injected family
// summing to a known value and the observed family far larger.
func fleetBody(t *testing.T) (body string, injected int64) {
	t.Helper()
	var b strings.Builder
	b.WriteString("# HELP k8s_event_watcher_events_seen_total Total k8s events observed by the informer, before filter.\n# TYPE k8s_event_watcher_events_seen_total counter\n")
	for c := 0; c < scrapeTestFleetClusters; c++ {
		for _, reason := range scrapeTestReasons {
			fmt.Fprintf(&b, "k8s_event_watcher_events_seen_total{cluster=\"c%d\",location=\"us-central1\",project=\"p\",reason=%q} %d\n", c, reason, 100000+c)
		}
	}
	for _, family := range []string{"k8s_event_watcher_events_deduped_total", "k8s_event_watcher_events_policy_filtered_total", "k8s_event_watcher_events_injected_total"} {
		fmt.Fprintf(&b, "# TYPE %s counter\n", family)
		for c := 0; c < scrapeTestFleetClusters; c++ {
			for n := 0; n < scrapeTestFleetNamespaces; n++ {
				for _, reason := range scrapeTestReasons {
					value := int64(c + n + 1)
					fmt.Fprintf(&b, "%s{cluster=\"c%d\",location=\"us-central1\",namespace=\"ns%d\",project=\"p\",reason=%q} %d\n", family, c, n, reason, value)
					if family == eventsInjectedSeries {
						injected += value
					}
				}
			}
		}
	}
	fmt.Fprintf(&b, "# TYPE %s gauge\n%s %v\n", processStartTimeSeries, processStartTimeSeries, scrapeTestStartTime)
	return b.String(), injected
}

func serveBody(t *testing.T, status int, body string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return strings.TrimPrefix(srv.URL, "http://")
}

func scrapeKind(t *testing.T, err error) string {
	t.Helper()
	var scrape *usageScrapeError
	if !errors.As(err, &scrape) {
		t.Fatalf("error %v is not a usageScrapeError", err)
	}
	return scrape.Kind
}

// A fleet-sized body, of the wanted family and of other families alike, is
// summed correctly: the injected series over every label set and not the
// observed one, with the start time read off the gauge.
func TestPodUsageSource_FoldsAFleetSizedBody(t *testing.T) {
	body, injected := fleetBody(t)
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != injected {
		t.Errorf("sample = %d, want the injected sum %d", reading.Sample, injected)
	}
	if reading.StartTime == nil || *reading.StartTime != scrapeTestStartTime {
		t.Errorf("start time = %v, want %v", reading.StartTime, scrapeTestStartTime)
	}
}

// The broker's series: status values success and error summed over tool and
// subcommand; blocked, busy and abandoned excluded.
func TestPodUsageSource_SumsTheBrokersCountedStatuses(t *testing.T) {
	body := strings.Join([]string{
		"# HELP kubeagents_tool_invocations_total CLI tool executions brokered, by tool, subcommand and outcome.",
		"# TYPE kubeagents_tool_invocations_total counter",
		`kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="success"} 40`,
		`kubeagents_tool_invocations_total{tool="kubectl",subcommand="get",status="error"} 2`,
		`kubeagents_tool_invocations_total{tool="kubectl",subcommand="delete",status="blocked"} 7`,
		`kubeagents_tool_invocations_total{tool="gcloud",subcommand="other",status="busy"} 3`,
		`kubeagents_tool_invocations_total{tool="gcloud",subcommand="other",status="abandoned"} 1`,
		`kubeagents_tool_invocations_total{tool="gcloud",subcommand="other",status="success"} 5`,
		"# TYPE kubeagents_tool_execution_duration_seconds histogram",
		`kubeagents_tool_execution_duration_seconds_bucket{tool="kubectl",le="+Inf"} 42`,
		`kubeagents_tool_execution_duration_seconds_sum{tool="kubectl"} 12.5`,
		`kubeagents_tool_execution_duration_seconds_count{tool="kubectl"} 42`,
		`kubeagents_credential_proxy_requests_total{endpoint="/v1/exec",status_code="200"} 42`,
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterToolExecutions)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 47 {
		t.Errorf("sample = %d, want 47 (success and error only)", reading.Sample)
	}
	if reading.StartTime != nil {
		t.Errorf("a body without the gauge yielded a start time %v", *reading.StartTime)
	}
}

// The watcher's body also carries k8s_event_watcher_cluster_up, one series per
// cluster the watcher built a client for: the count of series is the clusters
// registered and the count at 1 the clusters monitored. A broker body yields
// no cluster reading at all, so the poller can tell "none" from "zero".
func TestPodUsageSource_CountsTheWatchersClusterGauge(t *testing.T) {
	body := strings.Join([]string{
		"# TYPE k8s_event_watcher_events_injected_total counter",
		`k8s_event_watcher_events_injected_total{cluster="a",location="l",namespace="n",project="p",reason="BackOff"} 7`,
		"# HELP k8s_event_watcher_cluster_up 1 once this cluster's informer has completed its initial list.",
		"# TYPE k8s_event_watcher_cluster_up gauge",
		`k8s_event_watcher_cluster_up{cluster="a",location="l",project="p"} 1`,
		`k8s_event_watcher_cluster_up{cluster="b",location="l",project="p"} 0`,
		`k8s_event_watcher_cluster_up{cluster="c",location="l2",project="p"} 1`,
		"# TYPE k8s_event_watcher_cluster_discovery_errors_total counter",
		`k8s_event_watcher_cluster_discovery_errors_total{profile="broken"} 1`,
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 7 {
		t.Errorf("sample = %d, want 7", reading.Sample)
	}
	if reading.Clusters == nil || reading.Clusters.Registered != 3 || reading.Clusters.Monitored != 2 {
		t.Fatalf("clusters = %+v, want 3 registered and 2 monitored", reading.Clusters)
	}

	empty := serveBody(t, http.StatusOK, "# TYPE k8s_event_watcher_events_injected_total counter\n")
	reading, err = newPodUsageSource().Scrape(context.Background(), empty, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape of a watcher body without the gauge: %v", err)
	}
	if reading.Clusters == nil || reading.Clusters.Registered != 0 || reading.Clusters.Monitored != 0 {
		t.Fatalf("a watcher body without the gauge = %+v, want a zero reading, not none", reading.Clusters)
	}

	broker := serveBody(t, http.StatusOK, "# TYPE kubeagents_tool_invocations_total counter\nkubeagents_tool_invocations_total{status=\"success\"} 1\n")
	reading, err = newPodUsageSource().Scrape(context.Background(), broker, usageCounterToolExecutions)
	if err != nil {
		t.Fatalf("Scrape of the broker: %v", err)
	}
	if reading.Clusters != nil {
		t.Errorf("the broker's body yielded a cluster reading: %+v", reading.Clusters)
	}

	bad := serveBody(t, http.StatusOK, "# TYPE k8s_event_watcher_cluster_up gauge\nk8s_event_watcher_cluster_up{cluster=\"a\"} NaN\n")
	if _, err := newPodUsageSource().Scrape(context.Background(), bad, usageCounterEventsIngested); scrapeKind(t, err) != usageScrapeKindSample {
		t.Errorf("a NaN cluster_up sample: %v, want kind %q", err, usageScrapeKindSample)
	}
}

// A watcher body with more cluster_up series than any fleet has is refused
// whole, so a body on the port cannot set the gauges past the ceiling.
func TestPodUsageSource_RefusesAClusterGaugePastTheCeiling(t *testing.T) {
	var b strings.Builder
	b.WriteString("# TYPE k8s_event_watcher_cluster_up gauge\n")
	for i := int64(0); i <= usageClusterGaugeCeiling; i++ {
		fmt.Fprintf(&b, "k8s_event_watcher_cluster_up{cluster=\"c%d\",location=\"l\",project=\"p\"} 1\n", i)
	}
	addr := serveBody(t, http.StatusOK, b.String())
	if _, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested); scrapeKind(t, err) != usageScrapeKindSample {
		t.Errorf("a body past the gauge ceiling: %v, want kind %q", err, usageScrapeKindSample)
	}
	at := strings.TrimSuffix(b.String(), fmt.Sprintf("k8s_event_watcher_cluster_up{cluster=\"c%d\",location=\"l\",project=\"p\"} 1\n", usageClusterGaugeCeiling))
	addr = serveBody(t, http.StatusOK, at)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil || reading.Clusters == nil || reading.Clusters.Registered != usageClusterGaugeCeiling {
		t.Fatalf("a body at the ceiling: %+v, %v; want %d registered", reading.Clusters, err, usageClusterGaugeCeiling)
	}
}

// Each scrape that produces no body to count: a connection that failed, a
// 3xx answer (not followed), a line past its bound, a wanted line expfmt
// cannot parse, a sample that is negative or not finite.
func TestPodUsageSource_FailedScrapes(t *testing.T) {
	longLine := eventsInjectedSeries + `{cluster="c",namespace="` + strings.Repeat("n", usageScrapeMaxLineBytes) + `"} 1` + "\n"
	cases := []struct {
		name   string
		status int
		body   string
		kind   string
	}{
		{"a 302 answer, which the client does not follow (TestPodUsageSource_DoesNotFollowRedirects covers the guard)", http.StatusFound, "", usageScrapeKindStatus},
		{"a server error", http.StatusInternalServerError, "", usageScrapeKindStatus},
		{"a line past the bound", http.StatusOK, longLine, usageScrapeKindLine},
		{"a wanted line that does not parse", http.StatusOK, eventsInjectedSeries + `{cluster="c"} not-a-number` + "\n", usageScrapeKindParse},
		{"a bare wanted name with no value", http.StatusOK, eventsInjectedSeries + "\n", usageScrapeKindParse},
		{"a negative sample", http.StatusOK, eventsInjectedSeries + `{cluster="c"} -3` + "\n", usageScrapeKindSample},
		{"a NaN sample", http.StatusOK, eventsInjectedSeries + `{cluster="c"} NaN` + "\n", usageScrapeKindSample},
		{"an infinite sample", http.StatusOK, eventsInjectedSeries + `{cluster="c"} +Inf` + "\n", usageScrapeKindSample},
		{"a negative start time", http.StatusOK, processStartTimeSeries + " -1\n", usageScrapeKindSample},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			addr := serveBody(t, tc.status, tc.body)
			_, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
			if err == nil {
				t.Fatal("Scrape succeeded")
			}
			if got := scrapeKind(t, err); got != tc.kind {
				t.Errorf("kind = %q, want %q (%v)", got, tc.kind, err)
			}
		})
	}
	t.Run("a connection that failed", func(t *testing.T) {
		// Port 1 has no listener, so the connection is refused, and the kind
		// says so without quoting anything a peer sent.
		_, err := newPodUsageSource().Scrape(context.Background(), "127.0.0.1:1", usageCounterEventsIngested)
		if err == nil || scrapeKind(t, err) != usageScrapeKindRefused {
			t.Errorf("a refused connection: %v, want kind %q", err, usageScrapeKindRefused)
		}
		if err != nil && strings.Contains(err.Error(), "127.0.0.1") {
			t.Errorf("the error text carries the address: %q", err.Error())
		}
	})
	t.Run("a peer that is not HTTP", func(t *testing.T) {
		// What answers on the port is input: the error kind names no byte of
		// the junk status line.
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer ln.Close()
		go func() {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			_, _ = conn.Write([]byte("JUNK SECRET-STATUS-LINE\r\n\r\n"))
			conn.Close()
		}()
		_, err = newPodUsageSource().Scrape(context.Background(), ln.Addr().String(), usageCounterEventsIngested)
		if err == nil {
			t.Fatal("a non-HTTP peer scraped successfully")
		}
		if strings.Contains(err.Error(), "SECRET") || strings.Contains(err.Error(), "JUNK") {
			t.Errorf("the error text carries the peer's bytes: %q", err.Error())
		}
		if got := scrapeKind(t, err); got != usageScrapeKindMalformed {
			t.Errorf("kind = %q, want %q: the connection succeeded and the peer answered junk", got, usageScrapeKindMalformed)
		}
	})
	t.Run("a peer that accepts then closes before a byte", func(t *testing.T) {
		// Accept and drop the connection without answering -- a reset, a broken
		// pipe, or a clean EOF, never a status line. Nothing answered, so the
		// kind is connect (the NetworkPolicy/listener guidance), not the
		// malformed-response kind that says the listener answered.
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			t.Fatal(err)
		}
		defer ln.Close()
		go func() {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			conn.Close()
		}()
		_, err = newPodUsageSource().Scrape(context.Background(), ln.Addr().String(), usageCounterEventsIngested)
		if err == nil {
			t.Fatal("a peer that answered nothing scraped successfully")
		}
		if got := scrapeKind(t, err); got != usageScrapeKindConnect {
			t.Errorf("kind = %q, want %q: the peer accepted and closed without answering", got, usageScrapeKindConnect)
		}
	})
	t.Run("a listener that stalls mid-body", func(t *testing.T) {
		// Headers promptly, then nothing: the client's timeout fires on the
		// body read, and the kind says it was the body's -- not a dial timeout,
		// and not a read.
		release := make(chan struct{})
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(eventsInjectedSeries + " 1\n"))
			w.(http.Flusher).Flush()
			<-release
		}))
		defer srv.Close()
		defer close(release)
		source := newPodUsageSource()
		source.client.Timeout = scrapeTestStallTimeout
		_, err := source.Scrape(context.Background(), strings.TrimPrefix(srv.URL, "http://"), usageCounterEventsIngested)
		if err == nil || scrapeKind(t, err) != usageScrapeKindBodyTimeout {
			t.Errorf("a stalled body: %v, want kind %q", err, usageScrapeKindBodyTimeout)
		}
	})
}

// A line past the bound in a family the poller does not read is skipped, not a
// failed scrape, so a wanted line after it is still counted: the watcher's
// events_seen_total carries a reason label copied verbatim from whatever posted
// the Event, which nothing bounds, and one such line must not freeze the counter.
// An over-long *wanted* line is still a failed scrape; TestPodUsageSource_FailedScrapes
// pins that.
func TestPodUsageSource_SkipsAnOverLongLineOfAnotherFamily(t *testing.T) {
	overLongOther := `k8s_event_watcher_events_seen_total{reason="` + strings.Repeat("x", usageScrapeMaxLineBytes) + `"} 1`
	body := strings.Join([]string{
		overLongOther,
		eventsInjectedSeries + `{cluster="c"} 5`,
		processStartTimeSeries + " 1700000000",
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 5 {
		t.Errorf("sample = %d, want 5 (the wanted line after the skipped over-long one)", reading.Sample)
	}
	if reading.StartTime == nil || *reading.StartTime != 1700000000 {
		t.Errorf("start time = %v, want 1700000000 (the gauge after the skipped over-long line)", reading.StartTime)
	}
}

// An over-long line of an unwanted family is skipped even when its label value
// spells a wanted series' name. The watcher copies events_seen_total's reason
// label verbatim from whatever posted the Event, so the reason is attacker
// input; deciding an over-long line on a substring would let one such line, with
// a wanted name in its reason, fail the scrape and freeze the counter -- the
// round-12 denial the skip closes, by a route the substring left open. The
// wanted line after it is still folded.
func TestPodUsageSource_SkipsAnOverLongLineWhoseLabelNamesAWantedSeries(t *testing.T) {
	hostileReason := processStartTimeSeries + " " + eventsInjectedSeries + " " + strings.Repeat("x", usageScrapeMaxLineBytes)
	overLongHostile := `k8s_event_watcher_events_seen_total{reason="` + hostileReason + `"} 1`
	body := strings.Join([]string{
		overLongHostile,
		eventsInjectedSeries + `{cluster="c"} 5`,
		processStartTimeSeries + " 1700000000",
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v (an over-long line of another family must be skipped, not fail, however its labels read)", err)
	}
	if reading.Sample != 5 {
		t.Errorf("sample = %d, want 5 (the wanted line after the skipped hostile one)", reading.Sample)
	}
	if reading.StartTime == nil || *reading.StartTime != 1700000000 {
		t.Errorf("start time = %v, want 1700000000", reading.StartTime)
	}
}

// A redirect is not followed: the target that would answer 200 with a huge
// sample is never reached.
func TestPodUsageSource_DoesNotFollowRedirects(t *testing.T) {
	reached := false
	target := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reached = true
		_, _ = fmt.Fprintf(w, "%s 1000000\n", eventsInjectedSeries)
	}))
	t.Cleanup(target.Close)
	redirecting := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, target.URL+usageMetricsPath, http.StatusFound)
	}))
	t.Cleanup(redirecting.Close)
	_, err := newPodUsageSource().Scrape(context.Background(), strings.TrimPrefix(redirecting.URL, "http://"), usageCounterEventsIngested)
	if err == nil || scrapeKind(t, err) != usageScrapeKindStatus {
		t.Fatalf("redirect: err=%v, want kind %q", err, usageScrapeKindStatus)
	}
	if reached {
		t.Error("the redirect target was reached")
	}
}

// Other lines are skipped unread: a line of another family that would not
// parse is not a failed scrape, and only the wanted families are folded.
func TestPodUsageSource_SkipsOtherLinesUnread(t *testing.T) {
	body := strings.Join([]string{
		"garbage line that is not a sample",
		`k8s_event_watcher_events_seen_total{cluster="c"} not-a-number`,
		`k8s_event_watcher_events_injected_total_created{cluster="c"} 1700000000`,
		eventsInjectedSeries + `{cluster="c",namespace="a"} 3`,
		eventsInjectedSeries + "\t4",
		eventsInjectedSeries + " 5",
		"  " + eventsInjectedSeries + `{cluster="d"} 6`,
		"\t" + eventsInjectedSeries + " 7",
		"",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 25 {
		t.Errorf("sample = %d, want 25: leading blanks are skipped as expfmt skips them", reading.Sample)
	}
}

// A line in the UTF-8 name-in-braces form the parser is configured to accept is
// counted, not dropped. The prefilter matches the name as a substring and the
// parser names the metric; the hand-rolled name read it replaced returned "" for
// a line that opens with a brace and skipped it, under-counting the sample.
func TestPodUsageSource_CountsAUTF8NamedLine(t *testing.T) {
	body := strings.Join([]string{
		`{"` + eventsInjectedSeries + `",cluster="c"} 9`,
		eventsInjectedSeries + " 5",
	}, "\n")
	addr := serveBody(t, http.StatusOK, body)
	reading, err := newPodUsageSource().Scrape(context.Background(), addr, usageCounterEventsIngested)
	if err != nil {
		t.Fatalf("Scrape: %v", err)
	}
	if reading.Sample != 14 {
		t.Errorf("sample = %d, want 14: the UTF-8 named line (9) plus the classic line (5)", reading.Sample)
	}
}

// The transport is built by hand: no proxy function at all, so an HTTP_PROXY
// in the operator's environment cannot take a pod-network GET; bounded
// response headers; and a client that stops at the first redirect. Asserted
// on the transport rather than through a scrape under HTTP_PROXY, because a
// test server is loopback and Go never proxies loopback, so that scrape
// would pass with the guard removed.
func TestPodUsageSource_TransportUsesNoProxyAndBoundsHeaders(t *testing.T) {
	source := newPodUsageSource()
	transport, ok := source.client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("the client's transport is a %T, want *http.Transport", source.client.Transport)
	}
	if transport.Proxy != nil {
		t.Error("the transport has a proxy function; a pod-network scrape must have none")
	}
	if transport.MaxResponseHeaderBytes != usageScrapeMaxHeaderBytes {
		t.Errorf("MaxResponseHeaderBytes = %d, want %d", transport.MaxResponseHeaderBytes, usageScrapeMaxHeaderBytes)
	}
	if source.client.CheckRedirect == nil {
		t.Fatal("the client follows redirects")
	}
	if err := source.client.CheckRedirect(nil, nil); !errors.Is(err, http.ErrUseLastResponse) {
		t.Errorf("CheckRedirect = %v, want ErrUseLastResponse", err)
	}
	if source.client.Timeout != usageScrapeTimeout {
		t.Errorf("client timeout = %v, want %v", source.client.Timeout, usageScrapeTimeout)
	}
}
