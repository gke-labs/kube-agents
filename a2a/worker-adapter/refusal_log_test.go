package workeradapter

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"
)

// TestOnlyARefusedCallLineReachesTheWorkerLog: of everything a harness
// writes to stderr, only lines in the refused-call format are logged, each
// once with its tool and reason, whether a write splits a line or holds
// several; other text, a line that only starts like one, and an over-long
// line are never logged, in whole or in part.
func TestOnlyARefusedCallLineReachesTheWorkerLog(t *testing.T) {
	var out bytes.Buffer
	r := &refusalLines{log: slog.New(slog.NewJSONHandler(&out, nil)).With("task", "t1")}
	for _, w := range []string{
		"some model text: tool refused: \"bash\" (no allow rule matches)\n",
		"tool refused: \"ba",
		"sh\" (no allow rule matches)\ntool refused: \"read_file\" (outside the workspace)\n",
		"tool refused: \"bash\" (no allow rule matches) and the command was secret\n",
		"tool refused: \"x\\nforged\" (no such tool)\n",
		"tool refused: \"" + strings.Repeat("a", 600) + "\" (no such tool)\n",
		"tool refused: \"grep\" (outside the workspace) call \"adk-7f3a\"\n",
		"tool refused: \"grep\" (outside the workspace) call \"x\" and more\n",
		"tool refused: \"edit_file\" (not in the turn's grant)",
	} {
		if _, err := r.Write([]byte(w)); err != nil {
			t.Fatal(err)
		}
	}
	var got []string
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var rec map[string]any
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			t.Fatalf("log line %q: %v", line, err)
		}
		if rec["task"] != "t1" {
			t.Errorf("record %v has no task", rec)
		}
		entry := rec["tool"].(string) + "|" + rec["reason"].(string)
		if call, ok := rec["call"].(string); ok {
			entry += "|" + call
		}
		got = append(got, entry)
	}
	want := []string{"bash|no allow rule matches", "read_file|outside the workspace", "x\nforged|no such tool", "grep|outside the workspace|adk-7f3a"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Errorf("logged %q, want %q", got, want)
	}
	if strings.Contains(out.String(), "secret") || strings.Contains(out.String(), "model text") {
		t.Errorf("non-refusal text reached the log: %s", out.String())
	}
}

// TestARefusedCallTheHarnessReportsReachesTheWorkerLog: end to end, a
// refused-call line the harness writes to stderr lands in the adapter's log
// with the task's id; the rest of its stderr doesn't.
func TestARefusedCallTheHarnessReportsReachesTheWorkerLog(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-refusal-a1", "task-refusal-1"
	submit(t, c, session, taskID, "try it")
	harness := stub(t, `
read first || exit 1
echo 'chatter with a secret' >&2
echo 'tool refused: "bash" (no allow rule matches)' >&2
echo '{"type":"result","subtype":"success","result":"ok"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	var logs syncBuffer
	cfg.Logger = slog.New(slog.NewJSONHandler(&logs, nil))
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil {
		t.Fatalf("run: %v", out.err)
	}
	var found int
	for _, line := range strings.Split(logs.String(), "\n") {
		if strings.Contains(line, "the harness refused a tool call") {
			found++
			if !strings.Contains(line, `"task":"`+taskID+`"`) || !strings.Contains(line, `"tool":"bash"`) {
				t.Errorf("refusal record %s", line)
			}
		}
	}
	if found != 1 {
		t.Errorf("%d refusal records, want 1: %s", found, logs.String())
	}
	if strings.Contains(logs.String(), "secret") {
		t.Errorf("other stderr reached the log: %s", logs.String())
	}
}
