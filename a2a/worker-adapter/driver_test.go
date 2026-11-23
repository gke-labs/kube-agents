package workeradapter

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// TestTheClaudeDecoderMapsEachLine: the Claude driver's decoder turns each
// stream-json line into the events supervise used to read off the line
// itself, field for field.
func TestTheClaudeDecoderMapsEachLine(t *testing.T) {
	cases := []struct {
		name string
		line string
		want []Event
	}{
		{"init", `{"type":"system","subtype":"init","session_id":"s-1","model":"m-1"}`,
			[]Event{{Kind: EventInit, Session: "s-1", Model: "m-1"}}},
		{"init with an off-shape model", `{"type":"system","subtype":"init","session_id":"s-1","model":7}`,
			[]Event{{Kind: EventInit, Session: "s-1", ModelDropped: true}}},
		{"another system line", `{"type":"system","subtype":"compact"}`, nil},
		{"assistant blocks in order", `{"type":"assistant","message":{"content":[` +
			`{"type":"thinking","thinking":"hm"},{"type":"thinking","thinking":""},` +
			`{"type":"text","text":"doing it"},{"type":"text","text":""},` +
			`{"type":"tool_use","name":"Write","input":{"file_path":"x"}},{"type":"image"}]}}`,
			[]Event{
				{Kind: EventThinking, Text: "hm"},
				{Kind: EventProgress, Text: "doing it"},
				{Kind: EventActivity, Tool: "Write", Input: json.RawMessage(`{"file_path":"x"}`)},
			}},
		{"assistant with no message", `{"type":"assistant"}`, nil},
		// Every tool_result on a user message is one event, its content as
		// the seen set reads it: a string as it is, a block list as its text
		// blocks' texts. A result with no content and any other block are
		// no event.
		{"user tool results", `{"type":"user","message":{"role":"user","content":[` +
			`{"type":"tool_result","tool_use_id":"t1","content":"one"},` +
			`{"type":"text","text":"not a result"},` +
			`{"type":"tool_result","tool_use_id":"t2","content":[{"type":"text","text":"a"},{"type":"image","source":{"data":"aW1n"}},{"type":"text","text":"b"}]},` +
			`{"type":"tool_result","tool_use_id":"t3"},` +
			`{"type":"tool_result","tool_use_id":"t4","content":"four"}]}}`,
			[]Event{
				{Kind: EventToolResult, ToolResult: json.RawMessage(`"one"`)},
				{Kind: EventToolResult, ToolResult: json.RawMessage(`["a","b"]`)},
				{Kind: EventToolResult, ToolResult: json.RawMessage(`"four"`)},
			}},
		{"user prompt echo", `{"type":"user","message":{"role":"user","content":"a prompt"}}`, nil},
		{"user with no message", `{"type":"user"}`, nil},
		{"success", `{"type":"result","subtype":"success","result":"done"}`,
			[]Event{{Kind: EventResult, Text: "done", Token: "success"}}},
		{"error subtype", `{"type":"result","subtype":"error_max_turns","is_error":true,"result":"out"}`,
			[]Event{{Kind: EventResult, Text: "out", Failed: true, Token: "error_max_turns"}}},
		{"is_error on success", `{"type":"result","subtype":"success","is_error":true,"result":"x"}`,
			[]Event{{Kind: EventResult, Text: "x", Failed: true, Token: "success"}}},
		// A result with no subtype is Failed with an empty token, which
		// supervise has always completed with the result's text.
		{"no subtype", `{"type":"result","result":"x"}`,
			[]Event{{Kind: EventResult, Text: "x", Failed: true}}},
		{"unknown type", `{"type":"stream_event"}`, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := claudeDriver{}.NewDecoder().Decode([]byte(tc.line))
			if err != nil {
				t.Fatalf("decode: %v", err)
			}
			for i := range got {
				got[i].Telemetry = resultTelemetry{}
			}
			if !reflect.DeepEqual(got, tc.want) {
				t.Fatalf("events\n got %+v\nwant %+v", got, tc.want)
			}
		})
	}
	if _, err := (claudeDriver{}).NewDecoder().Decode([]byte("not json")); err == nil {
		t.Fatal("a non-JSON line decoded")
	}
}

