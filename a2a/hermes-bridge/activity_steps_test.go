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
	"unicode/utf8"

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

func TestScrubInlineSecrets_JSONQuotedMultiWordAndNonHTTPURLs(t *testing.T) {
	cases := []struct {
		name      string
		in        string
		forbidden []string
		wantSub   []string
	}{
		{
			name:      "json quoted secret keys",
			in:        `{"password": "super-secret-value", "client_secret": "top-secret-client"}`,
			forbidden: []string{"super-secret-value", "top-secret-client"},
			wantSub:   []string{`"password": "[redacted]"`, `"client_secret": "[redacted]"`},
		},
		{
			name:      "quoted multi-word secret values",
			in:        `password="my secret phrase" and api_key='two words' and secret=unquotedSecret123`,
			forbidden: []string{"my secret phrase", "two words", "unquotedSecret123"},
			wantSub:   []string{`password="[redacted]"`, `api_key='[redacted]'`, `secret=[redacted]`},
		},
		{
			name:      "non-http URL credentials",
			in:        `psql postgresql://admin:my-db-pass@db.internal:5432/prod && redis-cli -u redis://default:redispass@10.0.0.1:6379 && nats pub -s nats://user:natspass@nats:4222 && mysql://root:mysqlpass@db/app`,
			forbidden: []string{"my-db-pass", "redispass", "natspass", "mysqlpass"},
			wantSub:   []string{"postgresql://admin:[redacted]@db.internal:5432/prod", "redis://default:[redacted]@10.0.0.1:6379", "nats://user:[redacted]@nats:4222", "mysql://root:[redacted]@db/app"},
		},
		{
			name:      "compound secret keys",
			in:        `auth_token=xyz123 refresh_token: "abc def" GITHUB_TOKEN=ghp_123 webhook_secret=whsec_123 secret_access_key=AKIA123 MYSQL_PASSWORD=mysecret`,
			forbidden: []string{"xyz123", "abc def", "ghp_123", "whsec_123", "AKIA123", "mysecret"},
			wantSub:   []string{`auth_token=[redacted]`, `refresh_token: "[redacted]"`, `GITHUB_TOKEN=[redacted]`, `webhook_secret=[redacted]`, `secret_access_key=[redacted]`, `MYSQL_PASSWORD=[redacted]`},
		},
		{
			name:      "escaped json quotes in shell command",
			in:        `curl -d "{\"user\": \"alice\", \"password\": \"hunter2\", \"auth_token\": \"tok_secret\"}"`,
			forbidden: []string{"hunter2", "tok_secret"},
			wantSub:   []string{`\"user\": \"alice\"`, `\"password\": \"[redacted]\"`, `\"auth_token\": \"[redacted]\"`},
		},
		{
			name:      "redis password-only URLs",
			in:        `redis-cli -u redis://:redispass1@host:6379/0 && redis-cli -u rediss://:redispass2@host:6379`,
			forbidden: []string{"redispass1", "redispass2"},
			wantSub:   []string{"redis://:[redacted]@host:6379/0", "rediss://:[redacted]@host:6379"},
		},
		{
			name:      "unclosed truncated quoted secret",
			in:        `password="multi word secret that was truncated`,
			forbidden: []string{"multi word secret", "truncated"},
			wantSub:   []string{`password="[redacted]`},
		},
		{
			name:      "empty assignment before secret and yaml mapping header",
			in:        "PASSWORD= API_KEY=\"my_val\" ./run.sh\nsecrets:\n  password: hunter2",
			forbidden: []string{"my_val", "hunter2"},
			wantSub:   []string{`PASSWORD= API_KEY="[redacted]" ./run.sh`, "secrets:\n  password: [redacted]"},
		},
		{
			name:      "escaped quote inside escaped-quote value",
			in:        `\"password\": \"abc\\\"def\"`,
			forbidden: []string{"abc", "def"},
			wantSub:   []string{`\"password\": \"[redacted]\"`},
		},
		{
			name:      "values containing equals or colons (base64, URLs, compound values)",
			in:        `password="c2VjcmV0==" {"password": "c2VjcmV0=="} webhook_secret="https://hooks.example.com/T00/B00/X00" password: c2VjcmV0== password=abc=def`,
			forbidden: []string{"c2VjcmV0==", "https://hooks.example.com/T00/B00/X00", "T00/B00/X00", "abc=def"},
			wantSub:   []string{`password="[redacted]"`, `{"password": "[redacted]"}`, `webhook_secret="[redacted]"`, `password: [redacted]`, `password=[redacted]`},
		},
		{
			name:      "unquoted colon values with equals or colons (url-safe base64, user:pass, v1=, connstring)",
			in:        `password: c2Vj_cmV0= and password: user:secret and webhook_secret: v1=9f3c2a1b and connection_secret: host=10.0.0.1;pass=secret`,
			forbidden: []string{"c2Vj_cmV0", "user:secret", "9f3c2a1b", "host=10.0.0.1", "pass=secret"},
			wantSub:   []string{"password: [redacted] and password: [redacted] and webhook_secret: [redacted] and connection_secret: [redacted]"},
		},
		{
			name:      "cli short and long secret flags and user:pass",
			in:        `mysql -u root -psupersecret1 && mysql -u root -p supersecret2 && redis-cli -a redispass9 && curl -u admin:curlpass9 https://api.internal && docker login --docker-password dockpass9 && kubectl --token=k8stok9 get pods`,
			forbidden: []string{"supersecret1", "supersecret2", "redispass9", "curlpass9", "dockpass9", "k8stok9"},
			wantSub:   []string{"-p[redacted]", "-p [redacted]", "-a [redacted]", "-u admin:[redacted]", "--docker-password [redacted]", "--token=[redacted]"},
		},
		{
			name:      "non-bearer authorization headers and schemes",
			in:        `curl -H "Authorization: Token tok_secret_99" -H "Authorization: Digest username=admin, response=9f3c2a" -H "Authorization: ApiKey key_secret_88" && Authorization: Token bare_tok_77`,
			forbidden: []string{"tok_secret_99", "9f3c2a", "key_secret_88", "bare_tok_77"},
			wantSub:   []string{`"Authorization: Token [redacted]"`, `"Authorization: Digest [redacted]"`, `"Authorization: ApiKey [redacted]"`, `Authorization: Token [redacted]`},
		},
		{
			name:      "pem private key blocks, oauth refresh tokens, prose secrets, and yaml data blocks",
			in:        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA7\n-----END RSA PRIVATE KEY-----\nrefresh=1//0gABCDEF0123456789abcdefXYZ\nThe password is hunter299.\nDB_PASSWORD: my secret phrase\ndata:\n  tls.key: LS0tLS1CRUdJTi\n  custom_entry: YmFyBaz123",
			forbidden: []string{"MIIEpAIBAAKCAQEA7", "1//0gABCDEF", "hunter299", "my secret phrase", "LS0tLS1CRUdJTi", "YmFyBaz123"},
			wantSub:   []string{"[redacted]", "The password is [redacted].", "DB_PASSWORD: [redacted]", "  tls.key: [redacted]", "  custom_entry: [redacted]"},
		},
		{
			name:      "pgp private key block and prose with a qualifier, past tense, or a named user",
			in:        "-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQdGBF8pgpkeyBody\n-----END PGP PRIVATE KEY BLOCK-----\nThe password is now hunter301. The token was tok_old_302, and the password for admin is hunter303; the secret is set to s3cr3t304.",
			forbidden: []string{"lQdGBF8pgpkeyBody", "hunter301", "tok_old_302", "hunter303", "s3cr3t304"},
			wantSub:   []string{"The password is now [redacted].", "The token was [redacted],", "the password for admin is [redacted];", "the secret is set to [redacted]."},
		},
		{
			name:      "all-caps env keys ending in _KEY, _CREDS, _DSN or _PRIVATE",
			in:        "ENCRYPTION_KEY=enc_key_401 STRIPE_KEY=sk_live_402 AWS_CREDS=creds403 DB_DSN=postgres://u:p404@db/prod RSA_PRIVATE=priv405 sort_key=name PRIMARY_KEY_ID=7",
			forbidden: []string{"enc_key_401", "sk_live_402", "creds403", "p404", "priv405"},
			wantSub:   []string{"ENCRYPTION_KEY=[redacted]", "STRIPE_KEY=[redacted]", "AWS_CREDS=[redacted]", "DB_DSN=[redacted]", "RSA_PRIVATE=[redacted]", "sort_key=name", "PRIMARY_KEY_ID=7"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := scrubInlineSecrets(tc.in)
			for _, f := range tc.forbidden {
				if strings.Contains(got, f) {
					t.Fatalf("scrubInlineSecrets(%q) leaked %q in %q", tc.in, f, got)
				}
			}
			for _, w := range tc.wantSub {
				if !strings.Contains(got, w) {
					t.Fatalf("scrubInlineSecrets(%q) = %q, missing %q", tc.in, got, w)
				}
			}
		})
	}
}

