// Package console serves the a2a console page from inside the cluster: the
// built page, the bus credential the page connects with, and a websocket
// proxy to the bus. The page and the bus then share one origin and one
// port-forward, so the page needs no password field and no CORS.
//
// The posture is a kubectl port-forward and nothing else. The server answers
// only on the Host names that port-forward produces, which is also what keeps
// the credential away from a DNS-rebinding page. This is where the console
// starts, not where it is expected to stay.
package console

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"slices"
	"strings"
	"time"
)

const (
	// ConfigPath answers the page's request for its bus credential.
	ConfigPath = "/config.json"
	// BusPath is the websocket the page dials. It is proxied to the bus.
	BusPath = "/bus"
	// HealthPath is the kubelet's probe. It answers on any Host.
	HealthPath = "/healthz"

	headerCacheControl       = "Cache-Control"
	headerContentType        = "Content-Type"
	headerFrameOptions       = "X-Frame-Options"
	headerContentTypeOptions = "X-Content-Type-Options"
	headerUpgrade            = "Upgrade"

	valueNoStore   = "no-store"
	valueJSON      = "application/json"
	valueDeny      = "DENY"
	valueNoSniff   = "nosniff"
	valueWebsocket = "websocket"

	schemeHTTP  = "http"
	schemeHTTPS = "https"

	// lineEndings are trimmed off the password file. A Secret written with
	// `kubectl create secret --from-file` keeps the file's trailing newline.
	lineEndings = "\r\n"

	hostSeparator = " or "

	// busDialTimeout bounds the TCP connect to the bus. A NetworkPolicy that
	// drops SYNs rather than rejecting them would otherwise hold the browser's
	// upgrade request for however long the platform's default dial timeout is.
	busDialTimeout = 2 * time.Second
	// busResponseHeaderTimeout bounds the wait for the bus's upgrade response
	// once the connection is up. A bus that accepts TCP but never answers the
	// handshake — a stuck process, a proxy dropping the upgrade — would
	// otherwise hang the browser's socket forever, since the default
	// transport has no such timeout. It does not apply once the upgrade
	// succeeds: the proxy hijacks the connection at that point.
	busResponseHeaderTimeout = 3 * time.Second

	// The bodies a person sees. http.Error adds the trailing newline.
	msgWrongHost     = "this console only answers on %s - port-forward to that local port"
	msgWebsocketOnly = "websocket only"
	msgBusDown       = "the bus did not answer"
	msgNoCredential  = "no console credential at %s"
	healthBody       = "ok\n"
)

// Config is everything the server needs. Every field is required.
type Config struct {
	// StaticDir holds the built page (index.html and its assets).
	StaticDir string
	// BusURL is the bus's websocket listener as an http:// URL, because the
	// proxy dials HTTP and upgrades. Its path is sent as-is.
	BusURL *url.URL
	// User is the NATS user the page connects as.
	User string
	// PasswordFile holds that user's password. It is read on every request,
	// so a rotated Secret takes effect without a restart.
	PasswordFile string
	// AllowedHosts are the Host values the server answers on, as host:port.
	AllowedHosts []string
}

func (c Config) validate() error {
	var errs []error
	if c.BusURL == nil || (c.BusURL.Scheme != schemeHTTP && c.BusURL.Scheme != schemeHTTPS) || c.BusURL.Host == "" {
		errs = append(errs, errors.New("the bus URL must be http:// or https:// with a host"))
	}
	if c.User == "" {
		errs = append(errs, errors.New("no user"))
	}
	if c.PasswordFile == "" {
		errs = append(errs, errors.New("no password file"))
	}
	if len(c.AllowedHosts) == 0 {
		errs = append(errs, errors.New("no allowed hosts"))
	}
	if slices.Contains(c.AllowedHosts, "") {
		errs = append(errs, errors.New("an allowed host is empty"))
	}
	if info, err := os.Stat(c.StaticDir); err != nil || !info.IsDir() {
		errs = append(errs, fmt.Errorf("the static dir %q is not a directory", c.StaticDir))
	}
	return errors.Join(errs...)
}

