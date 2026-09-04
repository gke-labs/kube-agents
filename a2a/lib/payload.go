package lib

import (
	"encoding/json"
	"fmt"
	"strings"
)

// TaskState is the A2A task state machine, plus rejected. auth-required is
// reserved alongside the authority field.
type TaskState string

const (
	StateSubmitted     TaskState = "submitted"
	StateWorking       TaskState = "working"
	StateInputRequired TaskState = "input-required"
	StateCompleted     TaskState = "completed"
	StateFailed        TaskState = "failed"
	StateCanceled      TaskState = "canceled"
	StateRejected      TaskState = "rejected"
	StateAuthRequired  TaskState = "auth-required"
)

// Terminal reports whether the state ends the task.
func (s TaskState) Terminal() bool {
	switch s {
	case StateCompleted, StateFailed, StateCanceled, StateRejected:
		return true
	}
	return false
}

func validTaskState(s TaskState) bool {
	switch s {
	case StateSubmitted, StateWorking, StateInputRequired, StateCompleted,
		StateFailed, StateCanceled, StateRejected, StateAuthRequired:
		return true
	}
	return false
}

// Reserved artifact names. The set of names is open; only these four carry
// reserved semantics.
const (
	ArtifactResult   = "result"
	ArtifactThinking = "thinking"
	ArtifactActivity = "activity"
	ArtifactProgress = "progress"
)

// The A2A object shapes below carry only the fields the library consults.
// Payloads travel as raw bytes end to end (assertion 6); these views are for
// validation and folding, never re-serialized onto the wire.

// Part is one typed A2A message/artifact part.
type Part struct {
	Kind string          `json:"kind"`
	Text string          `json:"text,omitempty"`
	Data json.RawMessage `json:"data,omitempty"`
	File *FileRef        `json:"file,omitempty"`
}

// FileRef is the file half of a FilePart: inline bytes or a uri, never both
// required. Inline bytes over InlineFileThreshold are refused at emit.
type FileRef struct {
	Name     string `json:"name,omitempty"`
	MimeType string `json:"mimeType,omitempty"`
	Bytes    string `json:"bytes,omitempty"`
	URI      string `json:"uri,omitempty"`
}

// Message is the A2A Message object.
type Message struct {
	Role      string `json:"role"`
	Parts     []Part `json:"parts"`
	MessageID string `json:"messageId"`
	TaskID    string `json:"taskId,omitempty"`
	ContextID string `json:"contextId,omitempty"`
}

// TaskStatus is the status half of a status-update.
type TaskStatus struct {
	State   TaskState `json:"state"`
	Message *Message  `json:"message,omitempty"`
	TS      string    `json:"timestamp,omitempty"`
}

// StatusUpdate is the A2A TaskStatusUpdateEvent.
type StatusUpdate struct {
	TaskID    string     `json:"taskId"`
	ContextID string     `json:"contextId"`
	Status    TaskStatus `json:"status"`
	Final     bool       `json:"final"`
}

// Artifact is the A2A Artifact object.
type Artifact struct {
	ArtifactID string `json:"artifactId,omitempty"`
	Name       string `json:"name,omitempty"`
	Parts      []Part `json:"parts"`
}

// ArtifactUpdate is the A2A TaskArtifactUpdateEvent.
type ArtifactUpdate struct {
	TaskID    string   `json:"taskId"`
	ContextID string   `json:"contextId"`
	Artifact  Artifact `json:"artifact"`
	Append    bool     `json:"append,omitempty"`
	LastChunk bool     `json:"lastChunk,omitempty"`
}

// AgentCard carries only the field validation needs.
type AgentCard struct {
	Name string `json:"name"`
}

func protocolErrf(format string, args ...any) error {
	return &ProtocolError{Msg: fmt.Sprintf(format, args...)}
}

