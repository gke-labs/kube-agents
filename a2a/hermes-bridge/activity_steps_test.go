package hermesbridge

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/stepmsg"
)

// delivery is a hook delivery as hermes sends it, read the door's way.
func delivery(t *testing.T, event, session string, fields map[string]any) hookDelivery {
	t.Helper()
	body := map[string]any{"hook_event_name": event, "session_id": session}
	for k, v := range fields {
		body[k] = v
	}
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	var d hookDelivery
	if err := json.Unmarshal(raw, &d); err != nil {
		t.Fatal(err)
	}
	return d
}

// apiRequest is a post_api_request planning the named calls; with none it
// stopped.
func apiRequest(t *testing.T, session string, count int, calls ...[2]string) hookDelivery {
	t.Helper()
	finish := "stop"
	var tcs []any
	for _, c := range calls {
		finish = "tool_calls"
		tcs = append(tcs, map[string]any{"id": c[0], "type": "function",
			"function": map[string]any{"name": c[1], "arguments": `{"command":"ARG-VALUE"}`}})
	}
	return delivery(t, hookPostAPIRequest, session, map[string]any{"extra": map[string]any{
		"api_call_count": count, "finish_reason": finish,
		"response": map[string]any{"finish_reason": finish, "assistant_message": map[string]any{
			"content": "MODEL-SAID", "tool_calls": tcs}},
	}})
}

func preCall(t *testing.T, session, id, tool string) hookDelivery {
	return delivery(t, hookPreToolCall, session, map[string]any{"tool_name": tool, "extra": map[string]any{"tool_call_id": id}})
}

func postCall(t *testing.T, session, id, tool string) hookDelivery {
	return delivery(t, hookPostToolCall, session, map[string]any{"tool_name": tool,
		"extra": map[string]any{"tool_call_id": id, "status": "ok"}})
}

// observeAll feeds deliveries in order and returns every step they close,
// then the one the drain closes.
func observeAll(a *activityState, ds ...hookDelivery) []stepmsg.Step {
	var out []stepmsg.Step
	for _, d := range ds {
		_, _, steps := a.observe(d)
		out = append(out, steps...)
	}
	return append(out, a.closeStep()...)
}

func stepsEqual(got, want []stepmsg.Step) bool {
	return slices.EqualFunc(got, want, func(g, w stepmsg.Step) bool {
		return g.Kind == w.Kind && g.Title == w.Title && slices.Equal(g.CallIDs, w.CallIDs) && g.Thought == ""
	})
}

// Plan steps come from the trace: each pre_tool_call's validated tool name
// and call id, consecutive calls of one tool within one model request in
// one step. The request is only a boundary.
func TestSteps_PlanStepsComeFromTheTrace(t *testing.T) {
	a := newActivityState(false)
	got := observeAll(a,
		apiRequest(t, "s1", 1, [2]string{"raw-1", "termnal"}, [2]string{"raw-2", "terminal"}, [2]string{"raw-3", "read_file"}),
		preCall(t, "s1", "c1", "terminal"), postCall(t, "s1", "c1", "terminal"),
		preCall(t, "s1", "c2", "terminal"), postCall(t, "s1", "c2", "terminal"),
		preCall(t, "s1", "c3", "read_file"), postCall(t, "s1", "c3", "read_file"),
		apiRequest(t, "s1", 2, [2]string{"raw-4", "terminal"}),
		preCall(t, "s1", "c4", "terminal"),
	)
	want := []stepmsg.Step{
		{Kind: stepmsg.KindPlan, Title: "terminal", CallIDs: []string{"c1", "c2"}},
		{Kind: stepmsg.KindPlan, Title: "read_file", CallIDs: []string{"c3"}},
		{Kind: stepmsg.KindPlan, Title: "terminal", CallIDs: []string{"c4"}},
	}
	if !stepsEqual(got, want) {
		t.Fatalf("steps = %+v, want %+v", got, want)
	}
}

