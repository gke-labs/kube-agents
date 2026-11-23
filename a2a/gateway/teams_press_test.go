package gateway

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// teamsLineActions is the actions of the card the nth passthrough call
// carried, nil when the call carried no card or the card no actions.
func teamsLineActions(t *testing.T, relay *fakeTeamsRelay, n int) []any {
	t.Helper()
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if n >= len(relay.apiCall) {
		t.Fatalf("api calls %d, want more than %d", len(relay.apiCall), n)
	}
	payload, _ := relay.apiCall[n]["payload"].(map[string]any)
	attachments, _ := payload["attachments"].([]any)
	if len(attachments) == 0 {
		return nil
	}
	content, _ := attachments[0].(map[string]any)["content"].(map[string]any)
	actions, _ := content[teamsCardActionsKey].([]any)
	return actions
}

// A running task's line carries the Stop button on its card, naming the
// task; an edit that names no task takes the button off, and the whole
// answer edited onto the line has none.
func TestTeamsDrawsTheStopButtonOnARunningTasksLine(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	conv := "teams:" + testTeamsChannel
	id, err := a.PostMessage(conv, OutMessage{Text: "⏳ submitted…", Stop: "task-1"})
	if err != nil || id == "" {
		t.Fatalf("post: %v %q", err, id)
	}
	if err := a.EditMessage(conv, id, OutMessage{Text: "⚙️ **working**", Stop: "task-1"}); err != nil {
		t.Fatal(err)
	}
	if err := a.EditMessage(conv, id, OutMessage{Text: "🛑 cancel sent"}); err != nil {
		t.Fatal(err)
	}
	for n, want := range []bool{true, true, false} {
		actions := teamsLineActions(t, relay, n)
		if !want {
			if len(actions) != 0 {
				t.Errorf("call %d: actions %v, want none", n, actions)
			}
			continue
		}
		if len(actions) != 1 {
			t.Fatalf("call %d: actions %v, want the one Stop button", n, actions)
		}
		button := actions[0].(map[string]any)
		data, _ := button["data"].(map[string]any)
		if button["type"] != teamsActionSubmit || button["title"] != teamsStopTitle ||
			data[teamsPressActionKey] != teamsPressActionStop || data[teamsPressTaskKey] != "task-1" {
			t.Errorf("call %d: button %v", n, button)
		}
	}
}

// With cards off the line is plain text, which carries no button: a typed
// stop is the way to stop there.
func TestTeamsPlainTextLineCarriesNoStopButton(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	a.adaptiveCards = false
	if _, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "⏳ submitted…", Stop: "task-1"}); err != nil {
		t.Fatal(err)
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	payload := relay.apiCall[0]["payload"].(map[string]any)
	if payload["attachments"] != nil || payload["text"] != "⏳ submitted…" {
		t.Fatalf("payload %v, want the plain line", payload)
	}
}

// testTeamsPress is the activity Teams sends for a press of a button on the
// card of message: a message with no text, the button's data as its value.
func testTeamsPress(id, message string, data map[string]any) map[string]any {
	act := testTeamsActivity(id, "")
	delete(act, "entities")
	act["replyToId"] = message
	act["value"] = data
	return act
}

// A press of the Stop button is the cancel the gateway reads as a press: the
// task, marked a click, the pressed message in its id. A press that names no
// task or no message, a redelivery, another button and a value of another
// shape are not turns.
func TestTeamsClassifiesAStopPress(t *testing.T) {
	a := newTestTeamsAdapter(t, "http://relay.invalid", testTeamsTenant)
	stop := map[string]any{teamsPressActionKey: teamsPressActionStop, teamsPressTaskKey: "task-1"}
	msg, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:20", "1794603936403", stop)))
	if reason != "" {
		t.Fatalf("a stop press was not a turn: %s", reason)
	}
	if msg.Intent != IntentCancel || !msg.Click || msg.TaskID != "task-1" || msg.Text != "" ||
		msg.AuthorID != testTeamsUser || msg.Conversation != "teams:"+testTeamsChannel ||
		gchatClickMessage(msg.MessageID) != "1794603936403" || msg.MessageID == "1794603936403" {
		t.Fatalf("press: %+v", msg)
	}
	if !isStopPress(msg, teamsBackend) {
		t.Fatal("the gate does not read the press as a Stop press")
	}
	for name, act := range map[string]map[string]any{
		"a redelivery":   testTeamsPress("f:20", "1794603936403", stop),
		"no task":        testTeamsPress("f:21", "1794603936403", map[string]any{teamsPressActionKey: teamsPressActionStop}),
		"no message":     testTeamsPress("f:22", "", stop),
		"another button": testTeamsPress("f:23", "1794603936403", map[string]any{teamsPressActionKey: "rate", teamsPressTaskKey: "task-1"}),
		"another shape":  testTeamsPress("f:24", "1794603936403", map[string]any{"choice": "yes"}),
	} {
		if msg, reason := a.classify(decodeTeamsActivity(t, act)); reason == "" {
			t.Errorf("%s was a turn: %+v", name, msg)
		}
	}
	// A value that is not an object does not fail the decode: the message's
	// text is still a turn.
	typed := testTeamsActivity("f:25", "<at>Kage</at> hi")
	typed["value"] = "a string"
	raw, err := json.Marshal(typed)
	if err != nil {
		t.Fatal(err)
	}
	var act teamsActivity
	if err := json.Unmarshal(raw, &act); err != nil {
		t.Fatalf("a string value failed the decode: %v", err)
	}
	if msg, reason := a.classify(&act); reason != "" || msg.Text != "hi" {
		t.Fatalf("typed message with a value: %+v %q", msg, reason)
	}
}

