package workeradapter

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"os"
	"path/filepath"
	"slices"
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
	// The socket's directory exists in the pod (/scratch, an emptyDir the
	// image also creates). Off-cluster it is created here, private, when
	// its parent is writable; the default under / usually is not, so a run
	// by hand sets A2A_DELEGATE_SOCKET to a writable path or turns the tool
	// off with A2A_DELEGATE_TOOL=off. A listen that still fails fails the
	// turn (Adapter.Run).
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, nil, err
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

// taskSocketSuffixBytes is the random part of a reused pod's per-turn
// delegate socket name, in bytes (twice as many hex characters).
const taskSocketSuffixBytes = 8

// taskDelegateSocket is a fresh delegate socket path for one turn of a reused
// pod: the configured socket's name with a random suffix, in the same
// directory. The socket itself is 0600 (startDelegateListener); the name keeps
// a process holding an earlier turn's path, or the configured one, from
// reaching this turn, but not one running as the pod's user that lists the
// directory.
func taskDelegateSocket(base string) (string, error) {
	suffix := make([]byte, taskSocketSuffixBytes)
	if _, err := rand.Read(suffix); err != nil {
		return "", fmt.Errorf("delegate socket name: %w", err)
	}
	ext := filepath.Ext(base)
	name := strings.TrimSuffix(filepath.Base(base), ext) + "-" + hex.EncodeToString(suffix) + ext
	return filepath.Join(filepath.Dir(base), name), nil
}

// withDelegateSocket tells one turn's harness its delegate socket: in the
// environment, and in the delegate MCP server's own env inside the
// --mcp-config argument, which is where the harness passes it on to the
// server (cmd/worker-adapter builds that argument once, with the configured
// path). An argv with no such entry (an A2A_HARNESS_CMD override) is left as
// it is, and rewrote reports whether the delegate server's entry was found.
// Neither input is modified.
func withDelegateSocket(argv, env []string, socket string) (out, outEnv []string, rewrote bool) {
	if env == nil {
		env = os.Environ()
	}
	outEnv = append(slices.Clone(env), EnvDelegateSocket+"="+socket)
	out = slices.Clone(argv)
	for i := 1; i < len(out); i++ {
		if out[i-1] != MCPConfigFlag {
			continue
		}
		var cfg map[string]map[string]map[string]any
		if err := json.Unmarshal([]byte(out[i]), &cfg); err != nil {
			continue
		}
		srv, ok := cfg[MCPServersKey][DelegateMCPServer]
		if !ok {
			continue
		}
		srvEnv, _ := srv[MCPEnvKey].(map[string]any)
		if srvEnv == nil {
			srvEnv = map[string]any{}
		}
		srvEnv[EnvDelegateSocket] = socket
		srv[MCPEnvKey] = srvEnv
		if b, err := json.Marshal(cfg); err == nil {
			out[i] = string(b)
			rewrote = true
		}
	}
	return out, outEnv, rewrote
}
