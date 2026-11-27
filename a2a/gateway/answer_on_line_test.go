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

// On a backend that answers on the line, a direct answer posts, with its
// rating mark, so it notifies, and the task's live message ends as the bare
// mark: nothing reads "completed" under the ask.
func TestAnAnswerOnLineBackendEndsTheLineWithTheBareMarkAfterTheAnswerPosts(t *testing.T) {
	t.Parallel()
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line", false)
	line := lineID(t, r)
	waitFor(t, "the working edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "working")
	})
	completeTask(t, r.execFor(t, origin, "platform"), "the fleet is fine")
	waitFor(t, "the answer posted", postedContaining(r, "the fleet is fine"))
	waitFor(t, "the bare mark on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == lineDoneMark
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "completed") || strings.Contains(e, "the fleet is fine") {
			t.Fatalf("edits = %q, want neither the word completed nor the answer on the line", r.adapter.editTexts())
		}
	}
	r.adapter.mu.Lock()
	marked := append([]string(nil), r.adapter.markedTexts...)
	r.adapter.mu.Unlock()
	if len(marked) != 1 || marked[0] != "the fleet is fine" {
		t.Fatalf("marked texts = %q, want the posted answer rated", marked)
	}
}

// A laid-out answer posts laid out, as on any backend, and the line ends as
// the bare mark.
func TestAnAnswerOnLineBackendPostsALaidOutAnswer(t *testing.T) {
	t.Parallel()
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-laid-out", true)
	line := lineID(t, r)
	publishArtifactUpdate(t, r, origin, lib.ArtifactResult, []lib.Part{{Kind: "text", Text: testReportText}, chatPart(t, testReport)}, false, true)
	publishFinal(t, r, origin, "platform", lib.StateCompleted, "")
	waitFor(t, "the laid-out answer posted", func() bool { return len(r.adapter.chatPostsCopy()) == 1 })
	waitFor(t, "the bare mark on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == lineDoneMark
	})
	if got := r.adapter.chatPostsCopy()[0]; got.Headline != testReport.Headline {
		t.Fatalf("chat post = %+v, want the report", got)
	}
}

// A turn that handed the ask to cards puts its acknowledgement on the line,
// unrated, and no post follows: the card's plan and report do.
func TestAnAnswerOnLineBackendGetsTheHandOffLineOnTheLine(t *testing.T) {
	t.Parallel()
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
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "completed") || e == lineDoneMark {
			t.Fatalf("edits = %q, want no terminal edit after the acknowledgement", r.adapter.editTexts())
		}
	}
	r.adapter.mu.Lock()
	feedbacks := append([]string(nil), r.adapter.feedbacks...)
	r.adapter.mu.Unlock()
	if len(feedbacks) != 0 {
		t.Fatalf("feedback marks = %q, want none on a hand-off line", feedbacks)
	}
}

// An acknowledgement edit that does not land is posted as on any backend,
// and the line ends as the bare mark.
func TestAnAnswerOnLineBackendPostsTheAcknowledgementWhenTheEditFails(t *testing.T) {
	t.Parallel()
	r := startLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/answer-on-line-fails", false)
	line := lineID(t, r)
	waitFor(t, "the working edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "working")
	})
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishArtifact(context.Background(), lib.Artifact{Name: lib.ArtifactHandoff, Parts: []lib.Part{{Kind: "text", Text: lib.HandoffCards}}}); err != nil {
		t.Fatal(err)
	}
	r.adapter.mu.Lock()
	r.adapter.failEdits = 1
	r.adapter.mu.Unlock()
	completeTask(t, exec, "checking the fleet.")
	waitFor(t, "the acknowledgement posted", postedContaining(r, "checking the fleet."))
	waitFor(t, "the bare mark on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == lineDoneMark
	})
}

// A task that fails keeps its notice and its terminal edit: only a
// completed task ends as the bare mark.
func TestAnAnswerOnLineBackendKeepsTheFailedLine(t *testing.T) {
	t.Parallel()
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

// A task its requester asked to stop, which completed anyway, keeps the
// terminal edit it had: its line said "cancel sent", and a stale press
// rewrites such a line as canceled, so neither an acknowledgement nor the
// bare mark is written there, and its line ends stopped. Nothing a stopped
// task writes reaches the room, so its acknowledgement isn't posted either.
func TestAnAnswerOnLineBackendKeepsTheTerminalEditOfAStoppedTask(t *testing.T) {
	t.Parallel()
	r := startLineAnsweringRig(t)
	conv := "discord:g1/answer-on-line-stopped"
	origin := startChatTask(t, r, conv, false)
	line := lineID(t, r)
	waitFor(t, "the working edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], "working")
	})
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishArtifact(context.Background(), lib.Artifact{Name: lib.ArtifactHandoff, Parts: []lib.Part{{Kind: "text", Text: lib.HandoffCards}}}); err != nil {
		t.Fatal(err)
	}
	say(r, conv, "m-stop", "stop")
	waitForRecord(t, r, conv, "the task marked cancelled", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(origin.TaskID)
		return ok && ref.Canceled
	})
	completeTask(t, exec, "checking the fleet.")
	// Its line keeps reading stopped: a completed with no answer would read
	// as an answer that never came.
	waitFor(t, "the terminal edit", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], terminalLine(lib.StateCanceled, ""))
	})
	for _, e := range editsOf(r, line) {
		if strings.Contains(e, "completed") {
			t.Fatalf("a stopped task's line read completed: %q", e)
		}
	}
	if postedContaining(r, "checking the fleet.")() {
		t.Fatal("a stopped task's acknowledgement was posted")
	}
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
// Working… status, so the line is its receipt, and it ends as a reaction
// would come off.
func TestGchatAnswersOnTheLine(t *testing.T) {
	a := newTestGchatAdapterWithRelay(t, newFakeChatRelay(t))
	var answerer LineAnswerer = a
	if !answerer.AnswersOnLine("gchat:spaces/S1/threads/T1") || !answerer.AnswersOnLine("gchat:spaces/S1") {
		t.Error("want the answer on the line in a thread and in a thread-less DM")
	}
}

