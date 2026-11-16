package hermesbridge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

	natsserver "github.com/nats-io/nats-server/v2/server"

	lib "github.com/gke-labs/kube-agents/a2a/lib"
)

// completionOfSize answers with a valid chat completion whose body is
// exactly size bytes, padding the answer text to get there.
func completionOfSize(t *testing.T, size int) func(w http.ResponseWriter, r *http.Request, c apiCall) {
	t.Helper()
	const head = `{"choices":[{"message":{"role":"assistant","content":"`
	const foot = `"},"finish_reason":"stop"}]}`
	pad := size - len(head) - len(foot)
	if pad < 0 {
		t.Fatalf("size %d is smaller than an empty completion", size)
	}
	body := head + strings.Repeat("x", pad) + foot
	return func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		w.Header().Set(apiSessionIDHeader, c.sessionID)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, body)
	}
}

// A well-formed answer one byte over apiResponseCap is refused with the
// limit named. It used to be cut at the cap without an error, fail to parse,
// and end as hermes-api-unreadable with HTTP 200 and a tail of broken JSON,
// which points a reader at Hermes or the protocol instead of at an answer
// that was too big.
func TestAPI_OversizeAnswerIsRefusedWithTheLimitNamed(t *testing.T) {
	_, url := startServer(t)
	startAPIBridge(t, url, newAPIStub(t, completionOfSize(t, apiResponseCap+1)), nil)
	c := gatewayClient(t, url)
	submitIn(t, c, "task-oversize", "ctx-oversize", "write something enormous")
	task := waitTerminal(t, c, "task-oversize")
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed: an answer over the cap must not complete truncated", task.State)
	}
	reason := terminalReason(t, task)
	want := fmt.Sprintf("reason: hermes-api-oversize - HTTP 200; session: a2a-ctx-oversize; "+
		"the response body is over the %d-byte limit", apiResponseCap)
	if !strings.HasPrefix(reason, want) {
		t.Fatalf("reason = %q, want prefix %q", reason, want)
	}
	if !strings.Contains(reason, "refused rather than truncated") {
		t.Errorf("reason does not say the answer was refused, not truncated: %q", reason)
	}
}

// atCapAnswerBytes is the cap the at-cap test runs the bridge at, two result
// chunks. The boundary is the same comparison at any cap, and the chat fold
// and the result publish scale with the answer: at apiResponseCap's 8 MiB the
// answer took about 10 s to its terminal under the race detector on an idle
// machine, and on a loaded runner it outran finalizePublishTimeout, so the
// task ended with no terminal at all. TestAPI_OversizeAnswerIsRefusedWithTheLimitNamed
// keeps the default cap, which reads and refuses but never folds or
// publishes.
const atCapAnswerBytes = 512 << 10

// fullCapEnv opts in to TestAPI_AnswerAtTheProductionCapReachesATerminal,
// and fullCapTerminalWait is its terminal budget. An answer of apiResponseCap
// bytes reached its terminal in about 10 s under the race detector on an idle
// machine, and in 58 s to 132 s with the module's packages sharing four
// starved cores; the budget is over twice the slowest.
const (
	fullCapEnv          = "HERMES_BRIDGE_TEST_FULL_CAP"
	fullCapTerminalWait = 300 * time.Second
)

// An answer of exactly apiResponseCap bytes, folded and published, reaches a
// terminal. finalizeResult publishes the result chunks and the terminal inside
// one finalizePublishTimeout window, so on a host slow enough that the chunks
// use the window, the terminal publish fails too and no terminal arrives
// until the sweep. This test observes that defect; the at-cap boundary itself
// is TestAPI_AnswerAtTheCapCompletes, at atCapAnswerBytes.
//
// It runs only with fullCapEnv set. On a loaded host the window, not the
// wait, decides: with twenty-four busy loops on eight cores (load average 57
// to 72), 8 of 8 runs had no terminal at fullCapTerminalWait. Set it on an
// idle host, or to watch the defect.
func TestAPI_AnswerAtTheProductionCapReachesATerminal(t *testing.T) {
	if os.Getenv(fullCapEnv) == "" {
		t.Skipf("set %s to run an answer of apiResponseCap bytes to its terminal", fullCapEnv)
	}
	_, url := startServer(t)
	startAPIBridge(t, url, newAPIStub(t, completionOfSize(t, apiResponseCap)), nil)
	c := gatewayClient(t, url)
	submitIn(t, c, "task-fullcap", "ctx-fullcap", "write something enormous")
	task := waitTerminalFor(t, c, "task-fullcap", fullCapTerminalWait)
	if task.State != lib.StateCompleted {
		t.Fatalf("state = %s (%v), want completed", task.State, task.FinalMessage)
	}
}

// resultPublishCall is what the at-cap test's hooks record: when the
// bridge's chat fold returned, and the deadline of the context resultPublish
// was then entered with.
type resultPublishCall struct {
	foldedAt    time.Time
	deadline    time.Time
	hasDeadline bool
}

