package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// boardCard is the board-health card as board_health_card.chat_from_card
// sends it: the mock's two stuck rows, each with its card and both buttons.
func boardCard() *chatmsg.Message {
	return &chatmsg.Message{
		Schema:   chatmsg.Schema,
		Headline: "2 tasks are stuck and need someone to decide.",
		Rows: []chatmsg.Row{
			{Text: "**Investigate payments-api crash-loop on seeded-a**", Detail: "Stopped 3 days ago before finishing.",
				Tasks: []string{"t_7c2e91ab"}, Actions: []string{chatmsg.RowActionRetry, chatmsg.RowActionDrop}},
			{Text: "**Upgrade seeded-b node pool to 1.33**", Detail: "Paused 6 days ago.",
				Tasks: []string{"t_19d04f3c", "t_2"}, Actions: []string{chatmsg.RowActionRetry, chatmsg.RowActionDrop}},
		},
	}
}

func TestSlackRendersEachActingRowOverItsButtons(t *testing.T) {
	m := boardCard()
	if layout, _ := chatmsg.Validate(*m, chatmsg.Notify); layout != nil {
		t.Fatalf("the card is not valid: %v", layout)
	}
	layout, err := renderSlackChat(m, "fallback")
	if err != nil {
		t.Fatal(err)
	}
	var kinds, ids []string
	for _, b := range layout.Blocks {
		kind, _ := b["type"].(string)
		id, _ := b["block_id"].(string)
		kinds, ids = append(kinds, kind), append(ids, id)
	}
	wantKinds := []string{"rich_text", "divider", "rich_text", "actions", "rich_text", "actions", "divider"}
	wantIDs := []string{"", "", "kage_board.text.0", "kage_board.row.0", "kage_board.text.1", "kage_board.row.1", ""}
	if strings.Join(kinds, ",") != strings.Join(wantKinds, ",") || strings.Join(ids, ",") != strings.Join(wantIDs, ",") {
		t.Fatalf("blocks = %v %v, want %v %v", kinds, ids, wantKinds, wantIDs)
	}
	buttons, _ := layout.Blocks[5]["elements"].([]map[string]any)
	if len(buttons) != 2 {
		t.Fatalf("row 1 buttons = %v", layout.Blocks[5])
	}
	for i, want := range []struct{ label, id string }{{"Retry", "kage_board.retry"}, {"Drop", "kage_board.drop"}} {
		text, _ := buttons[i]["text"].(map[string]any)
		if text["text"] != want.label || buttons[i]["action_id"] != want.id || buttons[i]["value"] != "t_19d04f3c,t_2" {
			t.Errorf("button %d = %v", i, buttons[i])
		}
	}
}

func TestSlackRowsWithoutActionsStayOneBlock(t *testing.T) {
	m := &chatmsg.Message{Schema: chatmsg.Schema, Headline: "h", Rows: []chatmsg.Row{{Text: "a"}, {Text: "b"}}}
	layout, err := renderSlackChat(m, "fallback")
	if err != nil {
		t.Fatal(err)
	}
	if len(layout.Blocks) != 4 {
		t.Fatalf("blocks = %d, want head, divider, the rows' one block, divider", len(layout.Blocks))
	}
	if _, keyed := layout.Blocks[2]["block_id"]; keyed {
		t.Error("rows without actions are not keyed")
	}
}