// validatePayload enforces kind/payload agreement (assertion 7), including
// the payload's taskId agreeing with the envelope's: an event whose payload
// names another task is poison to the fold, so it is refused at emit and at
// parse rather than surviving to break tasks/get. A mismatch is a protocol
// error, never passed through.
func validatePayload(kind Kind, taskID string, payload json.RawMessage) error {
	if isJSONNull(payload) && kind != KindCancel && kind != KindAgentClosed {
		return protocolErrf("kind %q requires a payload", kind)
	}
	switch kind {
	case KindMessage:
		var m Message
		if err := json.Unmarshal(payload, &m); err != nil {
			return protocolErrf("kind message: malformed payload: %v", err)
		}
		if m.Role != "user" && m.Role != "agent" {
			return protocolErrf("kind message: payload is not an A2A Message (role %q)", m.Role)
		}
		if m.MessageID == "" {
			return protocolErrf("kind message: payload missing messageId")
		}
		if len(m.Parts) == 0 {
			return protocolErrf("kind message: payload has no parts")
		}
		if m.TaskID != "" && m.TaskID != taskID {
			return protocolErrf("kind message: payload names task %q but the envelope carries taskId %q", m.TaskID, taskID)
		}
		return validateParts(m.Parts, "message")
	case KindStatusUpdate:
		var s StatusUpdate
		if err := json.Unmarshal(payload, &s); err != nil {
			return protocolErrf("kind status-update: malformed payload: %v", err)
		}
		if s.Status.State == "" {
			return protocolErrf("kind status-update: payload is not a TaskStatusUpdateEvent (no status.state)")
		}
		if !validTaskState(s.Status.State) {
			return protocolErrf("kind status-update: unknown task state %q", s.Status.State)
		}
		if s.Final && !s.Status.State.Terminal() {
			return protocolErrf("kind status-update: final=true with non-terminal state %q", s.Status.State)
		}
		if s.TaskID != "" && s.TaskID != taskID {
			return protocolErrf("kind status-update: payload names task %q but the envelope carries taskId %q", s.TaskID, taskID)
		}
		if s.Status.Message != nil {
			if err := validateParts(s.Status.Message.Parts, "status-update message"); err != nil {
				return err
			}
		}
	case KindArtifactUpdate:
		var a ArtifactUpdate
		if err := json.Unmarshal(payload, &a); err != nil {
			return protocolErrf("kind artifact-update: malformed payload: %v", err)
		}
		if len(a.Artifact.Parts) == 0 {
			return protocolErrf("kind artifact-update: payload is not a TaskArtifactUpdateEvent (artifact has no parts)")
		}
		if a.TaskID != "" && a.TaskID != taskID {
			return protocolErrf("kind artifact-update: payload names task %q but the envelope carries taskId %q", a.TaskID, taskID)
		}
		return validateParts(a.Artifact.Parts, "artifact-update")
	case KindCancel, KindAgentClosed:
		if !isEmptyObject(payload) {
			return protocolErrf("kind %q carries an empty payload", kind)
		}
	case KindAgentCard:
		var c AgentCard
		if err := json.Unmarshal(payload, &c); err != nil {
			return protocolErrf("kind agent-card: malformed payload: %v", err)
		}
		if c.Name == "" {
			return protocolErrf("kind agent-card: payload is not an AgentCard (no name)")
		}
	case KindTopicUpdate:
		var a Artifact
		if err := json.Unmarshal(payload, &a); err != nil {
			return protocolErrf("kind topic-update: malformed payload: %v", err)
		}
		if a.Name == "" {
			return protocolErrf("kind topic-update: payload is not an Artifact with a topic name")
		}
		if strings.Contains(a.Name, ".") {
			return protocolErrf("kind topic-update: topic token %q contains a dot", a.Name)
		}
		if len(a.Parts) == 0 {
			return protocolErrf("kind topic-update: artifact has no parts")
		}
		return validateParts(a.Parts, "topic-update")
	default:
		return protocolErrf("unknown kind %q", kind)
	}
	return nil
}

func isEmptyObject(payload json.RawMessage) bool {
	if isJSONNull(payload) {
		return true
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(payload, &m); err != nil {
		return false
	}
	return len(m) == 0
}

func validateParts(parts []Part, where string) error {
	for i, p := range parts {
		switch p.Kind {
		case "text", "data":
		case "file":
			if p.File == nil {
				return protocolErrf("%s part %d: file part without file", where, i)
			}
			if p.File.Bytes == "" && p.File.URI == "" {
				return protocolErrf("%s part %d: file part with neither bytes nor uri", where, i)
			}
		default:
			return protocolErrf("%s part %d: unknown part kind %q", where, i, p.Kind)
		}
	}
	return nil
}

// checkInlineFileParts refuses inline FileParts whose decoded size exceeds
// InlineFileThreshold; those MUST use uri backed by the object store.
func checkInlineFileParts(kind Kind, payload json.RawMessage) error {
	var parts []Part
	switch kind {
	case KindMessage:
		var m Message
		if err := json.Unmarshal(payload, &m); err != nil {
			return nil // shape already validated; nothing to scan
		}
		parts = m.Parts
	case KindStatusUpdate:
		var s StatusUpdate
		if err := json.Unmarshal(payload, &s); err != nil {
			return nil
		}
		if s.Status.Message == nil {
			return nil
		}
		parts = s.Status.Message.Parts
	case KindArtifactUpdate:
		var a ArtifactUpdate
		if err := json.Unmarshal(payload, &a); err != nil {
			return nil
		}
		parts = a.Artifact.Parts
	case KindTopicUpdate:
		var a Artifact
		if err := json.Unmarshal(payload, &a); err != nil {
			return nil
		}
		parts = a.Parts
	default:
		return nil
	}
	for i, p := range parts {
		if p.Kind == "file" && p.File != nil && p.File.Bytes != "" {
			if base64DecodedSize(p.File.Bytes) > InlineFileThreshold {
				return &A2AError{
					Code: CodeInvalidParams,
					Message: fmt.Sprintf("part %d: inline FilePart exceeds %d-byte threshold; use uri via the object store",
						i, InlineFileThreshold),
				}
			}
		}
	}
	return nil
}

// base64DecodedSize is the exact decoded size of a padded std-base64 string —
// DecodedLen overestimates by up to two bytes, which would refuse a file of
// exactly the threshold (the spec bans only files above it).
func base64DecodedSize(s string) int {
	n := len(s) / 4 * 3
	if strings.HasSuffix(s, "==") {
		n -= 2
	} else if strings.HasSuffix(s, "=") {
		n--
	}
	return n
}
