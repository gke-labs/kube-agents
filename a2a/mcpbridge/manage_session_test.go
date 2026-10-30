package mcpbridge

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// noCallNamed fails if the gateway receives a call to tool within wait.
func noCallNamed(t *testing.T, gw *blockingGateway, tool string, wait time.Duration) {
	t.Helper()
	deadline := time.After(wait)
	for {
		select {
		case c := <-gw.calls:
			if c["name"] == tool {
				t.Fatalf("the gateway received %s %v; want none", tool, c["arguments"])
			}
		case <-deadline:
			return
		}
	}
}

func putConversations(t *testing.T, srv *Server, entries ...ConversationEntry) {
	t.Helper()
	if err := srv.Store().WithUpdateLock(func(cfg *PersistentConfig) bool {
		now := time.Now().UTC()
		for _, e := range entries {
			e.CreatedAt, e.LastUsedAt = now, now
			cfg.Conversations[e.Alias] = e
		}
		return true
	}); err != nil {
		t.Fatal(err)
	}
}

func TestManageSessionDeleteRefusesAnAmbiguousNameBeforeTheRemoteDelete(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	putConversations(t, srv,
		ConversationEntry{Alias: "session-a", Token: MintConversationToken(), SubagentRole: "Reviewer"},
		ConversationEntry{Alias: "session-b", Token: MintConversationToken(), SubagentRole: "Reviewer"},
	)
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"session-reviewer"}`))
	if res.IsError || !strings.Contains(res.Content[0].Text, "more than one conversation matches") {
		t.Fatalf("ambiguous delete: %+v; want it refused", res)
	}
	noCallNamed(t, gw, ToolManageSession, 200*time.Millisecond)
	for _, alias := range []string{"session-a", "session-b"} {
		if _, ok := srv.Store().LookupConversation(alias); !ok {
			t.Fatalf("ambiguous delete removed %s", alias)
		}
	}
}

func TestManageSessionDeleteSendsTheResolvedAliasUpstream(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	token := MintConversationToken()
	putConversations(t, srv, ConversationEntry{Alias: "c1", Token: token, SessionMode: true})
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"console:`+token+`"}`))
	if res.IsError {
		t.Fatalf("delete by token: %+v", res)
	}
	if args := nextCallNamed(t, gw, ToolManageSession); args["alias"] != "c1" || args["action"] != "delete" {
		t.Fatalf("forwarded delete arguments %v; want action delete on alias c1", args)
	}
	if _, ok := srv.Store().LookupConversation("c1"); ok {
		t.Fatal("c1 is still saved after its delete")
	}
}

func TestManageSessionDeleteByWorkerIDStopsTheRemoteSessionFirst(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	putConversations(t, srv, ConversationEntry{Alias: "c1", Token: MintConversationToken(), SessionWorkerID: "worker-1"})
	gw.srv.Close()
	// DeleteConversation matches worker-1 to c1, whose session worker must be stopped upstream
	// first: with the gateway gone, nothing is deleted.
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"worker-1"}`))
	if !res.IsError {
		t.Fatalf("delete by worker ID with no gateway: %+v; want the remote failure", res)
	}
	if _, ok := srv.Store().LookupConversation("c1"); !ok {
		t.Fatal("c1 was deleted locally though its session worker was never stopped")
	}
}

func TestManageSessionActionsTheServerLacksAreHiddenAndRefused(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	for _, d := range srv.toolDefinitions() {
		if d.Name != ToolManageSession {
			continue
		}
		raw, _ := json.Marshal(d.InputSchema)
		var schema struct {
			Properties map[string]struct {
				Enum []string `json:"enum"`
			} `json:"properties"`
		}
		if err := json.Unmarshal(raw, &schema); err != nil {
			t.Fatal(err)
		}
		for _, a := range schema.Properties["action"].Enum {
			if a == actionTranscript || a == actionClearKilled {
				t.Fatalf("manage_session advertises %s; the in-cluster server refuses it", a)
			}
		}
		if _, ok := schema.Properties["debug"]; ok {
			t.Fatal("manage_session advertises debug, an argument only transcript takes")
		}
		if strings.Contains(d.Description, "transcript") || strings.Contains(d.Description, "killed") {
			t.Fatalf("manage_session's description offers a gated action: %s", d.Description)
		}
	}
	for _, action := range []string{"transcript", "clear_killed", "prune"} {
		res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"`+action+`","alias":"c1"}`))
		if !res.IsError || !strings.Contains(res.Content[0].Text, "not supported by the in-cluster server yet") {
			t.Fatalf("manage_session %s: %+v; want it refused as not supported yet", action, res)
		}
		if _, _, _, err := srv.CallRemoteTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"`+action+`"}`), CallMeta{}, nil); err == nil ||
			!strings.Contains(err.Error(), "not supported by the in-cluster server yet") {
			t.Fatalf("CallRemoteTool manage_session %s: err %v; want it refused", action, err)
		}
	}
	if srv.IsSessionHealthy() {
		t.Fatal("a refused manage_session action connected")
	}
	noCallNamed(t, gw, ToolManageSession, 100*time.Millisecond)
}

func TestManageSessionReturnsTheServersRefusalInsteadOfTheLocalList(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	putConversations(t, srv, ConversationEntry{Alias: "c1", Token: MintConversationToken()})
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delte","alias":"c1"}`))
	if !res.IsError || !strings.Contains(res.Content[0].Text, "unknown action") {
		t.Fatalf("misspelled action: %+v; want the server's refusal as an error", res)
	}
	res = srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"list"}`))
	if res.IsError || res.Content[0].Text != "gateway ran manage_session" {
		t.Fatalf("list with the server up: %+v; want the server's list", res)
	}

	gw.srv.Close()
	srv.Close()
	srv = newStdioTestServer(t, gw)
	putConversations(t, srv, ConversationEntry{Alias: "c1", Token: MintConversationToken()})
	res = srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"help"}`))
	if !res.IsError {
		t.Fatalf("help with the server down: %+v; want an error, not the local list", res)
	}
	res = srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"list"}`))
	if res.IsError || !strings.Contains(res.Content[0].Text, "c1") || !strings.Contains(res.Content[0].Text, "not the server's") {
		t.Fatalf("list with the server down: %+v; want the local list, marked as local", res)
	}
}
