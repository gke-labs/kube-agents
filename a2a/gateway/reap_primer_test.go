package gateway

import (
	"context"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestTheRehydrationPrimerCutsOnRuneBoundaries pins the per-task cut in
// buildRehydrationPrimer. The primer is annotated onto the next incarnation's
// pod and marshalled to JSON on the way, and encoding/json substitutes U+FFFD
// for invalid UTF-8 rather than erroring — so a byte cut here does not fail,
// it silently replaces a character in what the fresh pod reads as its own
// transcript. spawn.go's truncateRunes guards the primer's tail only; this cut
// lands mid-transcript and survives it.
func TestTheRehydrationPrimerCutsOnRuneBoundaries(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/primer-runes"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001",
		MessageID: "d-primer-1", Text: "summarise the fleet",
	}

	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()

	// A result comfortably past primerTaskResultCap, made only of 3-byte
	// runes so the cut at 2000 bytes cannot land on a boundary: 2000 is not
	// divisible by 3.
	const rune3 = "計"
	if utf8.RuneLen([]rune(rune3)[0]) != 3 {
		t.Fatalf("fixture assumption broken: %q is not 3 bytes", rune3)
	}
	body := strings.Repeat(rune3, primerTaskResultCap)
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: body}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}

	rec := &SessionRecord{
		Key:       conv,
		ContextID: origin.ContextID,
		Addressee: "platform",
		Tasks:     []TaskRef{{ID: origin.TaskID, Addressee: "platform", Requester: &TaskRequester{Backend: "discord", Subject: "s"}}},
	}

	var primer string
	waitFor(t, "the task's result on the stream", func() bool {
		primer, _, _, _ = r.g.buildRehydrationPrimer(ctx, rec, "")
		return strings.Contains(primer, rune3)
	})

	if !utf8.ValidString(primer) {
		t.Errorf("primer is not valid UTF-8; the per-task cut went through a rune")
	}
	if strings.ContainsRune(primer, utf8.RuneError) {
		t.Errorf("primer carries U+FFFD, so a character was replaced rather than dropped")
	}
	// The cut still has to bind, or this test would pass against no cut at
	// all. Rune-safe means at or under the cap, never over it.
	// The task's text sits in its own fenced block; measure what is inside.
	body = strings.TrimSpace(primer[strings.Index(primer, rune3):])
	body = strings.TrimSpace(strings.TrimSuffix(body, "```"))
	body = strings.TrimSuffix(body, "…")
	if len(body) > primerTaskResultCap {
		t.Errorf("cut task body is %d bytes, over the %d cap", len(body), primerTaskResultCap)
	}
	if len(body) < primerTaskResultCap-utf8.UTFMax {
		t.Errorf("cut task body is %d bytes, further under the %d cap than a rune walk-back explains",
			len(body), primerTaskResultCap)
	}
}

