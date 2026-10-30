package lib

import (
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
)

func TestSteerNoticeRoundTrips(t *testing.T) {
	in := SteerNotice{Steer: SteerRefused, EnvelopeID: "env-1", Reason: SteerReasonQueueFull}
	p, err := SteerNoticePart(in)
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != "data" {
		t.Fatalf("kind = %q, want data", p.Kind)
	}
	got, ok := SteerNoticeOf([]Part{{Kind: "text", Text: "not taken"}, p})
	if !ok || got != in {
		t.Fatalf("SteerNoticeOf = %+v, %v; want %+v", got, ok, in)
	}
}

func TestSteerNoticeOfIgnoresEverythingElse(t *testing.T) {
	for _, parts := range [][]Part{
		nil,
		{{Kind: "text", Text: `{"steerNotice":{"steer":"queued","envelopeId":"e"}}`}},
		{{Kind: "data", Data: json.RawMessage(`{"tool":"kubectl"}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"steerNotice":{"steer":"maybe","envelopeId":"e"}}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"steerNotice":{"steer":"queued"}}`)}},
		{{Kind: "data", Data: json.RawMessage(`[1,2]`)}},
	} {
		if n, ok := SteerNoticeOf(parts); ok {
			t.Errorf("%s read as a steer notice: %+v", mustJSON(t, parts), n)
		}
	}
}

// The request is exactly {"cardStop":{"v":1}}, whatever version the caller
// left in it, and reads back as a request.
func TestCardStopRequestRoundTrips(t *testing.T) {
	p, err := CardStopPart(CardStop{})
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != "data" || string(p.Data) != `{"cardStop":{"v":1}}` {
		t.Fatalf("request part = %s %s, want data {\"cardStop\":{\"v\":1}}", p.Kind, p.Data)
	}
	got, ok := CardStopOf([]Part{p})
	if !ok || got.V != CardStopVersion || got.CardStopResult != nil {
		t.Fatalf("CardStopOf = %+v, %v; want a v1 request", got, ok)
	}
	if !CardStopRequested([]Part{p}) {
		t.Fatal("CardStopRequested = false for the request alone")
	}
}

