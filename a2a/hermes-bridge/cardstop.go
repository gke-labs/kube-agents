package hermesbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The card stop (#3263): `stop` in a gateway conversation cancels the front
// door's turn, and by then that turn has usually ended; what still runs is
// the kanban cards it filed, which the gateway cannot see or reach. So the
// gateway sends a control task, a submission whose message is the cardStop
// data part alone (lib.CardStopRequested), and the bridge answers it with no
// Hermes turn: it asks the pod's session-kv server to archive every open card
// in the conversation (POST /v1/sessions/{id}/cards/stop in
// agents/platform/scripts/session_kv_server.py) and reports what it stopped,
// as text and as the cardStop result part on the result artifact.
//
// The archive runs in the agent container, not here: Hermes kills a running
// card's worker only from the host that claimed it, and this sidecar has the
// hostname but not the pid namespace. The session id is the one the
// conversation's turns run under (apiSessionID), derived from the envelope's
// contextId, never from anything a person typed; session-kv refuses a
// context id that is not the session's.
//
// It is the same session-kv server and bearer the conversation route is
// recorded with (route.go), and the same sole publisher: only the gateway
// may publish on a2a.tasks.*.*.in, and it builds the control itself.
//
// The time budget, from the control's submission to its terminal: the
// capability check (capability.DefaultTimeout, 5 s), the wait for the
// session's turn (cardStopTurnWait, 20 s), and the POST
// (DefaultCardStopTimeout, 35 s), about 60 s at worst. session-kv answers
// inside its CARD_STOP_WORST_CASE_S (24 s), so the POST's bound is that plus
// room; the gateway's wait for the control (A2A_CARD_STOP_TIMEOUT) sits
// above the whole, at 75 s. Raise them together.
//
// A cancel ends the bridge's wait, not session-kv's work: the route is a
// synchronous handler that runs to its own deadline whether or not anyone
// still reads the answer, so a cancel that lands after the POST went out
// ends the control canceled-by-request while the cards may well be
// archived. One that lands while the control is still pending (its
// capability check) ends canceled-before-start, and one during the turn wait
// ends canceled-by-request with nothing sent.
const (
	// cardStopPathFormat is the stop endpoint under Config.RouteURL.
	cardStopPathFormat = "/v1/sessions/%s/cards/stop"
	// DefaultCardStopTimeout is Config.CardStopTimeout's default, the bound
	// on the POST: session-kv's CARD_STOP_WORST_CASE_S (24 s) plus room (the
	// budget above). The daemon reads BRIDGE_CARD_STOP_TIMEOUT_SECONDS, and
	// its defaultCardStopTimeoutSeconds must equal this.
	DefaultCardStopTimeout = 35 * time.Second
	// cardStopResponseCap bounds the answer read. A conversation's cards
	// are a handful, and session-kv archives at most 200 in a pass.
	cardStopResponseCap = 1 << 20
	// cardStopListMax and cardStopTitleMax bound what the result part
	// carries, so the result stays well under the bus's message size
	// whatever the board holds. A list cut short sets listsCut, never
	// truncated, which is session-kv's word for cards left open. A title cut
	// short is cardStopTitleMax runes with its "…".
	cardStopListMax  = lib.CardStopListMax
	cardStopTitleMax = 200
	// cardStopUnavailable and cardStopUnsupported are the failed terminal's
	// reason tokens: the stop could not be made, or this executor has no
	// way to make it. The gateway falls back to its card notice on either.
	cardStopUnavailable = "card-stop-unavailable"
	cardStopUnsupported = "card-stop-unsupported"
)

// cardStopTurnWait bounds the wait for the conversation's session turn, so
// the turn the gateway cancelled just before the stop has ended, and no
// later one starts, while the cards are archived. A var so a test can
// shorten it.
var cardStopTurnWait = 20 * time.Second

// cardStopRequested reports whether a submission's payload is a card-stop
// control.
func cardStopRequested(payload json.RawMessage) bool {
	var m lib.Message
	if err := json.Unmarshal(payload, &m); err != nil {
		return false
	}
	return lib.CardStopRequested(m.Parts)
}

// cardStopRequest is the POST's body: the context id the session must be
// derived from, and the gateway's conversation key, which adds the cards
// subscribed to this session and conversation.
type cardStopRequest struct {
	ContextID    string `json:"context_id"`
	Conversation string `json:"conversation"`
}

// cardStopResponse is the subset of session-kv's answer the bridge reads.
type cardStopResponse struct {
	SessionID string `json:"session_id"`
	Stopped   []struct {
		ID          string `json:"id"`
		Title       string `json:"title"`
		PriorStatus string `json:"prior_status"`
	} `json:"stopped"`
	Settled []struct {
		ID    string `json:"id"`
		Title string `json:"title"`
	} `json:"settled"`
	Failed []struct {
		ID    string `json:"id"`
		Title string `json:"title"`
		Error string `json:"error"`
	} `json:"failed"`
	// Pending is the archives still running at session-kv's deadline;
	// Unresolved the stopped cards claimed just before the stop whose worker
	// never appeared, so a worker may still start for them; Errors the
	// board errors after its first pass.
	Pending    []cardStopCardRef `json:"pending"`
	Unresolved []cardStopCardRef `json:"unresolved"`
	Errors     []string          `json:"errors"`
	Truncated  bool              `json:"truncated"`
	Deadline   bool              `json:"deadline"`
}

// cardStopCardRef is a card session-kv names by id and title alone.
type cardStopCardRef struct {
	ID    string `json:"id"`
	Title string `json:"title"`
}

// runCardStop runs one card-stop control, on its own goroutine under b.wg
// (accept). Its life is a task's: the capability check every task passes,
// working, one terminal. Cancel and shutdown end its wait and its request
// through cancelReq, as they end an API turn's.
func (b *Bridge) runCardStop(ctx context.Context, run *taskRun) {
	defer b.wg.Done()
	if !b.capabilityPermits(ctx, run) {
		return
	}
	taskID := run.origin.TaskID
	if b.cfg.Executor != ExecutorAPI {
		b.finalize(run, lib.StateFailed, "reason: "+cardStopUnsupported+
			" - the cli executor runs no conversation session, so it has no cards to stop", nil)
		return
	}
	stopCtx, cancel := context.WithCancel(context.Background())
	defer cancel()
	run.mu.Lock()
	if run.state != statePending {
		run.mu.Unlock()
		return // canceled or shut down first; its finalize wrote the terminal
	}
	run.state = stateRunning
	run.cancelReq = cancel
	run.mu.Unlock()
	defer func() { run.mu.Lock(); run.cancelReq = nil; run.mu.Unlock() }()
	if err := b.publishWorking(ctx, run); errors.Is(err, errRunEnded) {
		return
	} else if err != nil {
		b.cfg.Logger.Error("working publish failed", "task", taskID, "err", err)
		b.finalize(run, lib.StateFailed, "reason: bus-publish-failed at working", nil)
		return
	}
	if b.cfg.RouteURL == "" {
		b.finalizeCardStopError(run, errRouteDisabled)
		return
	}
	if strings.TrimSpace(b.cfg.RouteKey) == "" {
		b.finalizeCardStopError(run, fmt.Errorf("%s is not set", RouteKeyEnv))
		return
	}

	// The turn is held across the POST: the bridge's request for the turn the
	// gateway cancelled has ended (which does not prove the Hermes run behind
	// it stopped; see a2a/docs/hermes-bridge.md), and a bridge turn queued
	// behind this one cannot file a card the stop then misses. Nothing that
	// holds a session's turn waits on this goroutine, which takes no worker
	// slot.
	sessionID := apiSessionID(run.origin.ContextID)
	waitCtx, stopWait := context.WithTimeout(stopCtx, cardStopTurnWait)
	release := b.turns.acquire(waitCtx, sessionID)
	stopWait()
	if release == nil {
		b.finalizeCardStopError(run, fmt.Errorf("the session's turn did not end within %s; no card was stopped", cardStopTurnWait))
		return
	}
	resp, err := b.postCardStop(stopCtx, sessionID, run.origin)
	release()
	if err != nil {
		b.finalizeCardStopError(run, err)
		return
	}
	result := cardStopResult(resp)
	part, err := lib.CardStopPart(lib.CardStop{CardStopResult: result})
	if err != nil {
		b.finalizeCardStopError(run, err)
		return
	}
	b.cfg.Logger.Info("cards stopped", "task", taskID, "session", sessionID, "stopped", len(resp.Stopped),
		"settled", len(resp.Settled), "failed", len(resp.Failed), "pending", len(resp.Pending),
		"unresolved", len(resp.Unresolved), "truncated", resp.Truncated, "deadline", resp.Deadline)
	for _, f := range resp.Failed {
		b.cfg.Logger.Warn("card not stopped", "task", taskID, "session", sessionID, "card", f.ID, "err", f.Error)
	}
	for _, c := range resp.Pending {
		b.cfg.Logger.Warn("card's archive still running at session-kv's deadline", "task", taskID, "session", sessionID, "card", c.ID)
	}
	for _, c := range resp.Unresolved {
		b.cfg.Logger.Warn("card stopped mid-claim; a worker may still start for it", "task", taskID, "session", sessionID, "card", c.ID)
	}
	for _, e := range resp.Errors {
		b.cfg.Logger.Warn("session-kv board error during the card stop", "task", taskID, "session", sessionID, "err", e)
	}
	// The result rides the last result chunk beside the text. A bus that
	// refuses the two together gets the text alone (publishChunks), so a
	// completed control can arrive with no cardStop part; the gateway must
	// read that as a stop whose details are lost, not as nothing stopped.
	b.finalizeData(run, lib.StateCompleted, "", cardStopText(resp), part.Data)
}

// finalizeCardStopError ends a card stop that did not get its answer: the
// cancel and the shutdown each end its request, so they are read first.
func (b *Bridge) finalizeCardStopError(run *taskRun, err error) {
	switch {
	case run.canceled.Load():
		b.finalize(run, lib.StateCanceled, "reason: canceled-by-request", nil)
	case b.closing.Load():
		b.finalize(run, lib.StateFailed, shutdownReason, nil)
	default:
		b.finalize(run, lib.StateFailed, fmt.Sprintf("reason: %s - %v", cardStopUnavailable, err), nil)
	}
}

// postCardStop asks session-kv to stop sessionID's cards and returns its
// answer. The conversation is the gateway's key from the authority block when
// it names a chat conversation (routeFor), else "": the session's own cards
// are found either way.
func (b *Bridge) postCardStop(ctx context.Context, sessionID string, origin *lib.Envelope) (*cardStopResponse, error) {
	body := cardStopRequest{ContextID: origin.ContextID}
	if route, err := routeFor(origin); err == nil {
		body.Conversation = route.Conversation
	}
	raw, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, b.cfg.CardStopTimeout)
	defer cancel()
	target := strings.TrimRight(b.cfg.RouteURL, "/") + fmt.Sprintf(cardStopPathFormat, url.PathEscape(sessionID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target, bytes.NewReader(raw))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+b.cfg.RouteKey)
	resp, err := b.routeClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, cardStopResponseCap+1))
	if err != nil {
		return nil, fmt.Errorf("reading the answer: %w", err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(tail(string(data), routeErrorTailBytes)))
	}
	if len(data) > cardStopResponseCap {
		return nil, fmt.Errorf("the answer is over %d bytes", cardStopResponseCap)
	}
	var out cardStopResponse
	if err := json.Unmarshal(data, &out); err != nil {
		return nil, fmt.Errorf("unreadable answer: %w", err)
	}
	return &out, nil
}

