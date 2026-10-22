// Package stepmsg is the kube-agents.step/v1 step record: the structure an
// executor publishes on the "steps" artifact so a renderer can list what a
// turn did - one row per planned action, settled subtask, or synthesis pass -
// without reading prose.
//
// An executor publishes steps as it observes the model's requests and its
// subtasks settle; a client folds them into the step list it shows while a
// turn runs. Without this contract the same facts travel as "thinking" text
// that a renderer has to parse back out, while spec-a2a-payloads.md reserves
// "thinking" for the model's reasoning.
//
// "steps" is not a reserved artifact name (the set of names is open); the
// relay ignores it, the fold keeps it beside the task's other artifacts, and
// a reader that does not know it skips it.
package stepmsg

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

const (
	// ArtifactName is the artifact the step records are published under.
	ArtifactName = "steps"
	// Schema is the value of a step record's "schema" key.
	Schema = "kube-agents.step/v1"
	// schemaFamily prefixes every version of the schema; a data part whose
	// "schema" starts with it is a step record.
	schemaFamily = "kube-agents.step/"
	// partKindData is the A2A part kind step records travel on.
	partKindData = "data"
)

// Kind says what a step row stands for.
type Kind string

const (
	// KindPlan is one planned action: a tool call the model asked for. The
	// executor titles it; one that publishes structure only, as the Hermes
	// bridge does, uses the tool's name and never builds a title from a
	// call's arguments.
	KindPlan Kind = "plan"
	// KindSubtask is one subtask the turn started reaching a settled status;
	// Subtask carries it and how many of the turn's subtasks had settled.
	KindSubtask Kind = "subtask"
	// KindSynthesis is a pass that folds what came before into an answer: the
	// model's final response, or a turn that combines subtask results.
	KindSynthesis Kind = "synthesis"
)

// Subtask is the subtask a KindSubtask step reports. ParentTaskID names the
// task whose turn started it, when the subtask runs as a task of its own.
type Subtask struct {
	ID           string `json:"id"`
	Title        string `json:"title,omitempty"`
	Status       string `json:"status,omitempty"`
	Verdict      string `json:"verdict,omitempty"`
	Summary      string `json:"summary,omitempty"`
	Settled      int    `json:"settled,omitempty"`
	Total        int    `json:"total,omitempty"`
	ParentTaskID string `json:"parentTaskId,omitempty"`
}

// Step is one step record. Title is the row; Thought is the reasoning the
// model stated for it, when it stated any; CallIDs are the tool calls the
// step planned, so a renderer places each activity entry under the step that
// asked for it instead of guessing from titles.
type Step struct {
	Schema  string   `json:"schema"`
	Kind    Kind     `json:"kind"`
	Title   string   `json:"title"`
	Thought string   `json:"thought,omitempty"`
	CallIDs []string `json:"callIds,omitempty"`
	Subtask *Subtask `json:"subtask,omitempty"`
}

// Part encodes the step as the data part it travels as. The schema key is
// filled in when the caller left it empty. A step Decode would refuse (no
// title, an unknown kind, a schema outside the family) is an error rather
// than a part that every reader drops silently.
func (s Step) Part() (lib.Part, error) {
	if s.Schema == "" {
		s.Schema = Schema
	}
	if !strings.HasPrefix(s.Schema, schemaFamily) {
		return lib.Part{}, fmt.Errorf("stepmsg: schema %q is not a %s* schema", s.Schema, schemaFamily)
	}
	if strings.TrimSpace(s.Title) == "" {
		return lib.Part{}, errors.New("stepmsg: a step needs a title")
	}
	switch s.Kind {
	case KindPlan, KindSubtask, KindSynthesis:
	default:
		return lib.Part{}, fmt.Errorf("stepmsg: kind %q is not plan, subtask or synthesis", s.Kind)
	}
	raw, err := json.Marshal(s)
	if err != nil {
		return lib.Part{}, err
	}
	return lib.Part{Kind: partKindData, Data: raw}, nil
}

// stepKeys and subtaskKeys are the record's keys, matched exactly.
var (
	stepKeys    = []string{"schema", "kind", "title", "thought", "callIds", "subtask"}
	subtaskKeys = []string{"id", "title", "status", "verdict", "summary", "settled", "total", "parentTaskId"}
)

// decodeExact unmarshals the object raw into v through only the keys named,
// matched exactly: encoding/json would match a struct field to "Schema" or
// "TITLE" too, and the other schema readers here (chatmsg, the fold) match
// keys exactly. Other keys are a later version's, and skipped.
func decodeExact(raw json.RawMessage, keys []string, v any) (map[string]json.RawMessage, error) {
	var all map[string]json.RawMessage
	if err := json.Unmarshal(raw, &all); err != nil {
		return nil, err
	}
	kept := map[string]json.RawMessage{}
	for _, k := range keys {
		if val, ok := all[k]; ok {
			kept[k] = val
		}
	}
	clean, err := json.Marshal(kept)
	if err != nil {
		return nil, err
	}
	return kept, json.Unmarshal(clean, v)
}

// Decode reads a step record off a data part. It returns false for a part
// that is not a step record: a text part, a data part of another schema, or
// one without a title. Keys match exactly. A record with no kind reads as a
// plan step.
func Decode(p lib.Part) (Step, bool) {
	if p.Kind != partKindData || len(p.Data) == 0 {
		return Step{}, false
	}
	var s Step
	kept, err := decodeExact(p.Data, stepKeys, &s)
	if err != nil {
		return Step{}, false
	}
	s.Subtask = nil
	if raw, ok := kept["subtask"]; ok && string(raw) != "null" {
		var st Subtask
		if _, err := decodeExact(raw, subtaskKeys, &st); err != nil {
			return Step{}, false
		}
		s.Subtask = &st
	}
	if !strings.HasPrefix(s.Schema, schemaFamily) {
		return Step{}, false
	}
	s.Title = strings.TrimSpace(s.Title)
	s.Thought = strings.TrimSpace(s.Thought)
	if s.Title == "" {
		return Step{}, false
	}
	if s.Kind == "" {
		s.Kind = KindPlan
	}
	return s, true
}

// IsSynthesis reports whether the step is a synthesis pass, by kind.
func (s Step) IsSynthesis() bool {
	return s.Kind == KindSynthesis
}
