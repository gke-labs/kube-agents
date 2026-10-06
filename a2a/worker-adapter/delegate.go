package workeradapter

import (
	"encoding/json"
	"log/slog"
	"net"
	"os"
	"strings"
	"time"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The listener's budget has to fit inside the MCP client's
// delegateExchangeTimeout (15s), or the client reports a failure for a
// request the adapter goes on to publish: up to delegateHandoffWait for
// supervise to take the ask, then delegatePublishTimeout for the artifact,
// and the connection deadline covers both plus a second for the reply.
const (
	delegateHandoffWait    = 10 * time.Second
	delegatePublishTimeout = 3 * time.Second
	delegateConnDeadline   = delegateHandoffWait + delegatePublishTimeout + time.Second
)

// delegateAsk is one request handed from the listener to supervise, which
// answers it exactly once on reply.
type delegateAsk struct {
	req   lib.DelegateRequest
	reply chan delegateReply
}

// startDelegateListener owns the socket the MCP subcommand dials. An empty
// path means the tool is not served: nil channel, no-op stop. Each accepted
// connection is one ask, answered once by supervise.
func startDelegateListener(path string, log *slog.Logger) (<-chan delegateAsk, func(), error) {
	if path == "" {
		return nil, func() {}, nil
	}
	if log == nil {
		log = slog.Default()
	}
	_ = os.Remove(path)
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, nil, err
	}
	_ = os.Chmod(path, 0o600)
	ch := make(chan delegateAsk)
	done := make(chan struct{})
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go serveDelegateConn(c, ch, done, log)
		}
	}()
	stop := func() {
		close(done)
		_ = ln.Close()
		_ = os.Remove(path)
	}
	return ch, stop, nil
}

func serveDelegateConn(c net.Conn, ch chan<- delegateAsk, done <-chan struct{}, log *slog.Logger) {
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(delegateConnDeadline))
	enc := json.NewEncoder(c)
	var req lib.DelegateRequest
	if err := json.NewDecoder(c).Decode(&req); err != nil {
		log.Warn("delegate: unreadable request", "err", err)
		_ = enc.Encode(delegateReply{Message: "unreadable request"})
		return
	}
	ask := delegateAsk{req: req, reply: make(chan delegateReply, 1)}
	handoff := time.NewTimer(delegateHandoffWait)
	defer handoff.Stop()
	select {
	case ch <- ask:
	case <-done:
		_ = enc.Encode(delegateReply{Message: "the turn has ended"})
		return
	case <-handoff.C:
		_ = enc.Encode(delegateReply{Message: "the turn is not accepting requests"})
		return
	}
	// supervise answers every ask it takes, inside delegatePublishTimeout.
	_ = enc.Encode(<-ask.reply)
}

// validateDelegate is the adapter's share of the check; the gateway's is the
// one that matters (allowlist, target, depth), this one keeps junk off the bus.
func validateDelegate(req lib.DelegateRequest) string {
	switch {
	case strings.TrimSpace(req.Addressee) == "":
		return "addressee is required"
	case strings.TrimSpace(req.Text) == "":
		return "text is required"
	case len(req.Text) > lib.DelegateTextCap:
		return "text is too long for a delegation"
	}
	return ""
}