// TestTheClaudeDecoderCarriesTheResultTelemetry: a result line's figures
// reach the event as the line's own telemetry, with no model: the adapter
// adds the init line's.
func TestTheClaudeDecoderCarriesTheResultTelemetry(t *testing.T) {
	line := `{"type":"result","subtype":"success","result":"x","usage":{"input_tokens":3,"cache_read_input_tokens":4,"output_tokens":5},"total_cost_usd":0.5,"duration_ms":"slow"}`
	evs, err := claudeDriver{}.NewDecoder().Decode([]byte(line))
	if err != nil || len(evs) != 1 {
		t.Fatalf("decode: %v %+v", err, evs)
	}
	tel := evs[0].Telemetry
	if tel.usage.InputTokens != 7 || tel.usage.OutputTokens != 5 || tel.usage.Model != "" {
		t.Errorf("usage %+v", tel.usage)
	}
	if !tel.hasTotal || tel.total != 0.5 {
		t.Errorf("total %v %v", tel.total, tel.hasTotal)
	}
	if !reflect.DeepEqual(tel.dropped, []string{"duration_ms"}) {
		t.Errorf("dropped %v", tel.dropped)
	}
}

// TestTheClaudeDriverKeepsTheHarnessContract: the Claude driver's stdin line,
// resume flag, config directory and published name are what the adapter used
// to hard-code.
func TestTheClaudeDriverKeepsTheHarnessContract(t *testing.T) {
	d := Config{}.driver()
	if d.Harness() != persona.ClaudeCode {
		t.Errorf("harness %q", d.Harness())
	}
	line, err := d.EncodeUser("hi \"there\"")
	if err != nil || string(line) != `{"type":"user","message":{"role":"user","content":"hi \"there\""}}` {
		t.Errorf("stdin line %s %v", line, err)
	}
	base := []string{"claude", "--print"}
	if got := d.WithResume(base, "s-1"); !reflect.DeepEqual(got, []string{"claude", "--print", "--resume", "s-1"}) {
		t.Errorf("resume argv %v", got)
	}
	if !reflect.DeepEqual(base, []string{"claude", "--print"}) {
		t.Errorf("WithResume modified its argv: %v", base)
	}
	if got := d.WithResume(base, ""); !reflect.DeepEqual(got, base) {
		t.Errorf("no-resume argv %v", got)
	}
	if got := d.ConfigDirEnv("/cfg"); got != "CLAUDE_CONFIG_DIR=/cfg" {
		t.Errorf("config dir env %q", got)
	}
	if !d.ConfiguresMCP([]string{"claude", MCPConfigFlag, "{}"}) || d.ConfiguresMCP(base) {
		t.Error("ConfiguresMCP does not follow --mcp-config")
	}
	if d.HoldsSteers() {
		t.Error("the Claude driver holds steers")
	}
	if err := d.PrepareTurn(context.Background(), TurnDirs{}); err != nil {
		t.Errorf("PrepareTurn: %v", err)
	}
}

// toyDriver speaks a made-up dialect, so a task it runs through Run shows the
// adapter assumes nothing of Claude's stream-json. stdin lines are "TOY
// <text>"; stdout lines are "S <session>", "T <thinking>", "P <prose>",
// "A <tool>", "OK <text>" and "ERR <token> <text>".
type toyDriver struct {
	hold bool
	// prepare, if set, runs for each PrepareTurn call, numbered from 1.
	prepare func(n int, dirs TurnDirs) error

	mu    sync.Mutex
	calls int
	dirs  []TurnDirs
}

func (*toyDriver) Harness() persona.Harness { return "toy" }

func (*toyDriver) EncodeUser(text string) ([]byte, error) { return []byte("TOY " + text), nil }

func (*toyDriver) NewDecoder() Decoder { return toyDecoder{} }

func (*toyDriver) WithResume(argv []string, id string) []string {
	if id == "" {
		return argv
	}
	return append(append([]string(nil), argv...), "--again", id)
}

func (*toyDriver) WithDelegateServerEnv(argv []string, _, _ string) ([]string, bool) {
	return argv, false
}

func (*toyDriver) ConfiguresMCP([]string) bool { return false }

func (*toyDriver) ConfigDirEnv(dir string) string { return "TOY_HOME=" + dir }

