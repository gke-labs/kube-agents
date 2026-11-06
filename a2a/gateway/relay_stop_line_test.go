package gateway

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A task's live message carries OutMessage.Stop from the placeholder through
// every live edit, text or laid out, and the terminal edit arrives without
// it: a backend that draws a Stop control draws it while the task runs and
// takes it off when the task ends.
func TestTheLiveLineCarriesStopUntilTheTerminal(t *testing.T) {
	r := startRig(t)
	origin := startChatTask(t, r, "discord:g1/stop-line", true)
	waitFor(t, "the working line with Stop", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) >= 2 && strings.Contains(lines[0].Text, "submitted") && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "reading versions"}, chatPart(t, testPlan)}, false, false)
	waitFor(t, "the laid-out line with Stop", func() bool {
		edits := r.adapter.chatEditsCopy()
		return len(edits) > 0 && edits[len(edits)-1] != nil
	})
	laidOut := len(r.adapter.stopLinesCopy())
	completeTask(t, r.execFor(t, origin, "platform"), "done")
	waitFor(t, "the terminal edit", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "completed")
	})
	lines := r.adapter.stopLinesCopy()
	if len(lines) < 3 || laidOut < 3 {
		t.Fatalf("stop lines = %+v (laid out at %d)", lines, laidOut)
	}
	for _, l := range lines {
		if l.Conversation != "discord:g1/stop-line" || strings.Contains(l.Text, "completed") {
			t.Errorf("the terminal edit, or another conversation's line, carried Stop: %+v", l)
		}
	}
}

// A task the heal ends (here one that never started inside the grace) ends
// its live message too: the terminal line, with no Stop on it, so a line
// that read working with a Stop control does not outlive its task.
func TestTheHealEndsTheLiveLineWithoutStop(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, func(c *Config) {
		c.SessionReuse = true
		c.FirstEventGrace = time.Second
	})
	ctx := context.Background()
	pod := "chat-heron-line"
	rec := &SessionRecord{Key: "discord:g1/t-heal-line", ContextID: "ctx-hl", Kind: "group",
		Addressee: pod, BusSession: pod, PodName: pod, PodReuse: true, SessionRouted: true, Profile: "chat",
		ActiveTask: &ActiveTask{TaskID: "task-hl", CorrelationID: "corr-hl", StatusMsgID: "m-stale", LineNote: "(note)", SubmittedAt: time.Now().Add(-time.Minute)},
		Tasks:      []TaskRef{{ID: "task-hl", Addressee: pod}}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	spawn.setReusable(pod)
	say(r, rec.Key, "m1", "hello?")
	waitFor(t, "the stale line ended", func() bool {
		r.adapter.mu.Lock()
		edits := append([]fakePost(nil), r.adapter.edits...)
		r.adapter.mu.Unlock()
		for _, e := range edits {
			if e.MessageID == "m-stale" && strings.Contains(e.Text, "failed") && strings.HasSuffix(e.Text, "(note)") {
				return true
			}
		}
		return false
	})
	for _, l := range r.adapter.stopLinesCopy() {
		if l.MessageID == "m-stale" {
			t.Fatalf("the heal's terminal edit carried Stop: %+v", l)
		}
	}
}

// A stopped task whose terminal has not arrived when the next ask starts
// keeps its line otherwise, with its Stop control on it: the new turn
// closes it as canceled, without Stop, and the new task's line carries the
// control instead.
func TestANewTurnClosesASupersededStoppedLine(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/superseded-line"
	startChatTask(t, r, conv, false)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-stop", Text: "stop"}
	waitFor(t, "the cancel notice", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-2", Text: "and now the nodes"}
	r.awaitTask(t, "platform")
	first := r.adapter.stopLinesCopy()[0]
	waitFor(t, "the old line closed", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		for _, e := range r.adapter.edits {
			if e.MessageID == first.MessageID && strings.Contains(e.Text, "canceled") {
				return true
			}
		}
		return false
	})
	lines := r.adapter.stopLinesCopy()
	last := lines[len(lines)-1]
	if last.MessageID == first.MessageID || !strings.Contains(last.Text, "submitted") {
		t.Fatalf("stop lines = %+v; want the new task's placeholder last", lines)
	}
	for _, l := range lines {
		if l.MessageID == first.MessageID && strings.Contains(l.Text, "canceled") {
			t.Fatalf("the closing edit carried Stop: %+v", l)
		}
	}
}

