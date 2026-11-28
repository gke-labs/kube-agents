package lib

import (
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"
	"unicode/utf8"
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

// A batch entry that never got a child ends with its entry and no task id,
// failed, and reads back the same.
func TestChildResultForAnEntryThatNeverStarted(t *testing.T) {
	in := ChildResult{Entry: 3, Addressee: "cluster", State: StateFailed, Text: "not started"}
	part, err := ChildResultPart(in)
	if err != nil {
		t.Fatal(err)
	}
	if got, ok := ChildResultOf([]Part{part}); !ok || got != in {
		t.Fatalf("ChildResultOf = %+v, %v; want %+v", got, ok, in)
	}
	withID := ChildResult{ChildTaskID: "task-7", Entry: 2, State: StateCanceled}
	part, err = ChildResultPart(withID)
	if err != nil {
		t.Fatal(err)
	}
	if got, ok := ChildResultOf([]Part{part}); !ok || got != withID {
		t.Fatalf("ChildResultOf = %+v, %v; want %+v", got, ok, withID)
	}
}

// A single delegation's result carries no entry on the wire, so a reader
// built before entries reads it unchanged.
func TestASingleChildResultCarriesNoEntry(t *testing.T) {
	part, err := ChildResultPart(ChildResult{ChildTaskID: "task-7", Addressee: "cluster", State: StateCompleted})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(part.Data), "entry") {
		t.Fatalf("wire = %s", part.Data)
	}
}

