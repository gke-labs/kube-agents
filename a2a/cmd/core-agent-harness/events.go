package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"

	"google.golang.org/adk/v2/session"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// mapper turns core-agent's events into coreagentwire events.
//
// A model response arrives as streamed partial events, then once more
// whole: text is emitted from the partials, and from a whole event only
// when no partial carried it. A tool call is emitted the first time its id
// is seen. Every tool call gets exactly one tool result: its function
// response, or, when the turn ends without one, a failed result.
type mapper struct {
	emit     func(coreagentwire.Event)
	streamed bool // a partial text delta since the last whole event
	calls    []string
	tools    map[string]string // call id -> tool name, for unanswered calls
	answered map[string]bool
	usage    coreagentwire.Usage
	final    string
}

func newMapper(emit func(coreagentwire.Event)) *mapper {
	return &mapper{emit: emit, tools: map[string]string{}, answered: map[string]bool{}}
}

func (m *mapper) observe(ev *session.Event) {
	if ev == nil {
		return
	}
	if u := ev.UsageMetadata; u != nil && !ev.Partial {
		m.usage.Input += int64(u.PromptTokenCount)
		m.usage.Output += int64(u.CandidatesTokenCount)
		m.usage.Cached += int64(u.CachedContentTokenCount)
		m.usage.Thoughts += int64(u.ThoughtsTokenCount)
	}
	if ev.Content == nil {
		return
	}
	var whole []string
	for _, p := range ev.Content.Parts {
		switch {
		case p.FunctionCall != nil:
			id := p.FunctionCall.ID
			if _, seen := m.tools[id]; seen {
				continue
			}
			m.tools[id] = p.FunctionCall.Name
			m.calls = append(m.calls, id)
			input, _ := json.Marshal(p.FunctionCall.Args)
			m.emit(coreagentwire.Event{Type: coreagentwire.TypeToolCall, ID: id, Tool: p.FunctionCall.Name, Input: input})
		case p.FunctionResponse != nil:
			id := p.FunctionResponse.ID
			if m.answered[id] {
				continue
			}
			m.answered[id] = true
			out, _ := json.Marshal(p.FunctionResponse.Response)
			_, failed := p.FunctionResponse.Response["error"]
			m.emit(coreagentwire.Event{Type: coreagentwire.TypeToolResult, ID: id, Tool: p.FunctionResponse.Name, Text: string(out), Failed: failed})
		case p.Text != "":
			kind := coreagentwire.TypeText
			if p.Thought {
				kind = coreagentwire.TypeThinking
			}
			if ev.Partial {
				m.streamed = true
				m.emit(coreagentwire.Event{Type: kind, Text: p.Text})
			} else if !m.streamed {
				m.emit(coreagentwire.Event{Type: kind, Text: p.Text})
			}
			if kind == coreagentwire.TypeText && !ev.Partial {
				whole = append(whole, p.Text)
			}
		}
	}
	if len(whole) > 0 {
		m.final = strings.Join(whole, "")
	}
	if !ev.Partial {
		m.streamed = false
	}
}

// finish closes the turn: a failed result for every unanswered tool call,
// then the result itself, failed when err is set.
func (m *mapper) finish(err error) {
	reason := "the turn ended before the tool ran"
	if err != nil {
		reason = err.Error()
	}
	for _, id := range m.calls {
		if !m.answered[id] {
			m.emit(coreagentwire.Event{Type: coreagentwire.TypeToolResult, ID: id, Tool: m.tools[id], Failed: true, Error: reason})
		}
	}
	res := coreagentwire.Event{Type: coreagentwire.TypeResult, Text: m.final, Usage: &m.usage}
	if err != nil {
		res.Failed, res.Error = true, err.Error()
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			res.Error = "interrupted: " + err.Error()
		}
	}
	m.emit(res)
}
