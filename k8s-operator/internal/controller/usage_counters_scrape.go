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
	// usageMetricsPath is the path every kube-agents listener serves its
	// exposition at. usageLiteLLMMetricsPath is LiteLLM's: its router mounts
	// the exposition under a trailing slash and answers the bare path with a
	// 307 to it, which the client below does not follow, so the target names
	// the slashed path and the scrape never depends on a redirect.
	usageMetricsPath        = "/metrics"
	usageLiteLLMMetricsPath = "/metrics/"
	usageScrapeScheme       = "http://"

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
	// it.
	chatFeedbackNegativeSeries = "kubeagents_chat_feedback_negative_total"
	// a2aAnswerFeedbackSeries is the A2A gateway's count of the ratings
	// verified senders gave its answers, by platform and rating: under
	// a2aRatingDown the next-mode half of negativeFeedbackTotal, under
	// a2aRatingUp the whole of positiveFeedbackTotal, each summed over the
	// family's other labels (a2a/gateway/metrics.go).
	a2aAnswerFeedbackSeries = "kubeagents_a2a_gateway_answer_feedback_total"
	a2aRatingLabel          = "rating"
	a2aRatingUp             = "up"
	a2aRatingDown           = "down"
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
	// proposalsOpenedSeries is the chat plugin's count of the pull requests
	// the agent opened, by the origin of the work and, for chat, its platform:
	// a gauge it reads from the pod's proposals ledger at scrape time, served
	// with a row for every origin once its first ledger read succeeds and as
	// no family before. The origins reading (usageReadingOrigins) maps each
	// origin, summed over the platform label, to status.usage.
	proposalsOpenedSeries = "kubeagents_proposals_opened"
	// proposalsOpenedReadAgeSeries is the plugin's label-less gauge of the
	// seconds since its last successful ledger read, served only beside
	// proposalsOpenedSeries: 0 after a good read, growing while failed reads
	// keep the last counts.
	proposalsOpenedReadAgeSeries = "kubeagents_proposals_opened_read_age_seconds"
	// proposalsReadAgeLimit is the oldest ledger read the origins reading
	// accepts: two poll intervals, so a ledger that has not answered for two
	// polls reads as a miss and the two-miss rule clears the nine fields.
	proposalsReadAgeLimit = 2 * usageCountersPollInterval
	proposalsOriginLabel  = "origin"
	// The nine origins the ledger counts, in the chat plugin's ORIGINS order.
	proposalOriginChat      = "chat"
	proposalOriginConsole   = "console"
	proposalOriginAPI       = "api"
	proposalOriginEvent     = "event"
	proposalOriginDrift     = "drift"
	proposalOriginAudit     = "audit"
	proposalOriginScheduled = "scheduled"
	proposalOriginIssue     = "issue"
	proposalOriginOther     = "other"
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
	// The series' three labels, the cluster's triple as the watcher's profile
	// names it, read into usageClusterGauges.Refs for the ScopeUndeclared
	// condition (usage_scope.go). Label values are the peer's text and are
	// validated where they are printed, not here.
	clusterUpClusterLabel  = "cluster"
	clusterUpProjectLabel  = "project"
	clusterUpLocationLabel = "location"
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
	// kanbanDispatcherFailedTicksSeries is the chat plugin's gauge of the
	// kanban dispatcher's consecutive failed ticks, one series per failing
	// board, with the class of the last failure; a good tick drops the board's
	// series. Read from the body that serves kanbanGaveUpSeries, which only the
	// chat_metrics listener does, and projected to the KanbanDispatcherFailing
	// condition (usage_kanban_dispatcher.go).
	kanbanDispatcherFailedTicksSeries = "kubeagents_kanban_dispatcher_failed_ticks"
	// kanbanDispatcherFailingSecondsSeries is the plugin's age of each failing
	// board's streak, in seconds, beside the ticks; matched to them by board.
	// A plugin from before it serves only the ticks.
	kanbanDispatcherFailingSecondsSeries = "kubeagents_kanban_dispatcher_failing_seconds"
	kanbanDispatcherBoardLabel           = "board"
	kanbanDispatcherErrorLabel           = "error"
	// usageDispatcherBoardCeiling bounds the failing boards one body may list,
	// refused rather than clamped as usageClusterGaugeCeiling is. Hermes keeps
	// a handful of boards; a body past this is a broken one.
	usageDispatcherBoardCeiling = 1000
	// The inference gateway's (LiteLLM's) token and spend families, summed
	// over every label set (model, provider, key, team, user, requested
	// model) into the three inference totals. LiteLLM has named each with and
	// without the counter suffix across its releases, as the workstation
	// summary's TOKEN_FAMILIES and SPEND_FAMILIES record, so the bare name is
	// each counter's sibling (usageSiblingsFor); a body carries one or the
	// other. tests/test_usage_counters_series_names.py pins these to the
	// script's tuples. The token families' identifiers leave the word out:
	// gosec's G101 reads a `token` in a name holding a string literal as a
	// hardcoded credential, and the values are metric names.
	litellmInputSeries      = "litellm_input_tokens_metric_total"
	litellmInputBareSeries  = "litellm_input_tokens_metric"
	litellmOutputSeries     = "litellm_output_tokens_metric_total"
	litellmOutputBareSeries = "litellm_output_tokens_metric"
	litellmSpendSeries      = "litellm_spend_metric_total"
	litellmSpendBareSeries  = "litellm_spend_metric"
	// usageSpendMicrosPerUSD scales LiteLLM's dollar figure to the integer
	// micro-dollars status.usage.spendMicrosUSDTotal keeps: the sum over the
	// body is scaled and rounded once, before the int64 conversion, so a
	// fraction of a micro is never truncated per label set.
	usageSpendMicrosPerUSD = 1_000_000
	// toolInvocationsStatusLabel is the broker's outcome label. The outcomes
	// toolInvocationsCountedStatuses sums are the broker's success and error:
	// the commands it ran and the requests it rejected or failed on before
	// running. toolInvocationsRefusedStatuses are the rest, the requests the
	// broker turned away: blocked and busy are refusals, and abandoned cannot
	// say whether the command had started. The two sets partition the
	// broker's vocabulary, so the two counters together are every request.
	toolInvocationsStatusLabel = "status"
	// The broker's five outcomes (its TOOL_STATUS_* constants): the two
	// status sets below partition them, toolInvocationsStatusOrder prints
	// them, and a topTools row carries one field per outcome under the
	// same name.
	toolStatusSuccess   = "success"
	toolStatusError     = "error"
	toolStatusBlocked   = "blocked"
	toolStatusBusy      = "busy"
	toolStatusAbandoned = "abandoned"
	// The tool counter's other two labels, read for status.usage.topTools:
	// the executable and its first bare word, each from the broker's closed
	// vocabulary (toolInvocationsSubcommands). toolLabelOther is what the
	// broker writes for an executable it does not serve and for a word
	// outside a tool's vocabulary (its LABEL_OTHER), and what this fold reads
	// an unlisted subcommand as; toolSubcommandNone is a bare tool (its
	// SUBCOMMAND_NONE).
	toolInvocationsToolLabel       = "tool"
	toolInvocationsSubcommandLabel = "subcommand"
	toolLabelOther                 = "other"
	toolSubcommandNone             = "none"

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
	toolInvocationsCountedStatuses = map[string]bool{toolStatusSuccess: true, toolStatusError: true}
	toolInvocationsRefusedStatuses = map[string]bool{toolStatusBlocked: true, toolStatusBusy: true, toolStatusAbandoned: true}
	// toolInvocationsStatusOrder is the broker's whole outcome vocabulary, the
	// two sets above in the order the summary block prints them: the statuses
	// a topTools row carries, and the only ones a sample is folded under.
	toolInvocationsStatusOrder = []string{toolStatusSuccess, toolStatusError, toolStatusBlocked, toolStatusBusy, toolStatusAbandoned}
	// toolInvocationsSubcommands is the broker's tool vocabulary and, per tool,
	// its subcommand vocabulary (credential_proxy.py's ALLOWED_EXECUTABLES and
	// _subcommand_vocabulary; the empty entry is its LABEL_OTHER tool, which
	// has none). The vocabulary lives in four places: the broker's own, the
	// summary script's BROKER_SUBCOMMANDS (held to the broker's by its
	// GatewayCopyTest), the A2A gateway's usageSummaryBrokerSubcommands in
	// a2a/gateway/feedback_summary.go (held to the script's by the same test;
	// it is the chat block's audit vocabulary, so a word missing there has a
	// cluster named after it refuse the block), and this one, which
	// tests/test_usage_counters_series_names.py holds to the broker's along
	// with the tool enum on the status field. A sample whose tool is not a key here is
	// not folded into topTools, and a subcommand outside its tool's list reads
	// toolLabelOther, so the status carries no word the broker did not declare.
	toolInvocationsSubcommands = map[string][]string{
		"kubectl": {
			"annotate", "api-resources", "api-versions", "apply", "attach", "auth",
			"autoscale", "cluster-info", "config", "cordon", "cp", "create", "debug",
			"delete", "describe", "diff", "drain", "edit", "events", "exec", "explain",
			"expose", "get", "label", "logs", "patch", "port-forward", "proxy", "replace",
			"rollout", "run", "scale", "set", "taint", "top", "uncordon", "version", "wait",
		},
		"gcloud": {
			"artifacts", "asset", "auth", "billing", "components", "compute", "config",
			"container", "iam", "info", "init", "logging", "monitoring", "projects",
			"resource-manager", "services", "version",
		},
		"git": {
			"add", "am", "apply", "archive", "blame", "branch", "bundle", "cat-file",
			"checkout", "cherry-pick", "clean", "clone", "commit", "describe", "fetch",
			"init", "log", "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "mv",
			"pull", "push", "rebase", "remote", "reset", "restore", "rev-list", "rev-parse",
			"revert", "rm", "show", "sparse-checkout", "stash", "status", "submodule",
			"switch", "symbolic-ref", "tag", "update-ref", "version", "worktree",
		},
		"gh": {
			"api", "auth", "browse", "gist", "issue", "label", "pr", "project", "release",
			"repo", "run", "search", "secret", "ssh-key", "status", "variable", "version",
			"workflow",
		},
		"feedback":     {"submit"},
		toolLabelOther: {},
	}
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
	case usageCounterPositiveFeedback:
		return a2aAnswerFeedbackSeries, usageLabelFilter{a2aRatingLabel: {a2aRatingUp: true}}
	case usageCounterInputTokens:
		return litellmInputSeries, nil
	case usageCounterOutputTokens:
		return litellmOutputSeries, nil
	case usageCounterSpendMicros:
		return litellmSpendSeries, nil
	default:
		return toolInvocationsSeries, usageLabelFilter{toolInvocationsStatusLabel: toolInvocationsCountedStatuses}
	}
}

