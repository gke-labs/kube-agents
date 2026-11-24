package gateway

import (
	"context"
	"fmt"
	"time"
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
const (
	waitingNoticeNone = "⏳ **waiting** — the %s agent's workers are all busy; it starts as soon as one frees up"
	waitingNoticeOne  = "⏳ **waiting** — the %s agent's workers are all busy; 1 other request sent through me is waiting too"
	waitingNoticeMany = "⏳ **waiting** — the %s agent's workers are all busy; %d other requests sent through me are waiting too"

	// stillWaitingNotice closes a waiting line when the first-event grace
	// runs out (noticeNoFirstEvent): one plain line, in place of the ⏳, that
	// says the request didn't start and what to do instead, which is what the
	// grace notice says for a line with no waiting notice.
	stillWaitingNotice = "⚠️ **didn't start** — the %s agent's workers stayed busy for %s; send it again to start a new request (unless this one starts first)"
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

// childStillWaiting reports whether taskID's line is still at submitted, so
// no first event has been relayed: the relay's own render state, this
// gateway's (lineStillSubmitted). A task this process holds no relay state
// for isn't counted or shown: nothing here can vouch it hasn't started.
func (g *Gateway) childStillWaiting(taskID string) (*relayState, bool) {
	g.mu.Lock()
	rs := g.relays[taskID]
	g.mu.Unlock()
	return rs, lineStillSubmitted(rs)
}

// childWaitingOthers counts this gateway's other delegated children for the
// same profile and tier that haven't started, leaving out exclude. A count
// that fails returns the error, and no notice is shown or changed.
func (g *Gateway) childWaitingOthers(ctx context.Context, addressee, tier, exclude string) (int, error) {
	ctx, cancel := context.WithTimeout(ctx, busyCountTimeout)
	defer cancel()
	n := 0
	err := g.reg.eachSessionRecord(ctx, func(rec *SessionRecord) {
		active := rec.ActiveTask
		if active == nil || active.Detached || active.TaskID == exclude {
			return
		}
		ref, ok := rec.TaskRefFor(active.TaskID)
		if !ok || ref.Role != taskRoleChild || ref.Addressee != addressee || ref.Tier != tier {
			return
		}
		if _, waiting := g.childStillWaiting(active.TaskID); waiting {
			n++
		}
	})
	return n, err
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
	if _, waiting := g.childStillWaiting(active.TaskID); !waiting {
		return
	}
	others, err := g.childWaitingOthers(ctx, ref.Addressee, ref.Tier, active.TaskID)
	if err != nil {
		g.log.Warn("waiting notice: count failed; the line is left as it is",
			"conversation", rec.Key, "taskId", active.TaskID, "err", err)
		return
	}
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
	rs, waiting := g.childStillWaiting(cur.TaskID)
	if !waiting {
		return
	}
	line := withLineNote(waitingNotice(ref.Addressee, others), cur.LineNote)
	if !g.editLine(fresh.Key, cur.StatusMsgID, line, cur.TaskID) {
		return
	}
	rs.lastLine = line
	rs.busyShown = true
	if cur.WaitingNoticeAt.IsZero() {
		cur.WaitingNoticeAt = time.Now().UTC()
	}
	cur.WaitingNoticeOthers = others
	if err := g.reg.Put(ctx, fresh); err != nil {
		g.log.Error("waiting notice: record write failed", "conversation", fresh.Key, "err", err)
		return
	}
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
	rs, waiting := g.childStillWaiting(active.TaskID)
	if !waiting {
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
