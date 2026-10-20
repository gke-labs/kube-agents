package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The card stop (gke-labs#3263, cardstop.go): an armed stop sends the
// default addressee a control whose message is the cardStop part alone, and
// words the executor's answer.

// cardStopRig is a rig with the card stop armed at the given bound (0: the
// default).
func cardStopRig(t *testing.T, timeout time.Duration) *rig {
	t.Helper()
	return startRigWith(t, func(c *Config) {
		c.CardStop = true
		c.CardStopTimeout = timeout
	})
}

// ranATurn runs one turn on conv to its end, so the conversation has history
// and nothing active: the shape a stop after a card was filed meets.
func ranATurn(t *testing.T, r *rig, conv string) *lib.Envelope {
	t.Helper()
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: conv + "-ask", Text: "check the pods on x"}
	origin := awaitSubmission(t, r, "platform", 0)
	completeTask(t, r.execFor(t, origin, "platform"), "checking the pods on x")
	waitForRecord(t, r, conv, "the turn's terminal to release the conversation", func(rec *SessionRecord) bool {
		return rec.ActiveTask == nil
	})
	return origin
}

// sayStop sends a stop on conv.
func sayStop(r *rig, conv, id string) {
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: id, Text: "stop"}
}

// messagesOn counts the submissions (and steers) on addressee's in subjects.
func messagesOn(t *testing.T, r *rig, addressee string) int {
	t.Helper()
	n := 0
	for _, e := range inSubjectEnvelopes(t, r.url, addressee) {
		if e.Kind == lib.KindMessage {
			n++
		}
	}
	return n
}

// answerControl ends a control as the bridge does: a result artifact with a
// text part and, unless res is nil, the cardStop result part; then the state.
func answerControl(t *testing.T, r *rig, control *lib.Envelope, res *lib.CardStopResult) {
	t.Helper()
	answerControlOn(t, r.execFor(t, control, "platform"), res)
}

// answerControlOn is answerControl for any rig: it completes the control's
// execution with a result artifact carrying res, or no cardStop part when
// res is nil.
func answerControlOn(t *testing.T, exec *lib.TaskExecution, res *lib.CardStopResult) {
	t.Helper()
	ctx := context.Background()
	parts := []lib.Part{{Kind: "text", Text: "stopped some cards"}}
	if res != nil {
		part, err := lib.CardStopPart(lib.CardStop{CardStopResult: res})
		if err != nil {
			t.Fatal(err)
		}
		parts = append(parts, part)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "result", Name: lib.ArtifactResult, Parts: parts}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
}

// shown reports whether text was posted or edited in.
func shown(r *rig, text string) func() bool {
	return func() bool {
		return slices.Contains(r.adapter.postTexts(), text) || slices.Contains(r.adapter.editTexts(), text)
	}
}

// shownContaining reports whether a post or edit contains needle.
func shownContaining(r *rig, needle string) bool {
	for _, p := range append(r.adapter.postTexts(), r.adapter.editTexts()...) {
		if strings.Contains(p, needle) {
			return true
		}
	}
	return false
}

func TestCardStopPublishesTheDataPartAloneAndWordsTheAnswer(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-shape"
	ranATurn(t, r, conv)

	sayStop(r, conv, "cs-stop")
	control := awaitSubmission(t, r, "platform", 1)
	if control.To == nil || control.To.Session != "platform" {
		t.Fatalf("control to = %+v, want platform", control.To)
	}
	var m lib.Message
	if err := json.Unmarshal(control.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if len(m.Parts) != 1 || m.Parts[0].Kind != "data" || !lib.CardStopRequested(m.Parts) {
		t.Fatalf("control parts = %+v, want the cardStop request part alone", m.Parts)
	}
	if text := joinTextParts(m.Parts); text != "" {
		t.Fatalf("control carries text %q", text)
	}
	var auth Authority
	if err := json.Unmarshal(control.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	// The bridge reads the conversation from the audience, and the
	// capability check every task passes needs the grant.
	if auth.Audience.Conversation != conv {
		t.Fatalf("control audience = %+v, want the conversation", auth.Audience)
	}
	assertRootCapability(t, r, auth, control.TaskID, "platform")
	waitFor(t, "the placeholder", shown(r, cardStopPlaceholder))
	if slices.Contains(r.adapter.postTexts(), nothingRunningCardNotice) {
		t.Fatal("an armed stop posted the card notice before the executor answered")
	}

	// In flight, it is no turn: not active, not in history.
	rec := waitForRecord(t, r, conv, "the control on the record", func(rec *SessionRecord) bool { return rec.CardStop != nil })
	if rec.CardStop.TaskID != control.TaskID || rec.ActiveTask != nil || len(rec.Tasks) != 1 {
		t.Fatalf("record while the control runs: cardStop %+v, active %+v, %d tasks; want the control alone on CardStop", rec.CardStop, rec.ActiveTask, len(rec.Tasks))
	}

	answerControl(t, r, control, &lib.CardStopResult{
		Stopped: []lib.CardStopCard{{ID: "t_1", Title: "check node pools", PriorStatus: "running"}, {ID: "t_2", Title: "audit prod", PriorStatus: "todo"}},
	})
	want := "🛑 stopped 2 cards: check node pools, audit prod"
	waitFor(t, "the answer", shown(r, want))
	if !slices.Contains(r.adapter.editTexts(), want) {
		t.Fatalf("the answer was not edited into the placeholder: edits %q", r.adapter.editTexts())
	}
	rec = waitForRecord(t, r, conv, "the control off the record", func(rec *SessionRecord) bool { return rec.CardStop == nil })
	if len(rec.Tasks) != 1 || rec.ActiveTask != nil {
		t.Fatalf("after the control: %d tasks, active %+v; the control was counted as a turn", len(rec.Tasks), rec.ActiveTask)
	}
	if shownContaining(r, "stopped some cards") {
		t.Fatal("the executor's text was rendered as a deliverable")
	}
	r.g.mu.Lock()
	_, routed := r.g.taskSessions[control.TaskID]
	_, timed := r.g.cardStopTimers[control.TaskID]
	r.g.mu.Unlock()
	if routed || timed {
		t.Fatalf("the control's route (%v) or timer (%v) outlived its terminal", routed, timed)
	}
}

func TestCardStopGateOffKeepsTheCardNotice(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-cardstop-off"
	ranATurn(t, r, conv)
	sayStop(r, conv, "off-stop")
	waitFor(t, "the card notice", shown(r, nothingRunningCardNotice))
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("%d messages on platform, want the turn alone: the unarmed stop sent a control", n)
	}
}

func TestCardStopNeverOnTheSessionRoute(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, func(c *Config) { c.CardStop = true })
	conv := "discord:g1/thread-cardstop-session"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ss-1", Text: "check the pods"}
	waitFor(t, "spawn", func() bool { return len(spawn.calls()) > 0 })
	session := spawn.calls()[0].Session
	origin := r.awaitTask(t, session)
	completeTask(t, r.execFor(t, origin, session), "done")
	waitForRecord(t, r, conv, "the turn released", func(rec *SessionRecord) bool { return rec.ActiveTask == nil })

	sayStop(r, conv, "ss-stop")
	waitFor(t, "the card notice", shown(r, nothingRunningCardNotice))
	if n := messagesOn(t, r, session); n != 1 {
		t.Fatalf("%d messages to the session, want the turn alone", n)
	}
	if n := messagesOn(t, r, "platform"); n != 0 {
		t.Fatalf("%d messages to platform from a session-routed conversation", n)
	}
}

