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

	// The series the poller reads, and the gauge every listener exports so that
	// a restart is seen whatever the sample did.
	toolInvocationsSeries  = "kubeagents_tool_invocations_total"
	eventsInjectedSeries   = "k8s_event_watcher_events_injected_total"
	processStartTimeSeries = "process_start_time_seconds"
	// vcsRequestsSeries is the broker's version-control counter, by verb,
	// status and provider; remediationsProposedTotal sums it under
	// vcsProposalCreateVerb and vcsStatusSuccess alone, over every provider:
	// a proposal the forge accepted is a proposal.
	vcsRequestsSeries     = "kubeagents_vcs_requests_total"
	vcsVerbLabel          = "verb"
	vcsProposalCreateVerb = "proposal-create"
	vcsStatusSuccess      = "success"
	// chatInboundSeries is the chat_metrics plugin's counter of messages the
	// gateway accepted, by platform; summed over every platform.
	chatInboundSeries = "kubeagents_chat_messages_inbound_total"
	// brokerFailuresSeries is the broker's failure counter, summed over its
	// kind and tool labels into brokerFailuresTotal; the kinds, and the tool a
	// timed-out command ran, stay in Prometheus.
	brokerFailuresSeries = "kubeagents_broker_execution_failures_total"
	// chatTurnsErroredSeries is the chat plugin's count of synchronous turns
	// the Hermes gateway failed to complete, summed over its platform label
	// into chatTurnsErroredTotal; on a next-mode install the A2A gateway's
	// task terminals under a2aErroredTerminalStates join it, as the
	// counter's sibling on that pod.
	chatTurnsErroredSeries = "kubeagents_chat_turns_errored_total"
	a2aTaskTerminalsSeries = "kubeagents_a2a_gateway_task_terminals_total"
	a2aTerminalStateLabel  = "state"
	// kanbanGaveUpSeries is the chat plugin's count of board cards that gave
	// up since its process started, summed whole into backgroundTasksGaveUpTotal.
	kanbanGaveUpSeries = "kubeagents_kanban_tasks_gave_up_total"
	// chatFeedbackNegativeSeries is the chat plugin's count of thumbs-down
	// reactions a person put on the bot's messages, summed over its platform
	// label into negativeFeedbackTotal; the chat_feedback_reactions hook feeds
	// it, and no A2A family joins it.
	chatFeedbackNegativeSeries = "kubeagents_chat_feedback_negative_total"
	// a2aMessagesInboundSeries is the A2A gateway's counter of the chat
	// messages it accepted as turns, by platform: the same counter on a
	// next-mode install, where Chat ingress is the A2A gateway's rather than
	// the Hermes gateway's (a2a/gateway/metrics.go). A body carries one of the
	// two families; both fold into chatMessagesInboundTotal.
	a2aMessagesInboundSeries = "kubeagents_a2a_gateway_messages_inbound_total"
	// triageSessionsSeries is the watcher's count of the triage sessions it
	// opened whose event the daemon delivered, by cluster: one per injected
	// event in per-incident mode, never a session whose inject was filtered,
	// suppressed or failed. The triage half of sessionsTotal sums it whole.
	triageSessionsSeries = "k8s_event_watcher_triage_sessions_total"
	// chatSessionsSeries is the session_store plugin's count of the
	// conversations it first saw, by platform, served on the chat_metrics
	// listener beside the inbound counter; a2aSessionsSeries is the A2A
	// gateway's count of the conversations it minted a session for, on its
	// own pod. Both fold into the chat half of sessionsTotal, summed over
	// the platform label.
	chatSessionsSeries = "kubeagents_chat_sessions_total"
	a2aSessionsSeries  = "kubeagents_a2a_gateway_sessions_total"
	// a2aPeopleSeenSeries is the A2A gateway's count of the distinct people
	// it saw speak in each trailing window, by the window label: a gauge it
	// serves for both windows together or for neither. The people reading
	// (usageReadingPeople) maps the two windows to status.usage.
	a2aPeopleSeenSeries  = "kubeagents_a2a_gateway_people_seen"
	a2aPeopleWindowLabel = "window"
	a2aPeopleWindowShort = "7d"
	a2aPeopleWindowLong  = "30d"
	// The broker's proposal readings (vcs_readings.py), gauges its readings
	// sweep sets from what the forge reports now: the install's proposals by
	// state; the closed ones by who closed them; the touches by anyone but
	// the install, by kind and actor; the terminal proposals by how many
	// human touches each carried, cumulative over le; the ignored ones; and
	// each reading's age, served from a reading's first taking on and never
	// omitted after, where the value series go once the reading is an hour
	// old. usageReadingSpecs maps them to status.usage; the ignored gauge is
	// named here for the series-name pin and projects to no field.
	vcsProposalsSeries          = "kubeagents_vcs_proposals"
	vcsProposalsClosedBySeries  = "kubeagents_vcs_proposals_closed_by"
	vcsTouchesSeries            = "kubeagents_vcs_proposal_touches"
	vcsTouchesPerProposalSeries = "kubeagents_vcs_proposal_touches_per_proposal"
	vcsProposalsIgnoredSeries   = "kubeagents_vcs_proposals_ignored"
	vcsReadingsAgeSeries        = "kubeagents_vcs_readings_age_seconds"
	// The readings' labels and the values the specs select.
	vcsReadingsStateLabel   = "state"
	vcsReadingsActorLabel   = "actor"
	vcsReadingsOutcomeLabel = "outcome"
	vcsReadingsLeLabel      = "le"
	vcsReadingsReadingLabel = "reading"
	vcsStateOpen            = "open"
	vcsStateMerged          = "merged"
	vcsStateClosed          = "closed"
	vcsActorHuman           = "human"
	vcsActorBot             = "bot"
	vcsOutcomeMerged        = "merged"
	vcsLeUntouched          = "0"
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
	// dataVolumeSeries is the watcher's reading of the agent's data volume,
	// one series per dataVolumeKindLabel value: the filesystem's size and the
	// space a non-root writer has left. Read from the watcher's body beside
	// the cluster gauge, and projected to status.storageStatus
	// (usage_storage.go).
	dataVolumeSeries    = "kubeagents_data_volume_bytes"
	dataVolumeKindLabel = "kind"
	dataVolumeKindTotal = "total"
	dataVolumeKindFree  = "free"
	// toolInvocationsStatusLabel is the broker's outcome label. The outcomes
	// toolInvocationsCountedStatuses sums are the broker's success and error:
	// the commands it ran and the requests it rejected or failed on before
	// running. toolInvocationsRefusedStatuses are the rest, the requests the
	// broker turned away: blocked and busy are refusals, and abandoned cannot
	// say whether the command had started. The two sets partition the
	// broker's vocabulary, so the two counters together are every request.
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

