// Package mcpproto holds the Model Context Protocol wire types that both ends
// of a kube-agents MCP connection share: the JSON-RPC 2.0 envelope, the method
// names and error codes, the protocol revisions spoken, and the tool listing
// and result shapes.
//
// The in-cluster server (a2a/mcp) and a client such as the workstation bridge
// import it so the two cannot drift. It holds wire types only: anything one
// side does with a frame, such as the server's batch handling, id validation
// and tool registry, or a client's transport, stays with that side.
package mcpproto

import "encoding/json"

// JSONRPCVersion is the only "jsonrpc" value either side sends or accepts.
const JSONRPCVersion = "2.0"

// The MCP methods either side of a kube-agents connection names. The
// in-cluster server answers the requests and ignores the notifications; a
// client may also send resources/read.
const (
	MethodInitialize    = "initialize"
	MethodPing          = "ping"
	MethodToolsList     = "tools/list"
	MethodToolsCall     = "tools/call"
	MethodResourcesList = "resources/list"
	MethodResourcesRead = "resources/read"
	MethodPromptsList   = "prompts/list"

	NotificationInitialized = "notifications/initialized"
	NotificationCancelled   = "notifications/cancelled"
	NotificationProgress    = "notifications/progress"
)

// JSON-RPC 2.0 error codes.
const (
	ErrParseError     = -32700
	ErrInvalidRequest = -32600
	ErrMethodNotFound = -32601
	ErrInvalidParams  = -32602
	ErrInternal       = -32603
)

// ContentTypeText is the type of a text content block.
const ContentTypeText = "text"

// supportedProtocolVersions are the MCP revisions spoken, newest first.
var supportedProtocolVersions = []string{"2025-06-18", "2025-03-26", "2024-11-05"}

// SupportedProtocolVersions returns the MCP revisions spoken, newest first. A
// server answers initialize with the client's requested revision when it is
// one of these and with the newest otherwise; the client then decides whether
// it can proceed. The slice is a copy.
func SupportedProtocolVersions() []string {
	return append([]string(nil), supportedProtocolVersions...)
}

// Request is a JSON-RPC 2.0 request or notification frame. A notification has
// no ID.
type Request struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

// Response is a JSON-RPC 2.0 response frame: a Result or an Error, never both.
type Response struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *Error          `json:"error,omitempty"`
}

// Error is a JSON-RPC 2.0 error object.
type Error struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// ToolDefinition is one entry of tools/list.
type ToolDefinition struct {
	Name        string         `json:"name"`
	Description string         `json:"description"`
	InputSchema map[string]any `json:"inputSchema"`
}

// TextContent is one content block of a tool result.
type TextContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// CallToolResult is what tools/call answers with.
type CallToolResult struct {
	Content []TextContent `json:"content"`
	IsError bool          `json:"isError,omitempty"`
}
