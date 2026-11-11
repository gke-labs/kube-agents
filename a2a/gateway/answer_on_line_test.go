package gateway

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// lineAnsweringAdapter is the fake as a backend that takes a completed
// task's answer on its live message (LineAnswerer), as Google Chat does.
type lineAnsweringAdapter struct{ *fakeAdapter }

func (lineAnsweringAdapter) AnswersOnLine(string) bool { return true }

// startLineAnsweringRig is a rig on such a backend, with answer feedback on,
// so an answer carries its rating mark.
func startLineAnsweringRig(t *testing.T) *rig {
	t.Helper()
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, withFeedbackBucket(t, 24*time.Hour), func(a *fakeAdapter) Adapter {
		return lineAnsweringAdapter{a}
	})
	return r
}

// lineID is the id of the task's live message: the placeholder, the first
// post the turn made.
func lineID(t *testing.T, r *rig) string {
	t.Helper()
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	if len(r.adapter.posts) == 0 || !strings.Contains(r.adapter.posts[0].Text, "submitted") {
		t.Fatalf("posts = %+v, want the placeholder first", r.adapter.posts)
	}
	return r.adapter.posts[0].MessageID
}

// editsOf is every edit made to messageID, in order.
func editsOf(r *rig, messageID string) []string {
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	var out []string
	for _, e := range r.adapter.edits {
		if e.MessageID == messageID {
			out = append(out, e.Text)
		}
	}
	return out
}

// On a backend that answers on the line, a completed task's answer is the
// last edit of its live message, with its rating controls, and no post: the
// line never reads "✅ completed" under the ask, above the answer.
func TestAnAnswerOnLineBackendGetsTheAnswerOnTheLine(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line", false)
	line := lineID(t, r)
	waitFor(t, "the working edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "working")
	})
	completeTask(t, r.execFor(t, origin, "platform"), "the fleet is fine")
	waitFor(t, "the answer on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == "the fleet is fine"
	})
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "the fleet is fine") {
			t.Fatalf("posts = %q, want the answer on the line, not posted", r.adapter.postTexts())
		}
	}
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "completed") {
			t.Fatalf("edits = %q, want no terminal edit after the answer", r.adapter.editTexts())
		}
	}
	r.adapter.mu.Lock()
	feedbacks, marked := append([]string(nil), r.adapter.feedbacks...), append([]string(nil), r.adapter.markedTexts...)
	r.adapter.mu.Unlock()
	if len(feedbacks) != 1 || len(marked) != 1 || marked[0] != "the fleet is fine" {
		t.Fatalf("feedback marks = %q on %q, want one on the answer's edit", feedbacks, marked)
	}
}

// A laid-out answer is laid out on the line, as it would have been posted.
func TestAnAnswerOnLineBackendLaysTheAnswerOutOnTheLine(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-laid-out", true)
	line := lineID(t, r)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, testReport)}, false, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the laid-out answer on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == testReportText
	})
	chatEdits := r.adapter.chatEditsCopy()
	if len(chatEdits) == 0 || chatEdits[len(chatEdits)-1] == nil || chatEdits[len(chatEdits)-1].Headline != testReport.Headline {
		t.Fatalf("chat edits = %+v, want the report last", chatEdits)
	}
	if posts := r.adapter.chatPostsCopy(); len(posts) != 0 {
		t.Fatalf("chat posts = %+v, want none: the answer is on the line", posts)
	}
	if got := chatCount(t, r, chatRendered); got != 1 {
		t.Fatalf("rendered = %v, want 1", got)
	}
}

// A turn that handed the ask to cards puts its acknowledgement on the line,
// unrated: the card's report is the answer that is rated.
func TestAnAnswerOnLineBackendGetsTheHandOffLineOnTheLine(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-cards", false)
	line := lineID(t, r)
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishArtifact(context.Background(), lib.Artifact{Name: lib.ArtifactHandoff, Parts: []lib.Part{{Kind: "text", Text: lib.HandoffCards}}}); err != nil {
		t.Fatal(err)
	}
	completeTask(t, exec, "checking the fleet.")
	waitFor(t, "the acknowledgement on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == "checking the fleet."
	})
	if hasPost(r.adapter, "checking the fleet.") {
		t.Fatalf("posts = %q, want the acknowledgement on the line", r.adapter.postTexts())
	}
	r.adapter.mu.Lock()
	feedbacks := append([]string(nil), r.adapter.feedbacks...)
	r.adapter.mu.Unlock()
	if len(feedbacks) != 0 {
		t.Fatalf("feedback marks = %q, want none on a hand-off line", feedbacks)
	}
}