var (
	toolInvocationsCountedStatuses = map[string]bool{"success": true, "error": true}
	toolInvocationsRefusedStatuses = map[string]bool{"blocked": true, "busy": true, "abandoned": true}
)

// a2aErroredTerminalStates are the A2A task terminals that are a failed turn:
// failed, and rejected, which is a task the executor refused; not completed
// and not canceled.
var a2aErroredTerminalStates = map[string]bool{"failed": true, "rejected": true}

// usageSibling is another family a counter is read from on a pod whose body
// carries it in place of the counter's own, with the filter that family takes.
type usageSibling struct {
	family string
	filter usageLabelFilter
}

// usageRead is one counter a family feeds and the filter it is read through.
type usageRead struct {
	counter string
	filter  usageLabelFilter
}

// usageLabelFilter is, per label name, the values a metric must carry to be
// summed into a counter; a nil filter sums every label set.
type usageLabelFilter map[string]map[string]bool

// usageSeriesFor is the family each counter is read from and the label values
// summed: the broker's tool counter over its counted statuses, the same
// counter over its refused statuses, the proposals counter over the one verb
// and status that is a proposal opened, the watcher's injected counter over
// every label set.
func usageSeriesFor(counter string) (family string, filter usageLabelFilter) {
	switch counter {
	case usageCounterEventsIngested:
		return eventsInjectedSeries, nil
	case usageCounterRemediationsProposed:
		return vcsRequestsSeries, usageLabelFilter{
			vcsVerbLabel:               {vcsProposalCreateVerb: true},
			toolInvocationsStatusLabel: {vcsStatusSuccess: true},
		}
	case usageCounterChatMessagesInbound:
		return chatInboundSeries, nil
	case usageCounterSessionsTriage:
		return triageSessionsSeries, nil
	case usageCounterSessionsChat:
		return chatSessionsSeries, nil
	case usageCounterToolExecutionsBlocked:
		return toolInvocationsSeries, usageLabelFilter{toolInvocationsStatusLabel: toolInvocationsRefusedStatuses}
	case usageCounterBrokerFailures:
		return brokerFailuresSeries, nil
	case usageCounterChatTurnsErrored:
		return chatTurnsErroredSeries, nil
	case usageCounterBackgroundTasksGaveUp:
		return kanbanGaveUpSeries, nil
	case usageCounterNegativeFeedback:
		return chatFeedbackNegativeSeries, nil
	default:
		return toolInvocationsSeries, usageLabelFilter{toolInvocationsStatusLabel: toolInvocationsCountedStatuses}
	}
}