func TestCardStopCancelsTheRunningTurnFirst(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-active"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ca-1", Text: "check the pods"}
	turn := awaitSubmission(t, r, "platform", 0)
	_ = r.execFor(t, turn, "platform").PublishStatus(context.Background(), lib.StateWorking, false)

	sayStop(r, conv, "ca-stop")
	control := awaitSubmission(t, r, "platform", 1)
	var order []string
	for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
		order = append(order, fmt.Sprintf("%s:%s", e.Kind, e.TaskID))
	}
	want := []string{
		fmt.Sprintf("%s:%s", lib.KindMessage, turn.TaskID),
		fmt.Sprintf("%s:%s", lib.KindCancel, turn.TaskID),
		fmt.Sprintf("%s:%s", lib.KindMessage, control.TaskID),
	}
	if strings.Join(order, ",") != strings.Join(want, ",") {
		t.Fatalf("in-subject order %v, want the cancel before the control %v", order, want)
	}
	waitFor(t, "the cancel line", postedContaining(r, "cancel sent"))
	waitFor(t, "the placeholder", shown(r, cardStopPlaceholder))

	// A second stop while the control runs sends nothing now, and marks the
	// control for one follow-up.
	sayStop(r, conv, "ca-stop-2")
	waitFor(t, "already stopping", shown(r, cardStopAlreadyNotice))
	waitForRecord(t, r, conv, "the mark", func(rec *SessionRecord) bool { return rec.CardStop != nil && rec.CardStop.Again != nil })
	if n := messagesOn(t, r, "platform"); n != 2 {
		t.Fatalf("%d messages on platform after the second stop, want 2", n)
	}

	// The control fails: the armed fallback, then the follow-up.
	publishFinal(t, r, control, "platform", lib.StateFailed, "reason: card-stop-unavailable - HTTP 503")
	waitFor(t, "the fallback", shown(r, cardStopUnconfirmedNotice))
	followUp := awaitSubmission(t, r, "platform", 2)
	rec := waitForRecord(t, r, conv, "the follow-up on the record", func(rec *SessionRecord) bool {
		return rec.CardStop != nil && rec.CardStop.TaskID == followUp.TaskID
	})
	if !rec.CardStop.Quiet || rec.CardStop.StatusMsgID != "" || rec.CardStop.Again != nil {
		t.Fatalf("follow-up run %+v, want quiet, no placeholder, no mark", rec.CardStop)
	}
	// A follow-up that finds nothing more says nothing.
	shownBefore := len(r.adapter.postTexts()) + len(r.adapter.editTexts())
	answerControl(t, r, followUp, &lib.CardStopResult{Settled: []lib.CardStopCard{{ID: "t_9", Title: "old"}}})
	waitForRecord(t, r, conv, "the follow-up off the record", func(rec *SessionRecord) bool { return rec.CardStop == nil })
	if n := len(r.adapter.postTexts()) + len(r.adapter.editTexts()); n != shownBefore {
		t.Fatalf("a follow-up that stopped nothing posted: %q / %q", r.adapter.postTexts(), r.adapter.editTexts())
	}

	// With the control answered and the turn's cancel still pending, the
	// next stop sends another control rather than "cancel already sent".
	sayStop(r, conv, "ca-stop-3")
	awaitSubmission(t, r, "platform", 3)
	if shownContaining(r, "cancel already sent") {
		t.Fatal("an armed stop on a detached turn answered 'cancel already sent'")
	}
}

func TestCardStopNotForAProgramsNamedCancel(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-named"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "nc-1", Text: "check the pods"}
	turn := awaitSubmission(t, r, "platform", 0)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "nc-2",
		Text: "stop", Intent: IntentCancel, TaskID: turn.TaskID}
	waitFor(t, "the cancel line", postedContaining(r, "cancel sent"))
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("%d messages on platform, want the turn alone: a named cancel sent a control", n)
	}
}

func TestCardStopNotForATopLevelStop(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-top"
	ranATurn(t, r, conv)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "tl-stop", Text: "stop", TopLevel: true}
	waitFor(t, "the top-level notice", shown(r, topLevelNothingRunningNotice))
	if n := messagesOn(t, r, "platform"); n != 1 {
		t.Fatalf("%d messages on platform, want the turn alone", n)
	}
}

