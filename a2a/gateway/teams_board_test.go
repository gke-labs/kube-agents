package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"regexp"
	"strings"
	"sync"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// testTeamsBoardMessage is a stuck-task report: two rows that ask for Retry
// and Drop, and one that asks for nothing.
func testTeamsBoardMessage() *chatmsg.Message {
	return &chatmsg.Message{
		Schema:   chatmsg.Schema,
		Headline: "Two tasks are stuck.",
		Rows: []chatmsg.Row{
			{Text: "audit checkout", Detail: "blocked 3h", Tasks: []string{"t_1", "t_2"}, Actions: []string{chatmsg.RowActionRetry, chatmsg.RowActionDrop}},
			{Text: "a note with no buttons"},
			{Text: "upgrade dev-1", Tasks: []string{"t_3"}, Actions: []string{chatmsg.RowActionRetry, chatmsg.RowActionDrop}},
		},
	}
}

// teamsCallBody is call n's card as one string per body block: a text
// block's text, and an ActionSet as "[title|title]".
func teamsCallBody(t *testing.T, relay *fakeTeamsRelay, n int) (body []string, cardActions []any) {
	t.Helper()
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if n >= len(relay.apiCall) {
		t.Fatalf("api calls %d, want more than %d", len(relay.apiCall), n)
	}
	payload, _ := relay.apiCall[n]["payload"].(map[string]any)
	attachments, _ := payload["attachments"].([]any)
	if len(attachments) == 0 {
		t.Fatalf("call %d is not a card: %v", n, payload)
	}
	content, _ := attachments[0].(map[string]any)["content"].(map[string]any)
	blocks, _ := content["body"].([]any)
	for _, raw := range blocks {
		block, _ := raw.(map[string]any)
		if block["type"] == teamsCardActionSet {
			var titles []string
			for _, a := range block[teamsCardActionsKey].([]any) {
				titles = append(titles, a.(map[string]any)["title"].(string))
			}
			body = append(body, "["+strings.Join(titles, "|")+"]")
			continue
		}
		text, _ := block["text"].(string)
		body = append(body, text)
	}
	cardActions, _ = content[teamsCardActionsKey].([]any)
	return body, cardActions
}

// fakeTeamsBoardActor answers every press with outcome and records what it got.
type fakeTeamsBoardActor struct {
	mu      sync.Mutex
	outcome string
	err     error
	got     []lib.BoardAction
}

func (f *fakeTeamsBoardActor) act(_ context.Context, a lib.BoardAction) (*lib.BoardActionResult, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.got = append(f.got, a)
	if f.err != nil {
		return nil, f.err
	}
	res := &lib.BoardActionResult{Action: a.Action}
	for _, task := range a.Tasks {
		res.Results = append(res.Results, lib.BoardActionOutcome{ID: task, Outcome: f.outcome})
	}
	return res, nil
}

func (f *fakeTeamsBoardActor) calls() []lib.BoardAction {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]lib.BoardAction(nil), f.got...)
}

func testTeamsBoardAdapter(t *testing.T, actor *fakeTeamsBoardActor) (*TeamsAdapter, *fakeTeamsRelay) {
	t.Helper()
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	a.SetBoardActor(actor.act)
	a.SetBoardAdmits(func(backend, author string) bool { return backend == teamsBackend && author == testTeamsUser })
	return a, relay
}

func testTeamsBoardPress(verb string, row int, tasks string) map[string]any {
	return map[string]any{teamsPressActionKey: teamsPressActionBoard, teamsPressVerbKey: verb, teamsPressRowKey: row, teamsPressTasksKey: tasks}
}

var testTeamsBoardSettled = regexp.MustCompile(` hangdng, [0-9]{2}:[0-9]{2} UTC$`)