// A Stop press on Google Chat answers on the task's own line: the cancel a
// typed stop sends, no "cancel sent" post, the line edited to "cancel sent"
// with no Stop, left so by later progress, and the terminal edit making it
// "canceled" with no "canceled" notice posted. A second press changes
// nothing.
func TestAChatStopPressAnswersOnTheLine(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-press"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	line := r.adapter.stopLinesCopy()[0].MessageID
	press := func(id string) {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: id, Intent: IntentCancel, TaskID: origin.TaskID, Click: true}
	}
	press(line + "#t1")
	waitFor(t, "the line closed on cancel sent", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "cancel sent") {
				return true
			}
		}
		return false
	})
	edits := len(r.adapter.editTexts())
	press(line + "#t2")
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "still going"}}, false, false)
	if err := exec.PublishStatus(context.Background(), lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal edit", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "canceled")
	})
	texts := r.adapter.editTexts()
	if len(texts) != edits+1 {
		t.Errorf("edits after the press = %q; want the terminal edit alone", texts[edits:])
	}
	for _, l := range r.adapter.stopLinesCopy() {
		if strings.Contains(l.Text, "cancel sent") || strings.Contains(l.Text, "canceled") {
			t.Errorf("a closed line carried Stop: %+v", l)
		}
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") {
			t.Errorf("a press posted %q; the line is the reply", p)
		}
	}
}

// A press on a task that is not the running one (a line whose closing edit
// failed, or one from before a restart) is answered on the line it was
// pressed on, and cancels nothing: the running task keeps its line and its
// Stop, and ends as it would have.
func TestAStalePressAnswersOnItsOwnLineAndCancelsNothing(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-stale"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com",
		MessageID: "spaces/S1/messages/OLD-LINE#2026-11-06T15:00:00Z", Intent: IntentCancel, TaskID: "task-gone", Click: true}
	waitFor(t, "the stale line answered", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		for _, e := range r.adapter.edits {
			if e.MessageID == "spaces/S1/messages/OLD-LINE" && strings.Contains(e.Text, "has ended") {
				return true
			}
		}
		return false
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "cancel sent") || strings.Contains(e, "canceled") {
			t.Fatalf("a stale press touched the running task: %q", e)
		}
	}
	for _, l := range r.adapter.stopLinesCopy() {
		if l.MessageID == "spaces/S1/messages/OLD-LINE" {
			t.Fatalf("the stale line kept its Stop: %+v", l)
		}
	}
	completeTask(t, exec, "done")
	waitFor(t, "the running task's terminal", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "completed")
	})
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") {
			t.Errorf("a stale press posted %q", p)
		}
	}
}

