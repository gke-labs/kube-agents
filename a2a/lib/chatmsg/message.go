// Package chatmsg is the kube-agents.chat/v1 chat message: the structure an
// executor states beside its text so every chat adapter can lay out a
// report, a question or a plan without reading prose
// (spec-a2a-payloads.md, "Chat messages"). It holds the types, the
// validator for every rule in that section, the text fallback each carrier
// pairs with a chat message, and the DataPart encoding. The shared cases in
// tests/conformance/chat/ are checked against Validate here and against the
// Python presenters' reader there.
package chatmsg

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Schema is the value of a chat message's "schema" key.
const Schema = "kube-agents.chat/v1"

// schemaFamily prefixes every version of the schema. A data part whose
// "schema" starts with it is a chat message to a renderer, and one of
// another version is a chat message it refuses (an unknown schema), not a
// data part it skips.
const schemaFamily = "kube-agents.chat/"

// schemaKey and markKey name the two keys this package reads off the raw
// object before decoding the rest.
const (
	schemaKey = "schema"
	markKey   = "mark"
)

// dataPartKind is the A2A part kind a chat message rides in, and
// textPartKind the kind of its fallback.
const (
	dataPartKind = "data"
	textPartKind = "text"
)

// jsonNull is a JSON null, which a renderer reads as an absent field.
const jsonNull = "null"

// DefaultFoldTitle is the fold's title when the message gives none.
const DefaultFoldTitle = "why"

// Tone values.
const (
	ToneSuccess   = "success"
	ToneAttention = "attention"
	ToneCritical  = "critical"
)

// Plan item status values.
const (
	StatusPending    = "pending"
	StatusInProgress = "in_progress"
	StatusComplete   = "complete"
	StatusError      = "error"
)

// Mark settle values.
const (
	SettleDone    = "done"
	SettleBlocked = "blocked"
	SettleFailed  = "failed"
)

// Message is one kube-agents.chat/v1 object. Every field but Schema is
// optional, and a renderer omits a field that is absent or empty. Fields
// the type does not name are ignored on decode, so v1 can grow.
type Message struct {
	Schema   string   `json:"schema"`
	Headline string   `json:"headline,omitempty"`
	Note     string   `json:"note,omitempty"`
	Detail   string   `json:"detail,omitempty"`
	Tone     string   `json:"tone,omitempty"`
	Rows     []Row    `json:"rows,omitempty"`
	After    []string `json:"after,omitempty"`
	Fold     *Fold    `json:"fold,omitempty"`
	Choices  []Choice `json:"choices,omitempty"`
	Links    []Link   `json:"links,omitempty"`
	Plan     *Plan    `json:"plan,omitempty"`
	// Mark is the reaction state for the user message that started the
	// work. nil is absent and changes nothing; a non-nil empty Mark is the
	// whole state with nothing set.
	Mark *Mark `json:"mark,omitempty"`

	// markErr is why the raw mark did not decode. A bad mark drops alone,
	// so Parse keeps the rest and Validate reports this as the mark error.
	markErr error
}

// Row is one line of a report.
type Row struct {
	Text     string `json:"text"`
	Severity string `json:"severity,omitempty"`
	Detail   string `json:"detail,omitempty"`
}

// Fold is the message's one collapsed section.
type Fold struct {
	Title string `json:"title,omitempty"`
	Text  string `json:"text,omitempty"`
	Table *Table `json:"table,omitempty"`
}

// Table is a fold's table: plain cells, every row as wide as Columns.
type Table struct {
	Columns []string   `json:"columns"`
	Rows    [][]string `json:"rows,omitempty"`
}

// Choice is a button whose click arrives as the clicker's own message:
// Reply when set, else Text.
type Choice struct {
	Text    string `json:"text"`
	Reply   string `json:"reply,omitempty"`
	Primary bool   `json:"primary,omitempty"`
}

// Link is a button that opens a url.
type Link struct {
	Text    string `json:"text"`
	URL     string `json:"url"`
	Primary bool   `json:"primary,omitempty"`
}

// Plan is a running task's cards, read in progress and on a notify.
type Plan struct {
	Title string     `json:"title,omitempty"`
	Items []PlanItem `json:"items,omitempty"`
}

// PlanItem is one card. Title arrives composed; Note, Result and StepCount
// ride beside it for a backend that shows them as their own fields.
type PlanItem struct {
	ID        string   `json:"id"`
	Title     string   `json:"title,omitempty"`
	Status    string   `json:"status"`
	Steps     []string `json:"steps,omitempty"`
	StepCount int      `json:"stepCount,omitempty"`
	Note      string   `json:"note,omitempty"`
	Result    string   `json:"result,omitempty"`
}

