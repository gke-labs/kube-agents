package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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

func requestBoard(ctx context.Context, nc *nats.Conn) (*lib.Board, error) {
	reply := lib.BoardReplyPrefix + nuid.Next()
	in, err := nc.SubscribeSync(reply)
	if err != nil {
		return nil, fmt.Errorf("board: subscribe %s: %w", reply, err)
	}
	defer func() { _ = in.Unsubscribe() }()
	if err := nc.PublishRequest(lib.BoardSubject, reply, nil); err != nil {
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
	var b lib.Board
	if err := json.Unmarshal(msg.Data, &b); err != nil {
		return nil, fmt.Errorf("board: unreadable answer: %w", err)
	}
	if b.Error != "" {
		return nil, fmt.Errorf("board: the bridge could not read it: %s", b.Error)
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