// NewHandler returns the whole server as one handler.
func NewHandler(cfg Config, log *slog.Logger) (http.Handler, error) {
	if err := cfg.validate(); err != nil {
		return nil, fmt.Errorf("console config: %w", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET "+ConfigPath, configHandler(cfg, log))
	mux.Handle("GET "+BusPath, busHandler(cfg, log))
	mux.Handle("GET /", http.FileServer(http.Dir(cfg.StaticDir)))
	return guard(cfg.AllowedHosts, mux), nil
}

// guard refuses any Host the port-forward would not produce. A page on
// another origin that rebinds its own name to 127.0.0.1 still sends its own
// name as Host, so this is what keeps /config.json out of its reach. A
// port-forward on the wrong local port is refused here too: the page would
// load, and then the bus would refuse its Origin with nothing on screen to
// say why.
func guard(allowed []string, next http.Handler) http.Handler {
	want := make([]string, len(allowed))
	for i, h := range allowed {
		want[i] = strings.ToLower(h)
	}
	wrongHost := fmt.Sprintf(msgWrongHost, strings.Join(allowed, hostSeparator))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Before the Host check: the kubelet dials the pod IP.
		if r.URL.Path == HealthPath {
			_, _ = io.WriteString(w, healthBody)
			return
		}
		if !slices.Contains(want, strings.ToLower(r.Host)) {
			http.Error(w, wrongHost, http.StatusMisdirectedRequest)
			return
		}
		w.Header().Set(headerFrameOptions, valueDeny)
		w.Header().Set(headerContentTypeOptions, valueNoSniff)
		next.ServeHTTP(w, r)
	})
}

type servedConfig struct {
	User string `json:"user"`
	Pass string `json:"pass"`
}

// configHandler hands the page its credential. Same origin as the page, so
// no CORS header is set and no other origin can read the answer.
func configHandler(cfg Config, log *slog.Logger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set(headerCacheControl, valueNoStore)
		raw, err := os.ReadFile(cfg.PasswordFile)
		pass := strings.TrimRight(string(raw), lineEndings)
		if err != nil || pass == "" {
			log.Error("console credential unreadable", "file", cfg.PasswordFile, "err", err)
			http.Error(w, fmt.Sprintf(msgNoCredential, cfg.PasswordFile), http.StatusServiceUnavailable)
			return
		}
		w.Header().Set(headerContentType, valueJSON)
		if err := json.NewEncoder(w).Encode(servedConfig{User: cfg.User, Pass: pass}); err != nil {
			log.Error("console config write failed", "err", err)
		}
	}
}

// busTransport dials and waits for the bus's upgrade response with the
// package's short timeouts, so a bus that never answers fails fast into a
// 502 instead of hanging the browser's socket. It does not bound the
// connection once the upgrade completes: ResponseHeaderTimeout stops timing
// after the response headers arrive, and the proxy hijacks the connection
// from there.
func busTransport() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.DialContext = (&net.Dialer{Timeout: busDialTimeout}).DialContext
	t.ResponseHeaderTimeout = busResponseHeaderTimeout
	return t
}

// busHandler proxies the page's websocket to the bus. The browser's Origin
// goes through unchanged, so the bus's allowed_origins checks the page and
// not the proxy. A refusal from the bus (403) is copied back as it came.
func busHandler(cfg Config, log *slog.Logger) http.Handler {
	proxy := &httputil.ReverseProxy{
		Transport: busTransport(),
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(cfg.BusURL)
			// SetURL joins the target path with /bus. The bus URL names the
			// whole target, so its own path is what goes out.
			pr.Out.URL.Path = cfg.BusURL.Path
			pr.Out.URL.RawPath = ""
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Error("bus proxy failed", "bus", cfg.BusURL.Redacted(), "err", err)
			http.Error(w, msgBusDown, http.StatusBadGateway)
		},
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.EqualFold(r.Header.Get(headerUpgrade), valueWebsocket) {
			http.Error(w, msgWebsocketOnly, http.StatusBadRequest)
			return
		}
		proxy.ServeHTTP(w, r)
	})
}
