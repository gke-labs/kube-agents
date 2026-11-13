package gateway

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Rating the posts the agent makes on its own: a home alert, its triage, a
// report. Each is marked for rating under an id that says its kind, the
// rating is counted apart from answers, and its record and export say it
// answered no question.

// postFeedbackConversations is fakeConversations that rates home posts
// (notifyPostFeedback) under kind:key, recording each ask.
type postFeedbackConversations struct {
	*fakeConversations
	asked []string
}

func (f *postFeedbackConversations) PostFeedbackTarget(kind, key string) string {
	f.asked = append(f.asked, kind+":"+key)
	return kind + "-" + key
}

// ratedHomePoster is fakeHomePoster recording the mark each laid-out post or
// edit carries.
type ratedHomePoster struct {
	*fakeHomePoster
	posted []string
	edited []string
}

func (f *ratedHomePoster) PostNotifyMessage(space, thread string, m OutMessage) (string, string, error) {
	f.posted = append(f.posted, m.Feedback)
	return f.fakeHomePoster.PostNotifyMessage(space, thread, m)
}

func (f *ratedHomePoster) EditNotifyMessage(space, thread, message string, m OutMessage) error {
	f.edited = append(f.edited, m.Feedback)
	return f.fakeHomePoster.EditNotifyMessage(space, thread, message, m)
}

func newRatedHomeNotifier(t *testing.T) (*Notifier, *ratedHomePoster, *postFeedbackConversations) {
	t.Helper()
	p := &ratedHomePoster{fakeHomePoster: &fakeHomePoster{fakeNotifyPoster: &fakeNotifyPoster{landsIn: testHomeThread}}}
	n, err := NewGchatNotifier(p, testHome, nil)
	if err != nil {
		t.Fatal(err)
	}
	conv := &postFeedbackConversations{fakeConversations: &fakeConversations{contexts: map[string]string{testConversation: testContext}}}
	n.SetConversations(conv)
	return n, p, conv
}

// The alert and the triage that rewrites it are one post: both carry the
// alert's mark, keyed by its update key, so a rating survives the rewrite.
func TestNotifyHomeAlertAndItsTriageCarryOneRating(t *testing.T) {
	n, p, conv := newRatedHomeNotifier(t)
	alert := serveJSON(t, n, lib.NotifyRequest{Text: "🚨 seeded-b: CrashLoopBackOff", Update: "alert-s1"})
	if alert.Error != "" {
		t.Fatalf("alert = %+v", alert)
	}
	triage := serveJSON(t, n, homeKeyed("triage", "alert-s1", alert.ThreadID))
	if triage.Error != "" || triage.MessageID != alert.MessageID {
		t.Fatalf("triage = %+v, want it to edit the alert", triage)
	}
	want := postKindAlert + "-alert-s1"
	if len(p.posted) != 1 || p.posted[0] != want || len(p.edited) != 1 || p.edited[0] != want {
		t.Fatalf("marks posted = %q, edited = %q; want %q on both", p.posted, p.edited, want)
	}
	for _, asked := range conv.asked {
		if asked != postKindAlert+":alert-s1" {
			t.Errorf("asked for %q, want only the alert's key", asked)
		}
	}
}

// A reply in an alert's thread (the triage card's plan, a later note) is not
// the agent's own post and carries no controls, keyed or not.
func TestNotifyHomeThreadReplyIsNotRated(t *testing.T) {
	n, p, _ := newRatedHomeNotifier(t)
	alert := serveJSON(t, n, lib.NotifyRequest{Text: "🚨 seeded-b: CrashLoopBackOff", Update: "alert-s1"})
	if alert.Error != "" {
		t.Fatalf("alert = %+v", alert)
	}
	plan := serveJSON(t, n, homeKeyed("plan", "plan-c1-1", alert.ThreadID))
	note := serveJSON(t, n, lib.NotifyRequest{Text: "a note under the alert", Thread: alert.ThreadID})
	if plan.Error != "" || note.Error != "" {
		t.Fatalf("plan = %+v, note = %+v", plan, note)
	}
	// The note posts as plain text (PostNotify), which records no mark.
	if len(p.all()) != 3 || len(p.posted) != 2 || p.posted[0] == "" || p.posted[1] != "" {
		t.Fatalf("posts = %d, marks = %q; want 3 posts, the alert rated and the plan not", len(p.all()), p.posted)
	}
}

// A home post with no update key is a report.
func TestNotifyHomeUnkeyedPostIsRatedAsAReport(t *testing.T) {
	n, p, _ := newRatedHomeNotifier(t)
	if got := serveJSON(t, n, lib.NotifyRequest{Text: "fleet audit", Chat: json.RawMessage(testHomeChat)}); got.Error != "" {
		t.Fatalf("reply = %+v", got)
	}
	if len(p.posted) != 1 || p.posted[0] != postKindReport+"-" {
		t.Fatalf("marks = %q, want one report mark", p.posted)
	}
}

