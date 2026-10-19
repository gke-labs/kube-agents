package hermesbridge

import (
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
// Read-only on both sides; nothing here starts a turn.
const (
	// boardPath is the snapshot endpoint under the session-kv server.
	boardPath = "/v1/board"
	// boardTimeout bounds the GET, inside the gateway's own five-second wait.
	boardTimeout = 4 * time.Second
	// boardMaxBytes bounds the answer relayed; the server caps every list.
	boardMaxBytes = 256 << 10
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
