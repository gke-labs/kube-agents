package gateway

import (
	"context"
	"fmt"
	"time"

	"github.com/nats-io/nats.go/jetstream"
)

// The waiting notice (#7015): a delegated child that hasn't started has its
// own status line edited, in place, to say it's waiting for a worker, rather
// than the line sitting at "submitted" until the first-event grace runs out.
// It matches the busy notice's shape (busy.go) and the session-cap notice,
// "⏳ **word** — reason", but says "waiting", not "queued": the dispatcher's
// admission isn't first-in-first-out, a slow start reads the same, and the
// count is this gateway's children only, which the notice says. No time
// promise: the child's limit is the first-event grace, not a cap wait.
//
// No plumbing (.agents/rules/chat_ux.md): the profile is named as the agent
// the person asked for, not as an id, and "sent through me" says whose
// requests are counted without naming the gateway.
//
// It says only what the gateway knows: the request hasn't started. It
// doesn't claim why (busy workers, a cold start and a dispatcher that's down
// all read the same from here) and promises no start.
const (
	waitingNoticeNone = "⏳ **waiting** — the %s agent hasn't started on this yet"
	waitingNoticeOne  = "⏳ **waiting** — the %s agent hasn't started on this yet; 1 other request sent through me is waiting for it too"
	waitingNoticeMany = "⏳ **waiting** — the %s agent hasn't started on this yet; %d other requests sent through me are waiting for it too"

	// stillWaitingNotice closes a waiting line when the first-event grace
	// runs out (noticeNoFirstEvent): one plain line, in place of the ⏳, that
	// says the request didn't start and what to do instead, which is what the
	// grace notice says for a line with no waiting notice.
	stillWaitingNotice = "⚠️ **didn't start** — the %s agent didn't start on this in %s; ask again to start a new request (unless this one starts first)"
)

// graceWords renders the first-event grace for the closing line: whole
// minutes as words ("10 minutes"), anything else as Go prints it.
func graceWords(d time.Duration) string {
	switch {
	case d == time.Minute:
		return "1 minute"
	case d > 0 && d%time.Minute == 0:
		return fmt.Sprintf("%d minutes", int(d/time.Minute))
	default:
		return d.Round(time.Second).String()
	}
}

// waitingNoticeAfter is how long a delegated child may go without a first
// event before its line says it's waiting. Long enough that a child a free
// worker picks up never flashes the notice; the reap scan's interval
// (reapInterval) is the notice's real latency on top of it.
const waitingNoticeAfter = 30 * time.Second

// waitingNotice renders the notice for a child of profile with others
// waiting beside it.
func waitingNotice(profile string, others int) string {
	switch {
	case others <= 0:
		return fmt.Sprintf(waitingNoticeNone, profile)
	case others == 1:
		return fmt.Sprintf(waitingNoticeOne, profile)
	default:
		return fmt.Sprintf(waitingNoticeMany, profile, others)
	}
}

// waitingChild is rec's active task when it is a delegated profile child
// whose line may carry the waiting notice: its ref (for the profile and
// tier), and true. Only a child (role child, with a tier: a profile task),
// with a status line to edit, whose parent turn came in on a chat backend
// (busyNoticeBackend: the inject and A2A doors read the line as data), and
// that hasn't been told the grace ran out.
func waitingChild(rec *SessionRecord) (TaskRef, bool) {
	active := rec.ActiveTask
	if active == nil || active.Detached || active.LineClosed || active.StatusMsgID == "" ||
		!active.NoFirstEventNoticeAt.IsZero() {
		return TaskRef{}, false
	}
	ref, ok := rec.TaskRefFor(active.TaskID)
	if !ok || ref.Role != taskRoleChild || ref.Tier == "" || ref.ParentTaskID == "" {
		return TaskRef{}, false
	}
	parent, ok := rec.TaskRefFor(ref.ParentTaskID)
	if !ok || parent.Requester == nil || !busyNoticeBackend(parent.Requester.Backend) {
		return TaskRef{}, false
	}
	return ref, true
}

// lineStillWaiting reports whether taskID's line is still at submitted, by
// the relay's render state (lineStillSubmitted). It reads relayState.state,
// which the relay writes under the conversation's session lock, so callers
// hold that lock.
func (g *Gateway) lineStillWaiting(taskID string) (*relayState, bool) {
	g.mu.Lock()
	rs := g.relays[taskID]
	g.mu.Unlock()
	return rs, lineStillSubmitted(rs)
}

// childNotStarted reports whether a child's event stream is still empty,
// read with direct gets and no consumer (probeTask), so it holds across a
// restart and reads no other task's relay state.
func (g *Gateway) childNotStarted(ctx context.Context, rec *SessionRecord, stream jetstream.Stream, ref TaskRef) bool {
	probe, err := g.probeTask(ctx, stream, ref.Addressee, ref.ID, g.profileRead(rec, ref.ID, ref.Addressee))
	return err == nil && probe.empty
}