// A child result names a finished task: no id, a non-terminal state or text
// past the cap is refused on the way out and skipped on the way in.
func TestChildResultRefusesWhatIsNotAnEnd(t *testing.T) {
	for name, c := range map[string]ChildResult{
		"no task id":                  {State: StateCompleted},
		"no task id, completed entry": {Entry: 1, State: StateCompleted},
		"a negative entry":            {ChildTaskID: "task-7", Entry: -1, State: StateCompleted},
		"an entry past the batch cap": {Entry: DelegateBatchMax + 1, State: StateFailed},
		"a task id with a dot":        {ChildTaskID: "task.7", State: StateCompleted},
		"a working state":             {ChildTaskID: "task-7", State: StateWorking},
		"text past the cap":           {ChildTaskID: "task-7", State: StateCompleted, Text: strings.Repeat("a", ChildResultTextCap+1)},
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

// changeRequested is omitted when false, so a submission that asks for no
// change is byte-identical to one written before the field existed, and an
// absent field reads as false.
func TestTaskParametersChangeRequestedRoundTrip(t *testing.T) {
	off, err := TaskParametersPart(TaskParameters{})
	if err != nil || string(off.Data) != `{"taskParameters":{"parameters":{}}}` {
		t.Fatalf("no change: part = %s, %v", off.Data, err)
	}
	on, err := TaskParametersPart(TaskParameters{ChangeRequested: true})
	if err != nil || string(on.Data) != `{"taskParameters":{"parameters":{},"changeRequested":true}}` {
		t.Fatalf("change: part = %s, %v", on.Data, err)
	}
	for _, tc := range []struct {
		part Part
		want bool
	}{{off, false}, {on, true}} {
		got, ok, err := TaskParametersOf([]Part{tc.part})
		if err != nil || !ok || got.ChangeRequested != tc.want {
			t.Errorf("TaskParametersOf(%s) = %+v, %v, %v; want changeRequested %v", tc.part.Data, got, ok, err, tc.want)
		}
	}
}

func TestDelegateTargetsRoundTrip(t *testing.T) {
	want := DelegateTargets{Targets: []DelegateTarget{
		{Name: "platform", Description: "the platform agent"},
		{Name: "e2e5", Description: "reads one cluster's workloads"},
	}}
	part, err := DelegateTargetsPart(want)
	if err != nil {
		t.Fatal(err)
	}
	text := Part{Kind: "text", Text: "use e2e5"}
	got, ok, err := DelegateTargetsOf([]Part{text, part})
	if err != nil || !ok {
		t.Fatalf("DelegateTargetsOf = %v %v", ok, err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
	if _, ok, err := DelegateTargetsOf([]Part{text}); ok || err != nil {
		t.Fatalf("no part: ok=%v err=%v, want absent", ok, err)
	}
}

func TestScopeClustersRoundTrip(t *testing.T) {
	want := ScopeClusters{Count: 3, Truncated: true, Clusters: []ScopeCluster{
		{Project: "proj-a", Location: "us-central1", Cluster: "prod-1"},
		{Project: "proj-b", Location: "europe-west1-b", Cluster: "edge"},
	}}
	part, err := ScopeClustersPart(want)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(part.Data), `"cursor":""`) {
		t.Errorf("part = %s, want the reserved cursor present and empty", part.Data)
	}
	text := Part{Kind: "text", Text: "sweep the fleet"}
	got, ok, err := ScopeClustersOf([]Part{text, part})
	if err != nil || !ok {
		t.Fatalf("ScopeClustersOf = %v %v", ok, err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v, want %+v", got, want)
	}
	if _, ok, err := ScopeClustersOf([]Part{text}); ok || err != nil {
		t.Fatalf("no part: ok=%v err=%v, want absent", ok, err)
	}
	empty, err := ScopeClustersPart(ScopeClusters{})
	if err != nil || string(empty.Data) != `{"scopeClusters":{"count":0,"clusters":[],"truncated":false,"cursor":""}}` {
		t.Fatalf("empty page = %s, %v", empty.Data, err)
	}
	if _, ok, err := ScopeClustersOf([]Part{part, part}); !ok || err == nil {
		t.Errorf("two parts: ok %v err %v, want found and refused", ok, err)
	}
}

// The bounds hold both ways: a bad shape is refused on the way out, and is
// an error, not a skip, on the way in.
func TestScopeClustersBounds(t *testing.T) {
	one := ScopeCluster{Project: "p", Location: "l", Cluster: "c"}
	page := func(n int) []ScopeCluster {
		out := make([]ScopeCluster, n)
		for i := range out {
			out[i] = ScopeCluster{Project: "p", Location: "l", Cluster: "c" + strconv.Itoa(i)}
		}
		return out
	}
	long := strings.Repeat("a", 63)
	widest := make([]ScopeCluster, ScopeClustersPageMax)
	for i := range widest {
		widest[i] = ScopeCluster{Project: long, Location: long, Cluster: long}
	}
	// The widest page and unlisted list of the longest labels fit the byte
	// bound, so the byte bound never cuts a page the other rules allow.
	unlisted := make([]string, ScopeClustersUnlistedMax)
	for i := range unlisted {
		unlisted[i] = long
	}
	if _, err := ScopeClustersPart(ScopeClusters{Count: 1 << 20, Truncated: true, Clusters: widest,
		Unknown: true, UnlistedProjects: unlisted, UnlistedCount: 1 << 20, OldestListing: "2026-11-23T10:00:00Z"}); err != nil {
		t.Fatalf("widest page refused: %v", err)
	}
	for name, s := range map[string]ScopeClusters{
		"page past the max":      {Count: ScopeClustersPageMax + 1, Clusters: page(ScopeClustersPageMax + 1)},
		"count under page":       {Count: 1, Truncated: false, Clusters: page(2)},
		"negative count":         {Count: -1},
		"truncated, all here":    {Count: 1, Truncated: true, Clusters: []ScopeCluster{one}},
		"cut, not truncated":     {Count: 2, Clusters: []ScopeCluster{one}},
		"cursor set":             {Count: 1, Clusters: []ScopeCluster{one}, Cursor: "next"},
		"empty project":          {Count: 1, Clusters: []ScopeCluster{{Location: "l", Cluster: "c"}}},
		"dotted location":        {Count: 1, Clusters: []ScopeCluster{{Project: "p", Location: "us.central1", Cluster: "c"}}},
		"upper-case cluster":     {Count: 1, Clusters: []ScopeCluster{{Project: "p", Location: "l", Cluster: "Prod"}}},
		"label past 63":          {Count: 1, Clusters: []ScopeCluster{{Project: long + "a", Location: "l", Cluster: "c"}}},
		"unknown, not truncated": {Count: 0, Unknown: true},
		"unlisted past the max":  {UnlistedProjects: append(slices.Clone(unlisted), "p"), UnlistedCount: ScopeClustersUnlistedMax + 1},
		"unlisted count under":   {UnlistedProjects: []string{"p", "q"}, UnlistedCount: 1},
		"unlisted not a label":   {UnlistedProjects: []string{"P_1"}, UnlistedCount: 1},
		"oldest not a time":      {Count: 1, Clusters: []ScopeCluster{one}, OldestListing: "yesterday"},
		"unbounded, known":       {Unbounded: true},
		"unbounded, unlisted":    {Unbounded: true, Unknown: true, Truncated: true, UnlistedProjects: []string{"p"}, UnlistedCount: 1},
		"parent-scoped, two":     {ParentScoped: true, Count: 2, Truncated: true, Clusters: []ScopeCluster{one}},
		"parent-scoped, unknown": {ParentScoped: true, Unknown: true, Truncated: true},
	} {
		if _, err := ScopeClustersPart(s); err == nil {
			t.Errorf("%s: ScopeClustersPart accepted %+v", name, s)
		}
		raw, _ := json.Marshal(map[string]ScopeClusters{"scopeClusters": s})
		if _, ok, err := ScopeClustersOf([]Part{{Kind: "data", Data: raw}}); !ok || err == nil {
			t.Errorf("%s: ScopeClustersOf = ok %v err %v, want found and refused", name, ok, err)
		}
	}
	noList := Part{Kind: "data", Data: json.RawMessage(`{"scopeClusters":{"count":0}}`)}
	if _, ok, err := ScopeClustersOf([]Part{noList}); !ok || err == nil {
		t.Errorf("no clusters list: ok %v err %v, want found and refused", ok, err)
	}
}

// The fields that say what a page can't (unknown, unlisted, the oldest
// listing) round-trip, and are absent from a part that doesn't set them, so
// that part is byte for byte the shape first written.
func TestScopeClustersUnknownAndUnlisted(t *testing.T) {
	want := ScopeClusters{Count: 1, Truncated: true, Unknown: true,
		Clusters:         []ScopeCluster{{Project: "proj-a", Location: "us-central1", Cluster: "prod-1"}},
		UnlistedProjects: []string{"mgmt", "proj-x"}, UnlistedCount: 3, OldestListing: "2026-11-23T10:00:00Z"}
	part, err := ScopeClustersPart(want)
	if err != nil {
		t.Fatal(err)
	}
	got, ok, err := ScopeClustersOf([]Part{part})
	if err != nil || !ok || !reflect.DeepEqual(got, want) {
		t.Fatalf("ScopeClustersOf = %+v %v %v, want %+v", got, ok, err, want)
	}
	for name, s := range map[string]ScopeClusters{
		"parent-scoped":               {ParentScoped: true, Count: 1, Clusters: []ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}},
		"parent-scoped, out of scope": {ParentScoped: true, Clusters: []ScopeCluster{}},
		"unbounded":                   {Unbounded: true, Unknown: true, Truncated: true, Clusters: []ScopeCluster{}},
	} {
		part, err := ScopeClustersPart(s)
		if err != nil {
			t.Errorf("%s refused: %v", name, err)
			continue
		}
		if got, _, err := ScopeClustersOf([]Part{part}); err != nil || !reflect.DeepEqual(got, s) {
			t.Errorf("%s: round trip %+v %v", name, got, err)
		}
	}
	// Unknown with nothing known: an empty page, still truncated.
	if _, err := ScopeClustersPart(ScopeClusters{Unknown: true, Truncated: true}); err != nil {
		t.Errorf("unknown empty page refused: %v", err)
	}
	// Unknown lets the count stand under what a page would hold.
	if _, err := ScopeClustersPart(ScopeClusters{Count: 1, Truncated: true, Unknown: true,
		Clusters: []ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}}); err != nil {
		t.Errorf("unknown with all known clusters on the page refused: %v", err)
	}
	plain, err := ScopeClustersPart(ScopeClusters{Count: 1, Clusters: []ScopeCluster{{Project: "p", Location: "l", Cluster: "c"}}})
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"unknown", "unlistedProjects", "unlistedCount", "oldestListing", "parentScoped", "unbounded"} {
		if strings.Contains(string(plain.Data), key) {
			t.Errorf("a part without %s carries it: %s", key, plain.Data)
		}
	}
}

func TestReplyToRoundTrip(t *testing.T) {
	want := ReplyTo{TaskID: "task-0a1b2c3d"}
	part, err := ReplyToPart(want)
	if err != nil {
		t.Fatal(err)
	}
	if string(part.Data) != `{"replyTo":{"taskId":"task-0a1b2c3d"}}` {
		t.Fatalf("part = %s", part.Data)
	}
	text := Part{Kind: "text", Text: "and the other pool?"}
	got, ok, err := ReplyToOf([]Part{text, part})
	if err != nil || !ok || got != want {
		t.Fatalf("ReplyToOf = %+v %v %v, want %+v", got, ok, err, want)
	}
	if _, ok, err := ReplyToOf([]Part{text}); ok || err != nil {
		t.Fatalf("no part: ok=%v err=%v, want absent", ok, err)
	}
}

// The task id becomes half of a store key, so anything but a subject token
// is refused on the way out and an error, not a skip, on the way in; so is a
// second part, which would leave the report ambiguous.
func TestReplyToRefusesWhatIsNotATaskID(t *testing.T) {
	for _, id := range []string{"", "a.b", "task/1", strings.Repeat("x", 64)} {
		if _, err := ReplyToPart(ReplyTo{TaskID: id}); err == nil {
			t.Errorf("ReplyToPart(%q) accepted", id)
		}
		raw := Part{Kind: "data", Data: json.RawMessage(`{"replyTo":{"taskId":` + strconv.Quote(id) + `}}`)}
		if _, ok, err := ReplyToOf([]Part{raw}); !ok || err == nil {
			t.Errorf("ReplyToOf(%q) = ok %v err %v, want found and refused", id, ok, err)
		}
	}
	part, _ := ReplyToPart(ReplyTo{TaskID: "task-1"})
	if _, ok, err := ReplyToOf([]Part{part, part}); !ok || err == nil {
		t.Errorf("two parts: ok %v err %v, want found and refused", ok, err)
	}
}

func TestPushedReportRoundTrip(t *testing.T) {
	for _, want := range []PushedReport{{TaskID: "task-root", Text: "np-quartz-7 is at 92%"}, {Text: "a thread's report"}} {
		part, err := PushedReportPart(want)
		if err != nil {
			t.Fatal(err)
		}
		got, ok, err := PushedReportOf([]Part{{Kind: "text", Text: "and the other pool?"}, part})
		if err != nil || !ok || got != want {
			t.Fatalf("PushedReportOf = %+v %v %v, want %+v", got, ok, err, want)
		}
	}
	if _, ok, err := PushedReportOf([]Part{{Kind: "text", Text: "hi"}}); ok || err != nil {
		t.Fatalf("no part: ok=%v err=%v, want absent", ok, err)
	}
	part, _ := PushedReportPart(PushedReport{Text: "x"})
	if string(part.Data) != `{"pushedReport":{"text":"x"}}` {
		t.Fatalf("a report on no task = %s, want taskId omitted", part.Data)
	}
}

// The text is spliced ahead of a prompt, so it is bounded; the task id is
// half of a store key, so it is a subject token or absent. Refused on the
// way out, and an error, not a skip, on the way in; so is a second part.
func TestPushedReportRefusesEmptyOversizeAndBadTask(t *testing.T) {
	for name, r := range map[string]PushedReport{
		"no text":      {TaskID: "task-1"},
		"past the cap": {Text: strings.Repeat("x", PushedReportTextCap+1)},
		"a bad task":   {TaskID: "a.b", Text: "report"},
	} {
		if _, err := PushedReportPart(r); err == nil {
			t.Errorf("%s: PushedReportPart accepted", name)
		}
		raw, _ := json.Marshal(map[string]PushedReport{"pushedReport": r})
		if _, ok, err := PushedReportOf([]Part{{Kind: "data", Data: raw}}); !ok || err == nil {
			t.Errorf("%s: PushedReportOf = ok %v err %v, want found and refused", name, ok, err)
		}
	}
	part, _ := PushedReportPart(PushedReport{Text: "report"})
	if _, ok, err := PushedReportOf([]Part{part, part}); !ok || err == nil {
		t.Errorf("two parts: ok %v err %v, want found and refused", ok, err)
	}
}

func TestWholeConversationKey(t *testing.T) {
	for key, want := range map[string]bool{
		"slack:dm/D123": true, "gchat:dm/spaces/DDD": true, "gchat:space/spaces/AAA": true,
		"gchat:space/spaces/AAA/threads/BBB": true,
		"slack:C0HOME/1712.0001":             false, "slack:dm/D123/1712.0001": false, "gchat:spaces/AAA/threads/BBB": false,
		"gchat:dm/spaces/DDD/threads/TTT": false, "console:abc": false, "inject:bench-7": false, "": false,
		// Teams: a personal chat, a group chat and a channel's own id are
		// shared; a channel thread's id, with its token in either spelling, is not.
		"teams:a:1UxxASmI": true, "teams:19:group@thread.v2": true, "teams:19:chan@thread.tacv2": true,
		"teams:19:chan@thread.tacv2;messageid=1794603936403": false, "teams:19:chan@thread.tacv2;messageId=1794603936403": false,
	} {
		if got := WholeConversationKey(key); got != want {
			t.Errorf("WholeConversationKey(%q) = %v, want %v", key, got, want)
		}
	}
}

func TestThreadKeyedConversation(t *testing.T) {
	for key, want := range map[string]bool{
		"slack:C0HOME/1712.0001": true, "gchat:spaces/AAA/threads/BBB": true, "gchat:dm/spaces/DDD/threads/TTT": true,
		"slack:dm/D123/1712.0001": true,
		"slack:dm/D123":           false, "gchat:space/spaces/AAA": false, "gchat:dm/spaces/DDD": false,
		"console:abc": false, "inject:bench-7": false, "a2a:x": false, "discord:g1/t1": false, "": false,
		"teams:19:chan@thread.tacv2;messageid=1794603936403": true, "teams:a:1UxxASmI": false, "teams:19:chan@thread.tacv2": false,
	} {
		if got := ThreadKeyedConversation(key); got != want {
			t.Errorf("ThreadKeyedConversation(%q) = %v, want %v", key, got, want)
		}
	}
}

func TestDelegateTargetsRefuseWhatIsNotAnAddresseeOrOneLine(t *testing.T) {
	for name, tgt := range map[string]DelegateTarget{
		"a dotted name":         {Name: "a.b"},
		"a wildcard":            {Name: "*"},
		"no name":               {Name: ""},
		"a two-line blurb":      {Name: "p", Description: "first\nsecond"},
		"a blurb over the cap":  {Name: "p", Description: strings.Repeat("x", DelegateTargetDescriptionMax+1)},
		"a blurb with a tab":    {Name: "p", Description: "a\tb"},
		"a blurb padded by two": {Name: "p", Description: "a  b"},
	} {
		if _, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{tgt}}); err == nil {
			t.Errorf("%s: built, want refused", name)
		}
		raw, _ := json.Marshal(map[string]DelegateTargets{"delegateTargets": {Targets: []DelegateTarget{tgt}}})
		if _, ok, err := DelegateTargetsOf([]Part{{Kind: "data", Data: raw}}); !ok || err == nil {
			t.Errorf("%s: read ok=%v err=%v, want an error", name, ok, err)
		}
	}
	over := make([]DelegateTarget, DelegateTargetsMax+1)
	for i := range over {
		over[i] = DelegateTarget{Name: fmt.Sprintf("p%d", i)}
	}
	if _, err := DelegateTargetsPart(DelegateTargets{Targets: over}); err == nil {
		t.Error("more than the cap: built, want refused")
	}
	raw := []byte(`{"delegateTargets":{}}`)
	if _, ok, err := DelegateTargetsOf([]Part{{Kind: "data", Data: raw}}); !ok || err == nil {
		t.Errorf("no targets list: ok=%v err=%v, want an error", ok, err)
	}
}

