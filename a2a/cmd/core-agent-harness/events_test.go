package main

import (
	"errors"
	"testing"

	"google.golang.org/adk/v2/model"
	"google.golang.org/adk/v2/session"
	"google.golang.org/genai"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

func ev(partial bool, parts ...*genai.Part) *session.Event {
	return &session.Event{LLMResponse: model.LLMResponse{Partial: partial, Content: &genai.Content{Role: genai.RoleModel, Parts: parts}}}
}

func types(evs []coreagentwire.Event) []string {
	var out []string
	for _, e := range evs {
		out = append(out, e.Type)
	}
	return out
}

// A tool turn as core-agent streams it (partial then whole per model
// response) maps to one call, one result and the streamed text once.
func TestTheMapperEmitsEachCallResultAndDeltaOnce(t *testing.T) {
	var got []coreagentwire.Event
	m := newMapper(func(e coreagentwire.Event) { got = append(got, e) })
	call := &genai.Part{FunctionCall: &genai.FunctionCall{ID: "c1", Name: "bash", Args: map[string]any{"command": "echo hi"}}}
	m.observe(ev(true, call))
	m.observe(ev(false, call))
	m.observe(&session.Event{LLMResponse: model.LLMResponse{Content: &genai.Content{Role: genai.RoleUser, Parts: []*genai.Part{
		{FunctionResponse: &genai.FunctionResponse{ID: "c1", Name: "bash", Response: map[string]any{"stdout": "hi"}}}}}}})
	m.observe(ev(true, &genai.Part{Text: "all "}))
	m.observe(ev(true, &genai.Part{Text: "done"}))
	m.observe(ev(false, &genai.Part{Text: "all done"}))
	m.finish(nil)
	want := []string{"tool_call", "tool_result", "text", "text", "result"}
	if g := types(got); len(g) != len(want) {
		t.Fatalf("events %v, want %v", g, want)
	} else {
		for i := range want {
			if g[i] != want[i] {
				t.Fatalf("events %v, want %v", g, want)
			}
		}
	}
	if r := got[len(got)-1]; r.Failed || r.Text != "all done" {
		t.Errorf("result %+v", r)
	}
}

// A tool call the turn never answered still gets its one tool result,
// failed, before a failed result.
func TestAnUnansweredCallGetsAFailedResult(t *testing.T) {
	var got []coreagentwire.Event
	m := newMapper(func(e coreagentwire.Event) { got = append(got, e) })
	m.observe(ev(false, &genai.Part{FunctionCall: &genai.FunctionCall{ID: "c1", Name: "bash"}}))
	m.finish(errors.New("context deadline exceeded"))
	if g := types(got); len(g) != 3 || g[1] != "tool_result" || !got[1].Failed || g[2] != "result" || !got[2].Failed {
		t.Fatalf("events %+v", got)
	}
}

// An erroring step with no output still yields its one tool result, failed.
func TestAnErroringStepYieldsOneFailedResult(t *testing.T) {
	var got []coreagentwire.Event
	m := newMapper(func(e coreagentwire.Event) { got = append(got, e) })
	m.observe(ev(false, &genai.Part{FunctionCall: &genai.FunctionCall{ID: "c1", Name: "bash"}}))
	m.observe(&session.Event{LLMResponse: model.LLMResponse{Content: &genai.Content{Parts: []*genai.Part{
		{FunctionResponse: &genai.FunctionResponse{ID: "c1", Name: "bash", Response: map[string]any{"error": "refused"}}}}}}})
	m.finish(nil)
	n := 0
	for _, e := range got {
		if e.Type == coreagentwire.TypeToolResult {
			n++
			if !e.Failed {
				t.Errorf("tool result not failed: %+v", e)
			}
		}
	}
	if n != 1 {
		t.Errorf("%d tool results, want 1: %+v", n, got)
	}
}

// A final message in several text parts is the result's whole text.
func TestTheResultJoinsTheFinalTextParts(t *testing.T) {
	var got []coreagentwire.Event
	m := newMapper(func(e coreagentwire.Event) { got = append(got, e) })
	m.observe(ev(false, &genai.Part{Text: "one, "}, &genai.Part{Text: "two"}))
	m.finish(nil)
	if r := got[len(got)-1]; r.Text != "one, two" {
		t.Errorf("result text %q", r.Text)
	}
}