// The boundary's other side: an answer of exactly the cap is read whole and
// completes, so the one-byte look-ahead refuses nothing the cap admits. The
// bridge runs at atCapAnswerBytes here, not apiResponseCap.
//
// At the production cap the chat fold costs enough to matter, so the test
// also checks where finalizeResult runs it: before the publish window opens,
// not inside it. The chatPart hook records when the fold returned,
// the resultPublish hook the deadline of the context it was entered with,
// and the window opened finalizePublishTimeout before that deadline. Both
// run on the finalizer's goroutine, in that order, so the fold's time rides
// the publish's record. context.WithTimeout takes its deadline from
// time.Now, which keeps the monotonic reading, so the comparison is exact in
// either test mode and under any load.
func TestAPI_AnswerAtTheCapCompletes(t *testing.T) {
	_, url := startServer(t)
	published := make(chan resultPublishCall, 1)
	startAPIBridgeHooked(t, url, newAPIStub(t, completionOfSize(t, atCapAnswerBytes)), nil, func(b *Bridge) {
		b.responseCap = atCapAnswerBytes
		var foldedAt time.Time
		fold := b.chatPart
		b.chatPart = func(answer string) (lib.Part, bool) {
			part, ok := fold(answer)
			foldedAt = time.Now()
			return part, ok
		}
		publish := b.resultPublish
		b.resultPublish = func(ctx context.Context, run *taskRun, output string, chat []byte) error {
			deadline, ok := ctx.Deadline()
			select {
			case published <- resultPublishCall{foldedAt, deadline, ok}:
			default:
			}
			return publish(ctx, run, output, chat)
		}
	}, func(net.Addr) bool { return true })
	c := gatewayClient(t, url)
	submitIn(t, c, "task-atcap", "ctx-atcap", "write something large")
	task := waitTerminalFor(t, c, "task-atcap", 3*terminalWait)
	if task.State != lib.StateCompleted {
		reason := ""
		if task.FinalMessage != nil && len(task.FinalMessage.Parts) > 0 {
			reason = task.FinalMessage.Parts[0].Text
		}
		t.Fatalf("state = %s (%s), want completed", task.State, reason)
	}
	var call resultPublishCall
	select {
	case call = <-published:
	default:
		t.Fatal("the result was not published through resultPublish")
	}
	if !call.hasDeadline {
		t.Fatal("resultPublish got a context with no deadline; finalizeResult publishes under finalizePublishTimeout")
	}
	if call.foldedAt.IsZero() {
		t.Fatal("the answer was not folded through chatPart before resultPublish")
	}
	opened := call.deadline.Add(-finalizePublishTimeout)
	if opened.Before(call.foldedAt) {
		t.Errorf("the publish window opened %v before the chat fold returned: the fold ran inside the window",
			call.foldedAt.Sub(opened))
	} else {
		t.Logf("the chat fold returned %v before the publish window opened", opened.Sub(call.foldedAt))
	}
}

// A result artifact the bus refuses carries the refusal into the terminal.
// The test server's max payload is pubFailMaxPayload and the chunk size twice
// that, so the first chunk's envelope is over it, the case lib's publish gate
// names. The terminal used to read the bare `bus-publish-failed at result`,
// with the cause only in the bridge log.
//
// The limit is the server's, not the bus default of 1 MiB: an answer past
// 1 MiB spent 40 s in the chat fold before its first publish on a loaded
// runner, and the refusal is the same comparison at any limit.
func TestResultPublishFailureCarriesTheCause(t *testing.T) {
	const pubFailMaxPayload = 64 << 10
	const chunk = 2 * pubFailMaxPayload
	_, url := startServerOpt(t, true, func(o *natsserver.Options) { o.MaxPayload = pubFailMaxPayload })
	stub := newAPIStub(t, func(w http.ResponseWriter, _ *http.Request, c apiCall) {
		writeCompletion(w, c.sessionID, strings.Repeat("y", chunk-1))
	})
	startAPIBridge(t, url, stub, func(cfg *Config) { cfg.ResultChunkSize = chunk })
	c := gatewayClient(t, url)
	submitIn(t, c, "task-pubfail", "ctx-pubfail", "answer at length")
	task := waitTerminal(t, c, "task-pubfail")
	if task.State != lib.StateFailed {
		t.Fatalf("state = %s, want failed", task.State)
	}
	reason := terminalReason(t, task)
	const want = "reason: bus-publish-failed at result - "
	if !strings.HasPrefix(reason, want) {
		t.Fatalf("reason = %q, want prefix %q", reason, want)
	}
	if !strings.Contains(reason, fmt.Sprintf("bus max message size is %d", pubFailMaxPayload)) {
		t.Errorf("reason does not carry the bus's refusal: %q", reason)
	}
}

// The quoted cause is bounded: a publish error of any length leaves a
// terminal of the token plus at most publishErrTailBytes, cut on a rune
// boundary, ending in the cause.
func TestResultPublishFailedReasonIsBounded(t *testing.T) {
	const prefix = "reason: bus-publish-failed at result - "
	if got, want := resultPublishFailedReason(errors.New("nats: timeout")), prefix+"nats: timeout"; got != want {
		t.Errorf("short error: got %q, want %q", got, want)
	}
	long := errors.New(strings.Repeat("é", publishErrTailBytes) + "the cause")
	got := resultPublishFailedReason(long)
	if !strings.HasPrefix(got, prefix) {
		t.Fatalf("lost the token: %q", got[:len(prefix)])
	}
	if quoted := len(got) - len(prefix); quoted > publishErrTailBytes {
		t.Errorf("quoted %d bytes of the error, want at most %d", quoted, publishErrTailBytes)
	}
	if !strings.HasSuffix(got, "the cause") {
		t.Errorf("the tail dropped the cause: ...%q", got[len(got)-32:])
	}
	if !utf8.ValidString(got) {
		t.Error("the reason is not valid UTF-8")
	}
}
