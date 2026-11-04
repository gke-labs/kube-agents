package mcpbridge

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestPollEventsIsListedWithoutAHost(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	var poll, send *ToolDefinition
	defs := srv.toolDefinitions()
	for i := range defs {
		switch defs[i].Name {
		case ToolPollEvents:
			poll = &defs[i]
		case ToolSendMessage:
			send = &defs[i]
		}
	}
	if poll == nil {
		t.Fatal("poll_events is not listed by a bridge with no host integration")
	}
	props, _ := poll.InputSchema["properties"].(map[string]any)
	for _, arg := range []string{"conversation", "conversation_token", "since", "wait_seconds"} {
		if _, ok := props[arg]; !ok {
			t.Fatalf("poll_events schema lacks %q: %v", arg, props)
		}
	}
	if send == nil || !strings.Contains(send.Description, "poll_events") {
		t.Fatalf("send_message's description does not point at poll_events: %+v", send)
	}
}

func TestPollEventsForwardsTokenSinceAndWait(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	res := srv.CallTool(context.Background(), ToolPollEvents,
		json.RawMessage(`{"conversation_token":"mcp0123","conversation":"c1","since":42,"wait_seconds":30}`))
	if res.IsError {
		t.Fatalf("poll_events: %+v", res)
	}
	if got := res.Content[0].Text; got != "gateway ran poll_events" {
		t.Fatalf("poll_events answered %q; want the server's answer", got)
	}
	args := nextCallNamed(t, gw, ToolPollEvents)
	if args["conversation_token"] != "mcp0123" || args["since"] != float64(42) || args["wait_seconds"] != float64(30) {
		t.Fatalf("forwarded poll_events arguments %v; want conversation_token, since and wait_seconds", args)
	}
	if _, ok := args["conversation"]; ok {
		t.Fatalf("forwarded poll_events arguments %v carry conversation beside conversation_token", args)
	}
}

func TestPollEventsClampsAndReadsQuotedNumbers(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	res := srv.CallTool(context.Background(), ToolPollEvents, json.RawMessage(`{"since":"7","wait_seconds":600}`))
	if res.IsError {
		t.Fatalf("poll_events: %+v", res)
	}
	args := nextCallNamed(t, gw, ToolPollEvents)
	if args["since"] != float64(7) || args["wait_seconds"] != float64(maxPollWaitSeconds) {
		t.Fatalf("forwarded poll_events arguments %v; want since 7 and wait_seconds clamped to %d", args, maxPollWaitSeconds)
	}

	res = srv.CallTool(context.Background(), ToolPollEvents, json.RawMessage(`{"since":-1}`))
	if !res.IsError || !strings.Contains(res.Content[0].Text, "since") {
		t.Fatalf("a negative since was not refused: %+v", res)
	}
}

func TestPollEventsResolvesAnAliasOrConsoleTokenToTheAlias(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	// Created as manage_session new creates one: with a minted console token, not its alias.
	entry, err := srv.store.CreateConversation("triage", "")
	if err != nil {
		t.Fatal(err)
	}
	if entry.Token == entry.Alias {
		t.Fatalf("conversation token %q equals its alias; the test cannot tell them apart", entry.Token)
	}
	if _, err := srv.store.CreateConversation("other", ""); err != nil {
		t.Fatal(err)
	}
	// The bridge's console token is its own, not the server's: the server knows the
	// conversation by the alias the bridge's send_message sent the turn under.
	for _, given := range []string{"triage", "console:" + entry.Token, entry.Token} {
		res := srv.CallTool(context.Background(), ToolPollEvents, json.RawMessage(`{"conversation":"`+given+`"}`))
		if res.IsError {
			t.Fatalf("poll_events on %q: %+v", given, res)
		}
		args := nextCallNamed(t, gw, ToolPollEvents)
		if args["conversation"] != "triage" {
			t.Fatalf("poll_events on %q forwarded %v; want conversation triage", given, args)
		}
		if _, ok := args["conversation_token"]; ok {
			t.Fatalf("poll_events on %q forwarded the bridge's token as the server's: %v", given, args)
		}
	}
}

func TestPollEventsDefaultsToTheActiveConversation(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	if _, err := srv.store.CreateConversation("ops", ""); err != nil {
		t.Fatal(err)
	}
	res := srv.CallTool(context.Background(), ToolPollEvents, json.RawMessage(`{"since":3}`))
	if res.IsError {
		t.Fatalf("poll_events: %+v", res)
	}
	args := nextCallNamed(t, gw, ToolPollEvents)
	if args["conversation"] != "ops" || args["since"] != float64(3) {
		t.Fatalf("forwarded poll_events arguments %v; want the active conversation ops", args)
	}
}

// pollHost is a HostIntegration that reads poll_events itself.
type pollHost struct{ polled chan json.RawMessage }

func (h *pollHost) AugmentToolSchemas(defs []ToolDefinition) []ToolDefinition { return defs }
func (h *pollHost) OnBeforeToolCall(context.Context, *Server, string, json.RawMessage, CallMeta) {
}
func (h *pollHost) ResolveConversation(context.Context, *Server, string, json.RawMessage) (ConversationEntry, bool) {
	return ConversationEntry{}, false
}
func (h *pollHost) OnConnectDeployment(context.Context, *Server, *DeploymentProfile, ConversationEntry, json.RawMessage, CallMeta) {
}
func (h *pollHost) HandleManageSessionAction(context.Context, *Server, string, string, string, json.RawMessage, CallMeta) (bool, string, string, string, error) {
	return false, "", "", "", nil
}
func (h *pollHost) HandlePollEvents(_ context.Context, _ *Server, raw json.RawMessage) (string, error) {
	h.polled <- raw
	return "host events", nil
}
func (h *pollHost) OnAfterToolCall(_ context.Context, _ *Server, _ string, _ bool, text, md string) (string, string) {
	return text, md
}
func (h *pollHost) OnKeepAliveTick(context.Context, *Server) bool                   { return false }
func (h *pollHost) IsCascadeCanceled(context.Context, *Server, string, string) bool { return false }
func (h *pollHost) Close()                                                          {}

func TestPollEventsHostPathWinsWhenAHostIsSet(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	host := &pollHost{polled: make(chan json.RawMessage, 1)}
	srv.host = host
	for _, d := range srv.toolDefinitions() {
		if d.Name == ToolPollEvents {
			t.Fatal("the bridge lists its own poll_events beside a host integration's")
		}
	}
	res := srv.CallTool(context.Background(), ToolPollEvents, json.RawMessage(`{"since":5}`))
	if res.IsError || res.Content[0].Text != "host events" {
		t.Fatalf("poll_events with a host answered %+v; want the host's answer", res)
	}
	if got := string(<-host.polled); got != `{"since":5}` {
		t.Fatalf("the host got %s; want the call's arguments as given", got)
	}
	select {
	case c := <-gw.calls:
		if c["name"] == ToolPollEvents {
			t.Fatalf("poll_events went to the server with a host set: %v", c)
		}
	case <-time.After(100 * time.Millisecond):
	}
}
