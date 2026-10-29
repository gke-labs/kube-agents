package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/slack-go/slack"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// Retry and Drop on the board-health card (agents/platform/scripts/
// board_health_card.py). The cron relay posts the stuck tasks as a chat
// message whose rows carry the board cards they stand for (chatmsg.Row's
// Tasks) and the buttons to draw under them (Actions); the renderer lays each
// such row out as its text over an actions block (slackChatRowBlocks), the
// buttons labelled here, never by the sender.
//
// A tap is no turn. It is admitted by the Home tab's rule (homeAdmitted):
// the allowlist an inbound message is admitted by, a guest by the entry
// naming them, and under allow-all the bot's own workspace. An admitted tap
// goes over the board route as a lib.BoardAction (board.go), which the
// hermes-bridge relays to session-kv's POST /v1/board/actions; that unblocks
// (Retry) or archives (Drop) the row's cards while they are still blocked.
// The row is then edited in place: its buttons give way to "✓ Retrying.
// <name>, <time>" or "Dropped. <name>, <time>", or "No longer stuck. <name>, <time>" when
// the cards had already moved; a tap that did not land keeps the buttons
// under a line saying so, and the row can be tapped again. The time is
// Slack's date token, which each reader sees in their own zone.
//
// One tap per row at a time, held in this process: a tap during another on
// the same row is dropped, and a restart forgets which rows were tapped,
// which costs nothing, since session-kv answers a second tap on a moved card
// "moved".
const (
	// slackBoardActionPrefix begins both buttons' action ids, the same under
	// today (slack_ux_clicks.py's BOARD_ACTION_ID_PATTERN).
	slackBoardActionPrefix = "kage_board."
	// slackBoardTextBlock and slackBoardRowBlock are a row's text and its
	// buttons' block ids, by the row's index; a tap edits the row by the
	// latter, and the line it leaves has the same id plus slackBoardNoteSuffix.
	slackBoardTextBlock  = "kage_board.text.%d"
	slackBoardRowBlock   = "kage_board.row.%d"
	slackBoardNoteSuffix = ".note"
	// slackBoardTaskSeparator joins the row's ids in a button's value.
	slackBoardTaskSeparator = ","
	// slackBoardTimeout bounds a tap's round trip: the bridge gives
	// session-kv 28 s.
	slackBoardTimeout = 30 * time.Second
	// The lines a tapped row shows.
	slackBoardRetried      = "✓ Retrying. %s, %s"
	slackBoardDropped      = "Dropped. %s, %s"
	slackBoardMoved        = "No longer stuck. %s, %s"
	slackBoardRetryFailed  = "Couldn't retry it just now. Try again."
	slackBoardDropFailed   = "Couldn't drop it just now. Try again."
	slackBoardTime         = "<!date^%d^{time}|%s>"
	slackBoardTimeFallback = "15:04 UTC"
	// slackBoardFailed is a tap that settled nothing.
	slackBoardFailed = "failed"
)

// slackBoardLabels are the buttons, by the action a row asks for.
var slackBoardLabels = map[string]string{chatmsg.RowActionRetry: "Retry", chatmsg.RowActionDrop: "Drop"}

// BoardActor acts on a board-health row's Retry or Drop over the board route.
type BoardActor func(ctx context.Context, a lib.BoardAction) (*lib.BoardActionResult, error)

// BoardActSink is the optional extension an Adapter implements to take a
// board-health row's taps: the gateway hands it the board route. Wired in New
// the way HomeSink is.
type BoardActSink interface {
	SetBoardActor(actor BoardActor)
}

var _ BoardActSink = (*SlackAdapter)(nil)

// SetBoardActor arms the Retry and Drop buttons (BoardActSink). Unarmed, a
// tap is logged and changes nothing.
func (s *SlackAdapter) SetBoardActor(actor BoardActor) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.boardActor = actor
}

// slackBoardButtons is a row's buttons as one actions block keyed by the row,
// or nil for a row that asks for none.
func slackBoardButtons(index int, r chatmsg.Row) map[string]any {
	if len(r.Actions) == 0 || len(r.Tasks) == 0 {
		return nil
	}
	value := strings.Join(r.Tasks, slackBoardTaskSeparator)
	var buttons []map[string]any
	for _, action := range r.Actions {
		label, ok := slackBoardLabels[action]
		if !ok {
			continue
		}
		buttons = append(buttons, map[string]any{
			"type":      "button",
			"text":      map[string]any{"type": "plain_text", "text": label, "emoji": true},
			"action_id": slackBoardActionPrefix + action,
			"value":     value,
		})
	}
	if len(buttons) == 0 {
		return nil
	}
	return map[string]any{"type": slackChatActionsBlock, "block_id": fmt.Sprintf(slackBoardRowBlock, index), "elements": buttons}
}

