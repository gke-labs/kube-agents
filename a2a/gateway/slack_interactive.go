package gateway

import (
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/slack-go/slack"

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

	// slackAnsweredLine is the line an answer leaves on its question.
	slackAnsweredLine = "✓ %s: %s"
	// slackNamelessClicker stands in for a clicker Slack names nowhere:
	// never their raw id.
	slackNamelessClicker = "Someone"
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

	// slackUXCap bounds each of the click and question maps, oldest first.
	slackUXCap = 512

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
		ActionID string `json:"action_id"`
		Value    string `json:"value"`
		Text     struct {
			Text string `json:"text"`
		} `json:"text"`
	} `json:"actions"`
	ActionTS string `json:"action_ts"`
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
// click, with the question it answered and the label it showed, or a typed
// message, which answers the conversation's open question and may title the
// thread.
type slackPending struct {
	conversation, author, text string
	click                      bool
	label                      string
	question                   slackQuestion
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
}

// uxLocked is s.ux, made on first use. Caller holds s.mu.
func (s *SlackAdapter) uxLocked() *slackUX {
	if s.ux == nil {
		s.ux = &slackUX{pending: map[string]slackPending{}, claims: map[string]bool{},
			answered: map[string]bool{}, lines: map[string]string{}, questions: map[string]slackQuestion{}}
	}
	return s.ux
}

// remember adds key to a bounded ring, evicting the oldest past the cap with
// drop.
//
// A key keeps one slot: one already in the ring moves to the newest end
// rather than gaining a second slot, whose older twin's eviction would
// otherwise drop the live entry (a map entry deleted without retiring its
// slot, then added again). The rings hold at most slackUXCap keys, so the
// scan is bounded.
func remember(order *[]string, key string, drop func(string)) {
	forget(order, key)
	*order = append(*order, key)
	if len(*order) > slackUXCap {
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
// settled on the notify route by its update key) closes it. Called once
// Slack has the message.
func (s *SlackAdapter) trackQuestion(conversation, ts string, m OutMessage) {
	channel, thread, _ := slackChannelThread(conversation)
	if m.Chat != nil && m.Chat.Plan != nil {
		// A card moved: the Home tab's running list reads the board again.
		s.homeChanged(true)
	}
	if m.Chat == nil || len(m.Chat.Choices) == 0 {
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
		slackPending{conversation: msg.Conversation, author: msg.AuthorID, text: msg.Text})
}

func (s *SlackAdapter) addPendingLocked(key string, p slackPending) {
	ux := s.uxLocked()
	if _, ok := ux.pending[key]; !ok {
		remember(&ux.pendingOrder, key, func(k string) {
			if old, ok := ux.pending[k]; ok && old.click {
				delete(ux.claims, old.question.channel+"/"+old.question.ts)
			}
			delete(ux.pending, k)
		})
	}
	ux.pending[key] = p
}

// interactive turns one interactive envelope's payload into a turn, or
// reports it is not one: a click on a choice button, from a member of the
// bot's own workspace, on a question not already answered.
func (s *SlackAdapter) interactive(raw json.RawMessage) (InboundMessage, bool) {
	var p slackClickPayload
	if err := json.Unmarshal(raw, &p); err != nil {
		s.log.Warn("slack: unreadable interactive payload", "err", err)
		return InboundMessage{}, false
	}
	if p.Type != slackBlockActions || len(p.Actions) == 0 {
		return InboundMessage{}, false
	}
	action := p.Actions[0]
	if !strings.HasPrefix(action.ActionID, slackChoiceActionPrefix) {
		// A link button (Slack opened the url) or a button not ours.
		return InboundMessage{}, false
	}
	if p.User.ID == "" || p.Channel.ID == "" || p.Message.TS == "" || p.ActionTS == "" {
		return InboundMessage{}, false
	}
	if s.otherWorkspace(p.User.TeamID) || s.otherWorkspace(p.Team.ID) {
		s.log.Info("slack: ignoring a click from another workspace's member",
			"userTeam", p.User.TeamID, "team", s.teamID, "channel", p.Channel.ID)
		return InboundMessage{}, false
	}
	if s.alreadySeen("click:" + p.Channel.ID + "/" + p.ActionTS) {
		return InboundMessage{}, false
	}
	thread := p.Message.ThreadTS
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
	conversation := slackConversationID(channelType, p.Channel.ID, thread)
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
		AuthorID:     p.User.ID,
		MessageID:    p.ActionTS,
		Text:         text,
		Click:        true,
	}, true
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
	// Slack's entities as the line shows them: a mention dropped (the line
	// is plain text, and a name needs a lookup), a channel by its name, a
	// link by its label, or its url when it has none.
	text = slackTitleMention.ReplaceAllString(text, " ")
	text = slackTitleChannel.ReplaceAllString(text, "$1")
	text = slackTitleLink.ReplaceAllStringFunc(text, func(m string) string {
		sub := slackTitleLink.FindStringSubmatch(m)
		if sub[2] != "" {
			return sub[2]
		}
		return sub[1]
	})
	words := strings.Join(strings.Fields(text), " ")
	if utf8.RuneCountInString(words) <= slackTypedAnswerMax {
		return words
	}
	r := []rune(words)
	return strings.TrimRight(string(r[:slackTypedAnswerMax-utf8.RuneCountInString(slackClipEllipsis)]), " ") + slackClipEllipsis
}

// slackUserName is how a user is named on an answered line: display name,
// then real name, then handle, never a mention or an id.
func (s *SlackAdapter) slackUserName(userID string) string {
	u, err := s.api.GetUserInfo(userID)
	if err != nil || u == nil {
		if err != nil {
			s.log.Debug("slack: users.info failed; naming the answer generically", "user", userID, "err", err)
		}
		return slackNamelessClicker
	}
	for _, name := range []string{u.Profile.DisplayName, u.RealName, u.Profile.RealName, u.Name} {
		if name = strings.TrimSpace(name); name != "" {
			return name
		}
	}
	return slackNamelessClicker
}

// rewriteAnswered rewrites a question as answered by userID with label. If
// Slack refuses the rewrite, the line is posted in the question's thread
// instead, so an answer always shows once.
func (s *SlackAdapter) rewriteAnswered(q slackQuestion, userID, label string) {
	line := fmt.Sprintf(slackAnsweredLine, s.slackUserName(userID), label)
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
			list = append(list, note)
			placed = true
		}
		a["blocks"] = list
	}
	if !placed {
		top = append(top, note)
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
		out = append(out, note)
	}
	return out, dropped
}
