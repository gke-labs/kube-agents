package gateway

import (
	"fmt"
	"strings"
	"sync"

	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
	"github.com/gke-labs/kube-agents/a2a/lib/chattext"
)

// A press of a choice on Teams (spec-chatops-gateway.md, "The Teams
// adapter"). The button is an Action.Submit (teams_chat.go), so the press
// arrives as a message whose value is the button's data: the reply the
// choice stands for is the turn's text, exactly as if the presser had typed
// it, and the gateway checks the presser as it checks a typer.
//
// Teams acknowledges a Submit with nothing and leaves the buttons pressable
// for everyone, so the adapter keeps the bookkeeping the Google Chat
// adapter keeps (gchat_clicks.go): a question takes one press at a time
// (waiting), the first answer the gateway verifies stands (answered), and
// the card is then drawn again without its choices and with the line
// "✓ name: choice" under its text. A typed reply in the conversation of an
// open question answers it the same way. An update replaces the whole card
// and a press carries none of it, so a question the adapter no longer
// remembers (posted before a restart) cannot be drawn again: its answer is
// taken, and the line is posted in the conversation instead.

const (
	// teamsLinkJoin joins a markdown link's text to its target, and
	// teamsLinkApart is the same two marks with a space between, which
	// draws as text.
	teamsLinkJoin  = "]("
	teamsLinkApart = "] ("
)

// teamsEditLock orders the edits of one message; holders counts the edits
// holding or waiting on it, under the adapter's lock.
type teamsEditLock struct {
	mu      sync.Mutex
	holders int
}

// lockEdit takes message id's edit lock and returns its release. The
// adapter's lock is held only to find or drop the entry, never while
// waiting or editing.
func (a *TeamsAdapter) lockEdit(id string) (unlock func()) {
	a.mu.Lock()
	if a.editing == nil {
		a.editing = map[string]*teamsEditLock{}
	}
	l := a.editing[id]
	if l == nil {
		l = &teamsEditLock{}
		a.editing[id] = l
	}
	l.holders++
	a.mu.Unlock()
	l.mu.Lock()
	return func() {
		l.mu.Unlock()
		a.mu.Lock()
		if l.holders--; l.holders == 0 {
			delete(a.editing, id)
		}
		a.mu.Unlock()
	}
}

// teamsQuestion is a laid-out message with choices: the conversation it
// was posted in, whether it asks (a choice that is not a next step), and
// its choices' labels.
type teamsQuestion struct {
	conversation string
	asks         bool
	choices      []string
}

// teamsPendingAnswer is a press, or a typed reply to an open question,
// waiting for the gateway's verdict on its sender. asks is false for a
// next step's press, which answers no question.
type teamsPendingAnswer struct {
	message string
	clicker string
	label   string
	click   bool
	asks    bool
}

// teamsUX is the adapter's record of its laid-out messages, under the
// adapter's lock. Each ring holds at most uxCap keys, oldest dropped first.
type teamsUX struct {
	// questions is the messages showing choices, and open the one each
	// conversation's typed reply would answer.
	questions      map[string]teamsQuestion
	questionsOrder []string
	open           map[string]string
	// pending is the answers awaiting a verdict, by the turn's message id;
	// waiting the questions a press is pending on. Both empty out with
	// the verdict (InboundVerified), so neither needs a ring.
	pending map[string]teamsPendingAnswer
	waiting map[string]bool
	// answered is the questions answered, and lines the line each shows,
	// which a later edit keeps.
	answered     map[string]bool
	lines        map[string]string
	answeredRing []string
	// cards is what each laid-out message shows now.
	cards      map[string]teamsChatCard
	cardsOrder []string
}

func (a *TeamsAdapter) uxLocked() *teamsUX {
	if a.ux == nil {
		a.ux = &teamsUX{
			questions: map[string]teamsQuestion{}, open: map[string]string{},
			pending: map[string]teamsPendingAnswer{}, waiting: map[string]bool{},
			answered: map[string]bool{}, lines: map[string]string{},
			cards: map[string]teamsChatCard{},
		}
	}
	return a.ux
}

var _ VerifiedObserver = (*TeamsAdapter)(nil)

