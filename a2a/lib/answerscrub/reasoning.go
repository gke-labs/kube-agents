package answerscrub

import "strings"

// An answer that is the model's own reasoning is withheld, not posted
// (#3206, #3773). A model sometimes writes its reasoning as plain assistant
// content instead of a thought part, so no reasoning channel carries it and a
// tag-based think scrubber passes it through as the answer. No reliable
// boundary marks where the reasoning ends and an answer begins, so an
// executor does not try to cut one out: the task ends failed with
// UnstructuredReasoningToken, and the reason carries the notice and the
// session, never the text. One shared implementation for the bridge and
// session paths. The Hermes image's unstructured_reasoning_replay patch keeps
// such a reply out of the history Hermes sends the model on later calls; its
// copy of the rule below is deploy/docker/patches/unstructured_reasoning.py,
// and the two must agree.
const (
	// UnstructuredReasoningToken is the terminal's reason token. The name
	// predates the session path; the gateway's reason sentences and the
	// bench's reason lists key on it, so both executors publish it.
	UnstructuredReasoningToken = "hermes-unstructured-reasoning" // #nosec G101 -- a reason label, not a credential
	// UnstructuredReasoningNotice is what the person reads after the token.
	// The check runs on the finished answer, after every tool call the turn
	// made, so it says so: asking again repeats whatever the turn did.
	UnstructuredReasoningNotice = "the agent's reply was withheld because it contained unstructured model reasoning instead of an answer; " +
		"anything the agent did during the turn has already happened, so check that before asking again"
	// unstructuredReasoningWord and unstructuredReasoningSeparator spell
	// the rule OpensWithUnstructuredReasoning applies.
	unstructuredReasoningWord      = "thought"
	unstructuredReasoningSeparator = ":"
)

// OpensWithUnstructuredReasoning is the rule. Three shapes have been seen: a
// first line `thought`, a first line `thought:`, and `thought:` with the
// reasoning on the same line ("thought:The user asked..."). So it reports
// whether answer's first non-empty line, with its surrounding whitespace
// dropped and compared case-insensitively, is exactly `thought` or starts with
// `thought:`. Prose that only starts with the letters ("Thought about it",
// "thoughts") does not match, and neither does "thought" past the first
// non-empty line. Lines split on "\n" only, as the Python copy splits them.
func OpensWithUnstructuredReasoning(answer string) bool {
	for _, line := range strings.Split(answer, "\n") {
		line = strings.ToLower(strings.TrimSpace(line))
		if line == "" {
			continue
		}
		return line == unstructuredReasoningWord || strings.HasPrefix(line, unstructuredReasoningWord+unstructuredReasoningSeparator)
	}
	return false
}
