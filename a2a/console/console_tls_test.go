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
	now := time.Now()
	return ca.servingFor(t, now.Add(-time.Hour), now.Add(time.Hour))
}

// servingFor issues the bus's serving certificate for the given window.
func (ca testCA) servingFor(t *testing.T, notBefore, notAfter time.Time) tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: busServerName},
		DNSNames:     []string{busServerName},
		NotBefore:    notBefore,
		NotAfter:     notAfter,
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
	return busServing(t, &tls.Config{Certificates: []tls.Certificate{ca.serving(t)}, MinVersion: tls.VersionTLS12})
}

// busServing starts the bus with the given websocket TLS config, or a
// plaintext websocket listener when it is nil, and returns the https URL the
// proxy dials either way.
func busServing(t *testing.T, tlsConfig *tls.Config) *url.URL {
	t.Helper()
	opts := &natsserver.Options{}
	opts.NoLog, opts.NoSigs = true, true
	opts.Host = "127.0.0.1"
	opts.Port = -1
	opts.Websocket.Host = "127.0.0.1"
	opts.Websocket.Port = -1
	opts.Websocket.TLSConfig = tlsConfig
	opts.Websocket.NoTLS = tlsConfig == nil
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
	if want := map[bool]string{true: "wss", false: "ws"}[tlsConfig != nil]; u.Scheme != want {
		t.Fatalf("test bus advertises %s, want a %s listener", u, want)
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
	if !strings.Contains(string(body), "did not verify: ") || !strings.Contains(string(body), "unknown authority") {
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
		{"", http.StatusBadGateway, "for 127.0.0.1 because it doesn't contain any IP SANs" + msgBusWrongName},
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

	// Rewritten to something unusable after start: the page is told the
	// console's own CA file failed, not that the bus did not answer.
	if err := os.WriteFile(caFile, []byte("not a certificate"), 0o600); err != nil {
		t.Fatal(err)
	}
	resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
	if resp.StatusCode != http.StatusBadGateway || !strings.Contains(string(body), "NATS_CA_FILE") || !strings.Contains(string(body), caFile) {
		t.Errorf("status %d, body %q after the CA file went bad; want a 502 naming NATS_CA_FILE and %s", resp.StatusCode, body, caFile)
	}
}

// Each other shape a dial can fail in reaches the page as what it is.
func TestABusThatAnsweredIsNotCalledDown(t *testing.T) {
	ca := newTestCA(t)
	past := time.Now().Add(-48 * time.Hour)
	for name, tc := range map[string]struct {
		bus  *url.URL
		want string
	}{
		"an expired serving certificate": {
			busServing(t, &tls.Config{Certificates: []tls.Certificate{ca.servingFor(t, past, past.Add(time.Hour))}, MinVersion: tls.VersionTLS12}),
			"did not verify: x509: certificate has expired",
		},
		"a bus still serving plaintext": {busServing(t, nil), msgBusPlaintext},
	} {
		t.Run(name, func(t *testing.T) {
			ts := tlsConsole(t, tc.bus, ca.writeCA(t))
			resp, _, _ := handshake(t, ts.Listener.Addr().String(), "http://localhost:8080")
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
			if resp.StatusCode != http.StatusBadGateway || !strings.Contains(string(body), tc.want) {
				t.Errorf("status %d, body %q; want a 502 saying %q", resp.StatusCode, body, tc.want)
			}
		})
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
