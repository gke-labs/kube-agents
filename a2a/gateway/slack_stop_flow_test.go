package gateway

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The Slack Stop button end to end through the gateway (slack_stop.go):
// the card stop armed, its own replies, the quiet cancel, the requester gate.

func TestSlackStopReplyWording(t *testing.T) {
	stopped := []lib.CardStopCard{{ID: "t_1", Title: "check node pools", PriorStatus: "running"}}
	for _, tc := range []struct {
		name string
		r    lib.CardStopResult
		turn bool
		want string
	}{
		{"cards stopped", lib.CardStopResult{Stopped: stopped}, false, "Stopped."},
		{"only the turn", lib.CardStopResult{}, true, "Stopped."},
		{"nothing", lib.CardStopResult{}, false, "Nothing was running."},
		{"only finished cards", lib.CardStopResult{Settled: stopped}, false, "Nothing was running."},
		{"the only card did not stop", lib.CardStopResult{Failed: 1, NotStopped: []lib.CardStopCard{{ID: "t_2", Title: "audit prod"}}}, false,
			slackStopCouldntReply},
		{"one card did not stop", lib.CardStopResult{Stopped: stopped, Failed: 1, NotStopped: []lib.CardStopCard{{ID: "t_2", Title: "audit prod"}}}, false,
			"Stopped, except one task that didn't respond: audit prod."},
		{"one unnamed card did not stop", lib.CardStopResult{Stopped: stopped, Pending: 1}, false, slackStopCouldntReply},
		{"two did not stop", lib.CardStopResult{Failed: 1, Unresolved: 1, NotStopped: []lib.CardStopCard{{ID: "a"}, {ID: "b"}}}, false, slackStopCouldntReply},
		{"deadline", lib.CardStopResult{Stopped: stopped, Deadline: true}, false, slackStopCouldntReply},
		{"truncated", lib.CardStopResult{Stopped: stopped, Truncated: true}, false, slackStopCouldntReply},
	} {
		r := tc.r
		if got := slackStopReply(&r, tc.turn, false); got != tc.want {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

// A stop that found nothing while a newer question runs says so (approved
// 2026-10-22); one that stopped work, or could not promise, is unchanged.
func TestSlackStopReplyWhileANewerQuestionRuns(t *testing.T) {
	stopped := []lib.CardStopCard{{ID: "t_1", Title: "check pods"}}
	for name, tc := range map[string]struct {
		r    lib.CardStopResult
		turn bool
		want string
	}{
		"nothing found": {lib.CardStopResult{}, false, slackStopNewerTurnReply},

		"only finished cards": {lib.CardStopResult{Settled: stopped}, false, slackStopNewerTurnReply},

		"a card stopped": {lib.CardStopResult{Stopped: stopped}, false, slackStopStoppedReply},

		"a turn cancelled": {lib.CardStopResult{}, true, slackStopStoppedReply},

		"could not promise": {lib.CardStopResult{Pending: 1}, false, slackStopCouldntReply},
	} {
		r := tc.r
		if got := slackStopReply(&r, tc.turn, true); got != tc.want {
			t.Errorf("%s: got %q, want %q", name, got, tc.want)
		}
	}
	if slackStopNewerTurnReply != "No tasks were running yet. I'm still answering your latest question." {
		t.Fatalf("the approved line changed: %q", slackStopNewerTurnReply)
	}
}

func TestSlackStopReplyCleansTheTitle(t *testing.T) {
	r := lib.CardStopResult{Failed: 1, NotStopped: []lib.CardStopCard{{ID: "t_2", Title: "audit <@U1> [x](https://e.vil)"}}}
	got := slackStopReply(&r, true, false)
	if strings.Contains(got, "<@") || strings.Contains(got, "](") {
		t.Fatalf("a title could mention or link: %q", got)
	}
}

func TestSlackStopUnconfirmedIsItsOwn(t *testing.T) {
	if got := (CardStopRun{SlackStop: true}).unconfirmed(); got != slackStopCouldntReply {
		t.Errorf("a Slack Stop's fallback is %q", got)
	}
	if got := (CardStopRun{}).unconfirmed(); got != cardStopUnconfirmedNotice {
		t.Errorf("a typed stop's fallback changed to %q", got)
	}
}

// slackStopRig is the Slack rig with the card stop armed, U1 and U3 listed.
func slackStopRig(t *testing.T) *slackDMRig {
	t.Helper()
	return startSlackDMRigWith(t, nil, func(c *Config) {
		c.CardStop = true
		c.SlackAllowedUsers = []string{"U1", "U3"}
	})
}

// pressStop delivers the Stop button's stop, as inboundStop builds it, from
// user on the DM thread rooted at thread in D1.
func pressStop(d *slackDMRig, user, thread, eventTS string) {
	d.adapter.raw <- InboundMessage{Conversation: "slack:dm/D1/" + thread, Kind: "dm", AuthorID: user,
		MessageID: eventTS, Intent: IntentCancel}
}

func slackPostsIn(d *slackDMRig, thread string) []string {
	var out []string
	for _, p := range d.posts() {
		if p.channel == "D1" && p.thread == thread {
			out = append(out, p.text)
		}
	}
	return out
}

// askAndAnswer runs U1's ask in the DM thread rooted at 100.1 to its end.
func askAndAnswer(t *testing.T, d *slackDMRig) *lib.Envelope {
	t.Helper()
	d.adapter.events <- slackMsg("im", "D1", "U1", "check the pods on x", "100.1", "")
	first := d.awaitTasks(t, 1)[0]
	completeTask(t, d.r.execFor(t, first, "platform"), "checking the pods on x")
	waitForRecord(t, d.r, "slack:dm/D1/100.1", "the turn to end", func(rec *SessionRecord) bool { return rec.ActiveTask == nil })
	return first
}

func TestSlackStopButtonStopsTheCardsWithItsOwnReply(t *testing.T) {
	d := slackStopRig(t)
	askAndAnswer(t, d)

	pressStop(d, "U1", "100.1", "200.1")
	control := awaitSubmission(t, d.r, "platform", 1)
	var m lib.Message
	if err := json.Unmarshal(control.Payload, &m); err != nil {
		t.Fatal(err)
	}
	if !lib.CardStopRequested(m.Parts) {
		t.Fatalf("the Stop button sent %+v, not the card-stop control", m.Parts)
	}
	answerControl(t, d.r, control, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "check pods", PriorStatus: "running"}}})
	d.awaitPost(t, "the Slack Stop's reply", "D1", "100.1", slackStopStoppedReply)
	for _, p := range slackPostsIn(d, "100.1") {
		if strings.Contains(p, "🛑") {
			t.Fatalf("the Stop button posted the typed stop's line %q", p)
		}
	}
}

