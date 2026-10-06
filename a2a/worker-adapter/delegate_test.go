package workeradapter

import (
	"context"
	"encoding/json"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// delegateSock is a socket path short enough for macOS's 104-byte sun_path:
// t.TempDir() under $TMPDIR plus a test name is over it.
func delegateSock(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "d")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	return filepath.Join(dir, "d.sock")
}

// askDelegate plays the MCP subcommand's half: dial, one request, one reply.
// It retries the dial because the listener comes up inside Run.
func askDelegate(t *testing.T, sock string, req lib.DelegateRequest) delegateReply {
	t.Helper()
	var c net.Conn
	var err error
	deadline := time.Now().Add(waitDeadline)
	for {
		c, err = net.Dial("unix", sock)
		if err == nil || time.Now().After(deadline) {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if err != nil {
		t.Fatalf("dial %s: %v", sock, err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(delegateExchangeTimeout))
	if err := json.NewEncoder(c).Encode(req); err != nil {
		t.Fatal(err)
	}
	var reply delegateReply
	if err := json.NewDecoder(c).Decode(&reply); err != nil {
		t.Fatalf("reading the adapter's reply: %v", err)
	}
	return reply
}

// TestADelegateCallPublishesTheArtifactAndEndsTheTurn: while the harness is
// still mid-turn, a request on the socket produces the reserved artifact with
// one data part and the turn completes with the one-line result. The harness
// prints a result of its own after the call, and it is not the deliverable.
//
// The stub ignores SIGTERM, so it lives until the SIGKILL KillGrace after the
// delegation: that keeps the supervise loop open for the second request, and
// a TaskDeadline well past the wait proves the turn ended on the kill, not on
// the deadline.
func TestADelegateCallPublishesTheArtifactAndEndsTheTurn(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-test-del", "task-del-1"
	submit(t, c, session, taskID, "find out how the fleet is")
	sock := delegateSock(t)
	harness := stub(t, `
trap '' TERM
echo '{"type":"system","subtype":"init","session_id":"stub-1"}'
read first || exit 1
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"I will ask the platform agent."}]}}'
read -t 30 line || true
echo '{"type":"result","subtype":"success","result":"late result nobody should see"}'
sleep 60
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.DelegateSocket = sock
	cfg.TaskDeadline = 2 * time.Minute
	cfg.KillGrace = 5 * time.Second
	done := runAdapter(context.Background(), cfg)

	reply := askDelegate(t, sock, lib.DelegateRequest{Addressee: "platform", Text: "how is the fleet?"})
	if !reply.OK {
		t.Fatalf("refused: %+v", reply)
	}
	second := askDelegate(t, sock, lib.DelegateRequest{Addressee: "platform", Text: "again"})
	if second.OK || !strings.Contains(second.Message, "already delegated") {
		t.Fatalf("a second request in one turn: %+v", second)
	}
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	del := task.Artifact(lib.ArtifactDelegate)
	if del == nil || len(del.Parts) != 1 || del.Parts[0].Kind != "data" {
		t.Fatalf("delegate artifact = %+v", del)
	}
	var got lib.DelegateRequest
	if err := json.Unmarshal(del.Parts[0].Data, &got); err != nil || got.Addressee != "platform" || got.Text != "how is the fleet?" {
		t.Fatalf("data part = %s (%v)", del.Parts[0].Data, err)
	}
	if r := artifactText(task, lib.ArtifactResult); r != "delegated to platform" {
		t.Fatalf("result = %q", r)
	}
}

func TestADelegateCallIsRefusedOverTheTextCap(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-test-del2", "task-del-2"
	submit(t, c, session, taskID, "x")
	sock := delegateSock(t)
	harness := stub(t, `
echo '{"type":"system","subtype":"init","session_id":"stub-1"}'
read first || exit 1
read -t 8 line || true
echo '{"type":"result","subtype":"success","result":"normal result"}'
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.DelegateSocket = sock
	done := runAdapter(context.Background(), cfg)

	reply := askDelegate(t, sock, lib.DelegateRequest{Addressee: "platform", Text: strings.Repeat("x", lib.DelegateTextCap+1)})
	if reply.OK || !strings.Contains(reply.Message, "too long") {
		t.Fatalf("reply = %+v", reply)
	}
	if empty := askDelegate(t, sock, lib.DelegateRequest{Addressee: " ", Text: "y"}); empty.OK {
		t.Fatalf("empty addressee accepted: %+v", empty)
	}
	if empty := askDelegate(t, sock, lib.DelegateRequest{Addressee: "platform", Text: ""}); empty.OK {
		t.Fatalf("empty text accepted: %+v", empty)
	}
	// The turn continues: the harness's own result is the deliverable.
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCompleted {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if task.Artifact(lib.ArtifactDelegate) != nil {
		t.Fatal("a refused request published an artifact")
	}
	if r := artifactText(task, lib.ArtifactResult); r != "normal result" {
		t.Fatalf("result = %q", r)
	}
}

func TestNoSocketConfiguredMeansNoListener(t *testing.T) {
	ch, stop, err := startDelegateListener(Config{}.DelegateSocket, nil)
	if err != nil || ch != nil || stop == nil {
		t.Fatalf("empty path: ch=%v err=%v", ch, err)
	}
	stop()
}

// TestADelegateCallAfterCancelIsRefused: once cancel has reached supervise,
// the turn is over even though the harness is still dying. A delegate call in
// that window must not publish a child request, nor turn the canceled task
// into a completed one. The stub traps TERM and records it, so the test knows
// cancel has been acted on, and lives until the SIGKILL KillGrace later.
func TestADelegateCallAfterCancelIsRefused(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-test-del3", "task-del-3"
	origin := submit(t, c, session, taskID, "find out how the fleet is")
	sock := delegateSock(t)
	marker := filepath.Join(filepath.Dir(sock), "termed")
	harness := stub(t, `
trap 'touch `+marker+`' TERM
echo '{"type":"system","subtype":"init","session_id":"stub-1"}'
read first || exit 1
while true; do sleep 1; done
`)
	cfg := adapterConfig(url, taskID, session, harness)
	cfg.DelegateSocket = sock
	cfg.TaskDeadline = 2 * time.Minute
	cfg.KillGrace = 10 * time.Second
	done := runAdapter(context.Background(), cfg)
	waitState(t, c, session, taskID, lib.StateWorking)

	env, err := lib.NewCancelEnvelope(gatewayParty, taskID, origin.ContextID, origin.CorrelationID,
		lib.WithTo(lib.Party{Session: session}))
	if err != nil {
		t.Fatalf("cancel envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := c.Publish(ctx, lib.TaskInSubject(session, taskID), env); err != nil {
		t.Fatalf("publish cancel: %v", err)
	}
	deadline := time.Now().Add(waitDeadline)
	for {
		if _, err := os.Stat(marker); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("harness never received SIGTERM")
		}
		time.Sleep(20 * time.Millisecond)
	}

	reply := askDelegate(t, sock, lib.DelegateRequest{Addressee: "platform", Text: "how is the fleet?"})
	if reply.OK || !strings.Contains(reply.Message, "already ended") {
		t.Fatalf("delegate after cancel: %+v", reply)
	}
	out := waitOutcome(t, done, 45*time.Second)
	if out.err != nil || out.res.State != lib.StateCanceled {
		t.Fatalf("run: state=%q err=%v", out.res.State, out.err)
	}
	for _, ev := range replayEvents(t, url, session, taskID) {
		if ev.Kind != lib.KindArtifactUpdate {
			continue
		}
		var u lib.ArtifactUpdate
		if err := json.Unmarshal(ev.Payload, &u); err != nil {
			t.Fatalf("artifact payload: %v", err)
		}
		if u.Artifact.Name == lib.ArtifactDelegate {
			t.Fatalf("a delegate artifact was published after cancel: %s", ev.Payload)
		}
	}
}