// boardTap is a block_actions payload in Slack's documented shape (the
// click's action_ts inside the action) for a tap on row of the card the
// renderer drew.
func boardTap(t *testing.T, user, actionID string, row int, actionTS string) json.RawMessage {
	t.Helper()
	layout, err := renderSlackChat(boardCard(), "2 tasks are stuck")
	if err != nil {
		t.Fatal(err)
	}
	value := []string{"t_7c2e91ab", "t_19d04f3c,t_2"}[row]
	raw, err := json.Marshal(map[string]any{
		"type":      "block_actions",
		"user":      map[string]any{"id": user, "team_id": "T1"},
		"team":      map[string]any{"id": "T1"},
		"channel":   map[string]any{"id": "C1"},
		"container": map[string]any{"type": "message", "message_ts": "50.0", "channel_id": "C1"},
		"message":   map[string]any{"ts": "50.0", "text": "2 tasks are stuck", "blocks": layout.Blocks},
		"actions": []any{map[string]any{"type": "button", "action_id": actionID,
			"block_id": []string{"kage_board.row.0", "kage_board.row.1"}[row], "value": value, "action_ts": actionTS}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// fakeBoardActor records each BoardAction and answers with outcome, or err.
type fakeBoardActor struct {
	mu      sync.Mutex
	got     []lib.BoardAction
	outcome string
	err     error
}

func (f *fakeBoardActor) act(_ context.Context, a lib.BoardAction) (*lib.BoardActionResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.got = append(f.got, a)
	if f.err != nil {
		return nil, f.err
	}
	res := &lib.BoardActionResult{Action: a.Action}
	for _, id := range a.Tasks {
		res.Results = append(res.Results, lib.BoardActionOutcome{ID: id, Outcome: f.outcome})
	}
	return res, nil
}

func newBoardSlackAdapter() (*SlackAdapter, *fakeSlackAPI, *fakeBoardActor) {
	api := &fakeSlackAPI{team: "T1", users: map[string]*slack.User{
		"U1": {ID: "U1", TeamID: "T1", Profile: slack.UserProfile{DisplayName: "Jayanti"}},
	}}
	a := newTestSlackAdapter(api)
	a.teamID = "T1"
	a.syncSessions = true
	a.now = func() time.Time { return time.Unix(1761570000, 0) }
	a.SetHome(func(member string) (bool, bool) { return member == "U1", false }, nil)
	actor := &fakeBoardActor{outcome: lib.BoardOutcomeDone}
	a.SetBoardActor(actor.act)
	return a, api, actor
}

func updatedBlocks(t *testing.T, api *fakeSlackAPI, i int) []map[string]any {
	t.Helper()
	var blocks []map[string]any
	if err := json.Unmarshal([]byte(api.updatedBlocks[i]), &blocks); err != nil {
		t.Fatal(err)
	}
	return blocks
}

func blockIDs(blocks []map[string]any) []string {
	var ids []string
	for _, b := range blocks {
		id, _ := b["block_id"].(string)
		ids = append(ids, id)
	}
	return ids
}

func noteText(blocks []map[string]any, id string) string {
	for _, b := range blocks {
		if b["block_id"] == id {
			elems, _ := b["elements"].([]any)
			if len(elems) == 1 {
				e, _ := elems[0].(map[string]any)
				s, _ := e["text"].(string)
				return s
			}
		}
	}
	return ""
}

func TestSlackRetryTapUnblocksTheRowAndNamesWhoAndWhen(t *testing.T) {
	a, api, actor := newBoardSlackAdapter()
	if _, ok := a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "61.0")); ok {
		t.Fatal("a board tap is never a turn")
	}
	if len(actor.got) != 1 || actor.got[0].Action != "retry" || strings.Join(actor.got[0].Tasks, ",") != "t_7c2e91ab" ||
		actor.got[0].By != "Jayanti" {
		t.Fatalf("actions = %+v", actor.got)
	}
	if len(api.updated) != 1 || api.updated[0].channel != "C1" || api.updated[0].ts != "50.0" || api.updated[0].text != "2 tasks are stuck" {
		t.Fatalf("updated = %+v", api.updated)
	}
	blocks := updatedBlocks(t, api, 0)
	ids := strings.Join(blockIDs(blocks), ",")
	if want := ",,kage_board.text.0,kage_board.row.0.note,kage_board.text.1,kage_board.row.1,"; ids != want {
		t.Errorf("block ids = %s, want %s", ids, want)
	}
	if got, want := noteText(blocks, "kage_board.row.0.note"), "✓ Retrying. Jayanti, <!date^1761570000^{time}|13:00 UTC>"; got != want {
		t.Errorf("note = %q, want %q", got, want)
	}
}

func TestSlackDropTapActsOnEveryCardInTheRow(t *testing.T) {
	a, api, actor := newBoardSlackAdapter()
	a.interactive(boardTap(t, "U1", "kage_board.drop", 1, "61.0"))
	if len(actor.got) != 1 || actor.got[0].Action != "drop" || strings.Join(actor.got[0].Tasks, ",") != "t_19d04f3c,t_2" {
		t.Fatalf("actions = %+v", actor.got)
	}
	blocks := updatedBlocks(t, api, 0)
	if got := noteText(blocks, "kage_board.row.1.note"); got != "Dropped. Jayanti, <!date^1761570000^{time}|13:00 UTC>" {
		t.Errorf("note = %q", got)
	}
	if !strings.Contains(strings.Join(blockIDs(blocks), ","), "kage_board.row.0") {
		t.Error("the other row keeps its buttons")
	}
}

func TestSlackBoardTapOnCardsAlreadyMoved(t *testing.T) {
	a, api, actor := newBoardSlackAdapter()
	actor.outcome = lib.BoardOutcomeMoved
	a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "61.0"))
	blocks := updatedBlocks(t, api, 0)
	if got := noteText(blocks, "kage_board.row.0.note"); !strings.HasPrefix(got, "No longer stuck. Jayanti, ") {
		t.Errorf("note = %q", got)
	}
	if strings.Contains(strings.Join(blockIDs(blocks), ","), "kage_board.row.0,") {
		t.Error("a settled row loses its buttons")
	}
}

