// Package lib implements the a2a-jetstream/0.4 client library: the envelope,
// the A2A payload layer, and the JetStream transport with the NR resilience
// contract. Spec: docs/designs/spec-a2a-payloads.md and the client-resilience
// section of docs/designs/spec-nats-deployment.md.
package lib

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/nats-io/nuid"
)

// Protocol is the wire protocol this library speaks.
const Protocol = "a2a-jetstream/0.4"

// protocolMajor is the one major this library accepts. Consumers MUST reject
// unknown majors and ignore unknown envelope fields within a major.
const protocolMajor = 0

// InlineFileThreshold is the max decoded size in bytes of an inline FilePart;
// larger files must travel by uri (dev default per the payload spec).
const InlineFileThreshold = 128 * 1024

// Kind selects the payload type carried by an envelope.
type Kind string

const (
	KindMessage        Kind = "message"
	KindStatusUpdate   Kind = "status-update"
	KindArtifactUpdate Kind = "artifact-update"
	KindCancel         Kind = "cancel"
	KindAgentCard      Kind = "agent-card"
	KindAgentClosed    Kind = "agent-closed"
	KindTopicUpdate    Kind = "topic-update"
)

// Party identifies a sender or addressee. Routing and display only; never an
// authorization input.
type Party struct {
	Session   string `json:"session"`
	AgentType string `json:"agentType,omitempty"`
	Profile   string `json:"profile,omitempty"`
}

// Envelope is the transport wrapper. Everything below Payload is a standard
// A2A object; everything above it is ours. Identity and Authority are held as
// raw bytes so inbound values pass through byte-identical.
type Envelope struct {
	Protocol      string          `json:"protocol"`
	EnvelopeID    string          `json:"envelopeId"`
	CorrelationID string          `json:"correlationId"`
	Traceparent   string          `json:"traceparent,omitempty"`
	TaskID        string          `json:"taskId,omitempty"`
	ContextID     string          `json:"contextId,omitempty"`
	TS            time.Time       `json:"ts"`
	From          Party           `json:"from"`
	To            *Party          `json:"to,omitempty"`
	Identity      json.RawMessage `json:"identity"`
	Authority     json.RawMessage `json:"authority"`
	Kind          Kind            `json:"kind"`
	Payload       json.RawMessage `json:"payload"`
}

// EnvelopeOption mutates an envelope at construction.
type EnvelopeOption func(*Envelope)

// WithAuthority sets the advisory authority block. Gateway ingress path only;
// every other producer leaves it null.
func WithAuthority(authority json.RawMessage) EnvelopeOption {
	return func(e *Envelope) { e.Authority = authority }
}

// WithTo addresses the envelope to a named session.
func WithTo(to Party) EnvelopeOption {
	return func(e *Envelope) { e.To = &to }
}

// WithTraceparent attaches W3C trace context.
func WithTraceparent(tp string) EnvelopeOption {
	return func(e *Envelope) { e.Traceparent = tp }
}

// WithEnvelopeID overrides the minted envelope id (tests and replays only).
func WithEnvelopeID(id string) EnvelopeOption {
	return func(e *Envelope) { e.EnvelopeID = id }
}

func checkProtocol(p string) error {
	rest, ok := strings.CutPrefix(p, "a2a-jetstream/")
	if !ok {
		return &ProtocolError{Msg: fmt.Sprintf("unknown protocol %q", p)}
	}
	majorStr, _, ok := strings.Cut(rest, ".")
	if !ok {
		return &ProtocolError{Msg: fmt.Sprintf("malformed protocol version %q", p)}
	}
	major, err := strconv.Atoi(majorStr)
	if err != nil {
		return &ProtocolError{Msg: fmt.Sprintf("malformed protocol version %q", p)}
	}
	if major != protocolMajor {
		return &ProtocolError{Msg: fmt.Sprintf("unknown protocol major in %q (this library speaks %s)", p, Protocol)}
	}
	return nil
}

