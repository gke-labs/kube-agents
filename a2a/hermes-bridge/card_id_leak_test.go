package hermesbridge

import (
	"net/http"
	"strings"
	"testing"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The rule: an id in the answer, no task word in the ask.
func TestLeaksCardID(t *testing.T) {
	cases := []struct {
		name, prompt, answer string
		want                 bool
	}{
		{"id, count question", "how many clusters are running?", "platform counted 209 clusters on card t_7bd8e9a5.", true},
		{"two ids", "how many clusters?", "on cards t_78c29f59 and t_7bd8e9a5", true},
		{"id in a link", "how many clusters?", "see https://x.invalid/t_7bd8e9a5", true},
		{"no id", "how many clusters?", "platform counted 209 clusters about an hour ago.", false},
		{"id, asked about tasks", "what's on my tasks?", "t_7bd8e9a5 is running.", false},
		{"id, asked about a card", "summarize that card", "card t_7bd8e9a5 found 209 clusters.", false},
		{"id, asked about the board", "check the board for a count", "t_7bd8e9a5 has it.", false},
		{"id, asked about kanban", "list my kanban", "t_7bd8e9a5", false},
		{"id shape too short", "how many?", "t_7bd8e9a is not an id", false},
		{"id shape uppercase", "how many?", "T_7BD8E9A5 is not an id", false},
		{"id glued to a word", "how many?", "task_t_7bd8e9a5x", false},
	}
	for _, tc := range cases {
		if got := leaksCardID(tc.prompt, tc.answer); got != tc.want {
			t.Errorf("%s: leaksCardID(%q, %q) = %v, want %v", tc.name, tc.prompt, tc.answer, got, tc.want)
		}
	}
}

// An answer that names a card id where the user asked about no task is asked
// for again, once, under its own idempotency key, and the restated answer is
// the turn's result.
func TestAPI_CardIDOutsideABoardReadIsRestated(t *testing.T) {
	_, url := startServer(t)
	const leaked = "platform counted 209 running GKE clusters on card t_7bd8e9a5."
	const clean = "platform counted 209 running GKE clusters about an hour ago."
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		if c.prompt == cardIDRestatePrompt {
			writeCompletion(w, c.sessionID, clean)
			return
		}
		writeCompletion(w, c.sessionID, leaked)
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	submitIn(t, c, "task-restate", "ctx-restate", "how many GKE clusters are running?")
	task := waitTerminal(t, c, "task-restate")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s (%s), want completed", task.State, terminalReason(t, task))
	}
	if a := task.Artifact(lib.ArtifactResult); a == nil || len(a.Parts) == 0 || a.Parts[0].Text != clean {
		t.Fatalf("result = %+v, want the restated answer", a)
	}
	calls := stub.seen()
	if len(calls) != 2 {
		t.Fatalf("calls = %d, want the turn and one restate", len(calls))
	}
	if calls[1].prompt != cardIDRestatePrompt || calls[1].sessionID != calls[0].sessionID {
		t.Errorf("restate call = %+v, want the restate prompt in the same session", calls[1])
	}
	if calls[1].idempotency != calls[0].idempotency+cardIDRestateKeySuffix {
		t.Errorf("restate key = %q, want %q", calls[1].idempotency, calls[0].idempotency+cardIDRestateKeySuffix)
	}
}

// A restated answer that still names an id is delivered as it is: one
// restate, never two.
func TestAPI_ARestatedAnswerThatStillLeaksIsDelivered(t *testing.T) {
	_, url := startServer(t)
	const leaked = "platform counted 209 clusters on card t_7bd8e9a5."
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		writeCompletion(w, c.sessionID, leaked)
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	submitIn(t, c, "task-restate-twice", "ctx-restate-twice", "how many clusters?")
	task := waitTerminal(t, c, "task-restate-twice")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s (%s), want completed", task.State, terminalReason(t, task))
	}
	if a := task.Artifact(lib.ArtifactResult); a == nil || len(a.Parts) == 0 || a.Parts[0].Text != leaked {
		t.Fatalf("result = %+v, want the answer delivered as it is", a)
	}
	if n := len(stub.seen()); n != 2 {
		t.Fatalf("calls = %d, want the turn and one restate", n)
	}
}

// An ask about the user's tasks keeps the id, and an answer without one is
// never asked for again: one call each.
func TestAPI_ABoardReadOrACleanAnswerIsNotRestated(t *testing.T) {
	for name, tc := range map[string]struct{ prompt, answer string }{
		"board read":   {"what's on my tasks right now?", "t_7bd8e9a5 is counting clusters; t_78c29f59 is done."},
		"clean answer": {"how many clusters?", "platform counted 209 clusters about an hour ago."},
	} {
		t.Run(name, func(t *testing.T) {
			_, url := startServer(t)
			stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
				writeCompletion(w, c.sessionID, tc.answer)
			})
			startAPIBridge(t, url, stub, nil)
			c := gatewayClient(t, url)
			id := "task-keep-" + strings.ReplaceAll(name, " ", "-")
			submitIn(t, c, id, "ctx-keep", tc.prompt)
			task := waitTerminal(t, c, id)
			if task.State != lib.StateCompleted {
				t.Fatalf("state = %s (%s), want completed", task.State, terminalReason(t, task))
			}
			if a := task.Artifact(lib.ArtifactResult); a == nil || len(a.Parts) == 0 || a.Parts[0].Text != tc.answer {
				t.Fatalf("result = %+v, want the answer unchanged", a)
			}
			if n := len(stub.seen()); n != 1 {
				t.Fatalf("calls = %d, want one", n)
			}
		})
	}
}
