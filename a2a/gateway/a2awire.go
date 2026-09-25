package gateway

import (
	"encoding/json"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The A2A door's wire: JSON-RPC 2.0 over HTTP, the object shapes the A2A
// protocol puts on it, and the agent card. Only the fields the door reads or
// writes are modelled. Below `payload` the bus already speaks standard A2A
// (spec-a2a-payloads.md's layering rule), which is why the door can reuse
// lib.Message, lib.Part, lib.Artifact and lib.TaskStatus as they are and
// needs only the request/response envelopes, the Task object, and the card.

const (
	// a2aProtocolVersion is the A2A protocol version the card advertises.
	// The wire below is the JSON-RPC binding of the 0.3 line; the exact pin
	// a given client (Antigravity) wants is an open question on the track,
	// and this constant is where the answer lands.
	a2aProtocolVersion = "0.3.0"

	a2aMethodSend    = "message/send"
	a2aMethodStream  = "message/stream"
	a2aMethodGet     = "tasks/get"
	a2aMethodCancel  = "tasks/cancel"
	a2aKindTask      = "task"
	a2aKindMessage   = "message"
	a2aRoleAgent     = "agent"
	a2aRoleUser      = "user"
	a2aPartKindText  = "text"
	a2aContentType   = "application/json"
	a2aCardPath      = "/.well-known/agent-card.json"
	a2aRPCPath       = "/a2a"
	a2aTextMediaType = "text/plain"
)

// JSON-RPC 2.0 error codes: the standard four the door can hit, and the A2A
// protocol's own, which sit in the server-defined range.
const (
	rpcParseError     = -32700
	rpcInvalidRequest = -32600
	rpcMethodNotFound = -32601
	rpcInvalidParams  = -32602
	rpcInternalError  = -32603

	a2aErrTaskNotFound       = -32001
	a2aErrTaskNotCancelable  = -32002
	a2aErrUnsupportedOp      = -32004
	a2aErrContentTypeNotSupp = -32005
	a2aErrAuthenticationFail = -32010 // not in the spec's table; the door's own for an unmapped caller
)

// rpcRequest is one JSON-RPC 2.0 request. ID is kept raw so it is echoed
// back exactly as sent (a string or a number, per the spec).
type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// rpcResponse is one JSON-RPC 2.0 response: exactly one of Result and Error.
type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    any    `json:"data,omitempty"`
}

func rpcOK(id json.RawMessage, result any) rpcResponse {
	if len(id) == 0 {
		id = json.RawMessage("null")
	}
	return rpcResponse{JSONRPC: "2.0", ID: id, Result: result}
}

func rpcFail(id json.RawMessage, code int, message string, data any) rpcResponse {
	if len(id) == 0 {
		id = json.RawMessage("null")
	}
	return rpcResponse{JSONRPC: "2.0", ID: id, Error: &rpcError{Code: code, Message: message, Data: data}}
}

// a2aSendParams is the params object of message/send and message/stream.
type a2aSendParams struct {
	Message       a2aInboundMessage `json:"message"`
	Configuration *a2aSendConfig    `json:"configuration,omitempty"`
}

// a2aInboundMessage is the caller's Message. Metadata is where a caller that
// cannot set headers names itself (a2aCallerMetadataKey); see
// A2ADoor.callerOf.
type a2aInboundMessage struct {
	Role      string         `json:"role"`
	Parts     []lib.Part     `json:"parts"`
	MessageID string         `json:"messageId"`
	TaskID    string         `json:"taskId,omitempty"`
	ContextID string         `json:"contextId,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
}

type a2aSendConfig struct {
	// Blocking asks message/send to wait for the task's terminal and return
	// the finished Task, bounded by a2aBlockingWait. Absent, the door
	// answers as soon as the submission is on the bus. The card offers
	// text/plain alone, so acceptedOutputModes is not read.
	Blocking *bool `json:"blocking,omitempty"`
}

// a2aIDParams is the params object of tasks/get and tasks/cancel. Metadata
// is where a client that cannot set headers names itself, as on a send.
type a2aIDParams struct {
	ID            string         `json:"id"`
	HistoryLength *int           `json:"historyLength,omitempty"`
	Metadata      map[string]any `json:"metadata,omitempty"`
}

// a2aTaskObject is the A2A Task as the door returns it.
type a2aTaskObject struct {
	ID        string         `json:"id"`
	ContextID string         `json:"contextId"`
	Status    lib.TaskStatus `json:"status"`
	Artifacts []lib.Artifact `json:"artifacts,omitempty"`
	History   []lib.Message  `json:"history,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
	Kind      string         `json:"kind"`
}

// a2aMessageObject is an agent Message the door returns when a turn was
// answered without a task: a refusal, a status answer, a steer's
// acknowledgement, a reply.
type a2aMessageObject struct {
	Role      string         `json:"role"`
	Parts     []lib.Part     `json:"parts"`
	MessageID string         `json:"messageId"`
	ContextID string         `json:"contextId,omitempty"`
	TaskID    string         `json:"taskId,omitempty"`
	Metadata  map[string]any `json:"metadata,omitempty"`
	Kind      string         `json:"kind"`
}

// The agent card. Skills are the catalog: destinations this door will route
// to. In the first version that is the gateway's default addressee alone;
// when profiles land, the same list is rendered from DIRECTORY and the
// caller's entitlements, and the router reads the same list.
type a2aAgentCard struct {
	Name               string                `json:"name"`
	Description        string                `json:"description"`
	URL                string                `json:"url"`
	Version            string                `json:"version"`
	ProtocolVersion    string                `json:"protocolVersion"`
	Capabilities       a2aCapabilities       `json:"capabilities"`
	DefaultInputModes  []string              `json:"defaultInputModes"`
	DefaultOutputModes []string              `json:"defaultOutputModes"`
	Skills             []a2aSkill            `json:"skills"`
	SecuritySchemes    map[string]a2aScheme  `json:"securitySchemes,omitempty"`
	Security           []map[string][]string `json:"security,omitempty"`
}

type a2aCapabilities struct {
	Streaming              bool `json:"streaming"`
	PushNotifications      bool `json:"pushNotifications"`
	StateTransitionHistory bool `json:"stateTransitionHistory"`
}

type a2aSkill struct {
	ID          string   `json:"id"`
	Name        string   `json:"name"`
	Description string   `json:"description"`
	Tags        []string `json:"tags,omitempty"`
	Examples    []string `json:"examples,omitempty"`
}

type a2aScheme struct {
	Type   string `json:"type"`
	Scheme string `json:"scheme,omitempty"`
}

// textOf joins the text parts of a message, which is all the gateway routes
// on: a data or file part has no home on a chat turn and is refused at the
// door rather than silently dropped.
func textOf(parts []lib.Part) (string, bool) {
	text := ""
	for _, p := range parts {
		if p.Kind != a2aPartKindText {
			return "", false
		}
		if text != "" && p.Text != "" {
			text += "\n"
		}
		text += p.Text
	}
	return text, true
}

func textParts(texts ...string) []lib.Part {
	parts := make([]lib.Part, 0, len(texts))
	for _, t := range texts {
		if t == "" {
			continue
		}
		parts = append(parts, lib.Part{Kind: a2aPartKindText, Text: t})
	}
	return parts
}
