package gateway

import (
	"context"
	"fmt"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// The session-cap line (#6882). At the cap with nothing to evict, a turn
// that needs a fresh session pod waits in a bounded FIFO line instead of
// being refused, and starts when a slot frees. Chat backends, the console and
// the inject door park; the A2A door classes keep the refusal (parksAtCap).
//
// Parking never blocks a goroutine. Inbound turns run one at a time per
// conversation on its inbox worker (keyedQueue), under its session lock, so
// a turn that waited in place would hold the conversation's later messages,
// its stop included, behind it. A parked turn is a record instead: the
// message as handleInbound received it, kept in memory, and the turn
// returns the way a refused one does, leaving the session record as it was.
// To try again the message is put back on its conversation's inbox
// (retryCapHead), so it re-enters the normal path, reaches
// refuseAtSessionCap again, and takes the slot under the same check, the
// same count and the same eviction a fresh arrival does.
//
// One retry is in flight at a time, the head's: the line's order is the
// order slots are offered in. A fresh arrival checks the line under the cap
// check and queues behind a non-empty one rather than taking a slot or
// evicting for itself, so the cap, the eviction and the line share one count
// under one lock.
const (
	// sessionCapWaitMax is how long a turn waits in line before it gets the
	// cap's refusal after all, wherever it is in the line: the head is told
	// when its next try finds no room (capKeepOrGiveUp), a waiter behind it
	// on the line's next pass (expireCapWaiters), so within
	// sessionCapQueuePoll of the bound. A slot frees when any running conversation's
	// task ends (its pod idles and the cap evicts it) or its pod exits, so
	// at the default cap of 10 the wait is the shortest of ten tasks still
	// running. Five minutes outlasts a typical chat task, so most waits end
	// in a start; past it the person is better told plainly that the board
	// is full, with the operator's knob, than left looking at a line that
	// barely moves behind long tasks (the task deadline is 30 minutes).
	sessionCapWaitMax = 5 * time.Minute
	// sessionCapQueueMax is how many conversations wait in line at once:
	// twice the default cap. A longer line would rarely reach the front
	// inside sessionCapWaitMax, and each waiter costs a refusal post if the
	// gateway stops while it waits.
	sessionCapQueueMax = 20
	// sessionCapFollowUpsMax is how many later messages of a parked
	// conversation wait behind its parked turn, as the console's own
	// per-conversation queue is bounded (consoleQueueCap).
	sessionCapFollowUpsMax = 8
	// sessionCapQueuePoll is how often the head of the line tries for a
	// slot with nothing having nudged it: the backstop for a slot that
	// frees without a gateway path seeing it (a one-task pod that exits on
	// its own after its terminal was relayed). Each try is one turn's work
	// for the head conversation (a record read, a roster read, the pod
	// list), so not much more often than this.
	sessionCapQueuePoll = 15 * time.Second
	// sessionCapStopNoticeWait bounds the refusal posts a stopping gateway
	// makes to the turns still in line, inside the stop's own drain.
	sessionCapStopNoticeWait = 5 * time.Second
)

// The queued notice: one post per parked turn, edited in place as the line
// moves and once more when the turn leaves it. Worded as the platform
// route's busy notice is (busy.go), with no promise about time.
const (
	sessionQueuedNext = "⏳ **queued** — every session worker is busy and yours is next in line; I'll start as soon as there's room"
	sessionQueuedOne  = "⏳ **queued** — every session worker is busy and 1 conversation is ahead of yours; I'll start as soon as there's room"
	sessionQueuedMany = "⏳ **queued** — every session worker is busy and %d conversations are ahead of yours; I'll start as soon as there's room"

	sessionQueuedStarting = "▶️ **starting** — a session worker is free"
	sessionQueuedStopped  = "🛑 **stopped** — taken out of the line; nothing was started"
	sessionQueuedGaveUp   = "🚦 **not started** — waited too long for a session worker"
	sessionQueuedLeft     = "⏳ **no longer queued**"
	sessionQueuedRestart  = "🚦 **not started** — the gateway restarted"

	sessionQueueStoppedNotice  = "🛑 stopped — this conversation was waiting for a session worker; it has been taken out of the line and nothing was started"
	sessionQueueRestartNotice  = "🚦 not started: the gateway restarted while this conversation waited for a session worker — send it again"
	sessionQueueFollowUpNotice = "🚦 not started: this conversation is already waiting for a session worker with %d messages behind it — send this one once it starts"
)

// sessionQueuedNotice is the queued notice for a turn with ahead turns in
// front of it.
func sessionQueuedNotice(ahead int) string {
	switch ahead {
	case 0:
		return sessionQueuedNext
	case 1:
		return sessionQueuedOne
	}
	return fmt.Sprintf(sessionQueuedMany, ahead)
}

// capWaiter is one parked turn. ticket, key and parkedAt never change;
// msg, followUps and retrying are guarded by Gateway.capMu; noticeID and
// shownAhead by editMu, which also orders every edit of the notice, so a
// position edit can never land after the edit that closed it.
type capWaiter struct {
	ticket   uint64
	key      string
	parkedAt time.Time
	// msg is the parked turn as handleInbound received it, and followUps
	// the conversation's later messages, in order (routeParked).
	msg       InboundMessage
	followUps []InboundMessage
	// retrying is the head's message put back on the inbox and not yet
	// through refuseAtSessionCap.
	retrying bool
	// closed is set as the waiter leaves the line, before its last edit.
	closed atomic.Bool

	editMu     sync.Mutex
	noticeID   string
	shownAhead int
}

// capParkKey carries the turn a session-cap check may park, set by
// routeTurn for a turn whose backend may park (parksAtCap). A check without
// it (an A2A door turn, a test calling refuseAtSessionCap directly) refuses
// at the cap as before the line existed.
type capParkKey struct{}

func withParkableTurn(ctx context.Context, msg InboundMessage) context.Context {
	return context.WithValue(ctx, capParkKey{}, msg)
}

func parkableTurn(ctx context.Context) (InboundMessage, bool) {
	msg, ok := ctx.Value(capParkKey{}).(InboundMessage)
	return msg, ok
}

// parksAtCap reports whether a turn from backend may wait in the line: the
// chat backends, the console, and the inject door. The inject door is the
// eval harness's stand-in for a chat user (armed only on an eval install), so
// its turns wait as a chat user's do, and its caller learns so as a fact: the
// gateway tells the door the turn is queued (CapLineObserver.TurnQueued), the
// POST is answered with the queued refusal rather than "no task", and the
// queued notice, the task the turn later starts and its answer land on the
// conversation the caller reads, as they land in a chat thread. The A2A door
// classes keep the cap's refusal: their callers are programs that read a turn
// ended without a task as nothing started, so a task that started later would
// be one nobody is waiting for.
func parksAtCap(backend string) bool {
	return backend == injectBackend || busyNoticeBackend(backend)
}

// CapLineObserver is implemented by an adapter whose caller is a program
// that has to know what the session-cap line does with its turns: the inject
// door. Both are called on the conversation's inbox worker or the line's
// loop: record and return.
//
// TurnQueued says the turn now ending waits in the line (parked, or a
// later message held behind a parked turn), so it is not a turn that started
// nothing. ClaimRedelivery is asked before the line puts n messages back on
// the conversation's inbox, which ends in n turns no caller handed over; the
// adapter counts them as handed, so its own bookkeeping of which turn is
// whose stays whole, and reports false while a turn it handed over has not
// ended, in which case nothing is put back and the line tries again later.
type CapLineObserver interface {
	TurnQueued(conversation string)
	ClaimRedelivery(conversation string, n int) bool
}

// observeTurnQueued tells a CapLineObserver adapter that key's turn waits in
// the line.
func (g *Gateway) observeTurnQueued(key string) {
	if observer, ok := g.adapter.(CapLineObserver); ok {
		observer.TurnQueued(key)
	}
}

// claimRedelivery asks a CapLineObserver adapter whether n messages may go
// back on key's inbox now; any other adapter always says yes.
func (g *Gateway) claimRedelivery(key string, n int) bool {
	if observer, ok := g.adapter.(CapLineObserver); ok {
		return observer.ClaimRedelivery(key, n)
	}
	return true
}

func (g *Gateway) initCap() {
	g.capOnce.Do(func() {
		g.capCheck = make(chan struct{}, 1)
		g.capKick = make(chan struct{}, 1)
	})
}

// kickCapQueue nudges the line's loop: a slot may have freed. It never
// blocks, and nudges that arrive together are one.
func (g *Gateway) kickCapQueue() {
	g.initCap()
	select {
	case g.capKick <- struct{}{}:
	default:
	}
}

func (g *Gateway) capWaitMax() time.Duration {
	if d := time.Duration(g.capWaitOverride.Load()); d > 0 {
		return d
	}
	return sessionCapWaitMax
}

func (g *Gateway) capQueuePoll() time.Duration {
	if d := time.Duration(g.capPollOverride.Load()); d > 0 {
		return d
	}
	return sessionCapQueuePoll
}

// capWaiterLocked is key's waiter and its place in line, or nil, -1. The
// caller holds capMu.
func (g *Gateway) capWaiterLocked(key string) (*capWaiter, int) {
	for i, w := range g.capQueue {
		if w.key == key {
			return w, i
		}
	}
	return nil, -1
}

// dequeueLocked takes w out of the line and marks it closed. The caller
// holds capMu.
func (g *Gateway) dequeueLocked(w *capWaiter) {
	if i := slices.Index(g.capQueue, w); i >= 0 {
		g.capQueue = slices.Delete(g.capQueue, i, i+1)
	}
	w.closed.Store(true)
}

// parkLocked puts msg at the back of the line as a new waiter, or reports
// nil with the line full. The caller holds capMu.
func (g *Gateway) parkLocked(key string, msg InboundMessage) *capWaiter {
	if len(g.capQueue) >= sessionCapQueueMax {
		return nil
	}
	g.capTicket++
	msg.capRetry, msg.capReplay = 0, false
	w := &capWaiter{ticket: g.capTicket, key: key, parkedAt: time.Now(), msg: msg, shownAhead: -1}
	g.capQueue = append(g.capQueue, w)
	return w
}

// capDecision is refuseAtSessionCap's reading of the line for rec: its own
// waiter if it has one (the head's retry), and whether a fresh arrival that
// can park must queue behind waiters already there.
func (g *Gateway) capDecision(key string, parkable bool) (own *capWaiter, behind bool) {
	g.capMu.Lock()
	defer g.capMu.Unlock()
	own, _ = g.capWaiterLocked(key)
	return own, own == nil && parkable && len(g.capQueue) > 0
}

// capAdmit is the check's grant to a waiter: out of the line, its notice
// closed on the starting line after the check, and the next waiter nudged.
func (g *Gateway) capAdmit(w *capWaiter) func() {
	g.capMu.Lock()
	g.dequeueLocked(w)
	g.capMu.Unlock()
	g.log.Info("session cap: queued turn admitted", "conversation", w.key, "waited", time.Since(w.parkedAt).Round(time.Second))
	return func() {
		g.closeCapNotice(w, sessionQueuedStarting)
		g.kickCapQueue()
	}
}

// capKeepOrGiveUp is the check's answer to a waiter's retry that found no
// room: back in line at its place, or, past the wait bound, out of it with
// its notice closed (the refusal itself is the caller's). Reports whether
// it stays. The waiters behind the head meet the bound in expireCapWaiters.
func (g *Gateway) capKeepOrGiveUp(w *capWaiter, after *[]func()) bool {
	g.capMu.Lock()
	defer g.capMu.Unlock()
	if time.Since(w.parkedAt) < g.capWaitMax() {
		w.retrying = false
		return true
	}
	g.dequeueLocked(w)
	g.log.Warn("session cap: queued turn waited out its bound", "conversation", w.key, "waited", time.Since(w.parkedAt).Round(time.Second))
	*after = append(*after, func() {
		g.closeCapNotice(w, sessionQueuedGaveUp)
		g.kickCapQueue()
	})
	return false
}

// capPark parks msg for key at the back of the line, or reports false with
// the line full. Its notice goes out after the check.
func (g *Gateway) capPark(key string, msg InboundMessage, after *[]func()) bool {
	g.capMu.Lock()
	w := g.parkLocked(key, msg)
	n := len(g.capQueue)
	g.capMu.Unlock()
	if w == nil {
		return false
	}
	g.log.Info("session cap: turn queued", "conversation", key, "position", n)
	g.observeTurnQueued(key)
	*after = append(*after, func() {
		g.postCapNotice(w)
		g.kickCapQueue()
	})
	return true
}

// postCapNotice posts w's queued notice with its place in line as it is
// now, unless w has already left the line.
func (g *Gateway) postCapNotice(w *capWaiter) {
	w.editMu.Lock()
	defer w.editMu.Unlock()
	if w.closed.Load() {
		return
	}
	g.capMu.Lock()
	_, ahead := g.capWaiterLocked(w.key)
	g.capMu.Unlock()
	if ahead < 0 {
		return
	}
	id, err := g.adapter.Post(w.key, sessionQueuedNotice(ahead))
	if err != nil {
		g.log.Error("session cap: queued notice post failed", "conversation", w.key, "err", err)
		return
	}
	w.noticeID, w.shownAhead = id, ahead
}

// closeCapNotice edits a waiter's notice to its last line. It reports
// whether there was a notice to edit and the edit landed.
func (g *Gateway) closeCapNotice(w *capWaiter, line string) bool {
	w.editMu.Lock()
	defer w.editMu.Unlock()
	if w.noticeID == "" {
		return false
	}
	if err := g.adapter.Edit(w.key, w.noticeID, line); err != nil {
		g.log.Warn("session cap: queued notice edit failed", "conversation", w.key, "err", err)
		return false
	}
	return true
}

// refreshCapNotices edits each waiter's notice whose place in line has
// moved since it was last shown. Only the line's loop calls it, so the
// edits for one notice never cross each other.
func (g *Gateway) refreshCapNotices() {
	g.capMu.Lock()
	line := slices.Clone(g.capQueue)
	g.capMu.Unlock()
	for ahead, w := range line {
		w.editMu.Lock()
		if !w.closed.Load() && w.noticeID != "" && w.shownAhead != ahead {
			if err := g.adapter.Edit(w.key, w.noticeID, sessionQueuedNotice(ahead)); err != nil {
				g.log.Warn("session cap: queued notice edit failed", "conversation", w.key, "err", err)
			} else {
				w.shownAhead = ahead
			}
		}
		w.editMu.Unlock()
	}
}

// retryCapHead puts the head waiter's turn back on its conversation's inbox
// to try for a slot, then its follow-ups behind it in order, unless a retry
// is already in flight. A follow-up processed after a retry that found no
// room joins the waiter again (routeParked); after one that started the
// turn, it reaches the running task the way any later message does.
func (g *Gateway) retryCapHead() {
	g.capMu.Lock()
	if len(g.capQueue) == 0 || g.capQueue[0].retrying {
		g.capMu.Unlock()
		return
	}
	w := g.capQueue[0]
	msgs := append([]InboundMessage{w.msg}, w.followUps...)
	// A door with a turn of its own still running on the conversation says
	// not yet; the next nudge or poll tries again. Asked under capMu so
	// the count the door takes is the count put back.
	if !g.claimRedelivery(w.key, len(msgs)) {
		g.capMu.Unlock()
		return
	}
	w.retrying = true
	msgs[0].capRetry = w.ticket
	for i := range msgs {
		msgs[i].capReplay = true
	}
	w.followUps = nil
	g.capMu.Unlock()
	for _, m := range msgs {
		g.inbox.enqueue(w.key, m)
	}
}

// capRetryDone runs as the head's retried turn ends. A retry the cap check
// answered has already left retrying; one still retrying never reached the
// check: the turn went another way (the conversation's own pod could take
// it after all) or failed before the check and has said so in the log. It
// leaves the line either way, so the line never stalls behind it. At a
// stop it stays, for the stop's refusal post.
func (g *Gateway) capRetryDone(msg InboundMessage) {
	if g.runCtx.Err() != nil {
		return
	}
	g.capMu.Lock()
	w, _ := g.capWaiterLocked(msg.Conversation)
	if w == nil || w.ticket != msg.capRetry || !w.retrying {
		g.capMu.Unlock()
		return
	}
	g.dequeueLocked(w)
	g.capMu.Unlock()
	line := sessionQueuedLeft
	if rec, err := g.reg.Get(g.runCtx, msg.Conversation); err == nil && rec != nil && rec.ActiveTask != nil {
		line = sessionQueuedStarting
	}
	g.log.Warn("session cap: queued turn left the line without the cap check", "conversation", msg.Conversation)
	g.closeCapNotice(w, line)
	g.kickCapQueue()
}

// routeParked answers a message on a conversation that has a turn in the
// line, and reports whether it did. Everything a later message would do
// waits behind the parked turn, in order, the way the conversation's inbox
// already orders turns: the follow-up joins the waiter and is put back on
// the inbox behind the turn when it next tries, so it steers the task the
// turn starts, or starts its own after it. Two answers can't wait. A stop
// takes the turn out of the line and says so. A status ask is answered
// with the turn's place in line. A press of a stop control that names a
// task is about that task's line, not the waiter, and goes the way it
// always did (false), as does the head's own retry.
func (g *Gateway) routeParked(rec *SessionRecord, msg InboundMessage, backend string, stopping bool) bool {
	if g.spawner == nil {
		return false
	}
	g.capMu.Lock()
	w, ahead := g.capWaiterLocked(rec.Key)
	if w == nil || (msg.capRetry != 0 && msg.capRetry == w.ticket) || (stopping && msg.TaskID != "") {
		g.capMu.Unlock()
		return false
	}
	switch {
	case stopping:
		// The press gate holds here as for a running task (stopgate.go):
		// with nobody yet on record as asking, anyone in the thread may.
		if isStopPress(msg, backend) &&
			!mayStop(rec, backend, TaskRequester{Backend: backend, Subject: requesterSubject(g.ps, backend, msg.AuthorID)}) {
			g.capMu.Unlock()
			g.tellRefusedPresser(rec.Key, msg, backend)
			return true
		}
		g.dequeueLocked(w)
		g.capMu.Unlock()
		g.log.Info("session cap: queued turn stopped", "conversation", rec.Key)
		g.closeCapNotice(w, sessionQueuedStopped)
		g.post(rec.Key, sessionQueueStoppedNotice)
		g.kickCapQueue()
	case msg.Intent == "" && chattext.IsStatusQuery(msg.Text, false):
		g.capMu.Unlock()
		g.post(rec.Key, sessionQueuedNotice(ahead))
	case len(w.followUps) >= sessionCapFollowUpsMax:
		g.capMu.Unlock()
		g.post(rec.Key, fmt.Sprintf(sessionQueueFollowUpNotice, sessionCapFollowUpsMax))
	default:
		msg.capRetry, msg.capReplay = 0, false
		w.followUps = append(w.followUps, msg)
		g.capMu.Unlock()
		g.observeTurnQueued(rec.Key)
	}
	return true
}

// expireCapWaiters refuses every waiter behind the head whose wait has passed
// sessionCapWaitMax: out of the line, the cap's refusal posted and its notice
// closed as capKeepOrGiveUp and the check do for the head, so no waiter
// outlasts the bound because it never reached the front. The head is left to
// its own try, which meets the bound under the cap check (capKeepOrGiveUp);
// the head is the oldest waiter, so it is never kept past the bound by this.
// Its later messages wait on nothing now and are dropped, as at a stop; the
// refusal tells the person to send again. Only the line's loop calls it.
func (g *Gateway) expireCapWaiters(ctx context.Context) {
	bound := g.capWaitMax()
	var expired []*capWaiter
	g.capMu.Lock()
	if len(g.capQueue) > 1 {
		rest := slices.DeleteFunc(g.capQueue[1:], func(w *capWaiter) bool {
			if w.retrying || time.Since(w.parkedAt) < bound {
				return false
			}
			w.closed.Store(true)
			expired = append(expired, w)
			return true
		})
		g.capQueue = g.capQueue[:1+len(rest)]
	}
	g.capMu.Unlock()
	if len(expired) == 0 {
		return
	}
	// The refusal names the count as the check would; with the count
	// unreadable, the cap stands in, as the line only holds turns at it.
	live := g.cfg.MaxSessions
	if pods, err := g.spawner.SessionPods(ctx); err == nil {
		live = liveSessionCount(pods, g.capHoldSnapshot("", time.Now()))
	}
	for _, w := range expired {
		g.log.Warn("session cap: queued turn waited out its bound", "conversation", w.key, "waited", time.Since(w.parkedAt).Round(time.Second))
		g.post(w.key, sessionCapRefusal(live, g.cfg.MaxSessions))
		g.closeCapNotice(w, sessionQueuedGaveUp)
	}
}

// capQueueLoop moves the line: on a nudge from a path that may have freed a
// slot (kickCapQueue), or every sessionCapQueuePoll, it refuses the waiters
// behind the head that waited out their bound, brings every notice up to
// date and sends the head to try again. It ends with ctx; the turns
// still in line are refused by the stop (refuseParkedAtStop).
func (g *Gateway) capQueueLoop(ctx context.Context) {
	g.initCap()
	for {
		t := time.NewTimer(g.capQueuePoll())
		select {
		case <-ctx.Done():
			t.Stop()
			return
		case <-g.capKick:
			t.Stop()
		case <-t.C:
		}
		g.expireCapWaiters(ctx)
		g.refreshCapNotices()
		g.retryCapHead()
	}
}

// refuseParkedAtStop tells every turn still in line that it was not
// started, as the gateway stops: the line is memory, and the next leader
// starts with an empty one. Bounded by sessionCapStopNoticeWait; what is
// not posted by then is lost the way a turn in flight would be.
func (g *Gateway) refuseParkedAtStop() {
	g.capMu.Lock()
	line := g.capQueue
	g.capQueue = nil
	for _, w := range line {
		w.closed.Store(true)
	}
	g.capMu.Unlock()
	if len(line) == 0 {
		return
	}
	g.log.Warn("session cap: refusing the queued turns as the gateway stops", "queued", len(line))
	done := make(chan struct{})
	go func() {
		defer close(done)
		for _, w := range line {
			g.closeCapNotice(w, sessionQueuedRestart)
			g.post(w.key, sessionQueueRestartNotice)
		}
	}()
	select {
	case <-done:
	case <-time.After(sessionCapStopNoticeWait):
		g.log.Warn("session cap: stop notices to queued turns did not finish in time", "wait", sessionCapStopNoticeWait.String())
	}
}
