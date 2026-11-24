package gateway

import (
	"strings"
	"testing"
)

// teamsCallText is the text of the card the nth passthrough call carried:
// its TextBlocks joined, or the activity's plain text.
func teamsCallText(t *testing.T, relay *fakeTeamsRelay, n int) string {
	t.Helper()
	relay.mu.Lock()
	defer relay.mu.Unlock()
	if n >= len(relay.apiCall) {
		t.Fatalf("api calls %d, want more than %d", len(relay.apiCall), n)
	}
	payload, _ := relay.apiCall[n]["payload"].(map[string]any)
	attachments, _ := payload["attachments"].([]any)
	if len(attachments) == 0 {
		text, _ := payload["text"].(string)
		return text
	}
	content, _ := attachments[0].(map[string]any)["content"].(map[string]any)
	body, _ := content["body"].([]any)
	lines := []string{}
	for _, block := range body {
		if text, ok := block.(map[string]any)["text"].(string); ok {
			lines = append(lines, text)
		}
	}
	return strings.Join(lines, "\n")
}

func teamsCallCount(relay *fakeTeamsRelay) int {
	relay.mu.Lock()
	defer relay.mu.Unlock()
	return len(relay.apiCall)
}

// ratedTeamsAdapter is an adapter holding a recorder that admits the test
// user, with one rated answer posted under task-1; it returns the answer's
// message id.
func ratedTeamsAdapter(t *testing.T) (*TeamsAdapter, *fakeTeamsRelay, *fakeRecorder, string) {
	t.Helper()
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	recorder := &fakeRecorder{admit: map[string]bool{testTeamsUser: true}}
	a.SetFeedbackRecorder(recorder)
	id, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "the fleet is fine", Feedback: "task-1"})
	if err != nil || id == "" {
		t.Fatalf("post: %v %q", err, id)
	}
	return a, relay, recorder, id
}

func teamsRatePress(id, message, rating, note string) map[string]any {
	data := map[string]any{teamsPressActionKey: teamsPressActionRate, teamsPressTaskKey: "task-1", teamsPressRatingKey: rating}
	if note != "" {
		data[teamsPressNoteKey] = note
	}
	return testTeamsPress(id, message, data)
}

// An answer marked for feedback carries the two rating controls on its
// card once the adapter holds the recorder: a 👍 that submits, and a 👎
// that unfolds the note form whose Send submits the 👎. Without the
// recorder, or as plain text, it carries none.
func TestTeamsDrawsTheRatingControlsOnAMarkedAnswer(t *testing.T) {
	_, relay, _, _ := ratedTeamsAdapter(t)
	actions := teamsLineActions(t, relay, 0)
	if len(actions) != 2 {
		t.Fatalf("actions %v, want 👍 and 👎", actions)
	}
	up, down := actions[0].(map[string]any), actions[1].(map[string]any)
	upData, _ := up["data"].(map[string]any)
	if up["type"] != teamsActionSubmit || up["title"] != teamsRatingUpTitle || upData[teamsPressActionKey] != teamsPressActionRate ||
		upData[teamsPressTaskKey] != "task-1" || upData[teamsPressRatingKey] != FeedbackUp {
		t.Errorf("👍 %v", up)
	}
	form, _ := down["card"].(map[string]any)
	if down["type"] != teamsActionShowCard || down["title"] != teamsRatingDownTitle || form == nil {
		t.Fatalf("👎 %v, want the control that unfolds the note form", down)
	}
	body, _ := form["body"].([]any)
	if len(body) != 2 || body[0].(map[string]any)["type"] != teamsInputText || body[0].(map[string]any)["id"] != teamsPressNoteKey ||
		!strings.Contains(body[1].(map[string]any)["text"].(string), "30 days") {
		t.Errorf("note form body %v, want the note box and how long the answer is kept", body)
	}
	send, _ := form[teamsCardActionsKey].([]any)
	if len(send) != 1 {
		t.Fatalf("note form actions %v, want Send alone", send)
	}
	sendData, _ := send[0].(map[string]any)["data"].(map[string]any)
	if send[0].(map[string]any)["title"] != feedbackNoteSend || sendData[teamsPressRatingKey] != FeedbackDown || sendData[teamsPressTaskKey] != "task-1" {
		t.Errorf("Send %v", send[0])
	}

	bare := newFakeTeamsRelay(t, nil)
	b := newTestTeamsAdapter(t, bare.srv.URL, testTeamsTenant)
	if _, err := b.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: "the fleet is fine", Feedback: "task-1"}); err != nil {
		t.Fatal(err)
	}
	if actions := teamsLineActions(t, bare, 0); len(actions) != 0 {
		t.Errorf("actions without a recorder %v, want none", actions)
	}
}

