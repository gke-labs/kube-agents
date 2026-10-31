package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/slack-go/slack"
	"github.com/slack-go/slack/slackevents"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// Clicks and answered questions (#2149 build item 7, #2192, #2346).
//
// A click on a choice button the renderer drew is a text turn from the
// clicker: its text is the choice's reply (the button's value), or its label
// when it has none, handed to the gateway exactly like a typed message, so it
// passes the principal map and the allowlist like one and carries no intent
// of its own (spec-a2a-payloads.md, "choices"). Once the gateway says the
// clicker is verified (VerifiedObserver), the clicked message is rewritten:
// its choice buttons removed and "✓ <name>: <label>" added, the clicker
// named by their Slack display name, never by a mention or an id. A click
// from a sender the gateway drops changes nothing.
//
// A typed reply in a conversation whose question is still open answers it
// the same way (gap G3 on #2149): the question loses its buttons and gains
// "✓ <name>: <the reply's first words>". A question is answered once,
// whichever way comes first; a second click on it is dropped.
//
// What the adapter remembers lives in this process: a question posted before
// a restart keeps its buttons until clicked, and the click is still a turn.
const (
	// slackChoiceActionPrefix begins the action_id of every choice button
	// the renderer draws (slackChatActions); a link button's needs nothing
	// but the ack, since Slack has opened the url.
	slackChoiceActionPrefix = slackChatActionPrefix + "." + slackChatChoiceAction + "."

	// answeredLine is the line an answer leaves on its question.
	answeredLine = "✓ %s: %s"
	// namelessClicker stands in for a clicker the backend names nowhere:
	// never their raw id.
	namelessClicker = "Someone"
	// slackTypedAnswerMax clips a typed answer's words on its line (today's
	// TYPED_ANSWER_MAX).
	slackTypedAnswerMax = 80
	slackClipEllipsis   = "…"

	slackBlockActions  = "block_actions"
	slackBlockActionsT = "actions"
	slackContextBlock  = "context"
	slackSectionBlock  = "section"
	slackPlainText     = "plain_text"
	slackMrkdwnText    = "mrkdwn"

	// uxCap bounds each of the click and question maps, oldest first.
	uxCap = 512

	// slackDMChannelPrefix is a DM channel id's first letter.
	slackDMChannelPrefix = "D"
)

// slackClickPayload is the part of a block_actions payload the adapter reads.
type slackClickPayload struct {
	Type string `json:"type"`
	User struct {
		ID     string `json:"id"`
		TeamID string `json:"team_id"`
	} `json:"user"`
	Team struct {
		ID string `json:"id"`
	} `json:"team"`
	Channel struct {
		ID string `json:"id"`
	} `json:"channel"`
	Container struct {
		ThreadTS string `json:"thread_ts"`
	} `json:"container"`
	Message struct {
		TS          string          `json:"ts"`
		ThreadTS    string          `json:"thread_ts"`
		Text        string          `json:"text"`
		Blocks      json.RawMessage `json:"blocks"`
		Attachments json.RawMessage `json:"attachments"`
	} `json:"message"`
	Actions []struct {
		// Type is the element's (button, feedback_buttons), logged with
		// the ids when a click is dropped.
		Type     string `json:"type"`
		ActionID string `json:"action_id"`
		BlockID  string `json:"block_id"`
		Value    string `json:"value"`
		// ActionTS is where a block_actions payload carries the click's
		// timestamp (interactive copies it to the payload's ActionTS).
		ActionTS string `json:"action_ts"`
		Text     struct {
			Text string `json:"text"`
		} `json:"text"`
	} `json:"actions"`
	ActionTS string `json:"action_ts"`
	// TriggerID opens a modal for the clicker (a 👎's note,
	// slack_feedback.go).
	TriggerID string `json:"trigger_id"`
}

// slackQuestion is a posted message with choice buttons, as the rewrite
// needs it: where it is, its text, and its blocks and attachments as Slack
// holds them.
type slackQuestion struct {
	channel, thread, ts, text string
	blocks, attachments       json.RawMessage
	// title is the question as the Home tab lists it: its chat message's
	// headline, or its text when it has none.
	title string
}

