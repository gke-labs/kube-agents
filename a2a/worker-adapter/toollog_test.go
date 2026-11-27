package workeradapter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// toolLogHarness is a toolLog over a buffer and a clock the test moves, fed
// with Claude Code stream-json lines through the Claude decoder: the path a
// profile task's tool events take.
type toolLogHarness struct {
	t   *testing.T
	log *toolLog
	buf *bytes.Buffer
	now time.Time
}

func newToolLogHarness(t *testing.T, skills ...string) *toolLogHarness {
	t.Helper()
	h := &toolLogHarness{t: t, buf: &bytes.Buffer{}, now: time.Unix(1_700_000_000, 0)}
	h.log = newToolLog(slog.New(slog.NewJSONHandler(h.buf, nil)), "task-1", &ToolLines{Skills: skills})
	return h
}

// feed decodes one harness line, stamps its events with the test's clock as
// the adapter's stdout scanner stamps them with the real one, and hands the
// tool events to the log, as the adapter's event loop does.
func (h *toolLogHarness) feed(line string) {
	h.t.Helper()
	evs, err := claudeDecoder{}.Decode([]byte(line))
	if err != nil {
		h.t.Fatalf("decode %s: %v", line, err)
	}
	for _, ev := range evs {
		ev.At = h.now
		switch ev.Kind {
		case EventActivity:
			h.log.started(ev)
		case EventToolResult:
			h.log.ended(ev)
		}
	}
}

func (h *toolLogHarness) advance(d time.Duration) { h.now = h.now.Add(d) }

// lines is the log so far as decoded JSON objects, without slog's own time
// and level keys.
func (h *toolLogHarness) lines() []map[string]any {
	h.t.Helper()
	return toolLines(h.t, h.buf.String())
}

// toolLines is the per-tool lines of a JSON log, each without slog's own
// time and level keys.
func toolLines(t *testing.T, log string) []map[string]any {
	t.Helper()
	var out []map[string]any
	for _, raw := range strings.Split(strings.TrimSpace(log), "\n") {
		if raw == "" {
			continue
		}
		var line map[string]any
		if err := json.Unmarshal([]byte(raw), &line); err != nil {
			t.Fatalf("log line %q: %v", raw, err)
		}
		if msg := line["msg"]; msg != toolCallStartedMsg && msg != toolCallEndedMsg {
			continue
		}
		delete(line, "time")
		delete(line, "level")
		out = append(out, line)
	}
	return out
}

// wantLines compares whole lines, so a key the line must not have (the
// tool's input, its result, audit_event) fails as an unexpected key.
func wantLines(t *testing.T, got []map[string]any, want ...map[string]any) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("%d lines, want %d: %v", len(got), len(want), got)
	}
	for i := range want {
		if !maps.Equal(got[i], want[i]) {
			t.Errorf("line %d\n got %v\nwant %v", i, got[i], want[i])
		}
	}
}

func claudeUse(id, tool, input string) string {
	return fmt.Sprintf(`{"type":"assistant","message":{"content":[{"type":"tool_use","id":%q,"name":%q,"input":%s}]}}`, id, tool, input)
}

func claudeAnswer(id, content string, failed bool) string {
	return fmt.Sprintf(`{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":%q,"content":%q,"is_error":%t}]}}`, id, content, failed)
}

// A tool call that succeeds leaves a start line and an end line with the
// same call id, the status from the error flag and the duration the adapter
// measured, and nothing of the input or the result.
func TestAToolCallLeavesAStartLineAndAnEndLine(t *testing.T) {
	h := newToolLogHarness(t)
	h.feed(claudeUse("toolu_1", "Bash", `{"command":"echo INPUT-MARKER"}`))
	h.advance(1500 * time.Millisecond)
	h.feed(claudeAnswer("toolu_1", "RESULT-MARKER", false))

	wantLines(t, h.lines(),
		map[string]any{"msg": "tool call started", "task": "task-1", "call": "toolu_1", "tool": "Bash", "status": "started"},
		map[string]any{"msg": "tool call ended", "task": "task-1", "call": "toolu_1", "tool": "Bash", "status": "ok", "durationMs": float64(1500)},
	)
	for _, marker := range []string{"INPUT-MARKER", "RESULT-MARKER", "audit_event"} {
		if strings.Contains(h.buf.String(), marker) {
			t.Errorf("the log holds %q:\n%s", marker, h.buf.String())
		}
	}
}

