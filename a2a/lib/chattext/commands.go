// Package chattext holds the literal rules the chat gateway applies to text
// a person typed: which messages it acts on as commands, and which link
// labels misname their destination. The gateway acts on them; the chat
// message validator (lib/chatmsg) refuses a button that would trip them, so
// both read one copy.
package chattext

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

// The gateway holds no model, so its affordances are literal: a small set of
// normalized phrases, deterministic by construction. Anything richer belongs
// in the executors.

// Normalize lowercases and strips everything but letters, digits, and single
// spaces, so "What is it doing?!" and "what is it doing" are the same ask.
func Normalize(s string) string {
	var b strings.Builder
	lastSpace := true
	for _, r := range strings.ToLower(strings.TrimSpace(s)) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '\'':
			if r != '\'' { // drop apostrophes: "what's" -> "whats"
				b.WriteRune(r)
			}
			lastSpace = false
		case r == ' ' || r == '\t' || r == '\n':
			if !lastSpace {
				b.WriteRune(' ')
			}
			lastSpace = true
		}
	}
	return strings.TrimSpace(b.String())
}

var statusQueries = map[string]bool{
	"what is it doing":   true,
	"whats it doing":     true,
	"what are you doing": true,
	"whats happening":    true,
	"what is happening":  true,
	"whats going on":     true,
	"what is going on":   true,
	"status":             true,
	"progress":           true,
	"any progress":       true,
	"any update":         true,
	"any updates":        true,
	"where are we":       true,
	"hows it going":      true,
	"how is it going":    true,
}

// WideMatchLenCap bounds the wide interrogative match: past this length a
// message is a composed instruction, not a status poke, however it starts.
const WideMatchLenCap = 48

// IsStatusQuery reports whether a mid-task message asks what the task is
// doing rather than telling it something. Deterministic by design - the
// gateway holds no model - so this is a phrase set plus a narrow
// interrogative rule, not understanding. The interrogative rule is the wide
// half and it misfires ("any update to the config should be reverted" is a
// steer), so it applies only when wide is true. The caller sets wide by
// what the executor does with a steer: one that runs follow-ups (the
// bridge's api executor, a session worker) gets the exact phrases only,
// because a stolen steer there is a lost correction and a status-shaped
// steer is a question the agent can answer itself; one that refuses them
// (the bridge's cli executor, which refuses each no-resume) gets the wide
// rule, because a stolen false positive there costs nothing and the
// alternative is an ack followed by a refusal.
func IsStatusQuery(text string, wide bool) bool {
	n := Normalize(text)
	if statusQueries[n] {
		return true
	}
	if !wide || len(n) > WideMatchLenCap {
		return false
	}
	statusish := strings.Contains(n, "doing") || strings.Contains(n, "happening") ||
		strings.Contains(n, "going on") || strings.Contains(n, "update")
	interrogative := strings.HasPrefix(n, "what") || strings.HasPrefix(n, "how") ||
		strings.HasPrefix(n, "any") || strings.HasPrefix(n, "is ") || strings.HasPrefix(n, "are ")
	return statusish && interrogative
}

// IsDelegate reports whether the turn asks for a delegated session worker -
// the demo's "Delegate" flow, W4 amendment. Deterministic prefix, no model,
// same shape as IsStatusQuery: the normalized text must START with
// "delegate" as a whole word, and the rest of the ORIGINAL text (which
// keeps its punctuation and casing - it is the task) is returned. A bare
// "delegate" with nothing to do is not a delegation.
func IsDelegate(text string) (string, bool) {
	const word = "delegate"
	trimmed := strings.TrimSpace(text)
	if len(trimmed) < len(word) || !strings.EqualFold(trimmed[:len(word)], word) {
		return "", false
	}
	rest := trimmed[len(word):]
	if rest == "" {
		return "", false
	}
	// A separator keeps "delegated tasks are neat" out: whitespace or light
	// punctuation right after the word, nothing else.
	switch r, _ := utf8.DecodeRuneInString(rest); {
	case r == ' ', r == '\t', r == '\n', r == ':', r == ',', r == '-', r == '—':
	default:
		return "", false
	}
	rest = strings.TrimSpace(strings.TrimLeft(rest, ":,-— \t\n"))
	if rest == "" {
		return "", false
	}
	return rest, true
}