// usageSiblingsFor is the other families a counter is read from, on pods
// whose body carries one of them in place of the family usageSeriesFor names,
// each with its own filter: the chat counter and the chat sessions come from
// the chat_metrics plugin on a gateway pod and from the A2A gateway on its own
// pod, under the same label set; the errored counter comes from the plugin
// whole and from the A2A gateway's task terminals under the errored states.
func usageSiblingsFor(counter string) []usageSibling {
	switch counter {
	case usageCounterChatMessagesInbound:
		return []usageSibling{{family: a2aMessagesInboundSeries}}
	case usageCounterSessionsChat:
		return []usageSibling{{family: a2aSessionsSeries}}
	case usageCounterChatTurnsErrored:
		return []usageSibling{{family: a2aTaskTerminalsSeries, filter: usageLabelFilter{a2aTerminalStateLabel: a2aErroredTerminalStates}}}
	}
	return nil
}

// usageReadingKind names one reading a target serves. Two are the broker's
// proposal readings, the value of vcsReadingsAgeSeries's reading label: the
// outcomes, the proposals by state, and the details, everything the sweep's
// second step reads. A sweep can serve the first without the second, so each
// has its own presence and its own miss streak. The third is the A2A
// gateway's people reading, the people it saw in each window.
type usageReadingKind string

const (
	usageReadingOutcomes usageReadingKind = "outcomes"
	usageReadingDetails  usageReadingKind = "details"
	usageReadingPeople   usageReadingKind = "people"
)

// usageReadingSpec is one status field a reading lands in: the family it is
// read from, the label values it takes, the reading it belongs to, and
// whether it is a sum over every matching sample or one sample's value. A
// sum over no matching sample is a zero, since the broker serves only the
// touch label sets it has seen; a single-sample field resolves only on its
// sample. When a reading is present is its kind's rule (usageReadingPresence).
type usageReadingSpec struct {
	field   string
	family  string
	filter  usageLabelFilter
	reading usageReadingKind
	sum     bool
}

