package lib

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

// TaskState is the A2A task state machine, plus rejected. auth-required is
// reserved alongside the authority field.
type TaskState string

const (
	StateSubmitted     TaskState = "submitted"
	StateWorking       TaskState = "working"
	StateInputRequired TaskState = "input-required"
	StateCompleted     TaskState = "completed"
	StateFailed        TaskState = "failed"
	StateCanceled      TaskState = "canceled"
	StateRejected      TaskState = "rejected"
	StateAuthRequired  TaskState = "auth-required"
)

// Terminal reports whether the state ends the task.
func (s TaskState) Terminal() bool {
	switch s {
	case StateCompleted, StateFailed, StateCanceled, StateRejected:
		return true
	}
	return false
}

func validTaskState(s TaskState) bool {
	switch s {
	case StateSubmitted, StateWorking, StateInputRequired, StateCompleted,
		StateFailed, StateCanceled, StateRejected, StateAuthRequired:
		return true
	}
	return false
}

// Reserved artifact names. The set of names is open; only these carry
// reserved semantics.
const (
	ArtifactResult   = "result"
	ArtifactThinking = "thinking"
	ArtifactActivity = "activity"
	ArtifactProgress = "progress"
	// ArtifactDelegate is the session's request to the gateway to mint a
	// child task: one data part {"addressee","text"} on the session's own
	// task events. Reserved so the relay and the adapter spell it once
	// (spec-a2a-payloads.md, "Reserved artifact names").
	ArtifactDelegate = "delegate"
	// ArtifactTurn is one finished turn's answer on a task that has more
	// turns to run: an executor that queues follow-ups (the hermes-bridge)
	// publishes each earlier turn's answer under it, chunked like result,
	// and the last turn's answer as result. Renderers post it as it
	// completes; it is never the deliverable (spec-a2a-payloads.md,
	// "Reserved artifact names").
	ArtifactTurn = "turn"
	// ArtifactNotice is a message the task posts on its own, beside its
	// live message and its answer (a PR it opened): a text part, optionally
	// with a chat message. Each one posts once and is never edited
	// (spec-a2a-payloads.md, "Reserved artifact names").
	ArtifactNotice = "notice"
	// ArtifactHandoff says a completed task handed work to kanban cards,
	// which report into the task's conversation themselves: one text part,
	// HandoffCards. The hermes-bridge's API executor publishes it ahead of the
	// terminal of such a task whenever the conversation route was recorded
	// (Slack or Google Chat); the gateway never
	// renders it and reads it as a delegated child's cards-pending signal
	// beside the terminal's chat mark, which the bridge sets only on Slack
	// with KAGE_SLACK_UX on (#3258; spec-a2a-payloads.md, "Reserved artifact
	// names").
	ArtifactHandoff = "handoff"
	// ArtifactUsage is what a task's turns cost: one data part per finished
	// turn, a Usage object, published by the executor before the turn's
	// answer settles (the worker adapter on each harness result, the
	// hermes-bridge on each API turn's answer) and never after the
	// terminal. Every field is optional and a reader tolerates the
	// artifact's absence; UsageTotal folds a task's turns (#2816;
	// spec-a2a-payloads.md, "Reserved artifact names").
	ArtifactUsage = "usage"
)

// HandoffCards is the ArtifactHandoff text part: the work went to kanban
// cards.
const HandoffCards = "cards"

// Usage is one finished turn's cost, the ArtifactUsage data part. Model is
// the model the executor asked for or was told it ran; InputTokens is every
// token the model read for the turn, cached reads included, and OutputTokens
// every token it wrote; CostUsd is the producer's own estimate, carried only
// when the producer had a figure it did not invent (a harness that prices
// its turns; never Hermes behind the gateway, which records 0.0); DurationMs
// is the turn's wall time as the producer measured it. A zero is "not
// known" and is omitted on the wire, so a reader never mistakes unknown for
// free; a part with no figure at all is refused.
type Usage struct {
	Model        string  `json:"model,omitempty"`
	InputTokens  int64   `json:"inputTokens,omitempty"`
	OutputTokens int64   `json:"outputTokens,omitempty"`
	CostUsd      float64 `json:"costUsd,omitempty"`
	DurationMs   int64   `json:"durationMs,omitempty"`
}

// hasFigure reports whether u carries at least one figure; a model name
// alone is not usage.
func (u Usage) hasFigure() bool {
	return u.InputTokens > 0 || u.OutputTokens > 0 || u.CostUsd > 0 || u.DurationMs > 0
}

// negative reports whether any figure is below zero, which no producer can
// have measured.
func (u Usage) negative() bool {
	return u.InputTokens < 0 || u.OutputTokens < 0 || u.CostUsd < 0 || u.DurationMs < 0
}

