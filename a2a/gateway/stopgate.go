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
// authors, or one of the session's authors. The record prunes old tasks and
// clears a requester past AskTTL, so an early requester of a long thread can
// age out of it. A conversation that has run nothing has nothing to protect,
// so its stop passes and gets the nothing-is-running answer.
func mayStop(rec *SessionRecord, backend string, author TaskRequester) bool {
	if backend != stopGatedBackend || (len(rec.Tasks) == 0 && len(rec.SessionAuthors) == 0) {
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
