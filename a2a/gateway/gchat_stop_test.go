package gateway

import (
	"errors"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// stopButtons reads the buttons of a create or patch call's body: their
// labels, and the function, conversation and task of the last one.
func stopButtons(t *testing.T, c relayCall) (labels []string, function, conversation, task string) {
	t.Helper()
	body := c.arguments["body"].(map[string]any)
	widgets, _ := body["accessoryWidgets"].([]any)
	if len(widgets) == 0 {
		return nil, "", "", ""
	}
	buttons := widgets[0].(map[string]any)["buttonList"].(map[string]any)["buttons"].([]any)
	for _, b := range buttons {
		button := b.(map[string]any)
		labels = append(labels, button["text"].(string))
		action, _ := button["onClick"].(map[string]any)["action"].(map[string]any)
		if action == nil {
			continue
		}
		function, _ = action["function"].(string)
		conversation, task = "", ""
		for _, p := range action["parameters"].([]any) {
			switch param := p.(map[string]any); param["key"] {
			case gchatStopParamConversation:
				conversation, _ = param["value"].(string)
			case gchatStopParamTask:
				task, _ = param["value"].(string)
			}
		}
	}
	return labels, function, conversation, task
}

// A running task's line posts with a Stop button under it, bound to the
// conversation it was posted in; its live edits keep the button; the
// terminal edit, without Stop, takes it off.
func TestGchatStopLineCarriesTheButtonUntilTheTerminal(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/S1/messages/M1"}
	a := newTestGchatAdapterWithRelay(t, f)
	conv := "gchat:dm/spaces/S1"

	id, err := a.PostMessage(conv, OutMessage{Text: "⏳ submitted…", Stop: "task-1"})
	if err != nil || id != "spaces/S1/messages/M1" {
		t.Fatalf("post: id=%q err=%v", id, err)
	}
	c := f.call(0)
	if c.method != "create" || c.arguments["body"].(map[string]any)["text"] != "⏳ submitted…" {
		t.Fatalf("create = %+v", c)
	}
	if labels, fn, bound, task := stopButtons(t, c); len(labels) != 1 || labels[0] != gchatStopText || fn != gchatStopFunction || bound != conv || task != "task-1" {
		t.Errorf("posted buttons = %v %s %s %s", labels, fn, bound, task)
	}

	if err := a.EditMessage(conv, id, OutMessage{Text: "⚙️ *working*", Stop: "task-1"}); err != nil {
		t.Fatal(err)
	}
	if labels, fn, bound, task := stopButtons(t, f.call(1)); len(labels) != 1 || fn != gchatStopFunction || bound != conv || task != "task-1" {
		t.Errorf("live edit buttons = %v %s %s %s", labels, fn, bound, task)
	}

	if err := a.EditMessage(conv, id, OutMessage{Text: "✅ *completed*"}); err != nil {
		t.Fatal(err)
	}
	c = f.call(2)
	if labels, _, _, _ := stopButtons(t, c); len(labels) != 0 || c.arguments["updateMask"] != gchatLayoutUpdateMask {
		t.Errorf("terminal edit kept buttons: %v (%+v)", labels, c)
	}
	if err := a.Edit(conv, id, "✅ *completed*"); err != nil {
		t.Fatal(err)
	}
	if labels, _, _, _ := stopButtons(t, f.call(3)); len(labels) != 0 {
		t.Errorf("Edit kept buttons: %v", labels)
	}
}

// A laid-out line (a progress message with a link) takes the Stop button
// after its own buttons, and a text edit of it takes both off.
func TestGchatStopButtonFollowsALaidOutLinesButtons(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/S1/messages/M1"}
	a := newTestGchatAdapterWithRelay(t, f)
	conv := "gchat:spaces/S1/threads/T1"
	plan := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Upgrade readiness",
		Plan:  &chatmsg.Plan{Title: "Upgrade readiness", Items: []chatmsg.PlanItem{{ID: "t-1", Title: "Read versions", Status: chatmsg.StatusInProgress}}},
		Links: []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}}}
	if err := a.EditMessage(conv, "spaces/S1/messages/M1", OutMessage{Text: "⚙️ working", Chat: plan, Stop: "task-1"}); err != nil {
		t.Fatal(err)
	}
	if labels, fn, bound, task := stopButtons(t, f.call(0)); strings.Join(labels, ",") != "board,"+gchatStopText || fn != gchatStopFunction || bound != conv || task != "task-1" {
		t.Errorf("laid-out buttons = %v %s %s %s", labels, fn, bound, task)
	}
	if err := a.EditMessage(conv, "spaces/S1/messages/M1", OutMessage{Text: "✅ completed", Chat: plan}); err != nil {
		t.Fatal(err)
	}
	if labels, _, _, _ := stopButtons(t, f.call(1)); strings.Join(labels, ",") != "board" {
		t.Errorf("terminal laid-out buttons = %v", labels)
	}
}