// A press on a task with no status line (the placeholder post failed) has
// no line to answer on, so it speaks as the typed stop does: the "cancel
// sent" post, and no quiet mark, so the relay's "canceled" follows.
func TestAPressWithNoLineSpeaksAsATypedStop(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	ctx := context.Background()
	conv := "gchat:spaces/S1/threads/T-noline"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-noline", Kind: "group", Addressee: "platform",
		ActiveTask: &ActiveTask{TaskID: "task-noline", CorrelationID: "corr-noline", SubmittedAt: time.Now()},
		Tasks:      []TaskRef{{ID: "task-noline", Addressee: "platform"}}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com",
		MessageID: "spaces/S1/messages/L1#2026-11-06T15:00:00Z", Intent: IntentCancel, TaskID: "task-noline", Click: true}
	waitFor(t, "the cancel sent post", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	got := waitForRecord(t, r, conv, "the detached task", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	if ref, ok := got.TaskRefFor("task-noline"); !ok || ref.QuietCancel {
		t.Fatalf("a press with no line was quiet: %+v", ref)
	}
	if len(r.adapter.editTexts()) != 0 {
		t.Errorf("edits = %q; want none without a line", r.adapter.editTexts())
	}
}

// A stale press on a task the record cancelled re-renders the canceled line
// (the superseding turn's closing edit may have failed); one on a task the
// record knows ended otherwise leaves the line its end wrote, since the
// terminal edit may have landed a moment before the press.
func TestAStalePressOnAKnownTaskKeepsOrRestoresItsTerminalLine(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-known"
	say := func(id, text string) {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: id, Text: text}
	}
	say("spaces/S1/messages/M1", "first ask")
	first := r.awaitTask(t, "platform")
	if err := r.execFor(t, first, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the first line", func() bool { return len(r.adapter.stopLinesCopy()) >= 2 })
	firstLine := r.adapter.stopLinesCopy()[0].MessageID
	say("spaces/S1/messages/M2", "stop")
	waitFor(t, "the line closed on cancel sent", func() bool {
		return slices.ContainsFunc(r.adapter.editTexts(), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	say("spaces/S1/messages/M3", "second ask")
	second := awaitTaskAfter(t, r, "platform", first)
	countCanceled := func() int {
		n := 0
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		for _, e := range r.adapter.edits {
			if e.MessageID == firstLine && strings.Contains(e.Text, "canceled") {
				n++
			}
		}
		return n
	}
	waitFor(t, "the superseded line closed", func() bool { return countCanceled() == 1 })
	// A press on the first task's line: the record cancelled it, so the
	// canceled line is written again, and nothing else changes.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com",
		MessageID: firstLine + "#2026-11-06T16:00:00Z", Intent: IntentCancel, TaskID: first.TaskID, Click: true}
	waitFor(t, "the canceled line restored", func() bool { return countCanceled() == 2 })
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "has ended") {
			t.Fatalf("a known task's stale press wrote the unknown-task notice: %q", e)
		}
	}
	// The second task ends on its own; a press on its line afterwards
	// leaves the terminal edit alone.
	secondExec := r.execFor(t, second, "platform")
	if err := secondExec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the second working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working") && lines[len(lines)-1].MessageID != firstLine
	})
	completeTask(t, secondExec, "done")
	secondLine := r.adapter.stopLinesCopy()[len(r.adapter.stopLinesCopy())-1].MessageID
	waitFor(t, "the second terminal "+secondLine+" in "+fmt.Sprint(r.adapter.editTexts())+" posts "+fmt.Sprint(r.adapter.postTexts()), func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		for _, e := range r.adapter.edits {
			if e.MessageID == secondLine && strings.Contains(e.Text, "completed") {
				return true
			}
		}
		return false
	})
	edits := len(r.adapter.editTexts())
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com",
		MessageID: secondLine + "#2026-11-06T16:00:01Z", Intent: IntentCancel, TaskID: second.TaskID, Click: true}
	say("spaces/S1/messages/M4", "third ask")
	r.awaitTask(t, "platform")
	for _, e := range r.adapter.editTexts()[edits:] {
		if strings.Contains(e, "has ended") || strings.Contains(e, "canceled") {
			t.Fatalf("a press on an ended task's line rewrote it: %q", e)
		}
	}
}

