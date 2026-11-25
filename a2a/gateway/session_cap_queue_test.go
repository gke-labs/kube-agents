package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// queueRig is a post-flip gateway whose cap of limit is full: every live
// slot is taken, each spawn takes another (liveSpawns), and free gives one
// back, as a pod that is reaped or exits would, and nudges the line the way
// the paths that free one do.
func queueRig(t *testing.T, limit int) (r *rig, spawn *fakeSpawner, free func()) {
	t.Helper()
	r, spawn = startRigWithSpawnerCap(t, RouteSession, limit, nil)
	spawn.mu.Lock()
	spawn.liveSpawns = true
	spawn.live = limit
	spawn.mu.Unlock()
	free = func() {
		spawn.mu.Lock()
		spawn.live--
		spawn.mu.Unlock()
		r.g.kickCapQueue()
	}
	return r, spawn, free
}

// queuedNoticeOf is the id of conv's queued notice, or "".
func queuedNoticeOf(r *rig, conv string) string {
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	for _, p := range r.adapter.posts {
		if p.Conversation == conv && strings.HasPrefix(p.Text, "⏳ **queued**") {
			return p.MessageID
		}
	}
	return ""
}

// noticeNow is what conv's queued notice reads now: its last edit, or the
// post itself.
func noticeNow(r *rig, conv string) string {
	id := queuedNoticeOf(r, conv)
	if id == "" {
		return ""
	}
	if edits := r.adapter.editsOf(id); len(edits) > 0 {
		return edits[len(edits)-1]
	}
	r.adapter.mu.Lock()
	defer r.adapter.mu.Unlock()
	for _, p := range r.adapter.posts {
		if p.MessageID == id {
			return p.Text
		}
	}
	return ""
}

func noticeReads(r *rig, conv, want string) func() bool {
	return func() bool { return noticeNow(r, conv) == want }
}

func started(r *rig, conv string) bool {
	rec, err := r.g.reg.Get(context.Background(), conv)
	return err == nil && rec != nil && rec.ActiveTask != nil
}

// At the cap with nothing to evict, a new conversation waits in line with
// the queued notice and its place, and a second waits behind it. A freed
// slot goes to the oldest, which starts its turn without being sent again;
// the second's notice moves up and it keeps waiting, the slot taken again.
func TestAtTheCapConversationsWaitInLineAndTheOldestStartsFirst(t *testing.T) {
	t.Parallel()
	r, spawn, free := queueRig(t, 1)
	first, second := "discord:g1/t-line-1", "discord:g1/t-line-2"
	say(r, first, "m1", "check the fleet")
	waitFor(t, "the first's notice, next in line", noticeReads(r, first, sessionQueuedNext))
	say(r, second, "m2", "check the nodes")
	waitFor(t, "the second's notice, one ahead", noticeReads(r, second, sessionQueuedOne))
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns at a full cap", n)
	}

	free()
	waitFor(t, "the first conversation's turn started", func() bool { return started(r, first) })
	waitFor(t, "the first's notice closed on starting", noticeReads(r, first, sessionQueuedStarting))
	waitFor(t, "the second's notice moved up", noticeReads(r, second, sessionQueuedNext))
	// The first's pod took the slot back, so another nudge admits nobody.
	r.g.kickCapQueue()
	time.Sleep(200 * time.Millisecond)
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns; want only the first conversation's", n)
	}
	if started(r, second) {
		t.Fatal("the second conversation started with no slot free")
	}

	free()
	waitFor(t, "the second conversation's turn started", func() bool { return started(r, second) })
}

// A fresh arrival with a non-empty line queues behind it, even with a slot
// free: the slot is the oldest waiter's.
func TestAFreshArrivalQueuesBehindTheLine(t *testing.T) {
	t.Parallel()
	r, _, _ := queueRig(t, 1)
	waiter, fresh := "discord:g1/t-behind-1", "discord:g1/t-behind-2"
	say(r, waiter, "m1", "check the fleet")
	waitFor(t, "the waiter's notice", noticeReads(r, waiter, sessionQueuedNext))
	// A slot frees with nothing nudging the line yet, then the fresh turn
	// arrives: it would fit, but the waiter is first.
	r.g.capPollOverride.Store(int64(time.Hour))
	r.g.kickCapQueue()
	time.Sleep(50 * time.Millisecond)
	spawnFree := func() {
		s := r.g.spawner.(*fakeSpawner)
		s.mu.Lock()
		s.live--
		s.mu.Unlock()
	}
	spawnFree()
	say(r, fresh, "m2", "check the nodes")
	waitFor(t, "the waiter's turn started", func() bool { return started(r, waiter) })
	if started(r, fresh) {
		t.Fatal("the fresh arrival jumped the line")
	}
	waitFor(t, "the fresh arrival's notice, now next", noticeReads(r, fresh, sessionQueuedNext))
}