// A line Chat refuses with the button is posted, or patched, once more
// without it: the line never fails for the button. Any other failure is
// returned as it is.
func TestGchatStopButtonRefusedFallsBackToTheLine(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/S1/messages/M1"}
	f.responses["spaces.messages/patch"] = map[string]any{"name": "spaces/S1/messages/M1"}
	f.queue["spaces.messages/create"] = append(f.queue["spaces.messages/create"], fakeChatAPIError{status: 400, reason: "accessory widgets"})
	f.queue["spaces.messages/patch"] = append(f.queue["spaces.messages/patch"], fakeChatAPIError{status: 400, reason: "accessory widgets"})
	a := newTestGchatAdapterWithRelay(t, f)
	conv := "gchat:spaces/S1/threads/T1"

	id, err := a.PostMessage(conv, OutMessage{Text: "⏳ submitted…", Stop: "task-1"})
	if err != nil || id != "spaces/S1/messages/M1" {
		t.Fatalf("post: id=%q err=%v", id, err)
	}
	if labels, _, _, _ := stopButtons(t, f.call(0)); len(labels) != 1 {
		t.Errorf("first create buttons = %v", labels)
	}
	c := f.call(1)
	if c.method != "create" || c.arguments["body"].(map[string]any)["text"] != "⏳ submitted…" {
		t.Errorf("fallback create = %+v", c)
	}
	if _, has := c.arguments["body"].(map[string]any)["accessoryWidgets"]; has {
		t.Errorf("fallback create carries widgets: %+v", c)
	}

	if err := a.EditMessage(conv, id, OutMessage{Text: "⚙️ working", Stop: "task-1"}); err != nil {
		t.Fatal(err)
	}
	if labels, _, _, _ := stopButtons(t, f.call(2)); len(labels) != 1 {
		t.Errorf("first patch buttons = %v", labels)
	}
	if labels, _, _, _ := stopButtons(t, f.call(3)); len(labels) != 0 || f.call(3).method != "patch" {
		t.Errorf("fallback patch = %v %+v", labels, f.call(3))
	}

	f.queue["spaces.messages/create"] = append(f.queue["spaces.messages/create"], fakeChatAPIError{status: 500, reason: "backend"})
	if _, err := a.PostMessage(conv, OutMessage{Text: "⏳ submitted…", Stop: "task-1"}); err == nil || len(f.calls) != 5 {
		t.Errorf("a failed create: err=%v calls=%d", err, len(f.calls))
	}

	// A laid-out line refused is the layout's refusal: one patch, the
	// refusal returned, no retry without the button.
	f.queue["spaces.messages/patch"] = append(f.queue["spaces.messages/patch"], fakeChatAPIError{status: 400, reason: "bad link"})
	plan := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "Upgrade readiness", Links: []chatmsg.Link{{Text: "board", URL: "https://x.example/b"}}}
	if err := a.EditMessage(conv, id, OutMessage{Text: "⚙️ working", Chat: plan, Stop: "task-1"}); !errors.Is(err, ErrLayoutRefused) || len(f.calls) != 6 {
		t.Errorf("a refused laid-out line: err=%v calls=%d", err, len(f.calls))
	}
}