// A name a model request planned and the trace never saw is not a step: the
// request fires before hermes validates the names, so it may carry text
// the model invented or was handed.
func TestSteps_ANamePlannedButNeverCalledIsNoStep(t *testing.T) {
	got := observeAll(newActivityState(false), apiRequest(t, "s1", 1, [2]string{"c1", "the token is ghp_x"}))
	if len(got) != 0 {
		t.Fatalf("steps = %+v, want none", got)
	}
	// Beside a call the trace did see, only that call is a step.
	got = observeAll(newActivityState(false),
		apiRequest(t, "s1", 1, [2]string{"c1", "the token is ghp_x"}, [2]string{"c2", "terminal"}),
		preCall(t, "s1", "c2", "terminal"))
	want := []stepmsg.Step{{Kind: stepmsg.KindPlan, Title: "terminal", CallIDs: []string{"c2"}}}
	if !stepsEqual(got, want) {
		t.Fatalf("steps = %+v, want %+v", got, want)
	}
}

// The synthesis step: a request of the turn's own session that stopped
// after work, its finish reason read off the delivery when hermes
// truncated the response.
func TestSteps_SynthesisOnlyAfterWork(t *testing.T) {
	truncated := delivery(t, hookPostAPIRequest, "s1", map[string]any{"extra": map[string]any{
		"api_call_count": 2, "finish_reason": "stop",
		"response": map[string]any{"_truncated": true, "original_type": "dict", "preview": `{"finish_reason": "st`},
	}})
	for _, tc := range []struct {
		name      string
		ds        []hookDelivery
		synthesis bool
	}{
		{"a later request", []hookDelivery{apiRequest(t, "s1", 1, [2]string{"c1", "x"}), apiRequest(t, "s1", 2)}, true},
		{"a first request after a completed call", []hookDelivery{preCall(t, "s1", "c1", "x"), postCall(t, "s1", "c1", "x"), apiRequest(t, "s1", 1)}, true},
		{"a first request that answers outright", []hookDelivery{apiRequest(t, "s1", 1)}, false},
		{"a truncated response, the finish reason on the delivery", []hookDelivery{truncated}, true},
	} {
		got := observeAll(newActivityState(false), tc.ds...)
		has := slices.ContainsFunc(got, func(s stepmsg.Step) bool { return s.IsSynthesis() && s.Title == synthesisStepTitle })
		if has != tc.synthesis {
			t.Errorf("%s: steps = %+v, want synthesis %v", tc.name, got, tc.synthesis)
		}
	}
}

// A delegated child runs in the turn's process and posts its own requests,
// under its own session; its final request is not the turn writing its
// answer. Nor is a request whose session is not known.
func TestSteps_ADelegatedChildsAnswerIsNotTheTurns(t *testing.T) {
	for name, a := range map[string]*activityState{
		"cli (learns its session)": newActivityState(false),
		"api (knows its session)":  newSessionActivityState("s1", true),
	} {
		got := observeAll(a,
			apiRequest(t, "s1", 1, [2]string{"c1", "delegate_task"}),
			preCall(t, "s1", "c1", "delegate_task"),
			apiRequest(t, "child", 2),
			apiRequest(t, "", 3),
		)
		if slices.ContainsFunc(got, stepmsg.Step.IsSynthesis) {
			t.Errorf("%s: a child's or an unknown session's stop made a synthesis step: %+v", name, got)
		}
		got = observeAll(a, apiRequest(t, "s1", 4))
		if !slices.ContainsFunc(got, stepmsg.Step.IsSynthesis) {
			t.Errorf("%s: the turn's own stop made no synthesis step: %+v", name, got)
		}
	}
}

