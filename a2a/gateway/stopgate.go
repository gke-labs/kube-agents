package gateway

// Who may press a Slack thread's Stop button (spec-chatops-gateway.md, "The Slack
// adapter"): only the people who asked for work in it. Anyone else's press cancels
// nothing and posts nothing; the gateway logs it. A typed `stop`, and every
// other backend, keep the rule they had: anyone the gateway answers in the
// conversation.

// stopGatedBackend is the backend whose stops the requester gate holds.
const stopGatedBackend = slackBackend

// mayStop reports whether author may press Stop on rec's thread on backend. Off the
// gated backend, everyone the gateway answers may. On it, author must be a
// requester the record holds: a recorded task's requester or one of its steer
// authors, or one of the session's authors. The record keeps those copies for
// the delegation check, and bounds them for that check: a requester cleared
// past AskTTL, a steer or session author past its cap, tasks past the history
// cap. When the record cannot tell who asked (cannotTellWhoAsked), anyone the
// gateway answers may press Stop: refusing would lock out the very requester
// whose copy was dropped (the maintainer's decision, 2026-10-22).
func mayStop(rec *SessionRecord, backend string, author TaskRequester) bool {
	if backend != stopGatedBackend || cannotTellWhoAsked(rec) {
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