// A long text answer goes on the line whole: the line is one message, and
// the backend's own cap, not the chunk size, bounds it.
func TestAnAnswerOnLineBackendPutsALongAnswerOnTheLineWhole(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-long", false)
	line := lineID(t, r)
	long := strings.Repeat("the fleet is fine, cluster by cluster.\n", 120)
	if len(chatChunks(long, discordChunk)) < 3 {
		t.Fatalf("want an answer of several chunks")
	}
	completeTask(t, r.execFor(t, origin, "platform"), long)
	waitFor(t, "the whole answer on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == long
	})
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "cluster by cluster") {
			t.Fatalf("posts = %d, want no part of the answer posted", len(r.adapter.postTexts()))
		}
	}
	r.adapter.mu.Lock()
	marked := append([]string(nil), r.adapter.markedTexts...)
	r.adapter.mu.Unlock()
	if len(marked) != 1 || marked[0] != long {
		t.Fatalf("marked texts = %d, want the one edit", len(marked))
	}
}

// An edit that does not land leaves the answer to be posted as on any
// backend, with its rating controls, and the line takes the terminal edit.
func TestAnAnswerOnLineBackendPostsTheAnswerWhenTheEditFails(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-fails", false)
	line := lineID(t, r)
	waitFor(t, "the working edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "working")
	})
	r.adapter.mu.Lock()
	r.adapter.failEdits = 1
	r.adapter.mu.Unlock()
	completeTask(t, r.execFor(t, origin, "platform"), "the fleet is fine")
	waitFor(t, "the answer posted", postedContaining(r, "the fleet is fine"))
	waitFor(t, "the terminal edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "completed")
	})
	r.adapter.mu.Lock()
	marked := append([]string(nil), r.adapter.markedTexts...)
	r.adapter.mu.Unlock()
	if len(marked) != 2 || marked[1] != "the fleet is fine" {
		t.Fatalf("marked texts = %q, want the failed edit's mark and then the post's", marked)
	}
}

// A task its requester asked to stop, which completed anyway, keeps the
// post and the terminal edit: its line said "cancel sent", and a stale
// press rewrites such a line as canceled.
func TestAnAnswerOnLineBackendPostsTheAnswerOfAStoppedTask(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-stopped", false)
	line := lineID(t, r)
	rec := waitForRecord(t, r, "discord:g1/answer-on-line-stopped", "the task on record", func(rec *SessionRecord) bool {
		_, ok := rec.TaskRefFor(origin.TaskID)
		return ok
	})
	rec.MarkCanceled(origin.TaskID)
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	completeTask(t, r.execFor(t, origin, "platform"), "the fleet is fine")
	waitFor(t, "the answer posted", postedContaining(r, "the fleet is fine"))
	waitFor(t, "the terminal edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "completed")
	})
}

// A task that fails keeps its notice and its terminal edit: only an answer
// takes the line.
func TestAnAnswerOnLineBackendKeepsTheFailedLine(t *testing.T) {
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-failed", false)
	line := lineID(t, r)
	publishFinal(t, r, origin, "platform", lib.StateFailed, "the pod died")
	waitFor(t, "the failed edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "failed")
	})
	waitFor(t, "the failure notice", postedContaining(r, "the pod died"))
}

// Behind the mux the answer is the owning backend's: a conversation of a
// backend that answers on the line does, and one of a backend that does not
// keeps the post and the terminal edit.
func TestTheMuxAnswersOnLineForTheOwningBackend(t *testing.T) {
	mux, err := NewMultiAdapter("gchat", "console", map[string]Adapter{
		"gchat":   lineAnsweringAdapter{newFakeAdapter()},
		"console": newFakeAdapter(),
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !mux.AnswersOnLine("gchat:spaces/S1/threads/T1") {
		t.Error("a Google Chat conversation: want the answer on the line")
	}
	if mux.AnswersOnLine("console:abc") || mux.AnswersOnLine("discord:g/1") {
		t.Error("a console or unknown conversation: want the post and the terminal edit")
	}
	var _ LineAnswerer = mux
}

// The side door answers for the primary's conversations and keeps the line
// for its doors, whose callers read the line's edits as data.
func TestTheSideDoorKeepsTheTerminalEditForItsDoors(t *testing.T) {
	s := &sideDoorAdapter{
		primary: lineAnsweringAdapter{newFakeAdapter()},
		doors:   []DoorSpec{{Name: "inject", Prefix: "inject:", Door: stubDoor{}}},
	}
	if !s.AnswersOnLine("gchat:spaces/S1/threads/T1") {
		t.Error("the primary's conversation: want the answer on the line")
	}
	if s.AnswersOnLine("inject:case-1") {
		t.Error("a door's conversation: want the terminal edit kept")
	}
	if (&sideDoorAdapter{primary: newFakeAdapter()}).AnswersOnLine("discord:g/1") {
		t.Error("a primary that posts its answers: want the terminal edit kept")
	}
}

// The Google Chat adapter answers on the line: it draws no reaction and no
// Working… status, so the line is its receipt, and the answer is its end.
func TestGchatAnswersOnTheLine(t *testing.T) {
	a := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	var answerer LineAnswerer = a
	if !answerer.AnswersOnLine("gchat:spaces/S1/threads/T1") || !answerer.AnswersOnLine("gchat:spaces/S1") {
		t.Error("want the answer on the line in a thread and in a thread-less DM")
	}
}