// trackQuestion records what message id shows after a post or an edit.
// Choices open it as a question, even one answered before: a card asking
// again is a fresh question, so the earlier answer and its line are
// forgotten. Laying it out without choices, or as text (m nil), closes it,
// so a late press on buttons a client still shows is its turn and writes
// nothing on the message.
func (a *TeamsAdapter) trackQuestion(id, conversation string, m *chatmsg.Message, card *teamsChatCard) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	if card == nil {
		// A message that was a card and is text now keeps a closed record,
		// so a late press on buttons a client still shows writes nothing;
		// a message that never was one (every status line) keeps none.
		if _, was := ux.cards[id]; was {
			ux.cards[id] = teamsChatCard{closed: true}
		}
	} else {
		ux.cards[id] = *card
		// A card pushed out of the ring takes its question with it: the
		// adapter can no longer draw it answered, so it no longer asks.
		remember(&ux.cardsOrder, id, func(k string) {
			delete(ux.cards, k)
			ux.dropQuestionLocked(k)
			forget(&ux.questionsOrder, k)
		})
	}
	ux.forgetPendingLocked(id)
	if m == nil || len(m.Choices) == 0 {
		if _, ok := ux.questions[id]; ok {
			ux.dropQuestionLocked(id)
			forget(&ux.questionsOrder, id)
		}
		return
	}
	labels := make([]string, len(m.Choices))
	for i, c := range m.Choices {
		labels[i] = c.Text
	}
	q := teamsQuestion{conversation: conversation, asks: asksQuestion(m.Choices), choices: labels}
	ux.questions[id] = q
	remember(&ux.questionsOrder, id, func(k string) { ux.dropQuestionLocked(k) })
	switch {
	case q.asks:
		ux.open[conversation] = id
	case ux.open[conversation] == id:
		delete(ux.open, conversation)
	}
	delete(ux.answered, id)
	delete(ux.lines, id)
	forget(&ux.answeredRing, id)
}

// forgetAnswer drops the answered line message id carries, for a question
// asked again on it.
func (a *TeamsAdapter) forgetAnswer(id string) {
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	delete(ux.answered, id)
	delete(ux.lines, id)
	forget(&ux.answeredRing, id)
}

// dropQuestionLocked forgets a question, and the open slot that names it.
func (ux *teamsUX) dropQuestionLocked(id string) {
	if q, ok := ux.questions[id]; ok && ux.open[q.conversation] == id {
		delete(ux.open, q.conversation)
	}
	delete(ux.questions, id)
}

// forgetPendingLocked drops the answers waiting on a message that was just
// drawn again: the gateway's verdict on them would otherwise write the old
// question's answer over the new card. The turn they started is the
// gateway's as before.
func (ux *teamsUX) forgetPendingLocked(id string) {
	for turn, p := range ux.pending {
		if p.message == id {
			delete(ux.pending, turn)
		}
	}
	delete(ux.waiting, id)
}

// withAnsweredLine is text with the answered line of message id under it,
// when it has one.
func (a *TeamsAdapter) withAnsweredLine(id, text string) string {
	if id == "" {
		return text
	}
	a.mu.Lock()
	line := a.uxLocked().lines[id]
	a.mu.Unlock()
	return teamsWithLine(text, line)
}

// teamsWithLine is text with line as its last section.
func teamsWithLine(text, line string) string {
	if line == "" {
		return text
	}
	text = strings.TrimRight(text, "\n")
	if text == "" {
		return line
	}
	return text + teamsChatSectionGap + line
}