// A later message in a waiting conversation waits behind its turn, and
// reaches the task that turn starts as a steer; a status ask is answered
// with the place in line at once.
func TestALaterMessageWaitsBehindTheParkedTurnAndSteersIt(t *testing.T) {
	t.Parallel()
	r, spawn, free := queueRig(t, 1)
	conv := "discord:g1/t-follow"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	say(r, conv, "m2", "status")
	waitFor(t, "the status ask answered with the place in line", func() bool {
		n := 0
		for _, p := range r.adapter.postTextsFor(conv) {
			if p == sessionQueuedNext {
				n++
			}
		}
		return n == 2
	})
	say(r, conv, "m3", "and the nodes too")
	time.Sleep(100 * time.Millisecond)
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns while waiting", n)
	}

	free()
	waitFor(t, "the turn started", func() bool { return started(r, conv) })
	waitFor(t, "the follow-up steered the started task", func() bool {
		return strings.Contains(r.logs.String(), "routing as steer")
	})
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns; the follow-up should steer the one task, not start another", n)
	}
}

// Past the wait bound the turn gets the line's give-up, telling the person
// to send it again, and its notice is closed; it is never started.
func TestAWaitPastTheBoundGetsTheGiveUp(t *testing.T) {
	t.Parallel()
	r, spawn, _ := queueRig(t, 1)
	r.g.capWaitOverride.Store(int64(300 * time.Millisecond))
	r.g.capPollOverride.Store(int64(50 * time.Millisecond))
	r.g.kickCapQueue()
	conv := "discord:g1/t-waited"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	waitFor(t, "the give-up", postedContaining(r, sessionQueueGaveUpNotice))
	waitFor(t, "the notice closed", noticeReads(r, conv, sessionQueuedGaveUp))
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns", n)
	}
	r.g.capMu.Lock()
	n := len(r.g.capQueue)
	r.g.capMu.Unlock()
	if n != 0 {
		t.Fatalf("%d waiters left in line", n)
	}
}

// The bound holds wherever a waiter is in line, not only at the head. With
// the head's try still in flight, a pass of the line refuses every waiter
// behind it that has waited past the bound, today's text and notice, and
// takes it out of the line; the waiters behind move up, and the head stays,
// as its own try meets the bound. When that try comes, the head is refused
// as before.
func TestAWaiterBehindTheHeadIsRefusedPastTheBound(t *testing.T) {
	t.Parallel()
	r, spawn, _ := queueRig(t, 1)
	r.g.capPollOverride.Store(int64(time.Hour))
	r.g.kickCapQueue()
	time.Sleep(50 * time.Millisecond)

	// The line built by hand, so each waiter's time in line is exact: the
	// head and the waiters at places 2 and 3 parked past the bound, those
	// at places 4 and 5 just now. The head's try is in flight (retrying),
	// so the pass leaves it to that try.
	convs := []string{"discord:g1/t-expire-head", "discord:g1/t-expire-2", "discord:g1/t-expire-3", "discord:g1/t-expire-4", "discord:g1/t-expire-5"}
	long := time.Now().Add(-sessionCapWaitMax - time.Minute)
	var line []*capWaiter
	r.g.capMu.Lock()
	for i, conv := range convs {
		at := time.Now()
		if i < 3 {
			at = long
		}
		w := &capWaiter{
			key: conv, parkedAt: at, shownAhead: -1,
			msg: InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: fmt.Sprintf("m%d", i), Text: "check the fleet"},
		}
		line = append(line, w)
		r.g.capQueue = append(r.g.capQueue, w)
	}
	line[0].retry = 1 << 62
	r.g.capMu.Unlock()
	for _, w := range line {
		r.g.postCapNotice(w)
	}
	for i, conv := range convs {
		if got := noticeNow(r, conv); got != sessionQueuedNotice(i) {
			t.Fatalf("%s's notice %q, want %q", conv, got, sessionQueuedNotice(i))
		}
	}

	r.g.kickCapQueue()
	refusal := sessionQueueGaveUp(0)
	for _, conv := range convs[1:3] {
		waitFor(t, conv+"'s refusal", func() bool { return slices.Contains(r.adapter.postTextsFor(conv), refusal) })
		waitFor(t, conv+"'s notice closed", noticeReads(r, conv, sessionQueuedGaveUp))
	}
	waitFor(t, "place 4 moved up to 2", noticeReads(r, convs[3], sessionQueuedOne))
	waitFor(t, "place 5 moved up to 3", noticeReads(r, convs[4], fmt.Sprintf(sessionQueuedMany, 2)))
	r.g.capMu.Lock()
	var keys []string
	for _, w := range r.g.capQueue {
		keys = append(keys, w.key)
	}
	r.g.capMu.Unlock()
	if want := []string{convs[0], convs[3], convs[4]}; !slices.Equal(keys, want) {
		t.Fatalf("line %q, want %q", keys, want)
	}
	if got := noticeNow(r, convs[0]); got != sessionQueuedNext {
		t.Fatalf("the head's notice %q while its try is in flight, want it still next", got)
	}
	if slices.Contains(r.adapter.postTextsFor(convs[0]), refusal) {
		t.Fatal("the head was refused by the pass, not by its own try")
	}

	// The head's try comes back through the cap check, finds no room, and
	// meets the bound there as before.
	r.g.capMu.Lock()
	line[0].retry = 0
	r.g.capMu.Unlock()
	r.g.kickCapQueue()
	waitFor(t, "the head's refusal", func() bool { return slices.Contains(r.adapter.postTextsFor(convs[0]), refusal) })
	waitFor(t, "the head's notice closed", noticeReads(r, convs[0], sessionQueuedGaveUp))
	waitFor(t, "the waiters behind moved up again", noticeReads(r, convs[3], sessionQueuedNext))
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns", n)
	}
}