// UsagePart is u as the ArtifactUsage data part: the object itself, as an
// activity entry is. A usage with no figure, or a negative one, is refused
// here so it never reaches the bus.
func UsagePart(u Usage) (Part, error) {
	if !u.hasFigure() || u.negative() {
		return Part{}, fmt.Errorf("usage part carries no figure, or a negative one: %+v", u)
	}
	data, err := json.Marshal(u)
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// UsageOf reads p as a usage part. A part that is not data, does not decode
// as an object, carries no figure or a negative one is not usage; a key the
// reader does not know is ignored.
func UsageOf(p Part) (Usage, bool) {
	if p.Kind != "data" || len(p.Data) == 0 {
		return Usage{}, false
	}
	var u Usage
	if err := json.Unmarshal(p.Data, &u); err != nil || !u.hasFigure() || u.negative() {
		return Usage{}, false
	}
	return u, true
}

// UsageFigure is a producer's count or duration, read from a float, as the
// whole number a Usage part carries, and whether it is one: a figure that is
// not finite, below zero or at or above 2^63 (which int64 cannot hold and a
// conversion would wrap) is not, and the producer drops that field and names
// it rather than refuse the part or publish a wrapped value.
func UsageFigure(f float64) (int64, bool) {
	if math.IsNaN(f) || math.IsInf(f, 0) || f < 0 || f >= math.MaxInt64 {
		return 0, false
	}
	return int64(f), true
}

// UsageTotal folds the turns of one task: the figures sum and Model is the
// last one named. An absent figure stays absent.
func UsageTotal(turns []Usage) Usage {
	var total Usage
	for _, u := range turns {
		if u.Model != "" {
			total.Model = u.Model
		}
		total.InputTokens += u.InputTokens
		total.OutputTokens += u.OutputTokens
		total.CostUsd += u.CostUsd
		total.DurationMs += u.DurationMs
	}
	return total
}

// DelegateRequest is the ArtifactDelegate data part's shape: the child tasks
// to mint, either one (addressee, text and parameters) or a batch (Tasks). It
// is also the wire shape the session pod's MCP server sends over the
// adapter's unix socket (the socket hop is how the call gets from the
// harness's delegate tool to the adapter that actually publishes the
// artifact), so it is named here, beside ArtifactDelegate, rather than
// duplicated per caller.
//
// Parameters are the child task's parameters (TaskParameters), the target
// cluster among them. The contract (spec-a2a-payloads.md, "Task parameters
// and child results"): the gateway carries them into the child's submission
// and refuses a child whose target is not inside its parent's. The gateway
// holds the names to the target profile's declared ones.
//
// Tasks is a batch: several children minted from one request, each entry
// shaped like the single fields (spec-a2a-payloads.md, "Task parameters and
// child results"). A request sets the single fields or Tasks, never both
// (ValidateDelegateRequest). An adapter sends Tasks only to a gateway that
// advertised a width over one on the turn (DelegateTargets.WidthMax), so a
// gateway built before batches never sees one, and a request without Tasks is
// the single shape on the wire, unchanged.
type DelegateRequest struct {
	Addressee  string            `json:"addressee,omitempty"`
	Text       string            `json:"text,omitempty"`
	Parameters map[string]string `json:"parameters,omitempty"`
	Tasks      []DelegateTask    `json:"tasks,omitempty"`
}

// DelegateTask is one entry of a batch delegation: the child's addressee, its
// text (held to DelegateTextCap, like the single shape's) and its parameters.
type DelegateTask struct {
	Addressee  string            `json:"addressee"`
	Text       string            `json:"text"`
	Parameters map[string]string `json:"parameters,omitempty"`
}

// DelegateTextCap bounds DelegateRequest.Text, and each batch entry's text, in
// bytes: the adapter refuses a longer request before it reaches the bus and
// the gateway ignores one that arrives anyway, so the two sides cannot
// disagree on the number.
const DelegateTextCap = 16 * 1024

// DelegateBatchMax is the ceiling on a batch's length, whatever width a
// gateway is configured with: the gateway refuses a width setting above it,
// and every reader refuses a longer batch as malformed. The width a turn may
// use is the gateway's setting, advertised as DelegateTargets.WidthMax.
const DelegateBatchMax = 16

// IsBatch reports a request that carries a tasks list (possibly empty, which
// ValidateDelegateRequest refuses).
func (r DelegateRequest) IsBatch() bool {
	return r.Tasks != nil
}

// Entries is the request as the children it asks for: the single fields as
// one entry, or each batch entry in order.
func (r DelegateRequest) Entries() []DelegateTask {
	if r.IsBatch() {
		return r.Tasks
	}
	return []DelegateTask{{Addressee: r.Addressee, Text: r.Text, Parameters: r.Parameters}}
}

// ValidateDelegateRequest holds a request to its shape: the single fields or a
// batch, never both; a batch neither empty nor past DelegateBatchMax; and each
// entry's text present and within DelegateTextCap. The addressee and the
// parameters are checked by each side's own rules per entry (the adapter's
// validateDelegate, the gateway's route and allowlists).
func ValidateDelegateRequest(r DelegateRequest) error {
	if r.IsBatch() {
		if r.Addressee != "" || r.Text != "" || len(r.Parameters) > 0 {
			return errors.New("delegate: send either addressee and text, or tasks, not both")
		}
		if len(r.Tasks) == 0 {
			return errors.New("delegate: the tasks list is empty")
		}
		if len(r.Tasks) > DelegateBatchMax {
			return fmt.Errorf("delegate: %d tasks, at most %d in one request", len(r.Tasks), DelegateBatchMax)
		}
		for i, e := range r.Tasks {
			if err := validateDelegateText(e.Text); err != nil {
				return fmt.Errorf("delegate: task %d: %w", i+1, err)
			}
		}
		return nil
	}
	if err := validateDelegateText(r.Text); err != nil {
		return fmt.Errorf("delegate: %w", err)
	}
	return nil
}

func validateDelegateText(text string) error {
	switch {
	case strings.TrimSpace(text) == "":
		return errors.New("text is required")
	case len(text) > DelegateTextCap:
		return errors.New("text is too long for a delegation")
	}
	return nil
}

// TaskParameters is the taskParameters data part on a profile task's
// submission: the parameters its persona declares (the cluster persona's
// project, location and cluster), set by the gateway from what the client
// or the parent task asked. The dispatcher builds the verifier's resource
// from them and hands each to the worker as PROFILE_TASK_<NAME>; the
// verifier refuses a target outside the grant's scope, which is what makes
// these message fields authoritative. They never ride the authority block.
type TaskParameters struct {
	Parameters map[string]string `json:"parameters"`
	// ChangeRequested says the requester asked for a change (a pull
	// request) rather than an answer, and so whether the dispatcher grants
	// the worker the permit to open one. Only the gateway sets it, and only
	// true for a turn that arrived as a structured confirm action, never
	// from free text or from a delegation's arguments, which are the
	// model's; a child carries its parent's value from the gateway's own
	// record. Both rules are the gateway's to keep (#4457): the dispatcher
	// can't tell a parent's value from a raised one. Absent means false.
	ChangeRequested bool `json:"changeRequested,omitempty"`
}

// The bounds ValidateTaskParameters holds a parameter set to. A name is a
// lower-case identifier (TaskParameterNameRE), as a persona declares it and as the worker's
// PROFILE_TASK_<NAME> spells it upper-cased. A value is one token with no
// '/' or space, because the dispatcher joins values into a verifier resource
// path (project/<p>/location/<l>/cluster/<c>) and a '/' would let a value
// name a different resource. It starts with a letter or digit, so it is
// never "." or ".." (a path step) and never reads as a flag ("-x") where a
// worker passes it to a command.
const (
	TaskParametersMax        = 16
	TaskParameterValueMaxLen = 128
)

var (
	// TaskParameterNameRE is a parameter name: the persona loader declares
	// names by it (lib/persona), and a submission carries names by it, so a
	// name a persona can declare is one its tasks can be handed.
	TaskParameterNameRE = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)
	taskParameterValue  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]*$`)
)

// ValidateTaskParameters reports the first way params break the bounds
// above, or nil. An empty or nil set is valid: a task with no parameters.
func ValidateTaskParameters(params map[string]string) error {
	if len(params) > TaskParametersMax {
		return fmt.Errorf("task parameters: %d, at most %d", len(params), TaskParametersMax)
	}
	for name, value := range params {
		if !TaskParameterNameRE.MatchString(name) {
			return fmt.Errorf("task parameter name %q is not a lower-case identifier of at most 63 characters", name)
		}
		if len(value) > TaskParameterValueMaxLen || !taskParameterValue.MatchString(value) {
			return fmt.Errorf("task parameter %s: the value is not one token (a letter or digit, then letters, digits, . _ : -) of at most %d characters", name, TaskParameterValueMaxLen)
		}
	}
	return nil
}

const taskParametersKey = "taskParameters"

// TaskParametersPart is p as a data part, refused when p breaks the bounds.
func TaskParametersPart(p TaskParameters) (Part, error) {
	if err := ValidateTaskParameters(p.Parameters); err != nil {
		return Part{}, err
	}
	if p.Parameters == nil {
		p.Parameters = map[string]string{}
	}
	data, err := json.Marshal(map[string]TaskParameters{taskParametersKey: p})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// TaskParametersOf finds the taskParameters part among parts. A data part
// that is not one is skipped. One that is but breaks the bounds is an
// error rather than skipped, so a reader never runs a task as though it had
// no parameters when its submission carried bad ones; so are two such
// parts, which leave the task's target ambiguous.
func TaskParametersOf(parts []Part) (TaskParameters, bool, error) {
	var found *TaskParameters
	for _, p := range parts {
		raw, ok := wrappedData(p, taskParametersKey)
		if !ok {
			continue
		}
		if found != nil {
			return TaskParameters{}, true, errors.New("task parameters: more than one taskParameters part")
		}
		var tp TaskParameters
		if err := json.Unmarshal(raw, &tp); err != nil {
			return TaskParameters{}, true, fmt.Errorf("task parameters: %w", err)
		}
		// A null part, a null set or a misspelt field decodes to no
		// parameters at all; that is a broken part, not an empty set, which
		// is written {"parameters":{}}.
		if tp.Parameters == nil {
			return TaskParameters{}, true, errors.New("task parameters: the part carries no parameters object")
		}
		if err := ValidateTaskParameters(tp.Parameters); err != nil {
			return TaskParameters{}, true, err
		}
		found = &tp
	}
	if found == nil {
		return TaskParameters{}, false, nil
	}
	return *found, true, nil
}

// DelegateTargets is the delegateTargets data part on a session turn's
// submission: the agents the turn's requester may delegate to, as the gateway
// resolved them when it submitted the turn (platform, then each AgentProfile
// whose card admits them). The session pod's delegate tool names them in its
// addressee description, so the model is told what it can address rather than
// a fixed name. It's a hint for the model, not a grant: the gateway checks a
// delegation against the same rules when it arrives, so a stale list costs at
// most a refused delegation.
//
// The part's presence is the marker: a turn that carries one has exactly the
// agents listed (an empty list is none), and a turn with none reads as
// platform only. Truncated says the gateway left agents out to stay inside
// the bounds below.
//
// WidthMax is how many children one delegation from this turn may ask for
// (the gateway's A2A_DELEGATION_WIDTH_MAX). Absent means one: the single
// shape only, which is all a gateway built before batches takes, so the
// session's tool offers a batch only when the part says more (Width).
//
// RoundsMax is how many delegate requests this turn's task may make (the
// gateway's A2A_DELEGATION_ROUNDS_MAX), at most DelegateRoundsCeiling. Absent
// means one, which is all a gateway built before rounds takes (Rounds).
type DelegateTargets struct {
	Targets   []DelegateTarget `json:"targets"`
	Truncated bool             `json:"truncated,omitempty"`
	WidthMax  int              `json:"widthMax,omitempty"`
	RoundsMax int              `json:"roundsMax,omitempty"`
}

// DelegateRoundsCeiling bounds a task's delegation rounds: the most
// A2A_DELEGATION_ROUNDS_MAX may be set to, the most a delegateTargets part
// may advertise, and the highest Round a childResult or delegationRefused part
// may carry, so a forged part cannot put an arbitrary round into a reader's
// dedupe map.
const DelegateRoundsCeiling = 16

// Rounds is the delegation rounds the part advertises: RoundsMax, or one when
// unset.
func (t DelegateTargets) Rounds() int {
	if t.RoundsMax < 1 {
		return 1
	}
	return t.RoundsMax
}

// validRound: a round as a part carries it, 1-based, with 0 (omitted) read as
// one, and at most DelegateRoundsCeiling.
func validRound(r int) bool { return r >= 0 && r <= DelegateRoundsCeiling }

// Width is the batch width the part advertises: WidthMax, or one when unset.
func (t DelegateTargets) Width() int {
	if t.WidthMax < 1 {
		return 1
	}
	return t.WidthMax
}

// DelegateTarget is one agent a session may delegate to: its addressee, its
// card's routing blurb, and the parameters its card declares (a pinned
// profile's cluster, say), which a delegation may pass.
type DelegateTarget struct {
	Name        string             `json:"name"`
	Description string             `json:"description,omitempty"`
	Parameters  []ProfileParameter `json:"parameters,omitempty"`
}

// The bounds a delegateTargets part is held to. A name is a subject token (an
// addressee); a description is one line of at most DelegateTargetDescriptionMax
// bytes, cut by the gateway (DelegateTargetDescription); a parameter is named
// by TaskParameterNameRE, at most TaskParametersMax a target. The encoded part
// is at most DelegateTargetsMaxBytes, so the session pod can hand it to its
// tool server in one environment variable.
const (
	DelegateTargetsMax           = 32
	DelegateTargetDescriptionMax = 200
	DelegateTargetsMaxBytes      = 16 * 1024
)

const delegateTargetsKey = "delegateTargets"

// DelegateTargetDescription is a card's description as a delegate target
// carries it: control characters and line breaks folded to spaces, runs of
// space collapsed, and cut to DelegateTargetDescriptionMax bytes on a rune
// boundary.
func DelegateTargetDescription(s string) string {
	s = strings.Join(strings.Fields(strings.Map(func(r rune) rune {
		if unicode.IsControl(r) {
			return ' '
		}
		return r
	}, s)), " ")
	if len(s) <= DelegateTargetDescriptionMax {
		return s
	}
	cut := DelegateTargetDescriptionMax
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	// A cut that lands after a space would leave it trailing, and the
	// description would no longer read back as itself.
	return strings.TrimRight(s[:cut], " ")
}

func validateDelegateTargets(t DelegateTargets) error {
	if len(t.Targets) > DelegateTargetsMax {
		return fmt.Errorf("delegate targets: %d, at most %d", len(t.Targets), DelegateTargetsMax)
	}
	if t.WidthMax < 0 || t.WidthMax > DelegateBatchMax {
		return fmt.Errorf("delegate targets: width %d, want 1 to %d", t.WidthMax, DelegateBatchMax)
	}
	if !validRound(t.RoundsMax) {
		return fmt.Errorf("delegate targets: rounds %d, want 1 to %d", t.RoundsMax, DelegateRoundsCeiling)
	}
	for _, d := range t.Targets {
		if !ValidSubjectToken(d.Name) {
			return fmt.Errorf("delegate target %q is not an addressee", d.Name)
		}
		if d.Description != DelegateTargetDescription(d.Description) {
			return fmt.Errorf("delegate target %s: the description is not one line of at most %d bytes", d.Name, DelegateTargetDescriptionMax)
		}
		if len(d.Parameters) > TaskParametersMax {
			return fmt.Errorf("delegate target %s: %d parameters, at most %d", d.Name, len(d.Parameters), TaskParametersMax)
		}
		for _, p := range d.Parameters {
			if !TaskParameterNameRE.MatchString(p.Name) {
				return fmt.Errorf("delegate target %s: parameter %q is not a lower-case identifier", d.Name, p.Name)
			}
		}
	}
	if raw, err := json.Marshal(t); err != nil || len(raw) > DelegateTargetsMaxBytes {
		return fmt.Errorf("delegate targets: encoded past %d bytes", DelegateTargetsMaxBytes)
	}
	return nil
}

// DelegateTargetsPart is t as a data part, refused when t breaks the bounds.
func DelegateTargetsPart(t DelegateTargets) (Part, error) {
	if err := validateDelegateTargets(t); err != nil {
		return Part{}, err
	}
	if t.Targets == nil {
		t.Targets = []DelegateTarget{}
	}
	data, err := json.Marshal(map[string]DelegateTargets{delegateTargetsKey: t})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// DelegateTargetsOf finds the delegateTargets part among parts. A part that
// breaks the bounds, or a second one, is an error, and the reader falls back
// to what it would show with none.
func DelegateTargetsOf(parts []Part) (DelegateTargets, bool, error) {
	var found *DelegateTargets
	for _, p := range parts {
		raw, ok := wrappedData(p, delegateTargetsKey)
		if !ok {
			continue
		}
		if found != nil {
			return DelegateTargets{}, true, errors.New("delegate targets: more than one delegateTargets part")
		}
		var t DelegateTargets
		if err := json.Unmarshal(raw, &t); err != nil {
			return DelegateTargets{}, true, fmt.Errorf("delegate targets: %w", err)
		}
		if t.Targets == nil {
			return DelegateTargets{}, true, errors.New("delegate targets: the part carries no targets list")
		}
		if err := validateDelegateTargets(t); err != nil {
			return DelegateTargets{}, true, err
		}
		found = &t
	}
	if found == nil {
		return DelegateTargets{}, false, nil
	}
	return *found, true, nil
}

// ScopeClusters is the scopeClusters data part a delegating profile
// parent's submission carries beside delegateTargets: one bounded page of
// the install's in-scope clusters, each one a `cluster` child delegation
// would be accepted for, in (project, location, cluster) order. Count is
// how many clusters are in scope in all, so Truncated is Count past the
// page. Cursor is reserved for a later paging contract and is always empty
// for now; a reader ignores it.
//
// Three fields say what the page can't. Unknown is set when the gateway has
// no full cluster list to count (the resolved scope carries none, or dropped
// it for size): Count is then only the clusters it knows of, a lower bound,
// and Truncated is always set. UnlistedProjects names projects in scope
// whose clusters no list the gateway reads carries (an explicitly declared
// project, the management project, a member not yet listed), sorted and cut
// at ScopeClustersUnlistedMax, with UnlistedCount the total; their clusters
// are listed elsewhere, and a delegation to one is still checked as it
// arrives. OldestListing is when the oldest listing the counted clusters
// came from was read (RFC 3339), so a reader can tell a stale one; it is
// left out when that age is unknown (a stamp ahead of the gateway's clock),
// so clusters counted with no OldestListing have an unknown age.
//
// ParentScoped says the parent is scoped to one cluster, so the page is that
// cluster or nothing and Count is 0 or 1. Unbounded says the install's scope
// declares no boundary, so any project not excluded is in scope: Unknown is
// set and no project is named as listed elsewhere.
//
// Every one of these is left out when unset, so a part without them is the
// shape first written.
type ScopeClusters struct {
	Count            int            `json:"count"`
	Clusters         []ScopeCluster `json:"clusters"`
	Truncated        bool           `json:"truncated"`
	Cursor           string         `json:"cursor"`
	Unknown          bool           `json:"unknown,omitempty"`
	UnlistedProjects []string       `json:"unlistedProjects,omitempty"`
	UnlistedCount    int            `json:"unlistedCount,omitempty"`
	OldestListing    string         `json:"oldestListing,omitempty"`
	ParentScoped     bool           `json:"parentScoped,omitempty"`
	Unbounded        bool           `json:"unbounded,omitempty"`
}

// ScopeCluster is one cluster on a scopeClusters page: the target a
// `cluster` delegation names it by.
type ScopeCluster struct {
	Project  string `json:"project"`
	Location string `json:"location"`
	Cluster  string `json:"cluster"`
}

// ScopeClustersPageMax is the most clusters one scopeClusters page carries:
// the widest batch any gateway accepts (DelegateBatchMax). A gateway caps its
// page at its own delegation width under this bound, so the page is one
// batch's worth of targets. ScopeClustersUnlistedMax is the most projects
// UnlistedProjects names. ScopeClustersMaxBytes bounds the encoded part,
// which the session pod hands its tool server in one environment variable;
// the widest page and unlisted list of the longest labels fit under it.
const (
	ScopeClustersPageMax     = DelegateBatchMax
	ScopeClustersUnlistedMax = 16
	ScopeClustersMaxBytes    = 8 * 1024
)

const scopeClustersKey = "scopeClusters"

func validateScopeClusters(s ScopeClusters) error {
	if len(s.Clusters) > ScopeClustersPageMax {
		return fmt.Errorf("scope clusters: a page of %d, at most %d", len(s.Clusters), ScopeClustersPageMax)
	}
	if s.Count < len(s.Clusters) {
		return fmt.Errorf("scope clusters: count %d is under the page's %d", s.Count, len(s.Clusters))
	}
	if s.Unknown && !s.Truncated {
		return errors.New("scope clusters: an unknown count is always truncated")
	}
	if s.Unbounded && (!s.Unknown || len(s.UnlistedProjects) > 0 || s.UnlistedCount > 0) {
		return errors.New("scope clusters: an unbounded scope is unknown and names no unlisted projects")
	}
	if s.ParentScoped && (s.Count > 1 || s.Unknown || s.Unbounded || s.UnlistedCount > 0) {
		return errors.New("scope clusters: a parent scoped to one cluster has a known count of at most one and nothing unlisted")
	}
	if !s.Unknown && s.Truncated != (s.Count > len(s.Clusters)) {
		return fmt.Errorf("scope clusters: truncated is %t with count %d and a page of %d", s.Truncated, s.Count, len(s.Clusters))
	}
	if s.Cursor != "" {
		return errors.New("scope clusters: the cursor is reserved and must be empty")
	}
	for _, c := range s.Clusters {
		if !validDNS1123Label(c.Project) || !validDNS1123Label(c.Location) || !validDNS1123Label(c.Cluster) {
			return fmt.Errorf("scope clusters: %q/%q/%q is not a cluster target", c.Project, c.Location, c.Cluster)
		}
	}
	if len(s.UnlistedProjects) > ScopeClustersUnlistedMax {
		return fmt.Errorf("scope clusters: %d unlisted projects, at most %d", len(s.UnlistedProjects), ScopeClustersUnlistedMax)
	}
	if s.UnlistedCount < len(s.UnlistedProjects) {
		return fmt.Errorf("scope clusters: unlisted count %d is under the %d named", s.UnlistedCount, len(s.UnlistedProjects))
	}
	for _, p := range s.UnlistedProjects {
		if !validDNS1123Label(p) {
			return fmt.Errorf("scope clusters: unlisted %q is not a project", p)
		}
	}
	if s.OldestListing != "" {
		if _, err := time.Parse(time.RFC3339, s.OldestListing); err != nil {
			return fmt.Errorf("scope clusters: oldest listing %q is not a time", s.OldestListing)
		}
	}
	if raw, err := json.Marshal(s); err != nil || len(raw) > ScopeClustersMaxBytes {
		return fmt.Errorf("scope clusters: encoded past %d bytes", ScopeClustersMaxBytes)
	}
	return nil
}

// ScopeClustersPart is s as a data part, refused when s breaks the bounds.
func ScopeClustersPart(s ScopeClusters) (Part, error) {
	if s.Clusters == nil {
		s.Clusters = []ScopeCluster{}
	}
	if err := validateScopeClusters(s); err != nil {
		return Part{}, err
	}
	data, err := json.Marshal(map[string]ScopeClusters{scopeClustersKey: s})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// ScopeClustersOf finds the scopeClusters part among parts. A part that
// breaks the bounds, or a second one, is an error, and the reader shows no
// page.
func ScopeClustersOf(parts []Part) (ScopeClusters, bool, error) {
	var found *ScopeClusters
	for _, p := range parts {
		raw, ok := wrappedData(p, scopeClustersKey)
		if !ok {
			continue
		}
		if found != nil {
			return ScopeClusters{}, true, errors.New("scope clusters: more than one scopeClusters part")
		}
		var s ScopeClusters
		if err := json.Unmarshal(raw, &s); err != nil {
			return ScopeClusters{}, true, fmt.Errorf("scope clusters: %w", err)
		}
		if s.Clusters == nil {
			return ScopeClusters{}, true, errors.New("scope clusters: the part carries no clusters list")
		}
		if err := validateScopeClusters(s); err != nil {
			return ScopeClusters{}, true, err
		}
		found = &s
	}
	if found == nil {
		return ScopeClusters{}, false, nil
	}
	return *found, true, nil
}

// ReplyTo is the replyTo data part: the task whose stored report this turn
// answers (spec-a2a-payloads.md, "Task parameters and child results");
// written by the gateway only.
type ReplyTo struct {
	TaskID string `json:"taskId"`
}

const replyToKey = "replyTo"

// ReplyToPart is r as a data part, refused when its task id is not a
// subject token.
func ReplyToPart(r ReplyTo) (Part, error) {
	if !ValidSubjectToken(r.TaskID) {
		return Part{}, fmt.Errorf("reply to: %q is not a task id", r.TaskID)
	}
	data, err := json.Marshal(map[string]ReplyTo{replyToKey: r})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// ReplyToOf finds the replyTo part among parts. A part whose task id is not
// a subject token, or a second one, is an error, and the reader goes on as
// though the turn named no report.
func ReplyToOf(parts []Part) (ReplyTo, bool, error) {
	var found *ReplyTo
	for _, p := range parts {
		raw, ok := wrappedData(p, replyToKey)
		if !ok {
			continue
		}
		if found != nil {
			return ReplyTo{}, true, errors.New("reply to: more than one replyTo part")
		}
		var r ReplyTo
		if err := json.Unmarshal(raw, &r); err != nil {
			return ReplyTo{}, true, fmt.Errorf("reply to: %w", err)
		}
		if !ValidSubjectToken(r.TaskID) {
			return ReplyTo{}, true, fmt.Errorf("reply to: %q is not a task id", r.TaskID)
		}
		found = &r
	}
	if found == nil {
		return ReplyTo{}, false, nil
	}
	return *found, true, nil
}

// PushedReport is the pushedReport data part: a report the gateway's notify
// route posted into the conversation, put ahead of a reply under it, with
// the task the report was on when its sender named one; written by the
// gateway only (spec-a2a-payloads.md, "Task parameters and child results").
type PushedReport struct {
	TaskID string `json:"taskId,omitempty"`
	Text   string `json:"text"`
}

// PushedReportTextCap bounds PushedReport.Text in bytes; the gateway cuts a
// longer report and says so (ChildResultTextCap's value).
const PushedReportTextCap = 64 * 1024

const pushedReportKey = "pushedReport"

// PushedReportPart is r as a data part, refused with no text, text past
// PushedReportTextCap, or a task id that is not a subject token.
func PushedReportPart(r PushedReport) (Part, error) {
	if err := validatePushedReport(r); err != nil {
		return Part{}, err
	}
	data, err := json.Marshal(map[string]PushedReport{pushedReportKey: r})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

func validatePushedReport(r PushedReport) error {
	if r.Text == "" {
		return errors.New("pushed report: no text")
	}
	if len(r.Text) > PushedReportTextCap {
		return fmt.Errorf("pushed report: %d bytes of text, at most %d", len(r.Text), PushedReportTextCap)
	}
	if r.TaskID != "" && !ValidSubjectToken(r.TaskID) {
		return fmt.Errorf("pushed report: %q is not a task id", r.TaskID)
	}
	return nil
}

// PushedReportOf finds the pushedReport part among parts. A part breaking
// the bounds, or a second one, is an error, and the reader goes on as
// though the turn carried none.
func PushedReportOf(parts []Part) (PushedReport, bool, error) {
	var found *PushedReport
	for _, p := range parts {
		raw, ok := wrappedData(p, pushedReportKey)
		if !ok {
			continue
		}
		if found != nil {
			return PushedReport{}, true, errors.New("pushed report: more than one pushedReport part")
		}
		var r PushedReport
		if err := json.Unmarshal(raw, &r); err != nil {
			return PushedReport{}, true, fmt.Errorf("pushed report: %w", err)
		}
		if err := validatePushedReport(r); err != nil {
			return PushedReport{}, true, err
		}
		found = &r
	}
	if found == nil {
		return PushedReport{}, false, nil
	}
	return *found, true, nil
}

// ChildResult is the childResult data part: a child task's end, which the
// gateway delivers on its parent's .in as a message, for a parent that waits
// by staying open (a profile task under the dispatcher). A chat session's
// turn ends when it delegates, so a session parent is woken with the child's
// result as a new turn instead (spec-a2a-payloads.md, "Task parameters and
// child results").
type ChildResult struct {
	ChildTaskID string    `json:"childTaskId"`
	Addressee   string    `json:"addressee"`
	State       TaskState `json:"state"`
	// Text is the child's deliverable when it completed, and the reason it
	// gave otherwise; at most ChildResultTextCap bytes, cut by the gateway.
	Text string `json:"text,omitempty"`
	// Entry is the 1-based index of the batch entry this result ends, on
	// every end of a batch, and 0 for a single delegation (a tasks list of
	// one included). The gateway sends each entry of a batch one end, which
	// can arrive twice or, across a restart, be lost: a parent waiting on a
	// batch dedupes its ends on Entry and counts them to the batch's size,
	// with its own deadline bounding the wait, and dedupes a single
	// delegation's on ChildTaskID. An entry that never got a child (its
	// batch was cut short and withdrawn) ends failed with no ChildTaskID.
	Entry int `json:"entry,omitempty"`
	// Round is the 1-based delegation round whose request minted the child,
	// omitted for round one, at most DelegateRoundsCeiling. A parent dedupes
	// a batch's ends on (Round, Entry), 0 read as one.
	Round int `json:"round,omitempty"`
}

// validChildResult: a terminal end, with text in the cap, that names its
// child, or a batch entry that never got one and so ended failed.
func validChildResult(c ChildResult) bool {
	if !c.State.Terminal() || len(c.Text) > ChildResultTextCap || c.Entry < 0 || c.Entry > DelegateBatchMax || !validRound(c.Round) {
		return false
	}
	if c.ChildTaskID == "" {
		return c.Entry >= 1 && c.State == StateFailed
	}
	return ValidSubjectToken(c.ChildTaskID)
}

// ChildResultTextCap bounds ChildResult.Text in bytes. The gateway cuts a
// longer deliverable and says so; the child's own result artifact keeps it
// whole on TASKS.
const ChildResultTextCap = 64 * 1024

const childResultKey = "childResult"

// ChildResultPart is c as a data part, refused without a terminal state,
// with text past ChildResultTextCap, or without a child task id unless it
// ends a batch entry that never got a child (validChildResult).
func ChildResultPart(c ChildResult) (Part, error) {
	if !validChildResult(c) {
		return Part{}, fmt.Errorf("child result: needs a terminal state, at most %d bytes of text, and a task id or a not-started batch entry", ChildResultTextCap)
	}
	data, err := json.Marshal(map[string]ChildResult{childResultKey: c})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// ChildResultOf finds the childResult part among parts. A data part that is
// not one, or one ChildResultPart would refuse, is skipped: a malformed
// child result is no event, where a
// malformed parameter set (TaskParametersOf) would change what runs.
func ChildResultOf(parts []Part) (ChildResult, bool) {
	for _, p := range parts {
		raw, ok := wrappedData(p, childResultKey)
		if !ok {
			continue
		}
		var c ChildResult
		if json.Unmarshal(raw, &c) != nil || !validChildResult(c) {
			continue
		}
		return c, true
	}
	return ChildResult{}, false
}

// DelegationRefusal is the delegationRefused data part: the gateway's word
// to a profile parent, on its .in, that its delegate request minted no child
// (#4860). A refused request mints nothing, so there is no child to end and no
// childResult to send; without this the parent waits to its deadline. Text is
// the notice the room sees, at most ChildResultTextCap bytes. A parent that
// waits on children reads a first refusal as the end of the whole wait.
//
// Round is the 1-based delegation round the refused request would have been,
// omitted for round one, at most DelegateRoundsCeiling. A parent ends its wait
// only on a refusal for the round it waits on: a refusal of a request made
// after the task had used its rounds carries a later round, and ends nothing,
// so the children of the earlier request are still the task's to wait for.
// Any other JSON type there, or a round out of bounds, makes the part no
// refusal at all (DelegationRefusalOf).
type DelegationRefusal struct {
	Text  string `json:"text"`
	Round int    `json:"round,omitempty"`
}

func validDelegationRefusal(r DelegationRefusal) bool {
	return r.Text != "" && len(r.Text) <= ChildResultTextCap && validRound(r.Round)
}

const delegationRefusedKey = "delegationRefused"

// DelegationRefusalPart wraps r as the delegationRefused data part. A refusal
// with no text, or text over the cap, is refused: the parent's model reads it.
// So is a round out of bounds.
func DelegationRefusalPart(r DelegationRefusal) (Part, error) {
	if !validDelegationRefusal(r) {
		return Part{}, fmt.Errorf("delegation refusal: needs text of at most %d bytes and a round of at most %d", ChildResultTextCap, DelegateRoundsCeiling)
	}
	data, err := json.Marshal(map[string]DelegationRefusal{delegationRefusedKey: r})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// DelegationRefusalOf finds the delegationRefused part among parts, skipping
// one DelegationRefusalPart would refuse.
func DelegationRefusalOf(parts []Part) (DelegationRefusal, bool) {
	for _, p := range parts {
		raw, ok := wrappedData(p, delegationRefusedKey)
		if !ok {
			continue
		}
		var r DelegationRefusal
		if json.Unmarshal(raw, &r) != nil || !validDelegationRefusal(r) {
			continue
		}
		return r, true
	}
	return DelegationRefusal{}, false
}

// wrappedData is a data part's payload under key, when p is a data part
// wrapping one.
func wrappedData(p Part, key string) (json.RawMessage, bool) {
	if p.Kind != "data" || len(p.Data) == 0 {
		return nil, false
	}
	var wrap map[string]json.RawMessage
	if json.Unmarshal(p.Data, &wrap) != nil {
		return nil, false
	}
	raw, ok := wrap[key]
	return raw, ok
}

// SteerNotice is the data part an executor that queues follow-ups puts on
// the non-final status-update it publishes for each follow-up message it
// receives on a running task: one per follow-up envelope, either queued or
// refused with a reason. The gateway's relay reads it to word its posts
// and to count what it promised the room; a reader that does not know it
// sees the text part beside it.
type SteerNotice struct {
	Steer      string `json:"steer"`
	EnvelopeID string `json:"envelopeId"`
	Reason     string `json:"reason,omitempty"`
}

// SteerNotice.Steer values.
const (
	SteerQueued  = "queued"
	SteerRefused = "refused"
)

// SteerNotice.Reason tokens, for a refusal.
const (
	SteerReasonQueueFull  = "queue-full"  // the executor's queue is at its bound
	SteerReasonTaskEnding = "task-ending" // the deliverable was already chosen
	SteerReasonTaskEnded  = "task-ended"  // the task ended before the follow-up's turn
	SteerReasonNoText     = "no-text"     // nothing textual to ask
	SteerReasonCapability = "capability"  // the task's capability, carried on the follow-up, did not pass its check (refused, or the verifier unreachable)
	SteerReasonNoResume   = "no-resume"   // the executor cannot continue a session (the bridge's cli executor)
)

const steerNoticeKey = "steerNotice"

// SteerNoticePart is n as the data part a status message carries.
func SteerNoticePart(n SteerNotice) (Part, error) {
	data, err := json.Marshal(map[string]SteerNotice{steerNoticeKey: n})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// SteerNoticeOf finds the steer notice among parts. A data part that is not
// one, or one with an unknown steer value or no envelope id, is skipped.
func SteerNoticeOf(parts []Part) (SteerNotice, bool) {
	for _, p := range parts {
		if p.Kind != "data" || len(p.Data) == 0 {
			continue
		}
		var wrap map[string]json.RawMessage
		if json.Unmarshal(p.Data, &wrap) != nil {
			continue
		}
		raw, ok := wrap[steerNoticeKey]
		if !ok {
			continue
		}
		var n SteerNotice
		if json.Unmarshal(raw, &n) != nil || n.EnvelopeID == "" ||
			(n.Steer != SteerQueued && n.Steer != SteerRefused) {
			continue
		}
		return n, true
	}
	return SteerNotice{}, false
}

// CardStop is the cardStop data part: the gateway's request that the
// executor stop the kanban cards a conversation's turns filed (#3263), and
// the executor's answer. It rides a task of its own, so the request is
// answered on the same stream as any task: the terminal, and the result
// artifact before it.
//
// The request is a submission whose message carries this part and nothing
// else, no text (CardStopRequested):
//
//	{"cardStop":{"v":1}}
//
// The result is the same key on the completed task's result artifact,
// beside a text part that says the same for a reader that does not know it:
//
//	{"cardStop":{"v":1,"stopped":[{"id":"t_1","title":"…","priorStatus":"running"}],
//	  "settled":[{"id":"t_2","title":"…"}],"failed":0,"pending":0,"unresolved":0,
//	  "truncated":false,"deadline":false,"listsCut":false}}
//
// stopped is the cards the stop archived, with the status each had;
// settled the conversation's cards that had already finished. The counts
// own up to what it could not promise: failed, cards it could not archive;
// pending, archives still running when it ran out of time, so those cards
// may or may not end; unresolved, stopped cards that were claimed just
// before the stop with no worker seen yet, so a worker may still start for
// them (the executor's log names each card). truncated says the
// conversation had more open cards than one stop takes, so some may still
// be open; deadline that the stop ran out of time. listsCut says only that
// the executor cut stopped, settled or notStopped to fit the part, each at
// CardStopListMax, so the list that was cut is one at that length: the
// cards themselves are as the counts say. A stop that found nothing open answers empty lists.
// A stop that failed answers no part: its terminal is failed, and the
// reason says why.
type CardStop struct {
	V int `json:"v"`
	// The result's fields; nil on a request. Embedded so the wire form is
	// one flat object either way, and a request carries none of them.
	*CardStopResult
}

// CardStopListMax is the most cards a result's stopped or settled list
// carries; an executor cuts a longer one there and sets listsCut.
const CardStopListMax = 200

// CardStopResult is the result form's fields (CardStop).
type CardStopResult struct {
	Stopped    []CardStopCard `json:"stopped"`
	Settled    []CardStopCard `json:"settled"`
	Failed     int            `json:"failed"`
	Pending    int            `json:"pending"`
	Unresolved int            `json:"unresolved"`
	Truncated  bool           `json:"truncated"`
	Deadline   bool           `json:"deadline"`
	ListsCut   bool           `json:"listsCut"`
	// NotStopped names the cards Failed, Pending and Unresolved count, in that
	// order, cut at CardStopListMax (ListsCut covers it too). Absent from an
	// older executor's result, which reads as none named.
	NotStopped []CardStopCard `json:"notStopped,omitempty"`
}

// CardStopCard is one card a result names. PriorStatus is set on a stopped
// card only: the status it had before the stop archived it.
type CardStopCard struct {
	ID          string `json:"id"`
	Title       string `json:"title"`
	PriorStatus string `json:"priorStatus,omitempty"`
}

// CardStopVersion is the cardStop part's version, the only one there is. A
// part of any other version is not read as one.
const CardStopVersion = 1

const cardStopKey = "cardStop"

// CardStopPart is c as a data part, at CardStopVersion: the request when
// c.CardStopResult is nil, the result otherwise. A result's nil lists go
// out empty, so a result never reads as a request.
func CardStopPart(c CardStop) (Part, error) {
	c.V = CardStopVersion
	if r := c.CardStopResult; r != nil {
		norm := *r
		if norm.Stopped == nil {
			norm.Stopped = []CardStopCard{}
		}
		if norm.Settled == nil {
			norm.Settled = []CardStopCard{}
		}
		c.CardStopResult = &norm
	}
	data, err := json.Marshal(map[string]CardStop{cardStopKey: c})
	if err != nil {
		return Part{}, err
	}
	return Part{Kind: "data", Data: data}, nil
}

// CardStopOf finds the cardStop part among parts, request or result,
// whatever else they hold. A data part that is not one, or one of another
// version, is skipped.
func CardStopOf(parts []Part) (CardStop, bool) {
	for _, p := range parts {
		if p.Kind != "data" || len(p.Data) == 0 {
			continue
		}
		var wrap map[string]json.RawMessage
		if json.Unmarshal(p.Data, &wrap) != nil {
			continue
		}
		raw, ok := wrap[cardStopKey]
		if !ok {
			continue
		}
		var c CardStop
		if json.Unmarshal(raw, &c) != nil || c.V != CardStopVersion {
			continue
		}
		return c, true
	}
	return CardStop{}, false
}

// CardStopRequested reports whether parts are a card-stop request: data
// parts only, one of them a cardStop part in its request form. A message
// with a text or file part is a question, whatever data rides beside it, so
// a message that asks something is never taken for the control.
func CardStopRequested(parts []Part) bool {
	for _, p := range parts {
		if p.Kind != "data" {
			return false
		}
	}
	c, ok := CardStopOf(parts)
	return ok && c.CardStopResult == nil
}

// The A2A object shapes below carry only the fields the library consults.
// Payloads travel as raw bytes end to end (assertion 6); these views are for
// validation and folding, never re-serialized onto the wire.

// Part is one typed A2A message/artifact part.
type Part struct {
	Kind string          `json:"kind"`
	Text string          `json:"text,omitempty"`
	Data json.RawMessage `json:"data,omitempty"`
	File *FileRef        `json:"file,omitempty"`
}

// FileRef is the file half of a FilePart: inline bytes or a uri, never both
// required. Inline bytes over InlineFileThreshold are refused at emit.
type FileRef struct {
	Name     string `json:"name,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
	Bytes    string `json:"bytes,omitempty"`
	URI      string `json:"uri,omitempty"`
}

