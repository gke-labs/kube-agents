// Package stepmsg is the kube-agents.step/v1 step record: the structure an
// executor publishes on the "steps" artifact so a renderer can list what a
// turn did - one row per planned action, specialist card outcome, or
// synthesis pass - without reading prose.
//
// The hermes-bridge publishes steps as it observes the model's API requests
// and the Kanban board settle; the kube-agents MCP server folds them into the
// step list an MCP client shows while a turn runs. Before this contract existed the
// bridge encoded the same facts into "thinking" text ("title\tthought" lines,
// "Specialist check completed: X (verdict - n/m cards done).") and the MCP
// server parsed them back out with regexes; "thinking" now carries the
// model's reasoning text only, as spec-a2a-payloads.md reserves it for.
//
// "steps" is not a reserved artifact name (the set of names is open); the
// relay ignores it, the fold keeps it beside the task's other artifacts, and
// a reader that does not know it skips it.
package stepmsg

import (
	"encoding/json"
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
	// KindPlan is one planned action: a tool call the model asked for, titled
	// by the executor from the call's name and arguments.
	KindPlan Kind = "plan"
	// KindCard is one specialist Kanban card reaching a settled status; Card
	// carries the card and the board's settled/total count at that moment.
	KindCard Kind = "card"
	// KindSynthesis is a pass that folds what came before into an answer: the
	// model's final response, or the card-results synthesis turn.
	KindSynthesis Kind = "synthesis"
)

// Card is the specialist card a KindCard step reports, or the card a
// task-card-* task stands for (then ParentTaskID names the foreground task
// whose turn created it).
type Card struct {
	ID           string `json:"id"`
	Title        string `json:"title,omitempty"`
	Assignee     string `json:"assignee,omitempty"`
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
	Card    *Card    `json:"card,omitempty"`
}

// Part encodes the step as the data part it travels as. The schema key is
// filled in when the caller left it empty.
func (s Step) Part() lib.Part {
	if s.Schema == "" {
		s.Schema = Schema
	}
	raw, err := json.Marshal(s)
	if err != nil {
		return lib.Part{}
	}
	return lib.Part{Kind: partKindData, Data: raw}
}

// Decode reads a step record off a data part. It returns false for a part
// that is not a step record: a text part, a data part of another schema, or
// one without a title.
func Decode(p lib.Part) (Step, bool) {
	if p.Kind != partKindData || len(p.Data) == 0 {
		return Step{}, false
	}
	var s Step
	if err := json.Unmarshal(p.Data, &s); err != nil {
		return Step{}, false
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