const (
	teamsGateRequester = "05e62547-5ee7-41be-9639-155d9212be8f"
	teamsGateOther     = "0a1b2c3d-0000-4000-8000-000000000002"
	teamsGateChannel   = "teams:19:gate@thread.tacv2;messageid=1794603936000"
	teamsGateDM        = "teams:a:1gateDM"
)

// teamsGateRig is a rig on the Teams backend with two members admitted and
// a task of the requester's running with its working line, whose message id
// it returns.
func teamsGateRig(t *testing.T, conv, kind string) (*rig, *lib.Envelope, string) {
	t.Helper()
	r := startRigOnBackend(t, teamsBackend, nil, func(cfg *Config) {
		cfg.TeamsAllowedUsers = []string{teamsGateRequester, teamsGateOther}
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: kind, AuthorID: teamsGateRequester, MessageID: "1794603936001", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	return r, origin, r.adapter.stopLinesCopy()[0].MessageID
}

func pressTeamsStop(r *rig, conv, kind, who, id, task string) {
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: kind, AuthorID: who, MessageID: id, Intent: IntentCancel, TaskID: task, Click: true}
}

// teamsSteerAndWait is the sync point after a press that changes nothing:
// who's follow-up is handled after every message sent before it.
func teamsSteerAndWait(t *testing.T, r *rig, conv, kind, who, id string, origin *lib.Envelope) {
	t.Helper()
	before := 0
	if rec, err := r.g.reg.Get(context.Background(), conv); err == nil && rec != nil {
		if ref, ok := rec.TaskRefFor(origin.TaskID); ok {
			before = len(ref.SteerAuthors)
		}
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: kind, AuthorID: who, MessageID: id, Text: "and the node pools too"}
	waitForRecord(t, r, conv, "the steer author recorded", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && (len(ref.SteerAuthors) > before || ref.Canceled)
	})
}

// The requester's press on Teams stops the task and answers on the pressed
// line, as Google Chat's does: the line closes on "cancel sent" with its
// button gone, and nothing is posted.
func TestATeamsStopPressFromTheRequesterClosesTheLine(t *testing.T) {
	t.Parallel()
	r, origin, line := teamsGateRig(t, teamsGateChannel, sessionKindGroup)
	posts := len(r.adapter.postTexts())

	pressTeamsStop(r, teamsGateChannel, sessionKindGroup, teamsGateRequester, line+"#f:30", origin.TaskID)
	waitFor(t, "the press to close the line", func() bool { return lineClosedOnCancelSent(r) })
	rec := waitForRecord(t, r, teamsGateChannel, "the task stopping", func(rec *SessionRecord) bool {
		return rec.ActiveTask == nil || rec.ActiveTask.Detached
	})
	if rec.ActiveTask != nil && !rec.ActiveTask.LineClosed {
		t.Fatalf("the line was not closed: %+v", rec.ActiveTask)
	}
	for _, p := range r.adapter.postTexts()[posts:] {
		if strings.Contains(p, "cancel") {
			t.Errorf("the press posted %q; want the answer on the line alone", p)
		}
	}
}

// Another member's press on Teams cancels nothing and is answered on the
// pressed line, button kept, since Teams has no post only the presser sees;
// nothing is posted. The requester's press after it still stops the task.
func TestATeamsStopPressFromAnotherMemberIsSaidOnTheLine(t *testing.T) {
	t.Parallel()
	r, origin, line := teamsGateRig(t, teamsGateChannel, sessionKindGroup)

	pressTeamsStop(r, teamsGateChannel, sessionKindGroup, teamsGateOther, line+"#f:31", origin.TaskID)
	teamsSteerAndWait(t, r, teamsGateChannel, sessionKindGroup, teamsGateOther, "1794603936002", origin)

	rec := waitForRecord(t, r, teamsGateChannel, "the record", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })
	if rec.ActiveTask.Detached || rec.ActiveTask.LineClosed {
		t.Fatalf("another member's press cancelled the task: %+v", rec.ActiveTask)
	}
	want := fakePost{teamsGateChannel, line, refusedPressLineMark + refusedPressNotice}
	if !slices.Contains(r.adapter.stopLinesCopy(), want) {
		t.Fatalf("the refusal was not said on the line with its button: %+v", r.adapter.stopLinesCopy())
	}
	if got := r.adapter.privatePosts(); len(got) != 0 {
		t.Fatalf("a private post on Teams: %+v", got)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, refusedPressNotice) {
			t.Fatalf("the refusal was posted: %q", p)
		}
	}

	pressTeamsStop(r, teamsGateChannel, sessionKindGroup, teamsGateRequester, line+"#f:32", origin.TaskID)
	waitFor(t, "the requester's press to close the line", func() bool { return lineClosedOnCancelSent(r) })
}

// In a direct message a refused press says nothing: the only other member
// is the one who asked.
func TestATeamsStopPressRefusedInADMSaysNothing(t *testing.T) {
	t.Parallel()
	r, origin, line := teamsGateRig(t, teamsGateDM, sessionKindDM)

	pressTeamsStop(r, teamsGateDM, sessionKindDM, teamsGateOther, line+"#f:33", origin.TaskID)
	teamsSteerAndWait(t, r, teamsGateDM, sessionKindDM, teamsGateOther, "1794603936003", origin)

	rec := waitForRecord(t, r, teamsGateDM, "the record", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })
	if rec.ActiveTask.Detached || rec.ActiveTask.LineClosed {
		t.Fatalf("the press cancelled the task: %+v", rec.ActiveTask)
	}
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, refusedPressNotice) {
			t.Fatalf("a refused press in a DM was answered: %q", e)
		}
	}
}