// A result the harness flags as an error ends the call with status error.
func TestAToolCallThatFailsEndsWithStatusError(t *testing.T) {
	h := newToolLogHarness(t)
	h.feed(claudeUse("toolu_1", "Bash", `{"command":"false"}`))
	h.advance(20 * time.Millisecond)
	h.feed(claudeAnswer("toolu_1", "exit status 1", true))

	lines := h.lines()
	if len(lines) != 2 || lines[1]["status"] != "error" || lines[1]["durationMs"] != float64(20) {
		t.Fatalf("lines %v, want an end line with status error after 20 ms", lines)
	}
}

// Two calls that run at the same time, answered in the other order, each
// end with their own call id, tool and duration.
func TestTwoParallelToolCallsEachEndWithTheirOwnIDAndDuration(t *testing.T) {
	h := newToolLogHarness(t)
	h.feed(`{"type":"assistant","message":{"content":[` +
		`{"type":"tool_use","id":"toolu_a","name":"Read","input":{"file_path":"a"}},` +
		`{"type":"tool_use","id":"toolu_b","name":"Grep","input":{"pattern":"b"}}]}}`)
	h.advance(300 * time.Millisecond)
	h.feed(claudeAnswer("toolu_b", "b", false))
	h.advance(900 * time.Millisecond)
	h.feed(claudeAnswer("toolu_a", "a", true))

	wantLines(t, h.lines(),
		map[string]any{"msg": "tool call started", "task": "task-1", "call": "toolu_a", "tool": "Read", "status": "started"},
		map[string]any{"msg": "tool call started", "task": "task-1", "call": "toolu_b", "tool": "Grep", "status": "started"},
		map[string]any{"msg": "tool call ended", "task": "task-1", "call": "toolu_b", "tool": "Grep", "status": "ok", "durationMs": float64(300)},
		map[string]any{"msg": "tool call ended", "task": "task-1", "call": "toolu_a", "tool": "Read", "status": "error", "durationMs": float64(1200)},
	)
}

// The skill key is on both lines of a skill call and on no other tool's. It
// holds the skill's name only when the persona has that skill.
func TestTheSkillKeyHoldsOnlyASkillOfThePersona(t *testing.T) {
	for _, tc := range []struct {
		name, tool, input string
		want              any // the skill key's value, or nil for no key
	}{
		{"a skill of the persona", "Skill", `{"skill":"triage","args":"INPUT-MARKER"}`, "triage"},
		{"a skill the persona does not have", "Skill", `{"skill":"free text INPUT-MARKER"}`, "other"},
		{"a skill call with no name", "Skill", `{"args":"INPUT-MARKER"}`, "other"},
		{"a skill call whose name is not a string", "Skill", `{"skill":7}`, "other"},
		{"a skill call with no input object", "Skill", `"INPUT-MARKER"`, "other"},
		{"another tool with a skill key in its input", "Bash", `{"skill":"triage"}`, nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newToolLogHarness(t, "notes", "triage")
			h.feed(claudeUse("toolu_1", tc.tool, tc.input))
			h.feed(claudeAnswer("toolu_1", "loaded", false))
			lines := h.lines()
			if len(lines) != 2 {
				t.Fatalf("%d lines, want 2: %v", len(lines), lines)
			}
			for i, line := range lines {
				if got, has := line["skill"]; got != tc.want || has != (tc.want != nil) {
					t.Errorf("line %d: skill %v (present %t), want %v", i, got, has, tc.want)
				}
			}
			if strings.Contains(h.buf.String(), "INPUT-MARKER") {
				t.Errorf("the log holds the input:\n%s", h.buf.String())
			}
		})
	}
}