// usageSampleScaleFor is the factor a counter's summed sample is multiplied
// by, and rounded, before it becomes the integer the document keeps: the
// spend counter's dollars to micro-dollars; every other counter is a count
// already and is taken as it is.
func usageSampleScaleFor(counter string) float64 {
	if counter == usageCounterSpendMicros {
		return usageSpendMicrosPerUSD
	}
	return 1
}

// usageSiblingsFor is the other families a counter is read from, on pods
// whose body carries one of them in place of the family usageSeriesFor names,
// each with its own filter: the chat counter and the chat sessions come from
// the chat_metrics plugin on a gateway pod and from the A2A gateway on its own
// pod, under the same label set; the errored counter comes from the plugin
// whole and from the A2A gateway's task terminals under the errored states;
// the thumbs-downs come from the plugin whole and from the A2A gateway's
// answer ratings under the down rating; each inference total comes from
// LiteLLM's suffixed family or, on a release that names it without the
// suffix, the bare one, summed whole either way.
func usageSiblingsFor(counter string) []usageSibling {
	switch counter {
	case usageCounterChatMessagesInbound:
		return []usageSibling{{family: a2aMessagesInboundSeries}}
	case usageCounterSessionsChat:
		return []usageSibling{{family: a2aSessionsSeries}}
	case usageCounterChatTurnsErrored:
		return []usageSibling{{family: a2aTaskTerminalsSeries, filter: usageLabelFilter{a2aTerminalStateLabel: a2aErroredTerminalStates}}}
	case usageCounterNegativeFeedback:
		return []usageSibling{{family: a2aAnswerFeedbackSeries, filter: usageLabelFilter{a2aRatingLabel: {a2aRatingDown: true}}}}
	case usageCounterInputTokens:
		return []usageSibling{{family: litellmInputBareSeries}}
	case usageCounterOutputTokens:
		return []usageSibling{{family: litellmOutputBareSeries}}
	case usageCounterSpendMicros:
		return []usageSibling{{family: litellmSpendBareSeries}}
	}
	return nil
}