// A step is structure only: the encoded records carry nothing of the
// model's text, a call's arguments, or a tool's result or error.
func TestSteps_CarryNoText(t *testing.T) {
	a := newActivityState(false)
	err := delivery(t, hookPostToolCall, "s1", map[string]any{"tool_name": "terminal", "tool_input": map[string]any{"command": "ARG-VALUE"},
		"extra": map[string]any{"tool_call_id": "c1", "status": "error", "error_type": "tool_error",
			"error_message": "ERROR-TEXT", "result": "RESULT-TEXT"}})
	got := observeAll(a, apiRequest(t, "s1", 1, [2]string{"c1", "terminal"}), preCall(t, "s1", "c1", "terminal"), err, apiRequest(t, "s1", 2))
	if len(got) != 2 {
		t.Fatalf("steps = %+v, want a plan step and the synthesis step", got)
	}
	for _, st := range got {
		part, perr := st.Part()
		if perr != nil {
			t.Fatal(perr)
		}
		for _, f := range []string{"MODEL-SAID", "ARG-VALUE", "RESULT-TEXT", "ERROR-TEXT", `"thought"`} {
			if strings.Contains(string(part.Data), f) {
				t.Errorf("step %s carries %s", part.Data, f)
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

// After the terminal nothing goes out; a step the record refuses is skipped
// and said once, and fails nothing. Neither reaches the bus, which this
// bridge has no client for.
func TestPublishSteps_AfterTheTerminalOrRefusedPublishesNothing(t *testing.T) {
	b := &Bridge{cfg: Config{Logger: slog.New(slog.NewTextHandler(io.Discard, nil))}}
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-steps", ContextID: "ctx", CorrelationID: "corr"}}
	a := newActivityState(false)
	run.act.Store(a)
	run.state = stateDone
	b.publishSteps(run, a, []stepmsg.Step{{Kind: stepmsg.KindSynthesis, Title: synthesisStepTitle}})
	if a.steps != 0 {
		t.Fatalf("a step went out after the terminal (%d counted)", a.steps)
	}
	run.state = stateRunning
	b.publishSteps(run, a, []stepmsg.Step{{Kind: stepmsg.KindPlan, Title: "  "}, {Kind: "card", Title: "x"}})
	if a.steps != 0 || !a.stepRefused.Load() {
		t.Fatalf("refused steps: %d counted, refusal logged %v", a.steps, a.stepRefused.Load())
	}
}

// End to end through the door: the trace's calls become the steps
// artifact, the turn's stop the synthesis step, a failed call keeps its
// class on its activity entry, and nothing the stub sent besides names and
// ids - model words, a planned name it never called, arguments, results,
// error text - lands on any artifact.
func TestActivity_RequestsBecomeStepsAndCarryNoText(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
post("post_api_request", None, None, {"api_call_count": 1, "finish_reason": "tool_calls",
     "assistant_message": "MODEL-SAID",
     "response": {"finish_reason": "tool_calls", "assistant_message": {"content": "MODEL-SAID",
        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{\"command\": \"ARG-VALUE\"}"}},
                       {"id": "c9", "type": "function", "function": {"name": "the token is ghp_x", "arguments": "{}"}}]}}})
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
	want := []stepmsg.Step{
		{Kind: stepmsg.KindPlan, Title: "terminal", CallIDs: []string{"c1"}},
		{Kind: stepmsg.KindSynthesis, Title: synthesisStepTitle},
	}
	if !stepsEqual(steps, want) {
		t.Fatalf("steps = %+v, want %+v", steps, want)
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
		for _, f := range []string{"MODEL-SAID", "ghp_x", "ARG-VALUE", "RESULT-TEXT", "ERROR-TEXT"} {
			if strings.Contains(string(raw), f) {
				t.Errorf("artifact %q carries %s: %s", a.Name, f, raw)
			}
		}
	}
}

// The step the turn's last calls were building closes with the turn: no
// later model request is needed to publish it.
func TestActivity_TheOpenStepClosesWithTheTurn(t *testing.T) {
	_, url := startServer(t)
	startBridgeCfg(t, url, hermesStub(t, `
call("terminal", {"command": "ls"}, "c1")
call("terminal", {"command": "ls"}, "c2")
print("the answer")
`), nil)
	c := gatewayClient(t, url)
	submit(t, c, "task-open-step", "go")
	task := waitTerminal(t, c, "task-open-step")
	art := task.Artifact(stepmsg.ArtifactName)
	if art == nil || len(art.Parts) != 1 {
		t.Fatalf("steps artifact = %+v, want one step", art)
	}
	st, ok := stepmsg.Decode(art.Parts[0])
	if !ok || !stepsEqual([]stepmsg.Step{st}, []stepmsg.Step{{Kind: stepmsg.KindPlan, Title: "terminal", CallIDs: []string{"c1", "c2"}}}) {
		t.Fatalf("step = %+v, want the terminal step with c1 and c2", st)
	}
}

// A run of one tool's calls longer than a record holds continues in a new
// step of the same title, and the longest title and ids the trace makes
// still encode.
func TestStepCapsFitTheRecord(t *testing.T) {
	a := newActivityState(false)
	tool := strings.Repeat("t", activityToolNameCap+10)
	var ds []hookDelivery
	for i := 0; i < stepCallIDsCap+1; i++ {
		id := fmt.Sprintf("%d-%s", i, strings.Repeat("x", activityCallIDCap+10))
		ds = append(ds, preCall(t, "s1", id, tool))
	}
	got := observeAll(a, ds...)
	if len(got) != 2 || len(got[0].CallIDs) != stepCallIDsCap || len(got[1].CallIDs) != 1 || got[0].Title != got[1].Title {
		t.Fatalf("steps of %d calls: %d steps, ids %d/%d", stepCallIDsCap+1, len(got), len(got[0].CallIDs), len(got[len(got)-1].CallIDs))
	}
	for _, st := range got {
		if _, err := st.Part(); err != nil {
			t.Fatalf("a step the trace makes is refused: %v", err)
		}
	}
}

// postAPIRequest delivers a post_api_request that stopped, signed with the
// pod's shared key, as the pod-wide entry does on the API route.
func postAPIRequest(t *testing.T, url, key, sessionID string, count int) {
	t.Helper()
	body, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostAPIRequest, "session_id": sessionID, "delivery_id": fmt.Sprintf("api-%s-%d-%d", sessionID, count, time.Now().UnixNano()),
		"extra": map[string]any{"api_call_count": count, "finish_reason": "stop",
			"response": map[string]any{"finish_reason": "stop", "assistant_message": map[string]any{"content": "done"}}},
	})
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(body))
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write(body)
	req.Header.Set(hookSignatureHeader, hookSignaturePrefix+hex.EncodeToString(mac.Sum(nil)))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Errorf("delivery: %v", err)
		return
	}
	resp.Body.Close()
}