// slackPending is a message handed to the gateway and not yet verified: a
// click, with the question it answered and the label it showed; a typed
// message, which answers the conversation's open question and may title the
// thread; or a shortcut, whose text is the message it quotes, which may title
// the thread, and whose responseURL reaches its clicker alone.
type slackPending struct {
	conversation, author, text string
	// homeType is the channel_type of a top-level channel mention, the one
	// shape that can set the home channel (noteHomeCandidate); "" otherwise.
	homeType    string
	click       bool
	label       string
	question    slackQuestion
	shortcut    bool
	responseURL string
	// clicker is a shortcut clicker's Slack name, for the credit line.
	clicker string
}

// slackUX is the adapter's click and question state, under s.mu.
type slackUX struct {
	pending      map[string]slackPending
	pendingOrder []string
	// claims holds a clicked question (channel/ts) while its click waits on
	// verification, so a second click in that window is dropped; answered
	// holds it once a verified answer has taken it.
	claims        map[string]bool
	answered      map[string]bool
	answeredOrder []string
	// lines is the answered line a rewrite left on a question, so a later
	// edit of that message without its choices (the card's question
	// settled on the notify route) keeps it (answeredLine).
	lines map[string]string
	// questions is each conversation's open question, the latest posted.
	questions      map[string]slackQuestion
	questionsOrder []string
	// credits is each conversation's verified shortcut credit line until the
	// shortcut's own task starts there (slack_shortcut.go).
	credits      map[string]string
	creditsOrder []string
}

// uxLocked is s.ux, made on first use. Caller holds s.mu.
func (s *SlackAdapter) uxLocked() *slackUX {
	if s.ux == nil {
		s.ux = &slackUX{pending: map[string]slackPending{}, claims: map[string]bool{},
			answered: map[string]bool{}, lines: map[string]string{}, questions: map[string]slackQuestion{},
			credits: map[string]string{}}
	}
	return s.ux
}

// remember adds key to a bounded ring, evicting the oldest past the cap with
// drop.
//
// A key keeps one slot: one already in the ring moves to the newest end
// rather than gaining a second slot, whose older twin's eviction would
// otherwise drop the live entry (a map entry deleted without retiring its
// slot, then added again). The rings hold at most uxCap keys, so the
// scan is bounded.
func remember(order *[]string, key string, drop func(string)) {
	forget(order, key)
	*order = append(*order, key)
	if len(*order) > uxCap {
		drop((*order)[0])
		*order = (*order)[1:]
	}
}

// forget retires key's slot from a ring, if it has one, so the ring counts
// only live entries.
func forget(order *[]string, key string) {
	for i, k := range *order {
		if k == key {
			*order = append((*order)[:i:i], (*order)[i+1:]...)
			return
		}
	}
}

func slackPendingKey(conversation, messageID string) string {
	return conversation + "\n" + messageID
}

// noteQuestion records a message the adapter posted with choice buttons as
// its conversation's open question, so a typed reply can answer it. Called
// by the post path once Slack has the message.
func (s *SlackAdapter) noteQuestion(conversation string, q slackQuestion) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ux := s.uxLocked()
	if _, ok := ux.questions[conversation]; !ok {
		remember(&ux.questionsOrder, conversation, func(k string) { delete(ux.questions, k) })
	}
	ux.questions[conversation] = q
}

