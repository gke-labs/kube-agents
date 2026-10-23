package authcallout

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

const (
	// reuseGrantTTL is the grant lifetime the reconnect test runs under, and
	// reuseIdleSpan how long its pod sits idle: long enough to cross the
	// expiry twice, jitter included.
	reuseGrantTTL = 3 * time.Second
	reuseIdleSpan = 8 * time.Second
	// reuseMinReauths is the pod's two connections, each re-authenticating
	// at least once inside reuseIdleSpan.
	reuseMinReauths = 2
	// reuseAttempts bounds how many turns the reconnect test sends looking
	// for one that completes. Under a three-second grant every party in the
	// fixture (the verifier and the gateway too) reconnects every few
	// seconds, so a turn whose capability check lands in one of those
	// reconnects could be rejected as "the verifier could not be reached".
	// The capability client now asks again for its reach window
	// (capability.DefaultReachWindow), which rides out one such reconnect;
	// the attempts and the gap below predate it and stay as margin. That is
	// the fixture's churn, not the pod's: what the test needs from every
	// turn is that the pod took it. So it does not show that a turn landing
	// inside the pod's own reconnect is never rejected; that is for the live
	// run past the real hour.
	reuseAttempts = 5
	// reuseRetryGap spaces those turns past one reconnect of the fixture's
	// verifier. Its connection takes nats.go's default reconnect wait before
	// it is back, and the pod takes a turn within milliseconds of the
	// submission, so turns sent back to back all land inside the same gap and
	// are all rejected (seen under -race: five rejections in 150ms, every one
	// inside one verifier outage). A gap longer than the wait puts each turn
	// in a different part of the verifier's connect cycle.
	reuseRetryGap = nats.DefaultReconnectWait + time.Second
	// reuseRunTimeout bounds each whole test.
	reuseRunTimeout = 120 * time.Second
	// verifierUnreachable is the rejection reason a capability check gives
	// when no verifier answered.
	verifierUnreachable = "the verifier could not be reached"
)

// reusedPod runs the real adapter as a reused session pod under podA's
// session grants and returns its log and a stop that sends the idle SIGTERM
// and checks the clean exit.
func reusedPod(t *testing.T, ctx context.Context, h *harness, firstTask string, firstSeq uint64) (*safeBuffer, func()) {
	t.Helper()
	logs := &safeBuffer{}
	runCtx, stop := context.WithCancel(ctx)
	type outcome struct {
		res workeradapter.Result
		err error
	}
	done := make(chan outcome, 1)
	go func() {
		res, err := workeradapter.Run(runCtx, workeradapter.Config{
			NATSURL:      h.url,
			BusTokenFile: tokenFile(t, tokenPodA),
			PodName:      podA,
			TaskID:       firstTask,
			Profile:      "chat",
			Session:      podA,
			Scope:        adapterScope,
			OriginSeq:    firstSeq,
			SessionReuse: true,
			HarnessCommand: harnessStub(t, `
echo '{"type":"system","subtype":"init","session_id":"sess-reuse"}'
read first || exit 1
echo '{"type":"result","subtype":"success","result":"answered"}'
`),
			HarnessEnv:   os.Environ(),
			TaskDeadline: 30 * time.Second,
			KillGrace:    time.Second,
			Logger:       slog.New(slog.NewTextHandler(logs, nil)),
		})
		done <- outcome{res, err}
	}()
	return logs, func() {
		t.Helper()
		stop()
		select {
		case out := <-done:
			if out.err != nil || out.res != (workeradapter.Result{}) {
				t.Errorf("SIGTERM while idle: %+v err=%v, want a zero Result", out.res, out.err)
			}
		case <-time.After(30 * time.Second):
			t.Fatal("the adapter did not exit on SIGTERM while idle")
		}
	}
}

// A reused session pod takes its second task under exactly the grants it was
// minted at connect: the watcher it waits on is the origin consumer name with
// the `a2a.tasks.<pod>.*.in` filter the grant pins, and nothing about a
// second task asks for more. The server answers a refused CREATE with
// silence, so a watcher outside its grant shows up here as a second task that
// never completes; the log line the adapter writes on a permission violation
// is asserted absent too.
func TestAReusedSessionPodTakesItsNextTaskUnderItsOwnGrants(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), reuseRunTimeout)
	defer cancel()
	provisionTasksStream(t, h)
	provisionCapBucket(t, h)
	startCapabilityVerifier(t, ctx, h)

	ref1 := mintAs(t, h, "task-reuse-1", podA, adapterScope)
	seq1 := submitAs(t, h, podA, "task-reuse-1", "first turn", &ref1)
	logs, stop := reusedPod(t, ctx, h, "task-reuse-1", seq1)

	if st, why := waitFinal(t, h, podA, "task-reuse-1"); st != lib.StateCompleted {
		t.Fatalf("task-reuse-1 ended %s: %s", st, why)
	}
	ref2 := mintAs(t, h, "task-reuse-2", podA, adapterScope)
	submitAs(t, h, podA, "task-reuse-2", "second turn", &ref2)
	if st, why := waitFinal(t, h, podA, "task-reuse-2"); st != lib.StateCompleted {
		t.Fatalf("task-reuse-2 ended %s: %s", st, why)
	}
	stop()
	if strings.Contains(logs.String(), "the bus refused this session") {
		t.Errorf("the bus refused something the reused pod asked for:\n%s", logs.String())
	}
}