// With the line full, a new conversation gets the cap's refusal and no
// notice.
func TestAFullLineRefusesWithTheCapText(t *testing.T) {
	t.Parallel()
	r, _, _ := queueRig(t, 1)
	for i := 0; i < sessionCapQueueMax; i++ {
		say(r, fmt.Sprintf("discord:g1/t-full-%d", i), fmt.Sprintf("m%d", i), "check the fleet")
	}
	waitFor(t, "a full line", func() bool {
		r.g.capMu.Lock()
		defer r.g.capMu.Unlock()
		return len(r.g.capQueue) == sessionCapQueueMax
	})
	late := "discord:g1/t-full-late"
	say(r, late, "m-late", "check the fleet")
	waitFor(t, "today's refusal", func() bool {
		for _, p := range r.adapter.postTextsFor(late) {
			if strings.HasPrefix(p, "🚦 not started: 1 session worker is already running (cap 1)") {
				return true
			}
		}
		return false
	})
	if id := queuedNoticeOf(r, late); id != "" {
		t.Fatal("the refused conversation got a queued notice")
	}
}

// A stop takes a waiting turn out of the line and says so, typed or as a
// program's cancel; a freed slot then starts nothing for it.
func TestAStopTakesAWaitingTurnOutOfTheLine(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		msg  func(conv string) InboundMessage
	}{
		{"typed", func(conv string) InboundMessage {
			return InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-stop", Text: "stop"}
		}},
		{"cancel intent", func(conv string) InboundMessage {
			return InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-stop", Intent: IntentCancel}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r, spawn, free := queueRig(t, 1)
			conv := "discord:g1/t-stopped"
			say(r, conv, "m1", "check the fleet")
			waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
			r.adapter.inbox <- tc.msg(conv)
			waitFor(t, "the notice closed", noticeReads(r, conv, sessionQueuedStopped))
			if slices.Contains(r.adapter.postTextsFor(conv), sessionQueueStoppedNotice) {
				t.Fatal("the stop was said twice: the notice's edit and a post")
			}
			free()
			time.Sleep(200 * time.Millisecond)
			if n := len(spawn.calls()); n != 0 {
				t.Fatalf("%d spawns after the stop", n)
			}
			if started(r, conv) {
				t.Fatal("a stopped turn started")
			}
		})
	}
}

// Below the cap nothing changes: the turn starts at once, with no notice.
func TestBelowTheCapNoTurnWaits(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 2, nil)
	spawn.setLive(1)
	conv := "discord:g1/t-below"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the spawn", func() bool { return len(spawn.calls()) == 1 })
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "**queued**") {
			t.Fatalf("a queued notice below the cap: %q", p)
		}
	}
}