// trackQuestion keeps the open-question record in step with a message the
// adapter just posted or edited at ts: one whose chat message carries
// choices becomes the conversation's open question, a fresh one even when
// the message was answered before (a card asking again on its update key),
// and an edit that leaves the open question without them (a card's question
// settled on the notify route by its update key) closes it. Choices that are
// all an answer's next steps (Choice.Next) ask nothing, so their message is
// treated as one without choices: a typed reply never answers it, and it
// never displaces a question that is open. Called once Slack has the message.
func (s *SlackAdapter) trackQuestion(conversation, ts string, m OutMessage) {
	channel, thread, _ := slackChannelThread(conversation)
	if m.Chat != nil && m.Chat.Plan != nil {
		// A card moved: the Home tab's running list reads the board again.
		s.homeChanged(true)
	}
	if m.Chat == nil || !asksQuestion(m.Chat.Choices) {
		s.mu.Lock()
		ux := s.uxLocked()
		closed := false
		if open, ok := ux.questions[conversation]; ok && open.ts == ts {
			delete(ux.questions, conversation)
			closed = true
		}
		s.mu.Unlock()
		if closed {
			s.homeChanged(false)
		}
		return
	}
	text := toMrkdwn(m.Text)
	layout, err := renderSlackChat(m.Chat, text)
	if err != nil {
		return
	}
	if s.rates(m) {
		// As posted, so a typed answer's rewrite keeps the rating controls.
		layout = withFeedback(layout, m.Feedback)
	}
	blocks, err := json.Marshal(layout.Blocks)
	if err != nil {
		return
	}
	var attachments json.RawMessage
	if len(layout.Attachments) > 0 {
		if attachments, err = json.Marshal(layout.Attachments); err != nil {
			return
		}
	}
	s.mu.Lock()
	ux := s.uxLocked()
	delete(ux.answered, channel+"/"+ts)
	delete(ux.lines, channel+"/"+ts)
	s.mu.Unlock()
	title := text
	if strings.TrimSpace(m.Chat.Headline) != "" {
		title = toMrkdwn(m.Chat.Headline)
	}
	s.noteQuestion(conversation, slackQuestion{channel: channel, thread: thread, ts: ts, text: text, title: title,
		blocks: blocks, attachments: attachments})
	s.homeChanged(false)
}

// asksQuestion is whether choices hold one that is not an answer's next step.
func asksQuestion(choices []chatmsg.Choice) bool {
	for _, c := range choices {
		if !c.Next {
			return true
		}
	}
	return false
}

// slackAnsweredOptions is slackMessageOptions for a message laid over a
// question already answered: the same layout with the answered line added
// where slackAnsweredLayout would put it, or, for text alone, the text as a
// section with the line under it (text past a section's cap posts as text
// alone, without the line).
func slackAnsweredOptions(m OutMessage, line string) ([]slack.MsgOption, error) {
	text := toMrkdwn(m.Text)
	note := map[string]any{"type": slackContextBlock, "elements": []any{
		map[string]any{"type": slackPlainText, "text": line, "emoji": true}}}
	if m.Chat == nil {
		if utf8.RuneCountInString(text) > slackChatSectionMax {
			return slackMessageOptions(m)
		}
		set, err := slackRawBlocks([]map[string]any{
			{"type": slackSectionBlock, "text": map[string]any{"type": slackMrkdwnText, "text": text}}, note})
		if err != nil {
			return nil, err
		}
		return []slack.MsgOption{slack.MsgOptionText(text, false), slack.MsgOptionBlocks(set...),
			slack.MsgOptionAttachments([]slack.Attachment{}...)}, nil
	}
	// Refusals wrapped as slackMessageOptions wraps them, so a caller tells
	// a refused layout from a failed call (ErrLayoutRefused).
	if utf8.RuneCountInString(text) > slackChatTextMax {
		return nil, fmt.Errorf("%w: %w", ErrLayoutRefused, errSlackChatTooLarge)
	}
	layout, err := renderSlackChat(m.Chat, text)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", ErrLayoutRefused, err)
	}
	if n := len(layout.Attachments); n > 0 {
		last := layout.Attachments[n-1]
		inner, _ := last["blocks"].([]map[string]any)
		last["blocks"] = append(inner, note)
	} else {
		layout.Blocks = append(layout.Blocks, note)
	}
	return slackChatOptions(layout, text)
}

