package console

import (
	"bufio"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

const testHost = "localhost:8080"

func quietLog() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

// fixture is a static dir with an index.html and a password file.
func fixture(t *testing.T, pass string) Config {
	t.Helper()
	dir := t.TempDir()
	static := filepath.Join(dir, "static")
	if err := os.Mkdir(static, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(static, "index.html"), []byte("<!doctype html><title>a2a console</title>"), 0o644); err != nil {
		t.Fatal(err)
	}
	passFile := filepath.Join(dir, "console-password")
	if err := os.WriteFile(passFile, []byte(pass), 0o600); err != nil {
		t.Fatal(err)
	}
	bus, _ := url.Parse("http://127.0.0.1:1")
	return Config{
		StaticDir:    static,
		BusURL:       bus,
		User:         "console",
		PasswordFile: passFile,
		AllowedHosts: []string{"localhost:8080", "127.0.0.1:8080"},
	}
}

func handler(t *testing.T, cfg Config) http.Handler {
	t.Helper()
	h, err := NewHandler(cfg, quietLog())
	if err != nil {
		t.Fatalf("NewHandler: %v", err)
	}
	return h
}

func get(h http.Handler, host, path string, header http.Header) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, path, nil)
	req.Host = host
	for k, vs := range header {
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func TestConfigJSONHandsThePageTheCredential(t *testing.T) {
	h := handler(t, fixture(t, "s3cret\n"))
	rec := get(h, testHost, "/config.json", nil)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body %q", rec.Code, rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "no-store" {
		t.Errorf("Cache-Control = %q, want no-store: a cached credential outlives a rotation", cc)
	}
	var got struct{ User, Pass string }
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("body is not JSON: %v (%q)", err, rec.Body.String())
	}
	if got.User != "console" || got.Pass != "s3cret" {
		t.Errorf("got %+v, want user console and the file's password with the newline trimmed", got)
	}
}

func TestConfigJSONReadsTheFileOnEveryRequest(t *testing.T) {
	cfg := fixture(t, "one")
	h := handler(t, cfg)
	if body := get(h, testHost, "/config.json", nil).Body.String(); !strings.Contains(body, `"one"`) {
		t.Fatalf("first read = %q", body)
	}
	// The kubelet swaps the Secret volume's files in place on a rotation.
	// A server that cached the value would hand the page a dead password.
	if err := os.WriteFile(cfg.PasswordFile, []byte("two"), 0o600); err != nil {
		t.Fatal(err)
	}
	if body := get(h, testHost, "/config.json", nil).Body.String(); !strings.Contains(body, `"two"`) {
		t.Errorf("after rotation = %q, want the new password", body)
	}
}

func TestConfigJSONNamesAMissingOrEmptyCredential(t *testing.T) {
	for name, mutate := range map[string]func(Config){
		"missing": func(c Config) { _ = os.Remove(c.PasswordFile) },
		"empty":   func(c Config) { _ = os.WriteFile(c.PasswordFile, []byte("\n"), 0o600) },
	} {
		t.Run(name, func(t *testing.T) {
			cfg := fixture(t, "x")
			h := handler(t, cfg)
			mutate(cfg)
			rec := get(h, testHost, "/config.json", nil)
			if rec.Code != http.StatusServiceUnavailable {
				t.Fatalf("status = %d, want 503", rec.Code)
			}
			if !strings.Contains(rec.Body.String(), cfg.PasswordFile) {
				t.Errorf("body %q does not name %s; the page shows this sentence and it has to say where to look", rec.Body.String(), cfg.PasswordFile)
			}
		})
	}
}

func TestAWrongHostIsRefusedAndTheRefusalNamesThePort(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	for _, host := range []string{"localhost:8081", "evil.example:8080", "127.0.0.1"} {
		for _, path := range []string{"/", "/config.json", "/bus"} {
			rec := get(h, host, path, nil)
			if rec.Code != http.StatusMisdirectedRequest {
				t.Errorf("Host %s %s: status = %d, want 421", host, path, rec.Code)
				continue
			}
			if !strings.Contains(rec.Body.String(), "localhost:8080") {
				t.Errorf("Host %s %s: body %q does not say which port to forward", host, path, rec.Body.String())
			}
			if strings.Contains(rec.Body.String(), "pass") {
				t.Errorf("Host %s %s: refusal body mentions the credential: %q", host, path, rec.Body.String())
			}
		}
	}
}

