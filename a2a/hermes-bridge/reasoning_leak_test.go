package hermesbridge

import (
	"net/http"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The reasoning Gemini wrote as content in the #3773 turn, shortened: a bare
// `thought` line, the model's plan, and the intended ack run together.
const leakedReasoning = "thought\nNow that the task is created, I need to tell the user I am " +
	"checking the kubernetes version of a2a-next-dev.checking the kubernetes version of a2a-next-dev."

// leakedReasoningMarker is a phrase in every leaked shape below that must
// never reach the terminal's reason.
const leakedReasoningMarker = "Now that the task is created"

// The three shapes seen (#3206, #3773): `thought`, `thought:`, and `thought:`
// with the reasoning on the same line.
var leakedShapes = map[string]string{
	"bare line":      leakedReasoning,
	"colon line":     "thought:\nNow that the task is created, the user wants to delete the namespace.",
	"colon and text": "thought:Now that the task is created, the user asked for the delete.",
}

func TestOpensWithUnstructuredReasoning(t *testing.T) {
	cases := []struct {
		answer string
		want   bool
	}{
		{leakedReasoning, true},
		{"thought:\nThe user wants to delete the namespace.", true},
		{"thought:The user asked for the delete.", true},
		{"THOUGHT: the plan", true},
		{"thought", true},
		{"thought:", true},
		{"Thought\nthe plan", true},
		{"  THOUGHT  \r\nthe plan", true},
		{"\n\n  \nthought\nthe plan", true},
		{"The cluster runs 1.33.", false},
		{"I thought about it: the cluster runs 1.33.", false},
		{"Thought about it, the cluster runs 1.33.", false},
		{"My thought: the cluster runs 1.33.", false},
		{"thoughts\nthe plan", false},
		// TrimSpace keeps U+001C..U+001F; the Python side of the rule matches it.
		{"\x1cthought\nthe plan", false},
		{"thoughtful answer", false},
		{"thought :the plan", false},
		{"thought about it\nthe plan", false},
		{"The answer.\nthought\nmore", false},
		{"The answer.\nthought: more", false},
		{"thought\rthe plan", false},
		{"\n\nThe cluster runs 1.33.", false},
		{"", false},
		{"\n  \n", false},
	}
	for _, tc := range cases {
		if got := opensWithUnstructuredReasoning(tc.answer); got != tc.want {
			t.Errorf("opensWithUnstructuredReasoning(%q) = %v, want %v", tc.answer, got, tc.want)
		}
	}
}

// An API turn whose answer is the model's reasoning, in any of the shapes,
// ends failed with the token and the session, and neither the result nor the
// reason carries the text.
func TestAPI_UnstructuredReasoningIsWithheld(t *testing.T) {
	for name, answer := range leakedShapes {
		t.Run(name, func(t *testing.T) {
			_, url := startServer(t)
			stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
				writeCompletion(w, c.sessionID, answer)
			})
			startAPIBridge(t, url, stub, nil)
			c := gatewayClient(t, url)

			taskID := "task-thought-" + strings.ReplaceAll(name, " ", "-")
			submitIn(t, c, taskID, "ctx-thought", "what version is a2a-next-dev")
			task := waitTerminal(t, c, taskID)
			if task.State != lib.StateFailed {
				t.Fatalf("state = %s, want failed", task.State)
			}
			reason := terminalReason(t, task)
			want := "reason: " + unstructuredReasoningToken + " - " + unstructuredReasoningNotice + "; session: a2a-ctx-thought"
			if reason != want {
				t.Errorf("reason = %q, want %q", reason, want)
			}
			if strings.Contains(reason, leakedReasoningMarker) {
				t.Errorf("the reason carries the withheld text: %q", reason)
			}
			if a := task.Artifact(lib.ArtifactResult); a != nil {
				t.Errorf("a result artifact was published for a withheld answer: %+v", a.Parts)
			}
		})
	}
}

// Answers that only mention a thought, or open with blank lines, are answers.
func TestAPI_AnswersMentioningThoughtPassThrough(t *testing.T) {
	answers := map[string]string{
		"normal":        "The cluster runs 1.33.",
		"thought later": "I thought it was 1.32; the cluster runs 1.33.",
		"thought about": "Thought about it, the cluster runs 1.33.",
		"blank first":   "\n\nThe cluster runs 1.33.",
		"thought below": "The cluster runs 1.33.\nthought: nothing more",
	}
	for name, answer := range answers {
		t.Run(name, func(t *testing.T) {
			_, url := startServer(t)
			stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
				writeCompletion(w, c.sessionID, answer)
			})
			startAPIBridge(t, url, stub, nil)
			c := gatewayClient(t, url)

			taskID := "task-pass-" + strings.ReplaceAll(name, " ", "-")
			submitIn(t, c, taskID, "ctx-pass", "what version")
			task := waitTerminal(t, c, taskID)
			if task.State != lib.StateCompleted {
				t.Fatalf("state = %s (%s), want completed", task.State, terminalReason(t, task))
			}
			a := task.Artifact(lib.ArtifactResult)
			if a == nil || len(a.Parts) == 0 {
				t.Fatal("no result artifact")
			}
			if got := a.Parts[0].Text; got != answer {
				t.Errorf("result = %q, want the answer unchanged %q", got, answer)
			}
		})
	}
}