// A result with no start line on record still gets its end line, without a
// duration: a result with no id, one whose id was never started, and a call
// past the bound on calls waiting for a result.
func TestAResultWithNoStartOnRecordEndsWithNoDuration(t *testing.T) {
	h := newToolLogHarness(t)
	h.feed(`{"type":"user","message":{"content":[{"type":"tool_result","content":"x"}]}}`)
	h.feed(claudeAnswer("toolu_unknown", "x", true))
	wantLines(t, h.lines(),
		map[string]any{"msg": "tool call ended", "task": "task-1", "call": "", "tool": "", "status": "ok"},
		map[string]any{"msg": "tool call ended", "task": "task-1", "call": "toolu_unknown", "tool": "", "status": "error"},
	)

	h = newToolLogHarness(t)
	for i := range toolLogOpenMax + 1 {
		h.feed(claudeUse(fmt.Sprintf("toolu_%d", i), "Bash", `{}`))
	}
	h.advance(time.Second)
	h.feed(claudeAnswer("toolu_0", "x", false))
	h.feed(claudeAnswer(fmt.Sprintf("toolu_%d", toolLogOpenMax), "x", false))
	lines := h.lines()
	kept, past := lines[len(lines)-2], lines[len(lines)-1]
	if kept["durationMs"] != float64(1000) {
		t.Errorf("a call inside the bound ended as %v, want a duration of 1000 ms", kept)
	}
	if _, has := past["durationMs"]; has || past["status"] != "ok" {
		t.Errorf("the call past the bound ended as %v, want status ok and no duration", past)
	}
	if len(h.log.open) != toolLogOpenMax-1 {
		t.Errorf("%d calls still wait for a result, want %d", len(h.log.open), toolLogOpenMax-1)
	}
}

// The call id and the tool's name are the harness's text: each is cut. A cut
// id joins no result to a call, so two long ids that share their first bytes
// are not taken for one call.
func TestALongCallIDAndToolNameAreCut(t *testing.T) {
	h := newToolLogHarness(t)
	long := strings.Repeat("x", 3*toolLineFieldMax)
	h.feed(claudeUse(long+"-a", long, `{}`))
	h.feed(claudeUse(long+"-b", "Bash", `{}`))
	h.advance(time.Second)
	h.feed(claudeAnswer(long+"-a", "x", false))
	h.feed(claudeAnswer(long+"-b", "x", false))
	lines := h.lines()
	if len(lines) != 4 {
		t.Fatalf("%d lines, want 4", len(lines))
	}
	cut := func(line map[string]any, key string) {
		t.Helper()
		if got := line[key].(string); len(got) > toolLineFieldMax+len("…") || !strings.HasSuffix(got, "…") || !strings.HasPrefix(long, strings.TrimSuffix(got, "…")) {
			t.Errorf("%s is %q (%d bytes), want a cut and marked value inside %d bytes", key, got, len(got), toolLineFieldMax)
		}
	}
	cut(lines[0], "tool")
	for i, line := range lines {
		cut(line, "call")
		if _, has := line["durationMs"]; has {
			t.Errorf("line %d joined a cut id to a call: %v", i, line)
		}
	}
	if lines[2]["tool"] != "" || lines[3]["tool"] != "" {
		t.Errorf("an end line took the tool of a call that it was not joined to: %v, %v", lines[2], lines[3])
	}
	if len(h.log.open) != 0 {
		t.Errorf("%d calls with a cut id were kept", len(h.log.open))
	}
}

// A duration needs the read time of both events: with one missing the end
// line has none, never a duration from a zero time.
func TestAnEventWithNoReadTimeGivesNoDuration(t *testing.T) {
	var buf bytes.Buffer
	l := newToolLog(slog.New(slog.NewJSONHandler(&buf, nil)), "task-1", &ToolLines{})
	at := time.Unix(1_700_000_000, 0)
	l.started(Event{Kind: EventActivity, Tool: "Bash", CallID: "no-start-time"})
	l.ended(Event{Kind: EventToolResult, CallID: "no-start-time", At: at})
	l.started(Event{Kind: EventActivity, Tool: "Bash", CallID: "no-end-time", At: at})
	l.ended(Event{Kind: EventToolResult, CallID: "no-end-time"})
	lines := toolLines(t, buf.String())
	if len(lines) != 4 {
		t.Fatalf("%d lines, want 4", len(lines))
	}
	for _, i := range []int{1, 3} {
		if _, has := lines[i]["durationMs"]; has || lines[i]["tool"] != "Bash" {
			t.Errorf("line %d is %v, want the call's tool and no duration", i, lines[i])
		}
	}
}

