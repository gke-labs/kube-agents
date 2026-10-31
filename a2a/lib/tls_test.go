package lib

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

func applyTLSOptions(t *testing.T) *nats.Options {
	t.Helper()
	o := nats.GetDefaultOptions()
	for _, opt := range TLSOptions() {
		if err := opt(&o); err != nil {
			t.Fatalf("applying a TLS option: %v", err)
		}
	}
	return &o
}

// Unset, a client takes no bus trust of its own, and the system roots decide.
func TestTLSOptionsWithoutACAFileAddsNothing(t *testing.T) {
	t.Setenv(EnvNATSCAFile, "")
	if opts := TLSOptions(); len(opts) != 0 {
		t.Fatalf("TLSOptions() = %d options with no CA file, want none", len(opts))
	}
}

// With a CA file the client is secure and verifies against that CA; with a
// server name as well, it checks the certificate against that name.
func TestTLSOptionsTrustTheNamedCA(t *testing.T) {
	ca := writeTestCA(t)
	t.Setenv(EnvNATSCAFile, ca)
	t.Setenv(EnvNATSTLSServerName, "")
	o := applyTLSOptions(t)
	if !o.Secure || o.RootCAsCB == nil {
		t.Fatalf("Secure=%v RootCAsCB set=%v, want a secure client with the CA loaded", o.Secure, o.RootCAsCB != nil)
	}
	if o.TLSConfig.ServerName != "" {
		t.Errorf("ServerName = %q with none asked for", o.TLSConfig.ServerName)
	}

	t.Setenv(EnvNATSTLSServerName, "agent-a2a-nats.ns.svc")
	o = applyTLSOptions(t)
	if o.TLSConfig.ServerName != "agent-a2a-nats.ns.svc" || o.RootCAsCB == nil {
		t.Errorf("ServerName = %q, RootCAsCB set=%v; want the override with the CA kept", o.TLSConfig.ServerName, o.RootCAsCB != nil)
	}
}

// A CA file that does not exist is an error at connect, not a silent fall
// back to the system roots.
func TestTLSOptionsRefuseAMissingCAFile(t *testing.T) {
	t.Setenv(EnvNATSCAFile, "/nonexistent/ca.crt")
	o := nats.GetDefaultOptions()
	var err error
	for _, opt := range TLSOptions() {
		if err = opt(&o); err != nil {
			break
		}
	}
	if err == nil {
		t.Fatal("a missing CA file was accepted")
	}
}

// writeTestCA writes a self-signed CA certificate and returns its path.
func writeTestCA(t *testing.T) string {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "test CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}
