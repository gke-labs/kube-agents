package hermesbridge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
	"unicode/utf8"

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

// atCapTerminalWait is the at-cap test's own terminal budget. An answer of
// apiResponseCap bytes takes about 10 s to its terminal under the race
// detector on an idle machine (0.6 s to read and parse, 6.4 s in the chat
// fold scanning one 8 MiB line, 3 s publishing the result chunks; 1.3 s
// without -race), so the shared terminalWait is crossed on a busy CI runner
// at a two-fold slowdown. The task deadline bounds only the HTTP turn, and
// the fold runs before finalizeResult opens its publish window, so this
// budget is the one that decides.
const atCapTerminalWait = 60 * time.Second

// resultPublishCall is what the at-cap test's publish hook records: how
// much of the publish window was left when resultPublish was entered, and
// the answer it was handed.
type resultPublishCall struct {
	remaining   time.Duration
	hasDeadline bool
	output      string
}

// The boundary's other side: an answer of exactly apiResponseCap bytes is
// read whole and completes, so the one-byte look-ahead refuses nothing the
// cap admits.
//
// The same answer is the one whose chat fold costs enough to see, so the
// test also checks where finalizeResult runs it. The publish context
// resultPublish receives is opened just before it, so on entry its deadline
// is short of the whole finalizePublishTimeout by one nil check; with the
// fold inside the window it is short by the fold, about 0.85 s for this
// answer without the race detector and 5 s under it. The bound is half of a
// fold of the same answer timed here, after the terminal and outside the
// window, so the check is red on that order in either mode and on any host.
func TestAPI_AnswerAtTheCapCompletes(t *testing.T) {
	_, url := startServer(t)
	published := make(chan resultPublishCall, 1)
	startAPIBridgeHooked(t, url, newAPIStub(t, completionOfSize(t, apiResponseCap)), nil, func(b *Bridge) {
		publish := b.resultPublish
		b.resultPublish = func(ctx context.Context, run *taskRun, output string, chat []byte) error {
			deadline, ok := ctx.Deadline()
			select {
			case published <- resultPublishCall{time.Until(deadline), ok, output}:
			default:
			}
			return publish(ctx, run, output, chat)
		}
	}, func(net.Addr) bool { return true })
	c := gatewayClient(t, url)
	submitIn(t, c, "task-atcap", "ctx-atcap", "write something large")
	task := waitTerminalFor(t, c, "task-atcap", atCapTerminalWait)
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
	start := time.Now()
	if _, ok := resultChatPart(call.output); !ok {
		t.Fatal("the at-cap answer did not fold into a chat part")
	}
	fold := time.Since(start)
	spent := finalizePublishTimeout - call.remaining
	t.Logf("resultPublish entered %v into its %v window; a fold of its answer takes %v", spent, finalizePublishTimeout, fold)
	if spent > fold/2 {
		t.Errorf("resultPublish entered %v into its window, more than half a %v fold of its answer: the chat fold ran inside the window",
			spent, fold)
	}
}

// A result artifact the bus refuses carries the refusal into the terminal.
// The chunk size here is past the bus's 1 MiB default max payload, so the
// first chunk's envelope is over it, the case lib's publish gate names. The
// terminal used to read the bare `bus-publish-failed at result`, with the
// cause only in the bridge log.
func TestResultPublishFailureCarriesTheCause(t *testing.T) {
	const chunk = 2 << 20
	_, url := startServer(t)
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
	if !strings.Contains(reason, "bus max message size is") {
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
