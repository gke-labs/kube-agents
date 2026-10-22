package hermesbridge

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// apiRequestDelivery is a post_api_request delivery as hermes sends it: the
// sanitized response under extra, beside the counts.
func apiRequestDelivery(t *testing.T, extra map[string]any) hookDelivery {
	t.Helper()
	body, err := json.Marshal(map[string]any{"hook_event_name": hookPostAPIRequest, "session_id": "s1", "extra": extra})
	if err != nil {
		t.Fatal(err)
	}
	var d hookDelivery
	if err := json.Unmarshal(body, &d); err != nil {
		t.Fatal(err)
	}
	return d
}

func toolCall(id, name, args string) map[string]any {
	return map[string]any{"id": id, "type": "function", "function": map[string]any{"name": name, "arguments": args}}
}

// One plan step per tool, in plan order, each with its calls' ids; a call
// named at the top level (not under function) counts the same.
func TestPlannedSteps_OneStepPerToolWithItsCallIDs(t *testing.T) {
	d := apiRequestDelivery(t, map[string]any{
		"api_call_count": 1, "finish_reason": "tool_calls",
		"response": map[string]any{"finish_reason": "tool_calls", "assistant_message": map[string]any{
			"content": "I will look.",
			"tool_calls": []any{
				toolCall("c1", "mcp__gke__list_clusters", `{"project":"p"}`),
				toolCall("c2", "terminal", `{"command":"ls"}`),
				toolCall("c3", "mcp__gke__list_clusters", `{"project":"q"}`),
				map[string]any{"id": "c4", "name": "read_file"},
			},
		}},
	})
	got := plannedSteps(d, 0)
	want := []stepmsg.Step{
		{Kind: stepmsg.KindPlan, Title: "mcp__gke__list_clusters", CallIDs: []string{"c1", "c3"}},
		{Kind: stepmsg.KindPlan, Title: "terminal", CallIDs: []string{"c2"}},
		{Kind: stepmsg.KindPlan, Title: "read_file", CallIDs: []string{"c4"}},
	}
	if len(got) != len(want) {
		t.Fatalf("plannedSteps = %+v, want %+v", got, want)
	}
	for i := range want {
		if got[i].Kind != want[i].Kind || got[i].Title != want[i].Title || !slices.Equal(got[i].CallIDs, want[i].CallIDs) {
			t.Errorf("step %d = %+v, want %+v", i, got[i], want[i])
		}
	}
}

// A request that stops after work is the synthesis step; a first request
// that answers outright plans nothing.
func TestPlannedSteps_SynthesisOnlyAfterWork(t *testing.T) {
	stop := func(n int) hookDelivery {
		return apiRequestDelivery(t, map[string]any{"api_call_count": n, "finish_reason": "stop",
			"response": map[string]any{"finish_reason": "stop", "assistant_message": map[string]any{"content": "done"}}})
	}
	for _, tc := range []struct {
		name      string
		d         hookDelivery
		completed int
		synthesis bool
	}{
		{"a later request", stop(2), 0, true},
		{"a first request after a completed call", stop(1), 1, true},
		{"a first request that answers outright", stop(1), 0, false},
	} {
		got := plannedSteps(tc.d, tc.completed)
		isSynthesis := len(got) == 1 && got[0].IsSynthesis() && got[0].Title == synthesisStepTitle
		if isSynthesis != tc.synthesis || (!tc.synthesis && len(got) != 0) {
			t.Errorf("%s: plannedSteps = %+v, want synthesis %v", tc.name, got, tc.synthesis)
		}
	}
	// The finish reason read off the response when the delivery has none.
	d := apiRequestDelivery(t, map[string]any{"api_call_count": 3,
		"response": map[string]any{"finish_reason": "stop", "assistant_message": map[string]any{}}})
	if got := plannedSteps(d, 0); len(got) != 1 || !got[0].IsSynthesis() {
		t.Errorf("finish reason from the response: plannedSteps = %+v, want the synthesis step", got)
	}
}

// forbidden is text a step must never carry: the model's words, a call's
// arguments, a tool's result and error text, and the keys that would hold
// them.
var forbidden = []string{
	"MODEL-SAID", "ARG-VALUE", "RESULT-TEXT", "ERROR-TEXT",
	`"thought"`, `"errorMessage"`, `"resultPreview"`, `"result"`, `"arguments"`, `"content"`,
}