// answersOpenQuestion reports a typed message that answers its
// conversation's open question: one is open, unanswered, and the message is
// not a gateway command (InboundMessage.Answer).
func (s *SlackAdapter) answersOpenQuestion(msg InboundMessage) bool {
	if chattext.IsCommand(msg.Text) {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ux := s.uxLocked()
	open, ok := ux.questions[msg.Conversation]
	return ok && !ux.answered[open.channel+"/"+open.ts]
}

// notePending records a typed turn the gateway has not verified yet.
func (s *SlackAdapter) notePending(msg InboundMessage) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.addPendingLocked(slackPendingKey(msg.Conversation, msg.MessageID),
		slackPending{conversation: msg.Conversation, author: slackMemberOf(msg.AuthorID), text: msg.Text})
}

// noteHomeCandidate marks a pending turn that could set the home channel: a
// top-level message in a channel, a private channel or a group direct message
// (Kind "group"; a DM is never one). A top-level channel message that reached
// here mentioned the bot (inboundEvent). InboundVerified acts on the mark
// once the gateway has verified the sender.
func (s *SlackAdapter) noteHomeCandidate(msg InboundMessage, m *slackevents.MessageEvent) {
	if msg.Kind != slackKindGroup || m.ThreadTimeStamp != "" || msg.Unaddressed {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ux := s.uxLocked()
	key := slackPendingKey(msg.Conversation, msg.MessageID)
	if p, ok := ux.pending[key]; ok {
		p.homeType = m.ChannelType
		ux.pending[key] = p
	}
}

// slackMemberOf is the Slack member id behind an author id: the id itself for
// our own member, the part after the workspace for an admitted guest's
// "T…:U…" (slackGuestEntry). A pending turn keeps this raw id, because what it
// feeds, the answered line's display name and the Home tab's asker, are Slack
// API calls that take a member id.
func slackMemberOf(author string) string {
	if _, member, found := strings.Cut(author, slackGuestSep); found {
		return member
	}
	return author
}

// The first entry under a key stands until the gateway's verdict takes it: a
// shortcut and the typed message it was used on share one key (the message's
// ts), and a second writer would take the first's verdict. Nothing else
// writes a key twice: a redelivered message is dropped by alreadySeen, and a
// click is keyed by its own action_ts.
func (s *SlackAdapter) addPendingLocked(key string, p slackPending) {
	ux := s.uxLocked()
	if _, ok := ux.pending[key]; ok {
		return
	}
	remember(&ux.pendingOrder, key, func(k string) {
		if old, ok := ux.pending[k]; ok && old.click {
			delete(ux.claims, old.question.channel+"/"+old.question.ts)
		}
		delete(ux.pending, k)
	})
	ux.pending[key] = p
}

// interactive turns one interactive envelope's payload into a turn, or
// reports it is not one: a click on a choice button, from a member of the
// bot's own workspace or a guest the allowlist names as "T…:U…" (admitGuest),
// on a question not already answered.
func (s *SlackAdapter) interactive(raw json.RawMessage) (InboundMessage, bool) {
	var p slackClickPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		s.log.Warn("slack: unreadable interactive payload", "err", err)
		return InboundMessage{}, false
	}
	if p.ActionTS == "" && len(p.Actions) > 0 {
		// Slack documents a block_actions payload's action_ts inside each
		// action, not at the top as a shortcut's is; read without this, a
		// real click has no timestamp and is dropped.
		p.ActionTS = p.Actions[0].ActionTS
	}
	if p.Type == slackViewSubmission {
		// A 👎's note, sent from its modal: never a turn.
		s.feedbackNote(raw)
		return InboundMessage{}, false
	}
	if p.Type != slackBlockActions || len(p.Actions) == 0 {
		return InboundMessage{}, false
	}
	action := p.Actions[0]
	if action.Type == slackFeedbackButtons && action.ActionID != slackFeedbackAction {
		// A rating control the gateway did not draw (another app's, or an
		// action id that changed): logged, so a tap never vanishes unseen.
		s.log.Info("slack: dropping a rating tap with an unknown action id",
			"actionType", action.Type, "actionId", action.ActionID, "blockId", action.BlockID, "channel", p.Channel.ID)
		return InboundMessage{}, false
	}
	if action.ActionID == slackFeedbackAction {
		// A rating: never a turn.
		s.feedbackClick(p, action.Type, action.BlockID, action.Value)
		return InboundMessage{}, false
	}
	if strings.HasPrefix(action.ActionID, slackBoardActionPrefix) {
		// A board-health row's Retry or Drop: never a turn (slack_board.go).
		s.boardClick(p, action.ActionID, action.BlockID, action.Value)
		return InboundMessage{}, false
	}
	if !strings.HasPrefix(action.ActionID, slackChoiceActionPrefix) {
		// A link button (Slack opened the url) or a button not ours.
		return InboundMessage{}, false
	}
	if p.User.ID == "" || p.Channel.ID == "" || p.Message.TS == "" || p.ActionTS == "" {
		return InboundMessage{}, false
	}
	conversation, thread, kind := slackClickConversation(p)
	author := p.User.ID
	if s.otherWorkspace(p.User.TeamID) || s.otherWorkspace(p.Team.ID) {
		// The message path's rule (foreignSender): a member of another
		// workspace is admitted only when the allowlist names them by
		// workspace, and their click then carries the same "T…:U…" author.
		guest, ok := s.admitGuest(p.User.TeamID, p.Team.ID, p.User.ID)
		author = guest
		if !ok {
			s.log.Info("slack: ignoring a click from another workspace's member",
				"user", p.User.ID, "userTeam", p.User.TeamID, "team", s.teamID, "channel", p.Channel.ID)
			// The same once-per-sender notice a refused message gets, in the
			// question's thread: a button that does nothing looks broken.
			s.noticeForeignSender(p.Channel.ID, thread, p.User.TeamID, p.Team.ID, p.User.ID)
			return InboundMessage{}, false
		}
	}
	if s.alreadySeen("click:" + p.Channel.ID + "/" + p.ActionTS) {
		return InboundMessage{}, false
	}
	text := action.Value
	if text == "" {
		text = action.Text.Text
	}
	if strings.TrimSpace(text) == "" {
		return InboundMessage{}, false
	}
	q := slackQuestion{channel: p.Channel.ID, thread: thread, ts: p.Message.TS, text: p.Message.Text,
		blocks: p.Message.Blocks, attachments: p.Message.Attachments}
	claim := q.channel + "/" + q.ts
	s.mu.Lock()
	ux := s.uxLocked()
	if ux.answered[claim] || ux.claims[claim] {
		s.mu.Unlock()
		s.log.Info("slack: dropping a click on a question already answered", "channel", p.Channel.ID, "ts", p.Message.TS)
		return InboundMessage{}, false
	}
	ux.claims[claim] = true
	s.addPendingLocked(slackPendingKey(conversation, p.ActionTS), slackPending{conversation: conversation,
		author: p.User.ID, text: text, click: true, label: action.Text.Text, question: q})
	s.mu.Unlock()
	return InboundMessage{
		Conversation: conversation,
		Kind:         kind,
		AuthorID:     author,
		MessageID:    p.ActionTS,
		Text:         text,
		Click:        true,
	}, true
}