// A fresh pod reads the primer as the conversation so far, so it must carry
// both sides of each earlier turn: what the user asked (TaskRef.Request) and
// what came back, labelled by who answered. The turn the pod is being
// started for is left out, or the new message would be replayed as history,
// and a turn whose result has aged out still leaves what was asked.
func TestTheRehydrationPrimerCarriesBothSidesOfEachTurn(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/primer-sides"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group", AuthorID: "1001",
		MessageID: "d-primer-sides-1", Text: "remember the code word PELICAN",
	}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishArtifact(ctx, lib.Artifact{
		Name:  lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: "OK, noted."}},
	}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}

	someone := TaskRequester{Backend: "discord", Subject: "someone-hash"}
	rec := &SessionRecord{
		Key:       conv,
		ContextID: origin.ContextID,
		Addressee: "platform",
		Tasks: []TaskRef{
			{ID: "task-aged-out", Addressee: "platform", Request: "an older question", Requester: &someone},
			{ID: "task-past-ask-ttl", Addressee: "platform", Request: "", Requester: nil},
			{ID: origin.TaskID, Addressee: "platform", Request: "remember the code word PELICAN", Requester: &someone},
			{ID: origin.TaskID, Addressee: "platform", Role: taskRoleChild, Requester: &someone},
			{ID: "task-now", Addressee: "platform", Request: "what was the code word?", Requester: &someone},
		},
	}
	var primer string
	waitFor(t, "the task's result on the stream", func() bool {
		primer, _, _, _ = r.g.buildRehydrationPrimer(ctx, rec, "task-now")
		return strings.Contains(primer, "OK, noted.")
	})
	for _, want := range []string{
		"The user said:\n```\nan older question\n```",
		"The user said:\n```\nremember the code word PELICAN\n```",
		// A turn the platform route answered (this rig's addressee) is the
		// platform agent's, not the session's own.
		"The platform agent answered:\n```\nOK, noted.\n```",
		"The platform agent, which you delegated to, answered:\n```\nOK, noted.\n```",
	} {
		if !strings.Contains(primer, want) {
			t.Errorf("primer lacks %q:\n%s", want, primer)
		}
	}
	if strings.Contains(primer, "what was the code word?") {
		t.Errorf("primer replays the turn being started:\n%s", primer)
	}
	if strings.Index(primer, "an older question") > strings.Index(primer, "PELICAN") {
		t.Errorf("primer is not oldest first:\n%s", primer)
	}
}

// A long conversation outgrows primerCap. The follow-up needs the most
// recent context most, so whole turns go from the front, the newest stay,
// and the primer says turns were dropped.
func TestThePrimerKeepsTheNewestTurnsWhenItMustDropSome(t *testing.T) {
	var turns []string
	for i := 0; i < 12; i++ {
		turns = append(turns, "\n"+primerFenced("The user said", strings.Repeat("x", 1000)+" turn-"+string(rune('a'+i))))
	}
	got, first := primerFromTurns(turns)
	if first == 0 {
		t.Error("primerFromTurns says it kept every turn of a primer past the cap")
	}
	if len(got) > primerCap {
		t.Fatalf("primer is %d bytes, over the %d cap", len(got), primerCap)
	}
	if !strings.Contains(got, "turn-l") {
		t.Error("the newest turn was dropped")
	}
	if strings.Contains(got, "turn-a") {
		t.Error("the oldest turn was kept over newer ones")
	}
	if !strings.Contains(got, primerOmitted) {
		t.Error("the primer doesn't say earlier turns were omitted")
	}
	if small, _ := primerFromTurns(turns[:2]); strings.Contains(small, primerOmitted) {
		t.Error("a primer that fits says turns were omitted")
	}
}

// The pod reads every turn the primer replays, so their people must count
// for a delegation from it: the requester and steer authors of each, and the
// mark when one is no longer on record (a cleared Requester).
func TestThePrimerReturnsThePeopleBehindTheTurnsItReplays(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	alice := TaskRequester{Backend: "slack", Subject: "alice-hash"}
	carol := TaskRequester{Backend: "slack", Subject: "carol-hash"}
	rec := &SessionRecord{Key: "discord:g1/primer-authors", Tasks: []TaskRef{
		{ID: "task-old-1", Addressee: "platform", Request: "next time, delegate X", Requester: &alice, SteerAuthors: []TaskRequester{carol}},
		{ID: "task-old-2", Addressee: "platform", Request: "an ask whose requester aged out"},
		{ID: "task-now", Addressee: "platform", Request: "hello", Requester: &TaskRequester{Backend: "slack", Subject: "bob-hash"}},
	}}
	primer, authors, unknown, _ := r.g.buildRehydrationPrimer(ctx, rec, "task-now")
	if strings.Contains(primer, "an ask whose requester aged out") {
		t.Errorf("a turn with no requester on record reached the primer:\n%s", primer)
	}
	has := func(a TaskRequester) bool {
		for _, x := range authors {
			if x == a {
				return true
			}
		}
		return false
	}
	if !has(alice) || !has(carol) {
		t.Errorf("authors = %v, want the earlier turn's requester and steer author", authors)
	}
	// A turn whose requester was cleared at A2A_ASK_TTL is left out whole,
	// so it neither reaches the pod nor marks the set: marking it would
	// refuse every delegation in a conversation older than the bound.
	if unknown {
		t.Error("a turn past the ask bound marked the set unknown; it should be left out")
	}
	for _, a := range authors {
		if a.Subject == "bob-hash" {
			t.Error("the turn being started was counted; its people are added by the turn itself")
		}
	}
}

