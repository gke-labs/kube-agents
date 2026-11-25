package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"io/fs"
	"iter"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	adkmodel "google.golang.org/adk/v2/model"
	"google.golang.org/genai"

	"github.com/go-steer/core-models/llm"

	"github.com/go-steer/core-agent/v3/pkg/models"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// echoModel answers every request with a text naming how many contents it
// was sent, and records each request's texts.
type echoModel struct {
	mu    sync.Mutex
	texts [][]string
}

func (m *echoModel) Name() string { return "gemini-2.5-flash" }

func (m *echoModel) GenerateContent(_ context.Context, req *llm.Request, _ bool) iter.Seq2[*llm.Response, error] {
	var texts []string
	for _, c := range req.Contents {
		for _, p := range c.Parts {
			if p.Text != "" {
				texts = append(texts, p.Text)
			}
		}
	}
	m.mu.Lock()
	m.texts = append(m.texts, texts)
	m.mu.Unlock()
	return func(yield func(*llm.Response, error) bool) {
		yield(&llm.Response{Content: genai.NewContentFromText("reply", genai.RoleModel), TurnComplete: true}, nil)
	}
}

func withModel(t *testing.T, m llm.LLM) {
	t.Helper()
	prev := newModel
	newModel = func(context.Context, string) (adkmodel.LLM, error) { return models.Adapt(m), nil }
	t.Cleanup(func() { newModel = prev })
}

// runTurn runs one harness process's turn in dir and returns its lines.
func runTurn(t *testing.T, dir string, env coreagentwire.Envelope) []coreagentwire.Event {
	t.Helper()
	in, err := json.Marshal(env)
	if err != nil {
		t.Fatal(err)
	}
	wd, _ := os.Getwd()
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(wd)
	var out, errOut bytes.Buffer
	if code := run(context.Background(), []string{"--model", "gemini-2.5-flash"}, bytes.NewReader(in), &out, &errOut); code != 0 {
		t.Fatalf("exit %d: %s %s", code, out.String(), errOut.String())
	}
	var evs []coreagentwire.Event
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var ev coreagentwire.Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("line %q: %v", line, err)
		}
		evs = append(evs, ev)
	}
	return evs
}

// A second process resumes the conversation from the history the first
// one's event lines carried: the model sees turn 1's message on turn 2.
func TestASecondTurnResumesFromTheEnvelopeHistory(t *testing.T) {
	m := &echoModel{}
	withModel(t, m)
	dir := t.TempDir()
	first := runTurn(t, dir, coreagentwire.Envelope{Message: "remember the word heron"})
	var session string
	var history []json.RawMessage
	for _, ev := range first {
		switch ev.Type {
		case coreagentwire.TypeInit:
			session = ev.Session
		case coreagentwire.TypeEvent:
			history = append(history, ev.Event)
		}
	}
	if session == "" || len(history) < 2 {
		t.Fatalf("turn 1: session %q, %d history events: %+v", session, len(history), first)
	}
	if last := first[len(first)-1]; last.Type != coreagentwire.TypeResult || last.Failed {
		t.Fatalf("turn 1 result %+v", last)
	}
	second := runTurn(t, dir, coreagentwire.Envelope{Session: session, History: history, Message: "what was the word?"})
	if second[0].Session != session {
		t.Errorf("turn 2 session %q, want %q", second[0].Session, session)
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	saw := strings.Join(m.texts[len(m.texts)-1], " | ")
	if !strings.Contains(saw, "heron") || !strings.Contains(saw, "what was the word?") {
		t.Errorf("turn 2's model request held %q, want turn 1's message and turn 2's", saw)
	}
}

// A turn writes no file: the conversation lives in the adapter, so no
// session store exists where the turn's own commands could edit it.
func TestATurnWritesNoSessionFile(t *testing.T) {
	withModel(t, &echoModel{})
	work, home, tmp := t.TempDir(), t.TempDir(), t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("TMPDIR", tmp)
	runTurn(t, work, coreagentwire.Envelope{Message: "hi"})
	for _, root := range []string{work, home, tmp} {
		_ = filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err == nil && path != root {
				t.Errorf("the turn wrote %s", path)
			}
			return nil
		})
	}
}

