package workeradapter

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"
)

// DefaultGatewayProxyStreamIdle is how long a generation answer may send
// nothing before the proxy breaks the connection. It matches the bound the
// install's agents put on a stream with no chunk (stale_timeout_seconds,
// 120s, which the chart's LiteLLM fallback settings are held under). The
// harness's own client has no such bound once headers arrive: its model
// library times out only the wait for headers.
const DefaultGatewayProxyStreamIdle = 120 * time.Second

// GatewayProxyOptions are the gateway proxy's optional settings.
type GatewayProxyOptions struct {
	// StreamIdle bounds the silence in a 2xx generation answer, from its
	// headers on: no bytes for this long and the proxy breaks the
	// connection, as it does for an answer cut short, so the harness's
	// turn fails rather than waiting out the task deadline. Zero means
	// DefaultGatewayProxyStreamIdle.
	StreamIdle time.Duration
}

// cancelKey carries the outbound call's cancel func from the request handler
// to ModifyResponse.
type cancelKey struct{}

// idleBody passes the body through and cancels the call when no bytes
// arrive for d. The cancel ends the transport's read with an error, so
// ReverseProxy aborts the response to the harness.
type idleBody struct {
	r    io.ReadCloser
	d    time.Duration
	t    *time.Timer
	once sync.Once
}

func (b *idleBody) Read(p []byte) (int, error) {
	n, err := b.r.Read(p)
	if n > 0 {
		b.t.Reset(b.d)
	}
	return n, err
}

func (b *idleBody) Close() error {
	b.once.Do(func() { b.t.Stop() })
	return b.r.Close()
}

// watchIdle wraps a 2xx generation answer's body with the idle bound.
func watchIdle(resp *http.Response, d time.Duration, log *slog.Logger) {
	if resp.StatusCode < 200 || resp.StatusCode > 299 || strings.HasSuffix(resp.Request.URL.Path, ":countTokens") {
		return
	}
	cancel, _ := resp.Request.Context().Value(cancelKey{}).(context.CancelFunc)
	if cancel == nil {
		return
	}
	path := resp.Request.URL.Path
	resp.Body = &idleBody{r: resp.Body, d: d, t: time.AfterFunc(d, func() {
		log.Warn("gateway proxy: the model's answer sent nothing for too long; breaking the connection", "path", path, "idle", d.String())
		cancel()
	})}
}