// A whole answer edited onto the task's line carries the controls too.
func TestTeamsDrawsTheRatingControlsOnAnAnswerEditedOntoTheLine(t *testing.T) {
	a, relay, _, id := ratedTeamsAdapter(t)
	if err := a.EditMessage("teams:"+testTeamsChannel, id, OutMessage{Text: "the fleet is fine, all seven clusters", Feedback: "task-1"}); err != nil {
		t.Fatal(err)
	}
	if actions := teamsLineActions(t, relay, 1); len(actions) != 2 {
		t.Fatalf("actions on the edit %v, want 👍 and 👎", actions)
	}
}

// A 👍 press is recorded under the presser, the task and the pressed
// message, with the answer's text, and is not a turn; the card is redrawn
// once to say a rating was recorded, keeping its controls, and a second
// rating on it redraws nothing.
func TestTeamsRecordsARatingPress(t *testing.T) {
	a, relay, recorder, id := ratedTeamsAdapter(t)
	msg, reason := a.classify(decodeTeamsActivity(t, teamsRatePress("f:40", id, FeedbackUp, "")))
	if reason == "" {
		t.Fatalf("a rating press was a turn: %+v", msg)
	}
	a.rating.Wait()
	recorder.mu.Lock()
	ratings := append([]FeedbackRating(nil), recorder.ratings...)
	recorder.mu.Unlock()
	want := FeedbackRating{
		Platform: platformTeams, Backend: teamsBackend, Rater: testTeamsUser, Conversation: "teams:" + testTeamsChannel,
		MessageID: id, TaskID: "task-1", Rating: FeedbackUp, Answer: "the fleet is fine",
	}
	if len(ratings) != 1 || ratings[0] != want {
		t.Fatalf("ratings %+v, want %+v", ratings, want)
	}
	if got := teamsCallCount(relay); got != 2 {
		t.Fatalf("api calls %d, want the post and one redraw", got)
	}
	if text := teamsCallText(t, relay, 1); !strings.Contains(text, "the fleet is fine") || !strings.Contains(text, "Feedback recorded") {
		t.Errorf("redrawn card reads %q", text)
	}
	if actions := teamsLineActions(t, relay, 1); len(actions) != 2 {
		t.Errorf("redrawn card actions %v, want the controls kept", actions)
	}

	a.classify(decodeTeamsActivity(t, teamsRatePress("f:41", id, FeedbackDown, "")))
	a.rating.Wait()
	if got := teamsCallCount(relay); got != 2 {
		t.Errorf("api calls %d after a second rating, want no second redraw", got)
	}
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if len(recorder.ratings) != 2 || recorder.ratings[1].Rating != FeedbackDown || len(recorder.notes) != 0 {
		t.Errorf("ratings %+v notes %v, want the 👎 kept with no note", recorder.ratings, recorder.notes)
	}
}

// A 👎 sent with a note keeps the rating and the note.
func TestTeamsRecordsAThumbsDownWithItsNote(t *testing.T) {
	a, _, recorder, id := ratedTeamsAdapter(t)
	a.classify(decodeTeamsActivity(t, teamsRatePress("f:42", id, FeedbackDown, "it named the wrong cluster")))
	a.rating.Wait()
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if len(recorder.ratings) != 1 || recorder.ratings[0].Rating != FeedbackDown {
		t.Fatalf("ratings %+v", recorder.ratings)
	}
	if len(recorder.notes) != 1 || !strings.HasSuffix(recorder.notes[0], "it named the wrong cluster") || !strings.HasPrefix(recorder.notes[0], testTeamsUser+" task-1 "+id) {
		t.Fatalf("notes %q", recorder.notes)
	}
}

// A press the recorder does not admit, a redelivery, a rating the adapter
// does not draw and a press naming no message record nothing; a press on a
// message the adapter no longer holds is recorded without the answer's
// text and redraws nothing.
func TestTeamsRatingPressesThatRecordNothingOrLess(t *testing.T) {
	a, relay, recorder, id := ratedTeamsAdapter(t)
	stranger := teamsRatePress("f:43", id, FeedbackUp, "")
	stranger["from"] = map[string]any{"id": "29:2xyz", "aadObjectId": "0a1b2c3d-0000-4000-8000-000000000009", "name": "someone"}
	a.classify(decodeTeamsActivity(t, stranger))
	a.classify(decodeTeamsActivity(t, teamsRatePress("f:44", id, "sideways", "")))
	a.classify(decodeTeamsActivity(t, teamsRatePress("f:45", "", FeedbackUp, "")))
	a.rating.Wait()
	recorder.mu.Lock()
	n := len(recorder.ratings)
	recorder.mu.Unlock()
	if n != 0 {
		t.Fatalf("%d ratings recorded, want none", n)
	}

	a.classify(decodeTeamsActivity(t, teamsRatePress("f:46", "a-message-from-before-a-restart", FeedbackUp, "")))
	a.classify(decodeTeamsActivity(t, teamsRatePress("f:46", "a-message-from-before-a-restart", FeedbackUp, "")))
	a.rating.Wait()
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if len(recorder.ratings) != 1 || recorder.ratings[0].Answer != "" || recorder.ratings[0].MessageID != "a-message-from-before-a-restart" {
		t.Fatalf("ratings %+v, want the one press on the unheld message, without an answer", recorder.ratings)
	}
	if got := teamsCallCount(relay); got != 1 {
		t.Errorf("api calls %d, want the post alone", got)
	}
}

