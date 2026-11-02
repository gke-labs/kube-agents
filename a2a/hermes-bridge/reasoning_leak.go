package hermesbridge

import (
	"fmt"
	"strings"
)

// An answer that is the model's own reasoning is withheld, not posted
// (#3206, #3773). Gemini through LiteLLM sometimes writes its reasoning as
// plain assistant content instead of a thought part, so reasoning_content is
// empty, Hermes's tag-based think scrubber passes it through, and the bridge
// took it as the answer. No reliable boundary marks where the reasoning ends
// and an answer begins, so the bridge does not try to cut one out: the turn
// ends failed with unstructuredReasoningToken, and the reason carries the
// notice and the session, never the text. The image's
// unstructured_reasoning_replay patch keeps such a reply out of the history
// Hermes sends the model on later calls, which is what made it repeat; its
// copy of the rule below is deploy/docker/patches/unstructured_reasoning.py,
// and the two must agree.
const (
	// unstructuredReasoningToken is the terminal's reason token.
	unstructuredReasoningToken = "hermes-unstructured-reasoning"
	// unstructuredReasoningWord and unstructuredReasoningSeparator spell
	// the rule opensWithUnstructuredReasoning applies.
	unstructuredReasoningWord      = "thought"
	unstructuredReasoningSeparator = ":"
	// unstructuredReasoningNotice is what the person reads after the token.
	// The check runs on the finished answer, after every tool call the turn
	// made, so it says so: asking again repeats whatever the turn did.
	unstructuredReasoningNotice = "the agent's reply was withheld because it contained unstructured model reasoning instead of an answer; " +
		"anything the agent did during the turn has already happened, so check that before asking again"
)

// opensWithUnstructuredReasoning is the rule. Three shapes have been seen: a
// first line `thought`, a first line `thought:`, and `thought:` with the
// reasoning on the same line ("thought:The user asked..."). So it reports
// whether answer's first non-empty line, with its surrounding whitespace
// dropped and compared case-insensitively, is exactly `thought` or starts with
// `thought:`. Prose that only starts with the letters ("Thought about it",
// "thoughts") does not match, and neither does "thought" past the first
// non-empty line. Lines split on "\n" only, as the Python copy splits them.
func opensWithUnstructuredReasoning(answer string) bool {
	for _, line := range strings.Split(answer, "\n") {
		line = strings.ToLower(strings.TrimSpace(line))
		if line == "" {
			continue
		}
		return line == unstructuredReasoningWord || strings.HasPrefix(line, unstructuredReasoningWord+unstructuredReasoningSeparator)
	}
	return false
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
