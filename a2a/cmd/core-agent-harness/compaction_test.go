package main

import (
	"bytes"
	"context"
	"encoding/json"
	"iter"
	"os"
	"slices"
	"strings"
	"testing"

	"google.golang.org/genai"

	"github.com/go-steer/core-agent/v3/pkg/models"
	"github.com/go-steer/core-models/llm"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// bigModel answers every request with a long text, so the history grows
// quickly, and records each request's texts.
type bigModel struct{ echoModel }

func (m *bigModel) GenerateContent(ctx context.Context, req *llm.Request, stream bool) iter.Seq2[*llm.Response, error] {
	m.echoModel.GenerateContent(ctx, req, stream)
	return func(yield func(*llm.Response, error) bool) {
		yield(&llm.Response{Content: genai.NewContentFromText(strings.Repeat("words ", 400), genai.RoleModel), TurnComplete: true}, nil)
	}
}

// keep is the adapter's rule (worker-adapter's coreAgentDriver): append each
// event line, starting over at a compaction summary.
func keep(history []json.RawMessage, evs []coreagentwire.Event) []json.RawMessage {
	for _, ev := range evs {
		if ev.Type != coreagentwire.TypeEvent {
			continue
		}
		if coreagentwire.IsHistoryBoundary(ev.Event) {
			history = nil
		}
		history = append(history, ev.Event)
	}
	return history
}

// A long conversation stays bounded: once its history passes the threshold
// a turn compacts it first, the adapter keeps only the events from the
// summary on, and the envelope never grows past a few turns' worth.
func TestALongConversationStaysUnderTheCap(t *testing.T) {
	withModel(t, &bigModel{})
	const over = 16 << 10
	dir := t.TempDir()
	wd, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(wd)
	var session string
	var history []json.RawMessage
	largest, summaries := 0, 0
	for turn := 0; turn < 40; turn++ {
		env := coreagentwire.Envelope{Session: session, History: history, Message: "go on"}
		in, _ := json.Marshal(env)
		if len(in) > largest {
			largest = len(in)
		}
		var out, errOut bytes.Buffer
		if code := run(context.Background(), []string{"--model", "m", "--compact-over-bytes", "16384"}, bytes.NewReader(in), &out, &errOut); code != 0 {
			t.Fatalf("turn %d exit %d: %s %s", turn, code, out.String(), errOut.String())
		}
		var evs []coreagentwire.Event
		for _, l := range strings.Split(strings.TrimSpace(out.String()), "\n") {
			var ev coreagentwire.Event
			if err := json.Unmarshal([]byte(l), &ev); err != nil {
				t.Fatal(err)
			}
			if ev.Type == coreagentwire.TypeInit {
				session = ev.Session
			}
			if ev.Type == coreagentwire.TypeEvent && coreagentwire.IsHistoryBoundary(ev.Event) {
				summaries++
			}
			evs = append(evs, ev)
		}
		history = keep(history, evs)
	}
	if summaries == 0 {
		t.Fatal("40 long turns never compacted")
	}
	if largest > 4*over {
		t.Errorf("the envelope reached %d bytes; with compaction past %d it should stay within a few turns of it", largest, over)
	}
}

// sideCallRecorder is bigModel that records the side-call name of each
// request's context ("" for the turn's own requests).
type sideCallRecorder struct {
	bigModel
	names []string
}

func (m *sideCallRecorder) GenerateContent(ctx context.Context, req *llm.Request, stream bool) iter.Seq2[*llm.Response, error] {
	m.mu.Lock()
	m.names = append(m.names, models.SideCallName(ctx))
	m.mu.Unlock()
	return m.bigModel.GenerateContent(ctx, req, stream)
}

// The request that compacts the history goes out as a side call, the mark
// buildModel's sideCallsWithoutBuiltins keys on. This test's model is a
// fake, so it pins only that mark; that a side call then carries no
// server-side tool is TestASideCallCarriesNoServerSideTool's.
func TestTheCompactionRequestIsASideCall(t *testing.T) {
	m := &sideCallRecorder{}
	withModel(t, m)
	history := make([]json.RawMessage, 0, 64)
	for i := 0; i < 64; i++ {
		ev, _ := json.Marshal(map[string]any{"author": "user", "content": map[string]any{"role": "user", "parts": []map[string]string{{"text": strings.Repeat("words ", 400)}}}})
		history = append(history, ev)
	}
	in, _ := json.Marshal(coreagentwire.Envelope{History: history, Message: "go on"})
	var out, errOut bytes.Buffer
	if code := run(context.Background(), []string{"--model", "m", "--compact-over-bytes", "16384"}, bytes.NewReader(in), &out, &errOut); code != 0 {
		t.Fatalf("exit %d: %s %s", code, out.String(), errOut.String())
	}
	compacted := false
	for _, l := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var ev coreagentwire.Event
		if err := json.Unmarshal([]byte(l), &ev); err != nil {
			t.Fatal(err)
		}
		compacted = compacted || (ev.Type == coreagentwire.TypeEvent && coreagentwire.IsHistoryBoundary(ev.Event))
	}
	if !compacted {
		t.Fatalf("the turn did not compact: %s", out.String())
	}
	if !slices.Contains(m.names, "summarizer") {
		t.Errorf("no request went out as the summarizer side call; side-call names %q", m.names)
	}
}