// A long text report is chunked, and only its last chunk carries the rating
// controls, as a rated answer's text does.
func TestNotifyHomeTextReportRatesItsLastChunkOnly(t *testing.T) {
	n, p, _ := newRatedHomeNotifier(t)
	text := strings.Repeat("a line of the nightly report\n", discordChunk/20)
	if got := serveJSON(t, n, lib.NotifyRequest{Text: text}); got.Error != "" {
		t.Fatalf("reply = %+v", got)
	}
	chunks := chatChunks(text, discordChunk)
	if len(chunks) < 2 {
		t.Fatalf("the report fits one chunk (%d); make it longer", len(chunks))
	}
	if len(p.all()) != len(chunks) || len(p.posted) != 1 || p.posted[0] != postKindReport+"-" {
		t.Fatalf("posts = %d, rated posts = %q; want %d posts, the last one rated", len(p.all()), p.posted, len(chunks))
	}
	if last := p.all()[len(chunks)-1]; last.text != chunks[len(chunks)-1] {
		t.Errorf("the rated post is %q, want the last chunk", last.text)
	}
}

// A post into a conversation is a card's work for a person's ask, not a post
// of the agent's own: it is never rated as one.
func TestNotifyConversationPostIsNotRatedAsAPost(t *testing.T) {
	n, _, conv := newRatedHomeNotifier(t)
	req := lib.NotifyRequest{Text: "the card's progress", Conversation: testConversation, ContextID: testContext}
	if got := serveJSON(t, n, req); got.Error != "" {
		t.Fatalf("reply = %+v", got)
	}
	if len(conv.asked) != 0 {
		t.Errorf("asked for post marks %q, want none for a conversation post", conv.asked)
	}
}