func TestDelegateTargetDescriptionFoldsToOneLineUnderTheCap(t *testing.T) {
	if got := DelegateTargetDescription("  reads\none\tcluster\x00  "); got != "reads one cluster" {
		t.Fatalf("got %q", got)
	}
	long := strings.Repeat("é", DelegateTargetDescriptionMax) // two bytes a rune
	got := DelegateTargetDescription(long)
	if len(got) > DelegateTargetDescriptionMax || !utf8.ValidString(got) {
		t.Fatalf("cut to %d bytes, valid=%v", len(got), utf8.ValidString(got))
	}
}

func TestDelegateTargetsHoldParametersAndTheByteBound(t *testing.T) {
	ok := DelegateTargets{Targets: []DelegateTarget{{Name: "c", Parameters: []ProfileParameter{{Name: "cluster", Required: true}}}}, Truncated: true}
	part, err := DelegateTargetsPart(ok)
	if err != nil {
		t.Fatal(err)
	}
	if got, _, err := DelegateTargetsOf([]Part{part}); err != nil || !reflect.DeepEqual(got, ok) {
		t.Fatalf("round trip = %+v %v", got, err)
	}
	bad := DelegateTargets{Targets: []DelegateTarget{{Name: "c", Parameters: []ProfileParameter{{Name: "Cluster"}}}}}
	if _, err := DelegateTargetsPart(bad); err == nil {
		t.Error("a parameter that isn't an identifier: built, want refused")
	}
	var many []ProfileParameter
	for i := 0; i <= TaskParametersMax; i++ {
		many = append(many, ProfileParameter{Name: fmt.Sprintf("p%d", i)})
	}
	if _, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{{Name: "c", Parameters: many}}}); err == nil {
		t.Error("more parameters than the cap: built, want refused")
	}
	var huge []DelegateTarget
	for i := 0; i < DelegateTargetsMax; i++ {
		var ps []ProfileParameter
		for j := 0; j < TaskParametersMax; j++ {
			ps = append(ps, ProfileParameter{Name: fmt.Sprintf("parameter_number_%02d", j)})
		}
		huge = append(huge, DelegateTarget{Name: fmt.Sprintf("p%d", i), Description: strings.Repeat("x", DelegateTargetDescriptionMax), Parameters: ps})
	}
	if _, err := DelegateTargetsPart(DelegateTargets{Targets: huge}); err == nil {
		t.Errorf("a list past %d bytes: built, want refused", DelegateTargetsMaxBytes)
	}
}