// Under the api executor, what a tool was given, what it returned, what it
// failed with and what the model reasoned stay in the pod: the entry
// carries the input's shape (as under the cli executor), the status and
// hermes's error word, and the steps carry titles only. A scrubber is not
// relied on for any of it. The mutants this kills: publishing the detailed
// input, the error text, a result preview, or the reasoning as a thought.
func TestAPI_EntriesCarryShapesAndNoResultErrorOrReasoningText(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-shape-test"
	const (
		reasoningSecret = "the admin password is hunter2-reasoning"
		argSecret       = "my-db-pass-in-args"
		resultSecret    = "super-secret-result-value"
		errSecret       = "token=tok_in_error_text"
	)

	var apiDel hookDelivery
	apiBody, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostAPIRequest,
		"session_id":      "a2a-shape-test",
		"delivery_id":     "api-1",
		"extra": map[string]any{
			"api_call_count": 1,
			"finish_reason":  "tool_calls",
			"response": map[string]any{
				"finish_reason": "tool_calls",
				"assistant_message": map[string]any{
					"reasoning_content": "Connecting to the database; " + reasoningSecret + ".",
					"tool_calls": []map[string]any{
						{"id": "c1", "function": map[string]any{"name": "terminal", "arguments": json.RawMessage(`{"command":"psql postgresql://admin:` + argSecret + `@db.internal:5432/prod"}`)}},
					},
				},
			},
		},
	})
	if err := json.Unmarshal(apiBody, &apiDel); err != nil {
		t.Fatal(err)
	}
	_, _, steps := a.observe(apiDel)
	if len(steps) != 1 || steps[0].Kind != stepmsg.KindPlan || !slices.Equal(steps[0].CallIDs, []string{"c1"}) {
		t.Fatalf("planned steps = %+v, want one plan step for c1", steps)
	}
	if steps[0].Thought != "" {
		t.Fatalf("a plan step carried the model's reasoning as its thought: %q", steps[0].Thought)
	}
	rawSteps, _ := json.Marshal(steps)
	for _, leaked := range []string{"hunter2-reasoning", argSecret, "Connecting to the database"} {
		if strings.Contains(string(rawSteps), leaked) {
			t.Fatalf("steps leaked %q: %s", leaked, rawSteps)
		}
	}

	// A request that reasoned and planned nothing, without stopping, is a
	// step under the fixed title; its text is not in the record.
	var reasonOnly hookDelivery
	reasonBody, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostAPIRequest,
		"session_id":      "a2a-shape-test",
		"delivery_id":     "api-2",
		"extra": map[string]any{
			"api_call_count": 2,
			"finish_reason":  "length",
			"response": map[string]any{
				"finish_reason":     "length",
				"assistant_message": map[string]any{"reasoning_content": "Still thinking; " + reasoningSecret + "."},
			},
		},
	})
	if err := json.Unmarshal(reasonBody, &reasonOnly); err != nil {
		t.Fatal(err)
	}
	_, _, steps = a.observe(reasonOnly)
	if len(steps) != 1 || steps[0].Title != reasoningStepTitle || steps[0].Thought != "" {
		t.Fatalf("reasoning-only request steps = %+v, want one %q step with no thought", steps, reasoningStepTitle)
	}

	for _, tc := range []struct {
		name   string
		status string
		result string
		errMsg string
	}{
		{name: "completed", status: hookStatusOK, result: `{"output": "{\"password\": \"` + resultSecret + `\"}"}`},
		{name: "failed", status: "error", result: `{"output": "` + resultSecret + ` on line 1\nmore"}`, errMsg: "psql: FATAL " + errSecret},
	} {
		t.Run(tc.name, func(t *testing.T) {
			pre := hookDelivery{Event: hookPreToolCall, SessionID: "a2a-shape-test", DeliveryID: "pre-" + tc.name, ToolName: "terminal",
				ToolInput: json.RawMessage(`{"command":"psql postgresql://admin:` + argSecret + `@db.internal:5432/prod","cluster":"gke_p_us-east4_prod-a"}`)}
			pre.Extra.ToolCallID = "call-" + tc.name
			a.observe(pre)
			post := hookDelivery{Event: hookPostToolCall, SessionID: "a2a-shape-test", DeliveryID: "post-" + tc.name, ToolName: "terminal",
				ToolInput: json.RawMessage(`{"command":"psql postgresql://admin:` + argSecret + `@db.internal:5432/prod","cluster":"gke_p_us-east4_prod-a"}`)}
			post.Extra.ToolCallID = "call-" + tc.name
			post.Extra.Status = tc.status
			post.Extra.Result = json.RawMessage(tc.result)
			post.Extra.ErrorMessage = tc.errMsg
			entry, ok, _ := a.observe(post)
			if !ok {
				t.Fatal("expected post_tool_call entry")
			}
			var input map[string]any
			if err := json.Unmarshal(entry.Input, &input); err != nil {
				t.Fatalf("entry input is not an object: %s (%v)", entry.Input, err)
			}
			if cmd, _ := input["command"].(string); !strings.HasPrefix(cmd, "<string, ") {
				t.Fatalf("entry input command = %q, want its shape", cmd)
			}
			if entry.ResultPreview != "" || entry.ErrorMessage != "" {
				t.Fatalf("entry carried result or error text: preview=%q error=%q", entry.ResultPreview, entry.ErrorMessage)
			}
			if tc.status != hookStatusOK && entry.Status != ActivityStatusError {
				t.Fatalf("entry status = %q, want error", entry.Status)
			}
			if entry.Title == "" || entry.Worker != "platform" {
				t.Fatalf("entry lost its attribution: title=%q worker=%q", entry.Title, entry.Worker)
			}
			// The title names the cluster the command ran against (the one
			// argument-derived identifier that is published); the command,
			// the result and the error text are not anywhere on the entry.
			rawEntry, _ := json.Marshal(entry)
			for _, leaked := range []string{argSecret, resultSecret, errSecret, "tok_in_error_text", "on line 1", "FATAL", "psql"} {
				if strings.Contains(string(rawEntry), leaked) {
					t.Fatalf("entry leaked %q: %s", leaked, rawEntry)
				}
			}
		})
	}

	// The one result detail published: the id of the card a kanban_create
	// made, on the entry and as its preview; the card's title names the
	// step (by design), its body does not appear anywhere.
	create := hookDelivery{Event: hookPostToolCall, SessionID: "a2a-shape-test", DeliveryID: "post-create", ToolName: kanbanCreateTool,
		ToolInput: json.RawMessage(`{"title":"Rotate the credential on prod-a","body":"` + argSecret + `"}`)}
	create.Extra.ToolCallID = "call-create"
	create.Extra.Status = hookStatusOK
	create.Extra.Result = json.RawMessage(`{"id":"t_card_shape_1","title":"Rotate the credential on prod-a","body":"` + argSecret + `","output":"` + resultSecret + `"}`)
	entry, ok, _ := a.observe(create)
	if !ok || entry.CardID != "t_card_shape_1" || entry.ResultPreview != "card_id=t_card_shape_1" {
		t.Fatalf("kanban_create entry = %+v, want cardId and preview card_id=t_card_shape_1", entry)
	}
	if !strings.Contains(entry.Title, "Rotate the credential on prod-a") {
		t.Fatalf("kanban_create entry title = %q, want the card's title", entry.Title)
	}
	rawEntry, _ := json.Marshal(entry)
	for _, leaked := range []string{argSecret, resultSecret} {
		if strings.Contains(string(rawEntry), leaked) {
			t.Fatalf("kanban_create entry leaked %q: %s", leaked, rawEntry)
		}
	}
}

