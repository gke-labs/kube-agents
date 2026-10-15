package workeradapter

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"os"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The session's one tool: ask the gateway to mint a child task. The harness
// speaks MCP to this process over stdio; this process speaks one JSON object
// to the adapter over a unix socket in the pod's scratch volume. The adapter
// is the party that validates, publishes the `delegate` artifact and ends the
// turn (spec-chatops-gateway.md, "Sessions by default"); this server only
// carries the call and reports the adapter's answer.
const (
	DelegateToolName      = "delegate"
	DelegateMCPServer     = "a2a"
	EnvDelegateSocket     = "A2A_DELEGATE_SOCKET"
	DefaultDelegateSocket = "/scratch/a2a-delegate.sock"
	mcpProtocolVersion    = "2025-06-18"
	// delegateExchangeTimeout bounds the whole socket round trip: dial,
	// write the request, and read the reply. It is not the adapter's own
	// dial accept time; the adapter's listener may take up to 10s to hand
	// the connection off and publish the artifact before it replies, and a
	// shorter deadline here would report failure for a request the adapter
	// then goes on to publish anyway.
	delegateExchangeTimeout = 15 * time.Second
)

// delegateReply is the socket's answer; it has no reserved shape elsewhere,
// so unlike the request it stays package-local.
type delegateReply struct {
	OK      bool   `json:"ok"`
	Message string `json:"message"`
}

type rpcRequest struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

type rpcResponse struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      json.RawMessage `json:"id"`
	Result  any             `json:"result,omitempty"`
	Error   *rpcError       `json:"error,omitempty"`
}

var delegateToolSchema = map[string]any{
	"name":        DelegateToolName,
	"description": "Hand a task to another agent on the user's behalf. The gateway checks the user may reach that agent, runs the task, and its result arrives as your next turn. Calling this ends the current turn.",
	"inputSchema": map[string]any{
		"type": "object",
		"properties": map[string]any{
			"addressee": map[string]any{"type": "string", "description": "The agent to hand the task to. Today only \"platform\"."},
			"text":      map[string]any{"type": "string", "description": "The task, as you would state it to that agent."},
		},
		"required": []string{"addressee", "text"},
	},
}

// DelegateSocketPath resolves the unix socket the MCP server and the adapter
// agree on: EnvDelegateSocket if set, else DefaultDelegateSocket. One helper
// so the mcp subcommand and the harness-flag wiring that reuses it (the
// adapter's own listener) never drift apart on the fallback.
func DelegateSocketPath() string {
	if p := os.Getenv(EnvDelegateSocket); p != "" {
		return p
	}
	return DefaultDelegateSocket
}

// mcpLineMaxBytes is the ceiling on one JSON-RPC line from the harness. It
// sits far above lib.DelegateTextCap, so a delegate call over the cap still
// reaches the adapter and is refused with a reason the harness can read; a
// line over the ceiling stops the server with bufio.ErrTooLong. Not
// harness.go's scannerMaxBytes: that one bounds the largest answer a worker
// can return, and the two should be free to move apart. The starting buffer
// is harness.go's scannerInitialBytes.
const mcpLineMaxBytes = 4 * 1024 * 1024

// ServeDelegateMCP runs the MCP server until in is closed or ctx ends.
func ServeDelegateMCP(ctx context.Context, in io.Reader, out io.Writer, socketPath string, log *slog.Logger) error {
	sc := bufio.NewScanner(in)
	sc.Buffer(make([]byte, 0, scannerInitialBytes), mcpLineMaxBytes)
	enc := json.NewEncoder(out)
	for sc.Scan() {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		var req rpcRequest
		if err := json.Unmarshal(line, &req); err != nil {
			log.Warn("mcp: unreadable line", "err", err)
			continue
		}
		if len(req.ID) == 0 { // a notification; nothing to answer
			continue
		}
		resp := rpcResponse{JSONRPC: "2.0", ID: req.ID}
		switch req.Method {
		case "initialize":
			resp.Result = map[string]any{
				"protocolVersion": mcpProtocolVersion,
				"capabilities":    map[string]any{"tools": map[string]any{}},
				"serverInfo":      map[string]any{"name": DelegateMCPServer, "version": "1"},
			}
		case "tools/list":
			resp.Result = map[string]any{"tools": []any{delegateToolSchema}}
		case "tools/call":
			resp.Result = callDelegate(req.Params, socketPath, log)
		case "ping":
			resp.Result = map[string]any{}
		default:
			resp.Error = &rpcError{Code: -32601, Message: "method not found: " + req.Method}
		}
		if err := enc.Encode(resp); err != nil {
			return err
		}
	}
	return sc.Err()
}

func toolText(text string, isError bool) map[string]any {
	return map[string]any{
		"content": []any{map[string]any{"type": "text", "text": text}},
		"isError": isError,
	}
}

func callDelegate(params json.RawMessage, socketPath string, log *slog.Logger) map[string]any {
	var p struct {
		Name      string              `json:"name"`
		Arguments lib.DelegateRequest `json:"arguments"`
	}
	if err := json.Unmarshal(params, &p); err != nil || p.Name != DelegateToolName {
		return toolText("unknown tool", true)
	}
	reply, err := forwardDelegate(socketPath, p.Arguments)
	if err != nil {
		log.Warn("mcp: delegate forward failed", "err", err)
		return toolText("the delegation channel is unavailable: "+err.Error(), true)
	}
	return toolText(reply.Message, !reply.OK)
}

func forwardDelegate(socketPath string, req lib.DelegateRequest) (delegateReply, error) {
	c, err := net.DialTimeout("unix", socketPath, delegateExchangeTimeout)
	if err != nil {
		return delegateReply{}, err
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(delegateExchangeTimeout))
	if err := json.NewEncoder(c).Encode(req); err != nil {
		return delegateReply{}, err
	}
	var reply delegateReply
	if err := json.NewDecoder(c).Decode(&reply); err != nil {
		return delegateReply{}, fmt.Errorf("reading the adapter's reply: %w", err)
	}
	return reply, nil
}
