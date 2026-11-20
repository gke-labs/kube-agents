package workeradapter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/persona"
)

// The fixtures under testdata/agy are agy 1.5.13's own stdout, recorded
// headless with stream-json in and out (#6709): basic and deny from the
// spike's lane run, error (a gateway answering 500), timeout
// (--print-timeout expiry), cache (two turns in one process) and thinking (a
// reasoning prompt) from the kagents-helper probe. Only init's tool list is
// trimmed. Only the lane captures (basic, deny) carry init.model: agy names
// its model in gateway mode, and the probe ran on agy's own backend, which
// names none, so the model assertions rest on those two.

func agyFixture(t *testing.T, name string) [][]byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", "agy", name+".jsonl"))
	if err != nil {
		t.Fatalf("fixture %s: %v", name, err)
	}
	var lines [][]byte
	for _, l := range bytes.Split(b, []byte("\n")) {
		if len(bytes.TrimSpace(l)) > 0 {
			lines = append(lines, l)
		}
	}
	return lines
}

// decodeAll runs every line of a fixture through one decoder, as one agy
// process's stdout.
func decodeAll(t *testing.T, name string) []Event {
	t.Helper()
	d := agyDriver{}.NewDecoder()
	var events []Event
	for i, line := range agyFixture(t, name) {
		evs, err := d.Decode(line)
		if err != nil {
			t.Fatalf("%s line %d: %v", name, i+1, err)
		}
		events = append(events, evs...)
	}
	return events
}

func eventsOf(events []Event, kind EventKind) []Event {
	var out []Event
	for _, ev := range events {
		if ev.Kind == kind {
			out = append(out, ev)
		}
	}
	return out
}

func TestTheAgyDecoderReadsASuccessfulTurn(t *testing.T) {
	events := decodeAll(t, "basic")
	inits := eventsOf(events, EventInit)
	if len(inits) != 1 || inits[0].Session != "4b8bd7f1-c65a-4838-9df9-691d34b6e480" || inits[0].Model != "model-default" {
		t.Fatalf("init %+v", inits)
	}
	var prose strings.Builder
	for _, ev := range eventsOf(events, EventProgress) {
		prose.WriteString(ev.Text)
	}
	if prose.String() != "pong\n" {
		t.Errorf("progress %q", prose.String())
	}
	results := eventsOf(events, EventResult)
	if len(results) != 1 {
		t.Fatalf("results %+v", results)
	}
	r := results[0]
	if r.Failed || r.Token != "" || r.Text != "pong\n" {
		t.Errorf("result %+v", r)
	}
	u := r.Telemetry.usage
	if u.InputTokens != 7828 || u.OutputTokens != 1 || u.DurationMs != 2349 || u.Model != "model-default" {
		t.Errorf("usage %+v", u)
	}
	if r.Telemetry.hasTotal {
		t.Error("agy reports no cost, so there must be no running total")
	}
}

func TestAnAgyTurnWithDeniedActionsFails(t *testing.T) {
	events := decodeAll(t, "deny")
	acts := eventsOf(events, EventActivity)
	if len(acts) != 1 || acts[0].Tool != "run_command" || !strings.Contains(string(acts[0].Input), "touch /scratch/deny-marker") {
		t.Errorf("activity %+v", acts)
	}
	r := eventsOf(events, EventResult)[0]
	if !r.Failed || !strings.HasPrefix(r.Token, agyPermissionDeniedToken+reasonDetailSeparator) || !strings.Contains(r.Token, "command (Bash)") {
		t.Errorf("a result listing denied_actions under status SUCCESS must fail: %+v", r)
	}
}

func TestAnAgyErrorResultFailsWithTheGenericToken(t *testing.T) {
	r := eventsOf(decodeAll(t, "error"), EventResult)[0]
	if !r.Failed || !strings.HasPrefix(r.Token, agyErrorToken+reasonDetailSeparator) || !strings.Contains(r.Token, "code 500") {
		t.Errorf("result %+v", r)
	}
}

