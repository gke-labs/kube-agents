package workeradapter

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// Every turn is a fresh pod, so the conversation so far reaches the harness
// only through the primer. The opening prompt the harness reads has to carry
// it ahead of the new message, framed so the model reads it as history.
// Without it a follow-up ("what code word did I give you?") starts cold.
func TestTheOpeningPromptCarriesThePrimerAheadOfTheNewMessage(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-prim", "task-primer-1"
	submit(t, c, session, taskID, "what code word did I give you?")

	seen := filepath.Join(t.TempDir(), "stdin.txt")
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-1"}'
read first || exit 1
printf '%s' "$first" > '`+seen+`'
echo '{"type":"result","subtype":"success","result":"PELICAN"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.Primer = "Transcript primer:\n\nUser: remember the code word PELICAN\n\nYou: OK\n"
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	b, err := os.ReadFile(seen)
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	primer, question := strings.Index(got, "remember the code word PELICAN"), strings.Index(got, "what code word did I give you?")
	if primer < 0 || question < 0 || primer > question {
		t.Fatalf("the opening prompt doesn't carry the primer ahead of the new message: %s", got)
	}
	if !strings.Contains(got, "continuing an ongoing conversation") {
		t.Errorf("the primer isn't framed as earlier conversation: %s", got)
	}
}

// No primer is a first turn: the prompt is the message alone, unframed.
func TestWithPrimerLeavesAFirstTurnAlone(t *testing.T) {
	for _, p := range []string{"", "  \n"} {
		if got := withPrimer(p, "hello"); got != "hello" {
			t.Errorf("withPrimer(%q, hello) = %q, want the message alone", p, got)
		}
	}
}
