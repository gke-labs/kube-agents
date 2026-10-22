package console

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
)

// busServerName is the name the test bus's certificate carries and the proxy
// dials it by, as the operator's console dials the NATS Service name. The
// certificate names nothing else, not even the loopback address the listener
// binds.
const busServerName = "localhost"

// testCA is a CA that can issue the bus a serving certificate.
type testCA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func newTestCA(t *testing.T) testCA {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test bus CA"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return testCA{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

// writeCA writes the CA's PEM to a fresh file and returns its path.
func (ca testCA) writeCA(t *testing.T) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(path, ca.pem, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// serving issues a certificate for busServerName.
func (ca testCA) serving(t *testing.T) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: busServerName},
		DNSNames:     []string{busServerName},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// wssBus starts an embedded nats-server whose websocket listener serves TLS
// from a certificate ca issued, allowing exactly the console origin, and
// returns the listener as the https URL the proxy dials.
func wssBus(t *testing.T, ca testCA) *url.URL {
	t.Helper()
	opts := &natsserver.Options{}
	opts.NoLog, opts.NoSigs = true, true
	opts.Host = "127.0.0.1"
	opts.Port = -1
	opts.Websocket.Host = "127.0.0.1"
	opts.Websocket.Port = -1
	opts.Websocket.TLSConfig = &tls.Config{Certificates: []tls.Certificate{ca.serving(t)}, MinVersion: tls.VersionTLS12}
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
	u, err := url.Parse(srv.PortsInfo(20 * time.Second).WebSocket[0])
	if err != nil {
		t.Fatal(err)
	}
	if u.Scheme != "wss" {
		t.Fatalf("test bus advertises %s, want a wss listener", u)
	}
	u.Scheme = schemeHTTPS
	u.Host = busServerName + ":" + u.Port()
	return u
}

// tlsConsole serves the console in front of a TLS bus, verifying it against
// caFile.
func tlsConsole(t *testing.T, bus *url.URL, caFile string) *httptest.Server {
	t.Helper()
	cfg := fixture(t, "x")
	cfg.BusURL = bus
	cfg.CAFile = caFile
	ts := httptest.NewServer(handler(t, cfg))
	t.Cleanup(ts.Close)
	return ts
}

func TestTheProxyUpgradesToATLSBusItVerifies(t *testing.T) {
	ca := newTestCA(t)
	ts := tlsConsole(t, wssBus(t, ca), ca.writeCA(t))

	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	if resp.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("status = %d, want 101 over a verified TLS dial", resp.StatusCode)
	}
}

func TestATLSBusUnderAnotherCAIsA502(t *testing.T) {
	bus := wssBus(t, newTestCA(t))
	ts := tlsConsole(t, bus, newTestCA(t).writeCA(t))

	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502: the bus's certificate is not from the trusted CA", resp.StatusCode)
	}
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
	if !strings.Contains(string(body), msgBusUnverified) {
		t.Errorf("body %q does not say the certificate failed to verify; a bus that answered is not a bus that is down", body)
	}
}

// ServerName stands in for the URL's host when the console dials a
// port-forwarded bus by its loopback address, which the certificate does not
// name.
func TestServerNameOverridesTheBusURLsHost(t *testing.T) {
	ca := newTestCA(t)
	bus := wssBus(t, ca)
	loopback := *bus
	loopback.Host = "127.0.0.1:" + bus.Port()

	for _, tc := range []struct {
		serverName string
		want       int
		body       string
	}{
		// The CA is right and the name is not: the body says so, and
		// names the remedy, rather than sending the reader to the CA.
		{"", http.StatusBadGateway, "does not name 127.0.0.1 - set NATS_TLS_SERVER_NAME"},
		{busServerName, http.StatusSwitchingProtocols, ""},
	} {
		cfg := fixture(t, "x")
		cfg.BusURL = &loopback
		cfg.CAFile = ca.writeCA(t)
		cfg.ServerName = tc.serverName
		ts := httptest.NewServer(handler(t, cfg))
		resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		ts.Close()
		if resp.StatusCode != tc.want {
			t.Errorf("server name %q: status = %d, want %d", tc.serverName, resp.StatusCode, tc.want)
		}
		if tc.body != "" && !strings.Contains(string(body), tc.body) {
			t.Errorf("server name %q: body = %q, want it to contain %q", tc.serverName, body, tc.body)
		}
	}
}

// A plain http bus URL against the TLS listener, the pre-#3651 render, gets no
// websocket either: the listener does not speak plaintext.
func TestAPlainDialToATLSBusGetsNoWebsocket(t *testing.T) {
	bus := wssBus(t, newTestCA(t))
	plain := *bus
	plain.Scheme = schemeHTTP
	cfg := fixture(t, "x")
	cfg.BusURL = &plain
	ts := httptest.NewServer(handler(t, cfg))
	t.Cleanup(ts.Close)

	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	if resp.StatusCode == http.StatusSwitchingProtocols {
		t.Error("a plaintext dial to the TLS listener upgraded")
	}
}

// The CA file is read on every dial, so a bundle the kubelet rewrites in
// place, as on a CA rotation, takes effect without a restart.
func TestTheCAFileIsReadOnEveryDial(t *testing.T) {
	ca := newTestCA(t)
	bus := wssBus(t, ca)
	caFile := newTestCA(t).writeCA(t)
	ts := tlsConsole(t, bus, caFile)

	if resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080"); resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status = %d under the wrong CA, want 502", resp.StatusCode)
	}
	if err := os.WriteFile(caFile, ca.pem, 0o600); err != nil {
		t.Fatal(err)
	}
	if resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080"); resp.StatusCode != http.StatusSwitchingProtocols {
		t.Errorf("status = %d after the CA file was rewritten, want 101", resp.StatusCode)
	}
}

func TestNewHandlerRefusesAnHTTPSBusWithoutAUsableCA(t *testing.T) {
	bus, _ := url.Parse("https://nats.ns.svc:9222")
	notPEM := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(notPEM, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	cases := map[string]string{
		"no CA file":        "",
		"a missing CA file": filepath.Join(t.TempDir(), "absent.crt"),
		"a CA file not PEM": notPEM,
	}
	for name, caFile := range cases {
		t.Run(name, func(t *testing.T) {
			cfg := fixture(t, "x")
			cfg.BusURL = bus
			cfg.CAFile = caFile
			if _, err := NewHandler(cfg, quietLog()); err == nil {
				t.Errorf("NewHandler accepted an https bus with %s", name)
			}
		})
	}
}

func TestANATSClientRoundTripsThroughTheProxyToATLSBus(t *testing.T) {
	ca := newTestCA(t)
	cfg := fixture(t, "x")
	cfg.BusURL = wssBus(t, ca)
	cfg.CAFile = ca.writeCA(t)
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
	sub, err := nc.SubscribeSync("console.proxy.tls")
	if err != nil {
		t.Fatal(err)
	}
	if err := nc.Publish("console.proxy.tls", []byte("hello")); err != nil {
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
