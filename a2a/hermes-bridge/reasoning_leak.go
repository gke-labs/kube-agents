package hermesbridge

import (
	"fmt"

	"github.com/gke-labs/kube-agents/a2a/lib/answerscrub"
)

// An answer that is the model's own reasoning is withheld, not posted
// (#3206, #3773). Gemini through LiteLLM sometimes writes its reasoning as
// plain assistant content instead of a thought part, so reasoning_content is
// empty, Hermes's tag-based think scrubber passes it through, and the bridge
// took it as the answer. The rule (answerscrub.OpensWithUnstructuredReasoning),
// the token and the notice are one shared implementation for the bridge and
// session paths: the turn ends failed with the token, and the reason carries
// the notice and the session, never the text.
const (
	// unstructuredReasoningToken is the terminal's reason token.
	unstructuredReasoningToken = answerscrub.UnstructuredReasoningToken
	// unstructuredReasoningNotice is what the person reads after the token.
	unstructuredReasoningNotice = answerscrub.UnstructuredReasoningNotice
)

// opensWithUnstructuredReasoning is answerscrub.OpensWithUnstructuredReasoning.
func opensWithUnstructuredReasoning(answer string) bool {
	return answerscrub.OpensWithUnstructuredReasoning(answer)
}

// unstructuredReasoningReason is the terminal message for a withheld answer:
// the token, the notice, and the session when one is known, so an operator
// can find the turn in the profile's store. The answer's text stays out.
func unstructuredReasoningReason(sessionID string, turn int) string {
	reason := fmt.Sprintf("reason: %s - %s", unstructuredReasoningToken, unstructuredReasoningNotice)
	if sessionID != "" {
		reason += "; session: " + sessionID
	}
	return reason + turnNote(turn)
}

// withholdUnstructuredReasoning logs a withheld answer for an operator: the
// task, the session and the answer's size, never its text.
func (b *Bridge) withholdUnstructuredReasoning(run *taskRun, sessionID string, turn int, answer string) {
	b.cfg.Logger.Warn("answer withheld: it opens with a thought line (unstructured model reasoning)",
		"task", run.origin.TaskID, "session", sessionID, "turn", turn, "bytes", len(answer),
		"reason", unstructuredReasoningToken)
}