// Which doors park. The chat backends, the console and the inject door (the
// eval harness's stand-in for a chat user) wait in line; the A2A door classes
// keep the cap's refusal, since their callers read a turn that ended without
// a task as nothing started.
func TestWhichDoorsWaitAtTheCap(t *testing.T) {
	for _, b := range []string{a2aBackend, a2aGoogleBackend, a2aEntraBackend} {
		if parksAtCap(b) {
			t.Errorf("%s turns park at the cap", b)
		}
	}
	for _, b := range []string{injectBackend, discordBackend, slackBackend, gchatBackend, teamsBackend, consoleBackend} {
		if !parksAtCap(b) {
			t.Errorf("%s turns do not park at the cap", b)
		}
	}
}

// An inject turn at the cap waits as a chat turn does, and its caller sees
// it the way a chat user would: the POST is answered with the queued refusal
// and the queued notice; once a slot frees, the task the turn starts, its
// answer and its terminal land on the same conversation, after the notice.
// The door's turn bookkeeping survives the redelivery: the conversation's
// next POST is answered with its own task, not the redelivered one's.
func TestAnInjectTurnWaitsAtTheCapAndItsCallerReadsTheAnswer(t *testing.T) {
	t.Parallel()
	spawn := &fakeSpawner{liveSpawns: true, live: 1}
	r := startInjectRigSpawning(t, func(c *Config) {
		c.DefaultAddressee = RouteSession
		c.MaxSessions = 1
	}, spawn)

	first := r.inject(t, "cap-queue", injectTestAuthor, "reply with the word PINEAPPLE")
	if first.Refusal != injectRefusalQueued || first.TaskID != "" || first.Accepted {
		t.Fatalf("the parked POST: refusal=%q task=%q accepted=%v, want the queued refusal and no task",
			first.Refusal, first.TaskID, first.Accepted)
	}
	key := first.Conversation
	if posts := entryTexts(first.Entries, InjectEntryPost); len(posts) != 1 || posts[0] != sessionQueuedNext {
		t.Fatalf("the parked POST's entries %q, want the queued notice alone", posts)
	}
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns at a full cap", n)
	}

	spawn.mu.Lock()
	spawn.live--
	spawn.mu.Unlock()
	r.g.kickCapQueue()
	waitFor(t, "the parked turn's spawn", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	origin := r.awaitTask(t, session)
	completeTask(t, r.execFor(t, origin, session), "PINEAPPLE")
	r.waitForTerminal(t, key, origin.TaskID)

	entries, _, _ := r.adapter.snapshot(key, 0, "")
	noticeSeq, taskSeq, answerSeq := 0, 0, 0
	for _, e := range entries {
		switch {
		case e.Kind == InjectEntryPost && e.Text == sessionQueuedNext && noticeSeq == 0:
			noticeSeq = e.Seq
		case e.Kind == InjectEntryTask && e.TaskID == origin.TaskID:
			taskSeq = e.Seq
		case e.Kind == InjectEntryPost && strings.Contains(e.Text, "PINEAPPLE") && answerSeq == 0:
			answerSeq = e.Seq
		}
	}
	if noticeSeq == 0 || taskSeq <= noticeSeq || answerSeq <= taskSeq {
		t.Fatalf("notice at %d, task at %d, answer at %d: want the notice, then the task, then the answer\n%+v",
			noticeSeq, taskSeq, answerSeq, entries)
	}
	if edits := entryTexts(entries, InjectEntryEdit); !slices.Contains(edits, sessionQueuedStarting) {
		t.Fatalf("the queued notice was never closed on starting: edits %q", edits)
	}

	r.awaitTurnEnd(t, key)
	r.awaitRecordRelease(t, key)
	second := r.inject(t, "cap-queue", injectTestAuthor, "and again")
	if !second.Accepted || second.TaskID == "" || second.TaskID == origin.TaskID {
		t.Fatalf("the next POST: accepted=%v task=%q refusal=%q, want its own task (the redelivered one was %s)",
			second.Accepted, second.TaskID, second.Refusal, origin.TaskID)
	}
}

