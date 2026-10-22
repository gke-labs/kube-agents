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
	"context"
	"crypto/tls"
	"crypto/x509"
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

	// pathSeparator splits a URL path into the segments hiddenSegmentPrefix
	// checks.
	pathSeparator = "/"
	// hiddenSegmentPrefix marks a path segment as a dotfile, which the static
	// server never has a reason to ship.
	hiddenSegmentPrefix = "."

	// lineEndings are trimmed off the password file. A Secret written with
	// `kubectl create secret --from-file` keeps the file's trailing newline.
	lineEndings = "\r\n"

	hostSeparator = " or "

	// busDialTimeout bounds the TCP connect to the bus, and on an https bus
	// the TLS handshake after it. A NetworkPolicy that drops SYNs rather than
	// rejecting them would otherwise hold the browser's upgrade request for
	// however long the platform's default dial timeout is.
	busDialTimeout = 2 * time.Second
	// busResponseHeaderTimeout bounds the wait for the bus's upgrade response
	// once the connection is up. A bus that accepts TCP but never answers the
	// handshake (a stuck process, a proxy dropping the upgrade) would
	// otherwise hang the browser's socket forever, since the default
	// transport has no such timeout. It does not apply once the upgrade
	// succeeds: the proxy hijacks the connection at that point.
	busResponseHeaderTimeout = 3 * time.Second

	// The bodies a person sees. http.Error adds the trailing newline.
	msgWrongHost     = "this console only answers on %s - port-forward to that local port"
	msgWebsocketOnly = "websocket only"
	msgBusDown       = "the bus did not answer"
	msgNoCA          = "the CA file %s holds no PEM certificate"
	msgBusUnverified = "the bus's certificate did not verify: %v"
	msgBusWrongName  = " - set NATS_TLS_SERVER_NAME to a name it carries"
	msgBusPlaintext  = "the bus answered without TLS - it may still be restarting onto its TLS listener"
	msgBusCAFile     = "the console could not use its bus CA (NATS_CA_FILE): %v"
	msgNoCredential  = "no console credential at %s"
	healthBody       = "ok\n"
)

// Config is everything the server needs. Every field is required, except
// CAFile for a plain http:// bus.
type Config struct {
	// StaticDir holds the built page (index.html and its assets).
	StaticDir string
	// BusURL is the bus's websocket listener as an https:// URL (http:// for
	// a plain local nats-server), because the proxy dials HTTP and upgrades.
	// Its path is sent as-is, and its host is the name the bus's certificate
	// is verified under.
	BusURL *url.URL
	// CAFile holds the PEM CA an https:// bus is verified against, and the
	// only one: the system roots never vouch for the bus. It is read on every
	// dial, so a CA bundle the kubelet updates in place takes effect without
	// a restart.
	CAFile string
	// ServerName overrides the name the bus's certificate is verified under.
	// Empty means the bus URL's host, which in a pod is the Service name the
	// certificate carries; a console run locally against a port-forwarded bus
	// dials loopback and names the Service here instead.
	ServerName string
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
	if c.BusURL != nil && c.BusURL.Scheme == schemeHTTPS {
		if c.CAFile == "" {
			errs = append(errs, errors.New("an https bus URL needs a CA file to verify the bus against"))
		} else if _, err := loadBusCA(c.CAFile); err != nil {
			errs = append(errs, err)
		}
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
	mux.Handle("GET /", staticFileHandler(cfg.StaticDir))
	return guard(cfg.AllowedHosts, mux), nil
}

// staticFileHandler wraps the built page's directory in a FileServer that
// refuses two things a plain FileServer would otherwise serve: a directory
// listing, for any path other than "/" that ends in "/" and has no
// index.html, and a dotfile, for any path with a segment starting with ".".
// Neither is ever something the built page ships on purpose.
func staticFileHandler(dir string) http.Handler {
	fs := http.FileServer(http.Dir(dir))
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" && strings.HasSuffix(r.URL.Path, pathSeparator) {
			http.NotFound(w, r)
			return
		}
		for _, seg := range strings.Split(r.URL.Path, pathSeparator) {
			if strings.HasPrefix(seg, hiddenSegmentPrefix) {
				http.NotFound(w, r)
				return
			}
		}
		fs.ServeHTTP(w, r)
	})
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
//
// An https bus is dialled with TLS verified against cfg.CAFile alone, under
// cfg.ServerName or else the bus URL's host name. HTTP/1.1 only, stated
// rather than left to the custom dial: a websocket upgrade is an HTTP/1.1
// mechanism, so h2 is switched off here as well as never offered in ALPN.
func busTransport(cfg Config) *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	dialer := &net.Dialer{Timeout: busDialTimeout}
	t.DialContext = dialer.DialContext
	t.ResponseHeaderTimeout = busResponseHeaderTimeout
	// The bus is in-cluster. An HTTP_PROXY in the pod's environment must not
	// route the websocket out through it.
	t.Proxy = nil
	if cfg.BusURL.Scheme == schemeHTTPS {
		t.ForceAttemptHTTP2 = false
		t.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
		t.DialTLSContext = func(ctx context.Context, network, addr string) (net.Conn, error) {
			roots, err := loadBusCA(cfg.CAFile)
			if err != nil {
				return nil, err
			}
			name := cfg.ServerName
			if name == "" {
				name = cfg.BusURL.Hostname()
			}
			d := &tls.Dialer{NetDialer: dialer, Config: &tls.Config{
				RootCAs:    roots,
				ServerName: name,
				MinVersion: tls.VersionTLS12,
			}}
			return d.DialContext(ctx, network, addr)
		}
	}
	return t
}