// With BRIDGE_DEBUG_ERROR_TEXT on (a maintainer's debugging switch, off by
// default), a failed call's entry carries the first line of its error text,
// scrubbed and capped; hermes's error_message first, else the result. A
// successful call carries none either way.
func TestAPI_DebugErrorTextCarriesTheScrubbedFirstLine(t *testing.T) {
	t.Setenv(debugErrorTextEnv, "true")
	a := newActivityState(false)
	a.sessionID = "a2a-debug-test"
	call := func(id, status, errMsg, result string) ActivityEntry {
		t.Helper()
		pre := hookDelivery{Event: hookPreToolCall, SessionID: "a2a-debug-test", DeliveryID: "pre-" + id, ToolName: "terminal",
			ToolInput: json.RawMessage(`{"command":"psql -h db.internal"}`)}
		pre.Extra.ToolCallID = id
		a.observe(pre)
		post := hookDelivery{Event: hookPostToolCall, SessionID: "a2a-debug-test", DeliveryID: "post-" + id, ToolName: "terminal",
			ToolInput: json.RawMessage(`{"command":"psql -h db.internal"}`)}
		post.Extra.ToolCallID = id
		post.Extra.Status = status
		post.Extra.ErrorMessage = errMsg
		post.Extra.Result = json.RawMessage(result)
		e, ok, _ := a.observe(post)
		if !ok {
			t.Fatalf("%s: no entry", id)
		}
		return e
	}
	e := call("c1", "error", "psql: FATAL connection refused token=tok_in_error_text\nsecond line", `null`)
	if !strings.HasPrefix(e.ErrorMessage, "psql: FATAL connection refused") {
		t.Errorf("error text = %q, want the first line of error_message", e.ErrorMessage)
	}
	for _, leaked := range []string{"tok_in_error_text", "second line"} {
		if strings.Contains(e.ErrorMessage, leaked) {
			t.Errorf("error text %q carries %q", e.ErrorMessage, leaked)
		}
	}
	if e = call("c2", "error", "", `"exit status 2: no such host\nmore"`); e.ErrorMessage != "exit status 2: no such host" {
		t.Errorf("error text from the result = %q, want its first line", e.ErrorMessage)
	}
	if e = call("c3", hookStatusOK, "", `"all good"`); e.ErrorMessage != "" {
		t.Errorf("a successful call carried error text %q", e.ErrorMessage)
	}
	// A long line is cut at the cap on a rune boundary.
	long := strings.Repeat("é", 300) + " end"
	if e = call("c5", "error", long, `null`); len(e.ErrorMessage) > debugErrorTextCap || !utf8.ValidString(e.ErrorMessage) || e.ErrorMessage == "" {
		t.Errorf("error text of %d bytes (valid UTF-8 %v), want at most %d and valid", len(e.ErrorMessage), utf8.ValidString(e.ErrorMessage), debugErrorTextCap)
	}
	t.Setenv(debugErrorTextEnv, "false")
	if e = call("c4", "error", "psql: FATAL", `null`); e.ErrorMessage != "" {
		t.Errorf("error text %q with the switch off", e.ErrorMessage)
	}
}

