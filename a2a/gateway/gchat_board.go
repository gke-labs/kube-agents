package gateway

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// Retry and Drop on the board-health card, on Google Chat (slack_board.go
// has Slack's). The cron relay posts the stuck tasks into the home space as
// a chat message whose rows carry the board cards they stand for
// (chatmsg.Row's Tasks) and the buttons to draw under them (Actions). A Chat
// text message has one button list, under the whole message rather than
// under each row, so the renderer numbers the rows when any carries buttons
// ("1. ", "2. ", in the bullet's place) and labels each row's buttons with
// its number ("Retry 1", "Drop 1"); the number is the renderer's own, as
// the bullet is. The button's parameters carry the action, the row's number
// and the row's ids.
//
// A tap is no turn. It is admitted by the gateway's rule for the backend
// (BoardAdmits, the allowlist an inbound message is admitted by; Chat has
// no Home tab whose rule a Slack tap takes), then goes over the board route
// as a lib.BoardAction (board.go), as a Slack tap does. The row's line is
// then edited in place: "✓ Retrying. <name>, <time>", "Dropped. <name>,
// <time>" or "No longer stuck. <name>, <time>" set after its text on the
// same line, and its two buttons go; a tap that did not land keeps the
// buttons and the line says so, so the row can be tapped again. The time is
// UTC: Chat has no date token each reader sees in their own zone.
//
// One tap per row at a time, held in this process, and the edits of one
// message in turn under its rewrite lock (lockRewrite), each from the layout
// the last edit sent, which the adapter remembers per message; a message it
// does not remember (posted before a restart) is rebuilt from the event's
// copy of it, text and buttons.
const (
	// gchatBoardFunction is the onClick action a Retry or Drop sends, and
	// gchatBoardParamAction, gchatBoardParamRow and gchatBoardParamTasks its
	// parameters: the action (chatmsg.RowActionRetry or RowActionDrop), the
	// row's number as drawn, and the row's ids joined by
	// gchatBoardTaskSeparator.
	gchatBoardFunction      = "kage.board"
	gchatBoardParamAction   = "action"
	gchatBoardParamRow      = "row"
	gchatBoardParamTasks    = "tasks"
	gchatBoardTaskSeparator = ","
	// gchatBoardRowMarker leads each row of a message whose rows carry
	// buttons, in the bullet's place, and gchatBoardLabel is a button's
	// text: the action's label, then the row's number.
	gchatBoardRowMarker = "%d. "
	gchatBoardLabel     = "%s %d"
	// gchatBoardNoteSep sets a tapped row's line after its text, on the
	// same line: a line of its own would fall out of the list, as a row's
	// detail did (gchatChatRowDetailSep).
	gchatBoardNoteSep = " — "
	// gchatBoardSeenPrefix keys a board tap in the dedupe ring, apart from
	// a choice click on the same message at the same time.
	gchatBoardSeenPrefix = "board:"
	// gchatBoardTapSep joins a message name and a row's number into the
	// key one tap at a time holds.
	gchatBoardTapSep = "#"
	// The line a tapped row shows: its note, then who and when
	// (gchatBoardSettledLine) for a tap that settled the cards, the time in
	// gchatBoardTime; a tap that did not land shows Slack's whole line.
	gchatBoardRetriedNote = "✓ Retrying."
	gchatBoardDroppedNote = "Dropped."
	gchatBoardMovedNote   = "No longer stuck."
	gchatBoardSettledLine = "%s %s, %s"
	gchatBoardTime        = "15:04 UTC"
	// The widget keys a drawn button's action parameters sit under, read
	// back to find a row's buttons.
	gchatParametersKey = "parameters"
	gchatParamKey      = "key"
	gchatParamValue    = "value"

	// The reasons a board tap is not a turn, for the pull loop's log.
	gchatBoardNoRow      = "a board tap with no row, card or action"
	gchatBoardNoEmail    = "presser has no email"
	gchatBoardNoMessage  = "board tap names no space or no message"
	gchatBoardDuplicate  = "duplicate delivery of board tap "
	gchatBoardUnwired    = "a board tap with no board route wired"
	gchatBoardUnverified = "a board tap from a member the gateway would not answer"
	gchatBoardBusy       = "a second tap on a board row being tapped"
	gchatBoardTaken      = "a board tap, acted on off the pull loop: "
)