// Each row that asks for buttons gets its Retry and Drop right under it, in
// the card's body, and the card's own action list holds none of them.
func TestTeamsBoardCardDrawsRetryAndDropUnderEachRow(t *testing.T) {
	a, relay := testTeamsBoardAdapter(t, &fakeTeamsBoardActor{})
	if _, _, err := a.PostNotifyMessage(testTeamsChannel, "", OutMessage{Text: "Two tasks are stuck.", Chat: testTeamsBoardMessage()}); err != nil {
		t.Fatal(err)
	}
	body, cardActions := teamsCallBody(t, relay, 0)
	want := []string{"**Two tasks are stuck.**", "• audit checkout · blocked 3h", "[Retry|Drop]", "• a note with no buttons", "• upgrade dev-1", "[Retry|Drop]"}
	if strings.Join(body, "\n") != strings.Join(want, "\n") {
		t.Fatalf("card body\n%s\nwant\n%s", strings.Join(body, "\n"), strings.Join(want, "\n"))
	}
	if len(cardActions) != 0 {
		t.Fatalf("the card's own actions hold %v", cardActions)
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	raw, err := json.Marshal(relay.apiCall[0]["payload"])
	if err != nil || strings.Contains(string(raw), teamsBoardRowType) {
		t.Fatalf("the row marker reached Teams (err %v)", err)
	}
}

// A Retry that settles the row's cards goes over the board route with the
// row's cards and the presser's name, and the row is drawn again with who
// and when in place of its buttons; the other row keeps its buttons. A Drop
// whose cards had already moved reads "No longer stuck."
func TestTeamsBoardPressActsAndDrawsTheRowAgain(t *testing.T) {
	actor := &fakeTeamsBoardActor{outcome: lib.BoardOutcomeDone}
	a, relay := testTeamsBoardAdapter(t, actor)
	id, _, err := a.PostNotifyMessage(testTeamsChannel, "", OutMessage{Text: "Two tasks are stuck.", Chat: testTeamsBoardMessage()})
	if err != nil {
		t.Fatal(err)
	}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:80", id, testTeamsBoardPress(chatmsg.RowActionRetry, 0, "t_1,t_2")))); reason != teamsBoardTaken {
		t.Fatalf("retry press: %q", reason)
	}
	a.boarding.Wait()
	calls := actor.calls()
	if len(calls) != 1 || calls[0].Action != chatmsg.RowActionRetry || strings.Join(calls[0].Tasks, ",") != "t_1,t_2" || calls[0].By != "hangdng" {
		t.Fatalf("board actions %+v", calls)
	}
	body, _ := teamsCallBody(t, relay, 1)
	if len(body) != 6 || !strings.HasPrefix(body[2], gchatBoardRetriedNote) || !testTeamsBoardSettled.MatchString(body[2]) ||
		body[1] != "• audit checkout · blocked 3h" || body[4] != "• upgrade dev-1" || body[5] != "[Retry|Drop]" {
		t.Fatalf("card after the retry\n%s", strings.Join(body, "\n"))
	}
	relay.mu.Lock()
	method := relay.apiCall[1]["method"]
	relay.mu.Unlock()
	if method != "PUT" {
		t.Fatalf("the row was drawn by %v, want an edit", method)
	}

	actor.mu.Lock()
	actor.outcome = lib.BoardOutcomeMoved
	actor.mu.Unlock()
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:81", id, testTeamsBoardPress(chatmsg.RowActionDrop, 2, "t_3")))); reason != teamsBoardTaken {
		t.Fatalf("drop press: %q", reason)
	}
	a.boarding.Wait()
	body, _ = teamsCallBody(t, relay, 2)
	if len(body) != 6 || !strings.HasPrefix(body[5], gchatBoardMovedNote) || !strings.HasPrefix(body[2], gchatBoardRetriedNote) {
		t.Fatalf("card after the drop\n%s", strings.Join(body, "\n"))
	}
}