// The read time of an event is taken where the adapter reads the harness's
// stdout, ahead of whatever takes the events: a duration must not include
// the time the event loop is held by a publish. The test takes no event
// until the scanner has read every line, so a read time that is missing, or
// later than that moment, was not taken at the read. The wait is a backstop
// for a scanner that never ends, far above the milliseconds a run takes.
func TestEachEventCarriesWhenTheAdapterReadItsLine(t *testing.T) {
	stream := filepath.Join(t.TempDir(), "stream.jsonl")
	if err := os.WriteFile(stream, []byte(claudeUse("toolu_1", "Bash", `{}`)+"\n"+claudeAnswer("toolu_1", "x", false)+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	harness := stub(t, fmt.Sprintf("read first || exit 1\ncat %s\n", stream))
	p, err := startHarness(harness, os.Environ(), "go", reapBoundForTest, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		p.kill(0)
		_ = p.cmd.Wait()
		p.reaped()
	})
	select {
	case <-p.scanDone:
	case <-time.After(30 * time.Second):
		t.Fatal("the stdout scanner did not reach the end of a two-line stream")
	}
	taken := time.Now()
	var kinds []EventKind
	for evs := range p.events {
		for _, ev := range evs {
			kinds = append(kinds, ev.Kind)
			if ev.At.IsZero() || ev.At.After(taken) {
				t.Errorf("event %v was read at %v, want a time at or before %v, when the test took the first event", ev.Kind, ev.At, taken)
			}
		}
	}
	if !slices.Equal(kinds, []EventKind{EventActivity, EventToolResult}) {
		t.Errorf("events %v, want one tool call and its result", kinds)
	}
}

// With no ToolLines the adapter writes no per-tool line.
func TestNoToolLinesWritesNoLine(t *testing.T) {
	var buf bytes.Buffer
	l := newToolLog(slog.New(slog.NewJSONHandler(&buf, nil)), "task-1", nil)
	l.started(Event{Kind: EventActivity, Tool: "Bash", CallID: "toolu_1"})
	l.ended(Event{Kind: EventToolResult, CallID: "toolu_1"})
	if l != nil || buf.Len() != 0 {
		t.Errorf("a log with no ToolLines wrote %q", buf.String())
	}
}

// The skill tool has one name in two places: the persona renderer puts it on
// the harness's allowlist of a persona with skills (lib/persona/claudecode),
// and the Claude driver reads a call of it as a skill call (claudeSkillTool).
// So the driver must read exactly one tool of that allowlist as a skill call.
// With the two names apart, one of two faults follows: a renderer with
// another name leaves the harness's skill tool off the allowlist, and a
// driver with another name logs no skill key for a skill call. An allowlist
// entry can carry a scope in brackets after the tool's name; the name is the
// part before it.
func TestTheDriverReadsThePersonasSkillToolAsASkillCall(t *testing.T) {
	out, skills, err := renderPersona(ProfileWorkerConfig{PersonaDir: examplePersona, Harness: persona.ClaudeCode},
		slog.New(slog.NewJSONHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	if len(skills) == 0 {
		t.Fatal("the example persona has no skill, so its allowlist has no skill tool to read")
	}
	var read []string
	for _, entry := range out.Allowed {
		tool, _, _ := strings.Cut(entry, "(")
		evs, err := claudeDecoder{}.Decode([]byte(claudeUse("toolu_1", tool, `{"skill":"triage"}`)))
		if err != nil || len(evs) != 1 {
			t.Fatalf("a call of the allowed tool %q decodes to %v, %v; want one event", tool, evs, err)
		}
		if evs[0].SkillCall && !slices.Contains(read, tool) {
			read = append(read, tool)
		}
	}
	if len(read) != 1 {
		t.Errorf("the driver reads %v of the allowlist %v as a skill call, want exactly the persona's skill tool", read, out.Allowed)
	}
}

// End to end: a profile worker's task leaves the two lines on the worker's
// log, with the task's id and the persona's skill name, and with nothing of
// the tool's input or result. The activity entry on the bus keeps the tool
// and its input, and it carries the call's id beside them, as it does for
// each driver that reads one.
func TestAProfileTaskLeavesItsToolLinesOnTheWorkersLog(t *testing.T) {
	url := startServer(t)
	d := newFakeDispatcher(t, url)
	root := t.TempDir()
	stream := filepath.Join(root, "stream.jsonl")
	if err := os.WriteFile(stream, []byte(strings.Join([]string{
		claudeUse("toolu_e2e", "Skill", `{"skill":"triage","args":"INPUT-MARKER"}`),
		claudeAnswer("toolu_e2e", "RESULT-MARKER", false),
		`{"type":"result","subtype":"success","result":"done"}`,
	}, "\n")+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	harness := stub(t, fmt.Sprintf("read first || exit 1\ncat %s\n", stream))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	cfg := profileWorkerConfig(t, url, harness)
	var logBuf syncBuffer
	cfg.Logger = slog.New(slog.NewJSONHandler(&logBuf, nil))
	ln, addr := readyListener(t)
	go func() {
		_ = RunProfileWorker(ctx, cfg, ProfileWorkerConfig{
			Tier: pwTier, ReadyAddr: addr, readyListener: ln, deadlineMargin: time.Second,
			PersonaDir: examplePersona, Harness: persona.ClaudeCode, ConfigRoot: root,
		})
	}()
	waitReady(t, addr)

	const task = "task-tool-lines"
	d.offer(task, profileSubmission(t, task, "do the thing", nil), 5)
	d.nextAccept(5 * time.Second)
	d.send(lib.DispatchMessage{Type: lib.DispatchConfirm, TaskID: task})
	if _, final := drainLeases(d, task, 20*time.Second); !final {
		t.Fatal("no final renewal")
	}

	lines := toolLines(t, logBuf.String())
	if len(lines) != 2 {
		t.Fatalf("%d per-tool lines, want 2:\n%s", len(lines), logBuf.String())
	}
	// The duration is the real clock's here, so only its presence is held:
	// it is there when the stdout scanner stamped both events (Event.At).
	// Its value is the pure tests'.
	if _, has := lines[1]["durationMs"]; !has {
		t.Errorf("the end line has no duration: %v", lines[1])
	}
	delete(lines[1], "durationMs")
	wantLines(t, lines,
		map[string]any{"msg": "tool call started", "task": task, "call": "toolu_e2e", "tool": "Skill", "status": "started", "skill": "triage"},
		map[string]any{"msg": "tool call ended", "task": task, "call": "toolu_e2e", "tool": "Skill", "status": "ok", "skill": "triage"},
	)
	for _, marker := range []string{"INPUT-MARKER", "RESULT-MARKER"} {
		if strings.Contains(logBuf.String(), marker) {
			t.Errorf("the worker's log holds %q:\n%s", marker, logBuf.String())
		}
	}

	activity := foldTask(t, testClient(t, url), pwProfile, task).Artifact(lib.ArtifactActivity)
	if activity == nil || len(activity.Parts) != 1 {
		t.Fatalf("the activity artifact is %+v, want one entry", activity)
	}
	var entry map[string]json.RawMessage
	if err := json.Unmarshal(activity.Parts[0].Data, &entry); err != nil {
		t.Fatal(err)
	}
	if keys := slices.Sorted(maps.Keys(entry)); !slices.Equal(keys, []string{"callId", "input", "tool"}) ||
		string(entry["tool"]) != `"Skill"` || !strings.Contains(string(entry["input"]), "INPUT-MARKER") ||
		string(entry["callId"]) != `"toolu_e2e"` {
		t.Errorf("the activity entry is %s, want the tool, its input and the call's id", activity.Parts[0].Data)
	}
}
