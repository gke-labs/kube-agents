package gateway

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// Retry and Drop on the board-health card, on Teams (slack_board.go has the
// card and the board route; gchat_board.go the admission rule this follows).
//
// A row that carries the cards it stands for (chatmsg.Row's Tasks) and the
// buttons to draw (Actions) gets them as an ActionSet right under its line in
// the card's body, so each pair sits with its row: a card's own action list
// is one list under the whole card, and Teams folds all but its first few
// into a menu. The renderer hands each row's set on among the card's buttons
// as an entry of teamsBoardRowType, which the card builder alone reads
// (teamsPlaceBoardRows) and which never reaches Teams, so every rewrite that
// keeps a card's buttons (a rating's line, an answered question's) keeps the
// rows' sets with them.
//
// A press is no turn. It is admitted by the rule an inbound message is
// (BoardAdmits), goes over the board route as a lib.BoardAction, and the row
// is drawn again: its buttons give way to "✓ Retrying. <name>, <time>",
// "Dropped. …" or "No longer stuck. …" when the cards were settled, and a
// press that did not land keeps the buttons under a line saying so. A press
// carries none of its card, so a card the adapter no longer remembers
// (posted before a restart, or pushed out of its ring) cannot be drawn
// again: the press still acts, and its line is posted in the conversation.
//
// One press per row at a time, held in this process; a restart forgets which
// rows were pressed, which costs nothing, since session-kv answers a second
// press on a moved card "moved".
const (
	// teamsBoardRowType marks an entry among a card's buttons as a row's
	// set rather than a button, and teamsBoardRowBlock, teamsBoardRowNumber,
	// teamsBoardRowActions and teamsBoardRowNote are its fields: the text of
	// the row's block in the card, the row's index among the message's rows,
	// its buttons, and the line a press left.
	teamsBoardRowType    = "kage.boardRow"
	teamsBoardRowBlock   = "block"
	teamsBoardRowNumber  = "row"
	teamsBoardRowActions = "actions"
	teamsBoardRowNote    = "note"
	// teamsCardActionSet is the body element that holds a row's buttons.
	teamsCardActionSet = "ActionSet"
	// teamsPressActionBoard is the press of a row's Retry or Drop; its data
	// carries the action, the row's index and the row's card ids joined by
	// teamsBoardTaskSeparator.
	teamsPressActionBoard   = "board"
	teamsPressVerbKey       = "verb"
	teamsPressRowKey        = "row"
	teamsPressTasksKey      = "tasks"
	teamsBoardTaskSeparator = ","
	// teamsBoardTapSep joins a message id and a row's index into the key
	// one press at a time holds.
	teamsBoardTapSep = "#"

	// The reasons a board press is not a turn, for the pull loop's log.
	teamsBoardNoRow      = "a board press with no row, card or action"
	teamsBoardNoMessage  = "board press names no message"
	teamsBoardDuplicate  = "duplicate delivery of "
	teamsBoardUnwired    = "a board press with no board route wired"
	teamsBoardUnverified = "a board press from a sender the install does not serve"
	teamsBoardBusy       = "a second press on a board row being pressed"
	teamsBoardTaken      = "a board press, acted on off the pull loop"
)

var (
	_ BoardActSink   = (*TeamsAdapter)(nil)
	_ BoardAdmitSink = (*TeamsAdapter)(nil)
)

// SetBoardActor arms the Retry and Drop buttons (BoardActSink). Unarmed, a
// press is acked away and changes nothing.
func (a *TeamsAdapter) SetBoardActor(actor BoardActor) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.boardActor = actor
}

// SetBoardAdmits hands over the rule a press is admitted by
// (BoardAdmitSink). Without it no press is admitted.
func (a *TeamsAdapter) SetBoardAdmits(admits BoardAdmits) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.boardAdmits = admits
}

