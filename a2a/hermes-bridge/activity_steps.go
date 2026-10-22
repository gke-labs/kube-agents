package hermesbridge

// Plan steps: what a turn planned, published as kube-agents.step/v1 records
// on the task's `steps` artifact (a2a/lib/stepmsg), so a renderer lists a
// turn's steps without parsing them out of `thinking`.
//
// The source is hermes's post_api_request hook, one delivery per model
// request: the calls the model asked for become plan steps, one per tool in
// plan order, each carrying the ids of its calls so a renderer places every
// activity entry under the step that asked for it; a request that stops
// after work becomes the synthesis step.
//
// What a step carries is structure only, the same posture as the activity
// trace beside it: a tool's name, call ids, a kind, and a fixed title. Not
// the model's text, not a call's arguments, not a tool's result or error
// text. A model request's response can quote whatever a tool returned, so
// the door reads the planned calls' names and ids out of it and nothing
// else, and a step's title is the tool's name, which the trace already
// publishes. Nothing here knows a particular tool: a title is never built
// from what a call is about.

import (
	"encoding/json"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

const (
	// finishReasonStop is a request that ended with the model's answer
	// rather than with calls.
	finishReasonStop = "stop"
	// synthesisStepTitle is the step a request that stops after work stands
	// for: the model writing its answer.
	synthesisStepTitle = "Writing the answer"
)

// activityStepBudget bounds the step records one task publishes. They ride
// the task's events subject with the trace and the heartbeat, and count
// against the trace's share too (stepUnderBudget), so the subject stays
// under the stream's cap whatever a looping persona plans. A variable so a
// test can lower it.
var activityStepBudget = 256

// apiResponse is the part of a post_api_request's sanitized response the
// door reads: how the request ended, and each planned call's id and name.
// The arguments and the message's content are left undecoded.
type apiResponse struct {
	FinishReason     string `json:"finish_reason"`
	AssistantMessage struct {
		ToolCalls []struct {
			ID       string `json:"id"`
			Name     string `json:"name"`
			Function struct {
				Name string `json:"name"`
			} `json:"function"`
		} `json:"tool_calls"`
	} `json:"assistant_message"`
}

// plannedSteps is the step records one post_api_request stands for: a plan
// step per tool the request asked for, in plan order, carrying its calls'
// ids; else, when the request stopped after work (a later request in the
// turn, or any call already completed), the synthesis step; else none. A
// first request that answers outright plans nothing worth a row.
func plannedSteps(d hookDelivery, completedCalls int) []stepmsg.Step {
	var resp apiResponse
	if len(d.Extra.Response) > 0 {
		// A response of another shape plans nothing; the finish reason
		// may still be read off the delivery itself.
		_ = json.Unmarshal(d.Extra.Response, &resp)
	}
	finish := d.Extra.FinishReason
	if finish == "" {
		finish = resp.FinishReason
	}
	var steps []stepmsg.Step
	for _, tc := range resp.AssistantMessage.ToolCalls {
		name := strings.TrimSpace(tc.Function.Name)
		if name == "" {
			name = strings.TrimSpace(tc.Name)
		}
		if name == "" {
			continue
		}
		title := capRunes(name, activityToolNameCap)
		i := slices.IndexFunc(steps, func(s stepmsg.Step) bool { return s.Title == title })
		if i < 0 {
			steps = append(steps, stepmsg.Step{Kind: stepmsg.KindPlan, Title: title})
			i = len(steps) - 1
		}
		if id := capRunes(strings.TrimSpace(tc.ID), activityCallIDCap); id != "" && !slices.Contains(steps[i].CallIDs, id) {
			steps[i].CallIDs = append(steps[i].CallIDs, id)
		}
	}
	if len(steps) > 0 {
		return steps
	}
	if finish == finishReasonStop && (durationMillis(d.Extra.APICallCount) > 1 || completedCalls > 0) {
		return []stepmsg.Step{{Kind: stepmsg.KindSynthesis, Title: synthesisStepTitle}}
	}
	return nil
}

// takeSteps returns the steps the last delivery planned and forgets them.
func (a *activityState) takeSteps() []stepmsg.Step {
	a.mu.Lock()
	defer a.mu.Unlock()
	steps := a.pendingSteps
	a.pendingSteps = nil
	return steps
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

// publishPendingSteps publishes the steps the last delivery planned while
// the task runs; after its terminal they are dropped. Caller holds run.mu,
// which orders the publish against finalize.
func (b *Bridge) publishPendingSteps(run *taskRun, act *activityState) {
	steps := act.takeSteps()
	if len(steps) == 0 || run.state != stateRunning {
		return
	}
	for _, st := range steps {
		if !act.stepUnderBudget() {
			return
		}
		b.publishArtifactPart(run, stepmsg.ArtifactName, st.Part())
	}
}