func TestStep_BeginTurnResetsCardsFiledAndKeepsTheSynthesisRule(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-coord"

	// Turn 1: coordinator calls kanban_create, then finishes turn 1 with reasoning.
	var createPost hookDelivery
	_ = json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","session_id":"a2a-coord","delivery_id":"d1","tool_name":"kanban_create","tool_input":{"title":"Check cluster-a"},"extra":{"tool_call_id":"c1","status":"ok","result":{"id":"t_card1"}}}`), &createPost)
	a.observe(createPost)
	if !a.cardsFiled.Load() {
		t.Fatal("expected cardsFiled to be true after kanban_create")
	}

	var turn1Stop hookDelivery
	_ = json.Unmarshal([]byte(`{"hook_event_name":"post_api_request","session_id":"a2a-coord","delivery_id":"d2","extra":{"api_call_count":2,"finish_reason":"stop","response":{"finish_reason":"stop","assistant_message":{"reasoning_content":"Dispatched card to worker; waiting for results.","content":"Card filed."}}}}`), &turn1Stop)
	if _, _, steps1 := a.observe(turn1Stop); len(steps1) != 0 {
		t.Fatalf("turn 1 synthesis step should be suppressed when cardsFiled is true, got %+v", steps1)
	}

	// Turn 2: beginTurn resets turnCardsFiled while keeping task-wide cardsFiled intact for handoff,
	// so a subsequent turn's synthesis step is emitted.
	a.beginTurn()
	if !a.cardsFiled.Load() {
		t.Fatal("expected beginTurn to keep task-wide cardsFiled true for handoff")
	}
	if a.trace.turnCardsFiled {
		t.Fatal("expected beginTurn to reset turnCardsFiled to false")
	}

	var turn2Tool hookDelivery
	_ = json.Unmarshal([]byte(`{"hook_event_name":"post_tool_call","session_id":"a2a-coord","delivery_id":"d3","tool_name":"kubectl","tool_input":{"cmd":"get nodes"},"extra":{"tool_call_id":"c2","status":"ok"}}`), &turn2Tool)
	a.observe(turn2Tool)

	var turn2Stop hookDelivery
	_ = json.Unmarshal([]byte(`{"hook_event_name":"post_api_request","session_id":"a2a-coord","delivery_id":"d4","extra":{"api_call_count":2,"finish_reason":"stop","response":{"finish_reason":"stop","assistant_message":{"reasoning_content":"All nodes are Ready.","content":"Final report."}}}}`), &turn2Stop)
	_, _, steps2 := a.observe(turn2Stop)
	if len(steps2) != 1 || !steps2[0].IsSynthesis() || steps2[0].Title != apiSynthesisStepTitle {
		t.Fatalf("turn 2 synthesis step not emitted after beginTurn: %+v", steps2)
	}
	if steps2[0].Thought != "" {
		t.Fatalf("the synthesis step carried the model's reasoning: %q", steps2[0].Thought)
	}
}