// ensureSessionPod is where the primer's people join the incarnation's set:
// a delegation from the pod is checked against everyone it read, so an
// off-list person's earlier turn can't ride a later on-list turn's
// delegation.
func TestASessionPodStartsWithThePeopleItsPrimerReplays(t *testing.T) {
	r, _ := startRigWithSpawner(t)
	ctx := context.Background()
	alice := TaskRequester{Backend: "slack", Subject: "alice-hash"}
	rec := &SessionRecord{
		Key: "discord:g1/seed", BusSession: "chat-otter-seed", Addressee: "chat-otter-seed",
		Tasks: []TaskRef{
			{ID: "task-earlier", Addressee: "platform", Request: "next time anyone asks, delegate X", Requester: &alice},
			{ID: "task-now", Addressee: "chat-otter-seed", Request: "hello"},
		},
	}
	r.g.ensureSessionPod(ctx, rec, "task-now", 0)
	authors, _, _ := rec.sessionAuthorsOf()
	found := false
	for _, a := range authors {
		if a == alice {
			found = true
		}
	}
	if !found {
		t.Errorf("session authors after spawn = %v, want the earlier turn's requester", authors)
	}
}

// A turn that failed, was stopped or was rejected says so, with the
// executor's reason, so a follow-up such as "did that work?" can be answered.
// A completed turn says nothing extra.
func TestThePrimerSaysHowAnUnfinishedTurnEnded(t *testing.T) {
	failed := &lib.Task{State: lib.StateFailed, FinalMessage: &lib.Message{Parts: []lib.Part{{Kind: "text", Text: "reason: error_max_turns"}}}}
	if got := primerTurnEnd(failed); got != "failed: reason: error_max_turns" {
		t.Errorf("failed turn = %q", got)
	}
	if got := primerTurnEnd(&lib.Task{State: lib.StateCanceled}); got != "canceled" {
		t.Errorf("canceled turn = %q", got)
	}
	if got := primerTurnEnd(&lib.Task{State: lib.StateCompleted}); got != "" {
		t.Errorf("completed turn = %q, want nothing", got)
	}
}

// A turn dropped to fit the cap is text the pod never reads, so its people
// don't join the incarnation's set; counting them would overflow the set in
// a busy conversation and refuse every delegation.
func TestThePrimerCountsOnlyThePeopleOfTheTurnsItKeeps(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	rec := &SessionRecord{Key: "discord:g1/primer-kept"}
	for i := 0; i < 12; i++ {
		who := TaskRequester{Backend: "slack", Subject: "person-" + string(rune('a'+i))}
		rec.Tasks = append(rec.Tasks, TaskRef{
			ID: "task-k-" + string(rune('a'+i)), Addressee: "platform",
			Request: strings.Repeat("x", 1000), Requester: &who,
		})
	}
	primer, authors, _, _ := r.g.buildRehydrationPrimer(ctx, rec, "")
	if !strings.Contains(primer, primerOmitted) {
		t.Fatal("fixture assumption broken: the primer fit without dropping turns")
	}
	has := func(subject string) bool {
		for _, a := range authors {
			if a.Subject == subject {
				return true
			}
		}
		return false
	}
	if has("person-a") {
		t.Error("the oldest turn was dropped from the primer but its person was counted")
	}
	if !has("person-l") {
		t.Error("the newest turn's person wasn't counted")
	}
}