// With answer feedback off the gateway hands over no recorder, and a press
// of a control drawn before it was turned off records nothing.
func TestTeamsRatingPressWithFeedbackOffRecordsNothing(t *testing.T) {
	a := newTestTeamsAdapter(t, "http://relay.invalid", testTeamsTenant)
	if msg, reason := a.classify(decodeTeamsActivity(t, teamsRatePress("f:47", "1794603936403", FeedbackUp, ""))); reason == "" {
		t.Fatalf("a rating press was a turn: %+v", msg)
	}
	a.rating.Wait()
}

// An alert's triage edits it under the alert's own feedback id, which the
// notifier hands every backend; Teams rates answers only, so the edited
// alert carries no rating controls, and neither does a post so marked.
func TestTeamsDrawsNoRatingOnAnAlertItsTriageEdits(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	a.SetFeedbackRecorder(&fakeRecorder{})
	home := "19:11fb65f1e6c941b382a410dd0e383724@thread.tacv2"
	mark := feedbackPostAlertPrefix + "drift-1"
	var editor notifyChatEditor = a
	if err := editor.EditNotifyMessage(home, "", "act-7", OutMessage{Text: "triage: resolved", Feedback: mark}); err != nil {
		t.Fatalf("edit: %v", err)
	}
	if actions := teamsLineActions(t, relay, 0); len(actions) != 0 {
		t.Fatalf("an edited alert carries actions: %v", actions)
	}
	if _, err := a.PostMessage(teamsConversationID(home), OutMessage{Text: "report", Feedback: feedbackPostReportPrefix + "r-1"}); err != nil {
		t.Fatalf("post: %v", err)
	}
	if actions := teamsLineActions(t, relay, 1); len(actions) != 0 {
		t.Fatalf("a post marked as a report carries actions: %v", actions)
	}
}

// A rated card that only just fits the activity budget is not redrawn: with
// the recorded line added it would go out as plain text and lose its
// controls for everyone. The rating is still recorded.
func TestTeamsDoesNotRedrawARatedCardTheRecordedLineWouldOverflow(t *testing.T) {
	relay := newFakeTeamsRelay(t, nil)
	a := newTestTeamsAdapter(t, relay.srv.URL, testTeamsTenant)
	recorder := &fakeRecorder{admit: map[string]bool{testTeamsUser: true}}
	a.SetFeedbackRecorder(recorder)
	actions := teamsRatingActions("task-1", recorder.KeptDays())
	// The longest answer of short lines whose card with the controls fits.
	low, high := 1, teamsMaxActivityBytes
	for low < high {
		mid := (low + high + 1) / 2
		if teamsFitsCard(strings.Repeat("x\n", mid), actions) {
			low = mid
		} else {
			high = mid - 1
		}
	}
	answer := strings.Repeat("x\n", low)
	if !teamsFitsCard(answer, actions) || teamsFitsCard(answer+"\n\n"+teamsRatingRecordedLine, actions) {
		t.Fatalf("no answer of %d lines sits at the budget's edge", low)
	}
	id, err := a.PostMessage("teams:"+testTeamsChannel, OutMessage{Text: answer, Feedback: "task-1"})
	if err != nil || id == "" {
		t.Fatalf("post: %v %q", err, id)
	}
	if len(teamsLineActions(t, relay, 0)) == 0 {
		t.Fatal("the answer at the budget's edge was posted without its controls")
	}
	if _, reason := a.classify(decodeTeamsActivity(t, teamsRatePress("f:60", id, FeedbackUp, ""))); reason == "" {
		t.Fatal("a rating press was a turn")
	}
	a.rating.Wait()
	recorder.mu.Lock()
	kept := len(recorder.ratings)
	recorder.mu.Unlock()
	if kept != 1 {
		t.Fatalf("ratings kept = %d, want 1", kept)
	}
	if calls := teamsCallCount(relay); calls != 1 {
		t.Fatalf("api calls = %d, want the post alone: the card must not be redrawn", calls)
	}
}

// Teams draws no controls on an alert or a report, so a press that names
// one is no press this adapter drew: it records nothing.
func TestTeamsRecordsNoRatingForAPressThatNamesAPost(t *testing.T) {
	a, _, recorder, id := ratedTeamsAdapter(t)
	data := map[string]any{teamsPressActionKey: teamsPressActionRate, teamsPressTaskKey: feedbackPostAlertPrefix + "drift-1", teamsPressRatingKey: FeedbackUp}
	if _, reason := a.classify(decodeTeamsActivity(t, testTeamsPress("f:70", id, data))); reason == "" {
		t.Fatal("a rating press was a turn")
	}
	a.rating.Wait()
	recorder.mu.Lock()
	defer recorder.mu.Unlock()
	if len(recorder.ratings) != 0 {
		t.Fatalf("ratings %+v, want none for a press that names a post", recorder.ratings)
	}
}