// Message is the A2A Message object.
type Message struct {
	Role      string `json:"role"`
	Parts     []Part `json:"parts"`
	MessageID string `json:"messageId"`
	TaskID    string `json:"taskId,omitempty"`
	ContextID string `json:"contextId,omitempty"`
}

// TaskStatus is the status half of a status-update.
type TaskStatus struct {
	State   TaskState `json:"state"`
	Message *Message  `json:"message,omitempty"`
	TS      string    `json:"timestamp,omitempty"`
}

// StatusUpdate is the A2A TaskStatusUpdateEvent.
type StatusUpdate struct {
	TaskID    string     `json:"taskId"`
	ContextID string     `json:"contextId"`
	Status    TaskStatus `json:"status"`
	Final     bool       `json:"final"`
}

// Artifact is the A2A Artifact object.
type Artifact struct {
	ArtifactID string `json:"artifactId,omitempty"`
	Name       string `json:"name,omitempty"`
	Parts      []Part `json:"parts"`
}

// ArtifactUpdate is the A2A TaskArtifactUpdateEvent.
type ArtifactUpdate struct {
	TaskID    string   `json:"taskId"`
	ContextID string   `json:"contextId"`
	Artifact  Artifact `json:"artifact"`
	Append    bool     `json:"append,omitempty"`
	LastChunk bool     `json:"lastChunk,omitempty"`
}