// teamsBoardRowSets is one set per row that asks for buttons, in the rows'
// order: the row's block as the card builder writes it, and a button per
// action the adapter draws.
func teamsBoardRowSets(rows []chatmsg.Row) []map[string]any {
	var sets []map[string]any
	for i, r := range rows {
		line, ok := teamsChatRowLine(r)
		if !ok || len(r.Tasks) == 0 {
			continue
		}
		tasks := strings.Join(r.Tasks, teamsBoardTaskSeparator)
		var buttons []map[string]any
		for _, action := range r.Actions {
			label, ok := slackBoardLabels[action]
			if !ok {
				continue
			}
			buttons = append(buttons, map[string]any{
				"type":  teamsActionSubmit,
				"title": label,
				"data": map[string]any{
					teamsPressActionKey: teamsPressActionBoard,
					teamsPressVerbKey:   action,
					teamsPressRowKey:    i,
					teamsPressTasksKey:  tasks,
				},
			})
		}
		if len(buttons) == 0 {
			continue
		}
		sets = append(sets, map[string]any{
			"type":               teamsBoardRowType,
			teamsBoardRowBlock:   teamsBoardBlockText(line),
			teamsBoardRowNumber:  i,
			teamsBoardRowActions: buttons,
		})
	}
	return sets
}

// teamsBoardBlockText is the text of the card block teamsMarkdownToCard
// writes for a row's line.
func teamsBoardBlockText(line string) string {
	return teamsCardBullet + strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), teamsBulletDash))
}

// teamsPlaceBoardRows takes the rows' sets out of actions and puts each in
// card's body under its row's block: the line a press left, then the row's
// buttons. Sets are matched to blocks in order, so two rows with the same
// text each get their own; a set whose block is not in the body goes at the
// body's end rather than being lost. It returns the buttons left.
func teamsPlaceBoardRows(card map[string]any, actions []map[string]any) []map[string]any {
	var rest, sets []map[string]any
	for _, action := range actions {
		if action["type"] == teamsBoardRowType {
			sets = append(sets, action)
			continue
		}
		rest = append(rest, action)
	}
	if len(sets) == 0 {
		return actions
	}
	body, _ := card["body"].([]map[string]any)
	out := make([]map[string]any, 0, len(body)+2*len(sets))
	next := 0
	for _, block := range body {
		out = append(out, block)
		if next < len(sets) && block["type"] == teamsCardTextBlock && block["text"] == sets[next][teamsBoardRowBlock] {
			out = append(out, teamsBoardRowBlocks(sets[next])...)
			next++
		}
	}
	for ; next < len(sets); next++ {
		out = append(out, teamsBoardRowBlocks(sets[next])...)
	}
	card["body"] = out
	return rest
}

// teamsBoardRowBlocks is what a row's set draws under its row.
func teamsBoardRowBlocks(set map[string]any) []map[string]any {
	var blocks []map[string]any
	if note, _ := set[teamsBoardRowNote].(string); note != "" {
		blocks = append(blocks, map[string]any{"type": teamsCardTextBlock, "text": note, "wrap": true, "isSubtle": true})
	}
	if buttons, _ := set[teamsBoardRowActions].([]map[string]any); len(buttons) > 0 {
		blocks = append(blocks, map[string]any{"type": teamsCardActionSet, teamsCardActionsKey: buttons})
	}
	return blocks
}

// boardPress checks a press of a row's Retry or Drop and hands it on, and
// answers why the press is not a turn. The caller (classify) has checked
// the conversation, the service URL, the tenant and that the sender has an
// id.
func (a *TeamsAdapter) boardPress(act *teamsActivity, press teamsPress, conversation string) string {
	var tasks []string
	for _, task := range strings.Split(press.Tasks, teamsBoardTaskSeparator) {
		if task = strings.TrimSpace(task); task != "" {
			tasks = append(tasks, task)
		}
	}
	if slackBoardLabels[press.Verb] == "" || press.Row == nil || *press.Row < 0 || len(tasks) == 0 {
		return teamsBoardNoRow
	}
	message := strings.TrimSpace(act.ReplyToID)
	if message == "" {
		return teamsBoardNoMessage
	}
	presser, dup := a.admit(act, conversation)
	if dup {
		return teamsBoardDuplicate + act.ID
	}
	key := message + teamsBoardTapSep + strconv.Itoa(*press.Row)
	a.mu.Lock()
	actor, admits := a.boardActor, a.boardAdmits
	if actor == nil {
		a.mu.Unlock()
		return teamsBoardUnwired
	}
	if admits == nil || !admits(teamsBackend, presser) {
		a.mu.Unlock()
		return teamsBoardUnverified
	}
	if a.boardTaps[key] {
		a.mu.Unlock()
		return teamsBoardBusy
	}
	if a.boardTaps == nil {
		a.boardTaps = map[string]bool{}
	}
	a.boardTaps[key] = true
	a.mu.Unlock()
	name := teamsClicker(act.From.Name)
	a.boarding.Add(1)
	go func() {
		defer a.boarding.Done()
		a.runBoardPress(actor, teamsConversationID(conversation), message, key, press.Verb, *press.Row, tasks, name)
	}()
	return teamsBoardTaken
}