func TestSlackBoardTapThatDidNotLandKeepsTheButtonsAndCanBeTappedAgain(t *testing.T) {
	a, api, actor := newBoardSlackAdapter()
	actor.err = errors.New("nothing answers the board route")
	a.interactive(boardTap(t, "U1", "kage_board.drop", 0, "61.0"))
	blocks := updatedBlocks(t, api, 0)
	if got := strings.Join(blockIDs(blocks)[3:5], ","); got != "kage_board.row.0.note,kage_board.row.0" {
		t.Errorf("block ids = %v", blockIDs(blocks))
	}
	if got := noteText(blocks, "kage_board.row.0.note"); got != "Couldn't drop it just now. Try again." {
		t.Errorf("note = %q", got)
	}
	actor.err = nil
	a.interactive(boardTap(t, "U1", "kage_board.drop", 0, "62.0"))
	if len(actor.got) != 2 {
		t.Errorf("actions = %d, want the second tap to act", len(actor.got))
	}
}

// TestSlackSecondRowsTapKeepsTheFirstRowsEdit: each tap's payload holds the
// card as it was when tapped, so the second edit builds on the first one sent.
func TestSlackSecondRowsTapKeepsTheFirstRowsEdit(t *testing.T) {
	a, api, _ := newBoardSlackAdapter()
	a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "61.0"))
	a.interactive(boardTap(t, "U1", "kage_board.drop", 1, "62.0"))
	if len(api.updated) != 2 {
		t.Fatalf("updates = %d", len(api.updated))
	}
	ids := strings.Join(blockIDs(updatedBlocks(t, api, 1)), ",")
	if want := ",,kage_board.text.0,kage_board.row.0.note,kage_board.text.1,kage_board.row.1.note,"; ids != want {
		t.Errorf("block ids = %s, want %s", ids, want)
	}
}

// TestSlackBoardRowWhoseEditFailedCanBeTappedAgain: a tap that landed but
// could not edit its row (here, a message without the row) leaves the row
// open, so a second tap asks again.
func TestSlackBoardRowWhoseEditFailedCanBeTappedAgain(t *testing.T) {
	a, api, actor := newBoardSlackAdapter()
	withoutRow := func(raw json.RawMessage) json.RawMessage {
		var p map[string]any
		if err := json.Unmarshal(raw, &p); err != nil {
			t.Fatal(err)
		}
		p["message"].(map[string]any)["blocks"] = []any{}
		out, _ := json.Marshal(p)
		return out
	}
	a.interactive(withoutRow(boardTap(t, "U1", "kage_board.retry", 0, "61.0")))
	if len(api.updated) != 0 {
		t.Fatalf("updates = %+v", api.updated)
	}
	actor.outcome = lib.BoardOutcomeMoved
	a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "62.0"))
	if len(actor.got) != 2 || len(api.updated) != 1 {
		t.Fatalf("actions %d updates %d, want the second tap to act and edit", len(actor.got), len(api.updated))
	}
}

