package gateway

import (
	"context"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Google Chat's Stop button (gchat_stop.go) answers on the task's own line,
// as Slack's answers with one reply of its own (slack_stop.go). Both take a
// press from a requester of the thread alone (stopgate.go); a press that
// reaches here passed that gate. A press sends the cancel a typed stop
// sends, quietly (no "cancel sent" post, and no "canceled" notice from the
// relay, TaskRef.QuietCancel), and edits the
// line to cancelSentNotice with the Stop control gone, so the line a person
// pressed is the one that answers. The relay leaves the line so
// (ActiveTask.LineClosed) until its terminal edit makes it "canceled". A
// stop typed on Google Chat or on Teams with a line to answer on takes the
// same answer (chatTypedStop): the word and the button read alike, one line and no
// posts, where a DM's thread-less key would otherwise put the line, the
// "cancel sent" post and the relay's "canceled" each in a message of its
// own. A second typed stop while the cancel is pending moves "cancel
// already sent" onto the line. Every other backend keeps the posts: the
// console and Discord draw no button, and the posted replies are their
// contract; Slack's typed stop keeps its rule beside the native Stop. A
// second press while the cancel is pending has no button to land on; one
// that arrives anyway, or one on a task a typed stop already detached,
// closes the line if the first closing edit did not land, and otherwise
// changes nothing. The button names its task, so a press on a task that is
// not the running one (a line whose closing edit failed, or one from before
// a restart) cancels nothing: a press is never a cancel of a task the
// presser did not see. It is answered on the pressed line when the record
// does not know the task (staleStopNotice), or re-renders the canceled line
// for a task the record cancelled; a task the record knows ended otherwise
// keeps the terminal line its end wrote, since the heal or the relay may
// have written it a moment before the press.
const staleStopNotice = "ℹ️ this task has ended"

// refusedPressLineMark opens the line a refused press is answered on
// (tellRefusedOnLine): a hand, not a state, since the task's state is
// whatever it was.
const refusedPressLineMark = "✋ "

// lineStopBackends are the backends whose Stop button is the adapter's own
// drawing on the task's line, and so is answered on that line: Google Chat,
// and Teams, whose button is on the line's card (teams_press.go).
var lineStopBackends = map[string]bool{gchatBackend: true, teamsBackend: true}

// isChatStopPress reports a stop from a Stop button drawn on the task's
// line (lineStopBackends) rather than the word, as stopGatedBackends
// describes that backend's press (stopgate.go).
func isChatStopPress(msg InboundMessage, backend string) bool {
	return lineStopBackends[backend] && isStopPress(msg, backend)
}

// chatStopPress answers a Stop press on rec's active task: the quiet cancel,
// then the line closed on cancelSentNotice. A cancel that did not reach the
// bus is said on the line too, which keeps its control for another press.
// With no line to answer on (the placeholder post can fail) the press speaks
// as the typed stop does: its posts, and the relay's "canceled". The pressed
// message is closed too when it is not the task's line: a delegating
// parent's line names the parent, whose press is the running child's cancel
// (routeTurn's root rewrite), and the line a person pressed must answer.
func (g *Gateway) chatStopPress(ctx context.Context, rec *SessionRecord, msg InboundMessage, authority Authority) {
	active := rec.ActiveTask
	pressed := gchatClickMessage(msg.MessageID)
	if active.Detached {
		// Stopping already: the closing edit is retried if it never
		// landed, and the line stays closed.
		if active.StatusMsgID != "" && !active.LineClosed && g.closeLineOnPress(rec, active, cancelSentNotice) {
			active.LineClosed = true
		}
		g.closePressedLine(rec, active, pressed)
		g.log.Info("chat stop press on a task already stopping", "conversation", rec.Key, "taskId", active.TaskID)
		return
	}
	if active.StatusMsgID == "" {
		g.cancelTaskVoiced(ctx, rec, authority, false)
		return
	}
	if g.stopOnLine(ctx, rec, authority) {
		g.closePressedLine(rec, active, pressed)
	}
}

// stopOnLine sends the quiet cancel for rec's active task, which has a
// line, and closes the line on cancelSentNotice; it reports whether the
// cancel reached the bus. A cancel that did not is said on the line, which
// keeps its control for another press or stop.
func (g *Gateway) stopOnLine(ctx context.Context, rec *SessionRecord, authority Authority) bool {
	active := rec.ActiveTask
	if !g.cancelTaskVoiced(ctx, rec, authority, true) {
		g.editLine(rec.Key, active.StatusMsgID, withLineNote(cancelNotSentNotice, active.LineNote), active.TaskID)
		return false
	}
	if g.closeLineOnPress(rec, active, cancelSentNotice) {
		active.LineClosed = true
		return true
	}
	// The line did not take the edit (a refused patch, an outage): the
	// cancel is sent and quiet, so the typed stop's post says so instead,
	// and the next stop retries the close.
	g.post(rec.Key, cancelSentNotice)
	return true
}

// isChatTypedStop reports a stop typed on a backend whose Stop button is on
// the task's line (lineStopBackends): the word, from the backend's own
// ingress, naming no task (a press names one, and so does a program's
// cancel) and not a click. It is answered on the line as a press is, so the
// line and its button never outlive a stop the word already sent.
func isChatTypedStop(msg InboundMessage, backend string) bool {
	return lineStopBackends[backend] && msg.TaskID == "" && !msg.Click
}

// chatTypedStop answers a stop typed on Google Chat on rec's active task,
// which has a line, as a press is answered (stopOnLine). A second stop
// while the cancel is pending closes the line on cancelAlreadySentNotice,
// which also lands the close a first edit missed. No message of the
// typist's is touched: the typed message is not a line.
func (g *Gateway) chatTypedStop(ctx context.Context, rec *SessionRecord, authority Authority) {
	active := rec.ActiveTask
	if active.Detached {
		if g.closeLineOnPress(rec, active, cancelAlreadySentNotice) {
			active.LineClosed = true
		}
		g.log.Info("chat typed stop on a task already stopping", "conversation", rec.Key, "taskId", active.TaskID)
		return
	}
	g.stopOnLine(ctx, rec, authority)
}

// tellRefusedOnLine answers a refused Stop press on Teams, which has no
// message only the presser sees (tellRefusedPresser's way on Google Chat):
// the running task's line reads refusedPressNotice, its Stop button kept,
// until the relay next updates the line. Without it the button a
// non-requester pressed looks slow rather than refused. It is said on the
// running task's own line whichever line was pressed (a delegating parent's
// line stops the running child), since that is the line the relay keeps
// writing; and never in a direct message, where the only other member is
// the one who asked. The notice names nobody.
//
// The relay skips an update that renders the line it last wrote
// (relayState.lastLine), so the refusal clears that memory: the next
// update writes the line again even when its text has not changed, which
// is every update under the default display mode. A queued task whose line
// holds the busy notice is left as it is: the relay keeps that notice until
// the task starts (relayState.busyShown), so a refusal there would stay as
// long.
func (g *Gateway) tellRefusedOnLine(rec *SessionRecord, msg InboundMessage, backend string) {
	active := rec.ActiveTask
	if backend != teamsBackend || msg.Kind == sessionKindDM || active == nil || active.StatusMsgID == "" {
		return
	}
	g.mu.Lock()
	rs := g.relays[active.TaskID]
	g.mu.Unlock()
	if rs != nil && rs.busyShown && lineStillSubmitted(rs) {
		return
	}
	if !g.editLine(rec.Key, active.StatusMsgID, withLineNote(refusedPressLineMark+refusedPressNotice, active.LineNote), active.TaskID) {
		return
	}
	if rs != nil {
		rs.lastLine = ""
	}
	g.log.Info("refused stop press: said on the line", "backend", backend, "conversation", rec.Key)
}

// closeLineOnPress edits the active task's line to text with no Stop
// control, and reports whether the edit landed.
func (g *Gateway) closeLineOnPress(rec *SessionRecord, active *ActiveTask, text string) bool {
	return g.editLine(rec.Key, active.StatusMsgID, withLineNote(text, active.LineNote), "")
}

// closePressedLine edits the message a press landed on to cancelSentNotice,
// control gone, when it is not the active task's own line.
func (g *Gateway) closePressedLine(rec *SessionRecord, active *ActiveTask, pressed string) {
	if pressed == "" || pressed == active.StatusMsgID {
		return
	}
	g.editLine(rec.Key, pressed, cancelSentNotice, "")
}

// chatStopStale answers a press on a task that is not rec's running task,
// and cancels nothing. A task the record does not know is answered with
// staleStopNotice on the pressed line; one the record cancelled gets the
// canceled line, which the superseding turn may have failed to write; any
// other task the record knows ended keeps the line its end wrote.
func (g *Gateway) chatStopStale(rec *SessionRecord, msg InboundMessage) {
	pressed := gchatClickMessage(msg.MessageID)
	ref, known := rec.TaskRefFor(msg.TaskID)
	switch {
	case !known:
		g.log.Info("chat stop press on a task this conversation does not know", "conversation", rec.Key, "taskId", msg.TaskID)
		g.editLine(rec.Key, pressed, staleStopNotice, "")
	case ref.Canceled:
		g.log.Info("chat stop press on a task already cancelled", "conversation", rec.Key, "taskId", msg.TaskID)
		g.editLine(rec.Key, pressed, terminalLine(lib.StateCanceled, ""), "")
	default:
		g.log.Info("chat stop press on a task that has ended", "conversation", rec.Key, "taskId", msg.TaskID)
	}
}