// A press that did not land keeps the row's buttons under a line saying
// so, and the row can be pressed again; the second press replaces the line.
func TestTeamsBoardPressThatFailsKeepsTheButtons(t *testing.T) {
	actor := &fakeTeamsBoardActor{err: errors.New("no answer")}
	a, relay := testTeamsBoardAdapter(t, actor)
	id, _, err := a.PostNotifyMessage(testTeamsChannel, "", OutMessage{Text: "Two tasks are stuck.", Chat: testTeamsBoardMessage()})
	if err != nil {
		t.Fatal(err)
	}
	a.classify(decodeTeamsActivity(t, testTeamsPress("f:82", id, testTeamsBoardPress(chatmsg.RowActionDrop, 0, "t_1,t_2"))))
	a.boarding.Wait()
	body, _ := teamsCallBody(t, relay, 1)
	if len(body) != 7 || body[2] != slackBoardDropFailed || body[3] != "[Retry|Drop]" {
		t.Fatalf("card after a failed drop\n%s", strings.Join(body, "\n"))
	}
	actor.mu.Lock()
	actor.err, actor.outcome = nil, lib.BoardOutcomeDone
	actor.mu.Unlock()
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:83", id, testTeamsBoardPress(chatmsg.RowActionDrop, 0, "t_1,t_2")))); reason != teamsBoardTaken {
		t.Fatalf("second press: %q", reason)
	}
	a.boarding.Wait()
	body, _ = teamsCallBody(t, relay, 2)
	if len(body) != 6 || !strings.HasPrefix(body[2], gchatBoardDroppedNote) {
		t.Fatalf("card after the second drop\n%s", strings.Join(body, "\n"))
	}
}

// A press with no row, card, action or message, a redelivery, one from a
// sender the install does not serve, and one while no board route is wired
// reach no card and change no message.
func TestTeamsBoardPressRefusals(t *testing.T) {
	actor := &fakeTeamsBoardActor{outcome: lib.BoardOutcomeDone}
	a, relay := testTeamsBoardAdapter(t, actor)
	id, _, err := a.PostNotifyMessage(testTeamsChannel, "", OutMessage{Text: "Two tasks are stuck.", Chat: testTeamsBoardMessage()})
	if err != nil {
		t.Fatal(err)
	}
	good := testTeamsBoardPress(chatmsg.RowActionRetry, 0, "t_1,t_2")
	for _, c := range []struct {
		name, activity, message string
		data                    map[string]any
		want                    string
	}{
		{"no action", "f:90", id, testTeamsBoardPress("archive", 0, "t_1"), teamsBoardNoRow},
		{"no cards", "f:91", id, testTeamsBoardPress(chatmsg.RowActionRetry, 0, " , "), teamsBoardNoRow},
		{"no row", "f:92", id, map[string]any{teamsPressActionKey: teamsPressActionBoard, teamsPressVerbKey: chatmsg.RowActionRetry, teamsPressTasksKey: "t_1"}, teamsBoardNoRow},
		{"no message", "f:93", "", good, teamsBoardNoMessage},
	} {
		if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress(c.activity, c.message, c.data))); reason != c.want {
			t.Errorf("%s: %q, want %q", c.name, reason, c.want)
		}
	}
	stranger := testTeamsPress("f:94", id, good)
	stranger["from"] = map[string]any{"id": "29:other", "aadObjectId": "11111111-2222-3333-4444-555555555555", "name": "someone else"}
	if _, reason := a.classify(decodeTeamsActivity(t, stranger)); reason != teamsBoardUnverified {
		t.Errorf("a stranger's press: %q", reason)
	}
	a.SetBoardAdmits(nil)
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:95", id, good))); reason != teamsBoardUnverified {
		t.Errorf("a press with no admission rule: %q", reason)
	}
	a.SetBoardAdmits(func(string, string) bool { return true })
	a.SetBoardActor(nil)
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:96", id, good))); reason != teamsBoardUnwired {
		t.Errorf("a press with no board route: %q", reason)
	}
	a.SetBoardActor(actor.act)
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:97", id, good))); reason != teamsBoardTaken {
		t.Fatalf("an admitted press: %q", reason)
	}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:97", id, good))); reason != teamsBoardDuplicate+"f:97" {
		t.Errorf("a redelivery: %q", reason)
	}
	a.boarding.Wait()
	if n := len(actor.calls()); n != 1 {
		t.Fatalf("%d board actions, want the one admitted press", n)
	}
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if len(relay.apiCall) != 2 {
		t.Fatalf("%d calls, want the post and one edit", len(relay.apiCall))
	}
}