// AgentCard is the directory card on a2a.agents.<name>. Validation needs
// only Name; the rest is what the gateway routes a profile on (#3655), all
// optional so a card without them still parses.
type AgentCard struct {
	Name string `json:"name"`
	// Description is the routing blurb, a sentence a router can read.
	Description string `json:"description,omitempty"`
	// Tiers are the tiers the profile may run at (its allowedTiers).
	Tiers []string `json:"tiers,omitempty"`
	// ClusterRef pins a profile to one cluster; absent on a profile that
	// takes its target as task parameters.
	ClusterRef *ClusterRef `json:"clusterRef,omitempty"`
	// Parameters are the task parameters the profile's persona declares.
	Parameters []ProfileParameter `json:"parameters,omitempty"`
	// Delegates says the profile's tasks may delegate (AgentProfile
	// spec.delegates, #6789). The gateway refuses a delegate request from a
	// task of a profile whose card doesn't say so; a card from an operator
	// that predates the field doesn't, so it fails closed.
	Delegates bool `json:"delegates,omitempty"`
	// YieldToPinned hands a task addressed to this profile to the profile
	// pinned to its target cluster, when there is one: the generic cluster
	// agent sets it, and a specialist addressed by name keeps the task.
	YieldToPinned bool `json:"yieldToPinned,omitempty"`
}