// boardClick takes one tap on a row's Retry or Drop, off the event pump: the
// presser's admission, the round trip and the edit run on their own
// goroutine. A tap with no row, card, presser or message, a redelivery, and
// one on a row already being tapped are dropped here.
func (s *SlackAdapter) boardClick(p slackClickPayload, actionID, blockID, value string) {
	verb := strings.TrimPrefix(actionID, slackBoardActionPrefix)
	var tasks []string
	for _, task := range strings.Split(value, slackBoardTaskSeparator) {
		if task != "" {
			tasks = append(tasks, task)
		}
	}
	if slackBoardLabels[verb] == "" || blockID == "" || len(tasks) == 0 ||
		p.User.ID == "" || p.Channel.ID == "" || p.Message.TS == "" || p.ActionTS == "" {
		s.log.Info("slack: dropping a board tap with no row, card or presser",
			"actionId", actionID, "blockId", blockID, "channel", p.Channel.ID)
		return
	}
	if s.alreadySeen("board:" + p.Channel.ID + "/" + p.ActionTS) {
		return
	}
	key := p.Channel.ID + "/" + p.Message.TS + "/" + blockID
	s.mu.Lock()
	actor := s.boardActor
	if actor == nil {
		s.mu.Unlock()
		s.log.Warn("slack: dropping a board tap: no board route is wired", "channel", p.Channel.ID)
		return
	}
	if s.boardTaps[key] {
		s.mu.Unlock()
		s.log.Info("slack: dropping a second tap on a board row", "channel", p.Channel.ID, "ts", p.Message.TS, "blockId", blockID)
		return
	}
	if s.boardTaps == nil {
		s.boardTaps = map[string]bool{}
	}
	s.boardTaps[key] = true
	remember(&s.boardTapsOrder, key, func(k string) { delete(s.boardTaps, k) })
	s.mu.Unlock()
	s.goSession(func() { s.runBoardTap(p, key, verb, tasks, blockID, actor) })
}

// runBoardTap admits the presser, acts on the row's cards and edits the row.
func (s *SlackAdapter) runBoardTap(p slackClickPayload, key, verb string, tasks []string, blockID string, actor BoardActor) {
	release := func() {
		s.mu.Lock()
		delete(s.boardTaps, key)
		forget(&s.boardTapsOrder, key)
		s.mu.Unlock()
	}
	if admitted, decided := s.homeAdmitted(p.User.ID); !admitted || !decided {
		release()
		s.log.Info("slack: ignoring a board tap from a member the gateway would not answer",
			"user", p.User.ID, "decided", decided, "channel", p.Channel.ID)
		return
	}
	name := s.slackUserName(p.User.ID)
	ctx, cancel := context.WithTimeout(context.Background(), slackBoardTimeout)
	defer cancel()
	res, err := actor(ctx, lib.BoardAction{Action: verb, Tasks: tasks, By: name})
	outcome := slackBoardOutcome(res, err)
	s.log.Info("slack: board tap", "action", verb, "user", p.User.ID, "tasks", strings.Join(tasks, slackBoardTaskSeparator),
		"outcome", outcome, "err", err)
	if outcome == slackBoardFailed {
		release()
	}
	line := slackBoardLine(verb, outcome, name, s.now())
	// One board edit at a time, each from the blocks the last edit of that
	// message sent: a tap's payload holds the message as it was when tapped,
	// and another row's edit may have landed since.
	s.boardEditMu.Lock()
	defer s.boardEditMu.Unlock()
	msgKey := p.Channel.ID + "/" + p.Message.TS
	s.mu.Lock()
	base, ok := s.boardSent[msgKey]
	s.mu.Unlock()
	if !ok {
		base = p.Message.Blocks
	}
	sent, err := slackBoardRowLayout(base, blockID, line, outcome != slackBoardFailed)
	var blocks []slack.Block
	if err == nil {
		blocks, err = slackRawBlocksJSON(sent)
	}
	if err == nil {
		_, _, _, err = s.api.UpdateMessage(p.Channel.ID, p.Message.TS,
			slack.MsgOptionText(p.Message.Text, false), slack.MsgOptionBlocks(blocks...))
	}
	if err != nil {
		// The cards may have moved; a second tap asks again and redraws the
		// row ("No longer stuck." for cards already moved). A failed tap was
		// released already, and releasing it again could free a newer tap's
		// claim. The edit may have landed though Slack's answer did not, so
		// the next edit builds from its own tap's payload.
		if outcome != slackBoardFailed {
			release()
		}
		s.mu.Lock()
		delete(s.boardSent, msgKey)
		forget(&s.boardSentOrder, msgKey)
		s.mu.Unlock()
		s.log.Warn("slack: editing a board row failed", "channel", p.Channel.ID, "ts", p.Message.TS, "err", err)
		return
	}
	s.mu.Lock()
	if s.boardSent == nil {
		s.boardSent = map[string]json.RawMessage{}
	}
	s.boardSent[msgKey] = sent
	remember(&s.boardSentOrder, msgKey, func(k string) { delete(s.boardSent, k) })
	s.mu.Unlock()
}

