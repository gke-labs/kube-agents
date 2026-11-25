package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// coreAgentDriver drives core-agent-harness (a2a/cmd/core-agent-harness),
// the worker's core-agent runner, one process per turn. The turn's input is
// one coreagentwire.Envelope on stdin, which carries the tool policy and MCP
// servers with the user message: nothing that decides what a turn may do is
// a file the harness's own commands could rewrite. Its output is one
// coreagentwire.Event per line.
//
// The driver holds each conversation's history, in memory: the harness
// keeps no session file, so nothing a turn's commands write can change what
// the next turn resumes. The decoder keeps the event lines a turn emits and
// the next turn's envelope carries them back.
type coreAgentDriver struct {
	// Policy and MCP are the turn's tool policy and MCP servers, set from
	// the persona grant (SEAM: tigerteam-dev-1, tigerteam-dev-3).
	Policy coreagentwire.Policy
	MCP    []coreagentwire.MCPServer

	conv *coreAgentConversations
}

// coreAgentConversations is each conversation's history and the id the
// next turn resumes.
type coreAgentConversations struct {
	mu      sync.Mutex
	history map[string][]json.RawMessage
	resume  string
}

func newCoreAgentDriver() coreAgentDriver {
	return coreAgentDriver{conv: &coreAgentConversations{history: map[string][]json.RawMessage{}}}
}

// coreAgentErrorToken is a failed turn's reason token.
const coreAgentErrorToken = "error_during_execution" // #nosec G101 -- a failure reason token, not a credential

func (coreAgentDriver) Harness() persona.Harness { return persona.CoreAgent }

// EncodeUser is the turn's envelope: the message, the policy and MCP
// servers, and the history of the conversation WithResume named.
func (d coreAgentDriver) EncodeUser(text string) ([]byte, error) {
	env := coreagentwire.Envelope{Message: text, Policy: d.Policy, MCP: d.MCP}
	if d.conv != nil {
		d.conv.mu.Lock()
		env.Session = d.conv.resume
		env.History = append([]json.RawMessage(nil), d.conv.history[d.conv.resume]...)
		d.conv.mu.Unlock()
	}
	b, err := json.Marshal(env)
	if err != nil {
		return nil, err
	}
	if len(b) > coreagentwire.MaxEnvelopeBytes {
		return nil, fmt.Errorf("the conversation's history is over %d bytes", coreagentwire.MaxEnvelopeBytes)
	}
	return b, nil
}

func (d coreAgentDriver) NewDecoder() Decoder { return &coreAgentDecoder{conv: d.conv} }

// WithResume names the conversation the next turn resumes; its history goes
// in the envelope, not the argv.
func (d coreAgentDriver) WithResume(argv []string, id string) []string {
	if d.conv != nil {
		d.conv.mu.Lock()
		d.conv.resume = id
		d.conv.mu.Unlock()
	}
	return argv
}

// WithDelegateServerEnv reports the delegate server unconfigured: MCP
// servers reach the harness in the Envelope.
func (coreAgentDriver) WithDelegateServerEnv(argv []string, _, _ string) ([]string, bool) {
	return argv, false
}

func (coreAgentDriver) ConfiguresMCP([]string) bool { return false }

// ConfigDirEnv is empty: the harness reads no config directory.
func (coreAgentDriver) ConfigDirEnv(string) string { return "" }

// PrepareTurn checks nothing yet (SEAM: tigerteam-dev-3 and dev-2: the
// persona directory check before each spawn).
func (coreAgentDriver) PrepareTurn(context.Context, TurnDirs) error { return nil }

// HoldsSteers: a steer waits for the turn to end; core-agent has no way in
// mid-turn short of interrupting it.
func (coreAgentDriver) HoldsSteers() bool { return true }

// coreAgentDecoder maps the harness's event lines onto Events: one
// EventToolResult per tool call, an erroring step included.
type coreAgentDecoder struct {
	conv    *coreAgentConversations
	model   string
	session string
	// inited and ended bound the turn: one init, then events, then one
	// result. A line outside that order (a second init, an event before
	// init or after the result) is ignored, so nothing written into the
	// stream out of order reaches the conversation's history.
	inited, ended bool
}

func (d *coreAgentDecoder) Decode(line []byte) ([]Event, error) {
	var ln coreagentwire.Event
	if err := json.Unmarshal(line, &ln); err != nil {
		return nil, fmt.Errorf("core-agent-harness line: %w", err)
	}
	switch ln.Type {
	case coreagentwire.TypeInit:
		if d.inited {
			return nil, nil
		}
		d.inited = true
		d.model, d.session = ln.Model, ln.Session
		return []Event{{Kind: EventInit, Session: ln.Session, Model: ln.Model}}, nil
	case coreagentwire.TypeEvent:
		if d.inited && !d.ended && d.conv != nil && d.session != "" && len(ln.Event) > 0 {
			d.conv.mu.Lock()
			if coreagentwire.IsHistoryBoundary(ln.Event) {
				// A compaction summary: core-agent starts its next request
				// from it, so nothing before it is sent again.
				d.conv.history[d.session] = nil
			}
			d.conv.history[d.session] = append(d.conv.history[d.session], ln.Event)
			d.conv.mu.Unlock()
		}
		return nil, nil
	case coreagentwire.TypeThinking:
		return []Event{{Kind: EventThinking, Text: ln.Text}}, nil
	case coreagentwire.TypeText:
		return []Event{{Kind: EventProgress, Text: ln.Text}}, nil
	case coreagentwire.TypeToolCall:
		return []Event{{Kind: EventActivity, Tool: ln.Tool, Input: ln.Input}}, nil
	case coreagentwire.TypeToolResult:
		return []Event{{Kind: EventToolResult, Tool: ln.Tool, Text: ln.Text, Failed: ln.Failed}}, nil
	case coreagentwire.TypeResult:
		if d.ended {
			return nil, nil
		}
		d.ended = true
		ev := Event{Kind: EventResult, Text: ln.Text, Telemetry: resultTelemetry{usage: lib.Usage{Model: d.model}}}
		if ln.Usage != nil {
			ev.Telemetry.usage.InputTokens = int64(ln.Usage.Input)
			ev.Telemetry.usage.OutputTokens = int64(ln.Usage.Output)
		}
		if ln.Failed {
			ev.Failed, ev.Token = true, withDetail(coreAgentErrorToken, ln.Error)
		}
		return []Event{ev}, nil
	}
	// An event type this decoder doesn't know is skipped, so a newer
	// harness can add one.
	return nil, nil
}
