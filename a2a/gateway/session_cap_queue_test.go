package gateway

import (
	"context"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
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

// Past the wait bound the turn gets the cap's refusal, today's text, and its
// notice is closed; it is never started.
func TestAWaitPastTheBoundGetsTheCapRefusal(t *testing.T) {
	t.Parallel()
	r, spawn, _ := queueRig(t, 1)
	r.g.capWaitOverride.Store(int64(300 * time.Millisecond))
	r.g.capPollOverride.Store(int64(50 * time.Millisecond))
	r.g.kickCapQueue()
	conv := "discord:g1/t-waited"
	say(r, conv, "m1", "check the fleet")
	waitFor(t, "the notice", noticeReads(r, conv, sessionQueuedNext))
	waitFor(t, "today's refusal", postedContaining(r, "🚦 not started: 1 session worker is already running (cap 1)"))
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
			waitFor(t, "the stop said", postedContaining(r, sessionQueueStoppedNotice))
			waitFor(t, "the notice closed", noticeReads(r, conv, sessionQueuedStopped))
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

// A door's turn never waits: its caller holds the turn to a bound, so it
// keeps the cap's refusal.
func TestDoorTurnsDoNotWaitAtTheCap(t *testing.T) {
	for _, b := range []string{injectBackend, a2aBackend, a2aGoogleBackend, a2aEntraBackend} {
		if parksAtCap(b) {
			t.Errorf("%s turns park at the cap", b)
		}
	}
	for _, b := range []string{discordBackend, slackBackend, gchatBackend, teamsBackend, consoleBackend} {
		if !parksAtCap(b) {
			t.Errorf("%s turns do not park at the cap", b)
		}
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
