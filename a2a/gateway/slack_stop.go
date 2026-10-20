package gateway

import (
	"context"
	"fmt"
	"net/url"
	"regexp"
	"strings"

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
)

// The Slack Stop's replies, the maintainer's approved wording, one per stop.
const (
	slackStopStoppedReply = "Stopped."
	// slackStopPRReplyFormat takes the pull request as a markdown link,
	// written "#<number>".
	slackStopPRReplyFormat = "Stopped. I'd already opened a pull request before you stopped me: %s."
	// slackStopExceptReplyFormat takes the one card that did not stop.
	slackStopExceptReplyFormat = "Stopped, except one task that didn't respond: %s."
	slackStopNothingReply      = "Nothing was running."
	slackStopCouldntReply      = "I couldn't stop the work in this thread. Press Stop again."
)

// slackStopPRPathRE is the path of a pull request (GitHub) or a merge request
// (GitLab), with its number. A URL of any other shape is not linked.
var slackStopPRPathRE = regexp.MustCompile(`/(?:pull|-/merge_requests|merge_requests)/([0-9]+)/?$`)

const (
	// slackStopPRScheme is the only scheme a reported pull request may have.
	slackStopPRScheme = "https"
	// slackStopLinkBreakers are the characters that would end or reopen the
	// markdown link, or a Slack link, early; a URL holding one is not linked.
	slackStopLinkBreakers = "()[]<>| \t\n"
)

// isSlackStop reports a stop from the Slack Stop button rather than the word:
// the Slack adapter sets IntentCancel only on agent_session_stopped.
func isSlackStop(msg InboundMessage, backend string) bool {
	return backend == slackBackend && msg.Intent == IntentCancel
}

// slackStop is the Stop button's stop with the card stop armed and on its
// route (cardStopWanted): the running turn is cancelled quietly, then the
// thread's cards are stopped by a control that speaks with the Slack Stop's
// replies. A second press while that control runs adds nothing: the first
// one's reply is still coming.
func (g *Gateway) slackStop(ctx context.Context, rec *SessionRecord, authority Authority, backend string) {
	if rec.CardStop != nil {
		g.log.Info("slack stop: a card stop is already running; nothing added", "conversation", rec.Key)
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

// slackStopReply words a completed control's result as the Slack Stop's one
// reply. Anything the stop could not promise is the couldn't-stop reply,
// except exactly one card that did not stop, which is named; a stop that
// cancelled no turn and stopped no card says nothing was running; a stop that
// stopped something names the first pull request the thread's cards opened,
// when the executor reports one. Titles are cleaned as the card stop's are
// (cardStopTitle), so a title can mention no one and link nowhere.
func slackStopReply(r *lib.CardStopResult, turnCancelled bool) string {
	unpromised := r.Failed + r.Pending + r.Unresolved
	switch {
	case unpromised == 1 && len(r.NotStopped) == 1 && !r.Deadline && !r.Truncated:
		return fmt.Sprintf(slackStopExceptReplyFormat, cardStopTitle(r.NotStopped[0].Title))
	case unpromised > 0 || r.Deadline || r.Truncated:
		return slackStopCouldntReply
	case !turnCancelled && len(r.Stopped) == 0:
		return slackStopNothingReply
	}
	for _, u := range r.PullRequests {
		if link, ok := slackStopPRLink(u); ok {
			return fmt.Sprintf(slackStopPRReplyFormat, link)
		}
	}
	return slackStopStoppedReply
}

// slackStopPRLink is raw as a markdown link "[#<number>](<url>)", when it is
// an https pull or merge request URL with no user info, query or fragment,
// which toMrkdwn turns into a Slack link; anything else is not linked.
func slackStopPRLink(raw string) (string, bool) {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != slackStopPRScheme || u.Host == "" || u.User != nil ||
		u.RawQuery != "" || u.Fragment != "" {
		return "", false
	}
	m := slackStopPRPathRE.FindStringSubmatch(u.EscapedPath())
	s := u.String()
	if m == nil || strings.ContainsAny(s, slackStopLinkBreakers) {
		return "", false
	}
	return "[#" + m[1] + "](" + s + ")", true
}

// inboundStop normalizes one agent_session_stopped event into a stop, or
// reports it not-a-turn: a Stop with no user, channel, thread or timestamp,
// one pressed by the bot itself, and a redelivery. The conversation is the
// thread the session lives in. No foreign-workspace check: the event names
// no team, and Slack sends it only for this app's own session, which a
// member of another workspace cannot press Stop on unless Slack shows them
// the thread; the gateway's sender check holds either way.
func (s *SlackAdapter) inboundStop(e *slackevents.AgentSessionStoppedEvent) (InboundMessage, bool) {
	if e.User == "" || e.User == s.botUserID || e.Channel == "" || e.ThreadTimestamp == "" ||
		e.EventTimestamp == "" {
		return InboundMessage{}, false
	}
	if s.alreadySeen(slackStopSeenPrefix + e.Channel + "/" + e.EventTimestamp) {
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