// Under the threshold nothing is compacted.
func TestAShortConversationIsNotCompacted(t *testing.T) {
	withModel(t, &echoModel{})
	evs := runTurn(t, t.TempDir(), coreagentwire.Envelope{Message: "hi"})
	for _, ev := range evs {
		if ev.Type == coreagentwire.TypeEvent && coreagentwire.IsHistoryBoundary(ev.Event) {
			t.Fatal("a short conversation was compacted")
		}
	}
}

// The turn after a compaction starts from the summary the adapter kept, and
// the model gets it framed as a handover, as in the compacting turn itself:
// core-agent frames a summary only when an event comes before it, so the
// harness seeds one.
func TestTheTurnAfterACompactionGetsTheSummaryFramed(t *testing.T) {
	m := &bigModel{}
	withModel(t, m)
	dir := t.TempDir()
	wd, _ := os.Getwd()
	os.Chdir(dir)
	defer os.Chdir(wd)
	var session string
	var history []json.RawMessage
	compacted := -1
	for turn := 0; turn < 30; turn++ {
		in, _ := json.Marshal(coreagentwire.Envelope{Session: session, History: history, Message: "go on"})
		m.mu.Lock()
		before := len(m.texts)
		m.mu.Unlock()
		var out, errOut bytes.Buffer
		if code := run(context.Background(), []string{"--model", "m", "--compact-over-bytes", "16384"}, bytes.NewReader(in), &out, &errOut); code != 0 {
			t.Fatalf("turn %d exit %d: %s", turn, code, errOut.String())
		}
		var evs []coreagentwire.Event
		sawSummary := false
		for _, l := range strings.Split(strings.TrimSpace(out.String()), "\n") {
			var ev coreagentwire.Event
			json.Unmarshal([]byte(l), &ev)
			if ev.Type == coreagentwire.TypeInit {
				session = ev.Session
			}
			if ev.Type == coreagentwire.TypeEvent && coreagentwire.IsHistoryBoundary(ev.Event) {
				sawSummary = true
			}
			evs = append(evs, ev)
		}
		history = keep(history, evs)
		if compacted >= 0 {
			// This is the turn after the compaction: its first model
			// request (after the summariser's, which it doesn't make).
			m.mu.Lock()
			first := m.texts[before]
			m.mu.Unlock()
			if len(first) == 0 || !strings.HasPrefix(first[0], "[Conversation compacted.") {
				t.Fatalf("the turn after the compaction sent the model %q first, not the framed summary", first)
			}
			return
		}
		if sawSummary {
			compacted = turn
		}
	}
	t.Fatal("30 long turns never compacted")
}
