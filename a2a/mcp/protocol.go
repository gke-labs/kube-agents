// Package mcp is the kube-agents Model Context Protocol server: a JSON-RPC
// 2.0 surface over HTTP that lets an external coding agent or IDE host talk to
// one in-cluster `next` install as a console client.
//
// It runs as its own Deployment (a2a/cmd/mcp), holding the console credential
// and nothing else, and only when the install opts in. A caller authenticates
// with the server's own bearer token; the bus never sees that token, and the
// server never takes a caller's word for which bus to dial.
//
// This is the core: the protocol, the transport, the one bus connection and
// the connection_status tool. The tools that send turns, steer tasks and read
// topics are registered on top of it (gke-labs/kube-agents#3653 is where they
// come from).
package mcp

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"

	"github.com/gke-labs/kube-agents/a2a/mcpproto"
)

// The wire constants and types are mcpproto's, shared with MCP clients; the
// names here keep this package's own spelling of them.
const (
	jsonRPCVersion = mcpproto.JSONRPCVersion

	// ServerName and ServerVersion are what initialize reports.
	ServerName    = "kube-agents"
	ServerVersion = "0.5.0"

	methodInitialize    = mcpproto.MethodInitialize
	methodPing          = mcpproto.MethodPing
	methodToolsList     = mcpproto.MethodToolsList
	methodToolsCall     = mcpproto.MethodToolsCall
	methodResourcesList = mcpproto.MethodResourcesList
	methodPromptsList   = mcpproto.MethodPromptsList

	rpcErrParseError     = mcpproto.ErrParseError
	rpcErrInvalidRequest = mcpproto.ErrInvalidRequest
	rpcErrMethodNotFound = mcpproto.ErrMethodNotFound
	rpcErrInvalidParams  = mcpproto.ErrInvalidParams
	rpcErrInternal       = mcpproto.ErrInternal

	contentTypeText = mcpproto.ContentTypeText

	msgInvalidFrame   = "invalid JSON-RPC frame"
	msgMissingMethod  = "missing method"
	msgNullID         = "a request id may not be null"
	msgNotJSONRPC2    = "not a JSON-RPC 2.0 request"
	msgBadID          = "an id must be a string, a number or null"
	msgEmptyBatch     = "empty batch"
	msgBatchTooLong   = "a batch may hold at most %d requests"
	msgBatchTooLarge  = "a batch's answers may total at most %d bytes"
	msgNeedsToolName  = "tools/call requires a tool name"
	msgUnknownMethod  = "unknown method %q"
	msgUnknownTool    = "unknown tool %q"
	msgToolFailedHead = "**Error in `%s`**: %v"
)

// supportedProtocolVersions are the MCP revisions this server speaks, newest
// first (mcpproto.SupportedProtocolVersions). initialize answers with the
// client's requested revision when it is one of these and with the newest
// otherwise, which is the spec's negotiation: the client then decides whether
// it can proceed.
var supportedProtocolVersions = mcpproto.SupportedProtocolVersions()

// rpcRequest is mcpproto.Request plus the two fields that mark a frame as a
// response from the client, which this server sends no requests to and so
// answers with nothing. That detection is the server's, so the fields are not
// in the shared type.
type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
	Result  json.RawMessage `json:"result,omitempty"`
	Error   json.RawMessage `json:"error,omitempty"`
}

type (
	rpcResponse = mcpproto.Response
	rpcError    = mcpproto.Error

	// ToolDefinition is one entry of tools/list.
	ToolDefinition = mcpproto.ToolDefinition
	// TextContent is one content block of a tool result.
	TextContent = mcpproto.TextContent
	// CallToolResult is what tools/call answers with.
	CallToolResult = mcpproto.CallToolResult
)

// Tool is one registered tool: its schema and the function that runs it. The
// function returns markdown for the caller to read; an error becomes an
// isError result naming the tool, so a failed tool never fails the RPC.
type Tool struct {
	Definition ToolDefinition
	Call       func(ctx context.Context, args json.RawMessage) (string, error)
}

// Server is the protocol core: it dispatches JSON-RPC frames to the tools it
// was built with. It holds no transport and no bus; those are the tools' and
// the HTTP handler's.
type Server struct {
	tools map[string]Tool
	order []string
}