// A long-lived pod outlives the JWT its connections were issued: the server
// closes each at the grant TTL (an hour less jitter in an install) and the
// client re-authenticates with the pod's token, re-read from its file. The
// grant TTL is cut to seconds and the pod left idle past it twice; it must
// still take the next turn, and run one through.
func TestAReusedSessionPodTakesTurnsAcrossItsGrantExpiry(t *testing.T) {
	var reviews atomic.Int64
	h := startHarness(t, sessionMap, sessionTokens(), withGrantTTL(reuseGrantTTL),
		watchingTokenReviews(func(token string) func() {
			if token == tokenPodA {
				reviews.Add(1)
			}
			return func() {}
		}))
	ctx, cancel := context.WithTimeout(context.Background(), reuseRunTimeout)
	defer cancel()
	provisionTasksStream(t, h)
	provisionCapBucket(t, h)
	startCapabilityVerifier(t, ctx, h)

	ref1 := mintAs(t, h, "task-expiry-1", podA, adapterScope)
	seq1 := submitAs(t, h, podA, "task-expiry-1", "first turn", &ref1)
	logs, stop := reusedPod(t, ctx, h, "task-expiry-1", seq1)
	if os.Getenv("DEBUG_REUSE") != "" {
		defer func() { t.Log(logs.String()) }()
	}
	waitFinal(t, h, podA, "task-expiry-1")

	before := reviews.Load()
	time.Sleep(reuseIdleSpan)
	// Each re-authentication is one TokenReview of the pod's own token, which
	// no other party in the fixture presents. The pod's two connections
	// crossing the expiry at least once each is what makes this test about
	// reconnecting rather than about a connection that happened to last.
	if got := reviews.Load() - before; got < reuseMinReauths {
		t.Fatalf("%d re-authentications while idle past the grant TTL, want at least %d: the reconnect never happened", got, reuseMinReauths)
	}

	completed := false
	for i := 2; i <= reuseAttempts+1 && !completed; i++ {
		task := fmt.Sprintf("task-expiry-%d", i)
		ref := mintAs(t, h, task, podA, adapterScope)
		submitAs(t, h, podA, task, "a turn after the expiry", &ref)
		st, why := waitFinal(t, h, podA, task)
		switch {
		case st == lib.StateCompleted:
			completed = true
		case st == lib.StateRejected && strings.Contains(why, verifierUnreachable):
			t.Logf("%s rejected during a fixture reconnect; sending another in %s", task, reuseRetryGap)
			time.Sleep(reuseRetryGap)
		default:
			t.Fatalf("%s ended %s: %s", task, st, why)
		}
	}
	if !completed {
		t.Fatalf("no turn completed in %d attempts after the grant expiry", reuseAttempts)
	}
	stop()
	if strings.Contains(logs.String(), "the bus refused this session") {
		t.Errorf("the bus refused something the reused pod asked for:\n%s", logs.String())
	}
}

// waitFinal polls the task's events, as the gateway, until a final is among
// them, and returns its state and status text.
func waitFinal(t *testing.T, h *harness, addressee, taskID string) (lib.TaskState, string) {
	t.Helper()
	deadline := time.Now().Add(60 * time.Second)
	for time.Now().Before(deadline) {
		for _, e := range readEvents(t, h, addressee, taskID) {
			if e.Kind != lib.KindStatusUpdate {
				continue
			}
			var s lib.StatusUpdate
			if err := json.Unmarshal(e.Payload, &s); err != nil || !s.Final {
				continue
			}
			why := ""
			if s.Status.Message != nil && len(s.Status.Message.Parts) > 0 {
				why = s.Status.Message.Parts[0].Text
			}
			return s.Status.State, why
		}
		time.Sleep(200 * time.Millisecond)
	}
	t.Fatalf("%s never reached a final under the session's grants", taskID)
	return "", ""
}
