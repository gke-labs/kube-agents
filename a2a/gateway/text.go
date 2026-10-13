package gateway

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gateway holds no model, so its affordances are literal: a small set of
// normalized phrases, deterministic by construction. Anything richer belongs
// in the executors.

// normalize lowercases and strips everything but letters, digits, and single
// spaces, so "What is it doing?!" and "what is it doing" are the same ask.
func normalize(s string) string {
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

// wideMatchLenCap bounds the wide interrogative match: past this length a
// message is a composed instruction, not a status poke, however it starts.
const wideMatchLenCap = 48

// isStatusQuery reports whether a mid-task message asks what the task is
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
func isStatusQuery(text string, wide bool) bool {
	n := normalize(text)
	if statusQueries[n] {
		return true
	}
	if !wide || len(n) > wideMatchLenCap {
		return false
	}
	statusish := strings.Contains(n, "doing") || strings.Contains(n, "happening") ||
		strings.Contains(n, "going on") || strings.Contains(n, "update")
	interrogative := strings.HasPrefix(n, "what") || strings.HasPrefix(n, "how") ||
		strings.HasPrefix(n, "any") || strings.HasPrefix(n, "is ") || strings.HasPrefix(n, "are ")
	return statusish && interrogative
}

// isDelegate reports whether the turn asks for a delegated session worker -
// the demo's "Delegate" flow, W4 amendment. Deterministic prefix, no model,
// same shape as isStatusQuery: the normalized text must START with
// "delegate" as a whole word, and the rest of the ORIGINAL text (which
// keeps its punctuation and casing - it is the task) is returned. A bare
// "delegate" with nothing to do is not a delegation.
func isDelegate(text string) (string, bool) {
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

// slashSessionWord and slashOffWord spell the one slash command the gateway
// resolves itself. Spec-chatops-gateway "Sessions by default": a slash
// command resolves first, names a route rather than a handle, and is a
// debugging/opt-in door for the transition rather than the taught interface.
const (
	slashSessionWord = "session"
	slashOffWord     = "off"
)

// isSessionCommand reports whether the turn is "/session": a leading "/"
// (after trimming) followed by the word, then the end of the text or
// whitespace. rest is the ORIGINAL text after the word, trimmed - "" for a
// bare "/session", "off" for the way back, anything else is the first turn.
// Any other slash word is not a command to the gateway and falls through as
// plain text; the chat platforms' own slash commands never reach the gateway.
// Slack also keeps a bare leading slash for itself (an unregistered command
// is refused client-side), so there the form is "@<bot> /session" - the
// mention is stripped before this reads the text.
func isSessionCommand(text string) (string, bool) {
	trimmed := strings.TrimSpace(text)
	if len(trimmed) < 1+len(slashSessionWord) || trimmed[0] != '/' {
		return "", false
	}
	body := trimmed[1:]
	word, rest := body, ""
	if end := strings.IndexFunc(body, unicode.IsSpace); end >= 0 {
		word, rest = body[:end], strings.TrimSpace(body[end:])
	}
	if !strings.EqualFold(word, slashSessionWord) {
		return "", false
	}
	return rest, true
}

// isSessionOff reports whether a /session argument is the way back.
// Normalized like isStop, so "off." and "Off!" are the way back too and
// never a first turn that opens the pod the user meant to leave.
func isSessionOff(rest string) bool {
	return normalize(rest) == slashOffWord
}

var stopWords = map[string]bool{
	"stop":   true,
	"cancel": true,
	"abort":  true,
}

// isStop reports whether the turn is the cancel affordance — the hard
// interrupt, mapped to kind:cancel (gateway design).
func isStop(text string) bool {
	return stopWords[normalize(text)]
}

// statusHistoryCap bounds the rendered transition history — a long-running
// task accumulates one state per event and the answer must stay one chat
// message.
const statusHistoryCap = 12

// askCap bounds the instruction echo in status answers and in the session KV.
const askCap = 140

// formatTaskStatus renders a replayed Task for chat: current state, the
// echoed ask and elapsed clock, the transition history, and the latest
// progress line.
func formatTaskStatus(t *lib.Task, ask string, since time.Time) string {
	var b strings.Builder
	fmt.Fprintf(&b, "🔎 task `%s` is **%s**", t.ID, t.State)
	if !since.IsZero() && !t.Final {
		fmt.Fprintf(&b, " (%s so far)", time.Since(since).Round(time.Second))
	}
	if ask != "" {
		fmt.Fprintf(&b, "\n🎯 on: “%s”", ask)
	}
	if len(t.StatusHistory) > 0 {
		history := t.StatusHistory
		prefix := ""
		if len(history) > statusHistoryCap {
			history = history[len(history)-statusHistoryCap:]
			prefix = "… → "
		}
		states := make([]string, len(history))
		for i, s := range history {
			states[i] = string(s)
		}
		fmt.Fprintf(&b, " (history: %s%s)", prefix, strings.Join(states, " → "))
	}
	if p := t.Artifact(lib.ArtifactProgress); p != nil {
		if text := lastTextPart(p.Parts); text != "" {
			fmt.Fprintf(&b, "\n📋 latest progress: %s", truncateRunes(text, progressCap))
		}
	}
	if r := t.Artifact(lib.ArtifactResult); r != nil {
		fmt.Fprintf(&b, "\n📦 result so far: %d part(s)", len(r.Parts))
	}
	b.WriteString("\n_(answered by stream replay — no live connection to the executor)_")
	return b.String()
}

func lastTextPart(parts []lib.Part) string {
	for i := len(parts) - 1; i >= 0; i-- {
		if parts[i].Kind == "text" && parts[i].Text != "" {
			return parts[i].Text
		}
	}
	return ""
}

func joinTextParts(parts []lib.Part) string {
	var b strings.Builder
	for _, p := range parts {
		if p.Kind == "text" {
			b.WriteString(p.Text)
		}
	}
	return b.String()
}

// chatChunks splits text for backends with a message size cap (Discord:
// 2000); the chunk size leaves headroom for decoration. Cuts land on line
// breaks where possible and never inside a UTF-8 sequence — a split rune is
// an invalid payload the backend may refuse outright.
//
// Every chunk is balanced on its fences: the adapters translate each chunk
// alone (toMrkdwn, toGchatText), and a chunk that opened inside a fenced
// block would carry the block's closing fence first, which reads as an
// opener and turns the prose after it into code -- or, with a second block
// further on, pairs with that block's opener and leaves the prose between
// them live. So a cut that falls inside a fence closes it at the end of the
// chunk and reopens it with a bare fence at the start of the next, and the
// budget for the text between shrinks by both so no chunk exceeds size.
// Whether a candidate chunk ends inside a fence is read by fenceOpenAtEnd,
// the same parse the adapters make of it, so the two cannot disagree. The
// opener's info string is not carried onto the reopened fence: it is
// unbounded (a single-line opener carries its whole line), and carrying it
// both repeated that content at the head of every continuation and left
// the budget nothing to cut with. A continuation chunk therefore loses the
// language tag on Discord; the text between the inserted fences is the
// original, byte for byte. Text with no fence open at the cut is split
// exactly as before.
func chatChunks(text string, size int) []string {
	if text == "" {
		return nil
	}
	var chunks []string
	reopen := "" // after a cut inside a block: mdFence, with a line break where the cut left none
	for {
		// reopen is at most mdFence+"\n" (4 bytes), so the budget is at
		// least size-4 and the recut below is given at least size-8: with
		// size the Discord cap, chunkCut always has room to make progress.
		budget := size - len(reopen)
		if len(text) <= budget {
			return append(chunks, reopen+text)
		}
		cut := chunkCut(text, budget)
		open := fenceOpenAtEnd(reopen + text[:cut])
		if open {
			cut = chunkCut(text, budget-len(fenceClose))
			open = fenceOpenAtEnd(reopen + text[:cut])
		}
		chunk := reopen + text[:cut]
		reopen = ""
		if open {
			chunk += fenceClose
			reopen = mdFence
			if !strings.HasPrefix(text[cut:], "\n") {
				reopen += "\n"
			}
		}
		chunks = append(chunks, chunk)
		text = text[cut:]
	}
}

// fenceClose ends a chunk that was cut inside a fenced block; the next
// chunk reopens the block with a bare mdFence.
const fenceClose = "\n```"

// fenceOpenAtEnd reports whether chunk ends inside a fenced block, read as
// the adapters read a chunk: the last span mdCodeSpanRE finds is a fence
// that ran to the end of the text without its closer (mdFenceUnclosed). A
// line such as `use ``` to open a fence` holds no fence by this parse --
// the first backtick of the three closes the inline span `use ` and the
// other two are a span of their own -- where a count of ``` per line would
// say a fence opened, and the chunker would then wrap the prose after the
// cut in fences of its own.
func fenceOpenAtEnd(chunk string) bool {
	spans := mdCodeSpanRE.FindAllStringIndex(chunk, -1)
	if len(spans) == 0 {
		return false
	}
	last := spans[len(spans)-1]
	return last[1] == len(chunk) && mdFenceUnclosed(chunk[last[0]:last[1]])
}

// chunkCut finds where to cut text so the head fits in size: the last line
// break in the second half of the budget, else a hard cut at a rune start.
func chunkCut(text string, size int) int {
	cut := strings.LastIndex(text[:size], "\n")
	if cut < size/2 {
		cut = size
		for cut > 0 && !utf8.RuneStart(text[cut]) {
			cut--
		}
	}
	return cut
}

// truncateRunes bounds s to n bytes at a rune boundary, with an ellipsis
// marking the loss.
func truncateRunes(s string, n int) string {
	if len(s) <= n {
		return s
	}
	cut := n
	for cut > 0 && !utf8.RuneStart(s[cut]) {
		cut--
	}
	return s[:cut] + "…"
}

func marshalMessage(m lib.Message) ([]byte, error) {
	return json.Marshal(m)
}
