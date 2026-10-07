package lib

import (
	"testing"
	"time"
)

// TestTheFoldCarriesTheTerminalsTimestamp: FinalAt is the final
// status-update's envelope timestamp, and zero until a final event folds.
func TestTheFoldCarriesTheTerminalsTimestamp(t *testing.T) {
	origin, err := NewMessageEnvelope(Party{Session: "chatops"}, "task-fa", "ctx-fa", "corr-fa", validMessagePayload())
	if err != nil {
		t.Fatal(err)
	}
	exec, err := (&Client{}).NewTaskExecution(origin, Party{Session: "worker-fa"}, "worker-fa")
	if err != nil {
		t.Fatal(err)
	}
	mk := func(state TaskState, final bool, ts time.Time) *Envelope {
		e, err := exec.StatusEnvelope(state, final)
		if err != nil {
			t.Fatal(err)
		}
		e.TS = ts
		return e
	}
	start := time.Date(2026, 10, 6, 12, 0, 0, 0, time.UTC)
	end := start.Add(90 * time.Second)
	running, err := FoldTask("task-fa", []*Envelope{mk(StateSubmitted, false, start), mk(StateWorking, false, start)})
	if err != nil {
		t.Fatal(err)
	}
	if !running.FinalAt.IsZero() {
		t.Fatalf("FinalAt before the final event = %v, want zero", running.FinalAt)
	}
	done, err := FoldTask("task-fa", []*Envelope{mk(StateSubmitted, false, start), mk(StateWorking, false, start), mk(StateCompleted, true, end)})
	if err != nil {
		t.Fatal(err)
	}
	if !done.FinalAt.Equal(end) {
		t.Fatalf("FinalAt = %v, want the final event's %v", done.FinalAt, end)
	}
}
