package gateway

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/slack-go/slack/slackevents"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Slack's native Stop (spec-chatops-gateway.md, "The Slack adapter"). The Stop button on
// an Agent Session's Working… line sends agent_session_stopped, which an app
// receives once its manifest subscribes the event. The adapter turns it into
// a cancel that names no task, as a typed `stop` is, with IntentCancel, which
// no chat message carries, so the gateway can tell the button from the word
// (isSlackStop). Only a requester of the thread may press it (mayStop). With
// the card stop armed and on its route, the button cancels the running turn
// and stops the thread's cards through the same control a typed `stop` sends
// (stopCardsAs), and answers with one reply of its own (slackStopReply); with
// it unarmed, the button is a typed `stop` in every way. The event carries no
// channel_type, so a DM thread is told from a channel thread by its channel
// id (slackDMChannelPrefix).
const (
	// slackStopSeenPrefix keeps a Stop's redelivery key apart from a
	// message's (channel/ts), so an event_ts equal to a message ts in the
	// same channel is not taken for a redelivery of it.
	slackStopSeenPrefix = "stop:"
	// slackKindDM and slackKindGroup are InboundMessage.Kind for a DM and a
	// channel thread, as inboundEvent sets them.
	slackKindDM    = "dm"
	slackKindGroup = "group"
	// slackStopPresserTTL is how long a presser's workspace answer is cached.
	slackStopPresserTTL = 5 * time.Minute
)

// The Slack Stop's replies, the maintainer's approved wording, one per stop.
// The approved pull request line ("Stopped. I'd already opened a pull request
// before you stopped me: #412.") waits for a card-stop result that names the
// pull requests the stopped cards opened; none does yet.
const (
	slackStopStoppedReply = "Stopped."
	// slackStopExceptReplyFormat takes the one card that did not stop.
	slackStopExceptReplyFormat = "Stopped, except one task that didn't respond: %s."
	slackStopNothingReply      = "Nothing was running."
	slackStopCouldntReply      = "I couldn't stop the work in this thread. Press Stop again."
	// slackStopCouldntFinalReply is the same reply without "Press Stop
	// again", for a failure a second press would only repeat.
	slackStopCouldntFinalReply = "I couldn't stop the work in this thread."
	// slackStopNewerTurnReply answers a stop that found nothing to stop while
	// a newer question is running (approved 2026-10-22).
	slackStopNewerTurnReply = "No tasks were running yet. I'm still answering your latest question."
)

// isSlackStop reports a stop from the Slack Stop button rather than the word:
// the Slack adapter sets IntentCancel only on agent_session_stopped.
func isSlackStop(msg InboundMessage, backend string) bool {
	return backend == slackBackend && msg.Intent == IntentCancel
}

// slackStop is the Stop button's stop with the card stop armed and on its
// route (cardStopWanted): a running turn is cancelled quietly, then the
// thread's cards are stopped by a control that speaks with the Slack Stop's
// replies. With a control already in flight, a press that cancelled a newer
// turn marks it for one follow-up, which answers in the Slack Stop's voice,
// since that turn may have filed cards the control has passed; a press that
// cancelled nothing adds nothing, because the first press's reply is coming.
func (g *Gateway) slackStop(ctx context.Context, rec *SessionRecord, authority Authority, backend string) {
	// A thread that has run nothing has no cards to stop: answered without a
	// control, as a typed `stop` is.
	if rec.ActiveTask == nil && len(rec.Tasks) == 0 && rec.CardStop == nil {
		g.post(rec.Key, slackStopNothingReply)
		return
	}
	turn := false
	if active := rec.ActiveTask; active != nil && !active.Detached {
		if !g.cancelTaskVoiced(ctx, rec, authority, true) {
			g.post(rec.Key, slackStopCouldntReply)
			return
		}
		turn = true
	}
	if rec.CardStop != nil {
		if !turn {
			g.log.Info("slack stop: a card stop is already running; nothing added", "conversation", rec.Key)
			return
		}
		markCardStopAgain(rec, authority)
		rec.CardStop.AgainSlackStop = true
		return
	}
	g.stopCardsAs(ctx, rec, authority, backend, false, CardStopRun{SlackStop: true, TurnCancelled: turn})
}

// unconfirmed is the line a control posts when its stop cannot be confirmed:
// the Slack Stop's own for a control the button started, the card stop's
// notice otherwise.
func (run CardStopRun) unconfirmed() string {
	if run.SlackStop {
		return slackStopCouldntReply
	}
	return cardStopUnconfirmedNotice
}

// unconfirmedFinal is unconfirmed for a failure a second press would only
// repeat: the control could not be built or minted, the executor rejected it
// (a bridge older than the control), or it completed with no result part.
// The Slack Stop's reply then drops "Press Stop again"; the card stop's
// notice is the same either way.
func (run CardStopRun) unconfirmedFinal() string {
	if run.SlackStop {
		return slackStopCouldntFinalReply
	}
	return cardStopUnconfirmedNotice
}