// cardStopResult is session-kv's answer as the result part's fields, each
// list and title bounded (cardStopListMax, cardStopTitleMax).
func cardStopResult(r *cardStopResponse) *lib.CardStopResult {
	out := &lib.CardStopResult{
		Stopped:    []lib.CardStopCard{},
		Settled:    []lib.CardStopCard{},
		Failed:     len(r.Failed),
		Pending:    len(r.Pending),
		Unresolved: len(r.Unresolved),
		Truncated:  r.Truncated,
		Deadline:   r.Deadline,
	}
	for _, c := range r.Stopped {
		if len(out.Stopped) == cardStopListMax {
			out.ListsCut = true
			break
		}
		out.Stopped = append(out.Stopped, lib.CardStopCard{ID: c.ID, Title: cardStopTitle(c.Title), PriorStatus: c.PriorStatus})
	}
	for _, c := range r.Settled {
		if len(out.Settled) == cardStopListMax {
			out.ListsCut = true
			break
		}
		out.Settled = append(out.Settled, lib.CardStopCard{ID: c.ID, Title: cardStopTitle(c.Title)})
	}
	var notStopped []cardStopCardRef
	for _, c := range r.Failed {
		notStopped = append(notStopped, cardStopCardRef{ID: c.ID, Title: c.Title})
	}
	notStopped = append(append(notStopped, r.Pending...), r.Unresolved...)
	for _, c := range notStopped {
		if len(out.NotStopped) == cardStopListMax {
			out.ListsCut = true
			break
		}
		out.NotStopped = append(out.NotStopped, lib.CardStopCard{ID: c.ID, Title: cardStopTitle(c.Title)})
	}
	return out
}