// A follow-up turn whose answer is reasoning is withheld too: the opening
// turn's answer stays on the stream as a turn artifact, the terminal names
// the turn, and each turn's usage still goes out.
func TestAPI_FollowUpReasoningIsWithheld(t *testing.T) {
	_, url := startServer(t)
	release := make(chan struct{})
	var answered atomic.Bool
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		if answered.CompareAndSwap(false, true) {
			<-release
			writeCompletion(w, c.sessionID, "answer to "+c.prompt)
			return
		}
		writeCompletion(w, c.sessionID, leakedShapes["colon line"])
	})
	startAPIBridge(t, url, stub, nil)
	c := gatewayClient(t, url)

	origin := submitIn(t, c, "task-thought-turn2", "ctx-thought-turn2", "long question")
	waitFor(t, 10*time.Second, "turn 1 in flight", func() bool { return len(stub.seen()) == 1 })
	sendSteer(t, c, origin, "also east")
	waitFor(t, 10*time.Second, "queued", func() bool { return len(steerNotices(t, url, origin.TaskID)) == 1 })
	close(release)
	task := waitTerminal(t, c, origin.TaskID)
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	reason := terminalReason(t, task)
	want := "reason: " + unstructuredReasoningToken + " - " + unstructuredReasoningNotice + "; session: a2a-ctx-thought-turn2; turn: 2"
	if reason != want {
		t.Errorf("reason = %q, want %q", reason, want)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactTurn); !slices.Equal(got, []string{"answer to long question"}) {
		t.Errorf("turn answers %q, want the opening turn's", got)
	}
	if got := artifactsNamed(t, url, origin.TaskID, lib.ArtifactResult); len(got) != 0 {
		t.Errorf("result %q published for a withheld answer", got)
	}
	if got := usageSeen(t, url, origin.TaskID); len(got) != 2 {
		t.Errorf("usage parts = %+v, want one per turn", got)
	}
}

// The cli executor withholds the same answer, naming the session hermes
// printed, and passes an answer with a blank first line.
func TestLifecycle_UnstructuredReasoningIsWithheld(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, script(t, `case "$1" in
  leak) printf 'thought\nNow that the task is created, I need to check.\n'
        echo "session_id: 20261022_172000_th" >&2 ;;
  colon) printf 'thought:Now that the task is created, the user asked.\n' ;;
  *) printf '\nThe cluster runs 1.33.\n' ;;
esac`))
	c := gatewayClient(t, url)

	for _, tc := range []struct{ taskID, prompt, session string }{
		{"task-cli-thought", "leak", "; session: 20261022_172000_th"},
		{"task-cli-colon", "colon", ""},
	} {
		submit(t, c, tc.taskID, tc.prompt)
		task := waitTerminal(t, c, tc.taskID)
		if task.State != lib.StateFailed {
			t.Fatalf("%s: state = %s, want failed", tc.taskID, task.State)
		}
		reason := terminalReason(t, task)
		if want := "reason: " + unstructuredReasoningToken + " - " + unstructuredReasoningNotice + tc.session; reason != want {
			t.Errorf("%s: reason = %q, want %q", tc.taskID, reason, want)
		}
		if strings.Contains(reason, leakedReasoningMarker) {
			t.Errorf("%s: the reason carries the withheld text: %q", tc.taskID, reason)
		}
		if a := task.Artifact(lib.ArtifactResult); a != nil {
			t.Errorf("%s: a result artifact was published for a withheld answer: %+v", tc.taskID, a.Parts)
		}
	}

	submit(t, c, "task-cli-blank", "answer")
	task := waitTerminal(t, c, "task-cli-blank")
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s (%s), want completed", task.State, terminalReason(t, task))
	}
	if a := task.Artifact(lib.ArtifactResult); a == nil || !strings.Contains(a.Parts[0].Text, "The cluster runs 1.33.") {
		t.Errorf("result = %+v, want the answer", a)
	}
}