// A press whose closing edit failed leaves the line with its button and the
// task detached; the next press retries the close, and the relay's progress
// edits of a detached task carry no Stop meanwhile. A press whose message is
// not the task's own line (a delegating parent's) closes that message too.
func TestAPressRetriesAFailedCloseAndClosesThePressedLine(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-retry"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	line := r.adapter.stopLinesCopy()[0].MessageID
	r.adapter.mu.Lock()
	r.adapter.failEdits = 1
	r.adapter.mu.Unlock()
	press := func(message, when string) {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com",
			MessageID: message + "#" + when, Intent: IntentCancel, TaskID: origin.TaskID, Click: true}
	}
	press(line, "t1")
	waitForRecord(t, r, conv, "the detached task", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.Detached && !rec.ActiveTask.LineClosed
	})
	stops := len(r.adapter.stopLinesCopy())
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "still going"}}, false, false)
	waitFor(t, "the progress edit without Stop", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "working")
	})
	if len(r.adapter.stopLinesCopy()) != stops {
		t.Fatalf("a detached task's progress edit carried Stop: %+v", r.adapter.stopLinesCopy()[stops:])
	}
	press("spaces/S1/messages/PARENT", "t2")
	waitFor(t, "the retried close and the pressed line", func() bool {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		own, pressed := false, false
		for _, e := range r.adapter.edits {
			if strings.Contains(e.Text, "cancel sent") {
				own = own || e.MessageID == line
				pressed = pressed || e.MessageID == "spaces/S1/messages/PARENT"
			}
		}
		return own && pressed
	})
	waitForRecord(t, r, conv, "the closed line", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.LineClosed
	})
	for _, l := range r.adapter.stopLinesCopy() {
		if strings.Contains(l.Text, "cancel sent") {
			t.Fatalf("a closing edit carried Stop: %+v", l)
		}
	}
}

// With the card stop armed, a press on the running task takes the typed
// stop's path: the cancel sent post, the cards stopped, their replies.
func TestAPressWithTheCardStopArmedIsATypedStop(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false, func(c *Config) {
		c.CardStop = true
		c.CardStopTimeout = 200 * time.Millisecond
	})
	conv := "gchat:spaces/S1/threads/T-cards"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool { return len(r.adapter.stopLinesCopy()) >= 2 })
	line := r.adapter.stopLinesCopy()[0].MessageID
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com",
		MessageID: line + "#t1", Intent: IntentCancel, TaskID: origin.TaskID, Click: true}
	waitFor(t, "the typed stop's posts", func() bool {
		posts := r.adapter.postTexts()
		return slices.ContainsFunc(posts, func(s string) bool { return strings.Contains(s, "cancel sent") }) &&
			slices.ContainsFunc(posts, func(s string) bool { return strings.Contains(s, "cards") })
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "cancel sent") {
			t.Fatalf("the card-stop press closed the line as a quiet press would: %q", e)
		}
	}
}

// awaitTaskAfter is rig.awaitTask for the next submission on addressee after
// seen: awaitTask returns the subject's first, which is seen again.
func awaitTaskAfter(t *testing.T, r *rig, addressee string, seen *lib.Envelope) *lib.Envelope {
	t.Helper()
	var env *lib.Envelope
	waitFor(t, "a later task submission on "+addressee, func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, addressee) {
			if e.Kind == lib.KindMessage && e.TaskID != seen.TaskID {
				env = e
				return true
			}
		}
		return false
	})
	return env
}