func TestHostMatchingIgnoresCase(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	if rec := get(h, "LOCALHOST:8080", "/config.json", nil); rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200: hostnames are case-insensitive", rec.Code)
	}
}

func TestNoAnswerCarriesCORSHeaders(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	origin := http.Header{"Origin": {"https://evil.example"}}
	for _, path := range []string{"/", "/config.json"} {
		rec := get(h, testHost, path, origin)
		for k := range rec.Header() {
			if strings.HasPrefix(strings.ToLower(k), "access-control-") {
				t.Errorf("%s answered with %s; the credential must stay unreadable cross-origin", path, k)
			}
		}
	}
}

func TestPassingAnswersRefuseFraming(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	rec := get(h, testHost, "/", nil)
	if rec.Header().Get("X-Frame-Options") != "DENY" {
		t.Errorf("X-Frame-Options = %q, want DENY", rec.Header().Get("X-Frame-Options"))
	}
	if rec.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Errorf("X-Content-Type-Options = %q, want nosniff", rec.Header().Get("X-Content-Type-Options"))
	}
}

func TestHealthzIgnoresTheHostGuard(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	// The kubelet probes the pod IP, so its Host is never localhost.
	rec := get(h, "10.0.0.7:8080", "/healthz", nil)
	if rec.Code != http.StatusOK {
		t.Errorf("status = %d, want 200", rec.Code)
	}
}

func TestTheIndexIsServed(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	rec := get(h, testHost, "/", nil)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "a2a console") {
		t.Errorf("GET / = %d %q", rec.Code, rec.Body.String())
	}
}

func TestAPlainGETOnTheBusPathIsRefused(t *testing.T) {
	h := handler(t, fixture(t, "x"))
	rec := get(h, testHost, "/bus", nil)
	if rec.Code != http.StatusBadRequest {
		t.Errorf("status = %d, want 400", rec.Code)
	}
}

func TestNewHandlerRefusesABadConfig(t *testing.T) {
	cases := map[string]func(*Config){
		"no bus URL":           func(c *Config) { c.BusURL = nil },
		"ws bus URL":           func(c *Config) { c.BusURL, _ = url.Parse("ws://nats:9222") },
		"bus URL without host": func(c *Config) { c.BusURL, _ = url.Parse("http:///x") },
		"no user":              func(c *Config) { c.User = "" },
		"no password file":     func(c *Config) { c.PasswordFile = "" },
		"no allowed hosts":     func(c *Config) { c.AllowedHosts = nil },
		"an empty host":        func(c *Config) { c.AllowedHosts = []string{"localhost:8080", ""} },
		"missing static dir":   func(c *Config) { c.StaticDir = filepath.Join(c.StaticDir, "nope") },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := fixture(t, "x")
			mutate(&cfg)
			if _, err := NewHandler(cfg, quietLog()); err == nil {
				t.Errorf("NewHandler accepted a config with %s", name)
			}
		})
	}
}

// wsBus starts an embedded nats-server with a websocket listener that
// allows exactly the console origin, and returns the listener's http URL.
func wsBus(t *testing.T) *url.URL {
	t.Helper()
	opts := &natsserver.Options{}
	opts.NoLog, opts.NoSigs = true, true
	opts.Host = "127.0.0.1"
	opts.Port = -1
	opts.Websocket.Host = "127.0.0.1"
	opts.Websocket.Port = -1
	opts.Websocket.NoTLS = true
	opts.Websocket.AllowedOrigins = []string{"http://localhost:8080"}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(20 * time.Second) {
		t.Fatal("nats-server did not start")
	}
	t.Cleanup(srv.Shutdown)
	ws := srv.PortsInfo(20 * time.Second).WebSocket[0]
	u, err := url.Parse(ws)
	if err != nil {
		t.Fatal(err)
	}
	u.Scheme = "http"
	return u
}