func TestSlackStopButtonCancelsTheTurnQuietly(t *testing.T) {
	d := slackStopRig(t)
	ctx := context.Background()
	d.adapter.events <- slackMsg("im", "D1", "U1", "check the pods on x", "100.1", "")
	first := d.awaitTasks(t, 1)[0]
	exec := d.r.execFor(t, first, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	conv := "slack:dm/D1/100.1"
	waitForRecord(t, d.r, conv, "the turn to run", func(rec *SessionRecord) bool { return rec.ActiveTask != nil })

	pressStop(d, "U1", "100.1", "200.1")
	waitFor(t, "the cancel on the bus", func() bool {
		for _, e := range inSubjectEnvelopes(t, d.r.url, "platform") {
			if e.Kind == lib.KindCancel && e.TaskID == first.TaskID {
				return true
			}
		}
		return false
	})
	control := awaitSubmission(t, d.r, "platform", 1)
	if err := exec.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	answerControl(t, d.r, control, &lib.CardStopResult{})
	d.awaitPost(t, "the Slack Stop's reply", "D1", "100.1", slackStopStoppedReply)
	waitForRecord(t, d.r, conv, "the cancelled turn marked quiet", func(rec *SessionRecord) bool {
		ref, ok := rec.TaskRefFor(first.TaskID)
		return ok && ref.QuietCancel && rec.CardStop == nil
	})
	for _, p := range slackPostsIn(d, "100.1") {
		if strings.Contains(p, "cancel sent") || strings.Contains(p, "canceled") {
			t.Fatalf("the Stop button's cancel posted %q", p)
		}
	}
}

func TestSlackStopButtonDuringACardStopCancelsANewTurnAndFollowsUp(t *testing.T) {
	d := slackStopRig(t)
	ctx := context.Background()
	askAndAnswer(t, d)
	conv := "slack:dm/D1/100.1"

	pressStop(d, "U1", "100.1", "200.1")
	control := awaitSubmission(t, d.r, "platform", 1)
	// A new ask while the first control runs, and a second press on it.
	d.adapter.events <- slackMsg("im", "D1", "U1", "and the nodes", "300.1", "100.1")
	second := awaitSubmission(t, d.r, "platform", 2)
	exec := d.r.execFor(t, second, "platform")
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitForRecord(t, d.r, conv, "the new turn to run", func(rec *SessionRecord) bool {
		return rec.ActiveTask != nil && rec.ActiveTask.TaskID == second.TaskID
	})
	pressStop(d, "U1", "100.1", "400.1")
	waitFor(t, "the new turn's cancel on the bus", func() bool {
		for _, e := range inSubjectEnvelopes(t, d.r.url, "platform") {
			if e.Kind == lib.KindCancel && e.TaskID == second.TaskID {
				return true
			}
		}
		return false
	})
	rec := waitForRecord(t, d.r, conv, "the follow-up marked in the Slack voice", func(rec *SessionRecord) bool {
		return rec.CardStop != nil && rec.CardStop.Again != nil && rec.CardStop.AgainSlackStop
	})
	if rec.CardStop.TaskID != control.TaskID {
		t.Fatalf("the second press replaced the control in flight: %+v", rec.CardStop)
	}

	if err := exec.PublishStatus(ctx, lib.StateCanceled, true); err != nil {
		t.Fatal(err)
	}
	answerControl(t, d.r, control, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "check pods"}}})
	followUp := awaitSubmission(t, d.r, "platform", 3)
	answerControl(t, d.r, followUp, &lib.CardStopResult{})
	waitFor(t, "a Slack Stop reply for each press", func() bool {
		n := 0
		for _, p := range slackPostsIn(d, "100.1") {
			if p == slackStopStoppedReply {
				n++
			}
		}
		return n == 2
	})
	for _, p := range slackPostsIn(d, "100.1") {
		if strings.Contains(p, "🛑") || strings.Contains(p, "⚠️") {
			t.Fatalf("a Slack Stop posted the typed stop's line %q", p)
		}
	}
}

