package gateway

import (
	"strings"
	"testing"
)

// A Slack ask is marked with its kind once the gateway accepts it, with no
// executor heartbeat at all: a turn that answers inside the bridge's 60 s
// progress interval otherwise never shows the arrival reaction.
func TestAnAcceptedAskIsMarkedBeforeAnyHeartbeat(t *testing.T) {
	r := startSlackGateRig(t, []string{"U1"}, false)
	r.adapter.inbox <- InboundMessage{Conversation: "slack:D1", Kind: "dm", AuthorID: "U1", MessageID: "1.0", Text: "why is checkout slow?"}
	r.awaitTask(t, "platform")
	waitFor(t, "the ask's kind on the ask message", func() bool {
		for _, call := range r.adapter.reactionCalls() {
			if strings.HasSuffix(call, " +investigate@1.0") {
				return true
			}
		}
		return false
	})
}
