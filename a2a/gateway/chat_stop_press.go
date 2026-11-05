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
// by a typed stop is the same. With nothing running the press takes the
// typed stop's "nothing running" replies (routeTurn), since the line it was
// on is gone and a reply is owed somewhere.

// isChatStopPress reports a stop from Google Chat's Stop button rather than
// the word: the Google Chat adapter sets IntentCancel only on a kage.stop
// click.
func isChatStopPress(msg InboundMessage, backend string) bool {
	return backend == gchatBackend && msg.Intent == IntentCancel
}

// chatStopPress answers a Stop press on rec's active task: the quiet cancel,
// then the line closed on cancelSentNotice. A cancel that did not reach the
// bus is said on the line too, which keeps its control for another press.
func (g *Gateway) chatStopPress(ctx context.Context, rec *SessionRecord, authority Authority) {
	active := rec.ActiveTask
	if active.Detached {
		g.log.Info("chat stop press on a task already stopping", "conversation", rec.Key, "taskId", active.TaskID)
		return
	}
	if !g.cancelTaskVoiced(ctx, rec, authority, true) {
		if active.StatusMsgID != "" {
			g.editLine(rec.Key, active.StatusMsgID, withLineNote(cancelNotSentNotice, active.LineNote), true)
		}
		return
	}
	if active.StatusMsgID == "" {
		// No line to answer on (a backend that shows no line is not
		// Chat, but the placeholder post can fail): the typed stop's post.
		g.post(rec.Key, cancelSentNotice)
		return
	}
	if g.editLine(rec.Key, active.StatusMsgID, withLineNote(cancelSentNotice, active.LineNote), false) {
		active.LineClosed = true
	}
}