// gchatBoardNotes are the lines a tapped row may already show, as prefixes,
// so a later tap replaces the line rather than adding a second. Each is a
// whole word or sentence, never empty, so no row text is cut by one.
var gchatBoardNotes = []string{
	gchatBoardRetriedNote,
	gchatBoardDroppedNote,
	gchatBoardMovedNote,
	slackBoardRetryFailed,
	slackBoardDropFailed,
}

// errGchatBoardRowGone is a tapped row no longer in the message's text.
var errGchatBoardRowGone = errors.New("the tapped row is not in the message")

// BoardAdmits answers whether the gateway would take a turn from author on
// backend: the rule a board tap is admitted by on a backend with no Home tab
// (gchat_board.go), the same one a rating tap takes (FeedbackRecorder.Admits).
type BoardAdmits func(backend, author string) bool

// BoardAdmitSink is the optional extension an Adapter implements to take
// that rule. Wired in New beside BoardActSink.
type BoardAdmitSink interface {
	SetBoardAdmits(admits BoardAdmits)
}

var (
	_ BoardActSink   = (*GoogleChatAdapter)(nil)
	_ BoardAdmitSink = (*GoogleChatAdapter)(nil)
)

// SetBoardActor arms the Retry and Drop buttons (BoardActSink). Unarmed, a
// tap is logged and changes nothing.
func (a *GoogleChatAdapter) SetBoardActor(actor BoardActor) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.boardActor = actor
}

// SetBoardAdmits hands the adapter the rule a tap is admitted by
// (BoardAdmitSink). Without one, a tap changes nothing.
func (a *GoogleChatAdapter) SetBoardAdmits(admits BoardAdmits) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.boardAdmits = admits
}

// gchatBoardButtons is the buttons of every row that asks for them, in row
// order, each labelled with the action and the row's number as drawn
// (gchatChatRows); nil when no row does.
func gchatBoardButtons(rows []chatmsg.Row) []map[string]any {
	var buttons []map[string]any
	for i, r := range rows {
		if len(r.Actions) == 0 || len(r.Tasks) == 0 {
			continue
		}
		n := i + 1
		tasks := strings.Join(r.Tasks, gchatBoardTaskSeparator)
		for _, action := range r.Actions {
			label, ok := slackBoardLabels[action]
			if !ok {
				continue
			}
			b := gchatChatButton(fmt.Sprintf(gchatBoardLabel, label, n), false)
			b[gchatOnClickKey] = map[string]any{gchatActionKey: map[string]any{
				gchatFunctionKey: gchatBoardFunction,
				gchatParametersKey: []map[string]any{
					{gchatParamKey: gchatBoardParamAction, gchatParamValue: action},
					{gchatParamKey: gchatBoardParamRow, gchatParamValue: strconv.Itoa(n)},
					{gchatParamKey: gchatBoardParamTasks, gchatParamValue: tasks},
				},
			}}
			buttons = append(buttons, b)
		}
	}
	return buttons
}

// gchatButtonFunction is the onClick action function of a button as the
// renderer draws it, empty for a link.
func gchatButtonFunction(b map[string]any) string {
	click, _ := b[gchatOnClickKey].(map[string]any)
	action, _ := click[gchatActionKey].(map[string]any)
	function, _ := action[gchatFunctionKey].(string)
	return function
}

// gchatButtonParam is the value of a drawn button's action parameter key,
// empty when it carries none.
func gchatButtonParam(b map[string]any, key string) string {
	click, _ := b[gchatOnClickKey].(map[string]any)
	action, _ := click[gchatActionKey].(map[string]any)
	params, _ := action[gchatParametersKey].([]map[string]any)
	for _, p := range params {
		if p[gchatParamKey] == key {
			value, _ := p[gchatParamValue].(string)
			return value
		}
	}
	return ""
}

