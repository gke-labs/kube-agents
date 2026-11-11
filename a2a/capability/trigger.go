package capability

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"slices"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Trigger is the unattended trigger a grant binds (#4685): Name is
// "schedule/<name>" or a bare source, Origin is "builtin" (the one origin
// today: a user's schedule runs at its creator's tier with no trigger), and
// Stream is the report stream the grant binds, which can only be the
// schedule's own name, empty when it binds none. The
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

// The authority block's key for the claimed trigger, and the trigger's own keys.
const authorityTriggerKey = "trigger"

var triggerKeys = []string{"name", "origin", "stream"}

// TriggerFromAuthority pulls the trigger a submission claims out of an
// envelope's authority block. The gateway writes it there on a trigger's
// submission and nowhere else (that half is Track 2 of #4685, not yet built); it is a claim, not authority, until the
// verifier finds it equal to the grant's (profile.run). nil when the block
// names none (absent or null).
//
// It reads the block strictly, the way taskcred reads a credential: no key
// twice and no key that equals "trigger" (or one of the trigger's own keys)
// only under case folding, at either level, and no null inside the trigger.
// encoding/json would accept each of those, matching keys case-insensitively
// and letting the last duplicate win, so another reader of the same block
// could see a different trigger from the one the dispatcher checked. Anything
// malformed is an error, never read as none.
func TriggerFromAuthority(raw json.RawMessage) (*Trigger, error) {
	if len(raw) == 0 {
		return nil, nil
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if tok, err := dec.Token(); err != nil || tok != json.Delim('{') {
		return nil, fmt.Errorf("authority block is not a JSON object")
	}
	var found *Trigger
	seen := false
	for dec.More() {
		kt, err := dec.Token()
		if err != nil {
			return nil, fmt.Errorf("authority block: %w", err)
		}
		key, _ := kt.(string)
		if key != authorityTriggerKey {
			if strings.EqualFold(key, authorityTriggerKey) {
				return nil, fmt.Errorf("authority block carries %q, %q in another case", key, authorityTriggerKey)
			}
			var skip json.RawMessage
			if err := dec.Decode(&skip); err != nil {
				return nil, fmt.Errorf("authority block: %w", err)
			}
			continue
		}
		if seen {
			return nil, fmt.Errorf("authority block carries %q twice", authorityTriggerKey)
		}
		seen = true
		if found, err = readTrigger(dec); err != nil {
			return nil, fmt.Errorf("authority trigger: %w", err)
		}
	}
	if _, err := dec.Token(); err != nil {
		return nil, fmt.Errorf("authority block: %w", err)
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("authority block: trailing data")
	}
	return found, nil
}

// readTrigger reads the trigger value at dec: nil for null, else an object of
// exactly the trigger's keys, each once, each a string, in a valid shape.
func readTrigger(dec *json.Decoder) (*Trigger, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if tok == nil {
		return nil, nil
	}
	if tok != json.Delim('{') {
		return nil, fmt.Errorf("not a JSON object")
	}
	values := map[string]string{}
	for dec.More() {
		kt, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key, _ := kt.(string)
		if !slices.Contains(triggerKeys, key) {
			for _, k := range triggerKeys {
				if strings.EqualFold(key, k) {
					return nil, fmt.Errorf("key %q is %q in another case", key, k)
				}
			}
			return nil, fmt.Errorf("unknown key %q", key)
		}
		if _, dup := values[key]; dup {
			return nil, fmt.Errorf("key %q appears twice", key)
		}
		vt, err := dec.Token()
		if err != nil {
			return nil, err
		}
		v, ok := vt.(string)
		if !ok {
			return nil, fmt.Errorf("key %q is not a string", key)
		}
		values[key] = v
	}
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	t := &Trigger{Name: values["name"], Origin: values["origin"], Stream: values["stream"]}
	if err := t.validate(); err != nil {
		return nil, err
	}
	return t, nil
}