// Every way a control can end without a usable answer answers the reply the
// stop had before the card stop, never a success.
func TestCardStopFallsBackWithoutAnAnswer(t *testing.T) {
	for _, tc := range []struct {
		name   string
		finish func(t *testing.T, r *rig, control *lib.Envelope)
	}{
		{"rejected by an older bridge", func(t *testing.T, r *rig, c *lib.Envelope) {
			publishFinal(t, r, c, "platform", lib.StateRejected, "reason: no-text-parts")
		}},
		{"failed", func(t *testing.T, r *rig, c *lib.Envelope) {
			publishFinal(t, r, c, "platform", lib.StateFailed, "reason: card-stop-unavailable - HTTP 503")
		}},
		{"canceled", func(t *testing.T, r *rig, c *lib.Envelope) {
			publishFinal(t, r, c, "platform", lib.StateCanceled, "reason: canceled-by-request")
		}},
		{"completed with no cardStop part", func(t *testing.T, r *rig, c *lib.Envelope) {
			answerControl(t, r, c, nil)
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r := cardStopRig(t, 0)
			conv := "discord:g1/thread-cardstop-fb-" + strings.ReplaceAll(tc.name, " ", "-")
			ranATurn(t, r, conv)
			sayStop(r, conv, "fb-stop")
			control := awaitSubmission(t, r, "platform", 1)
			tc.finish(t, r, control)
			waitFor(t, "the armed fallback", shown(r, cardStopUnconfirmedNotice))
			waitForRecord(t, r, conv, "the control off the record", func(rec *SessionRecord) bool { return rec.CardStop == nil })
			for _, p := range append(r.adapter.postTexts(), r.adapter.editTexts()...) {
				if strings.HasPrefix(p, "🛑 stopped") || strings.HasPrefix(p, "❌") || strings.HasPrefix(p, "🚫") ||
					p == "🛑 canceled" || p == nothingRunningNotice || p == nothingRunningCardNotice {
					t.Fatalf("the control's end rendered as %q", p)
				}
			}
		})
	}
}

func TestCardStopTimesOut(t *testing.T) {
	r := cardStopRig(t, 300*time.Millisecond)
	conv := "discord:g1/thread-cardstop-timeout"
	ranATurn(t, r, conv)
	sayStop(r, conv, "to-stop")
	control := awaitSubmission(t, r, "platform", 1)
	waitFor(t, "the fallback at the bound", shown(r, cardStopUnconfirmedNotice))
	waitForRecord(t, r, conv, "the control off the record", func(rec *SessionRecord) bool { return rec.CardStop == nil })
	waitFor(t, "a cancel for the control", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.Kind == lib.KindCancel && e.TaskID == control.TaskID {
				return true
			}
		}
		return false
	})

	// The executor's late answer renders nothing.
	shownBefore := len(r.adapter.postTexts()) + len(r.adapter.editTexts())
	answerControl(t, r, control, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "late"}}})
	time.Sleep(500 * time.Millisecond)
	if n := len(r.adapter.postTexts()) + len(r.adapter.editTexts()); n != shownBefore {
		t.Fatalf("the late answer rendered: %q / %q", r.adapter.postTexts(), r.adapter.editTexts())
	}

	// The next stop sends a fresh control.
	sayStop(r, conv, "to-stop-2")
	awaitSubmission(t, r, "platform", 2)
}

// A control on the record past its bound with no timer in this process (it
// started before a restart) is given up on the next turn, so the stop it
// meets is not answered "already stopping" for ever.
func TestCardStopExpiresAControlFromBeforeARestart(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-expire"
	ranATurn(t, r, conv)
	ctx := context.Background()
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil || rec == nil {
		t.Fatalf("record: %v %v", rec, err)
	}
	rec.CardStop = &CardStopRun{TaskID: "task-stale", CorrelationID: "corr-stale", Addressee: "platform",
		StartedAt: time.Now().Add(-2 * defaultCardStopTimeout)}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	sayStop(r, conv, "ex-stop")
	waitFor(t, "the stale control's fallback", shown(r, cardStopUnconfirmedNotice))
	control := awaitSubmission(t, r, "platform", 1)
	if shownContaining(r, "already stopping") {
		t.Fatal("a control from before the restart answered 'already stopping'")
	}
	waitForRecord(t, r, conv, "the new control on the record", func(rec *SessionRecord) bool {
		return rec.CardStop != nil && rec.CardStop.TaskID == control.TaskID
	})
}

// While a control runs, the next message is an ordinary turn: not a steer
// into the control, and the control is not in the history the turn joins.
func TestCardStopIsNotATurn(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-notturn"
	ranATurn(t, r, conv)
	sayStop(r, conv, "nt-stop")
	control := awaitSubmission(t, r, "platform", 1)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "nt-ask", Text: "what is the agent doing"}
	next := awaitSubmission(t, r, "platform", 2)
	if next.TaskID == control.TaskID {
		t.Fatal("the next message was steered into the control")
	}
	rec := waitForRecord(t, r, conv, "the new turn active", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == next.TaskID
	})
	for _, ref := range rec.Tasks {
		if ref.ID == control.TaskID {
			t.Fatal("the control is in the task history")
		}
	}
	if rec.CardStop == nil || rec.CardStop.TaskID != control.TaskID {
		t.Fatalf("the new turn dropped the control: %+v", rec.CardStop)
	}
}