// usageReadingSpecs is the declared list of reading fields, in the order the
// status and the gauge export name them.
var usageReadingSpecs = []usageReadingSpec{
	{field: usageExportRemediationsMerged, family: vcsProposalsSeries, filter: usageLabelFilter{vcsReadingsStateLabel: {vcsStateMerged: true}}, reading: usageReadingOutcomes},
	{field: usageExportRemediationsOpen, family: vcsProposalsSeries, filter: usageLabelFilter{vcsReadingsStateLabel: {vcsStateOpen: true}}, reading: usageReadingOutcomes},
	{field: usageExportProposalsAuthored, family: vcsProposalsSeries, filter: usageLabelFilter{vcsReadingsStateLabel: {vcsStateOpen: true, vcsStateMerged: true, vcsStateClosed: true}}, reading: usageReadingOutcomes, sum: true},
	{field: usageExportRemediationsClosedUnmerged, family: vcsProposalsClosedBySeries, filter: usageLabelFilter{vcsReadingsActorLabel: {vcsActorHuman: true, vcsActorBot: true}}, reading: usageReadingDetails, sum: true},
	{field: usageExportProposalHumanTouches, family: vcsTouchesSeries, filter: usageLabelFilter{vcsReadingsActorLabel: {vcsActorHuman: true}}, reading: usageReadingDetails, sum: true},
	{field: usageExportProposalsMergedUntouched, family: vcsTouchesPerProposalSeries, filter: usageLabelFilter{vcsReadingsOutcomeLabel: {vcsOutcomeMerged: true}, vcsReadingsLeLabel: {vcsLeUntouched: true}}, reading: usageReadingDetails},
	{field: usageExportPeopleSeen7d, family: a2aPeopleSeenSeries, filter: usageLabelFilter{a2aPeopleWindowLabel: {a2aPeopleWindowShort: true}}, reading: usageReadingPeople},
	{field: usageExportPeopleSeen30d, family: a2aPeopleSeenSeries, filter: usageLabelFilter{a2aPeopleWindowLabel: {a2aPeopleWindowLong: true}}, reading: usageReadingPeople},
}

// usageReadingKinds is every reading, in a fixed order.
var usageReadingKinds = []usageReadingKind{usageReadingOutcomes, usageReadingDetails, usageReadingPeople}

// usageReadingKindsOf is the specs of specs that belong to one of kinds, in
// their order.
func usageReadingKindsOf(specs []usageReadingSpec, kinds ...usageReadingKind) []usageReadingSpec {
	var out []usageReadingSpec
	for _, spec := range specs {
		if slices.Contains(kinds, spec.reading) {
			out = append(out, spec)
		}
	}
	return out
}

// usageReadingAged reports whether kind is one of the broker's readings, whose
// presence needs its age series in the body.
func usageReadingAged(kind usageReadingKind) bool {
	return kind == usageReadingOutcomes || kind == usageReadingDetails
}

// usageReadingPresence is kind's rule for whether a body carried it. A
// broker reading is present when its age series was in the body and every
// single-sample field of it resolved, a sum resolving on no sample at all,
// since the broker serves only the label sets it has seen. The people
// reading is present when both its windows resolved, with no age series:
// the gateway serves both windows together or neither, so one alone is no
// reading.
func usageReadingPresence(kind usageReadingKind) func(f *usageReadingFold) bool {
	if usageReadingAged(kind) {
		return func(f *usageReadingFold) bool {
			if !f.aged[kind] {
				return false
			}
			for i, spec := range f.specs {
				if spec.reading == kind && !spec.sum && !f.resolved[i] {
					return false
				}
			}
			return true
		}
	}
	return func(f *usageReadingFold) bool {
		for i, spec := range f.specs {
			if spec.reading == kind && !f.resolved[i] {
				return false
			}
		}
		return true
	}
}

// usageReading is what one scrape yields: each wanted counter summed over its
// label sets, the start time the body carried, nil when it carried none, and,
// for the watcher's body alone, its cluster gauges; nil for any other body, so
// that a poll can tell a watcher reporting no clusters from no watcher read.
// For a target that serves readings, Readings holds the reading fields of
// every requested reading present in its body, by status field, and
// ReadingsPresent says, for each requested reading, whether it was; both nil
// for any other body. Neither is folded: the poll
// projects them as they stand.
type usageReading struct {
	Samples         map[string]int64
	StartTime       *float64
	Clusters        *usageClusterGauges
	Readings        map[string]int64
	ReadingsPresent map[usageReadingKind]bool
	// DataVolume is the watcher's data volume reading, nil when the body
	// carried no complete one: not the watcher's, or a watcher whose statfs
	// failed, or one built before it exported the series.
	DataVolume *usageDataVolume
}