// NewServer builds a Server over tools, listed in the order given.
func NewServer(tools ...Tool) (*Server, error) {
	s := &Server{tools: make(map[string]Tool, len(tools))}
	for _, t := range tools {
		name := t.Definition.Name
		if name == "" || t.Call == nil {
			return nil, errors.New("a tool needs a name and a function")
		}
		if _, dup := s.tools[name]; dup {
			return nil, fmt.Errorf("tool %q registered twice", name)
		}
		s.tools[name] = t
		s.order = append(s.order, name)
	}
	return s, nil
}

// HandleJSONRPC answers one JSON-RPC frame or batch. A notification (no id)
// and a client's response get no answer, which the caller reports as nil; a
// batch is answered with the array of the answers its members get.
func (s *Server) HandleJSONRPC(ctx context.Context, raw []byte) []byte {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) > 0 && trimmed[0] == '[' {
		frames, tooLong, err := batchFrames(trimmed)
		if err != nil {
			return marshalRPCError(json.RawMessage("null"), rpcErrParseError, msgInvalidFrame)
		}
		if tooLong {
			return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, fmt.Sprintf(msgBatchTooLong, maxBatchFrames))
		}
		if len(frames) == 0 {
			return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, msgEmptyBatch)
		}
		// Each answer is written into the response as it is produced, so it
		// is held once, and the whole is refused once it passes its budget.
		var out bytes.Buffer
		for _, f := range frames {
			a := s.handleOne(ctx, f)
			if a == nil {
				continue
			}
			// The response once a is in: what is written, a's separator
			// or opening bracket, a, and the closing bracket.
			if out.Len()+len(a)+len("[]") > maxBatchResponseBytes {
				return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, fmt.Sprintf(msgBatchTooLarge, maxBatchResponseBytes))
			}
			if out.Len() == 0 {
				out.WriteByte('[')
			} else {
				out.WriteByte(',')
			}
			out.Write(a)
		}
		if out.Len() == 0 {
			return nil
		}
		out.WriteByte(']')
		return out.Bytes()
	}
	return s.handleOne(ctx, trimmed)
}

func (s *Server) handleOne(ctx context.Context, raw []byte) []byte {
	// Unparseable JSON is a parse error; valid JSON that is not an object
	// (a number, a string, an array inside a batch) is an invalid request.
	if !json.Valid(raw) {
		return marshalRPCError(json.RawMessage("null"), rpcErrParseError, msgInvalidFrame)
	}
	var req rpcRequest
	if err := json.Unmarshal(raw, &req); err != nil {
		return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, msgInvalidFrame)
	}
	if !validID(req.ID) {
		return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, msgBadID)
	}
	if req.JSONRPC != jsonRPCVersion {
		return marshalRPCError(idOrNull(req.ID), rpcErrInvalidRequest, msgNotJSONRPC2)
	}
	if req.Method == "" && (len(req.Result) > 0 || len(req.Error) > 0) {
		return nil
	}
	if req.Method == "" {
		return marshalRPCError(idOrNull(req.ID), rpcErrInvalidRequest, msgMissingMethod)
	}
	// MCP forbids a null id, and JSON-RPC answers one rather than treating
	// it as a notification, so it is refused out loud, not dropped.
	if string(req.ID) == "null" {
		return marshalRPCError(json.RawMessage("null"), rpcErrInvalidRequest, msgNullID)
	}
	if len(req.ID) == 0 {
		return nil
	}

	switch req.Method {
	case methodInitialize:
		var params struct {
			ProtocolVersion string `json:"protocolVersion"`
		}
		_ = json.Unmarshal(req.Params, &params)
		version := supportedProtocolVersions[0]
		if slices.Contains(supportedProtocolVersions, params.ProtocolVersion) {
			version = params.ProtocolVersion
		}
		return marshalRPCResult(req.ID, map[string]any{
			"protocolVersion": version,
			"capabilities":    map[string]any{"tools": map[string]any{}},
			"serverInfo":      map[string]any{"name": ServerName, "version": ServerVersion},
		})
	case methodPing:
		return marshalRPCResult(req.ID, map[string]any{})
	case methodToolsList:
		defs := make([]ToolDefinition, 0, len(s.order))
		for _, name := range s.order {
			defs = append(defs, s.tools[name].Definition)
		}
		return marshalRPCResult(req.ID, map[string]any{"tools": defs})
	case methodToolsCall:
		var params struct {
			Name      string          `json:"name"`
			Arguments json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(req.Params, &params); err != nil || params.Name == "" {
			return marshalRPCError(req.ID, rpcErrInvalidParams, msgNeedsToolName)
		}
		// An unknown tool is a protocol error, as the MCP spec has it, so the
		// host sees it, not the model as a tool that ran and failed.
		if _, ok := s.tools[params.Name]; !ok {
			return marshalRPCError(req.ID, rpcErrInvalidParams, fmt.Sprintf(msgUnknownTool, params.Name))
		}
		return marshalRPCResult(req.ID, s.CallTool(ctx, params.Name, params.Arguments))
	// Neither is a capability this server declares; an empty list is the
	// polite answer to a client that asks anyway.
	case methodResourcesList:
		return marshalRPCResult(req.ID, map[string]any{"resources": []any{}})
	case methodPromptsList:
		return marshalRPCResult(req.ID, map[string]any{"prompts": []any{}})
	default:
		return marshalRPCError(req.ID, rpcErrMethodNotFound, fmt.Sprintf(msgUnknownMethod, req.Method))
	}
}