// noteTypedAnswer reads a typed message against the conversation's open
// question: when one is open and the text is not a command, the message is
// a pending answer to it, settled when the gateway says whether the sender
// is verified. It claims nothing, so a press meanwhile is still a turn.
func (a *TeamsAdapter) noteTypedAnswer(conversation, turn, text, name string) {
	if chattext.IsCommand(text) {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	ux := a.uxLocked()
	id := ux.open[conversation]
	if _, ok := ux.questions[id]; id == "" || !ok {
		return
	}
	ux.pending[turn] = teamsPendingAnswer{message: id, clicker: teamsClicker(name), label: teamsLineWords(text), asks: true}
}

// teamsClicker is the name an answered line shows for name.
func teamsClicker(name string) string {
	if name = teamsLineWords(name); name != "" {
		return name
	}
	return namelessClicker
}

// choicePress normalizes the press of a choice to the turn its reply
// stands for, or names why it is not one.
func (a *TeamsAdapter) choicePress(act *teamsActivity, press teamsPress, conversation, kind string) (InboundMessage, string) {
	reply := strings.TrimSpace(press.Reply)
	if reply == "" {
		return InboundMessage{}, "choice press carries no reply"
	}
	pressed := strings.TrimSpace(act.ReplyToID)
	if pressed == "" {
		return InboundMessage{}, "choice press names no message"
	}
	author, dup := a.admit(act, conversation)
	if dup {
		return InboundMessage{}, "duplicate delivery of " + act.ID
	}
	turn := gchatClickID(pressed, act.ID)
	a.mu.Lock()
	ux := a.uxLocked()
	if ux.answered[pressed] {
		a.mu.Unlock()
		return InboundMessage{}, "the question is already answered (" + pressed + ")"
	}
	if ux.waiting[pressed] {
		a.mu.Unlock()
		return InboundMessage{}, "a press on the question is already waiting (" + pressed + ")"
	}
	turnOf := InboundMessage{
		Conversation: teamsConversationID(conversation),
		Kind:         kind,
		AuthorID:     author,
		MessageID:    turn,
		Text:         reply,
		Click:        true,
	}
	label := press.Label
	q, asked := ux.questions[pressed]
	_, drawn := ux.cards[pressed]
	switch {
	case asked && q.conversation != turnOf.Conversation:
		// The press names a question of another conversation: its card
		// could not be drawn again from here, and it would stay answered
		// with nothing to show for it.
		a.mu.Unlock()
		return InboundMessage{}, "choice press names a question of another conversation"
	case asked && (press.Choice == nil || *press.Choice < 0 || *press.Choice >= len(q.choices)):
		a.mu.Unlock()
		return InboundMessage{}, "choice press names no choice of its question"
	case asked:
		label = q.choices[*press.Choice]
	case drawn:
		// The message was drawn again without choices: a late press on
		// buttons a client still shows is its turn, and writes nothing
		// on the card.
		a.mu.Unlock()
		return turnOf, ""
	}
	if strings.TrimSpace(label) == "" {
		label = reply
	}
	ux.pending[turn] = teamsPendingAnswer{message: pressed, clicker: teamsClicker(act.From.Name), label: teamsLineWords(label), click: true, asks: !press.Next}
	ux.waiting[pressed] = true
	a.mu.Unlock()
	return turnOf, ""
}

// teamsLineWords is words as the answered line shows them: one line, cut
// as a typed answer is, and with a markdown link taken apart so the line
// draws no link of the answerer's making.
func teamsLineWords(words string) string {
	return strings.ReplaceAll(slackTypedWords(words), teamsLinkJoin, teamsLinkApart)
}

// InboundVerified learns whether a press, or a typed reply to an open
// question, came from a sender the gateway serves (VerifiedObserver). The
// first verified answer stands: the question is drawn again without its
// choices and with the answered line, off the pull loop.
func (a *TeamsAdapter) InboundVerified(conversation, messageID string, verified bool) {
	a.mu.Lock()
	ux := a.uxLocked()
	p, ok := ux.pending[messageID]
	delete(ux.pending, messageID)
	if !ok {
		a.mu.Unlock()
		return
	}
	if p.click {
		delete(ux.waiting, p.message)
	}
	if !verified || ux.answered[p.message] {
		a.mu.Unlock()
		return
	}
	ux.answered[p.message] = true
	remember(&ux.answeredRing, p.message, func(k string) { delete(ux.answered, k); delete(ux.lines, k) })
	ux.dropQuestionLocked(p.message)
	forget(&ux.questionsOrder, p.message)
	a.mu.Unlock()
	line := fmt.Sprintf(answeredLine, p.clicker, p.label)
	a.rewrites.Add(1)
	go func() {
		defer a.rewrites.Done()
		a.rewriteAnswered(conversation, p.message, line, p.asks)
	}()
}

// rewriteAnswered draws an answered question again: its text with line
// under it, its choices gone, its other buttons kept. A question the
// adapter has no card for, one whose card would no longer fit, or one the
// edit fails on, gets the line as a post in its conversation instead, so
// the answer is on record where the question is; a next step's press
// (asks false) answered no question, so nothing is posted for it.
func (a *TeamsAdapter) rewriteAnswered(conversation, id, line string, asks bool) {
	defer a.lockEdit(id)()
	a.mu.Lock()
	ux := a.uxLocked()
	card, held := ux.cards[id]
	stands := ux.answered[id]
	a.mu.Unlock()
	if !stands {
		// The message asked again while this redraw waited: the new
		// question keeps its choices, and the old answer shows nowhere.
		return
	}
	if held && !card.closed {
		next := teamsChatCard{text: teamsWithLine(card.text, line), rest: card.rest}
		if teamsFitsCard(next.text, next.rest) {
			err := a.edit(conversation, id, next.text, next.rest)
			if err == nil {
				a.mu.Lock()
				ux := a.uxLocked()
				if ux.answered[id] {
					ux.lines[id] = line
				}
				if _, still := ux.cards[id]; still {
					ux.cards[id] = next
				}
				a.mu.Unlock()
				return
			}
			a.log.Warn("teams: drawing an answered question again failed; posting the answer in its conversation", "conversation", conversation, "err", err)
		}
		// The card still shows its choices; its record does not, so a
		// later redraw (a rating's) takes them off.
		a.mu.Lock()
		if now, still := a.uxLocked().cards[id]; still {
			now.choices = nil
			a.uxLocked().cards[id] = now
		}
		a.mu.Unlock()
	}
	if !asks {
		return
	}
	if _, err := a.Post(conversation, line); err != nil {
		a.log.Warn("teams: posting an answered line failed", "conversation", conversation, "err", err)
	}
}