// The door counts a redelivery as handed, and refuses one while a turn it
// handed over has not ended, so a POST waiting on its turn never reads a
// redelivered turn's task as its own. A replay of an admitted turn's
// follow-ups is counted unconditionally.
func TestTheInjectDoorClaimsARedeliveryOnlyWhenQuiet(t *testing.T) {
	t.Parallel()
	door, err := NewInjectAdapter("127.0.0.1:0", injectTestToken, injectTestGrace, nil)
	if err != nil {
		t.Fatal(err)
	}
	const key = injectKeyPrefix + "redeliver"
	door.mu.Lock()
	door.conversationLocked(key).handed = 1
	door.mu.Unlock()
	if door.ClaimRedelivery(key, 2) {
		t.Fatal("a redelivery claimed while a POST's turn runs")
	}
	door.TurnFinished(key)
	if !door.ClaimRedelivery(key, 2) {
		t.Fatal("a redelivery refused on a quiet conversation")
	}
	door.mu.Lock()
	conv := door.conversationLocked(key)
	handed, turns := conv.handed, conv.turns
	door.mu.Unlock()
	if handed != 3 || turns != 1 {
		t.Fatalf("handed=%d turns=%d after the claim, want 3 and 1", handed, turns)
	}
	// An admitted turn's follow-ups are replayed while that turn, one the
	// line claimed, still runs: counted all the same.
	door.ClaimReplay(key, 2)
	door.mu.Lock()
	handed = door.conversationLocked(key).handed
	door.mu.Unlock()
	if handed != 5 {
		t.Fatalf("handed=%d after the replay's claim, want 5", handed)
	}
}

// A gateway that stops with turns in line tells each it was not started.
func TestAStoppingGatewayRefusesTheTurnsInLine(t *testing.T) {
	t.Parallel()
	r, _, _ := queueRig(t, 1)
	conv := "discord:g1/t-restart"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	r.stop()
	select {
	case <-r.ran:
	case <-time.After(10 * time.Second):
		t.Fatal("the gateway did not stop")
	}
	waitFor(t, "the restart refusal", postedContaining(r, sessionQueueRestartNotice))
	waitFor(t, "the notice closed", noticeReads(r, conv, sessionQueuedRestart))
}

// Arrivals and freed slots racing never take the count past the cap, and
// every slot freed is taken by a waiter.
func TestTheLineNeverOvershootsTheCap(t *testing.T) {
	t.Parallel()
	const limit, convs, frees = 3, 12, 6
	r, spawn, free := queueRig(t, limit)
	r.g.capPollOverride.Store(int64(20 * time.Millisecond))
	r.g.kickCapQueue()
	var wg sync.WaitGroup
	for i := 0; i < convs; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			say(r, fmt.Sprintf("discord:g1/t-race-%d", i), fmt.Sprintf("m%d", i), "check the fleet")
		}(i)
	}
	for i := 0; i < frees; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			free()
		}()
	}
	wg.Wait()
	waitFor(t, "every freed slot taken", func() bool { return len(spawn.calls()) == frees })
	time.Sleep(200 * time.Millisecond)
	spawn.mu.Lock()
	maxLive, n := spawn.maxLive, len(spawn.spawns)
	spawn.mu.Unlock()
	if maxLive > limit {
		t.Fatalf("live reached %d under a cap of %d", maxLive, limit)
	}
	if n != frees {
		t.Fatalf("%d spawns for %d freed slots", n, frees)
	}
	r.g.capMu.Lock()
	waiting := len(r.g.capQueue)
	r.g.capMu.Unlock()
	if waiting != convs-frees {
		t.Fatalf("%d waiting; want %d", waiting, convs-frees)
	}
}

// steerTexts is the text of every message on session's in subject after its
// task's own, in the order they were published: the steers it received.
func steerTexts(t *testing.T, r *rig, session string) []string {
	t.Helper()
	var out []string
	for i, e := range inSubjectEnvelopes(t, r.url, session) {
		var m lib.Message
		if i > 0 && e.Kind == lib.KindMessage && json.Unmarshal(e.Payload, &m) == nil {
			out = append(out, joinTextParts(m.Parts))
		}
	}
	return out
}

// holdInbox takes conv's session lock and puts msg on its inbox, and returns
// once the conversation's inbox worker has taken msg, which then waits on the
// lock, so whatever the line puts on the inbox after it runs after it. The
// returned func lets the turn go.
func holdInbox(t *testing.T, r *rig, conv string, msg InboundMessage) func() {
	t.Helper()
	l := r.g.lockSession(conv)
	l.Lock()
	r.adapter.inbox <- msg
	waitFor(t, "the inbox worker took "+msg.MessageID, func() bool {
		r.g.inbox.mu.Lock()
		defer r.g.inbox.mu.Unlock()
		pending, running := r.g.inbox.m[conv]
		return running && len(pending) == 0
	})
	return l.Unlock
}

// waitRetrying waits until the head's retry is on the inbox.
func waitRetrying(t *testing.T, r *rig, conv string) {
	t.Helper()
	waitFor(t, "the head's retry on the inbox", func() bool {
		r.g.capMu.Lock()
		defer r.g.capMu.Unlock()
		w, i := r.g.capWaiterLocked(conv)
		return i == 0 && w.retry != 0
	})
}