// CallTool runs one tool by name.
func (s *Server) CallTool(ctx context.Context, name string, args json.RawMessage) CallToolResult {
	t, ok := s.tools[name]
	if !ok {
		return CallToolResult{Content: []TextContent{{Type: contentTypeText, Text: fmt.Sprintf(msgUnknownTool, name)}}, IsError: true}
	}
	text, err := t.Call(ctx, args)
	if err != nil {
		msg := fmt.Sprintf(msgToolFailedHead, name, err)
		if text != "" {
			msg = text + "\n\n" + msg
		}
		return CallToolResult{Content: []TextContent{{Type: contentTypeText, Text: msg}}, IsError: true}
	}
	return CallToolResult{Content: []TextContent{{Type: contentTypeText, Text: text}}}
}

// validID reports whether id is absent or a JSON string, number or null, the
// only kinds JSON-RPC allows.
func validID(id json.RawMessage) bool {
	if len(id) == 0 {
		return true
	}
	switch c := id[0]; {
	case c == '"', c == '-', c >= '0' && c <= '9':
		return true
	default:
		return string(id) == "null"
	}
}

func idOrNull(id json.RawMessage) json.RawMessage {
	if len(id) == 0 {
		return json.RawMessage("null")
	}
	return id
}

func marshalRPCResult(id json.RawMessage, result any) []byte {
	b, err := json.Marshal(rpcResponse{JSONRPC: jsonRPCVersion, ID: id, Result: result})
	if err != nil {
		return marshalRPCError(id, rpcErrInternal, err.Error())
	}
	return b
}

func marshalRPCError(id json.RawMessage, code int, msg string) []byte {
	b, _ := json.Marshal(rpcResponse{JSONRPC: jsonRPCVersion, ID: id, Error: &rpcError{Code: code, Message: msg}})
	return b
}

// maxBatchFrames bounds a JSON-RPC batch. Every member is answered before
// anything is written, and a malformed member's answer outweighs the member
// (a two-byte `7` gets an 85-byte error), so an unbounded batch inside the
// body cap would be answered tens of times over its own size. The 2025-06-18
// revision this server prefers dropped batching; older clients batch a
// handful of requests at most.
const maxBatchFrames = 64

// maxBatchResponseBytes bounds what a batch's answers may total. The member
// cap bounds their number, not their size: 64 large tool answers (a
// connection_status over a full DIRECTORY) would all be held before anything
// is written. A batch whose answers pass it gets one error instead.
const maxBatchResponseBytes = 8 << 20

// batchFrames splits a batch into its members as a stream, stopping once it
// holds more than maxBatchFrames, so a long batch is refused without first
// holding a copy of every member.
func batchFrames(raw []byte) (frames []json.RawMessage, tooLong bool, err error) {
	dec := json.NewDecoder(bytes.NewReader(raw))
	if _, err := dec.Token(); err != nil {
		return nil, false, err
	}
	for dec.More() {
		if len(frames) == maxBatchFrames {
			return nil, true, nil
		}
		var f json.RawMessage
		if err := dec.Decode(&f); err != nil {
			return nil, false, err
		}
		frames = append(frames, f)
	}
	if _, err := dec.Token(); err != nil {
		return nil, false, err
	}
	// Nothing but whitespace may follow the batch. More reports false at a
	// closing bracket, so a stray `]` or `}` needs the next token read.
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return nil, false, errors.New("trailing data after the batch")
	}
	return frames, false, nil
}