func TestSlackRowsWithButtonsTakeNoSideBar(t *testing.T) {
	m := boardCard()
	m.Tone = chatmsg.ToneAttention
	layout, err := renderSlackChat(m, "fallback")
	if err != nil {
		t.Fatal(err)
	}
	if len(layout.Attachments) != 0 {
		t.Fatalf("attachments = %v, want the acting rows top-level", layout.Attachments)
	}
	acts := 0
	for _, b := range layout.Blocks {
		if b["type"] == "actions" {
			acts++
		}
	}
	if acts != 2 {
		t.Errorf("top-level actions blocks = %d, want 2", acts)
	}
}

func TestSlackBoardTapIsNotRepeated(t *testing.T) {
	a, _, actor := newBoardSlackAdapter()
	a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "61.0"))
	a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "61.0")) // a redelivery
	a.interactive(boardTap(t, "U1", "kage_board.drop", 0, "63.0"))  // the row is settled
	if len(actor.got) != 1 {
		t.Errorf("actions = %+v, want one", actor.got)
	}
}

func TestSlackBoardTapFromAMemberTheGatewayWouldNotAnswerDoesNothing(t *testing.T) {
	a, api, actor := newBoardSlackAdapter()
	a.interactive(boardTap(t, "U9", "kage_board.drop", 0, "61.0"))
	if len(actor.got) != 0 || len(api.updated) != 0 {
		t.Errorf("actions %+v updates %+v, want none", actor.got, api.updated)
	}
}

func TestSlackBoardTapWithNoRouteDoesNothing(t *testing.T) {
	a, api, _ := newBoardSlackAdapter()
	a.SetBoardActor(nil)
	a.interactive(boardTap(t, "U1", "kage_board.retry", 0, "61.0"))
	if len(api.updated) != 0 {
		t.Errorf("updates = %+v", api.updated)
	}
}

func TestSlackBoardOutcome(t *testing.T) {
	result := func(outcomes ...string) *lib.BoardActionResult {
		r := &lib.BoardActionResult{}
		for _, o := range outcomes {
			r.Results = append(r.Results, lib.BoardActionOutcome{Outcome: o})
		}
		return r
	}
	for _, c := range []struct {
		res  *lib.BoardActionResult
		err  error
		want string
	}{
		{result("done", "failed"), nil, slackBoardFailed},
		{result("done", "moved"), nil, lib.BoardOutcomeDone},
		{result("moved", "gone"), nil, lib.BoardOutcomeMoved},
		{result("gone", "failed"), nil, slackBoardFailed},
		{result(), nil, slackBoardFailed},
		{&lib.BoardActionResult{Error: "busy"}, nil, slackBoardFailed},
		{nil, errors.New("timeout"), slackBoardFailed},
	} {
		if got := slackBoardOutcome(c.res, c.err); got != c.want {
			t.Errorf("slackBoardOutcome(%+v, %v) = %q, want %q", c.res, c.err, got, c.want)
		}
	}
}

func TestSlackBoardLineEscapesTheName(t *testing.T) {
	got := slackBoardLine(chatmsg.RowActionRetry, lib.BoardOutcomeDone, "<!here> & co", time.Unix(0, 0))
	if !strings.HasPrefix(got, "✓ Retrying. &lt;!here&gt; &amp; co, ") {
		t.Errorf("line = %q", got)
	}
}

func TestBusBoardActorOverTheBus(t *testing.T) {
	s := startServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	provision(t, s.ClientURL())
	client, err := lib.Connect(ctx, s.ClientURL(), lib.WithName("board-actor-test"))
	if err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	nc := client.Conn()
	var got lib.BoardAction
	sub, err := nc.Subscribe(lib.BoardSubject, func(m *nats.Msg) {
		_ = json.Unmarshal(m.Data, &got)
		data, _ := json.Marshal(lib.BoardActionResult{Action: got.Action,
			Results: []lib.BoardActionOutcome{{ID: "t_1", Outcome: lib.BoardOutcomeDone}}})
		_ = m.Respond(data)
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	res, err := busBoardActor(client)(ctx, lib.BoardAction{Action: "retry", Tasks: []string{"t_1"}, By: "Jayanti"})
	if err != nil {
		t.Fatal(err)
	}
	if got.Action != "retry" || got.By != "Jayanti" || len(res.Results) != 1 || res.Results[0].Outcome != lib.BoardOutcomeDone {
		t.Errorf("sent %+v, answer %+v", got, res)
	}
}
