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
// the next turn resumes. The decoder keeps the event lines a turn emits,
// starting over at each compaction summary, and the next turn's envelope
// carries them back.
type coreAgentDriver struct {
	// Policy and MCP are the turn's tool policy and MCP servers, set from
	// the persona grant.
	Policy coreagentwire.Policy
	MCP    []coreagentwire.MCPServer
	// Instructions are the session pod's system-prompt lines, sent in each
	// turn's envelope (WithInstructions). A profile worker has none: its
	// persona is its instruction layer.
	Instructions []string

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

// WithInstructions is d with the worker's system-prompt lines for core-agent,
// which reads them from each turn's envelope; any other driver is returned as
// it is (Claude Code gets the same lines on its argv).
func WithInstructions(d Driver, lines []string) Driver {
	if c, ok := d.(coreAgentDriver); ok {
		c.Instructions = append([]string(nil), lines...)
		return c
	}
	return d
}

// EncodeUser is the turn's envelope: the message, the policy and MCP
// servers, and the history of the conversation WithResume named.
func (d coreAgentDriver) EncodeUser(text string) ([]byte, error) {
	env := coreagentwire.Envelope{Message: text, Policy: d.Policy, MCP: d.MCP, Instructions: d.Instructions}
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
		return nil, fmt.Errorf("the turn's envelope (its message, policy, MCP servers, instructions and the conversation's history) is over %d bytes", coreagentwire.MaxEnvelopeBytes)
	}
	return b, nil
}

func (d coreAgentDriver) NewDecoder() Decoder { return &coreAgentDecoder{conv: d.conv} }

// WithResume names the conversation the next turn resumes; its history goes
// in the envelope, not the argv. It is the only history kept: every other
// conversation's is dropped, since a turn that resumes none (a profile
// worker's every task) or another one will never send it, and a long-lived
// worker would otherwise hold every task's history it ever ran.
func (d coreAgentDriver) WithResume(argv []string, id string) []string {
	if d.conv != nil {
		d.conv.mu.Lock()
		d.conv.resume = id
		for k := range d.conv.history {
			if k != id {
				delete(d.conv.history, k)
			}
		}
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

// PrepareTurn checks nothing yet; the persona directory check before each
// spawn comes with the persona render (#6713).
func (coreAgentDriver) PrepareTurn(context.Context, TurnDirs) error { return nil }

// HoldsSteers: a steer waits for the turn to end; core-agent has no way in
// mid-turn short of interrupting it.
func (coreAgentDriver) HoldsSteers() bool { return true }

// OneTurnPerProcess: the harness reads one envelope and ends with its turn,
// so a held steer written at the turn boundary is refused rather than left
// unread in the pipe (processEncoder). The per-turn restart work runs held
// steers in a fresh process.
func (coreAgentDriver) OneTurnPerProcess() {}

// KeepsOnlyCompletedTurns: a turn's events join the conversation's history
// only at a completed result (coreAgentDecoder), so the session loop keeps
// the primer until a turn completes (runSessionLoop).
func (coreAgentDriver) KeepsOnlyCompletedTurns() {}

// coreAgentDecoder maps the harness's event lines onto Events: one
// EventToolResult per tool call, an erroring step included.
type coreAgentDecoder struct {
	conv    *coreAgentConversations
	model   string
	session string
	// inited and ended bound the turn: one init, then events, then one
	// result. Every line outside that order (a second init, an init after
	// the result, any other line before init or after the result, except a
	// failed result that ends a turn that never started) is ignored, so
	// nothing written into the stream out of order reaches the adapter or
	// the conversation's history.
	inited, ended bool
	// pending is the turn's event lines, kept until its result: a turn's
	// events join the conversation's history only when it completes, so a
	// turn that was stopped (no result) or failed (perhaps a call with no
	// answer) leaves the history as it was.
	pending []json.RawMessage
}

func (d *coreAgentDecoder) Decode(line []byte) ([]Event, error) {
	var ln coreagentwire.Event
	if err := json.Unmarshal(line, &ln); err != nil {
		return nil, fmt.Errorf("core-agent-harness line: %w", err)
	}
	if ln.Type == coreagentwire.TypeInit {
		if d.inited || d.ended {
			return nil, nil
		}
		d.inited = true
		d.model, d.session = ln.Model, ln.Session
		return []Event{{Kind: EventInit, Session: ln.Session, Model: ln.Model}}, nil
	}
	// The one line allowed before init is a failed result: the harness
	// writes it when the turn fails to start (the envelope, the model, a
	// required persona), and it ends the turn.
	startFailure := ln.Type == coreagentwire.TypeResult && ln.Failed && !d.inited
	if (!d.inited && !startFailure) || d.ended {
		return nil, nil
	}
	switch ln.Type {
	case coreagentwire.TypeEvent:
		if len(ln.Event) > 0 {
			d.pending = append(d.pending, ln.Event)
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
		d.ended = true
		if !ln.Failed && d.conv != nil && d.session != "" {
			d.conv.mu.Lock()
			h := d.conv.history[d.session]
			for _, e := range d.pending {
				if coreagentwire.IsHistoryBoundary(e) {
					// A compaction summary: core-agent starts its next
					// request from it, so nothing before it is sent again.
					h = nil
				}
				h = append(h, e)
			}
			d.conv.history[d.session] = h
			d.conv.mu.Unlock()
		}
		d.pending = nil
		ev := Event{Kind: EventResult, Text: ln.Text, Telemetry: resultTelemetry{usage: lib.Usage{Model: d.model}}}
		if ln.Usage != nil {
			// Each count passes lib.UsageFigure, as the other drivers'
			// do: a bad one is that field dropped and named, and the
			// other still reaches the bus.
			var ok bool
			if ev.Telemetry.usage.InputTokens, ok = lib.UsageFigure(float64(ln.Usage.Input)); !ok {
				ev.Telemetry.dropped = append(ev.Telemetry.dropped, "usage.input")
			}
			if ev.Telemetry.usage.OutputTokens, ok = lib.UsageFigure(float64(ln.Usage.Output)); !ok {
				ev.Telemetry.dropped = append(ev.Telemetry.dropped, "usage.output")
			}
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
