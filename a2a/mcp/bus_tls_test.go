package mcp

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
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

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// tlsBus starts a TLS-only server with the console user and returns its
// tls:// URL and a file holding the CA that signed it.
func tlsBus(t *testing.T) (string, string) {
	t.Helper()
	caKey, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	now := time.Now()
	caTmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test bus CA"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour),
		KeyUsage: x509.KeyUsageCertSign, BasicConstraintsValid: true, IsCA: true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTmpl, caTmpl, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caCert, _ := x509.ParseCertificate(caDER)
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "localhost"},
		DNSNames: []string{"localhost"}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, caCert, &key.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	if err := os.WriteFile(caFile, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}), 0o600); err != nil {
		t.Fatal(err)
	}

	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, JetStream: true, StoreDir: t.TempDir(), NoLog: true, NoSigs: true}
	opts.TLS = true
	opts.TLSConfig = &tls.Config{Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}}, MinVersion: tls.VersionTLS12}
	opts.Users = []*natsserver.User{{Username: testUser, Password: testPass, Permissions: &natsserver.Permissions{
		Publish:   &natsserver.SubjectPermission{Allow: consolePublish},
		Subscribe: &natsserver.SubjectPermission{Allow: consoleSubscribe},
	}}}
	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server did not start")
	}
	t.Cleanup(srv.Shutdown)
	return "tls://" + net.JoinHostPort("127.0.0.1", strings.TrimPrefix(srv.Addr().String(), "127.0.0.1:")), caFile
}

// The server dials through lib, so it takes the bus CA from NATS_CA_FILE like
// every other client, and without it a TLS bus is refused.
func TestTheBusIsDialledOverTLSWithTheBusCA(t *testing.T) {
	url, caFile := tlsBus(t)

	t.Run("without the CA", func(t *testing.T) {
		t.Setenv(lib.EnvNATSCAFile, "")
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		if b, err := DialBus(ctx, BusConfig{URL: url, User: testUser, Password: testPass}); err == nil {
			b.Close()
			t.Fatal("dialled a TLS bus whose certificate nothing trusts")
		}
	})

	t.Run("with the CA", func(t *testing.T) {
		t.Setenv(lib.EnvNATSCAFile, caFile)
		st := dial(t, url).Status(context.Background())
		if !st.Connected || !strings.HasPrefix(st.TLSVersion, "TLS 1.") {
			t.Errorf("connected=%v tls=%q, want a TLS connection", st.Connected, st.TLSVersion)
		}
		if !strings.Contains(FormatBusStatusMarkdown(st), "connected over TLS 1.") {
			t.Error("the markdown does not say the connection is TLS")
		}
	})
}