func followUpsOf(r *rig, conv string) int {
	r.g.capMu.Lock()
	defer r.g.capMu.Unlock()
	if w, _ := r.g.capWaiterLocked(conv); w != nil {
		return len(w.followUps)
	}
	return -1
}

func queuedNotices(r *rig, conv string) int {
	n := 0
	for _, p := range r.adapter.postTextsFor(conv) {
		if strings.HasPrefix(p, "⏳ **queued**") {
			n++
		}
	}
	return n
}

// Later messages of a parked conversation reach the task its turn starts
// once admitted, in the order they were sent, as steers of that one task.
func TestFollowUpsSteerTheAdmittedTurnInOrder(t *testing.T) {
	t.Parallel()
	r, spawn, free := queueRig(t, 1)
	conv := "discord:g1/t-follow-order"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	say(r, conv, "m2", "first, the nodes")
	say(r, conv, "m3", "then the pods")
	waitFor(t, "both follow-ups behind the turn", func() bool { return followUpsOf(r, conv) == 2 })

	free()
	waitFor(t, "the spawn", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	origin := r.awaitTask(t, session)
	exec := r.execFor(t, origin, session)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	want := []string{"first, the nodes", "then the pods"}
	waitFor(t, "both steers, in order", func() bool { return slices.Equal(steerTexts(t, r, session), want) })
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns; the follow-ups should steer the one task", n)
	}
}

// A head that waits out its bound is refused, and its follow-ups are dropped
// with it, as for a waiter behind it that expires: none of them starts a
// task or parks again as a fresh turn.
func TestAHeadThatGivesUpDropsItsFollowUps(t *testing.T) {
	t.Parallel()
	r, spawn, free := queueRig(t, 1)
	r.g.capPollOverride.Store(int64(time.Hour))
	r.g.kickCapQueue()
	conv := "discord:g1/t-giveup-follow"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	say(r, conv, "m2", "and the nodes too")
	waitFor(t, "the follow-up behind the turn", func() bool { return followUpsOf(r, conv) == 1 })

	r.g.capWaitOverride.Store(1)
	r.g.kickCapQueue()
	refusal := sessionQueueGaveUp(1)
	waitFor(t, "the give-up, naming the dropped follow-up", func() bool { return slices.Contains(r.adapter.postTextsFor(conv), refusal) })
	waitFor(t, "the notice closed", noticeReads(r, conv, sessionQueuedGaveUp))

	// A slot frees: nothing of the conversation's is left to take it.
	free()
	time.Sleep(300 * time.Millisecond)
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns after the head gave up", n)
	}
	if n := queuedNotices(r, conv); n != 1 {
		t.Fatalf("%d queued notices; the follow-up parked again as a fresh turn", n)
	}
	n := 0
	for _, p := range r.adapter.postTextsFor(conv) {
		if p == refusal {
			n++
		}
	}
	if n != 1 {
		t.Fatalf("%d refusals, want one", n)
	}
	r.g.capMu.Lock()
	waiting, stashed := len(r.g.capQueue), len(r.g.capAdmitted)
	r.g.capMu.Unlock()
	if waiting != 0 || stashed != 0 {
		t.Fatalf("%d waiting, %d stashed, want none", waiting, stashed)
	}
}

// A stop already on the inbox when the head's retry is put there runs first
// and takes the turn out of the line; the retry behind it is dropped, not
// started as a fresh turn.
func TestARetryBehindAStopIsDropped(t *testing.T) {
	t.Parallel()
	r, spawn, free := queueRig(t, 1)
	r.g.capPollOverride.Store(int64(time.Hour))
	r.g.kickCapQueue()
	conv := "discord:g1/t-stop-retry"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))

	release := holdInbox(t, r, conv, InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m-stop", Text: "stop"})
	free()
	waitRetrying(t, r, conv)
	release()

	waitFor(t, "the notice closed on stopped", noticeReads(r, conv, sessionQueuedStopped))
	time.Sleep(300 * time.Millisecond)
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns; the stopped turn started", n)
	}
	if started(r, conv) || queuedNotices(r, conv) != 1 {
		t.Fatalf("the stopped turn came back: started=%v notices=%d", started(r, conv), queuedNotices(r, conv))
	}
	waitFor(t, "the retry dropped, and said so in the log", func() bool {
		return strings.Contains(r.logs.String(), "dropped a queued turn's retry")
	})
}

