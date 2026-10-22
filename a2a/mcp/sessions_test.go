package mcp

import (
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

const fakeEpoch = 1700000000

// fakeClock starts at fakeEpoch and moves a minute on every read.
func fakeClock() func() time.Time {
	var mu sync.Mutex
	at := time.Unix(fakeEpoch, 0)
	return func() time.Time {
		mu.Lock()
		defer mu.Unlock()
		at = at.Add(time.Minute)
		return at
	}
}

// sessionServer is a manage_session and send_message server on a real bus
// with a recording fake gateway.
func sessionServer(t *testing.T) (*Server, *Conversations, *frameLog) {
	t.Helper()
	url, js := testBus(t)
	frames := &frameLog{}
	runFakeGateway(t, url, js, gatewayScenario{finish: true, frames: frames})
	b := dial(t, url)
	convs := NewConversations()
	convs.now = fakeClock()
	return mustServer(t, SendMessageTool(b, convs), ManageSessionTool(b, convs)), convs, frames
}

// busless is a manage_session server for the actions that never reach the bus.
func busless(t *testing.T) (*Server, *Conversations) {
	t.Helper()
	convs := NewConversations()
	convs.now = fakeClock()
	return mustServer(t, ManageSessionTool(nil, convs)), convs
}

func mustSucceed(t *testing.T, res CallToolResult) string {
	t.Helper()
	if res.IsError {
		t.Fatalf("call failed: %s", res.Content[0].Text)
	}
	return res.Content[0].Text
}

func mustFail(t *testing.T, res CallToolResult, want ...string) {
	t.Helper()
	if !res.IsError {
		t.Fatalf("call succeeded: %s", res.Content[0].Text)
	}
	mustContain(t, res.Content[0].Text, want...)
}

// tableRow is the line of a list naming alias.
func tableRow(t *testing.T, list, alias string) string {
	t.Helper()
	for _, l := range strings.Split(list, "\n") {
		if strings.HasPrefix(l, "| `"+alias+"` |") {
			return l
		}
	}
	t.Fatalf("no row for %q in:\n%s", alias, list)
	return ""
}

// A send on a name counts as its use: list puts it first.
func TestManageSessionListsMostRecentlyUsedFirst(t *testing.T) {
	s, convs, _ := sessionServer(t)
	for _, a := range []string{"alpha", "bravo", "charlie"} {
		mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": a, "description": a + " work"}))
	}
	mustSucceed(t, callTool(t, s, ToolSendMessage, map[string]any{"text": "hi", "conversation": "alpha", "wait_seconds": 10}))
	list := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionList}))
	a, c, b := strings.Index(list, "`alpha`"), strings.Index(list, "`charlie`"), strings.Index(list, "`bravo`")
	if a < 0 || b < 0 || c < 0 || !(a < c && c < b) {
		t.Errorf("want alpha, charlie, bravo in that order:\n%s", list)
	}
	token, _ := convs.Resolve("bravo", "")
	row := tableRow(t, list, "bravo")
	// bravo was the second entry stamped: two minutes past the epoch.
	mustContain(t, row, "`console:"+token+"`", "`bravo work`", time.Unix(fakeEpoch, 0).Add(2*time.Minute).UTC().Format(time.RFC3339))
	mustContain(t, list, "this server's memory only", "a restart forgets", "resumes by its token")
}

func TestManageSessionListIsCapped(t *testing.T) {
	s, convs := busless(t)
	extra := 5
	for i := 0; i < maxConversationsShown+extra; i++ {
		if _, err := convs.Create(fmt.Sprintf("c%02d", i), ""); err != nil {
			t.Fatal(err)
		}
	}
	list := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionList}))
	if got := strings.Count(list, "| `console:"); got != maxConversationsShown {
		t.Errorf("list shows %d rows, want %d", got, maxConversationsShown)
	}
	mustContain(t, list, fmt.Sprintf("(+%d older omitted)", extra), "`c54`")
	mustNotContain(t, list, "`c00`", "`c04`")

	empty, _ := busless(t)
	mustContain(t, mustSucceed(t, callTool(t, empty, ToolManageSession, map[string]any{"action": actionList})), "No conversations yet")
}