// usageDataVolume is one reading of the agent's data volume, in bytes.
type usageDataVolume struct {
	Total int64
	Free  int64
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
	Scrape(ctx context.Context, addr string, counters []string, kinds []usageReadingKind) (usageReading, error)
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

// Scrape GETs the listener at addr and folds the body for each of counters,
// one sample per counter from the one body, and for each of kinds. Any status
// other than 200 is a failed scrape.
func (s *podUsageSource) Scrape(ctx context.Context, addr string, counters []string, kinds []usageReadingKind) (usageReading, error) {
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
	return foldUsageBody(resp.Body, counters, kinds)
}

// foldUsageBody scans body line by line and keeps none of it: a line that could
// be a wanted family -- each counter's own, the start-time gauge, and for the
// watcher's body the per-cluster up gauge and the data volume pair, one that
// contains a wanted name as a substring -- is parsed on its own with expfmt,
// and a metric expfmt names as one of them is folded as it is read, a
// counter's into its running sum under its label filter, the up gauge into the
// cluster counts and the volume pair into one reading; every other line is
// skipped unread. The substring is only a prefilter: the parser,
// not a hand-read of the line, names the metric, so the name in a comment or a
// label value adds nothing and a UTF-8 name the parser accepts is not dropped by
// a stricter hand-read. A candidate line that does not parse, or a sample that
// is negative or not finite, is a failed scrape; a wanted line past the bound is
// too, but a line of any other family past the bound is skipped, not a failure
// (usageReadScrapeLine). A body asked for readings, kinds, is read for the
// fields of those readings as well (usageReadingSpecs), each field folded under
// its filter.
func foldUsageBody(body io.Reader, counters []string, kinds []usageReadingKind) (usageReading, error) {
	parser := expfmt.NewTextParser(model.UTF8Validation)
	sums := make(map[string]float64, len(counters))
	var start *float64
	// wanted is every family the parser is asked to name in this body: each
	// counter's, the start-time gauge, and the watcher's cluster gauge when
	// the body is the watcher's. A family feeds every counter that names it,
	// each through its own filter: the broker's tool counter is read into
	// two, over disjoint statuses.
	var clusters *usageClusterGauges
	// volume collects the data volume series; a reading needs both kinds.
	var volume map[string]float64
	// readsOf is every (counter, filter) pair a family feeds, so a sibling
	// family takes its own filter, not the one its counter's own family takes.
	readsOf := make(map[string][]usageRead, len(counters))
	wanted := []string{processStartTimeSeries}
	want := func(family, counter string, filter usageLabelFilter) {
		readsOf[family] = append(readsOf[family], usageRead{counter: counter, filter: filter})
		if !slices.Contains(wanted, family) {
			wanted = append(wanted, family)
		}
	}
	for _, counter := range counters {
		family, filter := usageSeriesFor(counter)
		sums[counter] = 0
		want(family, counter, filter)
		for _, sibling := range usageSiblingsFor(counter) {
			want(sibling.family, counter, sibling.filter)
		}
		if family == eventsInjectedSeries {
			clusters = &usageClusterGauges{}
			volume = map[string]float64{}
			wanted = append(wanted, clusterUpSeries, dataVolumeSeries)
		}
	}
	// The reading slot, for a body asked for readings: a running sum per spec
	// of the kinds asked for, whether a sample resolved it, and the readings
	// whose age series the body carries.
	var readings *usageReadingFold
	if len(kinds) > 0 {
		readings = newUsageReadingFold(kinds)
		families := readings.families()
		if slices.ContainsFunc(kinds, usageReadingAged) {
			families = append([]string{vcsReadingsAgeSeries}, families...)
		}
		for _, family := range families {
			if !slices.Contains(wanted, family) {
				wanted = append(wanted, family)
			}
		}
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
					if name == dataVolumeSeries {
						if kind := usageLabelValue(metric, dataVolumeKindLabel); kind == dataVolumeKindTotal || kind == dataVolumeKindFree {
							volume[kind] = value
						}
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
					if readings != nil && readings.fold(name, metric, value) {
						continue
					}
					for _, read := range readsOf[name] {
						if usageLabelsMatch(metric, read.filter) {
							sums[read.counter] += value
						}
					}
				}
			}
		}
		if errors.Is(err, io.EOF) {
			break
		}
	}
	samples := make(map[string]int64, len(sums))
	for counter, sum := range sums {
		if sum >= float64(math.MaxInt64) {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
		}
		samples[counter] = int64(sum)
	}
	out := usageReading{Samples: samples, StartTime: start, Clusters: clusters, DataVolume: usageDataVolumeReading(volume)}
	if readings != nil {
		var ok bool
		if out.Readings, out.ReadingsPresent, ok = readings.result(); !ok {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
		}
	}
	return out, nil
}