func TestAnAgyPrintTimeoutFailsThoughItSaysSuccess(t *testing.T) {
	r := eventsOf(decodeAll(t, "timeout"), EventResult)[0]
	if !r.Failed || r.Token != agyPrintTimeoutToken {
		t.Errorf("a print_timeout finish reports SUCCESS and must still fail: %+v", r)
	}
}

func TestTheAgyDecoderReportsEachTurnsOwnUsage(t *testing.T) {
	results := eventsOf(decodeAll(t, "cache"), EventResult)
	if len(results) != 2 {
		t.Fatalf("results %d", len(results))
	}
	// result.usage is cumulative over the process. Turn two's own: 2144 fresh
	// input plus 9979 read from cache, 22 output (thinking included).
	first, second := results[0].Telemetry.usage, results[1].Telemetry.usage
	if first.InputTokens != 12033 || first.OutputTokens != 22 {
		t.Errorf("turn one %+v", first)
	}
	if second.InputTokens != 2144+9979 || second.OutputTokens != 22 {
		t.Errorf("turn two %+v, want its own figures, not the running sum", second)
	}
	if second.DurationMs <= 0 || second.DurationMs >= 47286 {
		t.Errorf("turn two duration %d, want its own share", second.DurationMs)
	}
}

func TestTheAgyDecoderEmitsNoThinking(t *testing.T) {
	events := decodeAll(t, "thinking")
	if got := eventsOf(events, EventThinking); len(got) != 0 {
		t.Errorf("agy streams no thinking text; got %+v", got)
	}
	var prose strings.Builder
	for _, ev := range eventsOf(events, EventProgress) {
		prose.WriteString(ev.Text)
	}
	r := eventsOf(events, EventResult)[0]
	if prose.String() != r.Text || !strings.Contains(r.Text, "0.05") {
		t.Errorf("progress deltas %q do not add up to the response %q", prose.String(), r.Text)
	}
}

func TestTheAgyDecoderSkipsLinesThatAreNotItsOwn(t *testing.T) {
	d := agyDriver{}.NewDecoder()
	for _, line := range []string{"plain chatter", `{"type":"system","subtype":"init"}`, `[]`} {
		if evs, err := d.Decode([]byte(line)); err == nil {
			t.Errorf("%q decoded as %+v", line, evs)
		}
	}
	if evs, err := d.Decode([]byte(`{"event":"something_new"}`)); err != nil || len(evs) != 0 {
		t.Errorf("an unknown agy event is no event and no error: %+v %v", evs, err)
	}
}

func TestTheAgyDriverKeepsItsContract(t *testing.T) {
	d := agyDriver{}
	if d.Harness() != persona.Antigravity {
		t.Errorf("harness %q", d.Harness())
	}
	line, err := d.EncodeUser(`say "pong"`)
	if err != nil {
		t.Fatal(err)
	}
	if want := `{"event":"user","message":{"role":"user","content":[{"type":"text","text":"say \"pong\""}]}}`; string(line) != want {
		t.Errorf("stdin line %s", line)
	}
	argv := []string{"agy", "-p="}
	resumed := d.WithResume(argv, "conv-1")
	if !slices.Equal(resumed, []string{"agy", "-p=", "--conversation", "conv-1"}) || len(argv) != 2 {
		t.Errorf("resume %v (argv now %v)", resumed, argv)
	}
	if got := d.WithResume(argv, ""); !slices.Equal(got, argv) {
		t.Errorf("no session resumes nothing: %v", got)
	}
	if _, ok := d.WithDelegateServerEnv(argv, "K", "V"); ok || d.ConfiguresMCP(argv) {
		t.Error("agy's MCP configuration is #6712's; the driver configures none yet")
	}
	if d.ConfigDirEnv("/task/home") != "HOME=/task/home" {
		t.Errorf("config dir env %q", d.ConfigDirEnv("/task/home"))
	}
	if !d.HoldsSteers() {
		t.Error("agy holds steers to the turn boundary")
	}
}