func isJSONNull(raw json.RawMessage) bool {
	return raw == nil || bytes.Equal(bytes.TrimSpace(raw), []byte("null"))
}

// validDNS1123Label reports whether s is a legal subject token: DNS-1123
// label shape - lowercase alphanumerics and '-', alphanumeric at both ends,
// at most 63 bytes. Dots are NATS token separators, so a dotted addressee or
// taskId changes the subject's token count under every wildcard filter.
func validDNS1123Label(s string) bool {
	if len(s) == 0 || len(s) > 63 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c >= 'a' && c <= 'z', c >= '0' && c <= '9':
		case c == '-':
			if i == 0 || i == len(s)-1 {
				return false
			}
		default:
			return false
		}
	}
	return true
}

// ValidSubjectToken reports whether s is a legal task-subject token — a
// dot-free DNS-1123 label. Exposed so components can validate configured
// addressees at boot instead of failing per-message at publish.
func ValidSubjectToken(s string) bool {
	return validDNS1123Label(s)
}

// taskScoped reports whether taskId/contextId are required for the kind.
func taskScoped(k Kind) bool {
	switch k {
	case KindMessage, KindStatusUpdate, KindArtifactUpdate, KindCancel:
		return true
	}
	return false
}

// ParseEnvelope decodes and validates an inbound envelope. Unknown envelope
// fields within the accepted major are ignored; an unknown protocol major or a
// kind/payload mismatch is a *ProtocolError.
func ParseEnvelope(raw []byte) (*Envelope, error) {
	var env Envelope
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil, &ProtocolError{Msg: fmt.Sprintf("malformed envelope: %v", err)}
	}
	if err := checkProtocol(env.Protocol); err != nil {
		return nil, err
	}
	if err := env.validateCommon(); err != nil {
		return nil, err
	}
	return &env, nil
}

// validateCommon holds the rules shared by inbound parse and outbound emit:
// required fields, per-kind taskId/contextId, and kind/payload agreement.
func (e *Envelope) validateCommon() error {
	switch {
	case e.EnvelopeID == "":
		return &ProtocolError{Msg: "missing envelopeId"}
	case e.CorrelationID == "":
		return &ProtocolError{Msg: "missing correlationId"}
	case e.TS.IsZero():
		return &ProtocolError{Msg: "missing ts"}
	case e.From.Session == "":
		return &ProtocolError{Msg: "missing from.session"}
	case e.Kind == "":
		return &ProtocolError{Msg: "missing kind"}
	}
	if taskScoped(e.Kind) {
		if e.TaskID == "" {
			return &ProtocolError{Msg: fmt.Sprintf("kind %q requires taskId", e.Kind)}
		}
		if !validDNS1123Label(e.TaskID) {
			return &ProtocolError{Msg: fmt.Sprintf("taskId %q is not a dot-free DNS-1123 label; it is a subject token", e.TaskID)}
		}
		if e.ContextID == "" {
			return &ProtocolError{Msg: fmt.Sprintf("kind %q requires contextId", e.Kind)}
		}
	}
	if e.Kind == KindAgentCard || e.Kind == KindAgentClosed {
		if e.TaskID != "" || e.ContextID != "" {
			return &ProtocolError{Msg: fmt.Sprintf("kind %q carries no taskId/contextId", e.Kind)}
		}
	}
	return validatePayload(e.Kind, e.TaskID, e.Payload)
}