// slackClickConversation is the conversation a clicked message is in, the
// thread it is in (the message itself when it is top-level), and the turn
// kind a click there is.
func slackClickConversation(p slackClickPayload) (conversation, thread, kind string) {
	thread = p.Message.ThreadTS
	if thread == "" {
		thread = p.Container.ThreadTS
	}
	if thread == "" {
		thread = p.Message.TS
	}
	kind, channelType := "group", "channel"
	if strings.HasPrefix(p.Channel.ID, slackDMChannelPrefix) {
		kind, channelType = "dm", slackChannelTypeIM
	}
	return slackConversationID(channelType, p.Channel.ID, thread), thread, kind
}

// InboundVerified acts on a message once the gateway has decided on its
// sender (VerifiedObserver): a verified click rewrites the question it
// answered; a verified typed reply answers the conversation's open question
// and offers the thread its first ask as a title. A dropped click releases
// its question for the next one. The Slack calls run off the inbox worker.
func (s *SlackAdapter) InboundVerified(conversation, messageID string, verified bool) {
	key := slackPendingKey(conversation, messageID)
	s.mu.Lock()
	ux := s.uxLocked()
	p, ok := ux.pending[key]
	delete(ux.pending, key)
	// Its slot too, so the cap counts messages still waiting, and a waiting
	// click is not evicted behind messages already verified.
	forget(&ux.pendingOrder, key)
	if !ok {
		s.mu.Unlock()
		return
	}
	if verified {
		// Any verified turn in the conversation: a shortcut's credit line
		// still waiting for its task is not about the task this turn may
		// start. A shortcut arms its own below.
		s.uncreditLocked(conversation)
	}
	if verified && p.homeType != "" && !p.click && !p.shortcut && !chattext.IsCommand(p.text) {
		// A verified ask typed at the top of a channel: the one turn that can
		// set the home channel (slack_home_channel.go). A command is not an
		// ask, and a click or a shortcut is not typed there.
		s.homeCandidate(conversation, p.homeType, p.author)
	}
	if p.shortcut {
		// A shortcut answers no question: it is an ask about a message, so
		// it titles the thread after that message and credits the clicker
		// when its task starts, and a clicker the gateway refused is told so
		// where only they can see it.
		if verified {
			s.creditLocked(conversation, fmt.Sprintf(slackShortcutCredit, p.clicker))
			s.mu.Unlock()
			s.noteAsk(conversation, p.text)
			// The clicker asked, so a question the task posts waits on them
			// (the Home tab's "waiting on you"), as for a typed ask.
			s.noteAsker(conversation, p.author)
			return
		}
		s.mu.Unlock()
		s.goSession(func() {
			ctx, cancel := context.WithTimeout(context.Background(), slackShortcutTimeout)
			defer cancel()
			s.respondPrivately(ctx, p.responseURL, unverifiedNotice(slackBackend, p.author))
		})
		return
	}
	var q slackQuestion
	label := ""
	switch {
	case p.click:
		claim := p.question.channel + "/" + p.question.ts
		delete(ux.claims, claim)
		// A typed reply verified first answered it already: the click's turn
		// has gone to the gateway, but the question is not rewritten twice.
		if !verified || ux.answered[claim] {
			s.mu.Unlock()
			return
		}
		s.answerLocked(claim)
		if open, ok := ux.questions[conversation]; ok && open.ts == p.question.ts {
			delete(ux.questions, conversation)
		}
		q, label = p.question, p.label
	case verified && !chattext.IsCommand(p.text):
		// A stop, a status ask, /session or a delegate is the gateway's,
		// not an answer.
		open, ok := ux.questions[conversation]
		if ok && !ux.answered[open.channel+"/"+open.ts] {
			delete(ux.questions, conversation)
			s.answerLocked(open.channel + "/" + open.ts)
			q, label = open, slackTypedWords(p.text)
		}
	}
	s.mu.Unlock()
	if verified && !p.click && !chattext.IsCommand(p.text) && q.ts == "" {
		// An ask, not an answer: whoever answers a question, by typing or by
		// clicking, does not become the one the next question waits on.
		s.noteAsker(conversation, p.author)
	}
	if q.ts != "" {
		s.homeChanged(false)
	}
	if verified && !p.click && !chattext.IsCommand(p.text) && q.ts == "" {
		// A command neither answers nor titles, and an answer to an open
		// question is no ask to title the thread after.
		s.noteAsk(conversation, p.text)
	}
	if q.ts == "" {
		return
	}
	s.goSession(func() { s.rewriteAnswered(q, p.author, label) })
}