// slackRawBlocksJSON is slackRawBlocks over blocks held as JSON.
func slackRawBlocksJSON(raw json.RawMessage) ([]slack.Block, error) {
	var blocks []map[string]any
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, err
	}
	return slackRawBlocks(blocks)
}

// slackBoardOutcome is one word for a row: failed when any card was not
// settled, else done when any card was acted on, moved when every card was
// already unstuck or gone.
func slackBoardOutcome(res *lib.BoardActionResult, err error) string {
	if err != nil || res == nil || res.Error != "" || len(res.Results) == 0 {
		return slackBoardFailed
	}
	done := false
	for _, r := range res.Results {
		switch r.Outcome {
		case lib.BoardOutcomeDone:
			done = true
		case lib.BoardOutcomeMoved, lib.BoardOutcomeGone:
		default:
			// A card not settled keeps the row's buttons, so a second tap
			// reaches the cards left; the settled ones answer "moved".
			return slackBoardFailed
		}
	}
	if done {
		return lib.BoardOutcomeDone
	}
	return lib.BoardOutcomeMoved
}

// slackBoardLine is the row's line after a tap: who and when for one that
// settled it, an ask to try again otherwise. name is escaped for mrkdwn.
func slackBoardLine(verb, outcome, name string, now time.Time) string {
	if outcome == slackBoardFailed {
		if verb == chatmsg.RowActionDrop {
			return slackBoardDropFailed
		}
		return slackBoardRetryFailed
	}
	stamp := fmt.Sprintf(slackBoardTime, now.Unix(), now.UTC().Format(slackBoardTimeFallback))
	format := slackBoardMoved
	switch {
	case outcome != lib.BoardOutcomeDone:
	case verb == chatmsg.RowActionDrop:
		format = slackBoardDropped
	default:
		format = slackBoardRetried
	}
	return fmt.Sprintf(format, slackEscaper.Replace(name), stamp)
}

// errSlackBoardRowGone is a tapped row no longer in the message.
var errSlackBoardRowGone = errors.New("the tapped row is not in the message")

// slackBoardRowLayout is the message's blocks with the row's line put in: in
// place of its buttons when settled, else just above them, replacing any line
// an earlier tap left. JSON, to be sent as written (slackRawBlocks), as the
// renderer sends them.
func slackBoardRowLayout(raw json.RawMessage, blockID, line string, settled bool) (json.RawMessage, error) {
	var blocks []map[string]any
	if err := json.Unmarshal(raw, &blocks); err != nil {
		return nil, err
	}
	noteID := blockID + slackBoardNoteSuffix
	note := map[string]any{"type": slackContextBlock, "block_id": noteID, "elements": []any{
		map[string]any{"type": slackMrkdwnText, "text": line}}}
	out := make([]map[string]any, 0, len(blocks)+1)
	found := false
	for _, b := range blocks {
		id, _ := b["block_id"].(string)
		switch id {
		case noteID:
			continue
		case blockID:
			found = true
			out = append(out, note)
			if !settled {
				out = append(out, b)
			}
			continue
		}
		out = append(out, b)
	}
	if !found {
		return nil, errSlackBoardRowGone
	}
	return json.Marshal(out)
}