// boardClick takes one tap on a row's Retry or Drop. It is never a turn: it
// always returns the reason the pull loop logs. A tap with no row, card,
// action, presser or message, a redelivery, one with no board route or
// admission rule wired, one from a presser the gateway would not answer,
// and one on a row already being tapped change nothing. Otherwise the tap
// is acted on off the pull loop (runBoardTap).
func (a *GoogleChatAdapter) boardClick(ev *gchatEvent) string {
	click := ev.click
	verb := click.Parameters[gchatBoardParamAction]
	n, err := strconv.Atoi(click.Parameters[gchatBoardParamRow])
	var tasks []string
	for _, task := range strings.Split(click.Parameters[gchatBoardParamTasks], gchatBoardTaskSeparator) {
		if task != "" {
			tasks = append(tasks, task)
		}
	}
	switch {
	case slackBoardLabels[verb] == "" || err != nil || n < 1 || len(tasks) == 0:
		return gchatBoardNoRow
	case click.User.Email == "":
		return gchatBoardNoEmail
	case ev.Space.Name == "" || ev.Message.Name == "":
		return gchatBoardNoMessage
	}
	message := ev.Message.Name
	id := gchatClickID(message, click.EventTime)
	a.mu.Lock()
	// As a choice click: a tap with no event time has no id of its own.
	if click.EventTime != "" && a.seenLocked(gchatBoardSeenPrefix+id) {
		a.mu.Unlock()
		return gchatBoardDuplicate + id
	}
	actor, admits := a.boardActor, a.boardAdmits
	a.mu.Unlock()
	if actor == nil || admits == nil {
		return gchatBoardUnwired
	}
	if !admits(gchatBackend, click.User.Email) {
		return gchatBoardUnverified
	}
	key := message + gchatBoardTapSep + strconv.Itoa(n)
	a.mu.Lock()
	ux := a.uxLocked()
	if ux.boardTaps[key] {
		a.mu.Unlock()
		return gchatBoardBusy
	}
	ux.boardTaps[key] = true
	remember(&ux.boardTapsOrder, key, func(k string) { delete(ux.boardTaps, k) })
	a.mu.Unlock()
	name := click.User.DisplayName
	if name == "" {
		name = namelessClicker
	}
	// The rewrite runs after this returns: it reads its own copy of the
	// message, never the event the caller holds.
	snapshot := &gchatEvent{Space: ev.Space, Message: gchatMessage{Name: message, Text: ev.Message.Text,
		Thread: ev.Message.Thread, AccessoryWidgets: cloneGchatWidgets(ev.Message.AccessoryWidgets)}}
	a.goRewrite(func() { a.runBoardTap(snapshot, key, verb, n, tasks, name, actor) })
	return gchatBoardTaken + verb
}

// runBoardTap acts on the row's cards and edits the row. A tap that
// settled its cards keeps the row claimed, since its buttons are gone; one
// that did not, or whose edit failed, releases it, so the row can be tapped
// again. The release comes after the edit, under the message's rewrite lock,
// so a second tap cannot edit the row before the first has drawn its line
// and then have that line drawn over it.
func (a *GoogleChatAdapter) runBoardTap(ev *gchatEvent, key, verb string, n int, tasks []string, name string, actor BoardActor) {
	message := ev.Message.Name
	ctx, cancel := context.WithTimeout(context.Background(), slackBoardTimeout)
	defer cancel()
	res, err := actor(ctx, lib.BoardAction{Action: verb, Tasks: tasks, By: name})
	outcome := slackBoardOutcome(res, err)
	a.log.Info("gchat: board tap", "action", verb, "message", message, "tasks", strings.Join(tasks, gchatBoardTaskSeparator),
		"outcome", outcome, "err", err)
	settled := outcome != slackBoardFailed
	line := gchatBoardLine(verb, outcome, name, a.clock())
	// Under the message's rewrite lock, so the edits of one message land in
	// turn, each from the layout the last one sent.
	defer a.lockRewrite(message)()
	base, remembered := a.boardLayoutOf(message)
	if !remembered {
		base = gchatEventLayout(ev.Message)
	}
	layout, err := gchatBoardRowLayout(base, n, line, settled)
	if err == nil {
		err = a.patchLayout(message, layout)
	}
	if err != nil || !settled {
		a.releaseBoardTap(key)
	}
	if err != nil {
		// The edit may have landed though Chat's answer did not, so the
		// next edit builds from its own tap's copy of the message.
		a.forgetBoard(message)
		a.log.Warn("gchat: editing a board row failed", "message", message, "err", err)
		return
	}
	a.rememberBoard(message, layout)
}