// The id says the kind and is a task-id-shaped token: keyed, the same for
// the same key; unkeyed, fresh each time; none while feedback is off.
func TestPostFeedbackTargetSaysItsKind(t *testing.T) {
	r := startRig(t)
	if got := r.g.postFeedbackTarget(postKindAlert, "alert-s1"); got != "" {
		t.Fatalf("target with feedback off = %q, want none", got)
	}
	r = startRigWith(t, withFeedbackBucket(t, time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	keyed := r.g.postFeedbackTarget(postKindAlert, "alert-s1")
	if keyed != r.g.postFeedbackTarget(postKindAlert, "alert-s1") || postKindOf(keyed) != postKindAlert || !lib.ValidSubjectToken(keyed) {
		t.Errorf("keyed target %q: want a stable alert token", keyed)
	}
	report := r.g.postFeedbackTarget(postKindReport, "")
	if report == r.g.postFeedbackTarget(postKindReport, "") || postKindOf(report) != postKindReport || !lib.ValidSubjectToken(report) {
		t.Errorf("unkeyed target %q: want a fresh report token", report)
	}
	if postKindOf(lib.NewTaskID()) != "" {
		t.Error("a task id must never read as a post")
	}
}

// A rating of an alert is kept with its kind and no ask, counted under the
// post family by kind and not as an answer, left out of the answers' tally,
// and exported as a draft whose prompt names the trigger to plant.
func TestDeskKeepsAnAlertRatingApartFromAnswers(t *testing.T) {
	r := startRigWith(t, withFeedbackBucket(t, 24*time.Hour))
	waitFor(t, "feedback on", func() bool { return r.g.feedback.Load() != nil })
	desk := r.g.feedback.Load()
	alert := r.g.postFeedbackTarget(postKindAlert, "alert-k8s-evt-1")
	down := FeedbackRating{Platform: platformSlack, Backend: "discord", Rater: "1001", Conversation: "slack:C1/1.0",
		MessageID: "m1", TaskID: alert, Rating: FeedbackDown, Answer: "🚨 seeded-b: CrashLoopBackOff"}
	askKept, err := desk.Rate(down)
	if err != nil || askKept {
		t.Fatalf("rate = %v, %v; want kept with no ask", askKept, err)
	}
	ctx := context.Background()
	kv, err := r.client.KV(ctx, lib.FeedbackBucket)
	if err != nil {
		t.Fatal(err)
	}
	rec, err := getFeedback(ctx, kv, feedbackKey(alert, "1001"))
	if err != nil || rec.Kind != postKindAlert || rec.Ask != "" || rec.Rating != FeedbackDown {
		t.Fatalf("record = %+v, %v; want a 👎 kept as an alert with no ask", rec, err)
	}
	m := r.g.Metrics()
	if got := counterValue(t, m.postFeedback.WithLabelValues(platformSlack, FeedbackDown, postKindAlert)); got != 1 {
		t.Errorf("alert 👎 count = %v, want 1", got)
	}
	for _, answer := range []string{answerReply, answerCard, metricsLabelOther} {
		if got := counterValue(t, m.answerFeedback.WithLabelValues(platformSlack, FeedbackDown, answer)); got != 0 {
			t.Errorf("answer %s 👎 count = %v, want 0: an alert is not an answer", answer, got)
		}
	}
	if u, d, err := desk.Tally(ctx, time.Now().Add(-time.Hour)); err != nil || u != 0 || d != 0 {
		t.Errorf("answers' tally = %d, %d, %v; want none", u, d, err)
	}
	var out, log bytes.Buffer
	if n, err := ExportFeedback(ctx, r.client, 0, time.Now(), &out, &log); err != nil || n != 1 {
		t.Fatalf("export = %d, %v", n, err)
	}
	if !strings.Contains(log.String(), "seeded-b") {
		t.Errorf("export log = %q, want the line to quote the post", log.String())
	}
	var task, transcript string
	tr := tar.NewReader(&out)
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		body, _ := io.ReadAll(tr)
		switch {
		case strings.HasSuffix(h.Name, "/"+feedbackTaskFile):
			task = string(body)
		case strings.HasSuffix(h.Name, "/"+feedbackTranscript):
			transcript = string(body)
		}
	}
	for _, want := range []string{"on an alert the agent posted on its own", "Plant the trigger"} {
		if !strings.Contains(task, want) {
			t.Errorf("task.yaml lacks %q:\n%s", want, task)
		}
	}
	if strings.Contains(task, "no longer on record") || !strings.Contains(transcript, "## The trigger") || !strings.Contains(transcript, "## The rated post") {
		t.Errorf("the draft must read as a post with no ask:\n%s\n%s", task, transcript)
	}
}

// A post's controls and note use the post's words, an answer's its own.
func TestPostRatingWordsAreAPosts(t *testing.T) {
	if good, bad := feedbackLabels(feedbackPostAlertPrefix + "0"); good != feedbackPostGoodLabel || bad != feedbackPostBadLabel {
		t.Errorf("post labels = %q, %q", good, bad)
	}
	if good, bad := feedbackLabels("task-1"); good != feedbackGoodLabel || bad != feedbackBadLabel {
		t.Errorf("answer labels = %q, %q", good, bad)
	}
	if got := feedbackKeptTextFor(feedbackPostReportPrefix+"0", 30, false); got != "This post is kept on your install for 30 days so an operator can turn it into a test." {
		t.Errorf("post kept line = %q", got)
	}
	if got := feedbackKeptTextFor("task-1", 30, true); got != feedbackKeptText(30, true) {
		t.Errorf("answer kept line = %q", got)
	}
	if got := slackFeedbackThanksFor(feedbackPostAlertPrefix + "0"); !strings.HasPrefix(got, feedbackPostThanksLine) {
		t.Errorf("post thanks = %q", got)
	}
	if feedbackNoteTitleFor(feedbackPostAlertPrefix+"0") != feedbackPostNoteTitle || feedbackNoteTitleFor("task-1") != feedbackNoteTitle {
		t.Error("the note's title must be a post's for a post and an answer's for an answer")
	}
	// Slack caps a modal's title at 24 characters.
	if n := len([]rune(feedbackPostNoteTitle)); n > 24 {
		t.Errorf("post note title is %d characters, over Slack's 24", n)
	}
}

// failingRatedPoster lays home posts out but fails every rated one with err.
type failingRatedPoster struct {
	*fakeNotifyPoster
	err   error
	tries int
}

func (f *failingRatedPoster) PostNotifyMessage(space, thread string, m OutMessage) (string, string, error) {
	if m.Feedback != "" {
		f.tries++
		return "", "", f.err
	}
	return f.PostNotify(space, thread, m.Text)
}

// A rated home post the backend refuses posts as plain text; one that fails
// for any other reason (a timeout, an outage) may have landed, so it is not
// posted a second time.
func TestNotifyRatedHomePostFallsBackOnlyOnARefusal(t *testing.T) {
	for _, c := range []struct {
		name      string
		err       error
		wantPosts int
		wantError bool
	}{
		{"refused", fmt.Errorf("%w: fake", ErrLayoutRefused), 1, false},
		{"timed out", errors.New("context deadline exceeded"), 0, true},
	} {
		p := &failingRatedPoster{fakeNotifyPoster: &fakeNotifyPoster{landsIn: testHomeThread}, err: c.err}
		n, err := NewGchatNotifier(p, testHome, nil)
		if err != nil {
			t.Fatal(err)
		}
		n.SetConversations(&postFeedbackConversations{fakeConversations: &fakeConversations{}})
		got := serveJSON(t, n, lib.NotifyRequest{Text: "Nightly: 3 clusters healthy."})
		if p.tries != 1 || len(p.all()) != c.wantPosts || (got.Error != "") != c.wantError {
			t.Errorf("%s: rated tries = %d, plain posts = %d, reply = %+v; want 1, %d, error %v", c.name, p.tries, len(p.all()), got, c.wantPosts, c.wantError)
		}
	}
}
