package gateway

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

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
// original, byte for byte.
//
// A cut inside a pipe table carries the table's header and delimiter rows to
// the head of the next chunk, for the same reason: each chunk is rendered
// alone, and the rows after the cut would otherwise be raw `| … |` lines with
// no header (Slack renders a table only from a header and a delimiter row).
// A header longer than a quarter of the chunk is not carried. Text with no
// fence open and no table running across the cut is split exactly as before.
func chatChunks(text string, size int) []string {
	if text == "" {
		return nil
	}
	var chunks []string
	reopen := "" // after a cut inside a block: mdFence, with a line break where the cut left none
	for {
		// reopen is a reopened fence (at most mdFence+"\n", 4 bytes) or a
		// carried table head (at most size/tableHeadShareMax plus a line
		// break), so the budget stays above half of size and chunkCut always
		// has room to make progress.
		budget := size - len(reopen)
		if len(text) <= budget {
			return append(chunks, reopen+text)
		}
		cut := chunkCut(text, budget)
		cut = adjustCutToCodeSpanStart(reopen, text, cut, budget)
		open := fenceOpenAtEnd(reopen + text[:cut])
		if open {
			cut = chunkCut(text, budget-len(fenceClose))
			cut = adjustCutToCodeSpanStart(reopen, text, cut, budget-len(fenceClose))
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
		} else if head := tableHeadAtCut(chunk, text[cut:]); head != "" && len(head) <= size/tableHeadShareMax {
			reopen = head
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

// tableHeadShareMax bounds a carried table head to 1/tableHeadShareMax of
// the chunk size, so a very wide header cannot leave the budget no room.
const tableHeadShareMax = 4

// tableHeadAtCut returns the header and delimiter rows of the pipe table that
// chunk ends inside, joined by a line break, when rest goes on with more of
// its rows; otherwise "". The table is the trailing run of non-blank lines
// that carry a pipe, holding a header followed by a delimiter row with the
// same cell count (tableEnd's test), and no fence line.
func tableHeadAtCut(chunk, rest string) string {
	next := strings.TrimPrefix(rest, "\n")
	if line, _, _ := strings.Cut(next, "\n"); !strings.Contains(line, slackTablePipe) || strings.TrimSpace(line) == "" {
		return ""
	}
	lines := strings.Split(chunk, "\n")
	start := len(lines)
	for start > 0 && strings.Contains(lines[start-1], slackTablePipe) && strings.TrimSpace(lines[start-1]) != "" &&
		!strings.HasPrefix(strings.TrimSpace(lines[start-1]), mdFence) {
		start--
	}
	for i := start; i+1 < len(lines); i++ {
		if slackTableSepRE.MatchString(lines[i+1]) && len(tableCells(lines[i])) == len(tableCells(lines[i+1])) {
			return lines[i] + "\n" + lines[i+1]
		}
	}
	return ""
}

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

// adjustCutToCodeSpanStart moves cut back to the start of any code span
// found by mdCodeSpanRE in (reopen+text) that contains cut (relative to text),
// provided that the cut falls inside the span's opening delimiter, or the
// span can be carried intact into the next chunk and its start lies in the second
// half of the budget (the same rule chunkCut applies to line breaks); otherwise
// cut is returned unchanged so that spans longer than a chunk, or spans whose closer
// is followed by an unbroken line exceeding remaining budget, are not moved unnecessarily.
// The scan is bounded to the prefix of text that can affect this cut, keeping
// chunking linear in message size.
func adjustCutToCodeSpanStart(reopen, text string, cut, budget int) int {
	textLimit := cut + budget
	if textLimit > len(text) {
		textLimit = len(text)
	} else {
		for textLimit < len(text) && !utf8.RuneStart(text[textLimit]) {
			textLimit++
		}
	}
	candidate := reopen + text[:textLimit]
	cutInCand := len(reopen) + cut

	for _, span := range mdCodeSpanRE.FindAllStringIndex(candidate, -1) {
		if span[0] >= cutInCand {
			break
		}
		if span[0] < cutInCand && cutInCand < span[1] {
			spanText := candidate[span[0]:span[1]]
			openTicks := 0
			for openTicks < len(spanText) && spanText[openTicks] == '`' {
				openTicks++
			}
			insideOpener := cutInCand <= span[0]+openTicks
			spanStartInText := span[0] - len(reopen)
			spanLen := span[1] - span[0]
			closedOrComplete := !mdFenceUnclosed(spanText) || textLimit == len(text)
			fitsIntact := false
			if closedOrComplete && spanStartInText >= 0 && spanLen <= budget {
				rest := text[spanStartInText:]
				fitsIntact = len(rest) <= budget || chunkCut(rest, budget) >= spanLen
			}
			if insideOpener || fitsIntact {
				if spanStartInText >= budget/2 {
					return spanStartInText
				}
			}
			break
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
