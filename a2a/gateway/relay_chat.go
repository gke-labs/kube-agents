package gateway

import (
	"encoding/json"
	"errors"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// The relay's half of chat messages (spec-a2a-payloads.md, "Chat
// messages"): each carrier it posts from -- a result, a finished turn, a
// notice, an input-required question, the live progress line -- is read with
// chatmsg (validated, and dropped whole with its fallback standing when it
// breaks a rule), and the chat message that survives goes to the adapter
// beside its text. An adapter that renders it posts one laid-out message; any
// other posts the text, exactly as before chat messages existed. A terminal's
// mark-only message is read for its mark alone, and every carrier's mark is
// drawn by the gateway's mark rules (mark.go).

// chatProblemMax bounds, in runes, the rule a dropped chat message broke as
// the log line carries it: the rule quotes the offending value for some
// rules (an unknown schema), and that value is executor output.
const chatProblemMax = 200

// lineKeySeparator joins a line's text and its chat message in lineKey: a
// NUL, which no status line carries.
const lineKeySeparator = "\x00"

// layoutOf is the chat message a reading hands an adapter to lay out: nil
// for one that carries only a mark, which lays nothing out and whose mark
// is applied, not drawn.
func layoutOf(r chatmsg.Reading) *chatmsg.Message {
	if r.Message == nil {
		return nil
	}
	// A tone colours what is below it and alone colours nothing, so a
	// message whose only layout field is its tone lays nothing out either.
	toneless := *r.Message
	toneless.Tone = ""
	if toneless.MarkOnly() {
		return nil
	}
	return r.Message
}

// readChat reads carrier c's chat message out of parts, logging and counting
// what it dropped.
func (g *Gateway) readChat(conversation, taskID string, c chatmsg.Carrier, parts []lib.Part) chatmsg.Reading {
	r := chatmsg.Read(c, parts)
	g.noteChatProblems(conversation, taskID, c, r)
	return r
}

// readChatArtifact is readChat for a result or turn published in chunks,
// whose chat message rides only its last chunk.
func (g *Gateway) readChatArtifact(conversation, taskID string, c chatmsg.Carrier, chunks []lib.ArtifactUpdate) chatmsg.Reading {
	r := chatmsg.ReadArtifact(c, chunks)
	g.noteChatProblems(conversation, taskID, c, r)
	return r
}

// noteChatProblems logs each rule a reading broke, and counts the reading
// once as invalid when it broke any: the spec's "logged and counted".
func (g *Gateway) noteChatProblems(conversation, taskID string, c chatmsg.Carrier, r chatmsg.Reading) {
	if len(r.Problems) == 0 {
		return
	}
	g.metrics.chatMessage(chatInvalid)
	what := chatDroppedWhat(c, r)
	for _, p := range r.Problems {
		g.log.Warn(what,
			"conversation", conversation, "taskId", taskID, "carrier", string(c),
			"rule", truncateRunes(p.Error(), chatProblemMax))
	}
}

// chatDroppedWhat says what a reading with problems kept: a bad mark is
// dropped alone with the layout still posted, and a terminal has no fallback
// to stand.
func chatDroppedWhat(c chatmsg.Carrier, r chatmsg.Reading) string {
	switch {
	case c == chatmsg.Terminal:
		return "terminal mark dropped"
	case r.Message != nil:
		return "chat message's mark dropped; its layout stands"
	default:
		return "chat message dropped; its text fallback stands"
	}
}

// postMessage posts m: laid out, as one message, when it carries a chat
// message the adapter renders; otherwise its text through post, chunked
// under the backend cap, which for a message without a chat message is
// exactly the post the relay made before chat messages existed. An answer
// marked for feedback keeps the mark on the text too, on its last chunk.
func (g *Gateway) postMessage(conversation string, m OutMessage) {
	if m.Chat != nil && strings.TrimSpace(m.Text) != "" {
		_, err := g.adapter.PostMessage(conversation, m)
		if g.chatOutcome(conversation, err) {
			return
		}
	}
	if m.Feedback == "" || strings.TrimSpace(m.Text) == "" {
		g.post(conversation, m.Text)
		return
	}
	chunks := chatChunks(m.Text, discordChunk)
	last := len(chunks) - 1
	for _, chunk := range chunks[:last] {
		if _, err := g.adapter.Post(conversation, chunk); err != nil {
			g.log.Error("post failed", "conversation", conversation, "err", err)
			return
		}
	}
	// A backend that draws no rating controls posts the text as Post would
	// (postTextOnly); one whose API refuses them leaves the chunk to Post.
	if _, err := g.adapter.PostMessage(conversation, OutMessage{Text: chunks[last], Feedback: m.Feedback}); err != nil {
		g.log.Warn("posting an answer's last chunk with its rating controls failed; posting it as text",
			"conversation", conversation, "err", err)
		g.post(conversation, chunks[last])
	}
}

// chatOutcome counts what became of one chat message the adapter was handed,
// logs a refusal, and reports whether the adapter laid it out. Anything but
// success leaves the caller to send the text instead: the spec's "retries it
// once as the fallback".
func (g *Gateway) chatOutcome(conversation string, err error) bool {
	switch {
	case err == nil:
		g.metrics.chatMessage(chatRendered)
		return true
	case errors.Is(err, ErrTextOnly):
		g.metrics.chatMessage(chatTextOnly)
	case errors.Is(err, ErrLayoutRefused):
		g.metrics.chatMessage(chatRefused)
		g.log.Warn("chat message not rendered; posting its text fallback", "conversation", conversation, "err", err)
	default:
		// A failed call, not a refused layout: nothing to count.
		g.log.Warn("chat message post or edit failed; trying its text", "conversation", conversation, "err", err)
	}
	return false
}

// renderLine edits the task's live message to line, laid out with the
// progress artifact's chat message when the task's newest progress carried
// one (relayState.lineChat). A line that has never been laid out, with no
// chat message to lay out, is the text edit the relay always made. One that
// was laid out and has none now is replaced by its text, layout cleared. A
// chat message the adapter will not lay out falls back to the text edit.
func (g *Gateway) renderLine(conversation, messageID, line string, rs *relayState) bool {
	line = truncateRunes(line, discordChunk)
	if rs.lineTextOnly || (rs.lineChat == nil && !rs.lineLaidOut) {
		return g.editLine(conversation, messageID, line)
	}
	if rs.lineChat != nil {
		err := g.adapter.EditMessage(conversation, messageID, OutMessage{Text: line, Chat: rs.lineChat})
		if g.chatOutcome(conversation, err) {
			rs.lineLaidOut = true
			return true
		}
		if errors.Is(err, ErrTextOnly) {
			// The backend shows text only, and will for the life of the
			// task: from here the line is the text edit alone.
			rs.lineTextOnly = true
		} else if !errors.Is(err, ErrLayoutRefused) {
			// The edit failed for another reason (a rate limit, an
			// outage): not landed, so the next artifact tries again with
			// the layout kept.
			return false
		} else {
			// The backend refused this layout: it is not offered again
			// until the progress chat message changes (applyArtifact).
			rs.lineRefused = lineKey("", rs)
			rs.lineChat = nil
		}
		if !rs.lineLaidOut {
			if lineKeyText(rs.lastLine) == line {
				// The line already shows this text; only the chat
				// message changed, and this backend did not lay it out.
				return true
			}
			return g.editLine(conversation, messageID, line)
		}
	}
	// The line shows a layout it should not any more: replace it whole.
	if err := g.adapter.EditMessage(conversation, messageID, OutMessage{Text: line}); err != nil {
		g.log.Warn("rolling line edit failed", "conversation", conversation, "err", err)
		return false
	}
	rs.lineLaidOut = false
	return true
}

// lineKey is what the rolling line shows, for the relay's skip of an edit
// that would change nothing: the text, and the chat message laid out with
// it. On a backend that showed it renders text only (lineTextOnly) the chat
// message changes nothing it shows, so it is no part of the key.
func lineKey(line string, rs *relayState) string {
	chat := rs.lineChat
	if chat == nil || rs.lineTextOnly {
		return line
	}
	encoded, err := json.Marshal(chat)
	if err != nil {
		return line
	}
	return line + lineKeySeparator + string(encoded)
}

// resultChat reads the result's chat message: off the chunks this process
// relayed, or, when the render state had no result and the stream was
// replayed for it, off the replayed artifact. That one arrives folded, so
// which chunk carried the chat message is lost and the last-chunk rule
// (assertion 27) cannot be applied to it: a chat message an executor put on
// an earlier chunk drops on the live path and is read on this one. The
// whole reading, for its mark beside its layout (layoutOf).
func (g *Gateway) resultChat(conversation, taskID string, rs *relayState, replayed []lib.Part) chatmsg.Reading {
	switch {
	case replayed != nil:
		return g.readChat(conversation, taskID, chatmsg.Result, replayed)
	case len(rs.resultChunks) > 0:
		return g.readChatArtifact(conversation, taskID, chatmsg.Result, rs.resultChunks)
	}
	return chatmsg.Reading{}
}

// lineKeyText is the text half of a lineKey.
func lineKeyText(key string) string {
	text, _, _ := strings.Cut(key, lineKeySeparator)
	return text
}

// markFrom draws the mark a task's chat message carried, if it carried one
// (mark.go): a bad mark was dropped alone by the read, so the layout and the
// mark stand or fall apart.
func (g *Gateway) markFrom(rec *SessionRecord, taskID string, r chatmsg.Reading) {
	if r.Mark != nil {
		g.applyTaskMark(rec, taskID, MarkState{Ask: r.Mark.Ask, Settle: r.Mark.Settle})
	}
}

// terminalMark draws a terminal's mark: the one its mark-only chat message
// carried, or the default for a terminal with none. A delegated child's end
// takes no default here: its terminal is relayed before the wake that
// digests it is minted, so the child still reads as the chain's newest task,
// and the default would take the ask off (or leave ❌ over a wake that then
// succeeds) before the chain's answer. The wake's own terminal settles the
// chain, or observeChildEnd when no wake starts.
func (g *Gateway) terminalMark(rec *SessionRecord, taskID string, state lib.TaskState, r chatmsg.Reading) {
	if r.Mark != nil {
		g.markFrom(rec, taskID, r)
		return
	}
	if ref, ok := rec.TaskRefFor(taskID); ok && ref.Role == taskRoleChild {
		return
	}
	g.applyTerminalMark(rec, taskID, state)
}

// appendChunk folds one artifact-update into a chunk list the way A2A
// chunking does: a chunk that does not append starts the artifact over.
func appendChunk(chunks []lib.ArtifactUpdate, a lib.ArtifactUpdate) []lib.ArtifactUpdate {
	if !a.Append {
		chunks = nil
	}
	return append(chunks, a)
}
