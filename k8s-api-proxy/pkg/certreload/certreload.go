// Package certreload serves PEM certificates, keys and CA bundles from files that are
// rotated on disk, without a restart. A Secret, ConfigMap or projected volume, a
// ClusterTrustBundle mount, cert-manager or a PKI agent can each replace the files; the
// next TLS handshake or dial after the change uses the new contents.
//
// A file is checked at most once per Options.CheckInterval, when a handshake or dial
// asks for it. It is re-read only when its size, modification time, device or inode
// changes (os.Stat follows the symlink a volume swaps on update, so the swap is seen), and
// re-parsed only when its bytes differ from the last good load. A load that fails, such as
// a file read mid-rotation, an empty file, or a certificate that does not match its key,
// is reported through Options.OnReloadError and the last good value keeps being served.
// Only the first load, in the constructor, returns an error.
package certreload

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"sync"
	"syscall"
	"time"
)

const (
	// DefaultCheckInterval is how often a file is checked for a change when
	// Options.CheckInterval is zero.
	DefaultCheckInterval = time.Second

	pemTypeCertificate = "CERTIFICATE"
	// pemBeginMarker opens every PEM block; parseCABundle requires one where each block
	// starts, and pemCommentPrefix opens a comment line it allows between blocks.
	pemBeginMarker   = "-----BEGIN "
	pemCommentPrefix = '#'
	// pemEndMarker closes a PEM block.
	pemEndMarker = "-----END "

	// dialTimeout and dialKeepAlive match http.DefaultTransport's dialer.
	dialTimeout   = 30 * time.Second
	dialKeepAlive = 30 * time.Second
	// tlsHandshakeTimeout bounds a CA-pinned handshake, as http.DefaultTransport's
	// TLSHandshakeTimeout does for connections it dials itself.
	tlsHandshakeTimeout = 10 * time.Second
)

// Options tunes a reloader. The zero value checks every DefaultCheckInterval and logs
// failed reloads with slog.Default.
type Options struct {
	// CheckInterval is the least time between two checks of the files; DefaultCheckInterval
	// when zero or negative.
	CheckInterval time.Duration
	// OnReloadError is called when a changed file cannot be loaded and the previous value
	// is kept, once per version of the files. It logs a warning with slog.Default when nil.
	OnReloadError func(error)
	// Now returns the current time; time.Now when nil. For tests.
	Now func() time.Time
}

// reloader holds the value last parsed from paths and re-parses it when they change.
type reloader[T any] struct {
	paths    []string
	parse    func([][]byte) (T, error)
	interval time.Duration
	onError  func(error)
	now      func() time.Time

	mu        sync.Mutex
	value     T
	raw       [][]byte
	checkedAt time.Time
	// seen identifies the files' version value was loaded from; failed the last version
	// that could not be loaded, so each bad version is reported once.
	seen   string
	failed string
}

func newReloader[T any](paths []string, parse func([][]byte) (T, error), opts Options) (*reloader[T], error) {
	r := &reloader[T]{
		paths:    paths,
		parse:    parse,
		interval: opts.CheckInterval,
		onError:  opts.OnReloadError,
		now:      opts.Now,
	}
	if r.interval <= 0 {
		r.interval = DefaultCheckInterval
	}
	if r.onError == nil {
		r.onError = func(err error) {
			slog.Default().Warn("certreload: keeping the last good contents", "err", err)
		}
	}
	if r.now == nil {
		r.now = time.Now
	}
	identity, err := r.identity()
	if err != nil {
		return nil, err
	}
	raw, err := r.read()
	if err != nil {
		return nil, err
	}
	value, err := parse(raw)
	if err != nil {
		return nil, err
	}
	r.value, r.raw, r.seen, r.checkedAt = value, raw, identity, r.now()
	return r, nil
}

