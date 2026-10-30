package gateway

import (
	"fmt"
	"strings"

	"github.com/slack-go/slack"
)

// slackMarkEmoji is the emoji each mark token shows on Slack: the ask kinds
// and the two settles that leave a reaction, with today's emoji names
// (slack_presenter.REACTION_*; reactions.add takes the name, not the glyph).
// `done` is absent on purpose: work that succeeded leaves no reaction, and
// the answer in the thread says it. A token not listed shows nothing
// (spec-a2a-payloads.md, "mark": an ask the adapter has no emoji for).
var slackMarkEmoji = map[string]string{
	"question":    "eyes",                 // 👀
	"investigate": "mag",                  // 🔍
	"fleet":       "globe_with_meridians", // 🌐
	"upgrade":     "arrow_up",             // ⬆️
	"cost":        "moneybag",             // 💰
	"security":    "shield",               // 🛡️
	"change":      "hammer_and_wrench",    // 🛠️
	"board":       "clipboard",            // 📋
	"incident":    "rotating_light",       // 🚨
	"blocked":     "double_vertical_bar",  // ⏸️
	"failed":      "x",                    // ❌
}

// The Web API errors that mean the reaction is already as asked: adding one
// the bot already put on, or removing one that is not there (or that someone
// else put on, which reactions.remove never touches: it removes the caller's
// own reaction only, which is the adapter's removal rule for free).
const (
	slackAlreadyReacted = "already_reacted"
	slackNoReaction     = "no_reaction"
)

// React puts the token's emoji on a message in the conversation's channel
// (Adapter.React). A token with no emoji, or a reaction already there, is a
// nil no-op.
func (s *SlackAdapter) React(conversation, messageID, token string) error {
	return s.reaction(conversation, messageID, token, true)
}

// RemoveReaction takes the bot's own reaction for token off the message
// (Adapter.RemoveReaction); reactions.remove cannot reach anyone else's.
func (s *SlackAdapter) RemoveReaction(conversation, messageID, token string) error {
	return s.reaction(conversation, messageID, token, false)
}

func (s *SlackAdapter) reaction(conversation, messageID, token string, add bool) error {
	emoji, ok := slackMarkEmoji[token]
	if !ok || messageID == "" {
		return nil
	}
	channel, _, ok := slackChannelThread(conversation)
	if !ok {
		return fmt.Errorf("malformed conversation id %q", conversation)
	}
	item := slack.NewRefToMessage(channel, messageID)
	var err error
	if add {
		err = s.api.AddReaction(emoji, item)
	} else {
		err = s.api.RemoveReaction(emoji, item)
	}
	if err != nil && (strings.Contains(err.Error(), slackAlreadyReacted) || strings.Contains(err.Error(), slackNoReaction)) {
		return nil
	}
	return err
}
