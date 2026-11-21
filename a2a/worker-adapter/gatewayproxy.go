package workeradapter

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"
)

// The gateway proxy (#6714) carries a harness's model calls from loopback to
// the install's inference gateway. The Antigravity CLI accepts a plain-http
// gateway only on loopback, and in gateway mode it sends no credential, so
// the adapter stands a small reverse proxy on 127.0.0.1 between them. It
// forwards only the Gemini-format model calls the harness makes, to the one
// gateway it was given: it is not an open proxy, and nothing it logs comes
// from a request or response body.
const (
	// GatewayProxyPathPrefix is the only path the proxy forwards: the
	// Gemini-format model calls (`/v1beta/models/<model>:streamGenerateContent`
	// and its siblings).
	GatewayProxyPathPrefix = "/v1beta/models/"
	// gatewayProxyMaxRequestBytes bounds one model call's body. A call
	// carries the whole conversation so far, so it is generous; anything
	// larger is refused before it reaches the gateway.
	gatewayProxyMaxRequestBytes = 32 << 20
	// gatewayProxyMaxResponseBytes bounds one model call's response. A
	// streamed answer past it is cut off, and the harness sees a broken
	// stream, which it reports as a failed turn.
	gatewayProxyMaxResponseBytes = 64 << 20
	// gatewayProxyHeaderTimeout bounds how long the proxy waits for the
	// harness's request headers, and gatewayProxyResponseHeaderTimeout how
	// long it waits for the gateway's first response header. The second is
	// long because a model can think for minutes before its first byte; the
	// task deadline bounds the turn as a whole.
	gatewayProxyHeaderTimeout         = 10 * time.Second
	gatewayProxyResponseHeaderTimeout = 10 * time.Minute
	gatewayProxyDialTimeout           = 10 * time.Second
	gatewayProxyShutdownTimeout       = 5 * time.Second
)

// GatewayProxy is a running gateway proxy. URL is what the harness is told
// (`http://127.0.0.1:<port>`).
type GatewayProxy struct {
	URL string
	srv *http.Server
	ln  net.Listener
}

// StartGatewayProxy listens on an ephemeral loopback port and forwards the
// harness's model calls to gateway, an http or https URL whose path, if any,
// prefixes every forwarded path. Close stops it.
func StartGatewayProxy(gateway string, log *slog.Logger) (*GatewayProxy, error) {
	upstream, err := url.Parse(gateway)
	if err != nil {
		return nil, fmt.Errorf("gateway URL %q: %w", gateway, err)
	}
	if (upstream.Scheme != "http" && upstream.Scheme != "https") || upstream.Host == "" || upstream.User != nil || upstream.RawQuery != "" || upstream.Fragment != "" {
		return nil, fmt.Errorf("gateway URL %q: want http(s)://host[:port][/path], with no credentials, query or fragment", gateway)
	}
	if log == nil {
		log = slog.Default()
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, fmt.Errorf("gateway proxy listen: %w", err)
	}
	self := ln.Addr().String()
	transport := &http.Transport{
		Proxy:                 nil, // straight to the gateway, whatever the pod's proxy env says
		DialContext:           (&net.Dialer{Timeout: gatewayProxyDialTimeout}).DialContext,
		ResponseHeaderTimeout: gatewayProxyResponseHeaderTimeout,
		TLSHandshakeTimeout:   gatewayProxyDialTimeout,
		MaxIdleConnsPerHost:   4,
	}
	rp := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.Out.Host = upstream.Host
			// #6706: once the gateway mints a key per caller class, the
			// worker's key reaches the pod as ANTHROPIC_API_KEY, and goes
			// here as `Authorization: Bearer <key>`, which the gateway's
			// caller check reads. Only a real key: unset, empty or a
			// placeholder sends nothing, since the gateway refuses a key it
			// doesn't know and admits a keyless call. Until then the call
			// passes through as the harness sent it: in gateway mode the
			// Antigravity CLI sends no key.
		},
		Transport:     transport,
		FlushInterval: -1, // stream each chunk as it comes: the answers are SSE
		ModifyResponse: func(resp *http.Response) error {
			resp.Body = &cappedBody{r: resp.Body, left: gatewayProxyMaxResponseBytes}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			// The path names the model and nothing else; the body is never
			// logged.
			log.Warn("gateway proxy: the call to the gateway failed", "path", r.URL.Path, "err", err)
			w.WriteHeader(http.StatusBadGateway)
		},
		ErrorLog: slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	mux := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Only origin-form requests for this listener: an absolute-form
		// request (`GET http://elsewhere/...`) or another Host is someone
		// using it as a proxy, which it is not.
		if r.URL.IsAbs() || r.Host != self {
			http.Error(w, "not a proxy", http.StatusForbidden)
			return
		}
		if !strings.HasPrefix(r.URL.Path, GatewayProxyPathPrefix) || strings.Contains(r.URL.Path, "..") {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodPost && r.Method != http.MethodGet {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		if r.ContentLength > gatewayProxyMaxRequestBytes {
			http.Error(w, "request too large", http.StatusRequestEntityTooLarge)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, gatewayProxyMaxRequestBytes)
		rp.ServeHTTP(w, r)
	})
	srv := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: gatewayProxyHeaderTimeout,
		MaxHeaderBytes:    64 << 10,
		ErrorLog:          slog.NewLogLogger(log.Handler(), slog.LevelWarn),
	}
	go func() {
		if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Warn("gateway proxy stopped", "err", err)
		}
	}()
	return &GatewayProxy{URL: "http://" + self, srv: srv, ln: ln}, nil
}

// Close stops the proxy, waiting briefly for calls in flight, then cutting
// them off.
func (p *GatewayProxy) Close() error {
	ctx, cancel := context.WithTimeout(context.Background(), gatewayProxyShutdownTimeout)
	defer cancel()
	if err := p.srv.Shutdown(ctx); err != nil {
		return p.srv.Close()
	}
	return nil
}

// cappedBody reads at most left bytes, then fails, so a response past the
// cap is cut off rather than streamed without bound.
type cappedBody struct {
	r    io.ReadCloser
	left int64
}

var errGatewayResponseTooLarge = errors.New("gateway proxy: the gateway's response is over the size limit")

func (b *cappedBody) Read(p []byte) (int, error) {
	if b.left <= 0 {
		return 0, errGatewayResponseTooLarge
	}
	if int64(len(p)) > b.left {
		p = p[:b.left]
	}
	n, err := b.r.Read(p)
	b.left -= int64(n)
	return n, err
}

func (b *cappedBody) Close() error { return b.r.Close() }
