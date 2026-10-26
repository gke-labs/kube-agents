package hermesbridge

// Plan steps: what a turn did, published as kube-agents.step/v1 records on
// the task's `steps` artifact (a2a/lib/stepmsg), so a renderer lists a
// turn's steps without parsing them out of `thinking`.
//
// A plan step is made from the trace itself: each pre_tool_call opens or
// extends one, titled with the call's tool name and carrying its call id,
// the same name and id its activity entry carries. Consecutive calls of one
// tool within one model request share a step; a call of another tool, the
// next model request, or the task's drain closes it and publishes it. The
// model request itself (hermes's post_api_request) is read only for where
// one request ends and the next begins, and for the synthesis step: a
// request of the turn's own session that stops after work. It is never the
// source of a title or an id. It fires before hermes validates and repairs
// the calls' names and de-duplicates their ids, so a name read off it could
// be one the model invented (or one injected into its context), and an id
// one the trace never carries.
//
// What a step carries is structure only, the same posture as the trace
// beside it: a tool's name, call ids, a kind and a fixed title. Not the
// model's text, not a call's arguments, not a tool's result or error text.
// Nothing here knows a particular tool: a title is never built from what a
// call is about.

import (
	"encoding/json"
	"slices"

	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

const (
	// finishReasonStop is a request that ended with the model's answer
	// rather than with calls.
	finishReasonStop = "stop"
	// synthesisStepTitle is the step a request that stops after work stands
	// for: the model writing its answer.
	synthesisStepTitle = "Writing the answer"
	// stepCallIDsCap is how many call ids one step record may carry
	// (stepmsg's limit); a longer run of one tool's calls continues in a
	// new step of the same title. A title is the trace's tool name, capped
	// at activityToolNameCap bytes, and an id at activityCallIDCap bytes,
	// both inside the record's own bounds (TestStepCapsFitTheRecord).
	stepCallIDsCap = 64
)

// activityStepBudget bounds the step records one task publishes. They ride
// the task's events subject with the trace and the heartbeat, and count
// against the trace's share too (stepUnderBudget), so the subject stays
// under the stream's cap whatever a looping persona does. A variable so a
// test can lower it.
var activityStepBudget = 256

// apiResponse is the part of a post_api_request's sanitized response the
// door reads: how the request ended, and whether it planned any call. The
// calls' names, ids and arguments and the message's content are left
// undecoded.
type apiResponse struct {
	FinishReason     string `json:"finish_reason"`
	AssistantMessage struct {
		ToolCalls []json.RawMessage `json:"tool_calls"`
	} `json:"assistant_message"`
}

// observeStepsLocked is observe's step half, under a.mu, for a delivery
// already attributed to this task and not a retry: the steps it closes,
// in order. A pre_tool_call opens or extends the open step; a
// post_api_request closes it, and may itself be the synthesis step.
func (a *activityState) observeStepsLocked(d hookDelivery) []stepmsg.Step {
	if a.turnSession == "" {
		// The CLI route learns the turn's own session from its first
		// delivery: the turn's first model request comes before any call,
		// a delegated child's included, so nothing of a child's can be
		// first. The API route knows it from the start.
		a.turnSession = d.SessionID
	}
	switch d.Event {
	case hookPreToolCall:
		title := capRunes(d.ToolName, activityToolNameCap)
		if title == "" {
			return nil
		}
		id := capRunes(d.Extra.ToolCallID, activityCallIDCap)
		if a.openStep != nil && a.openStep.Title == title && len(a.openStep.CallIDs) < stepCallIDsCap {
			if id != "" && !slices.Contains(a.openStep.CallIDs, id) {
				a.openStep.CallIDs = append(a.openStep.CallIDs, id)
			}
			return nil
		}
		closed := a.closeStepLocked()
		a.openStep = &stepmsg.Step{Kind: stepmsg.KindPlan, Title: title}
		if id != "" {
			a.openStep.CallIDs = []string{id}
		}
		return closed
	case hookPostAPIRequest:
		closed := a.closeStepLocked()
		if a.synthesisLocked(d) {
			closed = append(closed, stepmsg.Step{Kind: stepmsg.KindSynthesis, Title: synthesisStepTitle})
		}
		return closed
	}
	return nil
}

// synthesisLocked reports whether a post_api_request is the turn writing
// its answer: a request of the turn's own session that stopped, planned no
// call, and came after work (a later request in the turn, or one after a
// call this turn completed). A delegated child runs in the same process
// and posts its own requests, under its own session id; its final request
// is not the turn's answer. A delivery with no session id is not known to be the
// turn's, so it makes no synthesis step.
func (a *activityState) synthesisLocked(d hookDelivery) bool {
	if d.SessionID == "" || d.SessionID != a.turnSession {
		return false
	}
	var resp apiResponse
	if len(d.Extra.Response) > 0 {
		// A response hermes truncated, or of another shape, decodes to
		// nothing; the delivery's own finish reason still says how the
		// request ended.
		_ = json.Unmarshal(d.Extra.Response, &resp)
	}
	finish := d.Extra.FinishReason
	if finish == "" {
		finish = resp.FinishReason
	}
	return finish == finishReasonStop && len(resp.AssistantMessage.ToolCalls) == 0 &&
		(durationMillis(d.Extra.APICallCount) > 1 || a.turnCalls > 0)
}

// beginTurn marks the start of a turn for the synthesis rule. The CLI route
// runs one turn per process and state, so only the API route's turn loop
// calls it.
func (a *activityState) beginTurn() {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.turnCalls = 0
}

// closeStepLocked returns the open step, if any, and forgets it.
func (a *activityState) closeStepLocked() []stepmsg.Step {
	if a.openStep == nil {
		return nil
	}
	st := *a.openStep
	a.openStep = nil
	return []stepmsg.Step{st}
}

// closeStep is closeStepLocked for the drain.
func (a *activityState) closeStep() []stepmsg.Step {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.closeStepLocked()
}

// stepUnderBudget says whether the next step record may go out, counting
// it against both the steps' own budget and the trace's share of the
// subject. Past either the step is dropped, uncounted: the truncation
// marker counts calls, and a missing row costs a renderer a heading, not a
// call.
func (a *activityState) stepUnderBudget() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.steps < activityStepBudget && a.published < activityEntryBudget-activityHeartbeatReserve {
		a.steps++
		a.published++
		return true
	}
	return false
}

// publishSteps publishes steps while the task runs; after its terminal they
// are dropped. A step the record refuses to encode is skipped, said once per
// task in the log: a step never fails the task. Caller holds run.mu, which
// orders the publish against finalize.
func (b *Bridge) publishSteps(run *taskRun, act *activityState, steps []stepmsg.Step) {
	if len(steps) == 0 || run.state != stateRunning {
		return
	}
	for _, st := range steps {
		part, err := st.Part()
		if err != nil {
			if act.stepRefused.CompareAndSwap(false, true) {
				b.cfg.Logger.Warn("step record refused; skipped", "task", run.origin.TaskID, "kind", st.Kind, "err", err)
			}
			continue
		}
		if !act.stepUnderBudget() {
			return
		}
		b.publishArtifactPart(run, stepmsg.ArtifactName, part)
	}
}