// releaseBoardTap frees a row's claim, so it can be tapped again.
func (a *GoogleChatAdapter) releaseBoardTap(key string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.uxLocked().releaseBoardTapLocked(key)
}

// releaseBoardTapLocked is releaseBoardTap under the adapter's lock.
func (ux *gchatUX) releaseBoardTapLocked(key string) {
	delete(ux.boardTaps, key)
	forget(&ux.boardTapsOrder, key)
}

// releaseBoardTapsLocked frees every row claim on message: laid out again,
// its rows are new ones, and a claim a settled tap kept would hold their
// buttons dead. Called under the adapter's lock.
func (ux *gchatUX) releaseBoardTapsLocked(message string) {
	prefix := message + gchatBoardTapSep
	for key := range ux.boardTaps {
		if strings.HasPrefix(key, prefix) {
			ux.releaseBoardTapLocked(key)
		}
	}
}

// gchatBoardLine is the row's line after a tap: who and when for one that
// settled it, an ask to try again otherwise. The name is defanged like
// every string the adapter adds to a message.
func gchatBoardLine(verb, outcome, name string, now time.Time) string {
	if outcome == slackBoardFailed {
		if verb == chatmsg.RowActionDrop {
			return slackBoardDropFailed
		}
		return slackBoardRetryFailed
	}
	note := gchatBoardMovedNote
	switch {
	case outcome != lib.BoardOutcomeDone:
	case verb == chatmsg.RowActionDrop:
		note = gchatBoardDroppedNote
	default:
		note = gchatBoardRetriedNote
	}
	return fmt.Sprintf(gchatBoardSettledLine, note, defangGchatControls(name), now.UTC().Format(gchatBoardTime))
}

// gchatBoardRowLayout is layout with row n's line put in: after the row's
// text on its line, in place of the line an earlier tap left, and the row's
// buttons gone when settled. The row is the nth line of the rows block
// (gchatBoardRowsBlock), not the first line anywhere that starts "n. ": a
// detail or a fold may hold a numbered list of its own.
func gchatBoardRowLayout(layout gchatChatLayout, n int, line string, settled bool) (gchatChatLayout, error) {
	lines := strings.Split(layout.Text, "\n")
	start, count := gchatBoardRowsBlock(lines)
	if n > count {
		return gchatChatLayout{}, errGchatBoardRowGone
	}
	i := start + n - 1
	lines[i] = gchatBoardRowText(lines[i]) + gchatBoardNoteSep + line
	widgets := layout.Widgets
	if settled {
		widgets = gchatWithoutButtons(widgets, func(b map[string]any) bool {
			return gchatButtonFunction(b) == gchatBoardFunction && gchatButtonParam(b, gchatBoardParamRow) == strconv.Itoa(n)
		})
	}
	return gchatChatLayout{Text: strings.Join(lines, "\n"), Widgets: widgets}, nil
}

// gchatBoardRowsBlock finds the rows in a laid-out text: the longest run of
// lines numbered in order from "1. " (the renderer's markers,
// gchatChatRows), the last such run when two are as long, since the rows
// come after the detail and the fold's text is the one section after them
// that may hold a list. It returns the run's first line and its length, 0
// for none.
func gchatBoardRowsBlock(lines []string) (start, count int) {
	first := fmt.Sprintf(gchatBoardRowMarker, 1)
	for i := range lines {
		if !strings.HasPrefix(lines[i], first) {
			continue
		}
		n := 1
		for i+n < len(lines) && strings.HasPrefix(lines[i+n], fmt.Sprintf(gchatBoardRowMarker, n+1)) {
			n++
		}
		if n >= count {
			start, count = i, n
		}
	}
	return start, count
}