// ClusterRef names one GKE cluster.
type ClusterRef struct {
	ProjectID string `json:"projectId"`
	Location  string `json:"location"`
	Cluster   string `json:"cluster"`
}

// ProfileParameter is one task parameter a profile declares: its name, as
// TaskParameterNameRE, and whether a submission must carry it.
type ProfileParameter struct {
	Name     string `json:"name"`
	Required bool   `json:"required,omitempty"`
}

func protocolErrf(format string, args ...any) error {
	return &ProtocolError{Msg: fmt.Sprintf(format, args...)}
}

// validatePayload enforces kind/payload agreement (assertion 7), including
// the payload's taskId agreeing with the envelope's: an event whose payload
// names another task is poison to the fold, so it is refused at emit and at
// parse rather than surviving to break tasks/get. A mismatch is a protocol
// error, never passed through.
func validatePayload(kind Kind, taskID string, payload json.RawMessage) error {
	if isJSONNull(payload) && kind != KindCancel && kind != KindAgentClosed {
		return protocolErrf("kind %q requires a payload", kind)
	}
	switch kind {
	case KindMessage:
		var m Message
		if err := json.Unmarshal(payload, &m); err != nil {
			return protocolErrf("kind message: malformed payload: %v", err)
		}
		if m.Role != "user" && m.Role != "agent" {
			return protocolErrf("kind message: payload is not an A2A Message (role %q)", m.Role)
		}
		if m.MessageID == "" {
			return protocolErrf("kind message: payload missing messageId")
		}
		if len(m.Parts) == 0 {
			return protocolErrf("kind message: payload has no parts")
		}
		if m.TaskID != "" && m.TaskID != taskID {
			return protocolErrf("kind message: payload names task %q but the envelope carries taskId %q", m.TaskID, taskID)
		}
		return validateParts(m.Parts, "message")
	case KindStatusUpdate:
		var s StatusUpdate
		if err := json.Unmarshal(payload, &s); err != nil {
			return protocolErrf("kind status-update: malformed payload: %v", err)
		}
		if s.Status.State == "" {
			return protocolErrf("kind status-update: payload is not a TaskStatusUpdateEvent (no status.state)")
		}
		if !validTaskState(s.Status.State) {
			return protocolErrf("kind status-update: unknown task state %q", s.Status.State)
		}
		if s.Final && !s.Status.State.Terminal() {
			return protocolErrf("kind status-update: final=true with non-terminal state %q", s.Status.State)
		}
		if s.TaskID != "" && s.TaskID != taskID {
			return protocolErrf("kind status-update: payload names task %q but the envelope carries taskId %q", s.TaskID, taskID)
		}
		if s.Status.Message != nil {
			if err := validateParts(s.Status.Message.Parts, "status-update message"); err != nil {
				return err
			}
		}
	case KindArtifactUpdate:
		var a ArtifactUpdate
		if err := json.Unmarshal(payload, &a); err != nil {
			return protocolErrf("kind artifact-update: malformed payload: %v", err)
		}
		if len(a.Artifact.Parts) == 0 {
			return protocolErrf("kind artifact-update: payload is not a TaskArtifactUpdateEvent (artifact has no parts)")
		}
		if a.TaskID != "" && a.TaskID != taskID {
			return protocolErrf("kind artifact-update: payload names task %q but the envelope carries taskId %q", a.TaskID, taskID)
		}
		return validateParts(a.Artifact.Parts, "artifact-update")
	case KindCancel, KindAgentClosed:
		if !isEmptyObject(payload) {
			return protocolErrf("kind %q carries an empty payload", kind)
		}
	case KindAgentCard:
		var c AgentCard
		if err := json.Unmarshal(payload, &c); err != nil {
			return protocolErrf("kind agent-card: malformed payload: %v", err)
		}
		if c.Name == "" {
			return protocolErrf("kind agent-card: payload is not an AgentCard (no name)")
		}
	case KindTopicUpdate:
		var a Artifact
		if err := json.Unmarshal(payload, &a); err != nil {
			return protocolErrf("kind topic-update: malformed payload: %v", err)
		}
		if a.Name == "" {
			return protocolErrf("kind topic-update: payload is not an Artifact with a topic name")
		}
		if strings.Contains(a.Name, ".") {
			return protocolErrf("kind topic-update: topic token %q contains a dot", a.Name)
		}
		if len(a.Parts) == 0 {
			return protocolErrf("kind topic-update: artifact has no parts")
		}
		return validateParts(a.Parts, "topic-update")
	default:
		return protocolErrf("unknown kind %q", kind)
	}
	return nil
}