// A press on a card the adapter does not hold (posted before a restart)
// still acts, and its line is posted in the conversation the press came
// from.
func TestTeamsBoardPressOnAForgottenCardActsAndPostsItsLine(t *testing.T) {
	actor := &fakeTeamsBoardActor{outcome: lib.BoardOutcomeDone}
	a, relay := testTeamsBoardAdapter(t, actor)
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:98", "posted-before-the-restart", testTeamsBoardPress(chatmsg.RowActionRetry, 0, "t_1")))); reason != teamsBoardTaken {
		t.Fatalf("press: %q", reason)
	}
	a.boarding.Wait()
	if n := len(actor.calls()); n != 1 {
		t.Fatalf("%d board actions, want 1", n)
	}
	relay.mu.Lock()
	n, method, endpoint := len(relay.apiCall), relay.apiCall[0]["method"], relay.apiCall[0]["endpoint"]
	relay.mu.Unlock()
	if n != 1 || method != "POST" || endpoint != "v3/conversations/"+testTeamsChannel+"/activities" {
		t.Fatalf("calls %d: %v %v", n, method, endpoint)
	}
	if text := teamsCallText(t, relay, 0); !strings.HasPrefix(text, gchatBoardRetriedNote) || !testTeamsBoardSettled.MatchString(text) {
		t.Fatalf("the posted line reads %q", text)
	}
}

// A rating on a board card keeps the rows' buttons: the redraw that adds
// the recorded line draws them again under their rows.
func TestTeamsBoardRowsSurviveARatingRedraw(t *testing.T) {
	a, relay := testTeamsBoardAdapter(t, &fakeTeamsBoardActor{})
	recorder := &fakeRecorder{admit: map[string]bool{testTeamsUser: true}}
	a.SetFeedbackRecorder(recorder)
	mark := feedbackPostReportPrefix + "board-health"
	id, _, err := a.PostNotifyMessage(testTeamsChannel, "", OutMessage{Text: "Two tasks are stuck.", Chat: testTeamsBoardMessage(), Feedback: mark})
	if err != nil {
		t.Fatal(err)
	}
	data := map[string]any{teamsPressActionKey: teamsPressActionRate, teamsPressTaskKey: mark, teamsPressRatingKey: FeedbackUp}
	a.classify(decodeTeamsActivity(t, testTeamsPress("f:99", id, data)))
	a.rating.Wait()
	body, cardActions := teamsCallBody(t, relay, 1)
	joined := strings.Join(body, "\n")
	if strings.Count(joined, "[Retry|Drop]") != 2 || !strings.Contains(joined, "Feedback recorded") {
		t.Fatalf("card after the rating\n%s", joined)
	}
	if len(cardActions) == 0 {
		t.Fatal("the rating controls are gone")
	}
}

// Two rows with the same text each get their own buttons, and a set whose
// row is not in the card is drawn at the card's end rather than lost.
func TestTeamsPlaceBoardRowsMatchesInOrder(t *testing.T) {
	button := func(title string) []map[string]any {
		return []map[string]any{{"type": teamsActionSubmit, "title": title}}
	}
	card := teamsMarkdownToCard("- same\n- same\n- other")
	rest := teamsPlaceBoardRows(card, []map[string]any{
		{"type": teamsActionOpenURL, "title": "a link"},
		{"type": teamsBoardRowType, teamsBoardRowBlock: "• same", teamsBoardRowNumber: 0, teamsBoardRowActions: button("first")},
		{"type": teamsBoardRowType, teamsBoardRowBlock: "• same", teamsBoardRowNumber: 1, teamsBoardRowActions: button("second")},
		{"type": teamsBoardRowType, teamsBoardRowBlock: "• gone", teamsBoardRowNumber: 2, teamsBoardRowActions: button("third")},
	})
	if len(rest) != 1 || rest[0]["title"] != "a link" {
		t.Fatalf("buttons left %v", rest)
	}
	var got []string
	for _, block := range card["body"].([]map[string]any) {
		if block["type"] == teamsCardActionSet {
			got = append(got, "["+block[teamsCardActionsKey].([]map[string]any)[0]["title"].(string)+"]")
			continue
		}
		got = append(got, block["text"].(string))
	}
	want := "• same\n[first]\n• same\n[second]\n• other\n[third]"
	if strings.Join(got, "\n") != want {
		t.Fatalf("body\n%s\nwant\n%s", strings.Join(got, "\n"), want)
	}
}