// gchatBoardRowText is a row's line without the line an earlier tap left on
// it, found by its separator and one of the known lines after it.
func gchatBoardRowText(l string) string {
	i := strings.LastIndex(l, gchatBoardNoteSep)
	if i < 0 {
		return l
	}
	rest := l[i+len(gchatBoardNoteSep):]
	for _, note := range gchatBoardNotes {
		if strings.HasPrefix(rest, note) {
			return l[:i]
		}
	}
	return l
}

// gchatWithoutButtons is widgets with every button drop reports removed, and
// a button list left empty by that removed with it. It returns new slices
// and maps and leaves widgets as they are, since a remembered layout is kept.
func gchatWithoutButtons(widgets []map[string]any, drop func(b map[string]any) bool) []map[string]any {
	var out []map[string]any
	for _, w := range widgets {
		list, ok := w[gchatButtonListKey].(map[string]any)
		if !ok {
			out = append(out, w)
			continue
		}
		existing, _ := list[gchatButtonsKey].([]map[string]any)
		var buttons []map[string]any
		for _, b := range existing {
			if !drop(b) {
				buttons = append(buttons, b)
			}
		}
		if len(buttons) == len(existing) {
			out = append(out, w)
			continue
		}
		if len(buttons) == 0 {
			continue
		}
		newList := make(map[string]any, len(list))
		for k, v := range list {
			newList[k] = v
		}
		newList[gchatButtonsKey] = buttons
		newWidget := make(map[string]any, len(w))
		for k, v := range w {
			newWidget[k] = v
		}
		newWidget[gchatButtonListKey] = newList
		out = append(out, newWidget)
	}
	return out
}

// gchatEventLayout is a message as the event carries it, as a layout: its
// text, and every button it shows re-encoded as the renderer draws one, in
// the event's order.
func gchatEventLayout(m gchatMessage) gchatChatLayout {
	var buttons []map[string]any
	for _, w := range m.AccessoryWidgets {
		for _, b := range w.ButtonList.Buttons {
			buttons = append(buttons, gchatButtonWidget(b))
		}
	}
	return gchatChatLayout{Text: m.Text, Widgets: gchatButtonList(buttons)}
}

// patchLayout patches message to layout.
func (a *GoogleChatAdapter) patchLayout(message string, layout gchatChatLayout) error {
	arguments := map[string]any{
		gchatNameArg:       message,
		gchatUpdateMaskArg: gchatLayoutUpdateMask,
		gchatBodyArg:       gchatLayoutBody(layout),
	}
	return a.apiCall([]string{gchatSpacesResource, gchatMessagesResource}, gchatPatchMethod, arguments, nil)
}

// trackBoard remembers the layout a message with rows that carry buttons
// was last sent with, so a tap edits from it, and forgets one laid out
// again without such rows.
// Either way the rows are new, so every claim a tap holds on the message
// goes (releaseBoardTapsLocked).
func (a *GoogleChatAdapter) trackBoard(message string, m *chatmsg.Message, layout gchatChatLayout) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	ux.releaseBoardTapsLocked(message)
	if m == nil || !slackChatRowsAct(m.Rows) {
		ux.forgetBoardLocked(message)
		return
	}
	ux.board[message] = layout
	remember(&ux.boardOrder, message, func(k string) { delete(ux.board, k) })
}

func (a *GoogleChatAdapter) rememberBoard(message string, layout gchatChatLayout) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	ux.board[message] = layout
	remember(&ux.boardOrder, message, func(k string) { delete(ux.board, k) })
}

func (a *GoogleChatAdapter) forgetBoard(message string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.uxLocked().forgetBoardLocked(message)
}

// forgetBoardLocked is forgetBoard under the adapter's lock.
func (ux *gchatUX) forgetBoardLocked(message string) {
	delete(ux.board, message)
	forget(&ux.boardOrder, message)
}

// boardLayoutOf is the layout message was last sent with, when remembered.
func (a *GoogleChatAdapter) boardLayoutOf(message string) (gchatChatLayout, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	layout, ok := a.uxLocked().board[message]
	return layout, ok
}

// clock is the adapter's time, time.Now outside a test.
func (a *GoogleChatAdapter) clock() time.Time {
	if a.now != nil {
		return a.now()
	}
	return time.Now()
}
