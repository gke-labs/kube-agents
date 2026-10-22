package stepmsg

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func TestPartRoundTrip(t *testing.T) {
	in := Step{
		Kind:    KindSubtask,
		Title:   "Health check completed: drift-peer-ap-1",
		Thought: "Cluster RUNNING, 0 pod restarts.",
		CallIDs: []string{"call_1", "call_2"},
		Subtask: &Subtask{ID: "t_abc", Title: "Health check for drift-peer-ap-1", Verdict: "healthy", Settled: 1, Total: 2},
	}
	p, err := in.Part()
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != "data" || len(p.Data) == 0 {
		t.Fatalf("Part() = %+v, want a data part", p)
	}
	var head map[string]json.RawMessage
	if err := json.Unmarshal(p.Data, &head); err != nil {
		t.Fatal(err)
	}
	var schema string
	if json.Unmarshal(head["schema"], &schema) != nil || schema != Schema {
		t.Fatalf("schema = %q, want %q", schema, Schema)
	}
	out, ok := Decode(p)
	if !ok {
		t.Fatal("Decode rejected the step it encoded")
	}
	if out.Kind != in.Kind || out.Title != in.Title || out.Thought != in.Thought || len(out.CallIDs) != 2 ||
		out.Subtask == nil || out.Subtask.ID != "t_abc" || out.Subtask.Total != 2 {
		t.Fatalf("Decode = %+v, want %+v", out, in)
	}
}

// The wire names are the contract a renderer reads, so they are pinned:
// "subtask", not a scheduler's own word for its unit of work.
func TestWireNames(t *testing.T) {
	p, err := Step{Kind: KindSubtask, Title: "x", Subtask: &Subtask{ID: "s1", ParentTaskID: "task-1"}}.Part()
	if err != nil {
		t.Fatal(err)
	}
	got := string(p.Data)
	for _, want := range []string{`"kind":"subtask"`, `"subtask":{`, `"parentTaskId":"task-1"`, `"schema":"kube-agents.step/v1"`} {
		if !strings.Contains(got, want) {
			t.Errorf("encoded step %s lacks %s", got, want)
		}
	}
}

func TestDecodeRejectsNonSteps(t *testing.T) {
	cases := map[string]lib.Part{
		"text part":             {Kind: "text", Text: "Listing GKE clusters.\tthought"},
		"other schema":          {Kind: "data", Data: json.RawMessage(`{"schema":"kube-agents.chat/v1","title":"x"}`)},
		"no title":              {Kind: "data", Data: json.RawMessage(`{"schema":"kube-agents.step/v1","title":"   "}`)},
		"malformed json":        {Kind: "data", Data: json.RawMessage(`{"schema":`)},
		"keys in another case":  {Kind: "data", Data: json.RawMessage(`{"Schema":"kube-agents.step/v1","TITLE":"x"}`)},
		"title in another case": {Kind: "data", Data: json.RawMessage(`{"schema":"kube-agents.step/v1","Title":"x"}`)},
	}
	for name, p := range cases {
		if s, ok := Decode(p); ok {
			t.Errorf("%s: Decode accepted %+v", name, s)
		}
	}
	s, ok := Decode(lib.Part{Kind: "data", Data: json.RawMessage(`{"schema":"kube-agents.step/v2","title":" Reading a file. "}`)})
	if !ok || s.Kind != KindPlan || s.Title != "Reading a file." {
		t.Fatalf("a later schema version with a title should decode as a plan step, got %+v ok=%v", s, ok)
	}
}

// A subtask's keys match exactly too.
func TestDecodeMatchesSubtaskKeysExactly(t *testing.T) {
	s, ok := Decode(lib.Part{Kind: "data", Data: json.RawMessage(
		`{"schema":"kube-agents.step/v1","kind":"subtask","title":"x","subtask":{"ID":"wrong","id":"s1"}}`)})
	if !ok || s.Subtask == nil || s.Subtask.ID != "s1" {
		t.Fatalf("got %+v ok=%v, want the exact-case id", s.Subtask, ok)
	}
}

// Part refuses what Decode would drop, rather than publish it silently.
func TestPartRefusesWhatDecodeWouldDrop(t *testing.T) {
	for name, s := range map[string]Step{
		"empty":          {},
		"blank title":    {Kind: KindPlan, Title: "  "},
		"unknown kind":   {Kind: "card", Title: "x"},
		"foreign schema": {Schema: "kube-agents.chat/v1", Kind: KindPlan, Title: "x"},
	} {
		if p, err := s.Part(); err == nil {
			t.Errorf("%s: Part encoded %s", name, p.Data)
		}
	}
}