// slackStopReply words a completed control's result as the Slack Stop's one
// reply. A stop that cancelled no turn and stopped no card says nothing was
// running, or, with a newer question running by then (newerTurnRunning),
// that no tasks were running yet; unless something it found would not stop.
// Anything the stop could not promise is the couldn't-stop reply, except
// exactly one card that did not stop beside work that did, which is named.
// Titles are cleaned as the card stop's are (cardStopTitle), so a title can
// mention no one and link nowhere.
func slackStopReply(r *lib.CardStopResult, turnCancelled, newerTurnRunning bool) string {
	unpromised := r.Failed + r.Pending + r.Unresolved
	stoppedSome := turnCancelled || len(r.Stopped) > 0
	switch {
	case unpromised == 1 && len(r.NotStopped) == 1 && stoppedSome && !r.Deadline && !r.Truncated:
		return fmt.Sprintf(slackStopExceptReplyFormat, cardStopTitle(r.NotStopped[0].Title))
	case unpromised > 0 || r.Deadline || r.Truncated:
		return slackStopCouldntReply
	case !stoppedSome && newerTurnRunning:
		return slackStopNewerTurnReply
	case !stoppedSome:
		return slackStopNothingReply
	}
	return slackStopStoppedReply
}

// slackPresser is one cached workspace answer for a Stop presser.
type slackPresser struct {
	inWorkspace bool
	at          time.Time
}

// presserInWorkspace reports whether user may press Stop as far as their
// workspace goes. The event names no team for the presser, and a Slack
// Connect member of another workspace can see a shared thread, so the
// message path's foreign-sender check (which reads the message's own team
// fields) has nothing to read here. A member the allowed-users list names
// passes on the install's word, as on the Home tab (homeAdmitted). Anyone
// else is looked up with users.info and must be in this workspace; a failed
// lookup, or a workspace auth.test never named, refuses: closed for a
// foreign presser, apart from the who-asked rule (stopgate.go). A decided
// answer is cached for slackStopPresserTTL, so a press costs at most one
// call; the lookup runs on the event pump, bounded by the adapter's HTTP
// client, and Stop presses are rare.
func (s *SlackAdapter) presserInWorkspace(user string) bool {
	s.home.mu.Lock()
	admits := s.home.admits
	s.home.mu.Unlock()
	if admits != nil {
		if ok, byAllowAll := admits(user); ok && !byAllowAll {
			return true
		}
	}
	now := s.now()
	s.mu.Lock()
	if p, ok := s.pressers[user]; ok && now.Sub(p.at) < slackStopPresserTTL {
		s.mu.Unlock()
		return p.inWorkspace
	}
	team := s.teamID
	s.mu.Unlock()
	if team == "" {
		s.log.Warn("slack: refusing a Stop press: auth.test named no workspace to check the presser against", "user", user)
		return false
	}
	u, err := s.api.GetUserInfo(user)
	if err != nil {
		s.log.Warn("slack: refusing a Stop press: the presser's workspace could not be looked up", "user", user, "err", err)
		return false
	}
	in := u.TeamID == team
	s.mu.Lock()
	if s.pressers == nil || len(s.pressers) >= slackSessionsCap {
		s.pressers = map[string]slackPresser{}
	}
	s.pressers[user] = slackPresser{inWorkspace: in, at: now}
	s.mu.Unlock()
	return in
}

// inboundStop normalizes one agent_session_stopped event into a stop, or
// reports it not-a-turn: a Stop with no user, channel, thread or timestamp,
// one pressed by the bot itself, a redelivery, and one from a presser outside
// this workspace (presserInWorkspace). The conversation is the thread the
// session lives in.
func (s *SlackAdapter) inboundStop(e *slackevents.AgentSessionStoppedEvent) (InboundMessage, bool) {
	if e.User == "" || e.User == s.botUserID || e.Channel == "" || e.ThreadTimestamp == "" ||
		e.EventTimestamp == "" {
		return InboundMessage{}, false
	}
	if s.alreadySeen(slackStopSeenPrefix + e.Channel + "/" + e.EventTimestamp) {
		return InboundMessage{}, false
	}
	if !s.presserInWorkspace(e.User) {
		s.log.Info("slack: ignoring a Stop press from outside this workspace", "user", e.User, "channel", e.Channel)
		return InboundMessage{}, false
	}
	channelType, kind := "", slackKindGroup
	if strings.HasPrefix(e.Channel, slackDMChannelPrefix) {
		channelType, kind = slackChannelTypeIM, slackKindDM
	}
	return InboundMessage{
		Conversation: slackConversationID(channelType, e.Channel, e.ThreadTimestamp),
		Kind:         kind,
		AuthorID:     e.User,
		MessageID:    e.EventTimestamp,
		Intent:       IntentCancel,
	}, true
}