// An envelope over the cap fails the turn rather than being read.
func TestAnOversizedEnvelopeFailsTheTurn(t *testing.T) {
	withModel(t, &echoModel{})
	// A valid, newline-terminated envelope one byte over the cap: without
	// the cap check it would parse and run.
	in, _ := json.Marshal(coreagentwire.Envelope{Message: ""})
	pad := coreagentwire.MaxEnvelopeBytes + 1 - len(in)
	in, _ = json.Marshal(coreagentwire.Envelope{Message: strings.Repeat("x", pad)})
	if len(in) != coreagentwire.MaxEnvelopeBytes+1 {
		t.Fatalf("envelope is %d bytes, want the cap plus one", len(in))
	}
	var out, errOut bytes.Buffer
	if code := run(context.Background(), []string{"--model", "m"}, bytes.NewReader(append(in, '\n')), &out, &errOut); code == 0 {
		t.Fatalf("an oversized envelope ran: %s", out.String())
	}
	if !strings.Contains(out.String(), `"failed":true`) || !strings.Contains(out.String(), "over ") {
		t.Errorf("no failed result naming the cap: %s", out.String())
	}
}

// The harness reads its envelope to the newline and starts the turn while
// stdin stays open, as the adapter leaves it.
func TestTheHarnessStartsWithStdinStillOpen(t *testing.T) {
	withModel(t, &echoModel{})
	pr, pw := io.Pipe()
	defer pw.Close()
	go func() {
		b, _ := json.Marshal(coreagentwire.Envelope{Message: "hi"})
		pw.Write(append(b, '\n')) // and never close
	}()
	done := make(chan int, 1)
	var out, errOut bytes.Buffer
	wd, _ := os.Getwd()
	os.Chdir(t.TempDir())
	defer os.Chdir(wd)
	go func() { done <- run(context.Background(), []string{"--model", "m"}, pr, &out, &errOut) }()
	select {
	case code := <-done:
		if code != 0 || !strings.Contains(out.String(), `"type":"result"`) {
			t.Fatalf("exit %d: %s %s", code, out.String(), errOut.String())
		}
	case <-time.After(20 * time.Second):
		t.Fatal("the harness waited for stdin to close before running the turn")
	}
}

// callModel calls one tool, then answers.
type callModel struct {
	mu    sync.Mutex
	call  *genai.FunctionCall
	calls int
}

func (m *callModel) Name() string { return "gemini-2.5-flash" }

func (m *callModel) GenerateContent(_ context.Context, _ *llm.Request, _ bool) iter.Seq2[*llm.Response, error] {
	m.mu.Lock()
	m.calls++
	first := m.calls == 1
	m.mu.Unlock()
	return func(yield func(*llm.Response, error) bool) {
		if first {
			yield(&llm.Response{Content: &genai.Content{Role: genai.RoleModel, Parts: []*genai.Part{{FunctionCall: m.call}}}, TurnComplete: true}, nil)
			return
		}
		yield(&llm.Response{Content: genai.NewContentFromText("done", genai.RoleModel), TurnComplete: true}, nil)
	}
}