func TestSlackStopButtonFallbackIsItsOwn(t *testing.T) {
	d := slackStopRig(t)
	askAndAnswer(t, d)

	pressStop(d, "U1", "100.1", "200.1")
	control := awaitSubmission(t, d.r, "platform", 1)
	// Completed with no result part: a second press would repeat it, so the
	// reply drops "Press Stop again".
	answerControl(t, d.r, control, nil)
	d.awaitPost(t, "the Slack Stop's fallback", "D1", "100.1", slackStopCouldntFinalReply)
	for _, p := range slackPostsIn(d, "100.1") {
		if p == cardStopUnconfirmedNotice || p == slackStopCouldntReply {
			t.Fatalf("the Stop button posted %q for a failure a press would repeat", p)
		}
	}
}

func TestSlackStopButtonFailedControlKeepsPressStopAgain(t *testing.T) {
	d := slackStopRig(t)
	askAndAnswer(t, d)

	pressStop(d, "U1", "100.1", "200.1")
	control := awaitSubmission(t, d.r, "platform", 1)
	// A failed control (session-kv unreachable, the turn wait ran out) can
	// pass on a second press.
	if err := d.r.execFor(t, control, "platform").PublishStatus(context.Background(), lib.StateFailed, true); err != nil {
		t.Fatal(err)
	}
	d.awaitPost(t, "the retryable fallback", "D1", "100.1", slackStopCouldntReply)
}

