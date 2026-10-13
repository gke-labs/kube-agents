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
// mark-only message is read for its mark alone (ChatMarker).

// chatProblemMax bounds, in runes, the rule a dropped chat message broke as
// the log line carries it: the rule quotes the offending value for some
// rules (an unknown schema), and that value is executor output.
const chatProblemMax = 200

// lineKeySeparator joins a line's text and its chat message in lineKey: a
// NUL, which no status line carries.
const lineKeySeparator = "\x00"

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
	for _, p := range r.Problems {
		g.log.Warn("chat message dropped; its text fallback stands",
			"conversation", conversation, "taskId", taskID, "carrier", string(c),
			"rule", truncateRunes(p.Error(), chatProblemMax))
	}
}

// postMessage posts m: laid out, as one message, when it carries a chat
// message the adapter renders; otherwise its text through post, chunked
// under the backend cap, which for a message without a chat message is
// exactly the post the relay made before chat messages existed.
func (g *Gateway) postMessage(conversation string, m OutMessage) {
	if m.Chat != nil && strings.TrimSpace(m.Text) != "" {
		_, err := g.adapter.PostMessage(conversation, m)
		if g.chatOutcome(conversation, err) {
			return
		}
	}
	g.post(conversation, m.Text)
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
	default:
		g.metrics.chatMessage(chatRefused)
		g.log.Warn("chat message not rendered; posting its text fallback", "conversation", conversation, "err", err)
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
		}
		if !rs.lineLaidOut {
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
// replayed for it, off the replayed artifact, which arrives folded.
func (g *Gateway) resultChat(conversation, taskID string, rs *relayState, replayed []lib.Part) *chatmsg.Message {
	switch {
	case replayed != nil:
		return g.readChat(conversation, taskID, chatmsg.Result, replayed).Message
	case len(rs.resultChunks) > 0:
		return g.readChatArtifact(conversation, taskID, chatmsg.Result, rs.resultChunks).Message
	}
	return nil
}

// relayMark hands a terminal's mark to an adapter that applies marks.
func (g *Gateway) relayMark(conversation, taskID string, r chatmsg.Reading) {
	if r.Mark == nil {
		return
	}
	if marker, ok := g.adapter.(ChatMarker); ok {
		marker.ChatMark(conversation, taskID, *r.Mark)
	}
}

// appendChunk folds one artifact-update into a chunk list the way A2A
// chunking does: a chunk that does not append starts the artifact over.
func appendChunk(chunks []lib.ArtifactUpdate, a lib.ArtifactUpdate) []lib.ArtifactUpdate {
	if !a.Append {
		chunks = nil
	}
	return append(chunks, a)
}
