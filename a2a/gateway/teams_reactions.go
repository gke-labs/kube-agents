package gateway

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

// teamsReactionsSuffix is the path under one activity that holds the bot's
// own reactions on it: a PUT of a reaction id adds that reaction and a
// DELETE takes it off, neither with a body (the broker's passthrough admits
// the two on this path only: credential_proxy.py, TEAMS_REACTION_PATH_RE).
const teamsReactionsSuffix = "/reactions/"

// teamsMarkReaction is the Teams reaction each mark token shows: the same
// emoji as on Slack (slackMarkEmoji), under the ids Teams lists in its
// reactions reference. The ids are a lookup table, not derivable from the
// glyph. `done` is absent on purpose, as on Slack; a token not listed shows
// nothing.
var teamsMarkReaction = map[string]string{
	"question":    "1f440_eyes",                     // 👀
	"investigate": "1f50d_magnifiertiltedleft",      // 🔍
	"fleet":       "1f310_globewithmeridians",       // 🌐
	"upgrade":     "2b06_upwardsblackarrow",         // ⬆️
	"cost":        "1f4b0_moneybag",                 // 💰
	"security":    "1f6e1_shield",                   // 🛡️
	"change":      "1f6e0_hammerandwrench",          // 🛠️
	"board":       "1f4cb_clipboard",                // 📋
	"incident":    "1f6a8_policecarsrevolvinglight", // 🚨
	"blocked":     "23f8_doubleverticalbar",         // ⏸️
	"failed":      "274c_crossmark",                 // ❌
}

// SetMessageMarks turns the marks on a person's message on or off
// (Config.TeamsMessageMarks). Off, neither React nor RemoveReaction calls
// anything: the gateway records a mark whether or not the adapter drew it,
// so a removal it asks for is no sign a reaction is there. A mark drawn
// before a restart that turned the switch off stays on its message.
func (a *TeamsAdapter) SetMessageMarks(on bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.messageMarks = on
}

// React puts the token's reaction on a message in the conversation
// (Adapter.React). A token with no reaction, a message with no id, or marks
// turned off is a nil no-op. Adding a reaction already there is no error on
// Teams's side.
func (a *TeamsAdapter) React(conversation, messageID, token string) error {
	if !a.marksOn() {
		return nil
	}
	return a.reaction(conversation, messageID, token, http.MethodPut)
}

// RemoveReaction takes the bot's own reaction for token off the message
// (Adapter.RemoveReaction); a nil no-op with the marks turned off. Teams
// lets a bot remove only its own, and removing one that is not there is no
// error.
func (a *TeamsAdapter) RemoveReaction(conversation, messageID, token string) error {
	if !a.marksOn() {
		return nil
	}
	return a.reaction(conversation, messageID, token, http.MethodDelete)
}

func (a *TeamsAdapter) reaction(conversation, messageID, token, httpMethod string) error {
	reaction, ok := teamsMarkReaction[token]
	messageID = strings.TrimSpace(messageID)
	if !ok || messageID == "" {
		return nil
	}
	convID := teamsConversationFromID(conversation)
	if convID == "" {
		return fmt.Errorf("teams: malformed conversation id %q", conversation)
	}
	endpoint := teamsConversationsPath + convID + teamsActivitiesSuffix + "/" + messageID + teamsReactionsSuffix + reaction
	payload, err := json.Marshal(map[string]any{
		"serviceUrl": a.serviceURLFor(convID),
		"endpoint":   endpoint,
		"method":     httpMethod,
	})
	if err != nil {
		return fmt.Errorf("teams: encoding reaction call: %w", err)
	}
	_, err = a.apiPost(context.Background(), httpMethod, endpoint, payload)
	return err
}

func (a *TeamsAdapter) marksOn() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.messageMarks
}