// A click on the Stop button is a cancel of the task the button names:
// IntentCancel with no text, from the clicker, bound to the conversation the
// button carries. A redelivery is dropped; a button naming no conversation, or
// one in another space, is dropped; a clicker with no email is dropped.
func TestGchatStopClickIsACancel(t *testing.T) {
	a := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	stopClick := func() *gchatEvent {
		ev := gchatClickEvent(t)
		ev.click.Function = gchatStopFunction
		ev.click.Parameters = map[string]string{gchatStopParamConversation: "gchat:dm/spaces/_c2fixtureAA", gchatStopParamTask: "task-9"}
		return ev
	}
	ev := stopClick()
	msg, reason := a.classify(ev)
	if reason != "" {
		t.Fatalf("stop click dropped: %s", reason)
	}
	if msg.Intent != IntentCancel || msg.Text != "" || msg.Conversation != "gchat:dm/spaces/_c2fixtureAA" || msg.Kind != sessionKindDM ||
		msg.AuthorID != "sender@example.com" || msg.MessageID != gchatClickID(ev.Message.Name, ev.click.EventTime) || !msg.Click || msg.TaskID != "task-9" ||
		msg.Viewer != "users/100000000000000000042" {
		t.Errorf("stop turn = %+v", msg)
	}
	// A clicker Chat names by no user name is no viewer: nothing private
	// is addressed to them.
	nameless := stopClick()
	nameless.click.User.Name = "people/42"
	if msg, reason := a.classify(nameless); reason != "" || msg.Viewer != "" || msg.AuthorID != "sender@example.com" {
		t.Errorf("a nameless clicker's stop = %+v (%s)", msg, reason)
	}
	if _, reason := a.classify(ev); !strings.HasPrefix(reason, gchatStopDuplicate) {
		t.Errorf("a redelivered stop click: %q", reason)
	}
	if msg, reason := a.classify(stopClick()); reason != "" || msg.Intent != IntentCancel {
		t.Errorf("a second press at a new time: %+v (%s)", msg, reason)
	}
	// The clicker spoke, in the roster's sense.
	a.mu.Lock()
	known := a.userIDs["users/100000000000000000042"]
	a.mu.Unlock()
	if known != "sender@example.com" {
		t.Errorf("clicker not remembered: %q", known)
	}

	space := stopClick()
	space.click.Parameters[gchatStopParamConversation] = "gchat:spaces/_c2fixtureAA/threads/T1"
	if msg, reason := a.classify(space); reason != "" || msg.Kind != sessionKindGroup || msg.Conversation != "gchat:spaces/_c2fixtureAA/threads/T1" {
		t.Errorf("a thread's stop = %+v (%s)", msg, reason)
	}

	for _, tc := range []struct {
		name, want string
		edit       func(ev *gchatEvent)
	}{
		{"no conversation", gchatStopNoConversation, func(ev *gchatEvent) { delete(ev.click.Parameters, gchatStopParamConversation) }},
		{"not a conversation", gchatStopNoConversation, func(ev *gchatEvent) { ev.click.Parameters[gchatStopParamConversation] = "slack:C1/1.2" }},
		{"another space", gchatStopOtherSpace, func(ev *gchatEvent) { ev.click.Parameters[gchatStopParamConversation] = "gchat:dm/spaces/OTHER" }},
		{"no task", gchatStopNoTask, func(ev *gchatEvent) { delete(ev.click.Parameters, gchatStopParamTask) }},
		{"no email", gchatStopNoEmail, func(ev *gchatEvent) { ev.click.User.Email = "" }},
		{"no message", gchatStopNoMessage, func(ev *gchatEvent) { ev.Message.Name = "" }},
	} {
		ev := stopClick()
		tc.edit(ev)
		if _, reason := a.classify(ev); !strings.HasPrefix(reason, tc.want) {
			t.Errorf("%s: %q", tc.name, reason)
		}
	}
	// No event time, no id of its own: not deduplicated.
	for range 2 {
		ev := stopClick()
		ev.click.EventTime = ""
		if msg, reason := a.classify(ev); reason != "" || msg.MessageID != ev.Message.Name {
			t.Errorf("a stop click without a time: %+v (%s)", msg, reason)
		}
	}
}

