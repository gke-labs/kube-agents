// Package stepmsg is the kube-agents.step/v1 step record: the structure an
// executor publishes on the "steps" artifact so a renderer can list what a
// turn did - one row per planned action, settled subtask, or synthesis pass -
// without reading prose.
//
// An executor may publish steps as it observes the model's requests and its
// subtasks settle; a client that renders them folds them into the step list
// it shows while a turn runs. Without this contract the same facts travel as "thinking" text
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
	// partKindData is the A2A part kind step records travel on.
	partKindData = "data"
	// subtaskKey is the record's key for its subtask, read on its own.
	subtaskKey = "subtask"
	// nullJSON is an explicit JSON null, which is no subtask.
	nullJSON = "null"

	// Size bounds on what a step carries. A step is a row, not a report, so
	// a record over them is refused on both sides rather than trimmed.
	maxTitleBytes   = 256
	maxThoughtBytes = 4096
	maxCallIDs      = 64
	maxIDBytes      = 128
	maxWordBytes    = 64
	maxSummaryBytes = 4096
)

// stepKeys and subtaskKeys are the record's keys, matched exactly. The
// subtask is read on its own, so its keys match exactly too.
var (
	stepKeys    = []string{"schema", "kind", "title", "thought", "callIds"}
	subtaskKeys = []string{"id", "title", "status", "verdict", "summary", "settled", "total", "parentTaskId"}
)

// Kind says what a step row stands for.
type Kind string

const (
	// KindPlan is one planned action: a tool call the model asked for. The
	// executor titles it; one that publishes structure only uses the tool's
	// name and never builds a title from a call's arguments.
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

// validate is the one rule Part and Decode share, so a step Part emits is
// one Decode reads and the reverse.
func (s Step) validate() error {
	if s.Schema != Schema {
		return fmt.Errorf("stepmsg: schema %q is not %s", s.Schema, Schema)
	}
	if s.Title == "" {
		return errors.New("stepmsg: a step needs a title")
	}
	if len(s.Title) > maxTitleBytes || len(s.Thought) > maxThoughtBytes {
		return fmt.Errorf("stepmsg: title over %d bytes or thought over %d", maxTitleBytes, maxThoughtBytes)
	}
	if len(s.CallIDs) > maxCallIDs {
		return fmt.Errorf("stepmsg: more than %d call ids", maxCallIDs)
	}
	for _, id := range s.CallIDs {
		if id == "" || len(id) > maxIDBytes {
			return fmt.Errorf("stepmsg: a call id is empty or over %d bytes", maxIDBytes)
		}
	}
	switch s.Kind {
	case KindPlan, KindSynthesis:
		if s.Subtask != nil {
			return fmt.Errorf("stepmsg: a %s step carries no subtask", s.Kind)
		}
	case KindSubtask:
		st := s.Subtask
		if st == nil || st.ID == "" {
			return errors.New("stepmsg: a subtask step needs its subtask's id")
		}
		if st.Settled < 0 || st.Total < 0 || (st.Total > 0 && st.Settled > st.Total) {
			return fmt.Errorf("stepmsg: settled %d of %d is not a count", st.Settled, st.Total)
		}
		if len(st.ID) > maxIDBytes || len(st.ParentTaskID) > maxIDBytes || len(st.Title) > maxTitleBytes ||
			len(st.Status) > maxWordBytes || len(st.Verdict) > maxWordBytes || len(st.Summary) > maxSummaryBytes {
			return fmt.Errorf("stepmsg: a subtask field is over its bound (ids %d, title %d, status and verdict %d, summary %d bytes)",
				maxIDBytes, maxTitleBytes, maxWordBytes, maxSummaryBytes)
		}
	default:
		return fmt.Errorf("stepmsg: kind %q is not plan, subtask or synthesis", s.Kind)
	}
	return nil
}

// Part encodes the step as the data part it travels as. The schema key is
// filled in when the caller left it empty. A step Decode would refuse is an
// error rather than a part that every reader drops silently.
func (s Step) Part() (lib.Part, error) {
	if s.Schema == "" {
		s.Schema = Schema
	}
	s.Title = strings.TrimSpace(s.Title)
	s.Thought = strings.TrimSpace(s.Thought)
	if err := s.validate(); err != nil {
		return lib.Part{}, err
	}
	raw, err := json.Marshal(s)
	if err != nil {
		return lib.Part{}, err
	}
	return lib.Part{Kind: partKindData, Data: raw}, nil
}

// decodeExact unmarshals the object raw into v through only the keys named,
// matched exactly: encoding/json would match a struct field to "Schema" or
// "TITLE" too, and the other schema readers here (chatmsg, the fold) match
// keys exactly. It returns the whole object for the caller's other keys.
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
	return all, json.Unmarshal(clean, v)
}

// Decode reads a step record off a data part. It returns false for anything
// that is not a valid v1 step record: a text part, another schema, a later
// version of this one (which is a new schema, as chatmsg treats its own), an
// unknown kind, or a record Part would refuse. Keys match exactly at both
// levels. A record with no kind reads as a plan step.
func Decode(p lib.Part) (Step, bool) {
	if p.Kind != partKindData || len(p.Data) == 0 {
		return Step{}, false
	}
	var s Step
	all, err := decodeExact(p.Data, stepKeys, &s)
	if err != nil {
		return Step{}, false
	}
	if raw, ok := all[subtaskKey]; ok && string(raw) != nullJSON {
		var st Subtask
		if _, err := decodeExact(raw, subtaskKeys, &st); err != nil {
			return Step{}, false
		}
		s.Subtask = &st
	}
	s.Title = strings.TrimSpace(s.Title)
	s.Thought = strings.TrimSpace(s.Thought)
	if s.Kind == "" {
		s.Kind = KindPlan
	}
	if s.validate() != nil {
		return Step{}, false
	}
	return s, true
}

// IsSynthesis reports whether the step is a synthesis pass, by kind.
func (s Step) IsSynthesis() bool {
	return s.Kind == KindSynthesis
}