// wholeLineAnsweringAdapter is the fake as a backend that takes a direct
// answer whole on the task's live message when it fits one piece
// (WholeLineAnswerer, PieceSizer), as Teams does.
type wholeLineAnsweringAdapter struct{ *fakeAdapter }

func (wholeLineAnsweringAdapter) AnswersOnLine(string) bool      { return true }
func (wholeLineAnsweringAdapter) AnswersWholeOnLine(string) bool { return true }
func (wholeLineAnsweringAdapter) PieceSize(string) int           { return 60 }

func startWholeLineAnsweringRig(t *testing.T) *rig {
	t.Helper()
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, func(*Config) {}, func(a *fakeAdapter) Adapter {
		return wholeLineAnsweringAdapter{a}
	})
	return r
}

// On a backend that takes the whole answer on the line, a direct answer
// within one piece is edited onto the task's live message: no post carries
// it, and the line does not end as the bare mark.
func TestAWholeLineAnswerBackendEditsAFittingAnswerOntoTheLine(t *testing.T) {
	t.Parallel()
	r := startWholeLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/whole-answer-on-line", false)
	line := lineID(t, r)
	completeTask(t, r.execFor(t, origin, "platform"), "the fleet is fine")
	waitFor(t, "the answer edited onto the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == "the fleet is fine"
	})
	if postedContaining(r, "the fleet is fine")() {
		t.Fatalf("posts = %+v, want the answer on the line only", r.adapter.posts)
	}
	for _, e := range editsOf(r, line) {
		if e == lineDoneMark {
			t.Fatalf("edits = %q, want no bare mark on a line that carries the answer", editsOf(r, line))
		}
	}
}

// Once a turn answer has been posted under the line (a follow-up during
// the task), the final answer posts too: edited onto the line it would sit
// above the follow-up it answers, and an edit raises no notification.
func TestAWholeLineAnswerBackendPostsTheAnswerAfterATurnWasPosted(t *testing.T) {
	t.Parallel()
	r := startWholeLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/whole-answer-after-turn", false)
	line := lineID(t, r)
	exec := r.execFor(t, origin, "platform")
	_ = exec.PublishStatus(context.Background(), lib.StateWorking, false)
	publishTurnAnswer(t, r, origin, "platform", 1, "first answer")
	waitFor(t, "turn posted", postedContaining(r, "first answer"))
	completeTask(t, exec, "the fleet is fine")
	waitFor(t, "the answer posted under the turn", postedContaining(r, "the fleet is fine"))
	for _, e := range editsOf(r, line) {
		if e == "the fleet is fine" {
			t.Fatalf("edits = %q, want the answer posted, not edited above the turn", editsOf(r, line))
		}
	}
}

// The piece size is bytes, the unit the chunker and the activity budget
// use: an answer under the piece in runes but over it in bytes posts
// rather than being edited onto the line and cut by the activity cap.
func TestAWholeLineAnswerBackendMeasuresThePieceInBytes(t *testing.T) {
	t.Parallel()
	r := startWholeLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/whole-answer-multibyte", false)
	line := lineID(t, r)
	wide := strings.Repeat("艦", 30) // 30 runes, 90 bytes, past the fake's 60
	completeTask(t, r.execFor(t, origin, "platform"), wide)
	waitFor(t, "the answer posted", postedContaining(r, strings.Repeat("艦", 10)))
	for _, e := range editsOf(r, line) {
		if strings.Contains(e, "艦") {
			t.Fatalf("edits = %q, want a 90-byte answer posted, not edited onto a 60-byte piece", editsOf(r, line))
		}
	}
}