// Two turns of one API task share an activityState. Turn 1 completes a call
// and stops: its synthesis step. Turn 2's first request answers outright:
// it did no work, so no synthesis step, whatever turn 1 did.
func TestAPI_AFollowUpThatAnswersOutrightIsNoSynthesis(t *testing.T) {
	_, url := startServer(t)
	const shared = "pod-wide-activity-key"
	var doorURL atomic.Value
	door := func() string { return doorURL.Load().(string) }
	release := make(chan struct{})
	stub := newAPIStub(t, func(w http.ResponseWriter, r *http.Request, c apiCall) {
		if !strings.Contains(c.idempotency, "/") {
			select {
			case <-release:
			case <-r.Context().Done():
				return
			}
			postDelivery(t, door(), shared, c.sessionID, hookPreToolCall, "kubectl", "c1")
			postDelivery(t, door(), shared, c.sessionID, hookPostToolCall, "kubectl", "c1")
			postAPIRequest(t, door(), shared, c.sessionID, 2)
		} else {
			postAPIRequest(t, door(), shared, c.sessionID, 1)
		}
		writeCompletion(w, c.sessionID, "answer to "+c.prompt)
	})
	b := startAPIBridge(t, url, stub, func(cfg *Config) {
		cfg.ActivityListen = "127.0.0.1:0"
		cfg.ActivitySecret = shared
	})
	doorURL.Store(b.ActivityURL())
	c := gatewayClient(t, url)
	origin := submitIn(t, c, "task-two-turns", "ctx-two-turns", "first")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "and then")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if len(stub.seen()) != 2 {
		t.Fatalf("%d requests, want 2 turns", len(stub.seen()))
	}
	art := task.Artifact(stepmsg.ArtifactName)
	if art == nil {
		t.Fatal("no steps artifact")
	}
	var steps []stepmsg.Step
	for _, p := range art.Parts {
		st, ok := stepmsg.Decode(p)
		if !ok {
			t.Fatalf("not a step record: %+v", p)
		}
		steps = append(steps, st)
	}
	want := []stepmsg.Step{
		{Kind: stepmsg.KindPlan, Title: "kubectl", CallIDs: []string{"c1"}},
		{Kind: stepmsg.KindSynthesis, Title: synthesisStepTitle},
	}
	if !stepsEqual(steps, want) {
		t.Fatalf("steps = %+v, want %+v: turn 2 did no work", steps, want)
	}
}
