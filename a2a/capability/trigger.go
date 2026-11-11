package capability

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Trigger is the unattended trigger a grant binds (#4685): Name is
// "schedule/<name>" or a bare source, Origin is "builtin" or "user", and
// Stream is the report stream the grant binds, empty when it binds none. The
// same shape travels in the envelope's authority block (TriggerFromAuthority)
// and in the task credential the dispatcher signs (taskcred.Trigger).
type Trigger struct {
	Name   string `json:"name"`
	Origin string `json:"origin"`
	Stream string `json:"stream,omitempty"`
}

func (t *Trigger) validate() error {
	return lib.ValidateTrigger(t.Name, t.Origin, t.Stream)
}

// equal reports whether t and o name the same trigger, nil equal only to nil.
func (t *Trigger) equal(o *Trigger) bool {
	if t == nil || o == nil {
		return t == nil && o == nil
	}
	return *t == *o
}

// strictUnmarshal decodes one JSON object into v, refusing unknown fields and
// trailing data.
func strictUnmarshal(raw []byte, v any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(v); err != nil {
		return err
	}
	if dec.More() {
		return fmt.Errorf("trailing data")
	}
	return nil
}

// authorityTrigger is the one field TriggerFromAuthority reads.
type authorityTrigger struct {
	Trigger json.RawMessage `json:"trigger"`
}

// TriggerFromAuthority pulls the trigger a submission claims out of an
// envelope's authority block. The gateway writes it there on a trigger's
// submission and nowhere else; it is a claim, not authority, until the
// verifier finds it equal to the grant's (profile.run). nil when the block
// names none (absent or null). A trigger that is there but malformed, or
// carries a key this reader doesn't know, is an error: never read as none.
func TriggerFromAuthority(raw json.RawMessage) (*Trigger, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	var block authorityTrigger
	if err := json.Unmarshal(raw, &block); err != nil {
		return nil, err
	}
	if len(block.Trigger) == 0 || string(block.Trigger) == "null" {
		return nil, nil
	}
	var t Trigger
	if err := strictUnmarshal(block.Trigger, &t); err != nil {
		return nil, fmt.Errorf("authority trigger: %w", err)
	}
	if err := t.validate(); err != nil {
		return nil, fmt.Errorf("authority %w", err)
	}
	return &t, nil
}