// A step is structure only: whatever the response holds besides the calls'
// names and ids does not reach the encoded record.
func TestPlannedSteps_CarryNoTextFromTheRequest(t *testing.T) {
	d := apiRequestDelivery(t, map[string]any{
		"api_call_count": 2, "finish_reason": "tool_calls",
		"error_message": "ERROR-TEXT", "result": "RESULT-TEXT",
		"assistant_message": "MODEL-SAID",
		"response": map[string]any{"assistant_message": map[string]any{
			"content":           "MODEL-SAID <think>MODEL-SAID</think>",
			"reasoning_content": "MODEL-SAID",
			"tool_calls":        []any{toolCall("c1", "terminal", `{"command":"ARG-VALUE"}`)},
		}},
	})
	steps := plannedSteps(d, 0)
	if len(steps) == 0 {
		t.Fatal("no steps planned")
	}
	for _, st := range steps {
		raw := string(st.Part().Data)
		for _, f := range forbidden {
			if strings.Contains(raw, f) {
				t.Errorf("step %s carries %s", raw, f)
			}
		}
	}
}

// The steps' own budget stops them and leaves the trace's share alone past
// it; the trace's share stops them too.
func TestStepUnderBudget(t *testing.T) {
	defer func(n int) { activityStepBudget = n }(activityStepBudget)
	activityStepBudget = 2
	a := newActivityState(false)
	if !a.stepUnderBudget() || !a.stepUnderBudget() || a.stepUnderBudget() {
		t.Fatal("want exactly two steps under a budget of two")
	}
	if a.published != 2 || a.dropped != 0 {
		t.Fatalf("published %d dropped %d: steps count against the trace's share and are never counted as dropped calls", a.published, a.dropped)
	}
	b := newActivityState(false)
	b.published = activityEntryBudget - activityHeartbeatReserve
	if b.stepUnderBudget() {
		t.Fatal("a step went out past the trace's share")
	}
}

// End to end through the door: the model's requests become the steps
// artifact, a failed call keeps its error class on the activity entry, and
// no text the stub sent - model words, arguments, results, error text -
// lands on any artifact the task published.
func TestActivity_RequestsBecomeStepsAndCarryNoText(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
post("post_api_request", None, None, {"api_call_count": 1, "finish_reason": "tool_calls",
     "assistant_message": "MODEL-SAID",
     "response": {"finish_reason": "tool_calls", "assistant_message": {"content": "MODEL-SAID",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ARG-VALUE\"}"}}]}}})
post("pre_tool_call", "terminal", {"command": "ARG-VALUE"}, {"tool_call_id": "c1"})
post("post_tool_call", "terminal", {"command": "ARG-VALUE"}, {"tool_call_id": "c1", "status": "error",
     "error_type": "tool_error", "error_message": "ERROR-TEXT", "result": "RESULT-TEXT", "duration_ms": 5})
post("post_api_request", None, None, {"api_call_count": 2, "finish_reason": "stop",
     "response": {"finish_reason": "stop", "assistant_message": {"content": "MODEL-SAID"}}})
print("the answer")
`), nil)
	c := gatewayClient(t, url)
	submit(t, c, "task-steps", "go")
	task := waitTerminal(t, c, "task-steps")

	art := task.Artifact(stepmsg.ArtifactName)
	if art == nil {
		t.Fatalf("no %s artifact; artifacts: %+v", stepmsg.ArtifactName, task.Artifacts)
	}
	var steps []stepmsg.Step
	for _, p := range art.Parts {
		st, ok := stepmsg.Decode(p)
		if !ok {
			t.Fatalf("a %s part is not a step record: %+v", stepmsg.ArtifactName, p)
		}
		steps = append(steps, st)
	}
	if len(steps) != 2 || steps[0].Kind != stepmsg.KindPlan || steps[0].Title != "terminal" ||
		!slices.Equal(steps[0].CallIDs, []string{"c1"}) || !steps[1].IsSynthesis() {
		t.Fatalf("steps = %+v, want the terminal plan step with c1, then the synthesis step", steps)
	}

	entries := activityEntries(t, task)
	if len(entries) != 1 || entries[0].Status != ActivityStatusError || entries[0].ErrorType != "tool_error" {
		t.Fatalf("activity = %+v, want one error entry with its class", entries)
	}
	for _, a := range task.Artifacts {
		if a.Name == lib.ArtifactResult {
			continue
		}
		raw, err := json.Marshal(a.Parts)
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range []string{"MODEL-SAID", "ARG-VALUE", "RESULT-TEXT", "ERROR-TEXT"} {
			if strings.Contains(string(raw), f) {
				t.Errorf("artifact %q carries %s: %s", a.Name, f, raw)
			}
		}
	}
}