// get returns the current value, first reloading it when the check interval has passed
// and the files have changed.
func (r *reloader[T]) get() T {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	if now.Sub(r.checkedAt) < r.interval {
		return r.value
	}
	r.checkedAt = now
	identity, err := r.identity()
	if err != nil {
		r.fail(err.Error(), err)
		return r.value
	}
	if identity == r.seen {
		// Back on the loaded version: a later failure is a new one, reported again.
		r.failed = ""
		return r.value
	}
	raw, err := r.read()
	if err != nil {
		r.fail(identity, err)
		return r.value
	}
	if equalContents(raw, r.raw) {
		r.seen, r.failed = identity, ""
		return r.value
	}
	value, err := r.parse(raw)
	if err != nil {
		// seen stays as it was, so the next check reads the files again: a rotation
		// still being written settles into a version that loads.
		r.fail(identity, err)
		return r.value
	}
	r.value, r.raw, r.seen, r.failed = value, raw, identity, ""
	return r.value
}

func (r *reloader[T]) fail(version string, err error) {
	if version == r.failed {
		return
	}
	r.failed = version
	r.onError(err)
}

func (r *reloader[T]) identity() (string, error) {
	var id string
	for _, path := range r.paths {
		info, err := os.Stat(path)
		if err != nil {
			return "", fmt.Errorf("stat %s: %w", path, err)
		}
		id += fmt.Sprintf("%s:%d/%d", path, info.Size(), info.ModTime().UnixNano())
		if st, ok := info.Sys().(*syscall.Stat_t); ok {
			id += fmt.Sprintf("/%d/%d", st.Dev, st.Ino)
		}
		id += ";"
	}
	return id, nil
}

func (r *reloader[T]) read() ([][]byte, error) {
	raw := make([][]byte, len(r.paths))
	for i, path := range r.paths {
		data, err := os.ReadFile(path) // #nosec G304 -- a path the operator configured, not request data
		if err != nil {
			return nil, err
		}
		raw[i] = data
	}
	return raw, nil
}

func equalContents(a, b [][]byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if !bytes.Equal(a[i], b[i]) {
			return false
		}
	}
	return true
}

// ServerCertificate is a TLS server certificate and private key read from PEM files and
// reloaded when they change. Set its GetCertificate as tls.Config.GetCertificate.
type ServerCertificate struct {
	r *reloader[*tls.Certificate]
}

// NewServerCertificate loads the certificate chain in certFile and the private key in
// keyFile. When keyFile is empty or the same path as certFile, certFile holds both the
// CERTIFICATE blocks and the PRIVATE KEY block. It fails when the files cannot be read or
// do not hold a matching certificate and key.
func NewServerCertificate(certFile, keyFile string, opts Options) (*ServerCertificate, error) {
	if certFile == "" {
		return nil, errors.New("certreload: a certificate file is required")
	}
	paths := []string{certFile}
	if keyFile != "" && keyFile != certFile {
		paths = append(paths, keyFile)
	}
	parse := func(raw [][]byte) (*tls.Certificate, error) {
		keyPEM := raw[len(raw)-1]
		cert, err := tls.X509KeyPair(raw[0], keyPEM)
		if err != nil {
			return nil, fmt.Errorf("server certificate %v: %w", paths, err)
		}
		return &cert, nil
	}
	r, err := newReloader(paths, parse, opts)
	if err != nil {
		return nil, err
	}
	return &ServerCertificate{r: r}, nil
}

// GetCertificate returns the current certificate; it never fails once the first load
// has succeeded.
func (c *ServerCertificate) GetCertificate(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	return c.r.get(), nil
}

// CAPool is a pool of root CA certificates read from a PEM bundle and reloaded when the
// bundle changes.
type CAPool struct {
	r *reloader[*x509.CertPool]
}

// NewCAPool loads the CERTIFICATE blocks of the PEM bundle in file. It fails when the file
// cannot be read, holds no certificate, holds a certificate that does not parse, or holds
// anything besides PEM blocks, blank lines and # comment lines between them (a block that
// does not decode, a damaged BEGIN line, a file cut off mid-write). Other PEM block types
// are skipped.
func NewCAPool(file string, opts Options) (*CAPool, error) {
	if file == "" {
		return nil, errors.New("certreload: a CA bundle file is required")
	}
	parse := func(raw [][]byte) (*x509.CertPool, error) {
		pool, err := parseCABundle(raw[0])
		if err != nil {
			return nil, fmt.Errorf("CA bundle %s: %w", file, err)
		}
		return pool, nil
	}
	r, err := newReloader([]string{file}, parse, opts)
	if err != nil {
		return nil, err
	}
	return &CAPool{r: r}, nil
}