func TestCardStopReplyWording(t *testing.T) {
	cards := func(titles ...string) []lib.CardStopCard {
		out := make([]lib.CardStopCard, len(titles))
		for i, title := range titles {
			out[i] = lib.CardStopCard{ID: fmt.Sprintf("t_%d", i), Title: title}
		}
		return out
	}
	many := func(prefix string, n int) []lib.CardStopCard {
		out := make([]lib.CardStopCard, n)
		for i := range out {
			out[i] = lib.CardStopCard{ID: fmt.Sprintf("t_%s%d", prefix, i), Title: fmt.Sprintf("%s%d", prefix, i)}
		}
		return out
	}
	for _, tc := range []struct {
		name string
		r    lib.CardStopResult
		want string
	}{
		{"nothing open", lib.CardStopResult{}, nothingRunningNotice},
		{"one stopped", lib.CardStopResult{Stopped: cards("a")}, "🛑 stopped 1 card: a"},
		{"five named, the rest counted", lib.CardStopResult{Stopped: cards("a", "b", "c", "d", "e", "f", "g")},
			"🛑 stopped 7 cards: a, b, c, d, e and 2 more"},
		{"settled only", lib.CardStopResult{Settled: cards("x")}, "🤷 nothing is running; already finished: x"},
		{"stopped and settled", lib.CardStopResult{Stopped: cards("a"), Settled: cards("x", "y")},
			"🛑 stopped 1 card: a; already finished: x, y"},
		{"unresolved", lib.CardStopResult{Stopped: cards("a", "b"), Unresolved: 1},
			"🛑 stopped 2 cards: a, b; a worker may still start for 1 of them, claimed just before the stop"},
		{"failed", lib.CardStopResult{Stopped: cards("a"), Failed: 2},
			"🛑 stopped 1 card: a; 2 cards could not be stopped"},
		{"failed alone", lib.CardStopResult{Failed: 1}, "⚠️ no card was stopped; 1 card could not be stopped"},
		{"unresolved alone", lib.CardStopResult{Unresolved: 2},
			"⚠️ no card was stopped; a worker may still start for 2 cards claimed just before the stop"},
		{"pending", lib.CardStopResult{Stopped: cards("a"), Pending: 1, Deadline: true},
			"🛑 stopped 1 card: a; 1 card still being stopped when time ran out, so it may still be running"},
		{"deadline", lib.CardStopResult{Stopped: cards("a"), Deadline: true},
			"🛑 stopped 1 card: a; the stop ran out of time before it finished, so some may still be open"},
		{"truncated", lib.CardStopResult{Stopped: cards("a"), Truncated: true},
			"🛑 stopped 1 card: a; this conversation had more cards than one stop takes, so some may still be open"},
		// ListsCut is one flag; only the list at the cap is hedged.
		{"stopped list cut", lib.CardStopResult{Stopped: many("s", lib.CardStopListMax), Settled: cards("x", "y"), ListsCut: true},
			"🛑 stopped at least 200 cards: s0, s1, s2, s3, s4 and at least 195 more; already finished: x, y"},
		{"settled list cut", lib.CardStopResult{Stopped: cards("a", "b", "c"), Settled: many("d", lib.CardStopListMax), ListsCut: true},
			"🛑 stopped 3 cards: a, b, c; already finished: d0, d1, d2, d3, d4 and at least 195 more"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := cardStopReply(&tc.r, false); got != tc.want {
				t.Fatalf("reply\n got %q\nwant %q", got, tc.want)
			}
		})
	}
}

// A title is model text: nothing in it may ping, lay out, or run past one
// line or the rune bound.
func TestCardStopTitleIsSanitized(t *testing.T) {
	for _, in := range []string{
		"fix it <!here> now",
		"ping <@U012ABC> and <!channel>",
		"tell @here and @everyone",
		"chat <users/all>",
		"line one\nline two\r\n<#C123|general>",
		"zero​width‮bidi",
		"see [x](https://evil.example)",
		"check https://evil.example/x and evil.example",
		"evil。example/x and evil．example and evil｡example",
		"https：//x",
		"||spoiler|| text",
		"run `rm -rf` *now* _please_ ~~gone~~",
		"unclosed ` and *",
	} {
		got := cardStopTitle(in)
		if strings.ContainsAny(got, "<>@\n\r​‮[]`*_~|.:。．｡：") {
			t.Errorf("cardStopTitle(%q) = %q keeps markup or a line break", in, got)
		}
	}
	if got := cardStopTitle("see [x](https://evil.example)"); got != "see (x)(https꞉//evil․example)" {
		t.Errorf("a masked link became %q", got)
	}
	// A bare URL or domain: chat surfaces link those on their own.
	if got := cardStopTitle("check https://evil.example/x"); got != "check https꞉//evil․example/x" {
		t.Errorf("a bare URL became %q", got)
	}
	if got := cardStopTitle("run `ls` *now* node_pools"); got != "run 'ls' now node pools" {
		t.Errorf("markup became %q", got)
	}
	if got := cardStopTitle("tell @here"); got != "tell ＠here" {
		t.Errorf("an @ became %q", got)
	}
	if got := cardStopTitle("  a \n\n b  "); got != "a b" {
		t.Errorf("whitespace runs became %q, want %q", got, "a b")
	}
	if got := cardStopTitle("\n\t"); got != "(untitled card)" {
		t.Errorf("a blank title became %q", got)
	}
	long := cardStopTitle(strings.Repeat("é", 200))
	if n := utf8.RuneCountInString(long); n != cardStopTitleRunes || !strings.HasSuffix(long, "…") {
		t.Errorf("a long title is %d runes (%q), want %d ending in …", n, long, cardStopTitleRunes)
	}
	// Through the reply, end to end.
	reply := cardStopReply(&lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t", Title: "ok <!here>\n@channel"}}}, false)
	if strings.ContainsAny(reply, "<>@\n") {
		t.Errorf("reply %q carries a title's markup", reply)
	}
}

// The control's answer arrives on the result artifact; a gateway that lost
// the render state to a restart reads it off the stream.
func TestCardStopReadsTheAnswerOffTheStreamAfterARestart(t *testing.T) {
	r := cardStopRig(t, 0)
	var replayedID atomic.Value
	r.g.terminalReplayHook = func(taskID string) error {
		replayedID.Store(taskID)
		return nil
	}
	conv := "discord:g1/thread-cardstop-replay"
	ranATurn(t, r, conv)
	sayStop(r, conv, "rp-stop")
	control := awaitSubmission(t, r, "platform", 1)
	waitForRecord(t, r, conv, "the control on the record", func(rec *SessionRecord) bool { return rec.CardStop != nil })
	ctx := context.Background()
	exec := r.execFor(t, control, "platform")
	part, err := lib.CardStopPart(lib.CardStop{CardStopResult: &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "replayed"}}}})
	if err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{ArtifactID: "result", Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: "x"}, part}}); err != nil {
		t.Fatal(err)
	}
	// The restart: once the artifact is in the render state, the state is
	// dropped, read and replaced under the conversation's lock, which the
	// relay holds while it applies an event, so the terminal finds none.
	waitFor(t, "the artifact relayed, then the render state lost", func() bool {
		l := r.g.lockSession(conv)
		l.Lock()
		defer l.Unlock()
		r.g.mu.Lock()
		defer r.g.mu.Unlock()
		if rs := r.g.relays[control.TaskID]; rs == nil || len(rs.result) == 0 {
			return false
		}
		r.g.relays[control.TaskID] = &relayState{}
		return true
	})
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the replayed answer", shown(r, "🛑 stopped 1 card: replayed"))
	if id, _ := replayedID.Load().(string); id != control.TaskID {
		t.Fatal("the answer did not come from the stream replay")
	}
}