// answerLocked records a question as answered. Caller holds s.mu.
func (s *SlackAdapter) answerLocked(claim string) {
	ux := s.uxLocked()
	if !ux.answered[claim] {
		ux.answered[claim] = true
		remember(&ux.answeredOrder, claim, func(k string) {
			delete(ux.answered, k)
			delete(ux.lines, k)
		})
	}
}

// answeredLine is the line a rewrite left on the message at channel/ts, or
// "".
func (s *SlackAdapter) answeredLine(channel, ts string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.uxLocked().lines[channel+"/"+ts]
}

// slackTypedWords is a typed answer's words for its line: Slack's entities
// reduced, whitespace collapsed, clipped to slackTypedAnswerMax code points.
func slackTypedWords(text string) string {
	words := strings.Join(strings.Fields(slackEntitiesReduced(text)), " ")
	if utf8.RuneCountInString(words) <= slackTypedAnswerMax {
		return words
	}
	r := []rune(words)
	return strings.TrimRight(string(r[:slackTypedAnswerMax-utf8.RuneCountInString(slackClipEllipsis)]), " ") + slackClipEllipsis
}

// slackEntitiesReduced is text with Slack's entities as plain text shows
// them: a mention dropped (a name needs a lookup), a channel by its name, a
// link by its label, or its url when it has none.
func slackEntitiesReduced(text string) string {
	text = slackTitleMention.ReplaceAllString(text, " ")
	text = slackTitleChannel.ReplaceAllString(text, "$1")
	return slackTitleLink.ReplaceAllStringFunc(text, func(m string) string {
		sub := slackTitleLink.FindStringSubmatch(m)
		if sub[2] != "" {
			return sub[2]
		}
		return sub[1]
	})
}