// A name and a description are the caller's: shown as one inert cell each,
// whatever they hold, in list and in new's answer.
func TestManageSessionRendersCallerTextInert(t *testing.T) {
	s, _ := busless(t)
	alias := "x`| **bold** |\n| forged | row |\u202egnp.exe"
	desc := "see [this](https://evil.example) `code` | pipe\r\nnext line\u200b\u2066"
	created := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": alias, "description": desc}))
	list := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionList}))
	for name, out := range map[string]string{"new": created, "list": list} {
		for _, bad := range []string{"\u202e", "\u200b", "\u2066", "\r", "x`|", "| forged |", "`code`"} {
			if strings.Contains(out, bad) {
				t.Errorf("%s output contains %q:\n%s", name, bad, out)
			}
		}
	}
	var row string
	for _, l := range strings.Split(list, "\n") {
		if strings.Contains(l, "console:") {
			row = l
		}
	}
	// Four cells: five pipes; three code spans and a timestamp: six backticks.
	if strings.Count(row, "|") != 5 || strings.Count(row, "`") != 6 {
		t.Errorf("hostile text broke the row's cells: %q", row)
	}
	if strings.Count(list, "console:") != 1 {
		t.Errorf("hostile text forged a row:\n%s", list)
	}
}

func TestManageSessionNewRefusesADuplicateName(t *testing.T) {
	s, convs := busless(t)
	mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": "ops"}))
	first, _ := convs.Resolve("ops", "")
	mustFail(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": "ops"}), "already exists", "`ops`")
	if again, _ := convs.Resolve("ops", ""); again != first {
		t.Error("a refused new replaced the existing conversation's token")
	}
	// A name minted by a send is in use too.
	if _, err := convs.Resolve("used", ""); err != nil {
		t.Fatal(err)
	}
	mustFail(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": "used"}), "already exists")
}

// The cap counts characters, not bytes, and a longer description is refused
// whole, creating nothing.
func TestManageSessionNewCapsTheDescription(t *testing.T) {
	s, convs := busless(t)
	mustFail(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": "long",
		"description": strings.Repeat("é", maxDescriptionRunes+1)}), "description is")
	if len(convs.List()) != 0 {
		t.Error("a refused new created a conversation")
	}
	mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew, "conversation": "ok",
		"description": strings.Repeat("é", maxDescriptionRunes)}))
}

func TestManageSessionNewMintsADefaultName(t *testing.T) {
	s, convs := busless(t)
	convs.now = func() time.Time { return time.Unix(fakeEpoch, 0) }
	first := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew}))
	second := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionNew}))
	base := fmt.Sprintf("session-%d", fakeEpoch)
	mustContain(t, first, "`"+base+"`", "token `"+conversationTokenPrefix)
	mustContain(t, second, "`"+base+"-2`")
	if len(convs.List()) != 2 {
		t.Errorf("two news made %d conversations", len(convs.List()))
	}
}

// delete forgets the name only: the next send under it is a new conversation.
func TestManageSessionDeleteForgetsTheName(t *testing.T) {
	s, convs, _ := sessionServer(t)
	mustSucceed(t, callTool(t, s, ToolSendMessage, map[string]any{"text": "hi", "conversation": "ops", "wait_seconds": 10}))
	before, _ := convs.Resolve("ops", "")
	out := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionDelete, "conversation": "ops"}))
	mustContain(t, out, "only forgets the name", "history stays on the bus", "session_off")
	mustContain(t, mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionList})), "No conversations yet")
	res := mustSucceed(t, callTool(t, s, ToolSendMessage, map[string]any{"text": "hi again", "conversation": "ops", "wait_seconds": 10}))
	if strings.Contains(res, before) {
		t.Errorf("a send after delete reused the forgotten token %s:\n%s", before, res)
	}
	mustFail(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionDelete, "conversation": "nope"}), "no conversation named `nope`")
}

// Each control action sends exactly its console text on the named
// conversation, and answers with what the gateway said.
func TestManageSessionControlActionsSendTheirText(t *testing.T) {
	s, convs, frames := sessionServer(t)
	token, _ := convs.Resolve("ops", "")
	for action, want := range map[string]struct {
		text, answer string
	}{
		actionSessionOn:  {sessionOnText, "Session started."},
		actionSessionOff: {sessionOffText, "Session ended."},
		actionStatus:     {statusText, "Nothing is running"},
		actionHelp:       {helpText, answerText},
	} {
		t.Run(action, func(t *testing.T) {
			n := len(frames.all())
			out := mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": action, "conversation": "ops"}))
			mustContain(t, out, want.answer, "token `"+token+"`")
			got := frames.all()[n:]
			if len(got) != 1 || got[0] != (sentFrame{token, want.text}) {
				t.Errorf("frames sent = %+v, want one %q on %s", got, want.text, token)
			}
		})
	}
	// By token, in either form, the same conversation.
	for _, byToken := range []string{token, consoleConversationPrefix + token} {
		n := len(frames.all())
		mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": actionSessionOff, "conversation_token": byToken}))
		if got := frames.all()[n:]; len(got) != 1 || got[0].token != token {
			t.Errorf("session_off by token %q sent %+v", byToken, got)
		}
	}
}

