package main

import (
	"bytes"
	"context"
	"encoding/json"
	"iter"
	"os"
	"strings"
	"testing"

	"github.com/go-steer/core-models/llm"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// stopModel stands in for the stop signal arriving mid-turn: its first
// request cancels the turn's context and then fails with it, as a model
// call does when the adapter's SIGTERM cancels it.
type stopModel struct{ stop context.CancelFunc }

func (m *stopModel) Name() string { return "gemini-2.5-flash" }

func (m *stopModel) GenerateContent(ctx context.Context, _ *llm.Request, _ bool) iter.Seq2[*llm.Response, error] {
	return func(yield func(*llm.Response, error) bool) {
		m.stop()
		<-ctx.Done()
		yield(nil, ctx.Err())
	}
}

// A stopped turn writes no result and no history: the adapter sent the
// signal (a cancel or the task's deadline) and decides the task's state
// itself, so a result line from here would report the stop as the
// harness's own failure, or as a completed turn.
func TestAStoppedTurnWritesNoResult(t *testing.T) {
	ctx, stop := context.WithCancel(context.Background())
	defer stop()
	withModel(t, &stopModel{stop: stop})
	in, err := json.Marshal(coreagentwire.Envelope{Message: "hello"})
	if err != nil {
		t.Fatal(err)
	}
	wd, _ := os.Getwd()
	if err := os.Chdir(t.TempDir()); err != nil {
		t.Fatal(err)
	}
	defer os.Chdir(wd)

	var out, errOut bytes.Buffer
	code := run(ctx, []string{"--model", "gemini-2.5-flash"}, bytes.NewReader(append(in, '\n')), &out, &errOut)
	if code == 0 {
		t.Errorf("exit 0 for a stopped turn, want non-zero")
	}
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		if line == "" {
			continue
		}
		var ev coreagentwire.Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("line %q: %v", line, err)
		}
		switch ev.Type {
		case coreagentwire.TypeResult, coreagentwire.TypeEvent, coreagentwire.TypeToolResult:
			t.Errorf("a stopped turn wrote a %s line: %s", ev.Type, line)
		}
	}
}
