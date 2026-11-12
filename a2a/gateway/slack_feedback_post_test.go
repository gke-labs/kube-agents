package gateway

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The rating controls on a home post the agent made on its own, on Slack:
// after the post's own buttons, kept through the edit that rewrites it, on a
// Block Kit report, and never without the recorder.

const testPostAlertID = feedbackPostAlertPrefix + "0123456789abcdef"

func newRatedHomeSlackAdapter() (*SlackAdapter, *fakeSlackAPI) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	a.feedbackAPI = &fakeFeedbackAPI{}
	a.SetFeedbackRecorder(&fakeRecorder{admit: map[string]bool{"U1": true}})
	return a, api
}

// decodedBlocks is a blocks form value as a list of blocks.
func decodedBlocks(t *testing.T, blocks string) []map[string]any {
	t.Helper()
	var got []map[string]any
	if err := json.Unmarshal([]byte(blocks), &got); err != nil {
		t.Fatalf("blocks %q: %v", blocks, err)
	}
	return got
}

func testDriftAlert() OutMessage {
	return OutMessage{Text: "eval-drift-workload on seeded-a was changed outside Git.",
		Chat: &chatmsg.Message{Schema: chatmsg.Schema, Headline: "eval-drift-workload on seeded-a was changed outside Git.",
			Choices: []chatmsg.Choice{{Text: "Put Git's version back", Primary: true}, {Text: "Keep it · update Git"}}},
		Feedback: testPostAlertID}
}

// The drift alert's two buttons stay first and primary; the rating controls
// come after them, as the last block, labelled for a post.
func TestSlackHomeAlertRatingComesAfterItsActionButtons(t *testing.T) {
	a, api := newRatedHomeSlackAdapter()
	if _, _, err := a.PostNotifyMessage("C0HOME", "", testDriftAlert()); err != nil {
		t.Fatal(err)
	}
	if len(api.postOptions) != 1 {
		t.Fatalf("posts = %d, want 1", len(api.postOptions))
	}
	blocks, _ := sentBlocks(t, api.postOptions[0])
	got := decodedBlocks(t, blocks)
	last := got[len(got)-1]
	if last["type"] != slackContextActionsBlock || last["block_id"] != slackFeedbackBlockPrefix+testPostAlertID {
		t.Fatalf("last block = %v, want the rating controls", last)
	}
	if got[len(got)-2]["type"] != slackBlockActionsT {
		t.Errorf("the alert's own buttons must stay above the rating controls: %s", blocks)
	}
	if !strings.Contains(blocks, `"accessibility_label":"Useful"`) || strings.Contains(blocks, "Good answer") {
		t.Errorf("a post's controls must read Useful / Not useful: %s", blocks)
	}
}

// The triage that rewrites the alert keeps its rating controls, last.
func TestSlackEditOfARatedHomeAlertKeepsItsRating(t *testing.T) {
	a, api := newRatedHomeSlackAdapter()
	if err := a.EditNotifyMessage("C0HOME", "1.0", "1.0", testDriftAlert()); err != nil {
		t.Fatal(err)
	}
	if len(api.updatedBlocks) != 1 {
		t.Fatalf("updates = %d, want 1", len(api.updatedBlocks))
	}
	got := decodedBlocks(t, api.updatedBlocks[0])
	if last := got[len(got)-1]; last["type"] != slackContextActionsBlock {
		t.Errorf("last block of the edit = %v, want the rating controls", last)
	}
	text := testDriftAlert()
	text.Chat = nil
	if err := a.EditNotifyMessage("C0HOME", "1.0", "1.0", text); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(api.updatedBlocks[1], slackContextActionsBlock) || strings.Contains(api.updatedAttachments[1], "blocks") {
		t.Errorf("a text edit = %s / %s, want the controls kept and the side bar cleared", api.updatedBlocks[1], api.updatedAttachments[1])
	}
}

