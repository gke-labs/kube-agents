package gateway

import (
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Clearing a batch child's text at AskTTL keeps a sent end's state and
// drops its text. An end not sent yet (Unsent), which the heal would send
// with what is left, becomes a failed end saying its result was dropped,
// with none of the child's text, so the parent isn't sent an empty result.
func TestClearingAnUnsentBatchEndsTextSendsAFailureNotAnEmptyResult(t *testing.T) {
	for _, tc := range []struct {
		name      string
		end       BatchEnd
		wantState lib.TaskState
		wantText  string
	}{
		{"sent", BatchEnd{State: lib.StateCompleted, Text: "the result"}, lib.StateCompleted, ""},
		{"unsent completed", BatchEnd{State: lib.StateCompleted, Text: "the result", Unsent: true}, lib.StateFailed, unsentEndExpired},
		{"unsent question", BatchEnd{State: lib.StateInputRequired, Text: "which namespace?", Unsent: true}, lib.StateFailed, unsentEndExpired},
	} {
		t.Run(tc.name, func(t *testing.T) {
			shared := tc.end
			ref := TaskRef{ID: "c1", BatchEnd: &shared}
			ref.clearBatchText()
			if ref.BatchEnd.State != tc.wantState || ref.BatchEnd.Text != tc.wantText || ref.BatchEnd.Unsent != tc.end.Unsent {
				t.Fatalf("end after clearing = %+v, want state %s text %q unsent %v", *ref.BatchEnd, tc.wantState, tc.wantText, tc.end.Unsent)
			}
			if shared != tc.end {
				t.Fatalf("the shared end was changed: %+v", shared)
			}
			if got := profileBatchEndState(*ref.BatchEnd); got != tc.wantState {
				t.Fatalf("state sent = %s, want %s", got, tc.wantState)
			}
		})
	}
}