// gke-labs#3263 review M1: a stop that arrives while a control is in flight
// marks it, and when it ends one quiet follow-up control stops the cards the
// newer turn filed. Here the conversation had nothing running at the first
// stop, a new question started while the control ran, and the second stop
// cancelled that question.
func TestCardStopFollowUpCatchesCardsFiledSinceTheLastStop(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-again"
	ranATurn(t, r, conv)
	sayStop(r, conv, "ag-stop-1")
	first := awaitSubmission(t, r, "platform", 1)

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ag-ask", Text: "now check prod"}
	newer := awaitSubmission(t, r, "platform", 2)
	_ = r.execFor(t, newer, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
	sayStop(r, conv, "ag-stop-2")
	waitFor(t, "the newer turn's cancel", postedContaining(r, "cancel sent"))
	waitForRecord(t, r, conv, "the stop that cancelled the newer turn marks the control", func(rec *SessionRecord) bool {
		return rec.CardStop != nil && rec.CardStop.TaskID == first.TaskID && rec.CardStop.Again != nil
	})
	// A third stop changes nothing: one mark, one follow-up.
	sayStop(r, conv, "ag-stop-3")
	waitFor(t, "already stopping", shown(r, cardStopAlreadyNotice))
	if n := messagesOn(t, r, "platform"); n != 3 {
		t.Fatalf("%d messages on platform while the first control runs, want 3", n)
	}

	answerControl(t, r, first, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "pods on x"}}})
	waitFor(t, "the first answer", shown(r, "🛑 stopped 1 card: pods on x"))
	followUp := awaitSubmission(t, r, "platform", 3)
	if strings.Count(strings.Join(r.adapter.postTexts(), "\n"), cardStopPlaceholder) != 1 {
		t.Fatalf("the follow-up posted a placeholder: %q", r.adapter.postTexts())
	}
	answerControl(t, r, followUp, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_2", Title: "prod check"}}})
	waitFor(t, "the follow-up's answer, posted", func() bool {
		return slices.Contains(r.adapter.postTexts(), "🛑 stopped 1 card: prod check")
	})
	waitForRecord(t, r, conv, "nothing in flight", func(rec *SessionRecord) bool { return rec.CardStop == nil })
	time.Sleep(300 * time.Millisecond)
	if n := messagesOn(t, r, "platform"); n != 4 {
		t.Fatalf("%d messages on platform, want 4: the mark was not capped at one follow-up", n)
	}
}

// A stop told "the answer comes" while a quiet follow-up runs gets that
// answer, even when the follow-up finds nothing.
func TestCardStopPromisedAnswerFromAQuietFollowUp(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-again-promised"
	ranATurn(t, r, conv)
	sayStop(r, conv, "ap-stop-1")
	first := awaitSubmission(t, r, "platform", 1)
	sayStop(r, conv, "ap-stop-2")
	waitForRecord(t, r, conv, "the mark", func(rec *SessionRecord) bool { return rec.CardStop != nil && rec.CardStop.Again != nil })
	answerControl(t, r, first, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "pods on x"}}})
	waitFor(t, "the first answer", shown(r, "🛑 stopped 1 card: pods on x"))
	followUp := awaitSubmission(t, r, "platform", 2)
	sayStop(r, conv, "ap-stop-3")
	waitForRecord(t, r, conv, "the follow-up marked", func(rec *SessionRecord) bool {
		return rec.CardStop != nil && rec.CardStop.TaskID == followUp.TaskID && rec.CardStop.Again != nil
	})
	answerControl(t, r, followUp, &lib.CardStopResult{})
	waitFor(t, "the promised answer", shown(r, nothingRunningNotice))
	// The follow-up the third stop asked for is quiet again.
	second := awaitSubmission(t, r, "platform", 3)
	answerControl(t, r, second, &lib.CardStopResult{})
	waitForRecord(t, r, conv, "nothing in flight", func(rec *SessionRecord) bool { return rec.CardStop == nil })
	time.Sleep(300 * time.Millisecond)
	if n := strings.Count(strings.Join(r.adapter.postTexts(), "\n"), nothingRunningNotice); n != 1 {
		t.Fatalf("%q posted %d times, want once: the last follow-up was not quiet", nothingRunningNotice, n)
	}
}

// A follow-up that fails says so, as the first control would.
func TestCardStopFollowUpFailurePostsTheFallback(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-again-fail"
	ranATurn(t, r, conv)
	sayStop(r, conv, "af-stop-1")
	first := awaitSubmission(t, r, "platform", 1)
	sayStop(r, conv, "af-stop-2")
	waitForRecord(t, r, conv, "the mark", func(rec *SessionRecord) bool { return rec.CardStop != nil && rec.CardStop.Again != nil })
	answerControl(t, r, first, &lib.CardStopResult{})
	waitFor(t, "the first answer", shown(r, nothingRunningNotice))
	followUp := awaitSubmission(t, r, "platform", 2)
	publishFinal(t, r, followUp, "platform", lib.StateRejected, "reason: no-text-parts")
	waitFor(t, "the follow-up's fallback", func() bool {
		return slices.Contains(r.adapter.postTexts(), cardStopUnconfirmedNotice)
	})
}