// A plain text home report is a section and the rating controls.
func TestSlackHomeTextReportCarriesTheRating(t *testing.T) {
	a, api := newRatedHomeSlackAdapter()
	if _, _, err := a.PostNotifyMessage("C0HOME", "", OutMessage{Text: "Nightly: 3 clusters healthy.", Feedback: feedbackPostReportPrefix + "1"}); err != nil {
		t.Fatal(err)
	}
	blocks, _ := sentBlocks(t, api.postOptions[0])
	if !strings.Contains(blocks, `"type":"section"`) || !strings.Contains(blocks, slackContextActionsBlock) {
		t.Errorf("blocks = %s", blocks)
	}
}

// A Block Kit report (the fleet audit's card) ends with the rating controls.
func TestSlackHomeBlocksReportCarriesTheRating(t *testing.T) {
	a, api := newRatedHomeSlackAdapter()
	raw := json.RawMessage(`[{"type":"section","text":{"type":"mrkdwn","text":"*Fleet audit*: 2 findings"}}]`)
	if _, _, err := a.PostNotifyBlocks("C0HOME", "", "Fleet audit: 2 findings", raw, feedbackPostReportPrefix+"2"); err != nil {
		t.Fatal(err)
	}
	blocks, _ := sentBlocks(t, api.postOptions[0])
	got := decodedBlocks(t, blocks)
	if len(got) != 2 || got[1]["type"] != slackContextActionsBlock {
		t.Errorf("blocks = %s, want the report then the rating controls", blocks)
	}
}

// refusingRatedSlackAPI is fakeSlackAPI refusing, as invalid_blocks, every
// post that carries the rating controls.
type refusingRatedSlackAPI struct {
	*fakeSlackAPI
	refused int
}

func (f *refusingRatedSlackAPI) PostMessage(channelID string, options ...slack.MsgOption) (string, string, error) {
	if blocks := sentUpdateForm(channelID, "", options...).Get("blocks"); strings.Contains(blocks, slackContextActionsBlock) {
		f.refused++
		return "", "", slack.SlackErrorResponse{Err: "invalid_blocks"}
	}
	return f.fakeSlackAPI.PostMessage(channelID, options...)
}

// A rated home post Slack refuses posts once more without the controls, its
// own layout and buttons kept; so does a rated Block Kit report.
func TestSlackRefusedRatedHomePostKeepsItsLayout(t *testing.T) {
	a, api := newRatedHomeSlackAdapter()
	refusing := &refusingRatedSlackAPI{fakeSlackAPI: api}
	a.api = refusing
	if _, _, err := a.PostNotifyMessage("C0HOME", "", testDriftAlert()); err != nil {
		t.Fatal(err)
	}
	raw := json.RawMessage(`[{"type":"section","text":{"type":"mrkdwn","text":"x"}}]`)
	if _, _, err := a.PostNotifyBlocks("C0HOME", "", "x", raw, feedbackPostReportPrefix+"4"); err != nil {
		t.Fatal(err)
	}
	if refusing.refused != 2 || len(api.postOptions) != 2 {
		t.Fatalf("refused = %d, posted = %d; want each refused once and posted once without the controls", refusing.refused, len(api.postOptions))
	}
	blocks, _ := sentBlocks(t, api.postOptions[0])
	got := decodedBlocks(t, blocks)
	if got[len(got)-1]["type"] != slackBlockActionsT {
		t.Errorf("the alert reposted as %s, want its layout with its own buttons", blocks)
	}
}

// Without the recorder (feedback off) a marked home post draws no controls.
func TestSlackHomePostDrawsNoRatingWithoutTheRecorder(t *testing.T) {
	api := &fakeSlackAPI{}
	a, _ := newUXSlackAdapter(api)
	a.feedbackAPI = &fakeFeedbackAPI{}
	if _, _, err := a.PostNotifyMessage("C0HOME", "", testDriftAlert()); err != nil {
		t.Fatal(err)
	}
	raw := json.RawMessage(`[{"type":"section","text":{"type":"mrkdwn","text":"x"}}]`)
	if _, _, err := a.PostNotifyBlocks("C0HOME", "", "x", raw, feedbackPostReportPrefix+"3"); err != nil {
		t.Fatal(err)
	}
	for i, opts := range api.postOptions {
		if blocks, _ := sentBlocks(t, opts); strings.Contains(blocks, slackContextActionsBlock) {
			t.Errorf("post %d = %s, want no rating controls with feedback off", i, blocks)
		}
	}
}