func (d *toyDriver) PrepareTurn(_ context.Context, dirs TurnDirs) error {
	d.mu.Lock()
	d.calls++
	n := d.calls
	d.dirs = append(d.dirs, dirs)
	d.mu.Unlock()
	if d.prepare != nil {
		return d.prepare(n, dirs)
	}
	return nil
}

func (d *toyDriver) HoldsSteers() bool { return d.hold }

func (d *toyDriver) prepared() (int, []TurnDirs) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.calls, append([]TurnDirs(nil), d.dirs...)
}

type toyDecoder struct{}

func (toyDecoder) Decode(line []byte) ([]Event, error) {
	kind, rest, _ := strings.Cut(string(line), " ")
	switch kind {
	case "S":
		return []Event{{Kind: EventInit, Session: rest, Model: "toy-model"}}, nil
	case "T":
		return []Event{{Kind: EventThinking, Text: rest}}, nil
	case "P":
		return []Event{{Kind: EventProgress, Text: rest}}, nil
	case "A":
		return []Event{{Kind: EventActivity, Tool: rest, Input: json.RawMessage(`{}`)}}, nil
	case "R":
		return []Event{{Kind: EventToolResult, ToolResult: json.RawMessage(rest)}}, nil
	case "OK":
		return []Event{{Kind: EventResult, Text: rest}}, nil
	case "ERR":
		token, text, _ := strings.Cut(rest, " ")
		return []Event{{Kind: EventResult, Failed: true, Token: token, Text: text}}, nil
	}
	return nil, errors.New("not a toy line")
}

// TestADriverOfAnotherDialectRunsATask: a whole task through Run with the toy
// driver. The prompt reaches the harness in the toy's encoding, each event
// lands on its artifact, the result is the deliverable, and the messages
// name the toy as their agent type.
func TestADriverOfAnotherDialectRunsATask(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-toy-a1", "task-toy-1"
	submit(t, c, session, taskID, "count the buses")

	harness := stub(t, `
echo 'S toy-1'
read first || exit 1
case "$first" in "TOY "*) ;; *) echo "OK wrong encoding: $first"; exit 0 ;; esac
echo 'not a toy line'
echo 'T mulling it over'
echo 'P counting now'
echo 'A Count'
echo "OK done: ${first#TOY }"
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = &toyDriver{}
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if got := artifactText(task, lib.ArtifactResult); !strings.Contains(got, "done: ") || !strings.Contains(got, "count the buses") {
		t.Errorf("result artifact %q", got)
	}
	if got := artifactText(task, lib.ArtifactThinking); got != "mulling it over" {
		t.Errorf("thinking artifact %q", got)
	}
	if got := artifactText(task, lib.ArtifactProgress); got != "counting now" {
		t.Errorf("progress artifact %q", got)
	}
	if a := task.Artifact(lib.ArtifactActivity); a == nil || len(a.Parts) == 0 || !strings.Contains(string(a.Parts[0].Data), `"tool":"Count"`) {
		t.Errorf("activity artifact %+v", a)
	}
	for _, env := range replayEvents(t, url, session, taskID) {
		if env.From.Session == session && env.From.AgentType != "toy" {
			t.Errorf("%s from %+v, want agent type toy", env.Kind, env.From)
		}
	}
}

// TestADriversFailedResultCarriesItsToken: the toy's failure token is the
// terminal's reason token, as Claude's subtype is.
func TestADriversFailedResultCarriesItsToken(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-toy-b2", "task-toy-2"
	submit(t, c, session, taskID, "try it")

	harness := stub(t, `
echo 'S toy-2'
read first || exit 1
echo 'ERR toy_limit ran out of road'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = &toyDriver{}
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q err %v", out.res.State, out.err)
	}
	events := replayEvents(t, url, session, taskID)
	last := statusOf(t, events[len(events)-1])
	if text := joinParts(last.Status.Message.Parts); !strings.HasPrefix(text, "reason: toy_limit") {
		t.Fatalf("reason %q", text)
	}
}

