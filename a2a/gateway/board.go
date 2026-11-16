package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nuid"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The gateway's half of the board route (lib.BoardSubject): one request, one
// answer, on a reply subject in the gateway's own namespace rather than its
// _INBOX, so the bridge's grant to answer reaches nothing else the gateway
// reads.
const (
	// boardMaxReply bounds an answer; the sender caps every list, so a
	// board is a few kilobytes.
	boardMaxReply = 256 << 10
	// errBoardNoBridge is what a request nothing answers means: the agent
	// pod's bridge is down or not yet started.
	errBoardNoBridge = "nothing answers the board route; the agent pod's bridge is not running, or has no session-kv key and never bound the route"
)

// busBoardReader reads the board over client's connection.
func busBoardReader(client *lib.Client) BoardReader {
	return func(ctx context.Context) (*lib.Board, error) {
		nc := client.Conn()
		if nc == nil {
			return nil, errors.New("board: no bus connection")
		}
		return requestBoard(ctx, nc)
	}
}

// busBoardActor sends a board-health row's Retry or Drop over client's
// connection, on the same route with the action as the request's body.
func busBoardActor(client *lib.Client) BoardActor {
	return func(ctx context.Context, a lib.BoardAction) (*lib.BoardActionResult, error) {
		nc := client.Conn()
		if nc == nil {
			return nil, errors.New("board action: no bus connection")
		}
		body, err := json.Marshal(a)
		if err != nil {
			return nil, err
		}
		data, err := boardRoundTrip(ctx, nc, body)
		if err != nil {
			return nil, err
		}
		var r lib.BoardActionResult
		if err := json.Unmarshal(data, &r); err != nil {
			return nil, fmt.Errorf("board action: unreadable answer: %w", err)
		}
		if r.Error != "" {
			return nil, fmt.Errorf("board action: the bridge could not act: %s", r.Error)
		}
		return &r, nil
	}
}

// boardRoundTrip publishes one request on the board route, body empty for a
// snapshot, and returns the answer.
func boardRoundTrip(ctx context.Context, nc *nats.Conn, body []byte) ([]byte, error) {
	reply := lib.BoardReplyPrefix + nuid.Next()
	in, err := nc.SubscribeSync(reply)
	if err != nil {
		return nil, fmt.Errorf("board: subscribe %s: %w", reply, err)
	}
	defer func() { _ = in.Unsubscribe() }()
	if err := nc.PublishRequest(lib.BoardSubject, reply, body); err != nil {
		return nil, fmt.Errorf("board: publish: %w", err)
	}
	wait := time.Until(deadlineOr(ctx))
	msg, err := in.NextMsg(wait)
	switch {
	case errors.Is(err, nats.ErrNoResponders):
		return nil, errors.New(errBoardNoBridge)
	case errors.Is(err, nats.ErrTimeout):
		return nil, fmt.Errorf("board: no answer within %s", wait.Round(time.Millisecond))
	case err != nil:
		return nil, fmt.Errorf("board: %w", err)
	}
	if len(msg.Data) > boardMaxReply {
		return nil, fmt.Errorf("board: answer is %d bytes; the limit is %d", len(msg.Data), boardMaxReply)
	}
	return msg.Data, nil
}

func requestBoard(ctx context.Context, nc *nats.Conn) (*lib.Board, error) {
	data, err := boardRoundTrip(ctx, nc, nil)
	if err != nil {
		return nil, err
	}
	b, err := decodeBoard(data)
	if err != nil {
		return nil, err
	}
	if b.Error != "" {
		return nil, fmt.Errorf("board: the bridge could not read it: %s", b.Error)
	}
	return &b, nil
}

// decodeBoard reads a board answer. The dial section is decoded on its own,
// so one this gateway cannot read leaves the rest of the board drawn, with no
// dial panel, rather than taking the whole dashboard with it.
func decodeBoard(data []byte) (*lib.Board, error) {
	var wire struct {
		lib.Board
		Dial json.RawMessage `json:"dial"`
	}
	if err := json.Unmarshal(data, &wire); err != nil {
		return nil, fmt.Errorf("board: unreadable answer: %w", err)
	}
	b := wire.Board
	if len(wire.Dial) > 0 && string(wire.Dial) != "null" {
		var d lib.BoardDial
		if err := json.Unmarshal(wire.Dial, &d); err != nil {
			slog.Warn("board: the dial section is unreadable; drawing the board without it", "err", err)
		} else {
			b.Dial = &d
		}
	}
	return &b, nil
}

// deadlineOr is ctx's deadline, or the Home tab's read bound when it has
// none.
func deadlineOr(ctx context.Context) time.Time {
	if d, ok := ctx.Deadline(); ok {
		return d
	}
	return time.Now().Add(slackHomeBoardTimeout)
}
