package workeradapter

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
	"github.com/gke-labs/kube-agents/a2a/lib/askkind"
	"github.com/gke-labs/kube-agents/a2a/lib/chatmsg"
)

// #6536: a profile task's progress carries the ask's kind, as the
// hermes-bridge's does, so the gateway's reaction follows it: every progress
// update is the text, then a chat message carrying only the mark, which the
// gateway draws (markFrom) and which lays nothing out, so the rolling line
// still renders the text.
func TestProgressCarriesTheAsksKindAsAMarkOnlyChatMessage(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-otter-mark1", "task-mark-1"
	const ask = "why is checkout crashlooping in prod"
	submit(t, c, session, taskID, ask)

	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-1"}'
read first || exit 1
echo '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"pondering"}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"reading the pod events"}]}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"found the OOM kill"},{"type":"tool_use","name":"Bash","input":{"command":"kubectl get pods"}}]}}'
echo '{"type":"result","subtype":"success","result":"it was OOM killed"}'
`)
	out := waitOutcome(t, runAdapter(context.Background(), adapterConfig(url, taskID, session, harness)), 30*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}

	want := askkind.Kind(ask)
	if want != askkind.Investigate {
		t.Fatalf("fixture: askkind.Kind(%q) = %q, want investigate", ask, want)
	}
	var lines []string
	for _, env := range replayEvents(t, url, session, taskID) {
		if env.Kind != lib.KindArtifactUpdate {
			continue
		}
		var upd lib.ArtifactUpdate
		if err := json.Unmarshal(env.Payload, &upd); err != nil {
			t.Fatal(err)
		}
		switch upd.Artifact.Name {
		case lib.ArtifactProgress:
			r := chatmsg.Read(chatmsg.Progress, upd.Artifact.Parts)
			if len(r.Problems) != 0 {
				t.Fatalf("progress chat message problems: %v", r.Problems)
			}
			if r.Mark == nil || r.Mark.Ask != want {
				t.Fatalf("progress mark = %+v, want ask %q", r.Mark, want)
			}
			if r.Message == nil || !r.Message.MarkOnly() {
				t.Fatalf("progress chat message lays something out: %+v", r.Message)
			}
			lines = append(lines, r.Fallback)
		case lib.ArtifactThinking, lib.ArtifactActivity:
			// Unmarked, as before: one part each, no chat message.
			if len(upd.Artifact.Parts) != 1 || chatmsg.Read(chatmsg.Progress, upd.Artifact.Parts).Mark != nil {
				t.Fatalf("%s carries a mark: %+v", upd.Artifact.Name, upd.Artifact.Parts)
			}
		}
	}
	if len(lines) != 2 || lines[0] != "reading the pod events" || lines[1] != "found the OOM kill" {
		t.Fatalf("progress lines %q", lines)
	}
}

func TestProgressPartsFallsBackToBareText(t *testing.T) {
	if got := progressParts("hello", ""); len(got) != 1 || got[0].Text != "hello" {
		t.Fatalf("no kind: %+v", got)
	}
	// A whitespace line is no fallback a chat message may carry, so the
	// bare text goes, as the bridge's does.
	if got := progressParts("   ", askkind.Question); len(got) != 1 || got[0].Text != "   " {
		t.Fatalf("blank line: %+v", got)
	}
	got := progressParts("hello", askkind.Question)
	if len(got) != 2 || got[0].Text != "hello" {
		t.Fatalf("marked: %+v", got)
	}
	if r := chatmsg.Read(chatmsg.Progress, got); r.Mark == nil || r.Mark.Ask != askkind.Question {
		t.Fatalf("marked read: %+v", r)
	}
}