// PostPrivate is one create with the viewer set, in the thread, as the note
// card is posted: text alone, no widgets. A viewer that is not a user's name
// is refused with no call. A private thread reply Chat refuses is posted
// once more at the top level, still private; any other failure is returned
// as it is, once.
func TestGchatPostPrivateIsACreateTheViewerAloneSees(t *testing.T) {
	f := newFakeChatRelay(t)
	f.responses["spaces.messages/create"] = map[string]any{"name": "spaces/AAA/messages/P1"}
	a := newTestGchatAdapterWithRelay(t, f)
	conv := "gchat:spaces/AAA/threads/T1"
	if err := a.PostPrivate(conv, "users/42", refusedPressNotice); err != nil {
		t.Fatal(err)
	}
	c := f.call(0)
	body := callBody(t, c)
	if c.method != "create" || c.arguments["parent"] != "spaces/AAA" || c.arguments["messageReplyOption"] == nil {
		t.Fatalf("call 0 = %+v", c)
	}
	if v, _ := body["privateMessageViewer"].(map[string]any); v["name"] != "users/42" {
		t.Errorf("privateMessageViewer = %+v", body["privateMessageViewer"])
	}
	if th, _ := body["thread"].(map[string]any); th["name"] != "spaces/AAA/threads/T1" {
		t.Errorf("thread = %+v", body["thread"])
	}
	if body["text"] != refusedPressNotice || body["accessoryWidgets"] != nil || body["cardsV2"] != nil {
		t.Errorf("body = %+v", body)
	}

	// A group chat has no thread: the line posts at the top level.
	if err := a.PostPrivate("gchat:space/spaces/G1", "users/42", refusedPressNotice); err != nil {
		t.Fatal(err)
	}
	if g := f.call(1); g.arguments["parent"] != "spaces/G1" || g.arguments["messageReplyOption"] != nil || callBody(t, g)["thread"] != nil {
		t.Errorf("a group chat's private post = %+v", g)
	}
	f.calls = f.calls[:1]

	if err := a.PostPrivate(conv, "people/42", "x"); !errors.Is(err, errGchatNoViewer) || relayCallCount(f) != 1 {
		t.Errorf("a viewer that is not a user: err=%v calls=%d", err, relayCallCount(f))
	}
	if err := a.PostPrivate("slack:C1/1.0", "users/42", "x"); err == nil || relayCallCount(f) != 1 {
		t.Errorf("not a gchat conversation: err=%v calls=%d", err, relayCallCount(f))
	}

	f.queue["spaces.messages/create"] = append(f.queue["spaces.messages/create"], fakeChatAPIError{status: 400, reason: "private replies unsupported"})
	if err := a.PostPrivate(conv, "users/42", "x"); err != nil || relayCallCount(f) != 3 {
		t.Fatalf("a refused thread reply: err=%v calls=%d", err, relayCallCount(f))
	}
	top := callBody(t, f.call(2))
	if v, _ := top["privateMessageViewer"].(map[string]any); v["name"] != "users/42" || top["thread"] != nil || f.call(2).arguments["messageReplyOption"] != nil {
		t.Errorf("the top-level retry = %+v %+v", f.call(2).arguments, top)
	}

	f.queue["spaces.messages/create"] = append(f.queue["spaces.messages/create"], fakeChatAPIError{status: 429, reason: "slow down"})
	if err := a.PostPrivate(conv, "users/42", "x"); err == nil || relayCallCount(f) != 4 {
		t.Errorf("a rate limit: err=%v calls=%d; want the error returned, no retry", err, relayCallCount(f))
	}
}
