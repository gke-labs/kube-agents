package workeradapter

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// A warm session pod (Config.Warm): started with no task, it reports ready
// only once its task watcher's consumer exists, runs the first task addressed
// to it through the same capability check every turn takes, and then serves
// its conversation as a reused pod.

func warmConfig(url, session string, harness []string, statusAddr string) Config {
	cfg := reuseConfig(url, "", session, harness)
	cfg.Warm = true
	cfg.WarmStatusAddr = statusAddr
	return cfg
}

// freeLoopbackAddr is a loopback address nothing is listening on.
func freeLoopbackAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	_ = ln.Close()
	return addr
}

// warmReadyNow asks the warm status endpoint once.
func warmReadyNow(addr string) bool {
	client := &http.Client{Timeout: time.Second}
	resp, err := client.Get("http://" + addr + lib.SessionWarmReadyPath)
	if err != nil {
		return false
	}
	_ = resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

func waitWarmReady(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(waitDeadline)
	for !warmReadyNow(addr) {
		if time.Now().After(deadline) {
			t.Fatal("the warm pod never reported ready")
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// The pod is ready only once the consumer it will take its first task from
// exists, and the first task it is sent runs (on the gateway's minted
// capability, the same as any turn), followed by the conversation's next
// turn in the same harness session. A SIGTERM while idle ends it with nothing
// published.
func TestAWarmPodIsReadyOnceItsWatcherIsLiveAndThenServesItsConversation(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-otter-w4rm"
	dir := t.TempDir()
	ready := freeLoopbackAddr(t)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, warmConfig(url, session, recordingStub(t, dir, ""), ready))

	waitWarmReady(t, ready)
	ictx, icancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer icancel()
	if _, err := c.JetStream().Consumer(ictx, lib.TasksStream, lib.SessionConsumerName(session, lib.SessionConsumerOrigin)); err != nil {
		t.Fatalf("the warm pod reported ready before its watcher's consumer existed: %v", err)
	}
	if runCount(dir) != 0 {
		t.Fatal("a harness ran before any task was sent")
	}

	submit(t, c, session, "task-w-1", "first question")
	waitState(t, c, session, "task-w-1", lib.StateCompleted)
	submit(t, c, session, "task-w-2", "second question")
	waitState(t, c, session, "task-w-2", lib.StateCompleted)

	if p := readRun(t, dir, "prompt", 1); !strings.Contains(p, "first question") {
		t.Errorf("the first run's prompt lacks its question: %q", p)
	}
	if a := readRun(t, dir, "argv", 2); !strings.HasSuffix(strings.TrimSpace(a), harnessResumeFlag+" sess-1") {
		t.Errorf("the second turn did not resume the first run's session: %q", a)
	}
	// Claimed and serving, the pod still answers its probe: readiness is the
	// adapter's own state, which nothing a turn writes can take away.
	if !warmReadyNow(ready) {
		t.Error("a claimed warm pod stopped answering its readiness probe")
	}
	cancel()
	out := waitOutcome(t, done, 30*time.Second)
	if out.err != nil || out.res != (Result{}) {
		t.Fatalf("SIGTERM while idle: %+v err=%v, want a zero Result and no error", out.res, out.err)
	}
	for _, task := range []string{"task-w-1", "task-w-2"} {
		if n := finals(t, url, session, task); n != 1 {
			t.Errorf("%s has %d finals, want exactly 1", task, n)
		}
	}
}

// A task sent to a warm pod with no capability naming it is refused before
// any harness starts, as on a freshly spawned pod: being warm buys the pod
// nothing a capability does not.
func TestAWarmPodRefusesATaskWithNoCapabilityMintedForIt(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session = "chat-heron-w4rn"
	dir := t.TempDir()
	ready := freeLoopbackAddr(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, warmConfig(url, session, recordingStub(t, dir, ""), ready))
	waitWarmReady(t, ready)

	for _, tc := range []struct {
		taskID    string
		authority json.RawMessage
	}{
		{"task-w-null", json.RawMessage(`{"grants":null}`)},
		{"task-w-other", authorityFor(t, mintFor(t, c, "task-w-other", "chat-vole-somebody-else"))},
	} {
		submitWithAuthority(t, c, session, tc.taskID, "do the thing", tc.authority)
		waitState(t, c, session, tc.taskID, lib.StateRejected)
		if got := finalText(foldTask(t, c, session, tc.taskID)); !strings.HasPrefix(got, "reason: capability-refused") {
			t.Errorf("%s ended %q, want a capability refusal", tc.taskID, got)
		}
	}
	cancel()
	waitOutcome(t, done, 30*time.Second)
	if n := runCount(dir); n != 0 {
		t.Fatalf("a harness ran %d time(s) for a task without the pod's capability", n)
	}
}

// Nobody claims the pod within its idle bound: it exits with nothing
// published and a zero Result, which is what frees the slot under a gateway
// that does not know warm pods (its sweep deletes the exited pod).
func TestAnUnclaimedWarmPodExitsAtItsIdleBound(t *testing.T) {
	url := startServer(t)
	cfg := warmConfig(url, "chat-lynx-w4ro", noHarness, freeLoopbackAddr(t))
	cfg.WarmIdle = 2 * time.Second
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.err != nil || out.res != (Result{}) {
		t.Fatalf("idle exit: %+v err=%v, want a zero Result and no error", out.res, out.err)
	}
}

func TestAWarmPodNeedsReuseAndNoTask(t *testing.T) {
	cfg := Config{Warm: true, Session: "s", Profile: "chat"}
	if err := cfg.validate(); err == nil {
		t.Error("a warm pod without reuse validated")
	}
	cfg.SessionReuse = true
	cfg.TaskID = "task-1"
	if err := cfg.validate(); err == nil {
		t.Error("a warm pod with a TASK_ID validated")
	}
	cfg.TaskID = ""
	if err := cfg.validate(); err != nil {
		t.Errorf("a warm reused session pod with no task: %v", err)
	}
}