// A stop typed on Google Chat with a task running and a line answers as a
// press does: the quiet cancel, no "cancel sent" post, the line edited to
// "cancel sent" with no Stop, left so by later progress; a second typed
// stop moves "cancel already sent" onto the line; and the terminal edit
// makes it "canceled" with no "canceled" notice posted. The typed messages
// themselves are not edited.
func TestAChatTypedStopAnswersOnTheLine(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	conv := "gchat:dm/spaces/S1/threads/T-typed"
	say := func(id, text string) {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: sessionKindDM, AuthorID: "user@example.com", MessageID: id, Text: text}
	}
	say("spaces/S1/messages/M1", "explain node upgrades")
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	line := r.adapter.stopLinesCopy()[0].MessageID
	editsOf := func(id string) []string {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		var out []string
		for _, e := range r.adapter.edits {
			if e.MessageID == id {
				out = append(out, e.Text)
			}
		}
		return out
	}
	say("spaces/S1/messages/M2", "stop")
	waitFor(t, "the line closed on cancel sent", func() bool {
		return slices.ContainsFunc(editsOf(line), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	got := waitForRecord(t, r, conv, "the quiet, closed task", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.Detached && rec.ActiveTask.LineClosed
	})
	if ref, ok := got.TaskRefFor(origin.TaskID); !ok || !ref.QuietCancel {
		t.Fatalf("a typed stop with a line was not quiet: %+v", ref)
	}
	publishArtifactUpdate(t, r, origin, lib.ArtifactProgress, []lib.Part{{Kind: "text", Text: "still going"}}, false, false)
	say("spaces/S1/messages/M3", "stop")
	waitFor(t, "the line on cancel already sent", func() bool {
		return slices.ContainsFunc(editsOf(line), func(s string) bool { return strings.Contains(s, "cancel already sent") })
	})
	edits := len(r.adapter.editTexts())
	if err := exec.PublishStatus(context.Background(), lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the terminal edit", func() bool {
		texts := editsOf(line)
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "canceled")
	})
	if texts := r.adapter.editTexts(); len(texts) != edits+1 {
		t.Errorf("edits after the second stop = %q; want the terminal edit alone", texts[edits:])
	}
	for _, e := range editsOf(line) {
		if strings.Contains(e, "working") && strings.Contains(e, "still going") {
			t.Errorf("progress moved the closed line: %q", e)
		}
	}
	r.adapter.mu.Lock()
	for _, e := range r.adapter.edits {
		if e.MessageID != line {
			t.Errorf("a typed stop edited a message that is not the line: %+v", e)
		}
	}
	r.adapter.mu.Unlock()
	for _, l := range r.adapter.stopLinesCopy() {
		if strings.Contains(l.Text, "cancel") {
			t.Errorf("a closed line carried Stop: %+v", l)
		}
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") {
			t.Errorf("a typed stop posted %q; the line is the reply", p)
		}
	}
}

// A stop typed on Google Chat on a task with no status line has no line to
// answer on, so it keeps the typed stop's replies: the "cancel sent" post,
// "cancel already sent" for the second, and no quiet mark, so the relay's
// "canceled" follows.
func TestAChatTypedStopWithNoLineKeepsItsPosts(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	ctx := context.Background()
	conv := "gchat:spaces/S1/threads/T-typed-noline"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-typed-noline", Kind: "group", Addressee: "platform",
		ActiveTask: &ActiveTask{TaskID: "task-typed-noline", CorrelationID: "corr-typed-noline", SubmittedAt: time.Now()},
		Tasks:      []TaskRef{{ID: "task-typed-noline", Addressee: "platform"}}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	say := func(id, text string) {
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: id, Text: text}
	}
	say("spaces/S1/messages/M2", "stop")
	waitFor(t, "the cancel sent post", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	got := waitForRecord(t, r, conv, "the detached task", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	if ref, ok := got.TaskRefFor("task-typed-noline"); !ok || ref.QuietCancel {
		t.Fatalf("a typed stop with no line was quiet: %+v", ref)
	}
	say("spaces/S1/messages/M3", "stop")
	waitFor(t, "the cancel already sent post", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "cancel already sent") })
	})
	if len(r.adapter.editTexts()) != 0 {
		t.Errorf("edits = %q; want none without a line", r.adapter.editTexts())
	}
}