// ValidateEmit checks an envelope is legal to publish: the common rules, plus
// the emit-only ones — protocol pinned to ours, identity never populated, and
// inline FileParts under the threshold.
func (e *Envelope) ValidateEmit() error {
	if e.Protocol == "" {
		return &ProtocolError{Msg: "missing protocol"}
	}
	// Emit is pinned to the exact version this library speaks; same-major
	// tolerance is an inbound rule only.
	if e.Protocol != Protocol {
		return &ProtocolError{Msg: fmt.Sprintf("emitting protocol %q; this library emits only %s", e.Protocol, Protocol)}
	}
	if err := e.validateCommon(); err != nil {
		return err
	}
	if !isJSONNull(e.Identity) {
		return &ProtocolError{Msg: "identity is reserved and MUST NOT be populated"}
	}
	return checkInlineFileParts(e.Kind, e.Payload)
}

func newEnvelope(kind Kind, from Party, taskID, contextID, correlationID string, payload json.RawMessage, opts []EnvelopeOption) (*Envelope, error) {
	env := &Envelope{
		Protocol:      Protocol,
		EnvelopeID:    "env-" + nuid.Next(),
		CorrelationID: correlationID,
		TaskID:        taskID,
		ContextID:     contextID,
		TS:            time.Now().UTC(),
		From:          from,
		Kind:          kind,
		Payload:       payload,
	}
	for _, opt := range opts {
		opt(env)
	}
	if err := env.ValidateEmit(); err != nil {
		return nil, err
	}
	return env, nil
}

// NewMessageEnvelope builds a kind:message envelope for a task — submission,
// follow-up input, or steering; they are the same shape.
func NewMessageEnvelope(from Party, taskID, contextID, correlationID string, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindMessage, from, taskID, contextID, correlationID, payload, opts)
}

// NewStatusUpdateEnvelope builds a kind:status-update envelope.
func NewStatusUpdateEnvelope(from Party, taskID, contextID, correlationID string, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindStatusUpdate, from, taskID, contextID, correlationID, payload, opts)
}

// NewArtifactUpdateEnvelope builds a kind:artifact-update envelope.
func NewArtifactUpdateEnvelope(from Party, taskID, contextID, correlationID string, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindArtifactUpdate, from, taskID, contextID, correlationID, payload, opts)
}

// NewCancelEnvelope builds a kind:cancel envelope. The envelope's taskId names
// the target; the payload is the empty object.
func NewCancelEnvelope(from Party, taskID, contextID, correlationID string, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindCancel, from, taskID, contextID, correlationID, json.RawMessage(`{}`), opts)
}

// NewAgentCardEnvelope builds the kind:agent-card envelope published on startup.
func NewAgentCardEnvelope(from Party, correlationID string, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindAgentCard, from, "", "", correlationID, payload, opts)
}

// NewAgentClosedEnvelope builds the shutdown tombstone that replaces the card.
func NewAgentClosedEnvelope(from Party, correlationID string, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindAgentClosed, from, "", "", correlationID, json.RawMessage(`{}`), opts)
}

// NewTopicUpdateEnvelope builds a kind:topic-update envelope. taskID and
// contextID are optional: present when the write happened in the course of a
// task, empty for scheduled runs.
func NewTopicUpdateEnvelope(from Party, taskID, contextID, correlationID string, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return newEnvelope(KindTopicUpdate, from, taskID, contextID, correlationID, payload, opts)
}

// NewChildTaskEnvelope builds the submission message for a task spawned in
// service of a parent task. The child inherits the parent's correlationId
// verbatim — never re-minted by an intermediary.
func NewChildTaskEnvelope(parent *Envelope, from Party, taskID, contextID string, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return NewMessageEnvelope(from, taskID, contextID, parent.CorrelationID, payload, opts...)
}

// NewFollowUpEnvelope builds a follow-up or steering message for a running
// task: same taskId and contextId, and the task's original correlationId —
// a steer is attributed by its own envelope and authority block, never by a
// new correlation (0.4 field rule).
func NewFollowUpEnvelope(origin *Envelope, from Party, payload json.RawMessage, opts ...EnvelopeOption) (*Envelope, error) {
	return NewMessageEnvelope(from, origin.TaskID, origin.ContextID, origin.CorrelationID, payload, opts...)
}
