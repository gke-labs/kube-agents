package main

import (
	"context"
	"encoding/json"
	"iter"
	"strings"
	"sync"
	"testing"

	"google.golang.org/genai"

	"github.com/go-steer/core-models/llm"

	"github.com/gke-labs/kube-agents/a2a/lib/coreagentwire"
)

// systemModel records each request's system instruction text and answers.
type systemModel struct {
	mu     sync.Mutex
	system []string
}

func (m *systemModel) Name() string { return "gemini-2.5-flash" }

func (m *systemModel) GenerateContent(_ context.Context, req *llm.Request, _ bool) iter.Seq2[*llm.Response, error] {
	var b strings.Builder
	if req.Config != nil && req.Config.SystemInstruction != nil {
		for _, p := range req.Config.SystemInstruction.Parts {
			b.WriteString(p.Text)
		}
	}
	m.mu.Lock()
	m.system = append(m.system, b.String())
	m.mu.Unlock()
	return func(yield func(*llm.Response, error) bool) {
		yield(&llm.Response{Content: genai.NewContentFromText("reply", genai.RoleModel), TurnComplete: true}, nil)
	}
}

// The envelope's instructions reach the model's system instruction, and the
// conversation's history never carries them back.
func TestTheEnvelopesInstructionsReachTheSystemPromptNotTheHistory(t *testing.T) {
	m := &systemModel{}
	withModel(t, m)
	const line = "You have read-only kubectl on PATH; one command per call."
	evs := runTurn(t, t.TempDir(), coreagentwire.Envelope{Message: "hello", Instructions: []string{line}})

	m.mu.Lock()
	defer m.mu.Unlock()
	if len(m.system) == 0 || !strings.Contains(m.system[0], line) {
		t.Fatalf("system instruction %q, want it to carry %q", m.system, line)
	}
	for _, ev := range evs {
		if ev.Type == coreagentwire.TypeEvent && strings.Contains(string(ev.Event), "one command per call") {
			raw, _ := json.Marshal(ev)
			t.Errorf("an instruction reached the history: %s", raw)
		}
	}
}