func parseCABundle(data []byte) (*x509.CertPool, error) {
	pool := x509.NewCertPool()
	count := 0
	rest := data
	for {
		// pem.Decode skips anything before a line-start BEGIN marker, a damaged block
		// included, so each block is required where the previous one ended: only blank
		// lines and # comment lines may come between blocks.
		rest = skipBundleFiller(rest)
		if len(rest) == 0 {
			break
		}
		if !bytes.HasPrefix(rest, []byte(pemBeginMarker)) {
			return nil, errors.New("data that is not a PEM block or a # comment line")
		}
		block, next := pem.Decode(rest)
		// A block whose body does not decode is skipped too, and the next one returned:
		// the decoded block must end at the first END line.
		if block == nil || len(next) != len(afterFirstEndLine(rest)) {
			return nil, errors.New("a PEM block that could not be decoded")
		}
		rest = next
		if block.Type != pemTypeCertificate {
			continue
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, err
		}
		pool.AddCert(cert)
		count++
	}
	if count == 0 {
		return nil, errors.New("no CERTIFICATE block")
	}
	return pool, nil
}

// afterFirstEndLine returns what follows the line holding the first END marker in data,
// or nothing when there is none.
func afterFirstEndLine(data []byte) []byte {
	i := bytes.Index(data, []byte(pemEndMarker))
	if i < 0 {
		return nil
	}
	if j := bytes.IndexByte(data[i:], '\n'); j >= 0 {
		return data[i+j+1:]
	}
	return nil
}

// skipBundleFiller drops leading whitespace and whole lines that start with '#'.
func skipBundleFiller(data []byte) []byte {
	for {
		data = bytes.TrimLeft(data, " \t\r\n")
		if len(data) == 0 || data[0] != pemCommentPrefix {
			return data
		}
		if i := bytes.IndexByte(data, '\n'); i >= 0 {
			data = data[i+1:]
		} else {
			return nil
		}
	}
}

// Pool returns the current pool. Do not modify it.
func (p *CAPool) Pool() *x509.CertPool {
	return p.r.get()
}

// DialTLSContext returns a dialer for http.Transport.DialTLSContext that verifies each new
// connection against the current pool. Every connection gets a clone of base (a TLS 1.2
// minimum when base is nil) with RootCAs set to Pool(), and ServerName set to the dialed
// host when base leaves it empty, so the usual chain and hostname or IP SAN checks apply.
func (p *CAPool) DialTLSContext(base *tls.Config) func(ctx context.Context, network, addr string) (net.Conn, error) {
	dialer := &net.Dialer{Timeout: dialTimeout, KeepAlive: dialKeepAlive}
	return func(ctx context.Context, network, addr string) (net.Conn, error) {
		cfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if base != nil {
			cfg = base.Clone()
		}
		cfg.RootCAs = p.Pool()
		if cfg.ServerName == "" {
			host, _, err := net.SplitHostPort(addr)
			if err != nil {
				return nil, err
			}
			cfg.ServerName = host
		}
		raw, err := dialer.DialContext(ctx, network, addr)
		if err != nil {
			return nil, err
		}
		// net/http ignores Transport.TLSHandshakeTimeout once DialTLSContext is set, so
		// the handshake gets its own bound for a caller whose request has no deadline.
		hctx, cancel := context.WithTimeout(ctx, tlsHandshakeTimeout)
		defer cancel()
		conn := tls.Client(raw, cfg)
		if err := conn.HandshakeContext(hctx); err != nil {
			_ = raw.Close()
			return nil, err
		}
		return conn, nil
	}
}

// Transport returns a clone of http.DefaultTransport whose https connections are made by
// DialTLSContext(base). It speaks HTTP/1.1 and uses no proxy: net/http does not route a
// proxied https connection through DialTLSContext, so a proxy would bypass the pool.
func (p *CAPool) Transport(base *tls.Config) *http.Transport {
	t := &http.Transport{}
	if def, ok := http.DefaultTransport.(*http.Transport); ok {
		t = def.Clone()
	}
	t.Proxy = nil
	t.ForceAttemptHTTP2 = false
	t.TLSClientConfig = nil
	t.DialTLSContext = p.DialTLSContext(base)
	return t
}
