package gateway

import (
	"strings"

	"github.com/slack-go/slack/slackevents"
)

// Slack's native Stop (docs/designs/slack-stop.md, step 1). The Stop button on
// an Agent Session's Working… line sends agent_session_stopped, which an app
// receives once its manifest subscribes the event. The adapter turns it into
// the stop a typed `stop` is: a cancel that names no task, so the gateway
// cancels the running turn and, armed, stops the thread's cards
// (cardStopWanted). It reaches the gateway's sender check and the requester
// gate (mayStop) like any message.
// The event carries no channel_type, so a DM thread is told from a channel
// thread by its channel id (slackDMChannelPrefix).
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