// TestARefusedCallReachesTheHarnesssStderr: end to end through the
// harness's own turn, a shell command the gate refuses writes one
// "tool refused:" line to the process's stderr, without the command.
func TestARefusedCallReachesTheHarnesssStderr(t *testing.T) {
	withModel(t, &callModel{call: &genai.FunctionCall{Name: "bash", Args: map[string]any{"command": "gcloud secrets-xyz list"}}})
	in, err := json.Marshal(coreagentwire.Envelope{Message: "go", Policy: coreagentwire.Policy{
		Tools: []string{"bash"}, Allow: []string{"bash:kubectl", "bash:kubectl *"},
	}})
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	wd, _ := os.Getwd()
	if err := os.Chdir(dir); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(wd)
	var out, errOut bytes.Buffer
	if code := run(context.Background(), []string{"--model", "gemini-2.5-flash"}, bytes.NewReader(in), &out, &errOut); code != 0 {
		t.Fatalf("exit %d: %s %s", code, out.String(), errOut.String())
	}
	var lines []string
	for _, l := range strings.Split(errOut.String(), "\n") {
		if strings.HasPrefix(l, RefusalLogPrefix) {
			lines = append(lines, l)
		}
	}
	// The line names the call by the id the turn's tool_call event carries.
	var callID string
	for _, l := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var ev coreagentwire.Event
		if json.Unmarshal([]byte(l), &ev) == nil && ev.Type == coreagentwire.TypeToolCall {
			callID = ev.ID
		}
	}
	if callID == "" {
		t.Fatalf("no tool_call event: %s", out.String())
	}
	if want := `tool refused: "bash" (no allow rule matches) call ` + strconv.Quote(callID); len(lines) != 1 || lines[0] != want {
		t.Errorf("stderr refusal lines %q, want [%q]: %s", lines, want, errOut.String())
	}
	if strings.Contains(errOut.String(), "secrets-xyz") {
		t.Errorf("stderr carries the command: %s", errOut.String())
	}
}

// replyModel calls one tool, then records the tool's response it is sent.
type replyModel struct {
	callModel
	responses []map[string]any
}

func (m *replyModel) GenerateContent(ctx context.Context, req *llm.Request, stream bool) iter.Seq2[*llm.Response, error] {
	m.mu.Lock()
	for _, c := range req.Contents {
		for _, p := range c.Parts {
			if p.FunctionResponse != nil {
				m.responses = append(m.responses, p.FunctionResponse.Response)
			}
		}
	}
	m.mu.Unlock()
	return m.callModel.GenerateContent(ctx, req, stream)
}

// TestARefusedShellCommandTellsTheModelWhatToDo: through the harness's own
// turn, the tool result the model reads for a refused shell command says
// why and what to do instead: for a granted command in a pipe, that the
// shell runs one plain command and to shape output with the command's own
// flags; for a command the grant doesn't name, which commands may run.
func TestARefusedShellCommandTellsTheModelWhatToDo(t *testing.T) {
	for _, c := range []struct {
		name, command, want string
	}{
		{"a granted command in a pipe", "kubectl get pods -A | grep -v Running", "one plain command at a time"},
		{"a granted command with a redirect", "kubectl get pods 2>&1", "-o jsonpath"},
		{"a command not granted", "gcloud info", "Only these commands may run here, each as one plain command: kubectl"},
		{"a pipe against the prefix", "kubectl|grep Running", "one plain command at a time"},
		{"a chain against the prefix", "kubectl;id", "one plain command at a time"},
		{"an environment prefix", "FOO=1 kubectl get pods", "environment prefixes"},
		{"a command not granted, in a pipe", "gcloud info | jq .", "Only these commands may run here"},
	} {
		t.Run(c.name, func(t *testing.T) {
			m := &replyModel{callModel: callModel{call: &genai.FunctionCall{Name: "bash", Args: map[string]any{"command": c.command}}}}
			withModel(t, m)
			runTurn(t, t.TempDir(), coreagentwire.Envelope{Message: "go", Policy: coreagentwire.Policy{
				Tools: []string{"bash"}, Allow: []string{"bash:kubectl", "bash:kubectl *"},
			}})
			m.mu.Lock()
			defer m.mu.Unlock()
			if len(m.responses) != 1 {
				t.Fatalf("the model was sent %d tool responses, want 1: %v", len(m.responses), m.responses)
			}
			got, _ := m.responses[0]["error"].(string)
			if !strings.Contains(got, c.want) || !strings.Contains(got, "requires an allowlist entry") {
				t.Errorf("the model read %q, want the gate's refusal and %q", got, c.want)
			}
		})
	}
}