// usageReadingKind names one reading a target serves. Two are the broker's
// proposal readings, the value of vcsReadingsAgeSeries's reading label: the
// outcomes, the proposals by state, and the details, everything the sweep's
// second step reads. A sweep can serve the first without the second, so each
// has its own presence and its own miss streak. The third is the A2A
// gateway's people reading, the people it saw in each window. The fourth is
// the broker's top tools: its tool counter by tool and subcommand, which
// every broker body carries (a broker that has run nothing carries an empty
// one), read beside the counters the same series feeds and projected, not
// folded, as status.usage.topTools. The fifth is the chat plugin's origins
// reading, the pull requests the agent opened by the origin of the work.
type usageReadingKind string

const (
	usageReadingOutcomes usageReadingKind = "outcomes"
	usageReadingDetails  usageReadingKind = "details"
	usageReadingPeople   usageReadingKind = "people"
	usageReadingTopTools usageReadingKind = "topTools"
	usageReadingOrigins  usageReadingKind = "origins"
)

// usageToolPair is one tool and subcommand the broker's tool counter labels.
type usageToolPair struct {
	Tool       string
	Subcommand string
}

// usageToolCounts is a pair's invocations by status, over
// toolInvocationsStatusOrder; a status the body did not carry is absent.
type usageToolCounts map[string]int64

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
	{field: usageExportProposalsFromChat, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginChat: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromConsole, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginConsole: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromApi, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginAPI: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromEvent, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginEvent: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromDrift, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginDrift: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromAudit, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginAudit: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromScheduled, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginScheduled: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromIssue, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginIssue: true}}, reading: usageReadingOrigins, sum: true},
	{field: usageExportProposalsFromOther, family: proposalsOpenedSeries, filter: usageLabelFilter{proposalsOriginLabel: {proposalOriginOther: true}}, reading: usageReadingOrigins, sum: true},
}