// An answer past the backend's piece size posts, in pieces, and the line
// ends as the bare mark, as on a backend that answers on the line.
func TestAWholeLineAnswerBackendPostsAnAnswerPastOnePiece(t *testing.T) {
	t.Parallel()
	r := startWholeLineAnsweringRig(t)
	origin := startChatTask(t, r, "discord:g1/whole-answer-too-long", false)
	line := lineID(t, r)
	long := strings.Repeat("a line of the fleet report\n", 6) // 162 runes, past the fake's 60
	completeTask(t, r.execFor(t, origin, "platform"), long)
	waitFor(t, "the answer posted in pieces", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		n := 0
		for _, p := range r.adapter.posts {
			if strings.Contains(p.Text, "a line of the fleet report") {
				n++
			}
		}
		return n >= 2
	})
	waitFor(t, "the bare mark on the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == lineDoneMark
	})
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	for _, p := range r.adapter.posts {
		if n := len([]rune(p.Text)); n > 60 {
			t.Fatalf("a post of %d runes; want every piece within the backend's 60", n)
		}
	}
}

// Behind the mux, the whole-line answer and the piece size are the owning
// backend's; a backend without them keeps the post and Discord's piece.
func TestTheMuxTakesTheWholeLineAnswerAndPieceSizeFromTheOwningBackend(t *testing.T) {
	mux, err := NewMultiAdapter("teams", "console", map[string]Adapter{
		"teams":   wholeLineAnsweringAdapter{newFakeAdapter()},
		"console": newFakeAdapter(),
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !mux.AnswersWholeOnLine("teams:19:abc@thread.tacv2") || mux.PieceSize("teams:19:abc@thread.tacv2") != 60 {
		t.Error("a Teams conversation: want the whole answer on the line and the backend's piece size")
	}
	if mux.AnswersWholeOnLine("console:abc") || mux.PieceSize("console:abc") != discordChunk || mux.PieceSize("discord:g/1") != discordChunk {
		t.Error("a console or unknown conversation: want the post and Discord's piece")
	}
	s := &sideDoorAdapter{
		primary: wholeLineAnsweringAdapter{newFakeAdapter()},
		doors:   []DoorSpec{{Name: "inject", Prefix: "inject:", Door: stubDoor{}}},
	}
	if !s.AnswersWholeOnLine("teams:19:abc@thread.tacv2") || s.PieceSize("teams:19:abc@thread.tacv2") != 60 {
		t.Error("the primary's conversation: want the primary's whole-line answer and piece size")
	}
	if s.AnswersWholeOnLine("inject:case-1") || s.PieceSize("inject:case-1") != discordChunk {
		t.Error("a door's conversation: want the terminal edit kept and Discord's piece")
	}
	var _ WholeLineAnswerer = mux
	var _ PieceSizer = mux
}

// A whole answer taken on the line keeps its feedback mark, so the backend
// draws the rating controls on the line as it would on a posted answer;
// without the mark an answer that fits one piece could never be rated.
func TestAWholeLineAnswerBackendCarriesTheFeedbackMarkOntoTheLine(t *testing.T) {
	t.Parallel()
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, withFeedbackBucket(t, 24*time.Hour), func(a *fakeAdapter) Adapter {
		return wholeLineAnsweringAdapter{a}
	})
	origin := startChatTask(t, r, "discord:g1/whole-answer-rated-on-line", false)
	line := lineID(t, r)
	completeTask(t, r.execFor(t, origin, "platform"), "the fleet is fine")
	waitFor(t, "the answer edited onto the line", func() bool {
		edits := editsOf(r, line)
		return len(edits) > 0 && edits[len(edits)-1] == "the fleet is fine"
	})
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	if len(r.adapter.editFeedbacks) != 1 || r.adapter.editFeedbacks[0] != origin.TaskID {
		t.Fatalf("feedback marks on edits = %q, want the task's on the one answering edit", r.adapter.editFeedbacks)
	}
}

// A rated answer past the piece size posts in pieces of the backend's size
// too: the rated path (postMessage) reads PieceSizer as the plain one does.
func TestAWholeLineAnswerBackendSplitsARatedAnswerAtItsPieceSize(t *testing.T) {
	t.Parallel()
	r, _ := startRigWithSpawnerAdapter(t, "platform", 0, withFeedbackBucket(t, 24*time.Hour), func(a *fakeAdapter) Adapter {
		return wholeLineAnsweringAdapter{a}
	})
	origin := startChatTask(t, r, "discord:g1/whole-answer-rated-too-long", false)
	long := strings.Repeat("a line of the fleet report\n", 6) // 162 runes, past the fake's 60 and under discordChunk
	completeTask(t, r.execFor(t, origin, "platform"), long)
	waitFor(t, "the rated answer posted in pieces", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		n := 0
		for _, p := range r.adapter.posts {
			if strings.Contains(p.Text, "a line of the fleet report") {
				n++
			}
		}
		return n >= 2
	})
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	for _, p := range r.adapter.posts {
		if n := len([]rune(p.Text)); n > 60 {
			t.Fatalf("a rated post of %d runes; want every piece within the backend's 60", n)
		}
	}
}