// Mark is reaction state, not an operation: Ask names the kind of ask and
// Settle how it ended.
type Mark struct {
	Ask    string `json:"ask,omitempty"`
	Settle string `json:"settle,omitempty"`
}

// MarkOnly reports whether m carries nothing a renderer lays out: the
// terminal status-update's chat message, the one without a fallback.
func (m Message) MarkOnly() bool {
	return m.Headline == "" && m.Note == "" && m.Detail == "" && m.Tone == "" &&
		len(m.Rows) == 0 && len(m.After) == 0 && m.Fold == nil &&
		len(m.Choices) == 0 && len(m.Links) == 0 && m.Plan == nil
}

// Parse decodes one chat message's data object. A mark that does not decode
// is left nil and reported by Validate, since a bad mark drops alone; any
// other decode failure is the returned error, and drops the whole message.
func Parse(data []byte) (Message, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(data, &raw); err != nil {
		return Message{}, fmt.Errorf("chat message: not a JSON object: %w", err)
	}
	rawMark, hasMark := raw[markKey]
	delete(raw, markKey)
	rest, err := json.Marshal(raw)
	if err != nil {
		return Message{}, fmt.Errorf("chat message: %w", err)
	}
	var m Message
	if err := json.Unmarshal(rest, &m); err != nil {
		return Message{}, fmt.Errorf("chat message: %w", err)
	}
	if hasMark && string(bytes.TrimSpace(rawMark)) != jsonNull {
		var mark Mark
		if err := json.Unmarshal(rawMark, &mark); err != nil {
			m.markErr = &RuleError{Field: markKey, Rule: "not an object of ask and settle: " + err.Error()}
		} else {
			m.Mark = &mark
		}
	}
	return m, nil
}

// Encode is m as the data object a chat message part carries, with Schema
// filled in when m leaves it empty.
func Encode(m Message) (json.RawMessage, error) {
	if m.Schema == "" {
		m.Schema = Schema
	}
	return json.Marshal(m)
}

// Part validates m for carrier c and encodes it as the DataPart. It does
// not check the fallback, which sits elsewhere for some carriers; Parts
// does for the carriers whose fallback rides beside it.
func Part(c Carrier, m Message) (lib.Part, error) {
	if m.Schema == "" {
		m.Schema = Schema
	}
	if layout, mark := Validate(m, c); layout != nil {
		return lib.Part{}, layout
	} else if mark != nil {
		return lib.Part{}, mark
	}
	data, err := Encode(m)
	if err != nil {
		return lib.Part{}, err
	}
	return lib.Part{Kind: dataPartKind, Data: data}, nil
}

// Parts is the part list for a carrier whose fallback is the TextPart in
// the same list (progress, notice, an input-required question): the
// fallback, then m. It refuses an empty fallback, since a chat message is
// never emitted without one (assertion 27); a terminal's mark-only message
// is MarkPart, and a result or turn puts Part on its last chunk.
func Parts(c Carrier, fallback string, m Message) ([]lib.Part, error) {
	if !c.FallbackBeside() {
		return nil, fmt.Errorf("chat message: %s has no fallback beside the chat message; use Part", c)
	}
	if strings.TrimSpace(fallback) == "" {
		return nil, &RuleError{Field: "fallback", Rule: "a chat message is never emitted without its text fallback"}
	}
	part, err := Part(c, m)
	if err != nil {
		return nil, err
	}
	return []lib.Part{{Kind: textPartKind, Text: fallback}, part}, nil
}

// MarkPart is the chat message a terminal status-update may carry: mark
// alone, with no fallback.
func MarkPart(mark Mark) (lib.Part, error) {
	return Part(Terminal, Message{Schema: Schema, Mark: &mark})
}

// isChatPart reports whether p is a chat message of any version: a data
// part whose object's "schema" is a string in the schema's family.
func isChatPart(p lib.Part) bool {
	if p.Kind != dataPartKind || len(p.Data) == 0 {
		return false
	}
	var head map[string]json.RawMessage
	if json.Unmarshal(p.Data, &head) != nil {
		return false
	}
	var schema string
	if json.Unmarshal(head[schemaKey], &schema) != nil {
		return false
	}
	return strings.HasPrefix(schema, schemaFamily)
}

// chatParts is every chat message part in parts, in order.
func chatParts(parts []lib.Part) []lib.Part {
	var found []lib.Part
	for _, p := range parts {
		if isChatPart(p) {
			found = append(found, p)
		}
	}
	return found
}