// slackUserName is how a user is named on an answered line: display name,
// then real name, then handle, never a mention or an id.
func (s *SlackAdapter) slackUserName(userID string) string {
	u, err := s.api.GetUserInfo(userID)
	return s.slackNameOf(userID, u, err)
}

// slackUserNameContext is slackUserName under ctx.
func (s *SlackAdapter) slackUserNameContext(ctx context.Context, userID string) string {
	u, err := s.api.GetUserInfoContext(ctx, userID)
	return s.slackNameOf(userID, u, err)
}

// slackNameOf is the name a users.info answer gives userID.
func (s *SlackAdapter) slackNameOf(userID string, u *slack.User, err error) string {
	if err != nil || u == nil {
		if err != nil {
			s.log.Debug("slack: users.info failed; naming the answer generically", "user", userID, "err", err)
		}
		return namelessClicker
	}
	for _, name := range []string{u.Profile.DisplayName, u.RealName, u.Profile.RealName, u.Name} {
		if name = strings.TrimSpace(name); name != "" {
			return name
		}
	}
	return namelessClicker
}

// rewriteAnswered rewrites a question as answered by userID with label. If
// Slack refuses the rewrite, the line is posted in the question's thread
// instead, so an answer always shows once.
func (s *SlackAdapter) rewriteAnswered(q slackQuestion, userID, label string) {
	line := fmt.Sprintf(answeredLine, s.slackUserName(userID), label)
	blocks, attachments, err := slackAnsweredLayout(q.blocks, q.attachments, line)
	if err == nil {
		opts := []slack.MsgOption{slack.MsgOptionText(q.text, false), slack.MsgOptionBlocks(blocks.BlockSet...)}
		if attachments != nil {
			opts = append(opts, slack.MsgOptionAttachments(attachments...))
		}
		_, _, _, err = s.api.UpdateMessage(q.channel, q.ts, opts...)
	}
	if err == nil {
		// Only once it is on the message: a later edit keeps the line it
		// shows (answeredLine), and one posted in the thread instead below
		// must not show twice.
		s.mu.Lock()
		if s.uxLocked().answered[q.channel+"/"+q.ts] {
			s.uxLocked().lines[q.channel+"/"+q.ts] = line
		}
		s.mu.Unlock()
		return
	}
	s.log.Warn("slack: rewriting an answered question failed; posting the answer in its thread", "channel", q.channel, "ts", q.ts, "err", err)
	thread := q.thread
	if thread == "" {
		thread = q.ts
	}
	if _, _, err := s.api.PostMessage(q.channel, slack.MsgOptionText(slackEscaper.Replace(line), false), slack.MsgOptionTS(thread)); err != nil {
		s.log.Warn("slack: posting an answered line failed", "channel", q.channel, "err", err)
	}
}