// holdStub is the harness for the steer-gate tests. It reads the opening
// prompt, touches started, and waits up to five seconds for a second line:
// a steer the adapter wrote mid-turn arrives here. Without one it answers
// turn one, then reads the next line and answers with what it saw:
// "during=none prepared=<yes if the prepared file exists> second=<line>".
// With one it answers both turns at once, "during=early".
func holdStub(t *testing.T, started, prepared, got string) []string {
	t.Helper()
	return stub(t, fmt.Sprintf(`
echo 'S toy-hold'
read first || exit 1
: > %q
if read -t 5 early; then
  echo 'OK turn one'
  echo "OK during=early"
  exit 0
fi
echo 'OK turn one'
read second || exit 0
: > %q
[ -e %q ] && prep=yes || prep=no
echo "OK during=none prepared=$prep second=${second#TOY }"
`, started, got, prepared))
}

// publishSteerFor publishes one steer onto the running task.
func publishSteerFor(t *testing.T, c *lib.Client, origin *lib.Envelope, session, text string) {
	t.Helper()
	payload, err := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}},
		MessageID: "msg-" + strings.ReplaceAll(text, " ", "-"), TaskID: origin.TaskID, ContextID: origin.ContextID,
	})
	if err != nil {
		t.Fatalf("steer payload: %v", err)
	}
	steer, err := lib.NewFollowUpEnvelope(origin, gatewayParty, payload, lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("steer envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(session, origin.TaskID), steer); err != nil {
		t.Fatalf("steer publish: %v", err)
	}
}

// TestADriverThatHoldsSteersGetsThemAfterPrepareTurn: with the gate on, a
// steer sent during turn one is not written until turn one's result, and
// PrepareTurn runs before it is. With the gate off, the same steer reaches
// the harness during turn one: the control that shows the stub can tell.
func TestADriverThatHoldsSteersGetsThemAfterPrepareTurn(t *testing.T) {
	for _, hold := range []bool{true, false} {
		t.Run(fmt.Sprintf("hold=%v", hold), func(t *testing.T) {
			url := startServer(t)
			c := testClient(t, url)
			session, taskID := "chat-toy-c3", fmt.Sprintf("task-toy-hold-%v", hold)
			origin := submit(t, c, session, taskID, "opening")
			dir := t.TempDir()
			started, prepared, got := filepath.Join(dir, "started"), filepath.Join(dir, "prepared"), filepath.Join(dir, "got")

			d := &toyDriver{hold: hold, prepare: func(n int, _ TurnDirs) error {
				if n == 2 {
					return os.WriteFile(prepared, nil, 0o600)
				}
				return nil
			}}
			cfg := adapterConfig(url, taskID, session, holdStub(t, started, prepared, got))
			cfg.Driver = d
			cfg.HarnessEnv = append(os.Environ(), "HOME=/toy-home")
			done := runAdapter(context.Background(), cfg)
			waitPath(t, started, "the harness never started its first turn")
			publishSteerFor(t, c, origin, session, "STEERWORD again")

			out := waitOutcome(t, done, waitDeadline)
			if out.err != nil || out.res.State != lib.StateCompleted {
				t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
			}
			result := artifactText(foldTask(t, c, session, taskID), lib.ArtifactResult)
			calls, dirs := d.prepared()
			if !hold {
				if result != "during=early" {
					t.Fatalf("result %q: with the gate off the steer should reach turn one", result)
				}
				if calls != 1 {
					t.Errorf("PrepareTurn ran %d times, want once, before the spawn", calls)
				}
				return
			}
			if result != "during=none prepared=yes second=STEERWORD again" {
				t.Fatalf("result %q: the held steer should be written after turn one and PrepareTurn", result)
			}
			if calls != 2 {
				t.Fatalf("PrepareTurn ran %d times, want 2 (spawn, boundary)", calls)
			}
			wd, err := os.Getwd()
			if err != nil {
				t.Fatal(err)
			}
			wd, err = filepath.EvalSymlinks(wd)
			if err != nil {
				t.Fatal(err)
			}
			for i, dd := range dirs {
				if dd.Workspace != wd || dd.Home != "/toy-home" {
					t.Errorf("PrepareTurn %d dirs %+v, want workspace %s home /toy-home", i+1, dd, wd)
				}
			}
		})
	}
}