// waitingChildren counts this gateway's delegated children for addressee at
// tier that haven't started: every child task on a session record, a single
// child or a batch's, not cancelled or ended, whose stream is still empty.
// One count per (addressee, tier) per reap pass (waitingPass), however many
// waiting children ask. A count that fails returns the error.
func (g *Gateway) waitingChildren(ctx context.Context, addressee, tier string) (int, error) {
	key := addressee + "\x00" + tier
	g.mu.Lock()
	if n, ok := g.waitingPass[key]; ok {
		g.mu.Unlock()
		return n, nil
	}
	g.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, busyCountTimeout)
	defer cancel()
	stream, err := g.tasksStream(ctx)
	if err != nil {
		return 0, err
	}
	n := 0
	err = g.reg.eachSessionRecord(ctx, func(rec *SessionRecord) {
		for _, ref := range rec.Tasks {
			if ref.Role != taskRoleChild || ref.Addressee != addressee || ref.Tier != tier ||
				ref.Canceled || ref.TerminalObserved || ref.BatchEnd != nil {
				continue
			}
			if g.childNotStarted(ctx, rec, stream, ref) {
				n++
			}
		}
	})
	if err != nil {
		return 0, err
	}
	g.mu.Lock()
	if g.waitingPass != nil {
		g.waitingPass[key] = n
	}
	g.mu.Unlock()
	return n, nil
}

// noticeChildWaiting shows, or refreshes, the waiting notice on rec's active
// child (#7015). It runs in the reap scan beside noticeNoFirstEvent. It
// writes nothing to the task's stream, so the first-event grace, the heal,
// the reap notice and the busy count read the task exactly as before.
//
// The edit is made under the conversation's session lock, the lock every
// relay batch takes before it renders, and only while the line is still at
// submitted, so it never lands over a working or terminal edit. Once it
// lands, relayState.busyShown keeps the relay's own submitted render from
// replacing it, and the child's first working or terminal edit replaces it
// as it replaces the placeholder. The count is refreshed on each pass and
// the line edited only when it changes.
func (g *Gateway) noticeChildWaiting(ctx context.Context, rec *SessionRecord) {
	ref, ok := waitingChild(rec)
	if !ok {
		return
	}
	active := rec.ActiveTask
	if active.SubmittedAt.IsZero() || time.Since(active.SubmittedAt) < waitingNoticeAfter {
		return
	}
	stream, err := g.tasksStream(ctx)
	if err != nil || !g.childNotStarted(ctx, rec, stream, ref) {
		return
	}
	waiting, err := g.waitingChildren(ctx, ref.Addressee, ref.Tier)
	if err != nil {
		g.log.Warn("waiting notice: count failed; the line is left as it is",
			"conversation", rec.Key, "taskId", active.TaskID, "err", err)
		return
	}
	others := max(waiting-1, 0) // this child is one of them
	l := g.lockSession(rec.Key)
	l.Lock()
	defer l.Unlock()
	fresh, err := g.reg.Get(ctx, rec.Key)
	if err != nil || fresh == nil || fresh.ActiveTask == nil || fresh.ActiveTask.TaskID != active.TaskID {
		return
	}
	if _, ok := waitingChild(fresh); !ok {
		return
	}
	cur := fresh.ActiveTask
	if !cur.WaitingNoticeAt.IsZero() && cur.WaitingNoticeOthers == others {
		return
	}
	rs, still := g.lineStillWaiting(cur.TaskID)
	if !still {
		return
	}
	// The record first: a line edited with no record of it would be left
	// with its ⏳ at the grace (closeWaitingLine reads the record). A record
	// written with an edit that then fails is closed at the grace as usual.
	if cur.WaitingNoticeAt.IsZero() {
		cur.WaitingNoticeAt = time.Now().UTC()
	}
	cur.WaitingNoticeOthers = others
	if err := g.reg.Put(ctx, fresh); err != nil {
		g.log.Error("waiting notice: record write failed", "conversation", fresh.Key, "err", err)
		return
	}
	line := withLineNote(waitingNotice(ref.Addressee, others), cur.LineNote)
	if !g.editLine(fresh.Key, cur.StatusMsgID, line, cur.TaskID) {
		return
	}
	rs.lastLine = line
	rs.busyShown = true
	g.log.Info("waiting notice shown on a delegated child's line",
		"conversation", fresh.Key, "taskId", cur.TaskID, "profile", ref.Addressee, "tier", ref.Tier, "others", others)
}

// closeWaitingLine closes a waiting child's line when the first-event grace
// has run out, in place of the grace notice's separate post: the ⏳ goes, and
// the line says the request didn't start and what the next message does. It
// reports whether it did; false (no waiting notice shown, the line moved on,
// or the edit failed) leaves the caller to post the grace notice as before.
// The caller holds the session lock.
func (g *Gateway) closeWaitingLine(rec *SessionRecord) bool {
	active := rec.ActiveTask
	if active == nil || active.WaitingNoticeAt.IsZero() || active.StatusMsgID == "" {
		return false
	}
	ref, ok := rec.TaskRefFor(active.TaskID)
	if !ok {
		return false
	}
	rs, still := g.lineStillWaiting(active.TaskID)
	if !still {
		return false
	}
	line := withLineNote(fmt.Sprintf(stillWaitingNotice, ref.Addressee, graceWords(g.cfg.FirstEventGrace)), active.LineNote)
	if !g.editLine(rec.Key, active.StatusMsgID, line, active.TaskID) {
		return false
	}
	rs.lastLine = line
	rs.busyShown = true
	return true
}