func TestSlackStopUnconfirmedFinalDropsTheRetry(t *testing.T) {
	if got := (CardStopRun{SlackStop: true}).unconfirmedFinal(); got != slackStopCouldntFinalReply {
		t.Errorf("a Slack Stop's final fallback is %q", got)
	}
	if !strings.HasPrefix(slackStopCouldntReply, slackStopCouldntFinalReply) {
		t.Error("the final fallback is not the approved line less its retry")
	}
	if got := (CardStopRun{}).unconfirmedFinal(); got != cardStopUnconfirmedNotice {
		t.Errorf("a typed stop's fallback changed to %q", got)
	}
}

func TestSlackStopButtonFromSomeoneElseStopsNothing(t *testing.T) {
	d := slackStopRig(t)
	askAndAnswer(t, d)
	conv := "slack:dm/D1/100.1"
	before := len(slackPostsIn(d, "100.1"))
	done := d.adapter.turnsFinished(conv)

	pressStop(d, "U3", "100.1", "200.1")
	waitFor(t, "the press to be handled", func() bool { return d.adapter.turnsFinished(conv) > done })
	if n := messagesOn(t, d.r, "platform"); n != 1 {
		t.Fatalf("a non-requester's press sent %d message(s) beyond the ask", n-1)
	}
	if got := slackPostsIn(d, "100.1"); len(got) != before {
		t.Fatalf("a non-requester's press posted %q", got[before:])
	}
}

func TestSlackStopButtonOnAThreadThatRanNothingSendsNoControl(t *testing.T) {
	d := slackStopRig(t)
	pressStop(d, "U1", "100.1", "200.1")
	d.awaitPost(t, "the nothing-running reply", "D1", "100.1", slackStopNothingReply)
	if n := messagesOn(t, d.r, "platform"); n != 0 {
		t.Fatalf("a press on a thread that ran nothing sent %d message(s)", n)
	}
}

func TestSlackStopButtonFromAnUnlistedMemberPostsNoNotice(t *testing.T) {
	d := slackStopRig(t)
	askAndAnswer(t, d)
	conv := "slack:dm/D1/100.1"
	before := len(slackPostsIn(d, "100.1"))
	done := d.adapter.turnsFinished(conv)

	pressStop(d, "U2", "100.1", "200.1")
	waitFor(t, "the press to be handled", func() bool { return d.adapter.turnsFinished(conv) > done })
	if got := slackPostsIn(d, "100.1"); len(got) != before {
		t.Fatalf("an unlisted member's press posted %q", got[before:])
	}
}

func TestTypedStopFromSomeoneElseKeepsTheCardStop(t *testing.T) {
	d := slackStopRig(t)
	askAndAnswer(t, d)

	d.adapter.events <- slackMsg("im", "D1", "U3", "stop", "300.1", "100.1")
	control := awaitSubmission(t, d.r, "platform", 1)
	answerControl(t, d.r, control, &lib.CardStopResult{Stopped: []lib.CardStopCard{{ID: "t_1", Title: "check pods"}}})
	// The typed stop keeps its placeholder, and its answer is edited in.
	d.awaitPost(t, "the typed stop's placeholder", "D1", "100.1", cardStopPlaceholder)
	waitFor(t, "the typed stop's own answer", func() bool {
		d.api.mu.Lock()
		defer d.api.mu.Unlock()
		for _, u := range d.api.updated {
			if u.channel == "D1" && strings.Contains(u.text, "stopped 1 card: check pods") {
				return true
			}
		}
		return false
	})
}
