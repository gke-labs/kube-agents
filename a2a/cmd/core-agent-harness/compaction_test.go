package main

import (
	"bytes"
	"context"
	"encoding/json"
	"iter"
	"os"
	"strings"
	"testing"

	"google.golang.org/genai"

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
