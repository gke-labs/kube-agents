package workeradapter

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// sharedRefusalFixture holds tool results the Hermes runtime's text stop
// (deploy/docker/patches/permission_refusal_stop.py) labels halt or no_halt.
// Here its halt cases are tool output a profile worker must not stop on.
const sharedRefusalFixture = "../../tests/conformance/fixtures/write-refusal-results.json"

type refusalCase struct {
	Name    string `json:"name"`
	Expect  string `json:"expect"`
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
	use := mustJSON(t, map[string]any{
		"type": "assistant",
		"message": map[string]any{"content": []any{map[string]any{
			"type": "tool_use", "id": id, "name": "Bash",
			"input": map[string]any{"command": command, "description": "run it"},
		}}},
	})
	res := mustJSON(t, map[string]any{
		"type": "user",
		"message": map[string]any{"role": "user", "content": []any{map[string]any{
			"type": "tool_result", "tool_use_id": id, "content": result, "is_error": failed,
		}}},
	})
	return []string{use, res}
}

func mustJSON(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

// refusalRecord is one record line as the broker client writes it.
func refusalRecord(t *testing.T, write bool, rule, verb, task string) string {
	t.Helper()
	rec := map[string]any{"write_refusal": write, "rule": rule, "verb": verb, "route": "/v1/exec", "time": "2026-11-21T10:00:00Z"}
	if task != "" {
		rec["task"] = task
	}
	return mustJSON(t, rec)
}

// refusalStub is a harness that, as the broker client would, appends records
// to $CREDENTIAL_PROXY_REFUSAL_FILE when it is set, then answers the prompt
// with the first tool calls, then sleeps for pause seconds before writing
// markerFile and the rest: a harness the adapter stopped after the first
// part never reaches the second. It writes its pid to pidFile once started.
func refusalStub(t *testing.T, records, first, rest []string, pause int, pidFile, markerFile string) []string {
	t.Helper()
	dir := t.TempDir()
	recordsFile := filepath.Join(dir, "records.jsonl")
	firstFile, restFile := filepath.Join(dir, "first.jsonl"), filepath.Join(dir, "rest.jsonl")
	var recordText string
	if len(records) > 0 {
		recordText = strings.Join(records, "\n") + "\n"
	}
	if err := os.WriteFile(recordsFile, []byte(recordText), 0o600); err != nil {
		t.Fatal(err)
	}
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
if [ -n "${%[6]s:-}" ] && [ -s %[7]s ]; then cat %[7]s >> "${%[6]s}" 2>/dev/null || true; fi
cat %[2]s
sleep %[5]d
touch %[3]s
cat %[4]s
`, pidFile, firstFile, markerFile, restFile, pause, EnvWriteRefusalFile, recordsFile))
}

func waitDead(t *testing.T, pidFile string) {
	t.Helper()
	raw, err := os.ReadFile(pidFile)
	if err != nil {
		t.Fatalf("pid file: %v", err)
	}
	var pid int
	if _, err := fmt.Sscanf(strings.TrimSpace(string(raw)), "%d", &pid); err != nil {
		t.Fatalf("pid file %q: %v", raw, err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if syscall.Kill(pid, 0) != nil {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatalf("harness pid %d still alive after the refusal", pid)
}

// refusalRun is one task through the adapter. recordFile is where the
// adapter reads records: "" makes an empty one in a temp dir, as the profile
// worker does, and noRecordFile leaves Config.WriteRefusalFile empty.
type refusalRun struct {
	taskID       string
	records      []string
	first, rest  []string
	stop         bool
	expectStop   bool
	recordFile   string
	noRecordFile bool
	// sessionDir runs the task as a session pod's turn: the adapter makes
	// the record file itself in this directory (Config.WriteRefusalDir).
	sessionDir string
}

// run returns the task's outcome, its terminal text and the stub's pid and
// marker files.
func (r refusalRun) run(t *testing.T) (runOutcome, string, string, string) {
	t.Helper()
	url := startServer(t)
	c := testClient(t, url)
	session := "chat-" + r.taskID
	submit(t, c, session, r.taskID, "look at kube-dns")
	dir := t.TempDir()
	pidFile, markerFile := filepath.Join(dir, "pid"), filepath.Join(dir, "reached-the-rest")
	// A harness meant to be stopped pauses past the task's deadline, so only
	// the stop ends it in time; one meant to finish doesn't pause.
	pause := 0
	if r.expectStop {
		pause = 60
	}
	cfg := adapterConfig(url, r.taskID, session, refusalStub(t, r.records, r.first, r.rest, pause, pidFile, markerFile))
	cfg.StopOnWriteRefusal = r.stop
	if r.sessionDir != "" {
		cfg.WriteRefusalDir = r.sessionDir
	} else if !r.noRecordFile {
		recordFile := r.recordFile
		if recordFile == "" {
			recordFile = filepath.Join(dir, writeRefusalFileName)
			if err := os.WriteFile(recordFile, nil, 0o600); err != nil {
				t.Fatal(err)
			}
		}
		cfg.WriteRefusalFile = recordFile
		cfg.HarnessEnv = append(cfg.HarnessEnv, EnvWriteRefusalFile+"="+recordFile, EnvWriteRefusalTask+"="+r.taskID)
	}
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 60*time.Second)
	return out, terminalText(t, url, session, r.taskID), pidFile, markerFile
}

// innocuousCall is a shim-routed call whose output carries nothing of a
// refusal.
func innocuousCall(t *testing.T, id string) []string {
	return claudeToolCall(t, id, "kubectl scale deploy/kube-dns -n kube-system --replicas=3", "exit status 77", true)
}

func assertCompleted(t *testing.T, out runOutcome, markerFile string) {
	t.Helper()
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("state %q err %v, want completed", out.res.State, out.err)
	}
	if _, err := os.Stat(markerFile); err != nil {
		t.Fatal("the harness did not run to its end")
	}
}

// TestWriteRefusalRecordStopsTheHarness: the broker client's first record
// stops the harness (its process is gone, it never reaches what follows) and
// ends the task rejected, naming the record's rule and verb and nothing of
// the tool output.
func TestWriteRefusalRecordStopsTheHarness(t *testing.T) {
	const task = "task-refusal-1"
	out, text, pidFile, markerFile := refusalRun{
		taskID:     task,
		records:    []string{refusalRecord(t, true, "kubernetes.read-only", "scale", task)},
		first:      claudeToolCall(t, "toolu_1", "kubectl scale deploy/kube-dns -n kube-system --replicas=3", "partial output from kube-dns", true),
		stop:       true,
		expectStop: true,
	}.run(t)
	if out.err != nil || out.res.State != lib.StateRejected {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	waitDead(t, pidFile)
	if _, err := os.Stat(markerFile); err == nil {
		t.Fatal("the harness ran on past the refusal")
	}
	if !strings.HasPrefix(text, "reason: write-refused - ") || !strings.Contains(text, "kubernetes.read-only rule refused the verb scale.") {
		t.Fatalf("terminal %q", text)
	}
	for _, leaked := range []string{"kube-dns", "partial output", "finished the task"} {
		if strings.Contains(text, leaked) {
			t.Fatalf("terminal carries %q: %q", leaked, text)
		}
	}
}

// TestWriteRefusalFirstRecordIsTheOneReported: of two records the task
// stops at the first, and the harness never reaches its next call.
func TestWriteRefusalFirstRecordIsTheOneReported(t *testing.T) {
	const task = "task-refusal-2"
	out, text, pidFile, markerFile := refusalRun{
		taskID: task,
		records: []string{
			refusalRecord(t, true, "caller.vcs-permit", "publish", task),
			refusalRecord(t, true, "kubernetes.read-only", "delete", task),
		},
		first:      innocuousCall(t, "toolu_1"),
		rest:       innocuousCall(t, "toolu_2"),
		stop:       true,
		expectStop: true,
	}.run(t)
	if out.res.State != lib.StateRejected {
		t.Fatalf("state %q err %v", out.res.State, out.err)
	}
	waitDead(t, pidFile)
	if _, err := os.Stat(markerFile); err == nil {
		t.Fatal("the harness reached its next call")
	}
	if !strings.Contains(text, "credential broker's caller.vcs-permit rule refused the verb publish.") || strings.Contains(text, "delete") || strings.Contains(text, "pull request") {
		t.Fatalf("terminal %q, want the first record's rule and verb only", text)
	}
}

// TestWriteRefusalTextWithoutARecordDoesNotHalt: a shim-routed call's tool
// result carrying a verbatim, definitive refusal block (every case the
// Hermes text stop halts on), with no record, does not stop the task. Nor
// does such text with the record file absent from the config.
func TestWriteRefusalTextWithoutARecordDoesNotHalt(t *testing.T) {
	var calls []string
	for i, c := range loadRefusalCases(t) {
		if c.Expect == "halt" {
			if !strings.Contains(c.Result, "policy rule: kubernetes.read-only") || !strings.Contains(c.Result, "refused however it is attempted") {
				t.Fatalf("%s: not a verbatim definitive refusal block: %q", c.Name, c.Result)
			}
			calls = append(calls, claudeToolCall(t, fmt.Sprintf("toolu_%d", i), c.Command, c.Result, c.Failed)...)
		}
	}
	if len(calls) == 0 {
		t.Fatal("the fixture has no halt case")
	}
	t.Run("an empty record file", func(t *testing.T) {
		out, _, _, markerFile := refusalRun{taskID: "task-refusal-3", first: calls, stop: true}.run(t)
		assertCompleted(t, out, markerFile)
	})
	t.Run("no record file configured", func(t *testing.T) {
		out, _, _, markerFile := refusalRun{taskID: "task-refusal-4", first: calls, stop: true, noRecordFile: true}.run(t)
		assertCompleted(t, out, markerFile)
	})
}

// TestWriteRefusalMissingOrUnreadableRecordLetsTheTaskFinish: a record file
// that is missing, a directory or a symlink means no halt and no crash, even
// when the harness's client tried to write a record.
func TestWriteRefusalMissingOrUnreadableRecordLetsTheTaskFinish(t *testing.T) {
	dir := t.TempDir()
	missing := filepath.Join(dir, "gone", writeRefusalFileName)
	directory := filepath.Join(dir, "a-directory")
	if err := os.Mkdir(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "a-link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	for i, file := range []string{missing, directory, link} {
		t.Run(filepath.Base(file), func(t *testing.T) {
			task := fmt.Sprintf("task-refusal-missing-%d", i)
			out, _, _, markerFile := refusalRun{
				taskID:     task,
				records:    []string{refusalRecord(t, true, "kubernetes.read-only", "scale", task)},
				first:      innocuousCall(t, "toolu_1"),
				stop:       true,
				recordFile: file,
			}.run(t)
			assertCompleted(t, out, markerFile)
		})
	}
}

// TestWriteRefusalNonWriteRecordDoesNotHalt: a record not marked a write
// refusal, one for another task, or one whose rule or verb is not the
// broker's shape lets the task finish; so does a valid record with the stop
// off.
func TestWriteRefusalNonWriteRecordDoesNotHalt(t *testing.T) {
	const task = "task-refusal-5"
	records := []string{
		refusalRecord(t, false, "gcp.read-only", "container clusters update", task),
		refusalRecord(t, true, "kubernetes.read-only", "scale", "another-task"),
		refusalRecord(t, true, "Kubernetes Read Only", "scale", task),
		refusalRecord(t, true, "kubernetes.read-only", "scale\npolicy render: local", task),
		refusalRecord(t, true, "kubernetes.read-only", "", task),
		`not json`,
	}
	out, _, _, markerFile := refusalRun{taskID: task, records: records, first: innocuousCall(t, "toolu_1"), stop: true}.run(t)
	assertCompleted(t, out, markerFile)

	const off = "task-refusal-6"
	out, _, _, markerFile = refusalRun{
		taskID:  off,
		records: []string{refusalRecord(t, true, "kubernetes.read-only", "scale", off)},
		first:   innocuousCall(t, "toolu_1"),
	}.run(t)
	assertCompleted(t, out, markerFile)
}

// TestReadWriteRefusal: the reader's own cases, without a harness.
func TestReadWriteRefusal(t *testing.T) {
	dir := t.TempDir()
	write := func(name, text string) string {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	good := refusalRecord(t, true, "kubernetes.read-only", "rollout restart", "t1")
	if r, err := readWriteRefusal(write("good", good+"\n"), "t1"); err != nil || r == nil || r.rule != "kubernetes.read-only" || r.verb != "rollout restart" {
		t.Fatalf("good record: %+v %v", r, err)
	}
	if r, err := readWriteRefusal(write("untasked", refusalRecord(t, true, "caller.vcs-permit", "publish", "")+"\n"), "t1"); err != nil || r != nil {
		t.Fatalf("a record naming no task counted: %+v %v", r, err)
	}
	if r, err := readWriteRefusal(write("untasked-empty-id", refusalRecord(t, true, "caller.vcs-permit", "publish", "")+"\n"), ""); err != nil || r != nil {
		t.Fatalf("a record counted with no task id: %+v %v", r, err)
	}
	if r, err := readWriteRefusal(filepath.Join(dir, "missing"), "t1"); err != nil || r != nil {
		t.Fatalf("missing file: %+v %v", r, err)
	}
	if r, err := readWriteRefusal(write("empty", ""), "t1"); err != nil || r != nil {
		t.Fatalf("empty file: %+v %v", r, err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(filepath.Join(dir, "good"), link); err != nil {
		t.Fatal(err)
	}
	if r, err := readWriteRefusal(link, "t1"); err == nil || r != nil {
		t.Fatalf("a symlink was followed: %+v %v", r, err)
	}
	if r, err := readWriteRefusal(dir, "t1"); err == nil || r != nil {
		t.Fatalf("a directory read as a record: %+v %v", r, err)
	}
	fifo := filepath.Join(dir, "fifo")
	if err := syscall.Mkfifo(fifo, 0o600); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := readWriteRefusal(fifo, "t1"); done <- err }()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("a FIFO read as a record")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("a FIFO held the reader")
	}
	// Past the read bound, a record is not reached.
	padded := write("padded", strings.Repeat("x", maxWriteRefusalBytes)+"\n"+good+"\n")
	if r, _ := readWriteRefusal(padded, "t1"); r != nil {
		t.Fatalf("a record past the bound: %+v", r)
	}
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

// TestWriteRefusalStopIsOnForSessionPodsWithARecordDir: a session pod given a
// record directory (one with the cluster view) stops at a write refusal.
func TestWriteRefusalStopIsOnForSessionPodsWithARecordDir(t *testing.T) {
	session := Config{Session: "chat-a", WriteRefusalDir: "/scratch/.kube-agents"}
	session.applyDefaults()
	if !session.StopOnWriteRefusal {
		t.Fatal("a session pod with a record directory does not stop at a write refusal")
	}
}

// TestSessionTurnStopsAtAWriteAttempt: on a session pod the adapter makes the
// turn's record file in its directory, the kubectl wrapper's write-attempt
// record stops the harness, and the reason says a change was attempted and
// not made, without claiming a server refused it.
func TestSessionTurnStopsAtAWriteAttempt(t *testing.T) {
	const task = "task-session-attempt-1"
	dir := filepath.Join(t.TempDir(), ".kube-agents")
	out, text, pidFile, markerFile := refusalRun{
		taskID:     task,
		records:    []string{refusalRecord(t, true, writeAttemptRule, "scale", task)},
		first:      claudeToolCall(t, "toolu_1", "kubectl scale deploy/kube-dns -n kube-system --replicas=3", "Error from server (Forbidden)", true),
		stop:       true,
		expectStop: true,
		sessionDir: dir,
	}.run(t)
	if out.err != nil || out.res.State != lib.StateRejected {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	waitDead(t, pidFile)
	if _, err := os.Stat(markerFile); err == nil {
		t.Fatal("the harness ran on past the write attempt")
	}
	if !strings.HasPrefix(text, "reason: write-refused - ") || !strings.Contains(text, "kubectl scale was not made; writes are refused here.") {
		t.Fatalf("terminal %q", text)
	}
	for _, claim := range []string{"rule refused", "credential broker", "Forbidden", "kube-dns"} {
		if strings.Contains(text, claim) {
			t.Fatalf("terminal carries %q: %q", claim, text)
		}
	}
}

// TestSessionTurnIgnoresAnEarlierTurnsRecord: a record left from an earlier
// turn of a reused pod is gone when the next turn starts, so the next turn
// runs to its end.
func TestSessionTurnIgnoresAnEarlierTurnsRecord(t *testing.T) {
	const task = "task-session-attempt-2"
	dir := filepath.Join(t.TempDir(), ".kube-agents")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	stale := refusalRecord(t, true, writeAttemptRule, "scale", task) + "\n"
	if err := os.WriteFile(filepath.Join(dir, writeRefusalFileName), []byte(stale), 0o600); err != nil {
		t.Fatal(err)
	}
	out, _, _, markerFile := refusalRun{
		taskID:     task,
		first:      innocuousCall(t, "toolu_1"),
		stop:       true,
		sessionDir: dir,
	}.run(t)
	assertCompleted(t, out, markerFile)
}

// TestMakeTurnWriteRefusalFile: each turn's record file starts empty, 0600,
// whatever the turn before left.
func TestMakeTurnWriteRefusalFile(t *testing.T) {
	dir := filepath.Join(t.TempDir(), ".kube-agents")
	path, err := makeTurnWriteRefusalFile(dir)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(`{"write_refusal":true}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	again, err := makeTurnWriteRefusalFile(dir)
	if err != nil || again != path {
		t.Fatalf("second turn: %q %v", again, err)
	}
	if info, err := os.Stat(path); err != nil || info.Size() != 0 || info.Mode().Perm() != 0o600 {
		t.Fatalf("record file not made empty 0600: %v %v", info, err)
	}
}

// TestMakeWriteRefusalFile: the profile worker makes the record file empty
// beside the task token, replacing a stale one, names it in the harness's
// environment, and removes it after; with the stop off it makes none.
func TestMakeWriteRefusalFile(t *testing.T) {
	dir := t.TempDir()
	tokenFile := filepath.Join(dir, "token")
	stale := filepath.Join(dir, writeRefusalFileName)
	if err := os.WriteFile(stale, []byte(`{"write_refusal":true}`+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	w := &profileWorker{pw: ProfileWorkerConfig{TaskTokenFile: tokenFile}}
	w.cfg.Logger = slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := Config{TaskID: "t-7", StopOnWriteRefusal: true}
	remove := w.makeWriteRefusalFile(&cfg)
	if cfg.WriteRefusalFile != stale {
		t.Fatalf("record file %q, want %q", cfg.WriteRefusalFile, stale)
	}
	if info, err := os.Stat(stale); err != nil || info.Size() != 0 || info.Mode().Perm() != 0o600 {
		t.Fatalf("record file not made empty 0600: %v %v", info, err)
	}
	env := strings.Join(cfg.HarnessEnv, "\n")
	if !strings.Contains(env, EnvWriteRefusalFile+"="+stale) || !strings.Contains(env, EnvWriteRefusalTask+"=t-7") {
		t.Fatalf("harness env %q", env)
	}
	remove()
	if _, err := os.Stat(stale); !os.IsNotExist(err) {
		t.Fatalf("record file left after the task: %v", err)
	}
	off := Config{TaskID: "t-8"}
	w.makeWriteRefusalFile(&off)()
	if off.WriteRefusalFile != "" || len(off.HarnessEnv) != 0 {
		t.Fatalf("stop off made a record file: %+v", off)
	}
}

// TestSessionReadCountsOnlyTheWrappersOwnRecord: on a session pod, where the
// reason's verb is shown in chat, a record claiming another rule or carrying
// a verb no kubectl write has is skipped, and a later genuine record still
// counts.
func TestSessionReadCountsOnlyTheWrappersOwnRecord(t *testing.T) {
	path := filepath.Join(t.TempDir(), writeRefusalFileName)
	forged := refusalRecord(t, true, "kubernetes.read-only", "scale", "t1")
	odd := refusalRecord(t, true, writeAttemptRule, "http://x.example/y", "t1")
	genuine := refusalRecord(t, true, writeAttemptRule, "rollout restart", "t1")
	if err := os.WriteFile(path, []byte(forged+"\n"+odd+"\n"+genuine+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	r, err := readWriteRefusalFor(path, "t1", true)
	if err != nil || r == nil || r.rule != writeAttemptRule || r.verb != "rollout restart" {
		t.Fatalf("session read = %+v, %v; want the wrapper's rollout restart", r, err)
	}
	if err := os.WriteFile(path, []byte(forged+"\n"+odd+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if r, err := readWriteRefusalFor(path, "t1", true); err != nil || r != nil {
		t.Fatalf("session read of forged records = %+v, %v; want none", r, err)
	}
	// A profile pod's read is unchanged: the broker's rule counts.
	if r, err := readWriteRefusalFor(path, "t1", false); err != nil || r == nil || r.rule != "kubernetes.read-only" {
		t.Fatalf("profile read = %+v, %v", r, err)
	}
}