// handshake sends a websocket upgrade through the console and returns the
// response and the reader positioned after its headers.
func handshake(t *testing.T, addr, origin string) (*http.Response, *bufio.Reader, net.Conn) {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	req := "GET /bus HTTP/1.1\r\n" +
		"Host: localhost:8080\r\n" +
		"Upgrade: websocket\r\n" +
		"Connection: Upgrade\r\n" +
		"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n" +
		"Sec-WebSocket-Version: 13\r\n" +
		"Origin: " + origin + "\r\n\r\n"
	if _, err := io.WriteString(conn, req); err != nil {
		t.Fatal(err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(10 * time.Second))
	br := bufio.NewReader(conn)
	resp, err := http.ReadResponse(br, nil)
	if err != nil {
		t.Fatal(err)
	}
	return resp, br, conn
}

func TestTheProxyUpgradesToTheBus(t *testing.T) {
	cfg := fixture(t, "x")
	cfg.BusURL = wsBus(t)
	ts := httptest.NewServer(handler(t, cfg))
	t.Cleanup(ts.Close)

	resp, br, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status = %d, want 101", resp.StatusCode)
	}
	buf := make([]byte, 512)
	n, _ := io.ReadAtLeast(br, buf, 8)
	if !strings.Contains(string(buf[:n]), "INFO") {
		t.Errorf("first frame %q does not carry the server's INFO", buf[:n])
	}
}

func TestTheBusRefusalOfAForeignOriginComesBackUnchanged(t *testing.T) {
	cfg := fixture(t, "x")
	cfg.BusURL = wsBus(t)
	ts := httptest.NewServer(handler(t, cfg))
	t.Cleanup(ts.Close)

	// The proxy forwards Origin as the browser sent it, so the bus's own
	// allow-list is what decides.
	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "https://evil.example")
	if resp.StatusCode != http.StatusForbidden {
		t.Errorf("status = %d, want the bus's 403", resp.StatusCode)
	}
}

func TestABusThatDoesNotAnswerIsA502(t *testing.T) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	closed := ln.Addr().String()
	_ = ln.Close()

	cfg := fixture(t, "x")
	cfg.BusURL, _ = url.Parse("http://" + closed)
	ts := httptest.NewServer(handler(t, cfg))
	t.Cleanup(ts.Close)

	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", resp.StatusCode)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
	if !strings.Contains(string(body), "bus") {
		t.Errorf("body %q does not say the bus failed", body)
	}
}

func TestABusThatAcceptsButNeverAnswersIsA502WithinTheTimeout(t *testing.T) {
	// A listener that takes the TCP connection and then never writes anything
	// back: the black-holed-bus case (a NetworkPolicy dropping the reply, a
	// stuck process) rather than the refused-connection case above. Without
	// its own transport timeout, the proxy would wait for this forever.
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var mu sync.Mutex
	var held []net.Conn
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			mu.Lock()
			held = append(held, conn)
			mu.Unlock()
		}
	}()
	t.Cleanup(func() {
		_ = ln.Close()
		mu.Lock()
		defer mu.Unlock()
		for _, c := range held {
			_ = c.Close()
		}
	})

	cfg := fixture(t, "x")
	cfg.BusURL, _ = url.Parse("http://" + ln.Addr().String())
	ts := httptest.NewServer(handler(t, cfg))
	t.Cleanup(ts.Close)

	start := time.Now()
	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	elapsed := time.Since(start)
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", resp.StatusCode)
	}
	if elapsed > 8*time.Second {
		t.Errorf("took %s to answer; a hung upgrade must time out well inside that, not at the handshake's own read deadline", elapsed)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
	if !strings.Contains(string(body), "bus") {
		t.Errorf("body %q does not say the bus failed", body)
	}
}

func TestANATSClientRoundTripsThroughTheProxy(t *testing.T) {
	cfg := fixture(t, "x")
	cfg.BusURL = wsBus(t)
	// Unstarted, so the listener's address is known before the handler is
	// built. nats.go sends the URL's host as Host, so the test server's own
	// address has to be one the guard answers on.
	ts := httptest.NewUnstartedServer(nil)
	cfg.AllowedHosts = append(cfg.AllowedHosts, ts.Listener.Addr().String())
	ts.Config.Handler = handler(t, cfg)
	ts.Start()
	t.Cleanup(ts.Close)

	nc, err := nats.Connect("ws://"+ts.Listener.Addr().String()+"/bus", nats.Timeout(10*time.Second))
	if err != nil {
		t.Fatalf("connect through the proxy: %v", err)
	}
	t.Cleanup(nc.Close)
	sub, err := nc.SubscribeSync("console.proxy.test")
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish("console.proxy.test", []byte("hello")); err != nil {
		t.Fatal(err)
	}
	msg, err := sub.NextMsg(10 * time.Second)
	if err != nil {
		t.Fatalf("no message back through the proxy: %v", err)
	}
	if string(msg.Data) != "hello" {
		t.Errorf("got %q", msg.Data)
	}
}