// The original client's spellings still work: no action lists, start and end
// are session_on and session_off, remove is delete, prune is answered as the
// dropped clear_killed, and alias names the conversation.
func TestManageSessionTakesTheOriginalSpellings(t *testing.T) {
	s, convs, frames := sessionServer(t)
	token, _ := convs.Resolve("ops", "")
	mustContain(t, mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{})), "`ops`")
	for action, text := range map[string]string{"start": sessionOnText, "END": sessionOffText} {
		n := len(frames.all())
		mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": action, "alias": "ops"}))
		if got := frames.all()[n:]; len(got) != 1 || got[0] != (sentFrame{token, text}) {
			t.Errorf("%s sent %+v, want one %q on %s", action, got, text, token)
		}
	}
	mustSucceed(t, callTool(t, s, ToolManageSession, map[string]any{"action": "remove", "alias": "ops"}))
	if again, _ := convs.Resolve("ops", ""); again == token {
		t.Error("remove did not forget the name")
	}
	mustFail(t, callTool(t, s, ToolManageSession, map[string]any{"action": "prune"}), "cannot see session pods")
}

// The original client's dropped actions are answered by name, not as unknown.
func TestManageSessionAnswersDroppedActions(t *testing.T) {
	s, _ := busless(t)
	for action, want := range map[string]string{
		"switch":       "not available in the in-cluster server: there is no active conversation; name it with `conversation` on each call",
		"debug_on":     "not available in the in-cluster server yet: per-conversation formatting comes with a later change",
		"debug_off":    "not available in the in-cluster server yet: per-conversation formatting comes with a later change",
		"transcript":   "not available in the in-cluster server yet: reading a conversation's history needs a cheaper bus read than a scan of every task; use task_control get for a task",
		"clear_killed": "not available in the in-cluster server: it cannot see session pods",
	} {
		res := callTool(t, s, ToolManageSession, map[string]any{"action": action, "conversation": "ops"})
		mustFail(t, res, want)
		mustNotContain(t, res.Content[0].Text, "unknown action")
	}
	// And they are not offered.
	for _, tool := range s.tools {
		if tool.Definition.Name != ToolManageSession {
			continue
		}
		enum := tool.Definition.InputSchema["properties"].(map[string]any)["action"].(map[string]any)["enum"].([]string)
		for _, a := range enum {
			if _, dropped := droppedActions[a]; dropped {
				t.Errorf("the schema offers dropped action %s", a)
			}
		}
	}
}

// Bad input is refused before anything reaches the bus.
func TestManageSessionRefusesBadInput(t *testing.T) {
	s, _, frames := sessionServer(t)
	for name, args := range map[string]map[string]any{
		"a name past the cap":     {"action": actionNew, "conversation": strings.Repeat("n", maxAliasRunes+1)},
		"an unknown action":       {"action": "steer"},
		"new with a token":        {"action": actionNew, "conversation": "x", "conversation_token": "mcpabc"},
		"delete without a name":   {"action": actionDelete},
		"delete by token alone":   {"action": actionDelete, "conversation_token": "mcpabc"},
		"a wildcard token":        {"action": actionSessionOn, "conversation_token": "*"},
		"a dotted token":          {"action": actionStatus, "conversation_token": "a.b"},
		"a doubly prefixed token": {"action": actionHelp, "conversation_token": "console:console:mcpabc"},
	} {
		start := time.Now()
		res := callTool(t, s, ToolManageSession, args)
		if !res.IsError {
			t.Errorf("%s was accepted: %+v", name, res)
		}
		if elapsed := time.Since(start); elapsed > time.Second {
			t.Errorf("%s took %s to refuse; it must be refused before the bus", name, elapsed)
		}
	}
	if got := frames.all(); len(got) != 0 {
		t.Errorf("a refused call reached the gateway: %+v", got)
	}
}