// A marked control that ends while a question asked after the stop is
// running sends no follow-up: it would wait for that question and archive its
// cards too. The room is told instead.
func TestCardStopFollowUpSkippedForANewerTurn(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-again-newer"
	ranATurn(t, r, conv)
	sayStop(r, conv, "an-stop-1")
	first := awaitSubmission(t, r, "platform", 1)
	sayStop(r, conv, "an-stop-2")
	waitForRecord(t, r, conv, "the mark", func(rec *SessionRecord) bool { return rec.CardStop != nil && rec.CardStop.Again != nil })
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "an-ask", Text: "now check prod"}
	awaitSubmission(t, r, "platform", 2)
	answerControl(t, r, first, &lib.CardStopResult{})
	// The control found no card, but a question is running: not "nothing
	// is running".
	waitFor(t, "the first answer", shown(r, cardStopNoCardNotice))
	if shown(r, nothingRunningNotice)() {
		t.Fatalf("posted %q above a running question", nothingRunningNotice)
	}
	waitFor(t, "the skipped line", shown(r, cardStopSkippedNotice))
	time.Sleep(300 * time.Millisecond)
	if n := messagesOn(t, r, "platform"); n != 3 {
		t.Fatalf("%d messages on platform, want 3: a follow-up went out behind a newer turn", n)
	}
}

// A marked control that ends after the conversation moved to a session
// worker sends no follow-up, and says that, not that a question is running.
func TestCardStopFollowUpSkippedWhenTheRouteMoved(t *testing.T) {
	r, _ := startRigWithSpawnerCap(t, "platform", 0, func(c *Config) { c.CardStop = true })
	conv := "discord:g1/thread-cardstop-again-moved"
	ranATurn(t, r, conv)
	sayStop(r, conv, "am-stop-1")
	first := awaitSubmission(t, r, "platform", 1)
	sayStop(r, conv, "am-stop-2")
	waitForRecord(t, r, conv, "the mark", func(rec *SessionRecord) bool { return rec.CardStop != nil && rec.CardStop.Again != nil })
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "am-session", Text: "/session"}
	waitForRecord(t, r, conv, "the session route", func(rec *SessionRecord) bool { return rec.SessionRouted })
	answerControl(t, r, first, &lib.CardStopResult{})
	waitFor(t, "the moved line", shown(r, cardStopMovedNotice))
	if shown(r, cardStopSkippedNotice)() {
		t.Fatalf("blamed a newer question with none running: %q", r.adapter.postTexts())
	}
	time.Sleep(300 * time.Millisecond)
	if n := messagesOn(t, r, "platform"); n != 2 {
		t.Fatalf("%d messages on platform, want 2: a follow-up went out off the route", n)
	}
}

// A mark that outlived a restart with the card stop turned off sends no
// follow-up, and says the stop was turned off.
func TestCardStopFollowUpSkippedWhenTheStopIsOff(t *testing.T) {
	r := startRigWith(t, func(c *Config) { c.CardStop = false })
	rec := &SessionRecord{Key: "discord:g1/thread-cardstop-again-off", Addressee: "platform", ContextID: "ctx-off"}
	r.g.followUpCardStop(context.Background(), rec, Authority{})
	waitFor(t, "the off line", shown(r, cardStopOffNotice))
	if shown(r, cardStopSkippedNotice)() || rec.CardStop != nil {
		t.Fatalf("posts %q, control %+v: want the off line and no control", r.adapter.postTexts(), rec.CardStop)
	}
}

// The timer path with a control sent after a cancel answers the armed
// fallback, not the card notice.
func TestCardStopTimesOutAfterACancel(t *testing.T) {
	r := cardStopRig(t, 300*time.Millisecond)
	conv := "discord:g1/thread-cardstop-timeout-active"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "ta-1", Text: "check the pods"}
	turn := awaitSubmission(t, r, "platform", 0)
	_ = r.execFor(t, turn, "platform").PublishStatus(context.Background(), lib.StateWorking, false)
	sayStop(r, conv, "ta-stop")
	awaitSubmission(t, r, "platform", 1)
	waitFor(t, "the armed fallback at the bound", shown(r, cardStopUnconfirmedNotice))
	if shownContaining(r, "isn't supported yet") {
		t.Fatal("an armed install said stopping a card isn't supported")
	}
}

// A stop whose cancel cannot be published sends no control.
func TestCardStopNotSentWhenTheCancelFails(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-cancel-fails"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "cf-1", Text: "check the pods"}
	waitForRecord(t, r, conv, "the turn active", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })
	deleteTasksStream(t, r.url)
	sayStop(r, conv, "cf-stop")
	waitFor(t, "the cancel failure", postedContaining(r, "could not send the stop"))
	time.Sleep(200 * time.Millisecond)
	if shownContaining(r, cardStopPlaceholder) {
		t.Fatal("a control was started after the cancel failed")
	}
	if rec, _ := r.g.reg.Get(context.Background(), conv); rec == nil || rec.CardStop != nil {
		t.Fatalf("record after a failed cancel: %+v", rec)
	}
}