// With the card stop armed, a stop typed on Google Chat keeps the typed
// stop's path: the "cancel sent" post, the cards stopped, their replies,
// and the line left to the relay.
func TestAChatTypedStopWithTheCardStopArmedKeepsItsPosts(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false, func(c *Config) {
		c.CardStop = true
		c.CardStopTimeout = 200 * time.Millisecond
	})
	conv := "gchat:spaces/S1/threads/T-typed-cards"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool { return len(r.adapter.stopLinesCopy()) >= 2 })
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M2", Text: "stop"}
	waitFor(t, "the typed stop's posts", func() bool {
		posts := r.adapter.postTexts()
		return slices.ContainsFunc(posts, func(s string) bool { return strings.Contains(s, "cancel sent") }) &&
			slices.ContainsFunc(posts, func(s string) bool { return strings.Contains(s, "cards") })
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "cancel sent") {
			t.Fatalf("the armed typed stop closed the line as a quiet stop would: %q", e)
		}
	}
}

// A stop typed on Discord keeps today's replies, line or no line: the
// "cancel sent" post, the line moving on with no Stop, and the relay's
// "canceled" notice beside the terminal edit. Discord draws no button, and
// the posts are its contract.
func TestADiscordTypedStopKeepsItsPosts(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/typed-stop"
	origin := startChatTask(t, r, conv, false)
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "d-stop", Text: "stop"}
	waitFor(t, "the cancel sent post", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "cancel sent") })
	})
	got := waitForRecord(t, r, conv, "the detached task", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	if ref, ok := got.TaskRefFor(origin.TaskID); !ok || ref.QuietCancel || got.ActiveTask.LineClosed {
		t.Fatalf("a Discord typed stop took the quiet path: %+v, line closed %v", ref, got.ActiveTask.LineClosed)
	}
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the canceled notice", func() bool {
		return slices.ContainsFunc(r.adapter.postTexts(), func(s string) bool { return strings.Contains(s, "canceled") })
	})
	waitFor(t, "the terminal edit", func() bool {
		texts := r.adapter.editTexts()
		return len(texts) > 0 && strings.Contains(texts[len(texts)-1], "canceled")
	})
	for _, e := range r.adapter.editTexts() {
		if strings.Contains(e, "cancel sent") {
			t.Errorf("a Discord typed stop edited the line to %q", e)
		}
	}
}

// A typed stop on Google Chat whose cancel does not reach the bus is said
// on the line: "could not send the stop", with the Stop control kept for
// another try, and nothing posted. The task stays attached and unmarked.
func TestAChatTypedStopThatCannotSendSaysSoOnTheLine(t *testing.T) {
	r := startGchatRig(t, []string{"user@example.com"}, false)
	conv := "gchat:spaces/S1/threads/T-typed-nosend"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M1", Text: "explain node upgrades"}
	origin := r.awaitTask(t, "platform")
	if err := r.execFor(t, origin, "platform").PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the working line", func() bool {
		lines := r.adapter.stopLinesCopy()
		return len(lines) > 0 && strings.Contains(lines[len(lines)-1].Text, "working")
	})
	line := r.adapter.stopLinesCopy()[0].MessageID
	deleteTasksStream(t, r.url)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "user@example.com", MessageID: "spaces/S1/messages/M2", Text: "stop"}
	waitFor(t, "the failure on the line with its Stop", func() bool {
		return slices.ContainsFunc(r.adapter.stopLinesCopy(), func(l fakePost) bool {
			return l.MessageID == line && strings.Contains(l.Text, "could not send the stop")
		})
	})
	got, _ := r.g.reg.Get(context.Background(), conv)
	if got == nil || got.ActiveTask == nil || got.ActiveTask.Detached || got.ActiveTask.LineClosed {
		t.Fatalf("record after a failed cancel: %+v", got)
	}
	if ref, _ := got.TaskRefFor(origin.TaskID); ref.Canceled || ref.QuietCancel {
		t.Fatalf("a failed cancel was recorded: %+v", ref)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "🛑") || strings.Contains(p, "could not send") {
			t.Errorf("a failed typed stop posted %q; the line is the reply", p)
		}
	}
}