func TestStep_ConsecutiveToolCallsWithSameTitleAndDifferentCallIDsEmitBothSteps(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-batch-key"

	mkReq := func(delID, callID string, count int) hookDelivery {
		var d hookDelivery
		body, _ := json.Marshal(map[string]any{
			"hook_event_name": hookPostAPIRequest,
			"session_id":      "a2a-batch-key",
			"delivery_id":     delID,
			"extra": map[string]any{
				"api_call_count": count,
				"finish_reason":  "tool_calls",
				"response": map[string]any{
					"finish_reason": "tool_calls",
					"assistant_message": map[string]any{
						"tool_calls": []map[string]any{
							{"id": callID, "function": map[string]any{"name": "execute_code", "arguments": json.RawMessage(`{"code":"print(1)"}`)}},
						},
					},
				},
			},
		})
		if err := json.Unmarshal(body, &d); err != nil {
			t.Fatal(err)
		}
		return d
	}

	_, _, steps1 := a.observe(mkReq("api-1", "call_1", 1))
	if len(steps1) != 1 || len(steps1[0].CallIDs) != 1 || steps1[0].CallIDs[0] != "call_1" {
		t.Fatalf("first post_api_request steps = %+v, want call_1", steps1)
	}

	_, _, steps2 := a.observe(mkReq("api-2", "call_2", 2))
	if len(steps2) != 1 || len(steps2[0].CallIDs) != 1 || steps2[0].CallIDs[0] != "call_2" {
		t.Fatalf("second post_api_request with distinct CallID was dropped: %+v", steps2)
	}

	// A true duplicate delivery with the exact same CallID is still deduped.
	_, _, stepsDup := a.observe(mkReq("api-2-dup", "call_2", 2))
	if len(stepsDup) != 0 {
		t.Fatalf("duplicate post_api_request with identical CallID was not deduped: %+v", stepsDup)
	}
}

