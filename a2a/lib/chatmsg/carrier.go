package chatmsg

import (
	"encoding/json"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Carrier is where a chat message rides, which decides its text fallback
// and what a renderer reads of it (the "Where one may appear" table).
type Carrier string

const (
	// Result is the result artifact, on its last chunk.
	Result Carrier = Carrier(lib.ArtifactResult)
	// Turn is the turn artifact, on its last chunk.
	Turn Carrier = Carrier(lib.ArtifactTurn)
	// Progress is the progress artifact: the task's one live message.
	Progress Carrier = Carrier(lib.ArtifactProgress)
	// Notice is the notice artifact: one message of its own, posted once.
	Notice Carrier = "notice"
	// Question is the message of an input-required status-update.
	Question Carrier = Carrier(lib.StateInputRequired)
	// Terminal is the message of a terminal status-update; only its mark
	// is read, and it is the one carrier without a fallback.
	Terminal Carrier = "terminal"
	// Notify is a conversation chat.notify request's "chat", whose
	// fallback is the request's "text".
	Notify Carrier = "notify"
)

func (c Carrier) valid() bool {
	switch c {
	case Result, Turn, Progress, Notice, Question, Terminal, Notify:
		return true
	}
	return false
}

// FallbackBeside reports whether c's fallback is the TextPart in the same
// part list as the chat message.
func (c Carrier) FallbackBeside() bool {
	return c == Progress || c == Notice || c == Question
}

// ReadsPlan reports whether a renderer lays out a plan on c: the live
// message, and a card's notify, which stands in for it once the task ends.
func (c Carrier) ReadsPlan() bool {
	return c == Progress || c == Notify
}

// Reading is what a renderer does with one carrier: render Message when it
// is set, else post Fallback; apply Mark when it is set. Problems are why
// anything was dropped, for the log line and the counter.
type Reading struct {
	Message  *Message
	Fallback string
	Mark     *Mark
	Problems []error
}

// Fallback is the text a chat message on c falls back to, from parts. For a
// result or turn, parts are the artifact as folded per A2A chunking rules,
// and the fallback is all its text in order. For progress it is the last
// text part, the live message's current state, as the relay reads one; for
// a notice or an input-required question, the message's text parts joined.
// A terminal has none.
func Fallback(c Carrier, parts []lib.Part) string {
	switch c {
	case Terminal, Notify:
		return ""
	case Progress:
		for i := len(parts) - 1; i >= 0; i-- {
			if parts[i].Kind == textPartKind && parts[i].Text != "" {
				return parts[i].Text
			}
		}
		return ""
	}
	var b strings.Builder
	for _, p := range parts {
		if p.Kind == textPartKind {
			b.WriteString(p.Text)
		}
	}
	return b.String()
}

// Read is the renderer's side of one part list on c: progress, a notice,
// an input-required or terminal status message, or a result or turn
// published as one last chunk (ReadArtifact takes a chunked one). It never
// clips or repairs: a chat message that breaks a rule is dropped whole and
// the fallback stands, and a bad mark drops alone.
func Read(c Carrier, parts []lib.Part) Reading {
	return read(c, chatParts(parts), Fallback(c, parts))
}

// ReadArtifact is Read for a result or turn artifact published in chunks.
// The chat message is read only from the chunk with lastChunk set; one on
// any other chunk breaks assertion 27 and drops, and the fallback is the
// artifact's text as folded per A2A chunking rules.
func ReadArtifact(c Carrier, chunks []lib.ArtifactUpdate) Reading {
	var folded, chat []lib.Part
	var misplaced bool
	for _, chunk := range chunks {
		if !chunk.Append {
			folded = nil
		}
		folded = append(folded, chunk.Artifact.Parts...)
		found := chatParts(chunk.Artifact.Parts)
		if len(found) == 0 {
			continue
		}
		if !chunk.LastChunk {
			misplaced = true
			continue
		}
		chat = append(chat, found...)
	}
	r := read(c, chat, Fallback(c, folded))
	if misplaced {
		r.Message, r.Mark = nil, nil
		r.Problems = append(r.Problems, broke("chunk", "a chat message on a %s chunk that is not the last", c))
	}
	return r
}

// ReadNotify is Read for a conversation chat.notify request: its "chat"
// object, with its "text" as the fallback. A request with no "chat" reads
// as the text alone.
func ReadNotify(text string, chat json.RawMessage) Reading {
	if len(chat) == 0 || string(chat) == jsonNull {
		return Reading{Fallback: text}
	}
	return read(Notify, []lib.Part{{Kind: dataPartKind, Data: chat}}, text)
}

func read(c Carrier, chat []lib.Part, fallback string) Reading {
	r := Reading{Fallback: fallback}
	switch {
	case len(chat) == 0:
		return r
	case len(chat) > 1:
		r.Problems = append(r.Problems, broke("parts", "%d chat messages, at most one per chunk or status message", len(chat)))
		return r
	}
	m, err := Parse(chat[0].Data)
	if err != nil {
		r.Problems = append(r.Problems, err)
		return r
	}
	layout, mark := Validate(m, c)
	if layout == nil && c != Terminal && strings.TrimSpace(fallback) == "" {
		layout = broke("fallback", "a chat message on %s has no text fallback", c)
	}
	if layout != nil {
		r.Problems = append(r.Problems, layout)
		return r
	}
	if mark != nil {
		r.Problems = append(r.Problems, mark)
		m.Mark = nil
	}
	r.Mark = m.Mark
	if c != Terminal {
		if !c.ReadsPlan() {
			m.Plan = nil
		}
		r.Message = &m
	}
	return r
}
