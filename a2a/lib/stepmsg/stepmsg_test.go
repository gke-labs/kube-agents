package stepmsg

import (
	"encoding/json"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

func TestPartRoundTrip(t *testing.T) {
	in := Step{
		Kind:    KindCard,
		Title:   "Specialist check completed: drift-peer-ap-1",
		Thought: "Cluster RUNNING, 0 pod restarts.",
		CallIDs: []string{"call_1", "call_2"},
		Card:    &Card{ID: "t_abc", Title: "Health check for drift-peer-ap-1", Verdict: "healthy", Settled: 1, Total: 2},
	}
	p := in.Part()
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
	if out.Kind != in.Kind || out.Title != in.Title || out.Thought != in.Thought || len(out.CallIDs) != 2 || out.Card == nil || out.Card.ID != "t_abc" || out.Card.Total != 2 {
		t.Fatalf("Decode = %+v, want %+v", out, in)
	}
}

func TestDecodeRejectsNonSteps(t *testing.T) {
	cases := map[string]lib.Part{
		"text part":      {Kind: "text", Text: "Listing GKE clusters.\tthought"},
		"other schema":   {Kind: "data", Data: json.RawMessage(`{"schema":"kube-agents.chat/v1","title":"x"}`)},
		"no title":       {Kind: "data", Data: json.RawMessage(`{"schema":"kube-agents.step/v1","title":"   "}`)},
		"malformed json": {Kind: "data", Data: json.RawMessage(`{"schema":`)},
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
