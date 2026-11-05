package authcallout

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// renderedTLSDir is the directory the operator's rendered nats.conf names for
// the serving pair: the NATS pod's mount of the serving Secret.
const renderedTLSDir = "/etc/nats-tls/"

// testDialTimeout is the suite's connect budget, on both ends. A connect
// here is a TLS handshake plus the server's round trip to the callout, and
// the shipped shape bounds it at two seconds four times over: the client
// library's default dial timeout, and on the server the TLS handshake
// deadline, the authorization timeout the callout answers within, and the
// first ping to a client, which the Go client reads as a failed connect
// before it is authenticated (docs/designs/spec-nats-deployment.md,
// "authorization.timeout"). Under the race detector on a loaded runner the
// suite's own connects crossed two seconds, and the i/o timeout read as a
// refusal in whichever test happened to be connecting. None of the four is
// what this suite asserts, so the harness's dial option carries this on the
// client side and startHarness/startLiveHarness set it on the embedded
// server (serverConnectBudget); the rendered conf is untouched. Three
// clients stay at their own budgets: lib.Connect's callers keep its 10 s
// dial timeout; the worker adapter the session tests run opens a second,
// raw connection in dialBus with the library's 2 s; and the nats CLI
// (natscli_test.go) has no connect-timeout flag, so its connects keep the
// library's 2 s too. For those three only the server side widens.
const testDialTimeout = 30 * time.Second

// withTestTLS points the rendered conf's tls block at a CA and serving pair
// minted into a temp directory for this test, and returns the conf with the
// client option that trusts that CA and sets the suite's connect timeout
// (testDialTimeout). It is the third mechanical substitution beside the
// callout's public keys: the render requires TLS of every client, and a
// test process has no /etc/nats-tls.
func withTestTLS(t *testing.T, conf string) (string, nats.Option) {
	t.Helper()
	if !strings.Contains(conf, renderedTLSDir) {
		t.Fatalf("the rendered nats.conf no longer names %s; the TLS substitution would be inert", renderedTLSDir)
	}
	dir := t.TempDir()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	caTmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test bus CA"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "localhost"},
		DNSNames:     []string{"localhost"},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	write := func(name, typ string, b []byte) {
		if err := os.WriteFile(filepath.Join(dir, name), pem.EncodeToMemory(&pem.Block{Type: typ, Bytes: b}), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("ca.crt", "CERTIFICATE", caDER)
	write("tls.crt", "CERTIFICATE", der)
	write("tls.key", "PRIVATE KEY", keyDER)
	// The production clients this suite drives through lib (the worker
	// adapter, the agent principal) read their trust from the environment,
	// as they do in a pod the operator rendered.
	t.Setenv(lib.EnvNATSCAFile, filepath.Join(dir, "ca.crt"))
	rootCAs, timeout := nats.RootCAs(filepath.Join(dir, "ca.crt")), nats.Timeout(testDialTimeout)
	dial := func(o *nats.Options) error {
		if err := rootCAs(o); err != nil {
			return err
		}
		return timeout(o)
	}
	return strings.ReplaceAll(conf, renderedTLSDir, dir+"/"), dial
}

// serverConnectBudget lifts the embedded server's three two-second ceilings
// on an unauthenticated connection to the suite's connect budget; see
// testDialTimeout. Set on the parsed options, so the rendered conf the
// tests load keeps the shipped values.
func serverConnectBudget(opts *natsserver.Options) {
	opts.TLSTimeout = testDialTimeout.Seconds()
	opts.AuthTimeout = testDialTimeout.Seconds()
	opts.DisableShortFirstPing = true
}
