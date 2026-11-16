package hermesbridge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The board route's answering half (lib.BoardSubject): the gateway asks for
// the agent pod's board snapshot when a member opens the Slack Home tab, and
// the bridge, the one bus participant in this pod holding session-kv's
// bearer, answers with GET /v1/board (agents/platform/scripts/board_snapshot.py).
// A request with a body is a board-health row's Retry or Drop instead
// (lib.BoardAction), relayed to POST /v1/board/actions, which unblocks or
// archives only cards still blocked. Only the gateway may publish on the
// subject, and it has decided the presser may act. Nothing here starts a turn.
const (
	// boardPath is the snapshot endpoint under the session-kv server.
	boardPath = "/v1/board"
	// boardTimeout bounds the GET, inside the gateway's own five-second wait.
	boardTimeout = 4 * time.Second
	// boardMaxBytes bounds the answer relayed; the server caps every list.
	boardMaxBytes = 256 << 10
	// boardActionsPath is where a Retry or Drop on a board-health row goes
	// (lib.BoardAction): session-kv acts only on cards still blocked.
	boardActionsPath = "/v1/board/actions"
	// boardActionTimeout bounds the POST: session-kv answers inside the card
	// stop's 24 s bound, and the gateway waits a little longer than this.
	boardActionTimeout = 28 * time.Second
	// boardActionMaxBytes bounds the request read; a row is at most 50 ids.
	boardActionMaxBytes = 16 << 10
)

// serveBoard binds the board route, when the bridge has a session-kv to read.
// It returns the subscription to stop, or nil when nothing was bound.
func (b *Bridge) serveBoard() lib.Subscription {
	if b.cfg.RouteURL == "" || strings.TrimSpace(b.cfg.RouteKey) == "" {
		b.cfg.Logger.Info("board route not bound: no session-kv URL or key; the Slack Home tab will show no board",
			"env", RouteKeyEnv)
		return nil
	}
	sub, err := b.c.SubscribeCore(lib.BoardSubject, b.answerBoard)
	if err != nil {
		// The Home tab then says it could not read the board; tasks are
		// unaffected, so this is not a reason to stop the bridge.
		b.cfg.Logger.Warn("board route not bound; the Slack Home tab will show no board", "err", err)
		return nil
	}
	return sub
}

// answerBoard relays one snapshot. A request whose reply subject is not in
// the gateway's namespace is dropped: the grant would refuse the answer
// anyway.
func (b *Bridge) answerBoard(m *nats.Msg) {
	if !strings.HasPrefix(m.Reply, lib.BoardReplyPrefix) {
		return
	}
	if len(m.Data) > 0 {
		// A Retry or Drop can wait on Hermes for seconds; a snapshot asked
		// meanwhile is not held behind it on this subscription.
		go b.answerBoardAction(m)
		return
	}
	data, err := b.readBoard()
	if err != nil {
		b.cfg.Logger.Warn("board read failed", "err", err)
		data, _ = json.Marshal(lib.Board{Error: err.Error()})
	}
	if err := m.Respond(data); err != nil {
		b.cfg.Logger.Warn("board answer not sent", "err", err)
	}
}

func (b *Bridge) readBoard() ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), boardTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(b.cfg.RouteURL, "/")+boardPath, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+b.cfg.RouteKey)
	resp, err := b.routeClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, routeErrorTailBytes))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, boardMaxBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > boardMaxBytes {
		return nil, fmt.Errorf("board is over %d bytes", boardMaxBytes)
	}
	return raw, nil
}

// answerBoardAction relays one Retry or Drop (lib.BoardAction) to session-kv
// and answers with what it said, or a lib.BoardActionResult naming why it
// could not.
func (b *Bridge) answerBoardAction(m *nats.Msg) {
	data, err := b.postBoardAction(m.Data)
	if err != nil {
		b.cfg.Logger.Warn("board action failed", "err", err)
		data, _ = json.Marshal(lib.BoardActionResult{Error: err.Error()})
	}
	if err := m.Respond(data); err != nil {
		b.cfg.Logger.Warn("board action answer not sent", "err", err)
	}
}

// postBoardAction POSTs the request's known fields to session-kv and returns
// its answer as it came.
func (b *Bridge) postBoardAction(request []byte) ([]byte, error) {
	if len(request) > boardActionMaxBytes {
		return nil, fmt.Errorf("board action is over %d bytes", boardActionMaxBytes)
	}
	var action lib.BoardAction
	if err := json.Unmarshal(request, &action); err != nil {
		return nil, fmt.Errorf("unreadable board action: %w", err)
	}
	body, err := json.Marshal(action)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), boardActionTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		strings.TrimRight(b.cfg.RouteURL, "/")+boardActionsPath, bytes.NewReader(body))
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
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		raw, _ := io.ReadAll(io.LimitReader(resp.Body, routeErrorTailBytes))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, boardMaxBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > boardMaxBytes {
		return nil, fmt.Errorf("board action answer is over %d bytes", boardMaxBytes)
	}
	return raw, nil
}
