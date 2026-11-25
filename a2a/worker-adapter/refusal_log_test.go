package workeradapter

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
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

// TestACoreAgentToolCallsActivityCarriesItsCallID: end to end, a core-agent
// tool call's activity entry carries the harness's call id as callId,
// beside the input and never in it, so it joins a refused-call record; the
// input is passed as it was. A harness that gives no id, Claude Code here,
// gets no callId key at all.
func TestACoreAgentToolCallsActivityCarriesItsCallID(t *testing.T) {
	for _, c := range []struct {
		name, script string
		driver       Driver
		wantCallID   string
	}{
		{"core-agent", `
read first || exit 1
echo '{"type":"init","session":"s1","model":"m"}'
echo '{"type":"tool_call","id":"adk-42","tool":"bash","input":{"command":"kubectl get pods"}}'
echo '{"type":"tool_result","id":"adk-42","tool":"bash","text":"ok"}'
echo '{"type":"result","text":"done"}'
`, NewCoreAgentDriver(coreagentwire.Policy{}, nil), "adk-42"},
		{"claude-code", `
read first || exit 1
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_1","name":"Bash","input":{"command":"kubectl get pods"}}]}}'
echo '{"type":"result","subtype":"success","result":"done"}'
`, nil, ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			url := startServer(t)
			cl := testClient(t, url)
			session, taskID := "chat-callid-"+c.name, "task-callid-"+c.name
			submit(t, cl, session, taskID, "look")
			cfg := adapterConfig(url, taskID, session, stub(t, c.script))
			// The core-agent driver checks it is the container's init before
			// every process; a test stands in for that (useRespawnScope).
			useRespawnScope(t)
			cfg.Driver = c.driver
			if out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second); out.err != nil {
				t.Fatalf("run: %v", out.err)
			}
			a := foldTask(t, cl, session, taskID).Artifact(lib.ArtifactActivity)
			if a == nil || len(a.Parts) != 1 {
				t.Fatalf("activity artifact %+v, want one entry", a)
			}
			var entry map[string]json.RawMessage
			if err := json.Unmarshal(a.Parts[0].Data, &entry); err != nil {
				t.Fatal(err)
			}
			var input map[string]any
			if err := json.Unmarshal(entry["input"], &input); err != nil || len(input) != 1 || input["command"] != "kubectl get pods" {
				t.Errorf("input %s, want exactly the call's arguments", entry["input"])
			}
			raw, has := entry["callId"]
			if c.wantCallID == "" {
				if has {
					t.Errorf("entry %s has a callId the harness never gave", a.Parts[0].Data)
				}
				return
			}
			var id string
			if err := json.Unmarshal(raw, &id); err != nil || id != c.wantCallID {
				t.Errorf("callId %s, want %q", raw, c.wantCallID)
			}
		})
	}
}
