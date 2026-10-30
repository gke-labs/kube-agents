package controller

import (
	"crypto/tls"
	"crypto/x509"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
)

// The rendered nats.conf requires TLS and names the serving pair under
// a2aNATSTLSMountDir, which a test process does not have. The embedded-server
// tests mint one pair for the whole package, with the operator's own issuing
// code, and point the conf at it; every client those tests dial trusts the CA
// through a2aTestBusTLS.
//
// One pair, not one per test, because the restart tests start the same bus
// twice on one port and their clients reconnect across it: a second pair would
// be a different bus to them.

// a2aTestServerName is the serving pair's one SAN; clients set it as the TLS
// server name, since they dial the loopback address.
const a2aTestServerName = "localhost"

var (
	a2aTestTLSOnce sync.Once
	a2aTestTLSDir  string
	a2aTestTLSPool *x509.CertPool
	a2aTestTLSErr  error
)

func a2aTestTLSMaterial(t *testing.T) (string, *x509.CertPool) {
	t.Helper()
	a2aTestTLSOnce.Do(func() {
		now := time.Now()
		caCert, caKey, caPEM, _, err := generateA2ANATSCA(now)
		if err != nil {
			a2aTestTLSErr = err
			return
		}
		_, certPEM, keyPEM, err := generateA2ANATSServing(caCert, caKey, []string{a2aTestServerName}, now)
		if err != nil {
			a2aTestTLSErr = err
			return
		}
		dir, err := os.MkdirTemp("", "a2a-nats-tls-")
		if err != nil {
			a2aTestTLSErr = err
			return
		}
		for name, b := range map[string][]byte{a2aNATSTLSCertKey: certPEM, a2aNATSTLSKeyKey: keyPEM, a2aNATSTLSCAKey: caPEM} {
			if err := os.WriteFile(filepath.Join(dir, name), b, 0o600); err != nil {
				a2aTestTLSErr = err
				return
			}
		}
		pool := x509.NewCertPool()
		pool.AddCert(caCert)
		a2aTestTLSDir, a2aTestTLSPool = dir, pool
	})
	if a2aTestTLSErr != nil {
		t.Fatalf("minting the test bus certificate: %v", a2aTestTLSErr)
	}
	return a2aTestTLSDir, a2aTestTLSPool
}

// a2aWithTestTLS rewrites the conf's serving-pair paths to the package's test
// pair. It fails when the conf names no such path, so the substitution cannot
// go inert under a renamed mount.
func a2aWithTestTLS(t *testing.T, conf string) string {
	t.Helper()
	dir, _ := a2aTestTLSMaterial(t)
	if !strings.Contains(conf, a2aNATSTLSMountDir+"/") {
		t.Fatalf("the rendered nats.conf names nothing under %s; the TLS substitution would be inert", a2aNATSTLSMountDir)
	}
	return strings.ReplaceAll(conf, a2aNATSTLSMountDir+"/", dir+"/")
}

// a2aTestBusTLS is the client option that trusts the test pair's CA.
func a2aTestBusTLS(t *testing.T) nats.Option {
	t.Helper()
	_, pool := a2aTestTLSMaterial(t)
	return nats.Secure(&tls.Config{RootCAs: pool, ServerName: a2aTestServerName, MinVersion: tls.VersionTLS12})
}