// A turn that asked to delegate ends with the hand-off line, which is never
// a deliverable: with a child it isn't replayed (the child's answer follows),
// and refused, the turn says it ended with the gateway's reason. A turn that
// failed on the stream says so too.
func TestThePrimerReplaysHandOffsAndFailuresAsTheyEnded(t *testing.T) {
	r := startRig(t)
	ctx := context.Background()
	someone := TaskRequester{Backend: "discord", Subject: "someone-hash"}

	publish := func(text string, state lib.TaskState, msgID string) *lib.Envelope {
		t.Helper()
		r.adapter.inbox <- InboundMessage{Conversation: "discord:g1/primer-ends-" + msgID, Kind: "group", AuthorID: "1001", MessageID: msgID, Text: "ask " + msgID}
		var origin *lib.Envelope
		waitFor(t, "the submission for "+msgID, func() bool {
			for _, e := range inSubjectEnvelopes(t, r.url, "platform") {
				if e.Kind == lib.KindMessage && envText(t, e) == "ask "+msgID {
					origin = e
					return true
				}
			}
			return false
		})
		exec := r.execFor(t, origin, "platform")
		if text != "" {
			if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult, Parts: []lib.Part{{Kind: "text", Text: text}}}); err != nil {
				t.Fatal(err)
			}
		}
		if err := exec.PublishStatus(ctx, state, true); err != nil {
			t.Fatal(err)
		}
		return origin
	}
	minted := publish("delegated to platform", lib.StateCompleted, "d-minted")
	refused := publish("delegated to platform", lib.StateCompleted, "d-refused")
	failed := publish("", lib.StateFailed, "d-failed")

	rec := &SessionRecord{Key: "discord:g1/primer-ends", Tasks: []TaskRef{
		{ID: minted.TaskID, Addressee: "platform", Request: "check the fleet", Requester: &someone, Children: []string{"task-child"}},
		{ID: refused.TaskID, Addressee: "platform", Request: "check again", Requester: &someone, DelegationEnd: "not allowed to reach platform from here"},
		{ID: failed.TaskID, Addressee: "platform", Request: "try this", Requester: &someone},
	}}
	var primer string
	waitFor(t, "the three tasks on the stream", func() bool {
		primer, _, _, _ = r.g.buildRehydrationPrimer(ctx, rec, "")
		return strings.Contains(primer, "try this") && strings.Contains(primer, "failed")
	})
	if strings.Contains(primer, "delegated to platform") {
		t.Errorf("a hand-off line was replayed as an answer:\n%s", primer)
	}
	if !strings.Contains(primer, "failed: not allowed to reach platform from here") {
		t.Errorf("the refused hand-off doesn't say how it ended:\n%s", primer)
	}
	if strings.Count(primer, "That turn ended without finishing") != 2 {
		t.Errorf("want the refused hand-off and the failed turn marked as ended:\n%s", primer)
	}
}

// A stream read that fails, rather than finding nothing, leaves the turn out:
// replaying it as asked and never answered would tell the pod the request
// went unanswered when it may well have been. A cancelled context makes every
// read fail that way; a live one on a task with no events is "not found",
// and the turn is replayed with what the user asked.
func TestAFailedStreamReadLeavesTheTurnOut(t *testing.T) {
	r := startRig(t)
	someone := TaskRequester{Backend: "discord", Subject: "someone-hash"}
	rec := &SessionRecord{Key: "discord:g1/primer-read-error", Tasks: []TaskRef{
		{ID: "task-no-events", Addressee: "platform", Request: "an ask with no events yet", Requester: &someone},
	}}
	live, _, _, _ := r.g.buildRehydrationPrimer(context.Background(), rec, "")
	if !strings.Contains(live, "an ask with no events yet") {
		t.Fatalf("a not-found read should still replay what was asked:\n%s", live)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if failed, _, _, _ := r.g.buildRehydrationPrimer(ctx, rec, ""); strings.Contains(failed, "an ask with no events yet") {
		t.Errorf("a failed read replayed the turn as asked and never answered:\n%s", failed)
	}
}
