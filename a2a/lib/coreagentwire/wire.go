// Package coreagentwire is the wire between the worker adapter and
// core-agent-harness, the worker's core-agent runner (a2a/cmd/core-agent-harness):
// one Envelope in on stdin per turn, Event lines out on stdout.
//
// Everything that decides what a turn may do (its tool policy and its MCP
// servers) travels in the Envelope, never in a file: the harness's commands
// run as the same UID, and a file they can reach could be rewritten during
// one turn to widen the next.
//
// The conversation's history is the adapter's, by design, for the same
// reason: the harness keeps no session file. Each turn's Envelope carries
// the history so far, the harness runs on an in-memory session seeded from
// it, and every event the turn adds comes back as an event line for the
// adapter to keep. Nothing on disk records the conversation, so nothing a
// turn's commands can write changes what the next turn sees.
package coreagentwire

import "encoding/json"

// MaxEnvelopeBytes bounds one turn's Envelope, its trailing newline aside.
// The harness refuses a larger one, failing the turn. Nothing compacts the
// history yet, so a long enough conversation reaches it (TODO(#7158):
// compact before the turn and send only the events from the latest summary
// on).
const MaxEnvelopeBytes = 16 << 20

// Envelope is one turn's input, a single JSON object on stdin.
type Envelope struct {
	// Message is the user message for the turn.
	Message string `json:"message"`
	// Session is the conversation's id; empty starts a new one, whose id the
	// init event reports.
	Session string `json:"session,omitempty"`
	// History is the conversation's events so far, each as an event line's
	// Event carried it, oldest first.
	History []json.RawMessage `json:"history,omitempty"`
	// Policy is the turn's tool policy.
	Policy Policy `json:"policy"`
	// MCP lists the MCP servers the turn may use, built at the start of the
	// process and closed at its exit.
	MCP []MCPServer `json:"mcp,omitempty"`
	// Instructions are the worker's own system-prompt lines for the turn
	// (a session pod's, as Claude Code gets them on --append-system-prompt),
	// added to the agent's instruction, never to the conversation's history.
	Instructions []string `json:"instructions,omitempty"`
}

// Policy is a turn's tool policy: the tools the model is offered and the
// rules a call is checked against before it runs. Its contents are filled
// from the persona grant.
type Policy struct {
	// Tools names the built-in tools the model is offered; nothing else is.
	Tools []string `json:"tools,omitempty"`
	// Allow and Deny are core-agent permission patterns (`tool:key` globs).
	// A call no Allow pattern matches is refused, never prompted.
	Allow []string `json:"allow,omitempty"`
	Deny  []string `json:"deny,omitempty"`
}

// MCPServer is one MCP server a turn may use; the harness runs it for the
// turn, and the persona grant decides which of its tools are offered.
type MCPServer struct {
	Name    string            `json:"name"`
	Command string            `json:"command,omitempty"`
	Args    []string          `json:"args,omitempty"`
	Env     map[string]string `json:"env,omitempty"`
	URL     string            `json:"url,omitempty"`
	Tools   []string          `json:"tools,omitempty"`
}

// Event types, one JSON object per stdout line.
const (
	TypeInit       = "init"
	TypeThinking   = "thinking"
	TypeText       = "text"
	TypeToolCall   = "tool_call"
	TypeToolResult = "tool_result"
	TypeResult     = "result"
	// TypeEvent carries one event the turn added to the conversation, for
	// the adapter to keep and send back in the next turn's History.
	TypeEvent = "event"
)

// Event is one line of the harness's output.
type Event struct {
	Type string `json:"type"`
	// Session and Model are set on init.
	Session string `json:"session,omitempty"`
	Model   string `json:"model,omitempty"`
	// Text is a thinking or text delta, a tool result's output, or the
	// result's final text.
	Text string `json:"text,omitempty"`
	// ID, Tool and Input identify a tool call; a tool result carries the
	// call's ID and Tool. Every tool call gets exactly one tool result, an
	// erroring step included.
	ID    string          `json:"id,omitempty"`
	Tool  string          `json:"tool,omitempty"`
	Input json.RawMessage `json:"input,omitempty"`
	// Failed marks a tool result that errored or was refused, or a result
	// whose turn failed; Error says why.
	Failed bool   `json:"failed,omitempty"`
	Error  string `json:"error,omitempty"`
	// Usage is set on the result: the turn's token counts.
	Usage *Usage `json:"usage,omitempty"`
	// Event is an event line's conversation event, opaque to the adapter.
	Event json.RawMessage `json:"event,omitempty"`
}

// Usage is one turn's token counts, summed over its model responses in
// int64 so a long turn's total can't wrap.
type Usage struct {
	Input    int64 `json:"input"`
	Output   int64 `json:"output"`
	Cached   int64 `json:"cached,omitempty"`
	Thoughts int64 `json:"thoughts,omitempty"`
}
