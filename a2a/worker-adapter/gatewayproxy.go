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
	"regexp"
	"strings"
	"time"

	"golang.org/x/net/netutil"
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
	// gatewayProxyIdleTimeout bounds a kept-alive connection between
	// requests. There is deliberately no server ReadTimeout: once a request's
	// body is read, net/http cancels the request's context when that deadline
	// passes, which would cut off a model's answer that streams for longer.
	// A caller that drips a body is bounded instead by gatewayProxyMaxConns
	// and gatewayProxyCallTimeout.
	gatewayProxyIdleTimeout = time.Minute
	// gatewayProxyCallTimeout bounds one model call end to end, so a gateway
	// that streams forever under the response cap still ends.
	gatewayProxyCallTimeout = 30 * time.Minute
	// gatewayProxyMaxConns caps the connections the proxy holds at once, in
	// from the harness and out to the gateway alike. A harness makes one
	// call at a time; the cap is for a process on loopback that is not the
	// harness.
	gatewayProxyMaxConns = 16
)

// gatewayProxyPath is every path the proxy forwards: a model name, then one
// of the Gemini-format calls. Anything else, an encoded character, a second
// slash or a climb included, is refused.
var gatewayProxyPath = regexp.MustCompile(`^/v1beta/models/[A-Za-z0-9._-]+:(generateContent|streamGenerateContent|countTokens)$`)

// gatewayProxyCallerHeaders are request headers a caller may not set, since
// the gateway reads each as the caller's credential or attribution: a key
// the harness sent would decide which caller the gateway charges, once the
// proxy speaks for the worker (#6706). Any header starting x-litellm- is
// dropped too.
var gatewayProxyCallerHeaders = []string{"Authorization", "X-Api-Key", "Api-Key", "X-Goog-Api-Key", "Proxy-Authorization"}

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
	ln = netutil.LimitListener(ln, gatewayProxyMaxConns)
	transport := &http.Transport{
		Proxy:                 nil, // straight to the gateway, whatever the pod's proxy env says
		DialContext:           (&net.Dialer{Timeout: gatewayProxyDialTimeout}).DialContext,
		ResponseHeaderTimeout: gatewayProxyResponseHeaderTimeout,
		TLSHandshakeTimeout:   gatewayProxyDialTimeout,
		MaxIdleConnsPerHost:   4,
		MaxConnsPerHost:       gatewayProxyMaxConns,
		IdleConnTimeout:       gatewayProxyIdleTimeout,
	}
	rp := &httputil.ReverseProxy{
		Rewrite: func(r *httputil.ProxyRequest) {
			r.SetURL(upstream)
			r.Out.Host = upstream.Host
			// A key in the query is a credential to the gateway as well.
			q := r.Out.URL.Query()
			q.Del("key")
			r.Out.URL.RawQuery = q.Encode()
			for _, h := range gatewayProxyCallerHeaders {
				r.Out.Header.Del(h)
			}
			for h := range r.Out.Header {
				if strings.HasPrefix(strings.ToLower(h), "x-litellm-") {
					r.Out.Header.Del(h)
				}
			}
			// #6706: once the gateway mints a key per caller class, the
			// worker's key reaches the pod as ANTHROPIC_API_KEY, and goes
			// here as `Authorization: Bearer <key>`, which the gateway's
			// caller check reads. Only a real key: unset, empty or a
			// placeholder sends nothing, since the gateway refuses a key it
			// doesn't know and admits a keyless call. Until then the call
			// passes through as the harness sent it, less the caller headers
			// above: in gateway mode the Antigravity CLI sends no key. The
			// key is Set, never Added.
		},
		Transport:     transport,
		FlushInterval: -1, // stream each chunk as it comes: the answers are SSE
		ModifyResponse: func(resp *http.Response) error {
			resp.Body = &cappedBody{r: resp.Body, left: gatewayProxyMaxResponseBytes}
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			var tooBig *http.MaxBytesError
			if errors.As(err, &tooBig) {
				w.WriteHeader(http.StatusRequestEntityTooLarge)
				return
			}
			// The path names the model and nothing else. The transport's
			// error names the connection, not the outbound URL or its query
			// (TestAFailedGatewayCallLogsNoQuery); the body is never logged.
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
		if r.URL.RawPath != "" || !gatewayProxyPath.MatchString(r.URL.Path) {
			http.NotFound(w, r)
			return
		}
		// No upgrade: a tunnel would carry bytes past every cap here.
		if r.Header.Get("Upgrade") != "" || strings.Contains(strings.ToLower(r.Header.Get("Connection")), "upgrade") {
			http.Error(w, "upgrade not allowed", http.StatusBadRequest)
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
		ctx, cancel := context.WithTimeout(r.Context(), gatewayProxyCallTimeout)
		defer cancel()
		rp.ServeHTTP(w, r.WithContext(ctx))
	})
	srv := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: gatewayProxyHeaderTimeout,
		IdleTimeout:       gatewayProxyIdleTimeout,
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
		// Exactly at the cap is fine if the body ends there.
		var one [1]byte
		if n, err := b.r.Read(one[:]); n == 0 && errors.Is(err, io.EOF) {
			return 0, io.EOF
		}
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