// usageReadingKinds is every reading, in a fixed order.
var usageReadingKinds = []usageReadingKind{usageReadingOutcomes, usageReadingDetails, usageReadingPeople, usageReadingTopTools, usageReadingOrigins}

// usageReadingKindsOf is the entries of specs whose reading is one of kinds,
// in their order.
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
// reading. The origins reading is present when all nine origins resolved,
// each on at least one sample, sum fields though they are, and the body
// carried no proposalsOpenedReadAgeSeries above proposalsReadAgeLimit: the
// chat plugin serves a row for every origin, zero included, from its first
// ledger read on, and keeps serving the last counts while later reads fail,
// so only the age tells fresh counts from stale ones. A body with the counts
// and no age series, a plugin from before the age, keeps the old rule and is
// present: an agent image can be pinned apart from the operator, and nine
// fields absent with no cause would be worse than counts the plugin still
// vouches for.
func usageReadingPresence(kind usageReadingKind) func(f *usageReadingFold) bool {
	if kind == usageReadingTopTools {
		// The tool counter is the broker's own, served from its first request
		// on; a body that parsed is the reading, an empty table included.
		return func(*usageReadingFold) bool { return true }
	}
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
		if kind == usageReadingOrigins && f.originsAgeSeen && f.originsAge > proposalsReadAgeLimit.Seconds() {
			return false
		}
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
	// Dispatcher is the chat plugin's kanban dispatcher reading, nil for any
	// body but the chat_metrics listener's; an empty one when no board is
	// failing or the plugin predates the series.
	Dispatcher *usageDispatcherReading
	// ToolPairs is the broker's tool counter by tool and subcommand, under
	// the closed vocabularies, for a body asked for usageReadingTopTools;
	// nil for any other. Not folded: the poll sums it across the broker pods
	// read and projects the top rows as they stand (usageTopToolsForPoll).
	ToolPairs map[usageToolPair]usageToolCounts
}

// usageDispatcherReading is the kanban dispatcher's failing boards in one
// body: each board's consecutive failed ticks and the class of its last
// failure.
type usageDispatcherReading struct {
	Failing []usageDispatcherFailure
}

// usageDispatcherFailure is one board's failed-tick streak, and its age when
// the body served one (HasAge).
type usageDispatcherFailure struct {
	Board   string
	Error   string
	Ticks   int64
	Seconds int64
	HasAge  bool
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
	// Refs is every series' triple as the watcher labelled it, in body order,
	// one per Registered; read by the ScopeUndeclared condition (usage_scope.go).
	// The values are the peer's text, unvalidated here.
	Refs []usageClusterRef
}

