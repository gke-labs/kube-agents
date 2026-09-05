package gateway

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
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

// isStatusQuery reports whether a mid-task message asks what the task is
// doing rather than telling it something. Deterministic by design - the
// gateway holds no model - so this is a phrase set plus a narrow
// interrogative rule, not understanding. The interrogative rule is the wide
// half and it misfires ("any update to the config should be reverted" is a
// steer), so it only applies when wide is true. The caller sets wide by
// executor: a fixed-route executor (Hermes) refuses steers, so a stolen
// false positive costs nothing; a session worker absorbs steers, so a
// stolen one is a dropped correction and only the exact phrases match -
// a status-shaped steer there is a question the worker can answer itself.
// wideMatchLenCap bounds the wide interrogative match: past this length a
// message is a composed instruction, not a status poke, however it starts.
const wideMatchLenCap = 48

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
func chatChunks(text string, size int) []string {
	if text == "" {
		return nil
	}
	var chunks []string
	for len(text) > size {
		cut := strings.LastIndex(text[:size], "\n")
		if cut < size/2 {
			cut = size
			for cut > 0 && !utf8.RuneStart(text[cut]) {
				cut--
			}
		}
		chunks = append(chunks, text[:cut])
		text = text[cut:]
	}
	return append(chunks, text)
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