// slackAnsweredLayout is a question's blocks and attachments with its choice
// buttons removed and line added: after the
// blocks the buttons were taken from (an attachment's, beside its colour,
// when the question has a side bar), else after the message's own blocks.
func slackAnsweredLayout(rawBlocks, rawAttachments json.RawMessage, line string) (slack.Blocks, []slack.Attachment, error) {
	var top []map[string]any
	if len(rawBlocks) > 0 {
		if err := json.Unmarshal(rawBlocks, &top); err != nil {
			return slack.Blocks{}, nil, err
		}
	}
	var atts []map[string]any
	if len(rawAttachments) > 0 {
		if err := json.Unmarshal(rawAttachments, &atts); err != nil {
			return slack.Blocks{}, nil, err
		}
	}
	note := map[string]any{"type": slackContextBlock, "elements": []any{
		map[string]any{"type": slackPlainText, "text": line, "emoji": true}}}
	top, placed := slackWithoutChoices(top, note)
	for _, a := range atts {
		inner, _ := a["blocks"].([]any)
		if inner == nil {
			continue
		}
		var list []map[string]any
		for _, b := range inner {
			if m, ok := b.(map[string]any); ok {
				list = append(list, m)
			}
		}
		var did bool
		list, did = slackWithoutChoices(list, nil)
		if did && !placed {
			list = slackWithNote(list, note)
			placed = true
		}
		a["blocks"] = list
	}
	if !placed {
		top = slackWithNote(top, note)
	}
	// Sent as written, as the renderer sends them (slackRawBlocks):
	// slack-go's typed blocks drop what they do not model, a fold's
	// container children and a plan's task cards among it.
	set, err := slackRawBlocks(top)
	if err != nil {
		return slack.Blocks{}, nil, err
	}
	blocks := slack.Blocks{BlockSet: set}
	if atts == nil {
		return blocks, nil, nil
	}
	attachments := make([]slack.Attachment, 0, len(atts))
	for _, a := range atts {
		list, _ := a["blocks"].([]map[string]any)
		inner, err := slackRawBlocks(list)
		if err != nil {
			return slack.Blocks{}, nil, err
		}
		color, _ := a["color"].(string)
		fallback, _ := a["fallback"].(string)
		attachments = append(attachments, slack.Attachment{Color: color, Fallback: fallback, Blocks: slack.Blocks{BlockSet: inner}})
	}
	return blocks, attachments, nil
}

// slackWithoutChoices drops the choice buttons (and an actions block they
// leave empty) from blocks, and appends note, when given, if any were
// dropped. It reports whether any were.
func slackWithoutChoices(blocks []map[string]any, note map[string]any) ([]map[string]any, bool) {
	out := make([]map[string]any, 0, len(blocks)+1)
	dropped := false
	for _, b := range blocks {
		if t, _ := b["type"].(string); t == slackBlockActionsT {
			elems, _ := b["elements"].([]any)
			kept := make([]any, 0, len(elems))
			for _, e := range elems {
				m, _ := e.(map[string]any)
				if id, _ := m["action_id"].(string); strings.HasPrefix(id, slackChoiceActionPrefix) {
					dropped = true
					continue
				}
				kept = append(kept, e)
			}
			if len(kept) == 0 {
				continue
			}
			b["elements"] = kept
		}
		out = append(out, b)
	}
	if dropped && note != nil {
		out = slackWithNote(out, note)
	}
	return out, dropped
}

// slackWithNote adds the answered line as the last block, or just above an
// answer's rating controls (slack_feedback.go), which stay the last block.
func slackWithNote(blocks []map[string]any, note map[string]any) []map[string]any {
	if n := len(blocks); n > 0 {
		if t, _ := blocks[n-1]["type"].(string); t == slackContextActionsBlock {
			return append(blocks[:n-1:n-1], note, blocks[n-1])
		}
	}
	return append(blocks, note)
}