func TestMatchWorkerDeliveryRun_IgnoresUnrelatedBackgroundSession(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-waiting-task"
	a.waitingCard.Store(true)
	a.SyncKanbanCard("t_card42", "Upgrade gke-prod", "")
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-waiting"}}
	run.act.Store(a)
	runs := []*taskRun{run}

	// An unrelated background session with no matching Cwd, card ID, or claimed session ID is not attributed.
	unrelated := hookDelivery{
		Event:     hookPreToolCall,
		SessionID: "20990101_120000_bgcron",
		Cwd:       "/home/hermes",
		ToolName:  "terminal",
		ToolInput: json.RawMessage(`{"command":"ls"}`),
	}
	if got := matchWorkerDeliveryRun(runs, unrelated); got != nil {
		t.Fatalf("unrelated background session matched waiting run: %+v", got)
	}

	// A worker delivery naming another session's card (not owned by a.hasCardID) is dropped.
	otherSessionCard := hookDelivery{
		Event:     hookPreToolCall,
		SessionID: "20990101_120000_otherworker",
		Cwd:       "/var/lib/hermes/kanban/workspaces/t_card99_other/repo",
		ToolName:  "kanban_comment",
		ToolInput: json.RawMessage(`{"card_id":"t_card99_other","comment":"done"}`),
	}
	if got := matchWorkerDeliveryRun(runs, otherSessionCard); got != nil {
		t.Fatalf("worker delivery for another session's card matched waiting run: %+v", got)
	}

	// A delivery from another coordinator session (a2a-*) is dropped even if its Cwd or args name t_card42.
	otherCoordinator := hookDelivery{
		Event:     hookPreToolCall,
		SessionID: "a2a-other-conversation",
		Cwd:       "/var/lib/hermes/kanban/workspaces/t_card42/repo",
		ToolName:  "kanban_get",
		ToolInput: json.RawMessage(`{"card_id":"t_card42"}`),
	}
	if got := matchWorkerDeliveryRun(runs, otherCoordinator); got != nil {
		t.Fatalf("other coordinator a2a-* session matched waiting run via matchWorkerDeliveryRun: %+v", got)
	}

	// A delivery carrying the card's workspace in Cwd matches and claims the worker session.
	matchingCwd := hookDelivery{
		Event:     hookPreToolCall,
		SessionID: "20990101_120001_worker",
		Cwd:       "/var/lib/hermes/kanban/workspaces/t_card42/repo",
		ToolName:  "terminal",
		ToolInput: json.RawMessage(`{"command":"kubectl get nodes"}`),
	}
	if got := matchWorkerDeliveryRun(runs, matchingCwd); got != run {
		t.Fatalf("worker delivery with matching card Cwd did not match waiting run: %+v", got)
	}
}

