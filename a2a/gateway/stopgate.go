package gateway

// Who may press a thread's Stop button (spec-chatops-gateway.md, "Who may press
// Stop"): only the people who asked for work in it. The rule is the same on
// each backend that has a button: Slack's native Stop (slack_stop.go) and the
// Stop button the Google Chat adapter draws under a task's line
// (gchat_stop.go). Anyone else's press cancels nothing, and the gateway logs
// it. What the refused presser sees differs by backend (added 11/11). On
// Slack nothing is posted: the Working… line and its button are Slack's own
// and settle on their own. On Google Chat the button is the adapter's
// drawing and stays until the gateway edits the line, so a silent refusal
// is a frozen control the presser cannot tell from a slow one, and the
// gateway tells the presser alone, in the thread, through a post only they
// see (PrivatePoster, Chat's privateMessageViewer): refusedPressNotice. It
// notifies nobody else. It posts only when the click named the presser as a
// user (InboundMessage.Viewer), since a post with no viewer would be
// public, and never in a direct message, where there is no other member to
// refuse (tellsRefusedPresser). A member the allowlist drops never reaches
// the gate and gets nothing, as before. A typed `stop`, on every backend,
// keeps the rule it had: anyone the gateway answers in the conversation.
// The gate reads the presser of a live task's button: a press on a task that
// is not the running one cancels nothing whoever presses it, so it is
// answered before the gate is read (chatStopStale), and anyone may heal a
// dead line.

// refusedPressNotice is the one line a refused Stop press tells its presser,
// where tellsRefusedPresser says so: which of "not allowed" and "slow" the
// frozen button is, and the way that still works, since a typed stop is not
// gated. It names the button, not stopping, so the two sentences agree; it
// says "here", not "this thread", since a group chat has no thread; and it
// names the typed word rather than offering a button, since a button is what
// the gate refuses.
const refusedPressNotice = "Only the person who asked can press Stop here. If it must stop, type stop."

// stopGatedBackends are the backends with a Stop button, and what its press
// looks like on the wire; the one place that says which backends the gate
// holds. Slack's adapter sets IntentCancel only on agent_session_stopped, and
// Google Chat's only on a kage.stop click, which it marks as one.
var stopGatedBackends = map[string]func(InboundMessage) bool{
	slackBackend: func(msg InboundMessage) bool { return msg.Intent == IntentCancel },
	gchatBackend: func(msg InboundMessage) bool { return msg.Intent == IntentCancel && msg.Click },
}

// isStopPress reports a stop from a backend's Stop button rather than the word:
// backend is one the gate holds, and msg is what its press looks like.
func isStopPress(msg InboundMessage, backend string) bool {
	press, gated := stopGatedBackends[backend]
	return gated && press(msg)
}

// tellsRefusedPresser reports whether a press of msg on backend that mayStop
// refuses tells the presser alone (refusedPressNotice): on Google Chat, when
// the click named the presser as a user (msg.Viewer), outside a direct
// message. Slack's refused press stays silent, as does a click Chat named
// nobody for, and one in a direct message.
func tellsRefusedPresser(msg InboundMessage, backend string) bool {
	return backend == gchatBackend && msg.Viewer != "" && msg.Kind != sessionKindDM
}

// mayStop reports whether author may press Stop on rec's thread on backend. Off
// the gated backends, everyone the gateway answers may. On one, author must be
// a requester the record holds: a recorded task's requester or one of its steer
// authors, or one of the session's authors. The record keeps those copies for
// the delegation check, and bounds them for that check: a requester cleared
// past AskTTL, a steer or session author past its cap, tasks past the history
// cap. When the record cannot tell who asked (cannotTellWhoAsked), anyone the
// gateway answers may press Stop: refusing would lock out the very requester
// whose copy was dropped (the maintainer's decision, 2026-10-22).
func mayStop(rec *SessionRecord, backend string, author TaskRequester) bool {
	if stopGatedBackends[backend] == nil || cannotTellWhoAsked(rec) {
		return true
	}
	if author.Subject == "" {
		return false
	}
	for _, ref := range rec.Tasks {
		if ref.Requester != nil && *ref.Requester == author {
			return true
		}
		for _, s := range ref.SteerAuthors {
			if s == author {
				return true
			}
		}
	}
	for _, a := range rec.SessionAuthors {
		if a == author {
			return true
		}
	}
	return false
}

// cannotTellWhoAsked reports whether rec may have dropped someone who asked
// for work in the thread, or never held anyone: a conversation that ran
// nothing, a task whose requester was cleared, a steer author or session
// author past its cap, or a history at its cap, whose oldest tasks are gone.
func cannotTellWhoAsked(rec *SessionRecord) bool {
	if rec.SessionAuthorsUnknown || len(rec.Tasks) >= taskHistoryCap {
		return true
	}
	held := len(rec.SessionAuthors) > 0
	for _, ref := range rec.Tasks {
		if ref.Requester == nil || ref.SteerAuthorsOverflow {
			return true
		}
		held = true
	}
	return !held
}