// TestAPrepareTurnErrorFailsTheTask: PrepareTurn's error fails the task with
// harness-prepare-failed and its text. Before the spawn, nothing runs; at a
// boundary, the held steer is never written.
func TestAPrepareTurnErrorFailsTheTask(t *testing.T) {
	for _, at := range []int{1, 2} {
		t.Run(fmt.Sprintf("call=%d", at), func(t *testing.T) {
			url := startServer(t)
			c := testClient(t, url)
			session, taskID := "chat-toy-d4", fmt.Sprintf("task-toy-prep-%d", at)
			origin := submit(t, c, session, taskID, "opening")
			dir := t.TempDir()
			started, prepared, got := filepath.Join(dir, "started"), filepath.Join(dir, "prepared"), filepath.Join(dir, "got")

			cfg := adapterConfig(url, taskID, session, holdStub(t, started, prepared, got))
			cfg.Driver = &toyDriver{hold: true, prepare: func(n int, _ TurnDirs) error {
				if n == at {
					return errors.New("the workspace would not scrub")
				}
				return nil
			}}
			done := runAdapter(context.Background(), cfg)
			if at == 2 {
				waitPath(t, started, "the harness never started its first turn")
				publishSteerFor(t, c, origin, session, "STEERWORD again")
			}
			out := waitOutcome(t, done, waitDeadline)
			if out.res.State != lib.StateFailed {
				t.Fatalf("state %q err %v", out.res.State, out.err)
			}
			events := replayEvents(t, url, session, taskID)
			last := statusOf(t, events[len(events)-1])
			want := "reason: harness-prepare-failed - the workspace would not scrub"
			if text := joinParts(last.Status.Message.Parts); text != want {
				t.Errorf("reason %q, want %q", text, want)
			}
			if _, err := os.Stat(started); at == 1 && err == nil {
				t.Error("the harness ran after the first PrepareTurn failed")
			}
			if _, err := os.Stat(got); err == nil {
				t.Error("the held steer was written after PrepareTurn failed")
			}
			if at == 2 && refusalBeforeTerminal(t, url, session, taskID) < 0 {
				t.Error("the held steer was not refused after PrepareTurn failed")
			}
		})
	}
}

// TestAHeldSteerWithNoTurnToJoinIsRefusedVisibly: a steer held during a turn
// whose result fails has no turn to join. It is refused on the stream before
// the terminal, like a steer that arrives after the deliverable.
func TestAHeldSteerWithNoTurnToJoinIsRefusedVisibly(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-toy-e5", "task-toy-refuse"
	origin := submit(t, c, session, taskID, "opening")
	started := filepath.Join(t.TempDir(), "started")
	harness := stub(t, fmt.Sprintf(`
echo 'S toy-refuse'
read first || exit 1
: > %q
sleep 3
echo 'ERR toy_broke it broke'
`, started))
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = &toyDriver{hold: true}
	done := runAdapter(context.Background(), cfg)
	waitPath(t, started, "the harness never started its first turn")
	publishSteerFor(t, c, origin, session, "STEERWORD too late")

	out := waitOutcome(t, done, waitDeadline)
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q err %v", out.res.State, out.err)
	}
	if refusalBeforeTerminal(t, url, session, taskID) < 0 {
		t.Fatal("want a steer refusal before the terminal")
	}
}

// refusalBeforeTerminal is the index of the last steer refusal on the task's
// stream if it comes before the terminal, else -1.
func refusalBeforeTerminal(t *testing.T, url, session, taskID string) int {
	t.Helper()
	refusalAt, terminalAt := -1, -1
	for i, env := range replayEvents(t, url, session, taskID) {
		if env.Kind != lib.KindStatusUpdate {
			continue
		}
		s := statusOf(t, env)
		if s.Final {
			terminalAt = i
			continue
		}
		if s.Status.Message != nil && strings.Contains(joinParts(s.Status.Message.Parts), "steer refused") {
			refusalAt = i
		}
	}
	if refusalAt < 0 || terminalAt < 0 || refusalAt > terminalAt {
		return -1
	}
	return refusalAt
}