// loadBusCA reads the PEM CA the bus is verified against.
func loadBusCA(path string) (*x509.CertPool, error) {
	pem, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("%w: %w", errBusCA, err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(pem) {
		return nil, fmt.Errorf("%w: "+msgNoCA, errBusCA, path)
	}
	return pool, nil
}

// errBusCA marks a failure to load the bus CA file, the console's own mount,
// so the page is not told the bus failed when the bus was never dialled.
var errBusCA = errors.New("the bus CA")

// busHandler proxies the page's websocket to the bus. The browser's Origin
// goes through unchanged, so the bus's allowed_origins checks the page and
// not the proxy. A refusal from the bus (403) is copied back as it came.
func busHandler(cfg Config, log *slog.Logger) http.Handler {
	proxy := &httputil.ReverseProxy{
		Transport: busTransport(cfg),
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(cfg.BusURL)
			// SetURL joins the target path with /bus. The bus URL names the
			// whole target, so its own path is what goes out.
			pr.Out.URL.Path = cfg.BusURL.Path
			pr.Out.URL.RawPath = ""
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Error("bus proxy failed", "bus", cfg.BusURL.Redacted(), "err", err)
			// The person reading the page is told which side failed, not
			// handed one sentence per guessed cause: the console's own CA
			// file, a bus that answered in plaintext, a certificate that did
			// not verify (with Go's own reason, which already says unknown
			// authority, expired, or the wrong name), or no answer at all.
			// Only the wrong name gets a remedy added, because its fix is a
			// setting rather than the certificate.
			var verify *tls.CertificateVerificationError
			var plaintext tls.RecordHeaderError
			switch {
			case errors.Is(err, errBusCA):
				http.Error(w, fmt.Sprintf(msgBusCAFile, err), http.StatusBadGateway)
			case errors.As(err, &plaintext):
				http.Error(w, msgBusPlaintext, http.StatusBadGateway)
			case errors.As(err, &verify):
				body := fmt.Sprintf(msgBusUnverified, verify.Err)
				var wrongName x509.HostnameError
				if errors.As(verify.Err, &wrongName) {
					body += msgBusWrongName
				}
				http.Error(w, body, http.StatusBadGateway)
			default:
				http.Error(w, msgBusDown, http.StatusBadGateway)
			}
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
