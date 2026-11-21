package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// sharedRefusalFixture holds the tool results this predicate and
// deploy/docker/patches/permission_refusal_stop.py both classify
// (test_permission_refusal_stop.SharedFixtureTests reads the same file).
const sharedRefusalFixture = "../../tests/conformance/fixtures/write-refusal-results.json"

type refusalCase struct {
	Name    string `json:"name"`
	Expect  string `json:"expect"`
	Kind    string `json:"kind"`
	Failed  bool   `json:"failed"`
	Command string `json:"command"`
	Result  string `json:"result"`
}

func loadRefusalCases(t *testing.T) []refusalCase {
	t.Helper()
	raw, err := os.ReadFile(sharedRefusalFixture)
	if err != nil {
		t.Fatalf("read the shared fixture: %v", err)
	}
	var doc struct {
		Cases []refusalCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("decode the shared fixture: %v", err)
	}
	if len(doc.Cases) == 0 {
		t.Fatal("the shared fixture holds no cases")
	}
	return doc.Cases
}

// claudeToolCall is a tool call and its result as Claude Code's stream-json
// prints them: an assistant line with the tool_use, then a user line with
// the tool_result that answers it.
func claudeToolCall(t *testing.T, id, command, result string, failed bool) []string {
	t.Helper()
	use, err := json.Marshal(map[string]any{
		"type": "assistant",
		"message": map[string]any{"content": []any{map[string]any{
			"type": "tool_use", "id": id, "name": "Bash",
			"input": map[string]any{"command": command, "description": "run it"},
		}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	res, err := json.Marshal(map[string]any{
		"type": "user",
		"message": map[string]any{"role": "user", "content": []any{map[string]any{
			"type": "tool_result", "tool_use_id": id, "content": result, "is_error": failed,
		}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return []string{string(use), string(res)}
}

// replayToolCall runs a recorded tool call through the supervise loop's two
// readers: the tool_use recorder and the tool_result check.
func replayToolCall(t *testing.T, lines []string) *writeRefusal {
	t.Helper()
	commands := map[string]string{}
	var refused *writeRefusal
	for _, line := range lines {
		var ev harnessEvent
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("decode %s: %v", line, err)
		}
		switch ev.Type {
		case "assistant":
			recordToolCommands(commands, ev)
		case "user":
			if r := firstWriteRefusal(commands, ev); r != nil && refused == nil {
				refused = r
			}
		}
	}
	return refused
}

// TestWriteRefusalSharedFixture: every shared case classifies as labelled,
// as printed, inside a Hermes terminal result's JSON, and inside a Claude
// Code tool_result.
func TestWriteRefusalSharedFixture(t *testing.T) {
	sawHalt, sawNoHalt := false, false
	for _, c := range loadRefusalCases(t) {
		want := c.Kind
		if want == "none" {
			want = ""
		}
		if (want == refusalPolicy) != (c.Expect == "halt") {
			t.Fatalf("%s: kind %q disagrees with expect %q", c.Name, c.Kind, c.Expect)
		}
		sawHalt = sawHalt || c.Expect == "halt"
		sawNoHalt = sawNoHalt || c.Expect == "no_halt"
		wrapped, err := json.Marshal(map[string]any{"output": c.Result, "exit_code": 77, "error": nil})
		if err != nil {
			t.Fatal(err)
		}
		for _, result := range []string{c.Result, string(wrapped)} {
			if got := classifyRefusal(c.Command, result, c.Failed); got != want {
				t.Errorf("%s: classifyRefusal = %q, want %q", c.Name, got, want)
			}
		}
		refused := replayToolCall(t, claudeToolCall(t, "toolu_1", c.Command, c.Result, c.Failed))
		if (refused != nil) != (c.Expect == "halt") {
			t.Errorf("%s: halted=%v, want %s", c.Name, refused != nil, c.Expect)
		}
		if refused != nil && (refused.rule != definitiveRule || !verbIsMutating(strings.Fields(refused.verb))) {
			t.Errorf("%s: refusal %+v", c.Name, refused)
		}
	}
	if !sawHalt || !sawNoHalt {
		t.Fatalf("the fixture needs both labels: halt=%v no_halt=%v", sawHalt, sawNoHalt)
	}
}

// TestWriteRefusalInClaudeCodeEvents: the reader takes a tool_result's
// content as a string or as text blocks, matches it to its call by id, and
// a `policy render: local` line keeps a mutating verb from halting.
func TestWriteRefusalInClaudeCodeEvents(t *testing.T) {
	const refusal = "This is a permission boundary, not an error to work around: the change is refused however it is attempted.\npolicy rule: kubernetes.read-only\npolicy verb: rollout restart deploy/x"
	blocks := `{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"a","is_error":true,"content":[{"type":"text","text":` +
		mustJSON(t, refusal) + `}]}]}}`
	var ev harnessEvent
	if err := json.Unmarshal([]byte(blocks), &ev); err != nil {
		t.Fatal(err)
	}
	r := firstWriteRefusal(map[string]string{}, ev)
	if r == nil || r.verb != "rollout restart" {
		t.Fatalf("text-block content: %+v", r)
	}
	if r := replayToolCall(t, claudeToolCall(t, "b", "kubectl rollout restart deploy/x", refusal+"\npolicy render: local", true)); r != nil {
		t.Fatalf("a local render halted: %+v", r)
	}
	// An exit-0 call is read against its own command: the chained write counts,
	// a call with another id's command does not borrow it.
	lines := claudeToolCall(t, "c", "kubectl rollout restart deploy/x; kubectl get deploy x", refusal, false)
	if r := replayToolCall(t, lines); r == nil {
		t.Fatal("a refused write chained before a read did not halt")
	}
	other := claudeToolCall(t, "d", "kubectl rollout restart deploy/x", "ok", false)
	mismatched := claudeToolCall(t, "e", "kubectl logs deploy/x", refusal, false)
	if r := replayToolCall(t, append(other, mismatched...)); r != nil {
		t.Fatalf("an exit-0 log read halted on another call's command: %+v", r)
	}
	if got := toolCommandText(json.RawMessage(`{"command":"kubectl get x","timeout":5,"description":"look"}`)); got != "kubectl get x\nlook" {
		t.Fatalf("command text %q", got)
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

// refusalStub is a harness that answers the prompt with the first tool
// calls, then sleeps for pause seconds before writing markerFile and the
// rest: a harness the adapter stopped after the first part never reaches the
// second. It writes its pid to pidFile once it has started.
func refusalStub(t *testing.T, first, rest []string, pause int, pidFile, markerFile string) []string {
	t.Helper()
	dir := t.TempDir()
	firstFile, restFile := filepath.Join(dir, "first.jsonl"), filepath.Join(dir, "rest.jsonl")
	if err := os.WriteFile(firstFile, []byte(strings.Join(first, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(restFile, []byte(strings.Join(append(rest, `{"type":"result","subtype":"success","result":"finished the task"}`), "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return stub(t, fmt.Sprintf(`
echo '{"type":"system","subtype":"init","session_id":"stub-refusal"}'
echo "$$" > %[1]s.tmp && mv %[1]s.tmp %[1]s
read first || exit 1
cat %[2]s
sleep %[5]d
touch %[3]s
cat %[4]s
`, pidFile, firstFile, markerFile, restFile, pause))
}

func refusalCaseNamed(t *testing.T, name string) refusalCase {
	t.Helper()
	for _, c := range loadRefusalCases(t) {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no shared case %q", name)
	return refusalCase{}
}

func waitDead(t *testing.T, pidFile string) {
	t.Helper()
	raw, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("pid file: %v", err)
	}
	var pid int
	fmt.Sscanf(strings.TrimSpace(string(raw)), "%d", &pid)
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if syscall.Kill(pid, 0) != nil {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("harness pid %d still alive after the refusal", pid)
}

// runRefusalTask runs one task through the adapter, with the refusal stop on
// when stop is, and returns its outcome, its terminal text and the stub's
// pid and marker files.
func runRefusalTask(t *testing.T, taskID string, first, rest []string, stop, expectStop bool) (runOutcome, string, string, string) {
	t.Helper()
	url := startServer(t)
	c := testClient(t, url)
	session := "chat-" + taskID
	submit(t, c, session, taskID, "look at kube-dns")
	dir := t.TempDir()
	pidFile, markerFile := filepath.Join(dir, "pid"), filepath.Join(dir, "reached-the-rest")
	// A harness meant to be stopped pauses past the task's deadline, so only
	// the stop ends it in time; one meant to finish doesn't pause.
	pause := 0
	if expectStop {
		pause = 60
	}
	cfg := adapterConfig(url, taskID, session, refusalStub(t, first, rest, pause, pidFile, markerFile))
	cfg.StopOnWriteRefusal = stop
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 60*time.Second)
	return out, terminalText(t, url, session, taskID), pidFile, markerFile
}

// TestWriteRefusalStopsTheHarness: the first definitive refusal stops the
// harness (its process is gone, it never reaches what follows) and ends the
// task rejected, naming the rule and the verb and nothing of the output.
func TestWriteRefusalStopsTheHarness(t *testing.T) {
	scale := refusalCaseNamed(t, "refused scale")
	first := claudeToolCall(t, "toolu_1", scale.Command, scale.Result, scale.Failed)
	out, text, pidFile, markerFile := runRefusalTask(t, "task-refusal-1", first, nil, true, true)
	if out.err != nil || out.res.State != lib.StateRejected {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	waitDead(t, pidFile)
	if _, err := os.Stat(markerFile); err == nil {
		t.Fatal("the harness ran on past the refusal")
	}
	if !strings.HasPrefix(text, "reason: write-refused - ") || !strings.Contains(text, "kubernetes.read-only") || !strings.Contains(text, "verb scale") {
		t.Fatalf("terminal %q", text)
	}
	for _, leaked := range []string{"kube-dns", "permission boundary", "partial output", "finished the task"} {
		if strings.Contains(text, leaked) {
			t.Fatalf("terminal carries %q: %q", leaked, text)
		}
	}
}

// TestWriteRefusalSecondIsNeverReached: of two refusals the task stops at
// the first, and the second is never read.
func TestWriteRefusalSecondIsNeverReached(t *testing.T) {
	scale := refusalCaseNamed(t, "refused scale")
	del := refusalCaseNamed(t, "refused delete")
	first := claudeToolCall(t, "toolu_1", scale.Command, scale.Result, scale.Failed)
	rest := claudeToolCall(t, "toolu_2", del.Command, del.Result, del.Failed)
	out, text, pidFile, markerFile := runRefusalTask(t, "task-refusal-2", first, rest, true, true)
	if out.res.State != lib.StateRejected {
		t.Fatalf("state %q err %v", out.res.State, out.err)
	}
	waitDead(t, pidFile)
	if _, err := os.Stat(markerFile); err == nil {
		t.Fatal("the harness reached the second refusal")
	}
	if !strings.Contains(text, "verb scale") || strings.Contains(text, "delete") {
		t.Fatalf("terminal %q, want the first refusal's verb only", text)
	}
}

// TestWriteRefusalNonDefinitiveLetsTheTaskFinish: every no_halt case of the
// shared fixture (a gcp.read-only refusal, a refused read spelling, a local
// render, a `policy render: local` line, an API 403, ...) passes through, and
// the task completes with the harness's own answer. With the stop off, a
// definitive refusal passes through too.
func TestWriteRefusalNonDefinitiveLetsTheTaskFinish(t *testing.T) {
	var calls []string
	for i, c := range loadRefusalCases(t) {
		if c.Expect == "no_halt" {
			calls = append(calls, claudeToolCall(t, fmt.Sprintf("toolu_%d", i), c.Command, c.Result, c.Failed)...)
		}
	}
	t.Run("stop on, no definitive refusal", func(t *testing.T) {
		out, _, _, markerFile := runRefusalTask(t, "task-refusal-3", calls, nil, true, false)
		if out.err != nil || out.res.State != lib.StateCompleted {
			t.Fatalf("state %q err %v", out.res.State, out.err)
		}
		if _, err := os.Stat(markerFile); err != nil {
			t.Fatal("the harness did not run to its end")
		}
	})
	t.Run("stop off, a definitive refusal", func(t *testing.T) {
		scale := refusalCaseNamed(t, "refused scale")
		first := claudeToolCall(t, "toolu_1", scale.Command, scale.Result, scale.Failed)
		out, _, _, _ := runRefusalTask(t, "task-refusal-4", first, nil, false, false)
		if out.err != nil || out.res.State != lib.StateCompleted {
			t.Fatalf("state %q err %v", out.res.State, out.err)
		}
	})
}

// TestWriteRefusalStopIsOnForProfilePods: a profile pod stops at a write
// refusal; a session pod keeps its own behaviour.
func TestWriteRefusalStopIsOnForProfilePods(t *testing.T) {
	profile := Config{ProfileExecutor: true}
	profile.applyDefaults()
	session := Config{Session: "chat-a"}
	session.applyDefaults()
	if !profile.StopOnWriteRefusal || session.StopOnWriteRefusal {
		t.Fatalf("profile=%v session=%v", profile.StopOnWriteRefusal, session.StopOnWriteRefusal)
	}
}
