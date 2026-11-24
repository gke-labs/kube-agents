package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"

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
type coreAgentDriver struct {
	// Policy and MCP are the turn's tool policy and MCP servers, set from
	// the persona grant (SEAM: tigerteam-dev-1, tigerteam-dev-3).
	Policy coreagentwire.Policy
	MCP    []coreagentwire.MCPServer
}

// coreAgentSessionFlag resumes a conversation from the harness's session log.
const coreAgentSessionFlag = "--session"

// coreAgentErrorToken is a failed turn's reason token.
const coreAgentErrorToken = "error_during_execution" // #nosec G101 -- a failure reason token, not a credential

func (coreAgentDriver) Harness() persona.Harness { return persona.CoreAgent }

func (d coreAgentDriver) EncodeUser(text string) ([]byte, error) {
	b, err := json.Marshal(coreagentwire.Envelope{Message: text, Policy: d.Policy, MCP: d.MCP})
	if err != nil {
		return nil, err
	}
	return append(b, '\n'), nil
}

func (coreAgentDriver) NewDecoder() Decoder { return &coreAgentDecoder{} }

func (coreAgentDriver) WithResume(argv []string, id string) []string {
	if id == "" {
		return argv
	}
	return append(slices.Clone(argv), coreAgentSessionFlag, id)
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
	model string
}

func (d *coreAgentDecoder) Decode(line []byte) ([]Event, error) {
	var ln coreagentwire.Event
	if err := json.Unmarshal(line, &ln); err != nil {
		return nil, fmt.Errorf("core-agent-harness line: %w", err)
	}
	switch ln.Type {
	case coreagentwire.TypeInit:
		d.model = ln.Model
		return []Event{{Kind: EventInit, Session: ln.Session, Model: ln.Model}}, nil
	case coreagentwire.TypeThinking:
		return []Event{{Kind: EventThinking, Text: ln.Text}}, nil
	case coreagentwire.TypeText:
		return []Event{{Kind: EventProgress, Text: ln.Text}}, nil
	case coreagentwire.TypeToolCall:
		return []Event{{Kind: EventActivity, Tool: ln.Tool, Input: ln.Input}}, nil
	case coreagentwire.TypeToolResult:
		return []Event{{Kind: EventToolResult, Tool: ln.Tool, Text: ln.Text, Failed: ln.Failed}}, nil
	case coreagentwire.TypeResult:
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