// slashSessionWord and slashOffWord spell the first of the two slash commands
// the gateway resolves itself. Spec-chatops-gateway "Sessions by default": a
// slash command resolves first, names a route rather than a handle, and is a
// debugging/opt-in door for the transition rather than the taught interface.
// slashFeedbackWord is the second (docs/designs/chat-feedback-command.md):
// bare, the gateway answers it with the two reporting links below; with text
// after it, the gateway passes the message through as typed.
const (
	slashSessionWord  = "session"
	slashOffWord      = "off"
	slashFeedbackWord = "feedback"
)

// The two places a report about kube-agents itself goes, as the gateway's
// bare-/feedback notice hands them out. The same two links are pinned in the
// personas by tests/test_feedback_reference.py, and the short link is the
// docs site's redirect to the form (scripts/feedback_form/README.md), never
// a Google Forms URL, so a recreated form needs no release.
const (
	FeedbackTrackerURL = "https://github.com/gke-labs/kube-agents/issues"
	FeedbackFormURL    = "https://gke-labs.github.io/kube-agents/feedback"
)

// IsSessionCommand reports whether the turn is "/session": a leading "/"
// (after trimming) followed by the word, then the end of the text or
// whitespace. rest is the ORIGINAL text after the word, trimmed - "" for a
// bare "/session", "off" for the way back, anything else is the first turn.
// Any other slash word is not a command to the gateway and falls through as
// plain text; the chat platforms' own slash commands never reach the gateway.
// Slack also keeps a bare leading slash for itself (an unregistered command
// is refused client-side), so there the form is "@<bot> /session" - the
// mention is stripped before this reads the text.
func IsSessionCommand(text string) (string, bool) {
	return slashWord(text, slashSessionWord)
}

// IsFeedbackCommand reports whether the turn is "/feedback": the same shape
// as IsSessionCommand. rest is "" for a bare "/feedback", which the gateway
// answers itself with FeedbackTrackerURL and FeedbackFormURL; anything else
// is the report the person wants written up, and the gateway hands the text
// on as typed, prefix included, for the agent to compose and confirm.
func IsFeedbackCommand(text string) (string, bool) {
	return slashWord(text, slashFeedbackWord)
}

// slashWord resolves one slash command: a leading "/" (after trimming)
// followed by word, case-insensitively, then the end of the text or
// whitespace. rest is the ORIGINAL text after the word, trimmed.
func slashWord(text, word string) (string, bool) {
	trimmed := strings.TrimSpace(text)
	if len(trimmed) < 1+len(word) || trimmed[0] != '/' {
		return "", false
	}
	body := trimmed[1:]
	got, rest := body, ""
	if end := strings.IndexFunc(body, unicode.IsSpace); end >= 0 {
		got, rest = body[:end], strings.TrimSpace(body[end:])
	}
	if !strings.EqualFold(got, word) {
		return "", false
	}
	return rest, true
}

// IsSessionOff reports whether a /session argument is the way back.
// Normalized like IsStop, so "off." and "Off!" are the way back too and
// never a first turn that opens the pod the user meant to leave.
func IsSessionOff(rest string) bool {
	return Normalize(rest) == slashOffWord
}

var stopWords = map[string]bool{
	"stop":   true,
	"cancel": true,
	"abort":  true,
}

// IsStop reports whether the turn is the cancel affordance — the hard
// interrupt, mapped to kind:cancel (gateway design).
func IsStop(text string) bool {
	return stopWords[Normalize(text)]
}

// IsCommand reports whether the gateway would act on text as a command on
// any route and in any task state: the superset of the five matchers, with
// the status ask in its wide form. A chat message's choice must not be one
// (spec-a2a-payloads.md, "Chat messages"), since a click posts its text as
// the clicker's own message.
func IsCommand(text string) bool {
	if IsStop(text) || IsStatusQuery(text, true) {
		return true
	}
	if _, ok := IsSessionCommand(text); ok {
		return true
	}
	// Only the bare form: "/feedback <text>" is an ordinary turn the agent
	// answers, and a button may well offer one.
	if rest, ok := IsFeedbackCommand(text); ok && rest == "" {
		return true
	}
	_, ok := IsDelegate(text)
	return ok
}
