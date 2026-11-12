package hermesbridge

import (
	"regexp"
	"strings"
)

// An answer that names a card id where the user asked about no task is asked
// for again, once, without it. The chat UX rule (.agents/rules/chat_ux.md,
// "No plumbing in user text") keeps a task or card id out of user text, with
// one exception: a board read, where the user asked about their tasks and may
// name a card back. The Planning Agent's SOUL says so (§1.5, §2, §4), and
// holds in a fresh session; in a thread whose earlier replies already carry
// an id, the model copies its own precedent despite the rule (seen on
// 2026-11-12 in the live Chat thread, after three green bench runs of
// chat-voice-reused-answer-no-card-id). No deterministic rewrite of prose is
// safe ("Renderers never compose"), so the bridge does not cut the id out:
// it sends the session one more user message asking for the same answer
// without the id, under its own idempotency key, and delivers what comes
// back. A restated answer that still names an id is delivered as it is, with
// a log line, so a stubborn model costs one call and never an answer. The
// check reads the turn's prompt as the user's words: when those name a task,
// card, board or kanban, the id may be the answer and nothing is asked.
const (
	// cardIDRestatePrompt is the one follow-up the bridge sends. It names
	// the fault and the shape wanted, and nothing about the answer's
	// content, so the model restates rather than reconsiders.
	cardIDRestatePrompt = "Your last reply names a card or task id (t_ and eight characters). The person did not ask about their tasks, " +
		"so restate the same reply without any card or task id: keep what was found, when, and who found it, and change nothing else."
	// cardIDRestateKeySuffix is appended to the turn's Idempotency-Key for
	// the restate call, so the server runs it rather than replaying the
	// turn's first answer.
	cardIDRestateKeySuffix = "/restate-card-id"
	// cardIDRestateLog names the event in the bridge's log, for the
	// operator who counts how often the rule has to be enforced.
	cardIDRestateLog = "card-id-restated"
)

// cardIDPattern is hermes's card id: `t_` and eight hex characters, as a
// word (bench/kube_agents_bench/parsing.py, _TASK_ID_RE).
var cardIDPattern = regexp.MustCompile(`\bt_[0-9a-f]{8}\b`)

// cardIDAskWords are the words in a user's ask that make a card id part of
// the answer: a board read, where the user may name a card back.
var cardIDAskWords = []string{"task", "card", "board", "kanban"}

// asksAboutTasks reports whether prompt, the user's words, asks about their
// tasks: any of cardIDAskWords, case-insensitively, as a substring, so
// "tasks", "cards" and "the board" count.
func asksAboutTasks(prompt string) bool {
	lower := strings.ToLower(prompt)
	for _, w := range cardIDAskWords {
		if strings.Contains(lower, w) {
			return true
		}
	}
	return false
}

// leaksCardID reports whether answer names a card id the user did not ask
// about: the id shape is in the answer and prompt asks about no task.
func leaksCardID(prompt, answer string) bool {
	return cardIDPattern.MatchString(answer) && !asksAboutTasks(prompt)
}