// A control that cannot be started answers the armed fallback at once: a
// failed capability mint posts no placeholder, a failed publish edits it.
func TestCardStopThatCannotStartAnswersTheFallback(t *testing.T) {
	t.Run("capability mint fails", func(t *testing.T) {
		r := cardStopRig(t, 0)
		conv := "discord:g1/thread-cardstop-mint-fails"
		ranATurn(t, r, conv)
		deleteCapBucket(t, r.url)
		sayStop(r, conv, "mf-stop")
		waitFor(t, "the fallback", func() bool { return slices.Contains(r.adapter.postTexts(), cardStopUnconfirmedNotice) })
		if shownContaining(r, cardStopPlaceholder) || messagesOn(t, r, "platform") != 1 {
			t.Fatal("a control went out without a capability")
		}
		waitForRecord(t, r, conv, "no control on the record", func(rec *SessionRecord) bool { return rec.CardStop == nil })
	})
	t.Run("publish fails", func(t *testing.T) {
		r := cardStopRig(t, 0)
		conv := "discord:g1/thread-cardstop-publish-fails"
		ranATurn(t, r, conv)
		deleteTasksStream(t, r.url)
		sayStop(r, conv, "pf-stop")
		waitFor(t, "the placeholder edited to the fallback", func() bool {
			return slices.Contains(r.adapter.editTexts(), cardStopUnconfirmedNotice)
		})
		if !slices.Contains(r.adapter.postTexts(), cardStopPlaceholder) {
			t.Fatal("no placeholder was posted")
		}
		rec := waitForRecord(t, r, conv, "no control on the record", func(rec *SessionRecord) bool { return rec.CardStop == nil })
		r.g.mu.Lock()
		n := len(r.g.cardStopTimers)
		r.g.mu.Unlock()
		if n != 0 || rec.CardStop != nil {
			t.Fatalf("a control that never reached the bus left %d timers", n)
		}
	})
}

// An answer whose edit fails is posted.
func TestCardStopAnswerPostsWhenTheEditFails(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-edit-fails"
	ranATurn(t, r, conv)
	sayStop(r, conv, "ef-stop")
	control := awaitSubmission(t, r, "platform", 1)
	waitFor(t, "the placeholder", shown(r, cardStopPlaceholder))
	r.adapter.mu.Lock()
	r.adapter.failEdits = 1
	r.adapter.mu.Unlock()
	answerControl(t, r, control, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "a"}}})
	waitFor(t, "the answer posted", func() bool {
		return slices.Contains(r.adapter.postTexts(), "🛑 stopped 1 card: a")
	})
}

// The "card stop ended" line names the terminal's state, its reason token
// and the outcome, so an operator can tell an old bridge (rejected
// no-text-parts) from a failed stop and a timeout.
func TestCardStopEndedIsLogged(t *testing.T) {
	r, _ := startRigWithSpawnerCap(t, "platform", 0, func(c *Config) { c.CardStop = true })
	conv := "discord:g1/thread-cardstop-log"
	ranATurn(t, r, conv)
	sayStop(r, conv, "lg-stop")
	control := awaitSubmission(t, r, "platform", 1)
	publishFinal(t, r, control, "platform", lib.StateRejected, "reason: no-text-parts")
	waitFor(t, "the end logged", loggedContaining(r, "card stop ended", "state=rejected", "reason=no-text-parts",
		"outcome="+cardStopOutcomeExecutor, control.TaskID))
}

// gke-labs#3263 review L4: a late event for a control that has ended, whose
// task index outlived the route's retirement, is dropped, never rendered as a
// task's terminal.
func TestCardStopLateEventAfterAFailedIndexDropIsDropped(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-late"
	ranATurn(t, r, conv)
	sayStop(r, conv, "le-stop")
	control := awaitSubmission(t, r, "platform", 1)
	answerControl(t, r, control, &lib.CardStopResult{})
	waitForRecord(t, r, conv, "the control ended", func(rec *SessionRecord) bool {
		return rec.CardStop == nil && slices.Contains(rec.RetiredCardStops, control.TaskID)
	})
	// The index survives, as a failed DropTask leaves it.
	if err := r.g.reg.IndexTask(context.Background(), control.TaskID, conv); err != nil {
		t.Fatal(err)
	}
	shownBefore := len(r.adapter.postTexts()) + len(r.adapter.editTexts())
	publishFinal(t, r, control, "platform", lib.StateFailed, "raw bridge text")
	time.Sleep(500 * time.Millisecond)
	if n := len(r.adapter.postTexts()) + len(r.adapter.editTexts()); n != shownBefore || shownContaining(r, "raw bridge text") {
		t.Fatalf("a late control event rendered: %q", r.adapter.postTexts())
	}
}

// The same late event, for a control a follow-up has since followed and
// ended: one remembered id would name the follow-up, not the control.
func TestCardStopLateEventAfterAFollowUpIsDropped(t *testing.T) {
	r := cardStopRig(t, 0)
	conv := "discord:g1/thread-cardstop-late-fu"
	ranATurn(t, r, conv)
	sayStop(r, conv, "lf-stop-1")
	control := awaitSubmission(t, r, "platform", 1)
	sayStop(r, conv, "lf-stop-2")
	waitForRecord(t, r, conv, "the mark", func(rec *SessionRecord) bool { return rec.CardStop != nil && rec.CardStop.Again != nil })
	answerControl(t, r, control, &lib.CardStopResult{})
	followUp := awaitSubmission(t, r, "platform", 2)
	answerControl(t, r, followUp, &lib.CardStopResult{})
	waitForRecord(t, r, conv, "both ended", func(rec *SessionRecord) bool {
		return rec.CardStop == nil && slices.Contains(rec.RetiredCardStops, followUp.TaskID)
	})
	if err := r.g.reg.IndexTask(context.Background(), control.TaskID, conv); err != nil {
		t.Fatal(err)
	}
	shownBefore := len(r.adapter.postTexts()) + len(r.adapter.editTexts())
	publishFinal(t, r, control, "platform", lib.StateFailed, "raw bridge text")
	time.Sleep(500 * time.Millisecond)
	if n := len(r.adapter.postTexts()) + len(r.adapter.editTexts()); n != shownBefore || shownContaining(r, "raw bridge text") {
		t.Fatalf("a late event for the first control rendered: %q", r.adapter.postTexts())
	}
}