// TestEachHeldSteerGetsATurnOfItsOwn: two steers held during one turn go
// onto stdin one per boundary, each after its own PrepareTurn, so every turn
// the harness runs was prepared. Written together after one PrepareTurn, the
// third line would arrive before the third PrepareTurn ran.
func TestEachHeldSteerGetsATurnOfItsOwn(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-toy-f6", "task-toy-two-held"
	origin := submit(t, c, session, taskID, "opening")
	dir := t.TempDir()
	started := filepath.Join(dir, "started")
	harness := stub(t, fmt.Sprintf(`
dir=%q
echo 'S toy-two'
read first || exit 1
: > "$dir/started"
sleep 3
echo 'OK turn one'
read second || exit 1
[ -e "$dir/prepared-2" ] && p2=yes || p2=no
sleep 1
echo 'OK turn two'
read third || exit 1
[ -e "$dir/prepared-3" ] && p3=yes || p3=no
echo "OK second=${second#TOY } p2=$p2 third=${third#TOY } p3=$p3"
`, dir))
	d := &toyDriver{hold: true, prepare: func(n int, _ TurnDirs) error {
		return os.WriteFile(filepath.Join(dir, fmt.Sprintf("prepared-%d", n)), nil, 0o600)
	}}
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = d
	done := runAdapter(context.Background(), cfg)
	waitPath(t, started, "the harness never started its first turn")
	publishSteerFor(t, c, origin, session, "STEERWORD one")
	publishSteerFor(t, c, origin, session, "STEERWORD two")

	out := waitOutcome(t, done, waitDeadline)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	want := "second=STEERWORD one p2=yes third=STEERWORD two p3=yes"
	if got := artifactText(foldTask(t, c, session, taskID), lib.ArtifactResult); got != want {
		t.Fatalf("result %q, want %q", got, want)
	}
	if calls, _ := d.prepared(); calls != 3 {
		t.Errorf("PrepareTurn ran %d times, want 3 (spawn and two boundaries)", calls)
	}
}

// TestAHeldSteerAfterTheDeadlineStartsNoTurn: once the deadline has fired, a
// clean result does not prepare another turn for a harness being killed. The
// held steer is refused, and the result still wins, as it does against a
// cancel it raced.
func TestAHeldSteerAfterTheDeadlineStartsNoTurn(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-toy-g7", "task-toy-deadline"
	origin := submit(t, c, session, taskID, "opening")
	started := filepath.Join(t.TempDir(), "started")
	harness := stub(t, fmt.Sprintf(`
trap 'echo "OK answered at the deadline"; exit 0' TERM
echo 'S toy-deadline'
read first || exit 1
: > %q
sleep 30 &
wait
`, started))
	d := &toyDriver{hold: true}
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = d
	cfg.TaskDeadline = 4 * time.Second
	cfg.KillGrace = 3 * time.Second
	done := runAdapter(context.Background(), cfg)
	waitPath(t, started, "the harness never started its first turn")
	publishSteerFor(t, c, origin, session, "STEERWORD late")

	out := waitOutcome(t, done, waitDeadline)
	if out.res.State != lib.StateCompleted {
		t.Fatalf("state %q err %v: the result raced the deadline and should win", out.res.State, out.err)
	}
	if calls, _ := d.prepared(); calls != 1 {
		t.Errorf("PrepareTurn ran %d times, want once: no turn after the deadline", calls)
	}
	if refusalBeforeTerminal(t, url, session, taskID) < 0 {
		t.Error("the held steer was not refused")
	}
}

// TestAHeldSteerIsRefusedOnEviction: a steer held when the pod is evicted is
// refused on the stream before the evicted terminal.
func TestAHeldSteerIsRefusedOnEviction(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-toy-h8", "task-toy-evict"
	origin := submit(t, c, session, taskID, "opening")
	started := filepath.Join(t.TempDir(), "started")
	harness := stub(t, fmt.Sprintf(`
echo 'S toy-evict'
read first || exit 1
: > %q
sleep 30
`, started))
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = &toyDriver{hold: true}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, cfg)
	waitPath(t, started, "the harness never started its first turn")
	publishSteerFor(t, c, origin, session, "STEERWORD evicted")
	// The steer has to be held before the eviction lands.
	time.Sleep(time.Second)
	cancel()

	out := waitOutcome(t, done, waitDeadline)
	if !out.res.Evicted {
		t.Fatalf("result %+v err %v, want evicted", out.res, out.err)
	}
	if refusalBeforeTerminal(t, url, session, taskID) < 0 {
		t.Error("the held steer was not refused before the evicted terminal")
	}
}