func TestExtractLeadingCommand_SkipsFlagArgumentsAndEnvFlags(t *testing.T) {
	cases := []struct {
		cmd       string
		wantBin   string
		wantSub   string
		wantTitle string
	}{
		{
			cmd:       "kubectl -n kube-system get pods",
			wantBin:   "kubectl",
			wantSub:   "get",
			wantTitle: "Running kubectl get.",
		},
		{
			cmd:       "kubectl --context prod-1 get nodes",
			wantBin:   "kubectl",
			wantSub:   "get",
			wantTitle: "Running kubectl get (prod-1).",
		},
		{
			cmd:       "git -C /workspace status",
			wantBin:   "git",
			wantSub:   "status",
			wantTitle: "Running git status.",
		},
		{
			cmd:       "helm -n monitoring list",
			wantBin:   "helm",
			wantSub:   "list",
			wantTitle: "Running helm list.",
		},
		{
			cmd:       "env -i kubectl get pods",
			wantBin:   "kubectl",
			wantSub:   "get",
			wantTitle: "Running kubectl get.",
		},
		{
			cmd:       "env -u KUBECONFIG FOO=bar kubectl -o wide apply -f manifest.yaml",
			wantBin:   "kubectl",
			wantSub:   "apply",
			wantTitle: "Running kubectl apply.",
		},
	}
	for _, tc := range cases {
		bin, sub := extractLeadingCommand(tc.cmd)
		if bin != tc.wantBin || sub != tc.wantSub {
			t.Fatalf("extractLeadingCommand(%q) = (%q, %q), want (%q, %q)", tc.cmd, bin, sub, tc.wantBin, tc.wantSub)
		}
		if gotTitle := describeShellTool("terminal", map[string]any{"command": tc.cmd}); gotTitle != tc.wantTitle {
			t.Fatalf("describeShellTool(%q) = %q, want %q", tc.cmd, gotTitle, tc.wantTitle)
		}
	}
}

func TestRunForSignature_RoutesCoordinatorSessionWhileWaitingCard(t *testing.T) {
	const secret = "pod-wide-shared-secret"
	b := &Bridge{
		cfg:   Config{ActivitySecret: secret},
		tasks: make(map[string]*taskRun),
	}
	a := newActivityState(false)
	a.sessionID = "a2a-coord-wait"
	run := &taskRun{origin: &lib.Envelope{TaskID: "task-coord-wait"}}
	run.act.Store(a)
	b.tasks["task-coord-wait"] = run

	body := []byte(`{"hook_event_name":"post_api_request","session_id":"a2a-coord-wait","delivery_id":"d-wait-1"}`)
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write(body)
	sig := hookSignaturePrefix + hex.EncodeToString(mac.Sum(nil))

	// Neither inTurn nor waitingCard: not routed.
	if got := b.runForSignature(sig, body); got != nil {
		t.Fatalf("runForSignature matched idle task: %+v", got)
	}

	// inTurn=false, waitingCard=true (during Kanban wait / synthesis): routed to run.
	a.waitingCard.Store(true)
	if got := b.runForSignature(sig, body); got != run {
		t.Fatalf("runForSignature with waitingCard=true returned %+v, want %+v", got, run)
	}
}

