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

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// renderedTLSDir is the directory the operator's rendered nats.conf names for
// the serving pair: the NATS pod's mount of the serving Secret.
const renderedTLSDir = "/etc/nats-tls/"

// withTestTLS points the rendered conf's tls block at a CA and serving pair
// minted into a temp directory for this test, and returns the conf with the
// client option that trusts that CA. It is the third mechanical substitution
// beside the callout's public keys: the render requires TLS of every client,
// and a test process has no /etc/nats-tls.
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
	return strings.ReplaceAll(conf, renderedTLSDir, dir+"/"), nats.RootCAs(filepath.Join(dir, "ca.crt"))
}