// A follow-up already on the inbox when the head's retry is put there joins
// the waiter, and when the retry is admitted it reaches the task the turn
// started, not lost with the waiter.
func TestAFollowUpAheadOfTheRetryReachesTheAdmittedTurn(t *testing.T) {
	t.Parallel()
	r, spawn, free := queueRig(t, 1)
	r.g.capPollOverride.Store(int64(time.Hour))
	r.g.kickCapQueue()
	conv := "discord:g1/t-follow-retry"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))

	release := holdInbox(t, r, conv, InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m2", Text: "and the nodes too"})
	free()
	waitRetrying(t, r, conv)
	release()

	waitFor(t, "the spawn", func() bool { return len(spawn.calls()) == 1 })
	session := spawn.calls()[0].Session
	origin := r.awaitTask(t, session)
	exec := r.execFor(t, origin, session)
	if err := exec.PublishStatus(context.Background(), lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "the follow-up steered the started task", func() bool {
		return slices.Equal(steerTexts(t, r, session), []string{"and the nodes too"})
	})
	if n := len(spawn.calls()); n != 1 {
		t.Fatalf("%d spawns", n)
	}
}

// A try the cap check kept ends after the line has already put the next try
// on the inbox: the ending try is not the one in flight, so the waiter stays
// in line for the next, rather than leaving it as a try that never reached
// the check (and the next being dropped as a retry whose waiter is gone).
func TestAKeptTryEndingAfterTheNextIsQueuedLeavesTheWaiterInLine(t *testing.T) {
	t.Parallel()
	r, _, _ := queueRig(t, 1)
	r.g.capPollOverride.Store(int64(time.Hour))
	conv := "discord:g1/t-kept-try"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	// The park's own nudge sends the head to try once, and with no room it
	// is kept; wait for that try's turn to be over.
	waitFor(t, "the conversation's inbox idle, its try kept", func() bool {
		r.g.inbox.mu.Lock()
		_, running := r.g.inbox.m[conv]
		r.g.inbox.mu.Unlock()
		r.g.capMu.Lock()
		defer r.g.capMu.Unlock()
		w, _ := r.g.capWaiterLocked(conv)
		return !running && w != nil && w.retry == 0
	})

	// A try in flight, which the check keeps; then the next try is put on
	// the inbox before the kept one's turn has ended.
	r.g.capMu.Lock()
	w, _ := r.g.capWaiterLocked(conv)
	r.g.capTicket++
	first := r.g.capTicket
	w.retry = first
	r.g.capMu.Unlock()
	var after []func()
	if !r.g.capKeepOrGiveUp(w, &after) {
		t.Fatal("the first try gave up inside the bound")
	}
	r.g.capMu.Lock()
	r.g.capTicket++
	w.retry = r.g.capTicket
	r.g.capMu.Unlock()

	r.g.capRetryDone(InboundMessage{Conversation: conv, capRetry: first})
	if !r.g.parkedAtCap(conv) {
		t.Fatal("the waiter left the line as the kept try ended, with its next try in flight")
	}
}

// A head that has waited out the bound and whose try then can't list the
// session pods is answered once, with the give-up, not also with the
// can't-count refusal.
func TestAHeadPastTheBoundWithAPodListErrorIsAnsweredOnce(t *testing.T) {
	t.Parallel()
	r, spawn, _ := queueRig(t, 1)
	conv := "discord:g1/t-waited-listerr"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	spawn.setLiveErr(errors.New("api down"))
	r.g.capWaitOverride.Store(1)
	r.g.kickCapQueue()
	waitFor(t, "the give-up", postedContaining(r, sessionQueueGaveUpNotice))
	waitFor(t, "the notice closed", noticeReads(r, conv, sessionQueuedGaveUp))
	time.Sleep(200 * time.Millisecond)
	for _, p := range r.adapter.postTextsFor(conv) {
		if strings.HasPrefix(p, "⚠️ not started") {
			t.Fatalf("a second not-started notice: %q", p)
		}
	}
}

