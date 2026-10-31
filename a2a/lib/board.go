package lib

import "time"

// The board route: the gateway's read of what the platform agent's pod
// records, for the Slack Home tab (a2a/gateway/slack_home.go). The gateway
// publishes an empty request on BoardSubject; the Hermes bridge, the one
// process beside the agent that holds the session KV's key, answers it with
// GET /v1/board and replies on the request's subject under BoardReplyPrefix.
// No model is in the loop: every field is a row something already wrote.
// A request with a body is a BoardAction instead, a board-health row's Retry
// or Drop, answered the same way with a BoardActionResult.
const (
	// BoardSubject is the subject a board request is published on.
	BoardSubject = "chat.board"
	// BoardReplyPrefix is the namespace a board request's reply subject sits
	// in: only the bridge may publish there and only the gateway may read,
	// for the reason the notify reply has a namespace of its own.
	BoardReplyPrefix = "chat.board.reply.gateway."
)

// Board is one snapshot. Every list is newest or most urgent first and
// already capped by the sender.
type Board struct {
	// GeneratedAt is when the sender read it.
	GeneratedAt time.Time `json:"generated_at"`
	// Clusters is the clusters the agent manages (a Cluster Agent profile
	// each) and any other cluster an open finding names, with their open
	// findings, worst first and capped by the sender; ClusterCount is how many
	// there are in all. A name repeated across projects is "project/cluster".
	Clusters     []BoardCluster `json:"clusters"`
	ClusterCount int            `json:"cluster_count"`
	// Running is the kanban cards in flight.
	Running []BoardCard `json:"running"`
	// PRs is the pull requests cards opened (their pr_opened events).
	PRs []BoardPR `json:"prs"`
	// Audit is the most recent fleet audit run to finish; nil when none has.
	Audit *BoardAudit `json:"audit,omitempty"`
	// FirstReportPending is onboarding's sweep filed, its first report not yet
	// out, and the sweep's findings not yet in the queue: a cluster with no
	// open finding has not been checked yet, not found clean. FirstMessagePending adds that no message
	// has primed onboarding yet, which is what delivers that report. Both are
	// absent from an older sender, which renders as before.
	FirstReportPending  bool `json:"first_report_pending,omitempty"`
	FirstMessagePending bool `json:"first_message_pending,omitempty"`
	// Error, when set, is why the sender could not read the board; the rest
	// is then empty and must not render as a clean fleet.
	Error string `json:"error,omitempty"`
}

// BoardCluster is one managed cluster and its open findings.
type BoardCluster struct {
	Name string `json:"name"`
	// Findings counts the cluster's open findings.
	Findings int `json:"findings"`
	// Severity and Top are the highest-ranked open finding's severity
	// (the findings queue's critical, major or minor) and title; empty with no findings.
	Severity string `json:"severity,omitempty"`
	Top      string `json:"top,omitempty"`
}

// BoardCard is one card in flight.
type BoardCard struct {
	Title string `json:"title"`
	// Cluster is the cluster its assignee is scoped to; empty for a card
	// assigned to the platform agent itself.
	Cluster   string    `json:"cluster,omitempty"`
	StartedAt time.Time `json:"started_at"`
}

// BoardPR is one pull request a card opened.
type BoardPR struct {
	URL      string    `json:"url"`
	Number   int       `json:"number"`
	Title    string    `json:"title"`
	OpenedAt time.Time `json:"opened_at"`
}

// BoardAudit is the last fleet audit run: its stream's name, its findings,
// and the ledger issue it rewrote.
type BoardAudit struct {
	Name       string    `json:"name"`
	Findings   int       `json:"findings"`
	Critical   int       `json:"critical"`
	IssueURL   string    `json:"issue_url,omitempty"`
	Issue      int       `json:"issue,omitempty"`
	FinishedAt time.Time `json:"finished_at"`
}

// BoardAction is the board route's other request: a Retry or Drop tap on a
// board-health row (a2a/gateway/slack_board.go), carried as the request's
// body where a snapshot request carries none. The bridge relays it to
// session-kv's POST /v1/board/actions, which acts only on cards still
// blocked, and answers with a BoardActionResult. The gateway has decided the
// presser may act before it sends one.
type BoardAction struct {
	// Action is chatmsg.RowActionRetry or chatmsg.RowActionDrop.
	Action string `json:"action"`
	// Tasks are the row's card ids.
	Tasks []string `json:"tasks"`
	// By is who pressed, for session-kv's log line.
	By string `json:"by,omitempty"`
}

// BoardActionResult is session-kv's answer to a BoardAction, relayed as it
// came.
type BoardActionResult struct {
	Action  string               `json:"action"`
	Results []BoardActionOutcome `json:"results"`
	// Error, when set, is why the bridge could not get an answer; Results is
	// then empty and says nothing about the cards.
	Error string `json:"error,omitempty"`
}

// BoardActionOutcome is one card's: BoardOutcomeDone, BoardOutcomeMoved (no
// longer blocked), BoardOutcomeGone (no such card, or archived), or failed.
type BoardActionOutcome struct {
	ID      string `json:"id"`
	Title   string `json:"title,omitempty"`
	Status  string `json:"status,omitempty"`
	Outcome string `json:"outcome"`
}

// The outcomes of a BoardActionOutcome that settle its card.
const (
	BoardOutcomeDone  = "done"
	BoardOutcomeMoved = "moved"
	BoardOutcomeGone  = "gone"
)