// The result round-trips beside its text, and an empty one still carries its
// lists, so it never reads as a request.
func TestCardStopResultRoundTrips(t *testing.T) {
	in := CardStop{CardStopResult: &CardStopResult{
		Stopped:    []CardStopCard{{ID: "t_1", Title: "check node pools", PriorStatus: "running"}},
		Settled:    []CardStopCard{{ID: "t_2", Title: "list clusters"}},
		Failed:     1,
		Pending:    2,
		Unresolved: 3,
		Truncated:  true,
		Deadline:   true,
		ListsCut:   true,
	}}
	p, err := CardStopPart(in)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := CardStopOf([]Part{{Kind: "text", Text: "stopped 1 card"}, p})
	if !ok || got.CardStopResult == nil {
		t.Fatalf("CardStopOf = %+v, %v; want the result", got, ok)
	}
	if got.V != CardStopVersion || mustJSON(t, got.CardStopResult) != mustJSON(t, in.CardStopResult) {
		t.Fatalf("result = %s, want %s", mustJSON(t, got), mustJSON(t, in))
	}
	if CardStopRequested([]Part{p}) {
		t.Fatal("CardStopRequested = true for a result")
	}

	empty, err := CardStopPart(CardStop{CardStopResult: &CardStopResult{}})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"cardStop":{"v":1,"stopped":[],"settled":[],"failed":0,"pending":0,"unresolved":0,"truncated":false,"deadline":false,"listsCut":false}}`
	if string(empty.Data) != want {
		t.Fatalf("empty result = %s, want %s", empty.Data, want)
	}
	if CardStopRequested([]Part{empty}) {
		t.Fatal("CardStopRequested = true for an empty result")
	}
}

// A message that asks something is a question: a text part beside the
// request, even a blank one, or a file part, makes it no control. Neither is
// another key, another version, or the request spelled in text.
func TestCardStopRequestedRefusesEverythingElse(t *testing.T) {
	req, err := CardStopPart(CardStop{})
	if err != nil {
		t.Fatal(err)
	}
	for _, parts := range [][]Part{
		nil,
		{{Kind: "text", Text: "stop"}, req},
		{req, {Kind: "text", Text: " "}},
		{req, {Kind: "file", File: &FileRef{URI: "gs://b/o"}}},
		{{Kind: "text", Text: `{"cardStop":{"v":1}}`}},
		{{Kind: "data", Data: json.RawMessage(`{"cardStop":{"v":2}}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"cardStop":{}}`)}},
		{{Kind: "data", Data: json.RawMessage(`{"steerNotice":{"steer":"queued","envelopeId":"e"}}`)}},
		{{Kind: "data", Data: json.RawMessage(`[1,2]`)}},
	} {
		if CardStopRequested(parts) {
			t.Errorf("%s read as a card-stop request", mustJSON(t, parts))
		}
	}
	// Another data part beside it is still data only.
	if !CardStopRequested([]Part{{Kind: "data", Data: json.RawMessage(`{"tool":"x"}`)}, req}) {
		t.Error("the request beside another data part was not read as one")
	}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestTaskParametersRoundTrip(t *testing.T) {
	in := TaskParameters{Parameters: map[string]string{"project": "acme-prod", "location": "us-east4", "cluster": "web-1"}}
	part, err := TaskParametersPart(in)
	if err != nil {
		t.Fatal(err)
	}
	got, ok, err := TaskParametersOf([]Part{{Kind: "text", Text: "hi"}, part})
	if err != nil || !ok || !reflect.DeepEqual(got, in) {
		t.Fatalf("TaskParametersOf = %+v, %v, %v; want %+v", got, ok, err, in)
	}
	if _, ok, err := TaskParametersOf([]Part{{Kind: "text", Text: "hi"}}); ok || err != nil {
		t.Errorf("no part: ok=%v err=%v, want neither", ok, err)
	}
	// An empty set is a task with no parameters, and goes out as an object.
	empty, err := TaskParametersPart(TaskParameters{})
	if err != nil || string(empty.Data) != `{"taskParameters":{"parameters":{}}}` {
		t.Errorf("empty part = %s, %v", empty.Data, err)
	}
}

// A value is joined into the verifier's resource path, so a '/' (or a space,
// or anything past one token) would let it name another resource: refused
// on the way out, and an error, not a skip, on the way in.
func TestTaskParametersRefuseWhatCouldForgeAResource(t *testing.T) {
	tooMany := map[string]string{}
	for i := 0; i <= TaskParametersMax; i++ {
		tooMany[fmt.Sprintf("p%d", i)] = "v"
	}
	for name, params := range map[string]map[string]string{
		"a slash in a value":  {"cluster": "web-1/../other"},
		"a dot step":          {"cluster": "."},
		"a parent step":       {"cluster": ".."},
		"a flag":              {"cluster": "-rf"},
		"a long flag":         {"cluster": "--kubeconfig=x"},
		"a space in a value":  {"cluster": "web 1"},
		"an empty value":      {"cluster": ""},
		"a long value":        {"cluster": strings.Repeat("a", TaskParameterValueMaxLen+1)},
		"an upper-case name":  {"Cluster": "web-1"},
		"a name with a dash":  {"target-cluster": "web-1"},
		"a long name":         {strings.Repeat("a", 64): "v"},
		"too many parameters": tooMany,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := TaskParametersPart(TaskParameters{Parameters: params}); err == nil {
				t.Error("TaskParametersPart accepted it")
			}
			data, _ := json.Marshal(map[string]TaskParameters{"taskParameters": {Parameters: params}})
			if _, ok, err := TaskParametersOf([]Part{{Kind: "data", Data: data}}); !ok || err == nil {
				t.Errorf("TaskParametersOf: ok=%v err=%v, want the part found and refused", ok, err)
			}
		})
	}
}

func TestChildResultRoundTrip(t *testing.T) {
	in := ChildResult{ChildTaskID: "task-7", Addressee: "cluster", State: StateCompleted, Text: "3 nodes ready"}
	part, err := ChildResultPart(in)
	if err != nil {
		t.Fatal(err)
	}
	got, ok := ChildResultOf([]Part{{Kind: "text", Text: "x"}, part})
	if !ok || got != in {
		t.Fatalf("ChildResultOf = %+v, %v; want %+v", got, ok, in)
	}
}

// A child result names a finished task: no id, a non-terminal state or text
// past the cap is refused on the way out and skipped on the way in.
func TestChildResultRefusesWhatIsNotAnEnd(t *testing.T) {
	for name, c := range map[string]ChildResult{
		"no task id":           {State: StateCompleted},
		"a task id with a dot": {ChildTaskID: "task.7", State: StateCompleted},
		"a working state":      {ChildTaskID: "task-7", State: StateWorking},
		"text past the cap":    {ChildTaskID: "task-7", State: StateCompleted, Text: strings.Repeat("a", ChildResultTextCap+1)},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ChildResultPart(c); err == nil {
				t.Error("ChildResultPart accepted it")
			}
			data, _ := json.Marshal(map[string]ChildResult{"childResult": c})
			if _, ok := ChildResultOf([]Part{{Kind: "data", Data: data}}); ok {
				t.Error("ChildResultOf read it")
			}
		})
	}
}

// A delegate request without parameters is the wire shape it always was,
// so an adapter or gateway built before this reads it unchanged.
func TestADelegateRequestWithoutParametersIsUnchangedOnTheWire(t *testing.T) {
	data, err := json.Marshal(DelegateRequest{Addressee: "platform", Text: "do it"})
	if err != nil || string(data) != `{"addressee":"platform","text":"do it"}` {
		t.Fatalf("wire = %s, %v", data, err)
	}
	var got DelegateRequest
	if err := json.Unmarshal([]byte(`{"addressee":"cluster","text":"t","parameters":{"cluster":"web-1"}}`), &got); err != nil ||
		got.Parameters["cluster"] != "web-1" {
		t.Fatalf("decoded %+v, %v", got, err)
	}
}

// A part that is there but carries no parameters object (null, or a
// misspelt field) is broken, not an empty set: an error, never a task run
// with no target.
func TestTaskParametersOfRefusesAPartWithNoParametersObject(t *testing.T) {
	for _, data := range []string{
		`{"taskParameters":null}`,
		`{"taskParameters":{"parameters":null}}`,
		`{"taskParameters":{"parameterz":{"cluster":"web-1"}}}`,
	} {
		if _, ok, err := TaskParametersOf([]Part{{Kind: "data", Data: json.RawMessage(data)}}); !ok || err == nil {
			t.Errorf("%s: ok=%v err=%v, want found and refused", data, ok, err)
		}
	}
}

// A name as long as the persona loader allows (63) is one a submission can
// carry: the two share TaskParameterNameRE, so a persona never declares a
// parameter its tasks cannot be handed.
func TestATaskParameterNameAsLongAsAPersonaAllowsIsValid(t *testing.T) {
	if err := ValidateTaskParameters(map[string]string{strings.Repeat("a", 63): "v"}); err != nil {
		t.Fatal(err)
	}
}

// Two taskParameters parts leave a task's target ambiguous: an error, never
// the first or the last quietly chosen.
func TestTaskParametersOfRefusesTwoParts(t *testing.T) {
	one, _ := TaskParametersPart(TaskParameters{Parameters: map[string]string{"cluster": "web-1"}})
	two, _ := TaskParametersPart(TaskParameters{Parameters: map[string]string{"cluster": "prod-1"}})
	if _, ok, err := TaskParametersOf([]Part{one, two}); !ok || err == nil {
		t.Fatalf("ok=%v err=%v, want found and refused", ok, err)
	}
}