func TestBatchKanban_PlansAllCallIDsAndSuppressesFallbackSteps(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-batch-kanban"

	var apiReq hookDelivery
	reqBody, _ := json.Marshal(map[string]any{
		"hook_event_name": hookPostAPIRequest,
		"session_id":      "a2a-batch-kanban",
		"delivery_id":     "api-kb-batch",
		"extra": map[string]any{
			"api_call_count": 1,
			"finish_reason":  "tool_calls",
			"response": map[string]any{
				"finish_reason": "tool_calls",
				"assistant_message": map[string]any{
					"tool_calls": []map[string]any{
						{"id": "c1", "function": map[string]any{"name": kanbanCreateTool, "arguments": json.RawMessage(`{"title":"Inspect cluster-1"}`)}},
						{"id": "c2", "function": map[string]any{"name": kanbanCreateTool, "arguments": json.RawMessage(`{"title":"Inspect cluster-2"}`)}},
					},
				},
			},
		},
	})
	if err := json.Unmarshal(reqBody, &apiReq); err != nil {
		t.Fatal(err)
	}
	_, _, steps := a.observe(apiReq)
	wantTitle := "Dispatching 2 specialist tasks on the Kanban board."
	if len(steps) != 1 || steps[0].Title != wantTitle || !slices.Equal(steps[0].CallIDs, []string{"c1", "c2"}) {
		t.Fatalf("batch kanban planned steps = %+v, want single step %q with CallIDs [c1 c2]", steps, wantTitle)
	}
	if a.trace.plannedTitles["c1"] != wantTitle || a.trace.plannedTitles["c2"] != wantTitle {
		t.Fatalf("plannedTitles = %+v, want both c1 and c2 mapped to %q", a.trace.plannedTitles, wantTitle)
	}

	// Subsequent pre_tool_call deliveries for c1 and c2 must not open fallback raw "kanban_create" steps.
	for _, cid := range []string{"c1", "c2"} {
		pre := hookDelivery{
			Event:     hookPreToolCall,
			SessionID: "a2a-batch-kanban",
			ToolName:  kanbanCreateTool,
			ToolInput: json.RawMessage(`{"title":"Inspect cluster"}`),
		}
		pre.Extra.ToolCallID = cid
		if _, _, preSteps := a.observe(pre); len(preSteps) != 0 {
			t.Fatalf("pre_tool_call for %s emitted unexpected steps: %+v", cid, preSteps)
		}
		if a.openStep != nil {
			t.Fatalf("pre_tool_call for %s opened fallback step: %+v", cid, a.openStep)
		}
	}
}

func TestWorkerHooks_DoNotMutateCoordinatorStepsOrCardsFiled(t *testing.T) {
	a := newActivityState(false)
	a.sessionID = "a2a-coord-main"
	a.waitingCard.Store(true)

	// Unplanned worker pre_tool_call must not open a fallback KindPlan step on the coordinator.
	workerPre := hookDelivery{
		Event:      hookPreToolCall,
		SessionID:  "s-worker",
		DeliveryID: "w-pre-1",
		ToolName:   "terminal",
		ToolInput:  json.RawMessage(`{"command":"kubectl get pods"}`),
	}
	workerPre.Extra.ToolCallID = "w-call-1"
	if _, _, steps := a.observe(workerPre); len(steps) != 0 {
		t.Fatalf("worker unplanned pre_tool_call emitted steps: %+v", steps)
	}
	if a.openStep != nil {
		t.Fatalf("worker unplanned pre_tool_call opened fallback step on coordinator: %+v", a.openStep)
	}

	// Worker post_tool_call for kanban_create must not flip coordinator cardsFiled/turnCardsFiled or increment turnCalls.
	workerPost := hookDelivery{
		Event:      hookPostToolCall,
		SessionID:  "s-worker",
		DeliveryID: "w-post-1",
		ToolName:   kanbanCreateTool,
		ToolInput:  json.RawMessage(`{"title":"Child worker card"}`),
	}
	workerPost.Extra.ToolCallID = "w-call-2"
	workerPost.Extra.Status = hookStatusOK
	workerPost.Extra.Result = json.RawMessage(`{"id":"t_child1"}`)
	if _, ok, steps := a.observe(workerPost); !ok || len(steps) != 0 {
		t.Fatalf("worker post_tool_call ok=%v steps=%+v", ok, steps)
	}
	if a.cardsFiled.Load() || a.trace.turnCardsFiled {
		t.Fatalf("worker kanban_create mutated coordinator card-filing flags: cardsFiled=%v turnCardsFiled=%v", a.cardsFiled.Load(), a.trace.turnCardsFiled)
	}
	if a.turnCalls != 0 {
		t.Fatalf("worker post_tool_call incremented coordinator turnCalls = %d, want 0", a.turnCalls)
	}
}
