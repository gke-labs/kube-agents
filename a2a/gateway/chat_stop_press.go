package gateway

import "context"

// Google Chat's Stop button (gchat_stop.go) answers on the task's own line,
// as Slack's answers with one reply of its own (slack_stop.go): a press
// sends the cancel a typed stop sends, quietly (no "cancel sent" post, and
// no "canceled" notice from the relay, TaskRef.QuietCancel), and edits the
// line to cancelSentNotice with the Stop control gone, so the line a person
// pressed is the one that answers. The relay leaves the line so
// (ActiveTask.LineClosed) until its terminal edit makes it "canceled". A
// second press while the cancel is pending has no button to land on; one
// that arrives anyway changes nothing. A press on a task already detached
// by a typed stop is the same. The button names its task, so a press on a
// task that is not the running one (a line whose closing edit failed, or
// one from before a restart) is answered on that stale line, with
// staleStopNotice, and cancels nothing: a press is never a cancel of a task
// the presser did not see.
const staleStopNotice = "ℹ️ this task has ended"

// isChatStopPress reports a stop from Google Chat's Stop button rather than
// the word: the Google Chat adapter sets IntentCancel only on a kage.stop
// click.
func isChatStopPress(msg InboundMessage, backend string) bool {
	return backend == gchatBackend && msg.Intent == IntentCancel
}

// chatStopPress answers a Stop press on rec's active task: the quiet cancel,
// then the line closed on cancelSentNotice. A cancel that did not reach the
// bus is said on the line too, which keeps its control for another press.
// With no line to answer on (the placeholder post can fail) the press speaks
// as the typed stop does: its posts, and the relay's "canceled".
func (g *Gateway) chatStopPress(ctx context.Context, rec *SessionRecord, authority Authority) {
	active := rec.ActiveTask
	if active.Detached {
		g.log.Info("chat stop press on a task already stopping", "conversation", rec.Key, "taskId", active.TaskID)
		return
	}
	quiet := active.StatusMsgID != ""
	if !g.cancelTaskVoiced(ctx, rec, authority, quiet) {
		if quiet {
			g.editLine(rec.Key, active.StatusMsgID, withLineNote(cancelNotSentNotice, active.LineNote), active.TaskID)
		}
		return
	}
	if !quiet {
		return
	}
	if g.editLine(rec.Key, active.StatusMsgID, withLineNote(cancelSentNotice, active.LineNote), "") {
		active.LineClosed = true
	}
}

// chatStopStale answers a press on a task that is not rec's running task:
// the line the button was on is edited to staleStopNotice, control gone,
// and nothing is cancelled. The click id names the message it was on.
func (g *Gateway) chatStopStale(rec *SessionRecord, msg InboundMessage) {
	g.log.Info("chat stop press on a task that is not running", "conversation", rec.Key, "taskId", msg.TaskID)
	g.editLine(rec.Key, gchatClickMessage(msg.MessageID), staleStopNotice, "")
}