// agyStub is a harness that reads the opening prompt, checks it is agy's
// stdin shape, and then prints the named fixture's lines up to stop (all of
// them for -1), then exits with code.
func agyStub(t *testing.T, fixture string, stop, code int) []string {
	t.Helper()
	lines := agyFixture(t, fixture)
	if stop >= 0 {
		lines = lines[:stop]
	}
	path := filepath.Join(t.TempDir(), "out.jsonl")
	if err := os.WriteFile(path, append(bytes.Join(lines, []byte("\n")), '\n'), 0o644); err != nil {
		t.Fatal(err)
	}
	return stub(t, fmt.Sprintf(`
read first || exit 1
case "$first" in '{"event":"user"'*) ;; *) echo "wrong stdin: $first" >&2; exit 9 ;; esac
cat %q
exit %d
`, path, code))
}

func runAgyTask(t *testing.T, session, taskID string, harness []string) (runOutcome, []*lib.Envelope, *lib.Task) {
	t.Helper()
	url := startServer(t)
	c := testClient(t, url)
	submit(t, c, session, taskID, "say pong")
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Driver = agyDriver{}
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	return out, replayEvents(t, url, session, taskID), foldTask(t, c, session, taskID)
}

func TestAnAgyTaskRunsThroughTheAdapter(t *testing.T) {
	const session, taskID = "chat-agy-a1", "task-agy-1"
	out, events, task := runAgyTask(t, session, taskID, agyStub(t, "basic", -1, 0))
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("state=%q err=%v", out.res.State, out.err)
	}
	if got := artifactText(task, lib.ArtifactResult); got != "pong\n" {
		t.Errorf("result artifact %q", got)
	}
	for _, env := range events {
		if env.From.Session == session && env.From.AgentType != string(persona.Antigravity) {
			t.Errorf("%s from %+v, want agent type antigravity", env.Kind, env.From)
		}
	}
}

// A turn that ends without a result, whatever the exit code, is a failed
// task with its own token, never a success: agy's exit code carries no
// failure of its own (#6709), so the result line is the only success there is.
func TestAnAgyTurnWithNoResultFails(t *testing.T) {
	for i, tc := range []struct {
		name       string
		fixture    string
		stop, code int
	}{
		{"exit 0 after steps", "basic", 4, 0},
		{"exit 1 after steps", "basic", 4, 1},
		{"exit 2 before any line", "basic", 0, 2},
		{"exit 0 mid tool turn", "deny", 4, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session, taskID := fmt.Sprintf("chat-agy-n%d", i), fmt.Sprintf("task-agy-n%d", i)
			out, events, _ := runAgyTask(t, session, taskID, agyStub(t, tc.fixture, tc.stop, tc.code))
			if out.res.State != lib.StateFailed {
				t.Fatalf("state %q, want failed", out.res.State)
			}
			last := statusOf(t, events[len(events)-1])
			if text := joinParts(last.Status.Message.Parts); !strings.HasPrefix(text, "reason: stream-ended-without-result") {
				t.Errorf("reason %q", text)
			}
		})
	}
}

func TestAnAgyDeniedTurnFailsTheTask(t *testing.T) {
	const session, taskID = "chat-agy-d1", "task-agy-d1"
	out, events, _ := runAgyTask(t, session, taskID, agyStub(t, "deny", -1, 0))
	if out.res.State != lib.StateFailed {
		t.Fatalf("state %q, want failed: agy exits 0 and says SUCCESS for a skipped tool call", out.res.State)
	}
	last := statusOf(t, events[len(events)-1])
	if text := joinParts(last.Status.Message.Parts); !strings.HasPrefix(text, "reason: "+agyPermissionDeniedToken) {
		t.Errorf("reason %q", text)
	}
}

// The fixtures stay the dialect the decoder reads: every line is an agy
// event, and each fixture ends in exactly one result per user message.
func TestTheAgyFixturesAreAgyLines(t *testing.T) {
	for _, name := range []string{"basic", "deny", "error", "timeout", "cache", "thinking"} {
		results := 0
		for _, line := range agyFixture(t, name) {
			var ln agyLine
			if err := json.Unmarshal(line, &ln); err != nil || ln.Event == "" {
				t.Errorf("%s: %s", name, line)
			}
			if ln.Event == "result" {
				results++
			}
		}
		if want := map[string]int{"cache": 2}[name]; results != max(want, 1) {
			t.Errorf("%s: %d results", name, results)
		}
	}
}