// gke-labs#3263 review M3: the inject door's cancel route, on a conversation
// with nothing running, reports the card stop as a stop that reached the bus,
// not as a refusal that leaves the work running.
func TestCardStopInjectCancelReportsTheStop(t *testing.T) {
	r := startInjectRigWith(t, func(c *Config) { c.CardStop = true })
	reply := r.inject(t, "case-cardstop", injectTestAuthor, "check the pods")
	origin := r.awaitTask(t, "platform")
	completeTask(t, r.execFor(t, origin, "platform"), "checking")
	r.waitForTerminal(t, reply.Conversation, reply.TaskID)
	waitFor(t, "the conversation released", func() bool {
		rec, err := r.g.reg.Get(context.Background(), reply.Conversation)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})

	out := r.cancel(t, reply.Conversation, injectTestAuthor)
	if !out.CancelPublished || out.Refusal != "" || strings.Contains(out.Note, "still doing") {
		t.Fatalf("cancel reply %+v, want the stop reported as published", out)
	}
	var control *lib.Envelope
	waitFor(t, "the control on the bus", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			var m lib.Message
			if e.Kind == lib.KindMessage && json.Unmarshal(e.Payload, &m) == nil && lib.CardStopRequested(m.Parts) {
				control = e
				return true
			}
		}
		return false
	})
	// A second cancel while the control is in flight reaches the mark, and
	// still reports the stop as on the bus, not as a cancel never sent.
	again := r.cancel(t, reply.Conversation, injectTestAuthor)
	if !again.CancelPublished || again.Refusal != "" || strings.Contains(again.Note, "still doing") {
		t.Fatalf("second cancel reply %+v, want the stop in flight reported as published", again)
	}
	answerControlOn(t, r.execFor(t, control, "platform"), &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "pods"}}})
	// The transcript reads placeholder, then the answer as its edit.
	waitFor(t, "the answer in the transcript", func() bool {
		entries, _, _ := r.adapter.snapshot(reply.Conversation, 0, "")
		return slices.Contains(entryTexts(entries, InjectEntryPost), cardStopPlaceholder) &&
			slices.Contains(entryTexts(entries, InjectEntryEdit), "🛑 stopped 1 card: pods")
	})
}

// A follow-up runs from the relay, inside no turn, so it must not move the
// inject door's cancel count: a program's cancel waiting on the conversation
// would read the follow-up as its own cancel on the bus.
func TestCardStopFollowUpTellsNoDoorACancelWent(t *testing.T) {
	r := startInjectRigWith(t, func(c *Config) { c.CardStop = true })
	reply := r.inject(t, "case-cardstop-fu", injectTestAuthor, "check the pods")
	origin := r.awaitTask(t, "platform")
	completeTask(t, r.execFor(t, origin, "platform"), "checking")
	r.waitForTerminal(t, reply.Conversation, reply.TaskID)
	record := func() *SessionRecord {
		rec, err := r.g.reg.Get(context.Background(), reply.Conversation)
		if err != nil {
			return nil
		}
		return rec
	}
	waitFor(t, "the conversation released", func() bool { rec := record(); return rec != nil && rec.ActiveTask == nil })
	r.cancel(t, reply.Conversation, injectTestAuthor)
	r.cancel(t, reply.Conversation, injectTestAuthor)
	var first *lib.Envelope
	waitFor(t, "the marked control", func() bool {
		rec := record()
		if rec == nil || rec.CardStop == nil || rec.CardStop.Again == nil {
			return false
		}
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			if e.TaskID == rec.CardStop.TaskID {
				first = e
				return true
			}
		}
		return false
	})
	cancels := func() int {
		r.adapter.mu.Lock()
		defer r.adapter.mu.Unlock()
		return r.adapter.conversationLocked(reply.Conversation).cancelsPublished
	}
	before := cancels()
	answerControlOn(t, r.execFor(t, first, "platform"), &lib.CardStopResult{})
	waitFor(t, "the follow-up in flight", func() bool {
		rec := record()
		return rec != nil && rec.CardStop != nil && rec.CardStop.TaskID != first.TaskID
	})
	if got := cancels(); got != before {
		t.Fatalf("the follow-up moved the door's cancel count from %d to %d", before, got)
	}
}

// gke-labs#3263 review L3: on the A2A door a `stop` that names no task sends
// the control, and the reply the caller gets is a line that promises no
// answer: the door's Edit cannot reach a placeholder, and the answer comes
// after the turn with no task to carry it.
func TestCardStopOnTheA2ADoorPromisesNoAnswer(t *testing.T) {
	r := startA2ARigOpts(t, func(door *A2ADoor) Adapter { return door }, nil, func(c *Config) { c.CardStop = true })
	taskOf(t, r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("check the pods", "m-1", "ctx-cs", false)))
	origin := r.awaitTask(t, "platform")
	r.complete(t, origin, "checking")
	waitFor(t, "the conversation released", func() bool {
		recs, err := r.g.reg.Sessions(context.Background())
		if err != nil {
			return false
		}
		for _, rec := range recs {
			if rec.ContextID != "" && len(rec.Tasks) == 1 && rec.ActiveTask == nil {
				return true
			}
		}
		return false
	})
	resp := r.rpc(t, a2aTestCaller, a2aMethodSend, sendParams("stop", "m-2", "ctx-cs", false))
	if resp.Error != nil {
		t.Fatalf("stop refused: %d %s", resp.Error.Code, resp.Error.Message)
	}
	raw, _ := json.Marshal(resp.Result)
	var reply a2aMessageObject
	if err := json.Unmarshal(raw, &reply); err != nil {
		t.Fatalf("result %s: %v", raw, err)
	}
	if text := joinTextParts(reply.Parts); text != cardStopDoorNotice {
		t.Fatalf("stop reply %q, want %q", text, cardStopDoorNotice)
	}
	waitFor(t, "the control on the bus", func() bool {
		for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
			var m lib.Message
			if e.Kind == lib.KindMessage && json.Unmarshal(e.Payload, &m) == nil && lib.CardStopRequested(m.Parts) {
				return true
			}
		}
		return false
	})
}
