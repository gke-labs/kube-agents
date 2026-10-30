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
	if !res.IsError || !strings.Contains(res.Content[0].Text, "more than one conversation matches") {
		t.Fatalf("ambiguous delete: %+v; want it refused as an error", res)
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

func TestManageSessionDeleteOfASessionDeletesLocallyWhenTheRemoteDeleteFails(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	// The server restarted and forgot the name: it refuses the delete.
	putConversations(t, srv, ConversationEntry{Alias: "ghost", Token: MintConversationToken(), SessionMode: true})
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"ghost"}`))
	if res.IsError || !strings.Contains(res.Content[0].Text, "no conversation named ghost") {
		t.Fatalf("delete of a session the server forgot: %+v; want the local delete with the server's answer as a warning", res)
	}
	if _, ok := srv.Store().LookupConversation("ghost"); ok {
		t.Fatal("ghost is still saved because the server no longer held the name")
	}

	putConversations(t, srv, ConversationEntry{Alias: "c1", Token: MintConversationToken(), SessionWorkerID: "worker-1"})
	gw.srv.Close()
	// DeleteConversation matches worker-1 to c1. With the gateway gone the remote delete fails:
	// c1 is still deleted here, and the result warns about the server.
	res = srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"worker-1"}`))
	if res.IsError || !strings.Contains(res.Content[0].Text, "Deleted session **`c1`**") || !strings.Contains(res.Content[0].Text, "server's delete failed") {
		t.Fatalf("delete by worker ID with no gateway: %+v; want the local delete with a warning about the server", res)
	}
	if _, ok := srv.Store().LookupConversation("c1"); ok {
		t.Fatal("c1 is still saved because the server's delete failed")
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

func TestManageSessionNewWithoutAnAliasKeepsTheActiveConversation(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	active := srv.Store().ActiveConversation()
	if _, err := srv.Store().SetConversationSessionMode(active.Alias, true); err != nil {
		t.Fatal(err)
	}
	for _, args := range []string{`{"action":"new"}`, `{"action":"new","alias":"  "}`} {
		if res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(args)); !res.IsError {
			t.Fatalf("manage_session %s: %+v; want it refused", args, res)
		}
	}
	if c, _ := srv.Store().LookupConversation(active.Alias); c.Token != active.Token || !c.SessionMode {
		t.Fatalf("new without an alias replaced the active conversation: %+v (was %+v)", c, active)
	}
}

func TestManageSessionDeleteOfANameNobodyHoldsIsAnError(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"ghost"}`))
	if !res.IsError || !strings.Contains(res.Content[0].Text, "no conversation named ghost") {
		t.Fatalf("delete of a name neither the bridge nor the server holds: %+v; want the server's refusal as an error", res)
	}
	gw.srv.Close()
	res = srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":"ghost2"}`))
	if !res.IsError {
		t.Fatalf("delete of an unknown name with the server unreachable: %+v; want an error", res)
	}
}

func TestManageSessionDeleteWithoutAnAliasDoesNotConnect(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"delete","alias":" "}`))
	if !res.IsError || !strings.Contains(res.Content[0].Text, "needs the conversation's alias") {
		t.Fatalf("delete with no alias: %+v; want it refused as an error", res)
	}
	if srv.IsSessionHealthy() {
		t.Fatal("a delete with no alias connected to the cluster")
	}
}

func TestManageSessionNewRefusesAnAliasThatExists(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(`{"action":"new","alias":"incident-7"}`))
	if res.IsError {
		t.Fatalf("manage_session new incident-7: %+v", res)
	}
	existing, ok := srv.Store().LookupConversation("incident-7")
	if !ok {
		t.Fatal("setup: incident-7 was not created")
	}
	if _, err := srv.Store().SetConversationSessionMode(existing.Alias, true); err != nil {
		t.Fatal(err)
	}
	for _, args := range []string{`{"action":"new","alias":"incident-7"}`, `{"action":"new","alias":"console:incident-7"}`} {
		res := srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(args))
		if !res.IsError || len(res.Content) == 0 {
			t.Fatalf("manage_session %s: %+v; want it refused", args, res)
		}
		text := res.Content[0].Text
		if !strings.Contains(text, "alias already exists; pick another or delete it first") || !strings.Contains(text, "incident-7") {
			t.Fatalf("manage_session %s refused with %q; want it to name the alias and say it already exists", args, text)
		}
	}
	if c, _ := srv.Store().LookupConversation("incident-7"); c.Token != existing.Token || !c.SessionMode {
		t.Fatalf("new with an existing alias replaced the conversation: %+v (was %+v)", c, existing)
	}
}

// A dedicated subagent/session conversation can't be the active one: switch and new refuse
// it with the reason instead of answering "Switched" while nothing changed, and nothing the
// bridge does makes one active, so a restart has no reason to move the active conversation.
func TestManageSessionSwitchRefusesADedicatedSubagentConversation(t *testing.T) {
	gw := startBlockingGateway(t, "")
	srv := newStdioTestServer(t, gw)
	putConversations(t, srv,
		ConversationEntry{Alias: "c1", Token: MintConversationToken()},
		ConversationEntry{Alias: "c2", Token: MintConversationToken()},
		ConversationEntry{Alias: "session-1", Token: MintConversationToken()},
		ConversationEntry{Alias: "reviewer", Token: MintConversationToken(), SubagentRole: "Reviewer"},
	)
	call := func(args string) CallToolResult {
		return srv.CallTool(context.Background(), ToolManageSession, json.RawMessage(args))
	}
	if res := call(`{"action":"switch","alias":"c1"}`); res.IsError || !strings.Contains(res.Content[0].Text, "Switched active conversation to **`c1`**") {
		t.Fatalf("switch c1: %+v", res)
	}
	for _, alias := range []string{"session-1", "reviewer", "s-new", "chat-new"} {
		res := call(`{"action":"switch","alias":"` + alias + `"}`)
		if !res.IsError || !strings.Contains(res.Content[0].Text, "dedicated subagent/session conversation") || strings.Contains(res.Content[0].Text, "Switched") {
			t.Errorf("switch %s: %+v; want it refused, saying why", alias, res)
		}
		if got := srv.Store().Snapshot().ActiveConversation; got != "c1" {
			t.Errorf("switch %s moved the active conversation to %q", alias, got)
		}
	}
	for _, alias := range []string{"s-new", "chat-new"} {
		if _, ok := srv.Store().LookupConversation(alias); ok {
			t.Errorf("a refused switch created %s", alias)
		}
	}
	if res := call(`{"action":"new","alias":"session-9"}`); !res.IsError || !strings.Contains(res.Content[0].Text, "dedicated subagent/session conversation") {
		t.Errorf("new session-9: %+v; want it refused, saying why", res)
	}
	if _, ok := srv.Store().LookupConversation("session-9"); ok {
		t.Error("a refused new created session-9")
	}
	// A turn tool naming a new session- conversation creates it without making it active.
	if _, err := srv.Store().EnsureConversation("session-10", false, ""); err != nil {
		t.Fatal(err)
	}
	if got := srv.Store().Snapshot().ActiveConversation; got != "c1" {
		t.Errorf("creating session-10 made %q active; want c1 kept", got)
	}
	if res := call(`{"action":"switch","alias":"c2"}`); res.IsError || srv.Store().Snapshot().ActiveConversation != "c2" {
		t.Fatalf("switch c2: %+v, active %q", res, srv.Store().Snapshot().ActiveConversation)
	}
}