// decodeLines runs synthetic agy lines through one decoder and returns the
// results.
func decodeLines(t *testing.T, lines ...string) []Event {
	t.Helper()
	d := agyDriver{}.NewDecoder()
	var out []Event
	for _, l := range lines {
		evs, err := d.Decode([]byte(l))
		if err != nil {
			t.Fatalf("%s: %v", l, err)
		}
		out = append(out, eventsOf(evs, EventResult)...)
	}
	return out
}

func TestAnAgyFailureDetailStaysOneShortLine(t *testing.T) {
	page := "<html>\n<body>\n" + strings.Repeat("upstream says no ", 100) + "\n</body></html>"
	b, _ := json.Marshal(map[string]any{"event": "result", "result": map[string]any{"status": "ERROR", "error": page}})
	r := decodeLines(t, string(b))[0]
	detail := strings.TrimPrefix(r.Token, agyErrorToken+reasonDetailSeparator)
	if !r.Failed || detail == r.Token || strings.ContainsAny(detail, "\n\t") || len([]rune(detail)) > agyDetailCap {
		t.Errorf("token %q: want one line of at most %d runes", r.Token, agyDetailCap)
	}
}

func TestAnUnknownAgyStatusFailsUnderTheGenericToken(t *testing.T) {
	r := decodeLines(t, `{"event":"result","result":{"status":"CANCELLED weird","error":"stopped"}}`)[0]
	if !r.Failed || !strings.HasPrefix(r.Token, agyErrorToken+reasonDetailSeparator+"status CANCELLED weird; stopped") {
		t.Errorf("token %q: an unknown status is a failure under the generic token, with the status in the detail", r.Token)
	}
	if r := decodeLines(t, `{"event":"result","result":{"error":"no status at all"}}`)[0]; !r.Failed || !strings.HasPrefix(r.Token, agyErrorToken) {
		t.Errorf("a result with no status must fail: %+v", r)
	}
}

func TestAnAgyFailureKeepsItsDeniedActionsInTheDetail(t *testing.T) {
	r := decodeLines(t, `{"event":"result","result":{"status":"ERROR","error":"boom","denied_actions":[{"action":"command","display_name":"Bash"}]}}`)[0]
	if !r.Failed || !strings.HasPrefix(r.Token, agyErrorToken+reasonDetailSeparator) || !strings.Contains(r.Token, "command (Bash)") {
		t.Errorf("token %q", r.Token)
	}
	r = decodeLines(t, `{"event":"result","result":{"status":"SUCCESS","finished_reason":"print_timeout","denied_actions":[{"action":"mcp"}]}}`)[0]
	if r.Token != agyPrintTimeoutToken+reasonDetailSeparator+"skipped for want of approval: mcp" {
		t.Errorf("token %q", r.Token)
	}
}

func TestAnAgyResultWithoutUsageKeepsTheBaseline(t *testing.T) {
	results := decodeLines(t,
		`{"event":"result","result":{"status":"SUCCESS","response":"a","usage":{"input_tokens":100,"output_tokens":10,"cache_read_tokens":0}}}`,
		`{"event":"result","result":{"status":"SUCCESS","response":"b"}}`,
		`{"event":"result","result":{"status":"SUCCESS","response":"c","usage":{"input_tokens":150,"output_tokens":25,"cache_read_tokens":20}}}`,
	)
	if u := results[1].Telemetry.usage; u.InputTokens != 0 || u.OutputTokens != 0 {
		t.Errorf("a result without usage reports none: %+v", u)
	}
	if u := results[2].Telemetry.usage; u.InputTokens != 70 || u.OutputTokens != 15 {
		t.Errorf("turn three %+v, want its difference from turn one's (70 in, 15 out)", u)
	}
}