// A cut that lands after a space doesn't leave it trailing: the description
// reads back as itself, so the gateway's own cut never fails the reader.
func TestDelegateTargetDescriptionIsStableAcrossTheCut(t *testing.T) {
	s := strings.Repeat("a", DelegateTargetDescriptionMax-1) + " tail words here"
	got := DelegateTargetDescription(s)
	if strings.HasSuffix(got, " ") || DelegateTargetDescription(got) != got {
		t.Fatalf("cut to %q, which doesn't read back as itself", got[len(got)-5:])
	}
	if _, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{{Name: "p", Description: got}}}); err != nil {
		t.Fatalf("the cut description doesn't build a part: %v", err)
	}
}

// A batch rides the optional tasks list, beside the single fields, and a
// request without one is the single shape on the wire, byte for byte.
func TestADelegateBatchRoundTripsAndTheSingleShapeIsUnchanged(t *testing.T) {
	data, err := json.Marshal(DelegateRequest{Tasks: []DelegateTask{
		{Addressee: "cluster", Text: "look at dev", Parameters: map[string]string{"cluster": "dev"}},
		{Addressee: "platform", Text: "and the fleet"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"tasks":[{"addressee":"cluster","text":"look at dev","parameters":{"cluster":"dev"}},{"addressee":"platform","text":"and the fleet"}]}`
	if string(data) != want {
		t.Fatalf("wire = %s\nwant   %s", data, want)
	}
	var got DelegateRequest
	if err := json.Unmarshal(data, &got); err != nil || len(got.Tasks) != 2 || got.Tasks[0].Parameters["cluster"] != "dev" {
		t.Fatalf("decoded %+v, %v", got, err)
	}
	single, _ := json.Marshal(DelegateRequest{Addressee: "platform", Text: "do it"})
	if string(single) != `{"addressee":"platform","text":"do it"}` {
		t.Fatalf("single wire = %s", single)
	}
}

// DelegateEntries is the request as entries to mint: one for the single
// shape, each batch entry for a batch.
func TestDelegateEntriesReadsEitherShape(t *testing.T) {
	single := DelegateRequest{Addressee: "platform", Text: "x", Parameters: map[string]string{"a": "b"}}
	if got := single.Entries(); len(got) != 1 || got[0].Addressee != "platform" || got[0].Text != "x" || got[0].Parameters["a"] != "b" {
		t.Fatalf("single entries = %+v", got)
	}
	batch := DelegateRequest{Tasks: []DelegateTask{{Addressee: "a", Text: "1"}, {Addressee: "b", Text: "2"}}}
	if got := batch.Entries(); len(got) != 2 || got[1].Addressee != "b" {
		t.Fatalf("batch entries = %+v", got)
	}
	if !batch.IsBatch() || single.IsBatch() {
		t.Fatal("IsBatch reads the wrong shape")
	}
}

func TestValidateDelegateRequest(t *testing.T) {
	entry := DelegateTask{Addressee: "platform", Text: "x"}
	many := make([]DelegateTask, DelegateBatchMax+1)
	for i := range many {
		many[i] = entry
	}
	for name, tc := range map[string]struct {
		req  DelegateRequest
		want string
	}{
		"the single shape":         {req: DelegateRequest{Addressee: "platform", Text: "x"}},
		"a batch at the ceiling":   {req: DelegateRequest{Tasks: many[:DelegateBatchMax]}},
		"both shapes set":          {req: DelegateRequest{Addressee: "platform", Text: "x", Tasks: []DelegateTask{entry}}, want: "either"},
		"parameters beside tasks":  {req: DelegateRequest{Parameters: map[string]string{"a": "b"}, Tasks: []DelegateTask{entry}}, want: "either"},
		"an empty batch":           {req: DelegateRequest{Tasks: []DelegateTask{}}, want: "empty"},
		"a batch over the ceiling": {req: DelegateRequest{Tasks: many}, want: fmt.Sprintf("at most %d", DelegateBatchMax)},
		"an entry over the cap": {req: DelegateRequest{Tasks: []DelegateTask{entry, {Addressee: "platform", Text: strings.Repeat("x", DelegateTextCap+1)}}},
			want: "task 2"},
		"an entry with blank text": {req: DelegateRequest{Tasks: []DelegateTask{entry, {Addressee: "platform", Text: " "}}}, want: "task 2"},
		"single text over the cap": {req: DelegateRequest{Addressee: "platform", Text: strings.Repeat("x", DelegateTextCap+1)}, want: "too long"},
		"single blank text":        {req: DelegateRequest{Addressee: "platform", Text: " "}, want: "text"},
	} {
		t.Run(name, func(t *testing.T) {
			err := ValidateDelegateRequest(tc.req)
			if tc.want == "" {
				if err != nil {
					t.Fatalf("refused: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("err = %v, want one naming %q", err, tc.want)
			}
		})
	}
}

// The width a turn may fan out to rides the delegateTargets part, absent
// meaning one, and is held to the batch ceiling like everything else there.
func TestDelegateTargetsCarryTheWidth(t *testing.T) {
	want := DelegateTargets{Targets: []DelegateTarget{{Name: "platform"}}, WidthMax: 4}
	part, err := DelegateTargetsPart(want)
	if err != nil {
		t.Fatal(err)
	}
	got, ok, err := DelegateTargetsOf([]Part{part})
	if err != nil || !ok || got.WidthMax != 4 || got.Width() != 4 {
		t.Fatalf("got %+v ok=%v err=%v", got, ok, err)
	}
	if w := (DelegateTargets{}).Width(); w != 1 {
		t.Fatalf("absent width = %d, want 1", w)
	}
	plain, _ := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{{Name: "platform"}}})
	if strings.Contains(string(plain.Data), "widthMax") {
		t.Fatalf("an unset width is on the wire: %s", plain.Data)
	}
	for _, w := range []int{-1, DelegateBatchMax + 1} {
		if _, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{}, WidthMax: w}); err == nil {
			t.Errorf("width %d accepted", w)
		}
	}
}

// TestDelegationRefusalPartRoundTripsAndRefusesEmptyOrOversizeText: the
// parent's model reads the text, so it must be there and within the cap.
func TestDelegationRefusalPartRoundTripsAndRefusesEmptyOrOversizeText(t *testing.T) {
	p, err := DelegationRefusalPart(DelegationRefusal{Text: "⚠️ delegation refused: out of scope"})
	if err != nil {
		t.Fatal(err)
	}
	got, ok := DelegationRefusalOf([]Part{{Kind: "text", Text: "x"}, p})
	if !ok || got.Text != "⚠️ delegation refused: out of scope" {
		t.Fatalf("round trip = %+v, %v", got, ok)
	}
	if _, ok := ChildResultOf([]Part{p}); ok {
		t.Fatal("a refusal read as a child result")
	}
	for _, text := range []string{"", strings.Repeat("a", ChildResultTextCap+1)} {
		if _, err := DelegationRefusalPart(DelegationRefusal{Text: text}); err == nil {
			t.Fatalf("a refusal with %d bytes of text was built", len(text))
		}
	}
	bad := Part{Kind: "data", Data: []byte(`{"delegationRefused":{"text":""}}`)}
	if _, ok := DelegationRefusalOf([]Part{bad}); ok {
		t.Fatal("an empty refusal was read")
	}
}

// TestDelegationRefusalRoundRoundTripsAndIsStrict: a round-one refusal goes
// out with no round field, as it did before the field, and a later round
// round-trips. A round that isn't a JSON number, or is out of bounds, makes
// the part no refusal, rather than a round-one refusal that would end the
// parent's wait; a later-round refusal is held to the same text bounds.
// TestDelegationRefusalBusyIsOmittedWhenFalseAndRoundTrips: Busy (#7547) is
// absent from every refusal but a busy one, so a reader that predates it
// reads the same bytes it always did; set, it reads back, and a non-bool is
// no refusal.
func TestDelegationRefusalBusyIsOmittedWhenFalseAndRoundTrips(t *testing.T) {
	plain, err := DelegationRefusalPart(DelegationRefusal{Text: "no", Round: 2})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(plain.Data), "busy") {
		t.Fatalf("a refusal that isn't busy carries the busy field: %s", plain.Data)
	}
	busy, err := DelegationRefusalPart(DelegationRefusal{Text: "wait", Round: 2, Busy: true})
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"delegationRefused":{"text":"wait","round":2,"busy":true}}`; string(busy.Data) != want {
		t.Fatalf("busy refusal = %s, want %s", busy.Data, want)
	}
	if got, ok := DelegationRefusalOf([]Part{busy}); !ok || !got.Busy || got.Round != 2 {
		t.Fatalf("busy refusal read back = %+v, %v", got, ok)
	}
	if got, ok := DelegationRefusalOf([]Part{{Kind: "data", Data: []byte(`{"delegationRefused":{"text":"x","round":2,"busy":"yes"}}`)}}); ok {
		t.Errorf("a string busy read as %+v", got)
	}
}

func TestDelegationRefusalRoundRoundTripsAndIsStrict(t *testing.T) {
	first, err := DelegationRefusalPart(DelegationRefusal{Text: "no"})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(first.Data), "round") {
		t.Fatalf("a round-one refusal carries the round field: %s", first.Data)
	}
	if got, ok := DelegationRefusalOf([]Part{first}); !ok || got.Round != 0 {
		t.Fatalf("round-one refusal read back = %+v, %v; want no round", got, ok)
	}
	rep, err := DelegationRefusalPart(DelegationRefusal{Text: "again", Round: 2})
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"delegationRefused":{"text":"again","round":2}}`; string(rep.Data) != want {
		t.Fatalf("round-two refusal = %s, want %s", rep.Data, want)
	}
	if got, ok := DelegationRefusalOf([]Part{rep}); !ok || got.Round != 2 || got.Text != "again" {
		t.Fatalf("round-two refusal read back = %+v, %v", got, ok)
	}
	if _, err := DelegationRefusalPart(DelegationRefusal{Text: "x", Round: DelegateRoundsCeiling}); err != nil {
		t.Fatalf("a refusal at the ceiling round was refused: %v", err)
	}
	for _, raw := range []string{
		`{"delegationRefused":{"text":"x","round":"2"}}`,
		`{"delegationRefused":{"text":"x","round":true}}`,
		`{"delegationRefused":{"text":"x","round":{}}}`,
		`{"delegationRefused":{"text":"x","round":-1}}`,
		`{"delegationRefused":{"text":"x","round":17}}`,
		`{"delegationRefused":{"text":"x","round":1000000}}`,
		`{"delegationRefused":{"text":"","round":2}}`,
	} {
		if got, ok := DelegationRefusalOf([]Part{{Kind: "data", Data: []byte(raw)}}); ok {
			t.Errorf("%s read as %+v", raw, got)
		}
	}
	for _, r := range []DelegationRefusal{
		{Text: "", Round: 2},
		{Text: strings.Repeat("a", ChildResultTextCap+1), Round: 2},
		{Text: "x", Round: -1},
		{Text: "x", Round: DelegateRoundsCeiling + 1},
	} {
		if _, err := DelegationRefusalPart(r); err == nil {
			t.Errorf("a refusal with %d bytes of text, round %d, was built", len(r.Text), r.Round)
		}
	}
}

// TestChildResultCarriesItsRound: a round-one end carries no round on the
// wire, a later round round-trips, and a round out of bounds is refused on
// the way out and skipped on the way in.
func TestChildResultCarriesItsRound(t *testing.T) {
	one, err := ChildResultPart(ChildResult{ChildTaskID: "task-7", Entry: 1, State: StateCompleted})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(one.Data), "round") {
		t.Fatalf("a round-one end carries the round field: %s", one.Data)
	}
	for _, round := range []int{2, DelegateRoundsCeiling} {
		in := ChildResult{ChildTaskID: "task-7", Entry: 1, State: StateCompleted, Round: round}
		part, err := ChildResultPart(in)
		if err != nil {
			t.Fatal(err)
		}
		if got, ok := ChildResultOf([]Part{part}); !ok || got != in {
			t.Fatalf("ChildResultOf = %+v, %v; want %+v", got, ok, in)
		}
	}
	for _, round := range []int{-1, DelegateRoundsCeiling + 1} {
		c := ChildResult{ChildTaskID: "task-7", Entry: 1, State: StateCompleted, Round: round}
		if _, err := ChildResultPart(c); err == nil {
			t.Errorf("round %d built", round)
		}
		data, _ := json.Marshal(map[string]ChildResult{"childResult": c})
		if _, ok := ChildResultOf([]Part{{Kind: "data", Data: data}}); ok {
			t.Errorf("round %d read", round)
		}
	}
	str := Part{Kind: "data", Data: []byte(`{"childResult":{"childTaskId":"task-7","state":"completed","round":"2"}}`)}
	if _, ok := ChildResultOf([]Part{str}); ok {
		t.Error("a string round was read")
	}
}

// TestDelegateTargetsCarryTheRounds: an unset RoundsMax is off the wire and
// reads as one; a set one round-trips; one out of bounds is refused.
func TestDelegateTargetsCarryTheRounds(t *testing.T) {
	plain, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{{Name: "platform"}}})
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(plain.Data), "roundsMax") {
		t.Fatalf("an unset rounds max is on the wire: %s", plain.Data)
	}
	if r := (DelegateTargets{}).Rounds(); r != 1 {
		t.Fatalf("absent rounds = %d, want 1", r)
	}
	part, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{{Name: "platform"}}, RoundsMax: 3})
	if err != nil {
		t.Fatal(err)
	}
	got, ok, err := DelegateTargetsOf([]Part{part})
	if err != nil || !ok || got.RoundsMax != 3 || got.Rounds() != 3 {
		t.Fatalf("got %+v ok=%v err=%v", got, ok, err)
	}
	for _, r := range []int{-1, DelegateRoundsCeiling + 1} {
		if _, err := DelegateTargetsPart(DelegateTargets{Targets: []DelegateTarget{}, RoundsMax: r}); err == nil {
			t.Errorf("rounds %d accepted", r)
		}
		raw := []byte(fmt.Sprintf(`{"delegateTargets":{"targets":[],"roundsMax":%d}}`, r))
		if _, _, err := DelegateTargetsOf([]Part{{Kind: "data", Data: raw}}); err == nil {
			t.Errorf("rounds %d read", r)
		}
	}
}