// usageDataVolumeReading turns the data volume series a body carried into a
// reading, or nil when it carried neither kind, only one, or a pair no statfs
// produces (free past total, or a size past int64). Nil rather than a failed
// scrape, so such a pair costs the volume fields and not the counters and
// cluster gauges read from the same body. A NaN or negative sample fails the
// whole scrape in foldUsageBody's sample check, as it does for any series.
func usageDataVolumeReading(volume map[string]float64) *usageDataVolume {
	total, hasTotal := volume[dataVolumeKindTotal]
	free, hasFree := volume[dataVolumeKindFree]
	if !hasTotal || !hasFree || total >= float64(math.MaxInt64) || free > total {
		return nil
	}
	return &usageDataVolume{Total: int64(total), Free: int64(free)}
}

// usageReadingFold is the reading slot's state through one body: the kinds
// asked for, their specs, and per spec its running sum and whether a sample
// resolved it.
type usageReadingFold struct {
	kinds    []usageReadingKind
	specs    []usageReadingSpec
	sums     []float64
	resolved []bool
	aged     map[usageReadingKind]bool
}

func newUsageReadingFold(kinds []usageReadingKind) *usageReadingFold {
	specs := usageReadingKindsOf(usageReadingSpecs, kinds...)
	return &usageReadingFold{
		kinds:    kinds,
		specs:    specs,
		sums:     make([]float64, len(specs)),
		resolved: make([]bool, len(specs)),
		aged:     map[usageReadingKind]bool{},
	}
}

// families is every family a field of the fold's readings is read from.
func (f *usageReadingFold) families() []string {
	var families []string
	for _, spec := range f.specs {
		if !slices.Contains(families, spec.family) {
			families = append(families, spec.family)
		}
	}
	return families
}

// fold takes one sample of family name into the reading fields it feeds, or
// marks a reading aged, and reports whether name is a readings family.
func (f *usageReadingFold) fold(name string, metric *dto.Metric, value float64) bool {
	if name == vcsReadingsAgeSeries && slices.ContainsFunc(f.kinds, usageReadingAged) {
		kind := usageReadingKind(usageLabelValue(metric, vcsReadingsReadingLabel))
		if slices.Contains(f.kinds, kind) && usageReadingAged(kind) {
			f.aged[kind] = true
		}
		return true
	}
	matched := false
	for i, spec := range f.specs {
		if spec.family != name {
			continue
		}
		matched = true
		if usageLabelsMatch(metric, spec.filter) {
			// A summed field adds every matching sample; a single-sample
			// field takes the largest, so a body that repeats a label set
			// (which no broker renders) cannot double it.
			if spec.sum || !f.resolved[i] || value > f.sums[i] {
				if spec.sum {
					f.sums[i] += value
				} else {
					f.sums[i] = value
				}
			}
			f.resolved[i] = true
		}
	}
	return matched
}

// result is, for each reading asked for, whether the body carried it by its
// kind's rule (usageReadingPresence), and the fields' values of those it
// carried. ok is false when a value is past the float64-exact ceiling, which
// no count reaches.
func (f *usageReadingFold) result() (values map[string]int64, present map[usageReadingKind]bool, ok bool) {
	present = map[usageReadingKind]bool{}
	for _, kind := range f.kinds {
		present[kind] = usageReadingPresence(kind)(f)
	}
	values = map[string]int64{}
	for i, spec := range f.specs {
		if f.sums[i] > float64(usageTotalCeiling) {
			return nil, nil, false
		}
		if present[spec.reading] {
			values[spec.field] = int64(f.sums[i])
		}
	}
	return values, present, true
}

// usageLabelsMatch reports whether metric carries, for every label the filter
// names, one of the values it allows; a nil filter matches every metric.
func usageLabelsMatch(metric *dto.Metric, filter usageLabelFilter) bool {
	for label, allowed := range filter {
		if !allowed[usageLabelValue(metric, label)] {
			return false
		}
	}
	return true
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