// cardStopTitle is a card's title in at most cardStopTitleMax runes, the
// "…" that marks a cut one included.
func cardStopTitle(s string) string {
	if utf8.RuneCountInString(s) <= cardStopTitleMax {
		return s
	}
	return string([]rune(s)[:cardStopTitleMax-1]) + "…"
}

// cardStopText is the result's text part, for a reader that does not know the
// data part: the counts session-kv answered, whatever the lists were cut to,
// and plainly whatever the stop could not promise. The gateway words its own
// reply from the data part.
func cardStopText(r *cardStopResponse) string {
	parts := []string{"stopped " + cards(len(r.Stopped))}
	if n := len(r.Unresolved); n > 0 {
		parts = append(parts, fmt.Sprintf("a worker may still start for %d of them, claimed just before the stop", n))
	}
	if n := len(r.Settled); n > 0 {
		parts = append(parts, fmt.Sprintf("%s had already finished", cards(n)))
	}
	if n := len(r.Failed); n > 0 {
		parts = append(parts, fmt.Sprintf("%s could not be stopped", cards(n)))
	}
	if n := len(r.Pending); n > 0 {
		parts = append(parts, fmt.Sprintf("%s still being stopped when the stop ran out of time, so they may still be running", cards(n)))
	} else if r.Deadline {
		parts = append(parts, "the stop ran out of time before it finished")
	}
	if r.Truncated {
		parts = append(parts, "the conversation had more cards than one stop takes, so some may still be open")
	}
	return strings.Join(parts, "; ")
}

// cards is n with the noun "card".
func cards(n int) string {
	if n == 1 {
		return "1 card"
	}
	return fmt.Sprintf("%d cards", n)
}
