package gateway

import (
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The one layout record a Google Chat message has. Every rewrite of a
// message this process makes (an answered question's rewrite, a rating
// tap's patch, a board row's edit, an edit on a notify key) reads the
// layout the message shows now as its base, under the message's rewrite
// lock (lockRewrite), changes only its own part, and writes what it sent
// back as the record: the text, and the buttons as drawn, rating buttons
// included. So a message with choices, links and acting rows keeps its row
// buttons through its answer, keeps its answered line through a tap, and
// keeps a tapped row's line through a rating. A message with no record
// (posted before a restart, forgotten after a board edit whose answer was
// lost, or created outside the layout path: a rated text answer, a Stop
// line) is rebuilt from the event's copy of it, text and buttons
// (gchatEventLayout), and is on record from then on.

// rememberLayout records layout as what message shows now.
func (a *GoogleChatAdapter) rememberLayout(message string, layout gchatChatLayout) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.uxLocked().rememberLayoutLocked(message, layout)
}

// rememberLayoutLocked is rememberLayout under the adapter's lock. The
// ring's eviction closes the question the evicted message asked
// (closeUnrecordedLocked).
func (ux *gchatUX) rememberLayoutLocked(message string, layout gchatChatLayout) {
	ux.layouts[message] = layout
	remember(&ux.layoutsOrder, message, func(k string) {
		delete(ux.layouts, k)
		ux.closeUnrecordedLocked(k)
	})
}

// forgetLayout drops message's record: an edit whose answer did not come
// back may have landed, so the next rewrite builds from the event's copy.
func (a *GoogleChatAdapter) forgetLayout(message string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	delete(ux.layouts, message)
	forget(&ux.layoutsOrder, message)
	ux.closeUnrecordedLocked(message)
}

// closeUnrecordedLocked closes the question a message with no record asked:
// a typed reply has no copy of the message to rewrite, so the question goes
// with the record, as one evicted from its own ring does, and the message
// is as one from before a restart: its buttons still answer, from the
// click's copy, and a typed reply in its thread is a turn of its own.
func (ux *gchatUX) closeUnrecordedLocked(message string) {
	ux.dropQuestionLocked(message)
	forget(&ux.questionsOrder, message)
}

// layoutOf is the layout message shows now, when the adapter has a record
// of it, and whether the message was laid out again without rows that act
// while its record drew such rows (boardMoved).
func (a *GoogleChatAdapter) layoutOf(message string) (layout gchatChatLayout, remembered, moved bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	layout, remembered = ux.layouts[message]
	return layout, remembered, ux.boardMoved[message]
}

// trackLayout records layout as what message shows after a create or an
// edit laid m out on it (m nil for a text edit), and keeps the board state
// in step: the rows are new either way, so every claim a tap holds on the
// message goes (releaseBoardTapsLocked); a message whose record drew a
// row's Retry or Drop and now has no row that acts is marked, so a tap
// still in flight on it draws nothing on it (gchat_board.go); one laid out
// with such rows again is unmarked.
func (a *GoogleChatAdapter) trackLayout(message string, m *chatmsg.Message, layout gchatChatLayout) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	ux.releaseBoardTapsLocked(message)
	switch {
	case m != nil && slackChatRowsAct(m.Rows):
		delete(ux.boardMoved, message)
		forget(&ux.boardMovedOrder, message)
	case gchatLayoutActs(ux.layouts[message]):
		ux.boardMoved[message] = true
		remember(&ux.boardMovedOrder, message, func(k string) { delete(ux.boardMoved, k) })
	}
	ux.rememberLayoutLocked(message, layout)
}

// gchatLayoutActs reports whether layout draws a board row's Retry or Drop.
func gchatLayoutActs(layout gchatChatLayout) bool {
	for _, w := range layout.Widgets {
		list, _ := w[gchatButtonListKey].(map[string]any)
		buttons, _ := list[gchatButtonsKey].([]map[string]any)
		for _, b := range buttons {
			if gchatButtonFunction(b) == gchatBoardFunction {
				return true
			}
		}
	}
	return false
}