// usageClusterRef is one cluster_up series' labels: the cluster's name, project
// and location as its profile's identity names them. The watcher's directly
// reached cluster carries an empty project and location until its profile is
// absorbed.
type usageClusterRef struct {
	Name     string
	Project  string
	Location string
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

// usageSource reads a listener at a pod address and the path its exposition
// is served at (usageMetricsPath for every kube-agents listener,
// usageLiteLLMMetricsPath for LiteLLM). The pod scraper is its one
// implementation; a test supplies a stub, and a deployment that cannot admit
// operator-to-pod traffic could gain another without changing the
// accumulation or the writer.
type usageSource interface {
	Scrape(ctx context.Context, addr, path string, counters []string, kinds []usageReadingKind) (usageReading, error)
}

// podUsageSource reads the exposition at a pod IP and port over the pod
// network.
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

// Scrape GETs path on the listener at addr and folds the body for each of
// counters, one sample per counter from the one body, and for each of kinds.
// Any status other than 200 is a failed scrape, a redirect included: the
// target names the path the listener serves, and the client follows nothing.
func (s *podUsageSource) Scrape(ctx context.Context, addr, path string, counters []string, kinds []usageReadingKind) (usageReading, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, usageScrapeScheme+addr+path, nil)
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
	// dispatcher collects the kanban dispatcher's failing boards, for the
	// chat_metrics listener's body alone.
	var dispatcher *usageDispatcherReading
	// dispatcherAges is each board's streak age, joined to its ticks after the body.
	var dispatcherAges map[string]int64
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
		if family == kanbanGaveUpSeries {
			dispatcher = &usageDispatcherReading{}
			dispatcherAges = map[string]int64{}
			wanted = append(wanted, kanbanDispatcherFailedTicksSeries, kanbanDispatcherFailingSecondsSeries)
		}
	}
	// The reading slot, for a body asked for readings: a running sum per spec
	// of the kinds asked for, whether a sample resolved it, and the readings
	// whose age series the body carries.
	var readings *usageReadingFold
	// pairs is the top-tools table, for a body asked for that reading: the
	// tool counter's samples by tool and subcommand under the closed
	// vocabularies, bounded by their product rather than by the body.
	var pairs map[usageToolPair]map[string]float64
	if len(kinds) > 0 {
		readings = newUsageReadingFold(kinds)
		families := readings.families()
		if slices.Contains(kinds, usageReadingTopTools) {
			pairs = map[usageToolPair]map[string]float64{}
			families = append(families, toolInvocationsSeries)
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
					// A wanted family's sample that is not a finite non-negative number
					// fails the scrape; the plugin's read age is clamped at zero on a
					// monotonic clock and printed finite, so it cannot trip this.
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
					if name == kanbanDispatcherFailedTicksSeries {
						if len(dispatcher.Failing) >= usageDispatcherBoardCeiling || value >= float64(math.MaxInt64) {
							return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
						}
						dispatcher.Failing = append(dispatcher.Failing, usageDispatcherFailure{
							Board: usageLabelValue(metric, kanbanDispatcherBoardLabel),
							Error: usageLabelValue(metric, kanbanDispatcherErrorLabel),
							Ticks: int64(value),
						})
						continue
					}
					if name == kanbanDispatcherFailingSecondsSeries {
						// Bounded to what a time.Duration holds, for the condition's message.
						if len(dispatcherAges) >= usageDispatcherBoardCeiling || value >= float64(math.MaxInt64/int64(time.Second)) {
							return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
						}
						dispatcherAges[usageLabelValue(metric, kanbanDispatcherBoardLabel)] = int64(value)
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
						clusters.Refs = append(clusters.Refs, usageClusterRef{
							Name:     usageLabelValue(metric, clusterUpClusterLabel),
							Project:  usageLabelValue(metric, clusterUpProjectLabel),
							Location: usageLabelValue(metric, clusterUpLocationLabel),
						})
						continue
					}
					if pairs != nil && name == toolInvocationsSeries {
						// Into the table and on to the counters the same
						// series feeds.
						usageFoldToolPair(pairs, metric, value)
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
		// The spend counter's dollars become micro-dollars here, rounded once
		// over the whole body; a count's scale is one and leaves it exact.
		if scale := usageSampleScaleFor(counter); scale != 1 {
			sum = math.Round(sum * scale)
		}
		if sum >= float64(math.MaxInt64) {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
		}
		samples[counter] = int64(sum)
	}
	if dispatcher != nil {
		for i := range dispatcher.Failing {
			if age, ok := dispatcherAges[dispatcher.Failing[i].Board]; ok {
				dispatcher.Failing[i].Seconds, dispatcher.Failing[i].HasAge = age, true
			}
		}
	}
	out := usageReading{Samples: samples, StartTime: start, Clusters: clusters, DataVolume: usageDataVolumeReading(volume), Dispatcher: dispatcher}
	if readings != nil {
		var ok bool
		if out.Readings, out.ReadingsPresent, ok = readings.result(); !ok {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
		}
	}
	if pairs != nil {
		var ok bool
		if out.ToolPairs, ok = usageToolPairsReading(pairs); !ok {
			return usageReading{}, &usageScrapeError{Kind: usageScrapeKindSample}
		}
	}
	return out, nil
}

// usageFoldToolPair takes one sample of the tool counter into the top-tools
// table under the closed vocabularies: a tool outside toolInvocationsSubcommands
// or a status outside toolInvocationsStatusOrder is not counted; a subcommand
// outside its tool's list, or outside any (the broker's own other), reads
// toolLabelOther; a bare tool, or a body without the label, reads
// toolSubcommandNone. The summary script folds the broker's text the same way.
func usageFoldToolPair(pairs map[usageToolPair]map[string]float64, metric *dto.Metric, value float64) {
	tool := usageLabelValue(metric, toolInvocationsToolLabel)
	status := usageLabelValue(metric, toolInvocationsStatusLabel)
	subcommands, known := toolInvocationsSubcommands[tool]
	if !known || !slices.Contains(toolInvocationsStatusOrder, status) {
		return
	}
	subcommand := usageLabelValue(metric, toolInvocationsSubcommandLabel)
	switch {
	case subcommand == "":
		subcommand = toolSubcommandNone
	case subcommand != toolSubcommandNone && !slices.Contains(subcommands, subcommand):
		subcommand = toolLabelOther
	}
	pair := usageToolPair{Tool: tool, Subcommand: subcommand}
	if pairs[pair] == nil {
		pairs[pair] = map[string]float64{}
	}
	pairs[pair][status] += value
}

// usageToolPairsReading is the table as integers, with a pair that counted
// nothing dropped; ok is false when a count is past the float64-exact ceiling,
// which no honest body reaches.
func usageToolPairsReading(pairs map[usageToolPair]map[string]float64) (map[usageToolPair]usageToolCounts, bool) {
	out := make(map[usageToolPair]usageToolCounts, len(pairs))
	for pair, counts := range pairs {
		row := usageToolCounts{}
		for status, count := range counts {
			if count > float64(usageTotalCeiling) {
				return nil, false
			}
			if count > 0 {
				row[status] = int64(count)
			}
		}
		if len(row) > 0 {
			out[pair] = row
		}
	}
	return out, true
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
// resolved it; the broker readings whose age series the body carried; and,
// for the origins reading, the age value the body carried.
type usageReadingFold struct {
	kinds    []usageReadingKind
	specs    []usageReadingSpec
	sums     []float64
	resolved []bool
	aged     map[usageReadingKind]bool
	// originsAge is the plugin's read age when the body carried it, the
	// largest of any repeated sample; originsAgeSeen says whether it did.
	originsAge     float64
	originsAgeSeen bool
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

// families is every family a field of the fold's readings is read from, and
// the age series of the readings whose presence needs one.
func (f *usageReadingFold) families() []string {
	var families []string
	if slices.ContainsFunc(f.kinds, usageReadingAged) {
		families = append(families, vcsReadingsAgeSeries)
	}
	if slices.Contains(f.kinds, usageReadingOrigins) {
		families = append(families, proposalsOpenedReadAgeSeries)
	}
	for _, spec := range f.specs {
		if !slices.Contains(families, spec.family) {
			families = append(families, spec.family)
		}
	}
	return families
}

// fold takes one sample of family name into the reading fields it feeds,
// marks a reading aged, or records the origins reading's age, and reports
// whether name is a readings family.
func (f *usageReadingFold) fold(name string, metric *dto.Metric, value float64) bool {
	if name == vcsReadingsAgeSeries && slices.ContainsFunc(f.kinds, usageReadingAged) {
		kind := usageReadingKind(usageLabelValue(metric, vcsReadingsReadingLabel))
		if slices.Contains(f.kinds, kind) && usageReadingAged(kind) {
			f.aged[kind] = true
		}
		return true
	}
	if name == proposalsOpenedReadAgeSeries && slices.Contains(f.kinds, usageReadingOrigins) {
		// The plugin renders one sample; a body that repeats it is judged by
		// the oldest, so a stale ledger cannot pass on a fresh duplicate.
		if !f.originsAgeSeen || value > f.originsAge {
			f.originsAge = value
			f.originsAgeSeen = true
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