// runBoardPress acts on the row's cards and draws the row again, or posts
// the press's line when the adapter no longer holds the card. The row's
// claim is released at the end, under the message's edit lock, so a second
// press cannot draw the row before the first has.
func (a *TeamsAdapter) runBoardPress(actor BoardActor, conversation, message, key, verb string, row int, tasks []string, name string) {
	ctx, cancel := context.WithTimeout(context.Background(), slackBoardTimeout)
	defer cancel()
	res, err := actor(ctx, lib.BoardAction{Action: verb, Tasks: tasks, By: name})
	outcome := slackBoardOutcome(res, err)
	a.log.Info("teams: board press", "action", verb, "conversation", conversation, "tasks", strings.Join(tasks, teamsBoardTaskSeparator),
		"outcome", outcome, "err", err)
	line := teamsBoardLine(verb, outcome, name, time.Now())

	defer a.lockEdit(message)()
	defer func() {
		a.mu.Lock()
		delete(a.boardTaps, key)
		a.mu.Unlock()
	}()
	a.mu.Lock()
	card, held := a.uxLocked().cards[message]
	a.mu.Unlock()
	if held && !card.closed {
		if next, ok := teamsBoardRowDrawn(card, row, line, outcome != slackBoardFailed); ok && teamsFitsCard(next.text, next.actions()) {
			err := a.edit(conversation, message, next.text, next.actions())
			if err == nil {
				a.mu.Lock()
				if _, still := a.uxLocked().cards[message]; still {
					a.uxLocked().cards[message] = next
				}
				a.mu.Unlock()
				return
			}
			a.log.Warn("teams: drawing a pressed board row failed; posting its line in the conversation", "conversation", conversation, "err", err)
		}
	}
	if _, err := a.Post(conversation, line); err != nil {
		a.log.Warn("teams: posting a board press's line failed", "conversation", conversation, "err", err)
	}
}

// teamsBoardRowDrawn is card with row's line put in: under the row, in
// place of the line an earlier press left, with the row's buttons gone when
// settled. It reports false when the card shows no such row.
func teamsBoardRowDrawn(card teamsChatCard, row int, line string, settled bool) (teamsChatCard, bool) {
	next := card
	next.rest = make([]map[string]any, len(card.rest))
	found := false
	for i, action := range card.rest {
		next.rest[i] = action
		if action["type"] != teamsBoardRowType || action[teamsBoardRowNumber] != row {
			continue
		}
		found = true
		set := map[string]any{
			"type":              teamsBoardRowType,
			teamsBoardRowBlock:  action[teamsBoardRowBlock],
			teamsBoardRowNumber: row,
			teamsBoardRowNote:   line,
		}
		if !settled {
			set[teamsBoardRowActions] = action[teamsBoardRowActions]
		}
		next.rest[i] = set
	}
	return next, found
}

// teamsBoardLine is the row's line after a press: who and when for one that
// settled it, in Google Chat's words and its UTC time, an ask to try again
// otherwise.
func teamsBoardLine(verb, outcome, name string, now time.Time) string {
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
	return fmt.Sprintf(gchatBoardSettledLine, note, name, now.UTC().Format(gchatBoardTime))
}