// A turn whose heal wakes the session at the cap is not parked by the
// wake's cap check: the wake is refused on its own, and the turn parks once,
// on its own route, with nothing queued behind it.
func TestAHealsWakeAtTheCapDoesNotParkTheTurn(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawner(t)
	conv := "discord:g1/t-heal-wake-cap"
	_, _, child := delegated(t, r, spawn, conv, "")
	waitFor(t, "parent terminal relayed", postedContaining(r, "delegated to platform"))
	cexec := r.execFor(t, child, targetPlatform)
	r2, spawn2 := restartRig(t, r, func() {
		completeTask(t, cexec, "fleet is green")
		drainRelayDurable(t, r.url)
		r.g.cfg.MaxSessions = 1
		r.g.cfg.DefaultAddressee = RouteSession
	})
	spawn2.mu.Lock()
	spawn2.liveSpawns = true
	spawn2.live = 2
	spawn2.mu.Unlock()

	sessionRigTurn(r2, conv, "h-heal", "check the nodes")
	waitFor(t, "the heal's status card", postedContaining(r2, "🔎 task `"+child.TaskID+"` is **completed**"))
	waitFor(t, "the turn in line", func() bool { return r2.g.parkedAtCap(conv) })
	time.Sleep(200 * time.Millisecond)
	if n := followUpsOf(r2, conv); n != 0 {
		t.Fatalf("%d follow-ups behind the parked turn; the turn was parked by the wake and queued behind itself", n)
	}
	r2.adapter.mu.Lock()
	notices := 0
	for _, p := range r2.adapter.posts {
		if p.Conversation == conv && strings.HasPrefix(p.Text, "⏳ **queued**") {
			notices++
		}
	}
	r2.adapter.mu.Unlock()
	if notices != 1 {
		t.Fatalf("%d queued notices, want 1", notices)
	}
}

// A turn replacing its own pod frees a slot as it takes one, so it starts
// with a line waiting, as it did before the line existed.
func TestAReplacingTurnNeedNotWaitBehindTheLine(t *testing.T) {
	t.Parallel()
	r, spawn, _ := queueRig(t, 1)
	waiting := "discord:g1/t-replace-waiting"
	say(r, waiting, "m1", "check the fleet")
	waitFor(t, "the waiter's notice", noticeReads(r, waiting, sessionQueuedNext))

	conv := "discord:g1/t-replace-own"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-replace", Kind: "group",
		Addressee: "chat-otter-bbbb", BusSession: "chat-otter-bbbb",
		PodName: "chat-otter-bbbb",
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group", AuthorID: "1001", MessageID: "m2", Text: "Delegate: try again"}
	waitFor(t, "the replacement spawn", func() bool { return len(spawn.calls()) == 1 })
	if queuedNoticeOf(r, conv) != "" {
		t.Fatal("the replacing turn was queued")
	}
	if !r.g.parkedAtCap(waiting) {
		t.Fatal("the waiter left the line")
	}
}

// After the stop has refused the line, a turn reaching the cap check is
// refused rather than parked where nothing would answer it.
func TestNoTurnParksAfterTheStopRefusedTheLine(t *testing.T) {
	t.Parallel()
	r, _, _ := queueRig(t, 1)
	r.g.refuseParkedAtStop()
	conv := "discord:g1/t-after-stop"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the cap refusal", postedContaining(r, "🚦 not started: 1 session worker is already running (cap 1)"))
	if r.g.parkedAtCap(conv) || queuedNoticeOf(r, conv) != "" {
		t.Fatal("a turn parked after the stop")
	}
}

// capLineLen is how many turns are in the session-cap line.
func capLineLen(r *rig) int {
	r.g.capMu.Lock()
	defer r.g.capMu.Unlock()
	return len(r.g.capQueue)
}

// With the warm pool on, a full cap still holds a waiter back even while an
// unclaimed warm pod stands ready: claiming it would take a slot. When a
// slot frees, the oldest waiter starts on that warm pod rather than a cold
// spawn, so the line and the pool share one count (acquireCapCheck).
func TestAWaiterAtAFullCapStartsOnTheWarmPodWhenASlotFrees(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, warmOn(1, 4))
	spawn.mu.Lock()
	spawn.liveSpawns = true
	spawn.live = 4
	spawn.mu.Unlock()
	spawn.setWarm(readyWarm("chat-warm-1"))
	conv := "discord:g1/t-warm-waiter"

	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the waiter's notice, next in line", noticeReads(r, conv, sessionQueuedNext))
	if claims := warmClaims(t, r); len(claims) != 0 {
		t.Fatalf("a warm pod was claimed at a full cap: %v", claims)
	}

	spawn.mu.Lock()
	spawn.live--
	spawn.mu.Unlock()
	r.g.kickCapQueue()
	waitFor(t, "the waiter's turn started", func() bool { return started(r, conv) })
	if got := warmClaims(t, r)["chat-warm-1"]; got != conv {
		t.Fatalf("the warm pod's claim = %q, want the waiter %q", got, conv)
	}
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d cold spawns; the waiter should have started on the warm pod", n)
	}
}