func isEmptyObject(payload json.RawMessage) bool {
	if isJSONNull(payload) {
		return true
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(payload, &m); err != nil {
		return false
	}
	return len(m) == 0
}

func validateParts(parts []Part, where string) error {
	for i, p := range parts {
		switch p.Kind {
		case "text", "data":
		case "file":
			if p.File == nil {
				return protocolErrf("%s part %d: file part without file", where, i)
			}
			if p.File.Bytes == "" && p.File.URI == "" {
				return protocolErrf("%s part %d: file part with neither bytes nor uri", where, i)
			}
		default:
			return protocolErrf("%s part %d: unknown part kind %q", where, i, p.Kind)
		}
	}
	return nil
}

// checkInlineFileParts refuses inline FileParts whose decoded size exceeds
// InlineFileThreshold; those MUST use uri backed by the object store.
func checkInlineFileParts(kind Kind, payload json.RawMessage) error {
	var parts []Part
	switch kind {
	case KindMessage:
		var m Message
		if err := json.Unmarshal(payload, &m); err != nil {
			return nil // shape already validated; nothing to scan
		}
		parts = m.Parts
	case KindStatusUpdate:
		var s StatusUpdate
		if err := json.Unmarshal(payload, &s); err != nil {
			return nil
		}
		if s.Status.Message == nil {
			return nil
		}
		parts = s.Status.Message.Parts
	case KindArtifactUpdate:
		var a ArtifactUpdate
		if err := json.Unmarshal(payload, &a); err != nil {
			return nil
		}
		parts = a.Artifact.Parts
	case KindTopicUpdate:
		var a Artifact
		if err := json.Unmarshal(payload, &a); err != nil {
			return nil
		}
		parts = a.Parts
	default:
		return nil
	}
	for i, p := range parts {
		if p.Kind == "file" && p.File != nil && p.File.Bytes != "" {
			if base64DecodedSize(p.File.Bytes) > InlineFileThreshold {
				return &A2AError{
					Code: CodeInvalidParams,
					Message: fmt.Sprintf("part %d: inline FilePart exceeds %d-byte threshold; use uri via the object store",
						i, InlineFileThreshold),
				}
			}
		}
	}
	return nil
}

// base64DecodedSize is the exact decoded size of a padded std-base64 string —
// DecodedLen overestimates by up to two bytes, which would refuse a file of
// exactly the threshold (the spec bans only files above it).
func base64DecodedSize(s string) int {
	n := len(s) / 4 * 3
	if strings.HasSuffix(s, "==") {
		n -= 2
	} else if strings.HasSuffix(s, "=") {
		n--
	}
	return n
}
