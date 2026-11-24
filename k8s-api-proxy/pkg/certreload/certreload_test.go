package certreload

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const (
	testInterval  = time.Minute
	testValidity  = time.Hour
	testHost      = "localhost"
	testOtherHost = "other.test"
	certName      = "tls.crt"
	keyName       = "tls.key"
	bundleName    = "bundle.pem"
	caName        = "ca.crt"
	dataLink      = "..data"
	pemTypeKey    = "PRIVATE KEY"
)

var (
	testEpoch    = time.Date(2026, 11, 20, 12, 0, 0, 0, time.UTC)
	serialNumber atomic.Int64
	// mtimeTick gives every write a distinct modification time: a filesystem's timestamp
	// granularity could otherwise make two quick same-size writes look unchanged.
	mtimeTick atomic.Int64
)

// fakeClock is the reloader's clock; advance moves it past the check interval.
type fakeClock struct{ nanos atomic.Int64 }

func newClock() *fakeClock {
	c := &fakeClock{}
	c.nanos.Store(testEpoch.UnixNano())
	return c
}

func (c *fakeClock) now() time.Time { return time.Unix(0, c.nanos.Load()) }

func (c *fakeClock) advance() { c.nanos.Add(int64(testInterval) + 1) }

// errorLog records the errors a reloader reports.
type errorLog struct {
	mu   sync.Mutex
	errs []error
}

func (l *errorLog) record(err error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.errs = append(l.errs, err)
}

func (l *errorLog) count() int {
	l.mu.Lock()
	defer l.mu.Unlock()
	return len(l.errs)
}

func testOptions(clock *fakeClock, log *errorLog) Options {
	return Options{CheckInterval: testInterval, OnReloadError: log.record, Now: clock.now}
}

type authority struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func nextSerial() *big.Int { return big.NewInt(serialNumber.Add(1)) }

func newAuthority(t *testing.T, name string) *authority {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          nextSerial(),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-testValidity),
		NotAfter:              time.Now().Add(testValidity),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &authority{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: pemTypeCertificate, Bytes: der})}
}

// leaf is a server certificate the authority signed, with its PEM encodings.
type leaf struct {
	serial  int64
	certPEM []byte
	keyPEM  []byte
}

// issue signs a server certificate for dnsName, and for 127.0.0.1 when withIP is set.
func (a *authority) issue(t *testing.T, dnsName string, withIP bool) leaf {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serial := nextSerial()
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: dnsName},
		DNSNames:     []string{dnsName},
		NotBefore:    time.Now().Add(-testValidity),
		NotAfter:     time.Now().Add(testValidity),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	if withIP {
		tmpl.IPAddresses = []net.IP{net.IPv4(127, 0, 0, 1)}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, a.cert, &key.PublicKey, a.key)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	return leaf{
		serial:  serial.Int64(),
		certPEM: pem.EncodeToMemory(&pem.Block{Type: pemTypeCertificate, Bytes: der}),
		keyPEM:  pem.EncodeToMemory(&pem.Block{Type: pemTypeKey, Bytes: keyDER}),
	}
}

func (l leaf) bundle() []byte { return append(append([]byte{}, l.certPEM...), l.keyPEM...) }

func (l leaf) tlsCertificate(t *testing.T) tls.Certificate {
	t.Helper()
	cert, err := tls.X509KeyPair(l.certPEM, l.keyPEM)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

// writeFile writes data in place and gives the file a modification time no other write had.
func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	mtime := testEpoch.Add(time.Duration(mtimeTick.Add(1)) * time.Second)
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

// serveTLS serves TLS with cfg on a loopback port and returns its address.
func serveTLS(t *testing.T, cfg *tls.Config) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler:           http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}),
		ReadHeaderTimeout: testInterval,
	}
	go func() { _ = srv.Serve(tls.NewListener(ln, cfg)) }()
	t.Cleanup(func() { _ = srv.Close() })
	return ln.Addr().String()
}

// servedSerial opens a new TLS connection to addr and returns the leaf's serial number.
func servedSerial(t *testing.T, addr string, roots *x509.CertPool) int64 {
	t.Helper()
	conn, err := tls.Dial("tcp", addr, &tls.Config{RootCAs: roots, ServerName: testHost, MinVersion: tls.VersionTLS12})
	if err != nil {
		t.Fatalf("TLS handshake: %v", err)
	}
	defer func() { _ = conn.Close() }()
	return conn.ConnectionState().PeerCertificates[0].SerialNumber.Int64()
}

// startServer serves sc's certificate and returns the address and a pool trusting ca.
func startServer(t *testing.T, sc *ServerCertificate, ca *authority) (string, *x509.CertPool) {
	t.Helper()
	roots := x509.NewCertPool()
	roots.AddCert(ca.cert)
	return serveTLS(t, &tls.Config{GetCertificate: sc.GetCertificate, MinVersion: tls.VersionTLS12}), roots
}

func TestServerCertificateReloadsRotatedSeparateFiles(t *testing.T) {
	ca := newAuthority(t, "server-ca")
	first, second := ca.issue(t, testHost, false), ca.issue(t, testHost, false)
	dir := t.TempDir()
	certFile, keyFile := filepath.Join(dir, certName), filepath.Join(dir, keyName)
	writeFile(t, certFile, first.certPEM)
	writeFile(t, keyFile, first.keyPEM)

	clock, log := newClock(), &errorLog{}
	sc, err := NewServerCertificate(certFile, keyFile, testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewServerCertificate: %v", err)
	}
	addr, roots := startServer(t, sc, ca)
	if got := servedSerial(t, addr, roots); got != first.serial {
		t.Fatalf("served serial %d, want the first certificate's %d", got, first.serial)
	}

	writeFile(t, certFile, second.certPEM)
	writeFile(t, keyFile, second.keyPEM)
	if got := servedSerial(t, addr, roots); got != first.serial {
		t.Errorf("served serial %d within the check interval, want the first certificate's %d", got, first.serial)
	}
	clock.advance()
	if got := servedSerial(t, addr, roots); got != second.serial {
		t.Errorf("served serial %d after rotation, want the second certificate's %d", got, second.serial)
	}
	if n := log.count(); n != 0 {
		t.Errorf("%d reload errors reported, want none", n)
	}
}

func TestServerCertificateReloadsACombinedPEMFile(t *testing.T) {
	for name, keyFileFor := range map[string]func(bundle string) string{
		"no key file":             func(string) string { return "" },
		"key file is cert's path": func(bundle string) string { return bundle },
	} {
		t.Run(name, func(t *testing.T) {
			ca := newAuthority(t, "server-ca")
			first, second := ca.issue(t, testHost, false), ca.issue(t, testHost, false)
			bundle := filepath.Join(t.TempDir(), bundleName)
			writeFile(t, bundle, first.bundle())

			clock, log := newClock(), &errorLog{}
			sc, err := NewServerCertificate(bundle, keyFileFor(bundle), testOptions(clock, log))
			if err != nil {
				t.Fatalf("NewServerCertificate: %v", err)
			}
			addr, roots := startServer(t, sc, ca)
			if got := servedSerial(t, addr, roots); got != first.serial {
				t.Fatalf("served serial %d, want %d", got, first.serial)
			}

			writeFile(t, bundle, second.bundle())
			clock.advance()
			if got := servedSerial(t, addr, roots); got != second.serial {
				t.Errorf("served serial %d after rotation, want %d", got, second.serial)
			}
		})
	}
}

// swapData points dir/..data at a new timestamped directory holding files, the way a
// Kubernetes Secret, ConfigMap or projected volume updates: dir/<name> is a symlink to
// ..data/<name>, and ..data is replaced by an atomic rename of a new symlink.
func swapData(t *testing.T, dir, version string, files map[string][]byte) {
	t.Helper()
	versionDir := filepath.Join(dir, version)
	if err := os.Mkdir(versionDir, 0o700); err != nil {
		t.Fatal(err)
	}
	for name, data := range files {
		writeFile(t, filepath.Join(versionDir, name), data)
		link := filepath.Join(dir, name)
		if _, err := os.Lstat(link); errors.Is(err, os.ErrNotExist) {
			if err := os.Symlink(filepath.Join(dataLink, name), link); err != nil {
				t.Fatal(err)
			}
		}
	}
	tmp := filepath.Join(dir, dataLink+"_tmp")
	if err := os.Symlink(version, tmp); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, filepath.Join(dir, dataLink)); err != nil {
		t.Fatal(err)
	}
}

func TestServerCertificateFollowsAVolumeSymlinkSwap(t *testing.T) {
	ca := newAuthority(t, "server-ca")
	first, second := ca.issue(t, testHost, false), ca.issue(t, testHost, false)
	dir := t.TempDir()
	swapData(t, dir, "..2026_11_20_12_00_00.1", map[string][]byte{certName: first.certPEM, keyName: first.keyPEM})

	clock, log := newClock(), &errorLog{}
	sc, err := NewServerCertificate(filepath.Join(dir, certName), filepath.Join(dir, keyName), testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewServerCertificate: %v", err)
	}
	addr, roots := startServer(t, sc, ca)
	if got := servedSerial(t, addr, roots); got != first.serial {
		t.Fatalf("served serial %d, want %d", got, first.serial)
	}

	swapData(t, dir, "..2026_11_20_13_00_00.2", map[string][]byte{certName: second.certPEM, keyName: second.keyPEM})
	clock.advance()
	if got := servedSerial(t, addr, roots); got != second.serial {
		t.Errorf("served serial %d after the swap, want %d", got, second.serial)
	}
	if n := log.count(); n != 0 {
		t.Errorf("%d reload errors reported, want none", n)
	}
}

func TestServerCertificateKeepsTheLastGoodCertificate(t *testing.T) {
	ca := newAuthority(t, "server-ca")
	first, second := ca.issue(t, testHost, false), ca.issue(t, testHost, false)
	dir := t.TempDir()
	certFile, keyFile := filepath.Join(dir, certName), filepath.Join(dir, keyName)
	writeFile(t, certFile, first.certPEM)
	writeFile(t, keyFile, first.keyPEM)

	clock, log := newClock(), &errorLog{}
	sc, err := NewServerCertificate(certFile, keyFile, testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewServerCertificate: %v", err)
	}
	addr, roots := startServer(t, sc, ca)

	steps := []struct {
		name      string
		cert, key []byte
	}{
		{"empty certificate file", []byte{}, first.keyPEM},
		{"truncated certificate", second.certPEM[:len(second.certPEM)/2], first.keyPEM},
		{"certificate without its key", second.certPEM, first.keyPEM},
		{"corrupt key", second.certPEM, []byte("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")},
	}
	for i, step := range steps {
		writeFile(t, certFile, step.cert)
		writeFile(t, keyFile, step.key)
		clock.advance()
		if got := servedSerial(t, addr, roots); got != first.serial {
			t.Errorf("%s: served serial %d, want the last good certificate's %d", step.name, got, first.serial)
		}
		if n := log.count(); n != i+1 {
			t.Errorf("%s: %d reload errors reported, want %d", step.name, n, i+1)
		}
	}

	// The same bad version is read again on the next check but reported only once.
	clock.advance()
	if got := servedSerial(t, addr, roots); got != first.serial {
		t.Errorf("served serial %d, want %d", got, first.serial)
	}
	if n := log.count(); n != len(steps) {
		t.Errorf("%d reload errors reported, want %d: an unchanged bad version is reported once", n, len(steps))
	}

	writeFile(t, keyFile, second.keyPEM)
	clock.advance()
	if got := servedSerial(t, addr, roots); got != second.serial {
		t.Errorf("served serial %d once the rotation completed, want %d", got, second.serial)
	}
}

func TestServerCertificateKeepsServingWhenAFileDisappears(t *testing.T) {
	ca := newAuthority(t, "server-ca")
	first := ca.issue(t, testHost, false)
	bundle := filepath.Join(t.TempDir(), bundleName)
	writeFile(t, bundle, first.bundle())

	clock, log := newClock(), &errorLog{}
	sc, err := NewServerCertificate(bundle, "", testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewServerCertificate: %v", err)
	}
	addr, roots := startServer(t, sc, ca)
	if err := os.Remove(bundle); err != nil {
		t.Fatal(err)
	}
	clock.advance()
	if got := servedSerial(t, addr, roots); got != first.serial {
		t.Errorf("served serial %d, want %d", got, first.serial)
	}
	if n := log.count(); n != 1 {
		t.Errorf("%d reload errors reported, want 1", n)
	}
}

func TestNewServerCertificateFailsFast(t *testing.T) {
	ca := newAuthority(t, "server-ca")
	good, other := ca.issue(t, testHost, false), ca.issue(t, testHost, false)
	for name, tc := range map[string]struct {
		cert, key []byte // nil leaves the file absent
		combined  bool
	}{
		"missing certificate file":   {cert: nil, key: good.keyPEM},
		"missing key file":           {cert: good.certPEM, key: nil},
		"corrupt certificate":        {cert: good.certPEM[:len(good.certPEM)/2], key: good.keyPEM},
		"mismatched key":             {cert: good.certPEM, key: other.keyPEM},
		"combined file without key":  {cert: good.certPEM, combined: true},
		"combined file without cert": {cert: good.keyPEM, combined: true},
	} {
		t.Run(name, func(t *testing.T) {
			dir := t.TempDir()
			certFile, keyFile := filepath.Join(dir, certName), filepath.Join(dir, keyName)
			if tc.cert != nil {
				writeFile(t, certFile, tc.cert)
			}
			if tc.key != nil {
				writeFile(t, keyFile, tc.key)
			}
			if tc.combined {
				keyFile = ""
			}
			if _, err := NewServerCertificate(certFile, keyFile, Options{}); err == nil {
				t.Error("NewServerCertificate succeeded, want an error")
			}
		})
	}
	if _, err := NewServerCertificate("", "", Options{}); err == nil {
		t.Error("NewServerCertificate with no file succeeded, want an error")
	}
}

// caClient returns a client whose transport trusts pool.
func caClient(t *testing.T, pool *CAPool) (*http.Client, *http.Transport) {
	t.Helper()
	transport := pool.Transport(nil)
	t.Cleanup(transport.CloseIdleConnections)
	return &http.Client{Transport: transport, Timeout: testInterval}, transport
}

// httpsServer serves cert on 127.0.0.1.
func httpsServer(t *testing.T, cert tls.Certificate) *httptest.Server {
	t.Helper()
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	}))
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv
}

func get(client *http.Client, url string) error {
	req, err := http.NewRequestWithContext(context.Background(), http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	return resp.Body.Close()
}

func TestCAPoolTrustsARotatedCAOnTheNextConnection(t *testing.T) {
	oldCA, newCA := newAuthority(t, "old-ca"), newAuthority(t, "new-ca")
	srv := httpsServer(t, newCA.issue(t, testHost, true).tlsCertificate(t))
	caFile := filepath.Join(t.TempDir(), caName)
	writeFile(t, caFile, oldCA.pem)

	clock, log := newClock(), &errorLog{}
	pool, err := NewCAPool(caFile, testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewCAPool: %v", err)
	}
	client, transport := caClient(t, pool)

	var unknown x509.UnknownAuthorityError
	if err := get(client, srv.URL); !errors.As(err, &unknown) {
		t.Fatalf("GET with only the old CA trusted: err = %v, want an unknown authority error", err)
	}

	writeFile(t, caFile, newCA.pem)
	clock.advance()
	if err := get(client, srv.URL); err != nil {
		t.Fatalf("GET after the CA rotated: %v", err)
	}

	// Rotating the new CA out again rejects the server on the next connection.
	writeFile(t, caFile, oldCA.pem)
	clock.advance()
	transport.CloseIdleConnections()
	if err := get(client, srv.URL); !errors.As(err, &unknown) {
		t.Errorf("GET after the CA was rotated out: err = %v, want an unknown authority error", err)
	}
	if n := log.count(); n != 0 {
		t.Errorf("%d reload errors reported, want none", n)
	}
}

func TestCAPoolKeepsHostnameVerification(t *testing.T) {
	ca := newAuthority(t, "ca")
	// Valid for another host only, not for the 127.0.0.1 the client dials.
	srv := httpsServer(t, ca.issue(t, testOtherHost, false).tlsCertificate(t))
	caFile := filepath.Join(t.TempDir(), caName)
	writeFile(t, caFile, ca.pem)

	pool, err := NewCAPool(caFile, testOptions(newClock(), &errorLog{}))
	if err != nil {
		t.Fatalf("NewCAPool: %v", err)
	}
	client, _ := caClient(t, pool)
	var hostname x509.HostnameError
	if err := get(client, srv.URL); !errors.As(err, &hostname) {
		t.Errorf("GET to a host the certificate does not name: err = %v, want a hostname error", err)
	}
}

func TestCAPoolKeepsTheLastGoodBundle(t *testing.T) {
	ca, otherCA := newAuthority(t, "ca"), newAuthority(t, "other-ca")
	srv := httpsServer(t, ca.issue(t, testHost, true).tlsCertificate(t))
	caFile := filepath.Join(t.TempDir(), caName)
	writeFile(t, caFile, ca.pem)

	clock, log := newClock(), &errorLog{}
	pool, err := NewCAPool(caFile, testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewCAPool: %v", err)
	}
	client, transport := caClient(t, pool)

	// A bundle cut off mid-write would otherwise drop the CA it was cut in.
	truncated := append(append([]byte{}, otherCA.pem...), ca.pem[:len(ca.pem)/2]...)
	for i, data := range [][]byte{{}, truncated, []byte("not PEM")} {
		writeFile(t, caFile, data)
		clock.advance()
		transport.CloseIdleConnections()
		if err := get(client, srv.URL); err != nil {
			t.Errorf("GET after bad bundle %d: %v, want the last good bundle still trusted", i, err)
		}
		if n := log.count(); n != i+1 {
			t.Errorf("bad bundle %d: %d reload errors reported, want %d", i, n, i+1)
		}
	}
}

func TestNewCAPoolFailsFast(t *testing.T) {
	ca := newAuthority(t, "ca")
	for name, data := range map[string][]byte{
		"empty":           {},
		"no certificate":  []byte("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"),
		"truncated":       ca.pem[:len(ca.pem)/2],
		"bad certificate": []byte("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"),
		"trailing bytes":  append(append([]byte{}, ca.pem...), []byte("garbage")...),
		// pem.Decode skips an undecodable block and returns the next good one.
		"corrupt block before a good one":      append([]byte("-----BEGIN CERTIFICATE-----\n!corrupt!\n-----END CERTIFICATE-----\n"), ca.pem...),
		"damaged BEGIN line before a good one": append([]byte("----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"), ca.pem...),
		"lost head before a good one":          append([]byte("AAAA\n-----END CERTIFICATE-----\n"), ca.pem...),
	} {
		t.Run(name, func(t *testing.T) {
			caFile := filepath.Join(t.TempDir(), caName)
			writeFile(t, caFile, data)
			if _, err := NewCAPool(caFile, Options{}); err == nil {
				t.Error("NewCAPool succeeded, want an error")
			}
		})
	}
	if _, err := NewCAPool(filepath.Join(t.TempDir(), caName), Options{}); err == nil {
		t.Error("NewCAPool with a missing file succeeded, want an error")
	}
	if _, err := NewCAPool("", Options{}); err == nil {
		t.Error("NewCAPool with no file succeeded, want an error")
	}
}

// Comment lines between blocks, a BEGIN marker inside one included, are not blocks.
func TestNewCAPoolAllowsCommentLinesBetweenBlocks(t *testing.T) {
	ca := newAuthority(t, "ca")
	other := newAuthority(t, "other")
	data := append([]byte("# rotated bundle, each -----BEGIN CERTIFICATE----- is one root\n\n"), ca.pem...)
	data = append(append(data, []byte("\n# second root\n")...), other.pem...)
	caFile := filepath.Join(t.TempDir(), caName)
	writeFile(t, caFile, data)
	if _, err := NewCAPool(caFile, Options{}); err != nil {
		t.Fatalf("NewCAPool with comment lines: %v", err)
	}
}

func TestCAPoolDialerVerifiesAgainstBaseServerName(t *testing.T) {
	ca := newAuthority(t, "ca")
	srv := httpsServer(t, ca.issue(t, testOtherHost, false).tlsCertificate(t))
	caFile := filepath.Join(t.TempDir(), caName)
	writeFile(t, caFile, ca.pem)
	pool, err := NewCAPool(caFile, testOptions(newClock(), &errorLog{}))
	if err != nil {
		t.Fatalf("NewCAPool: %v", err)
	}
	// A base ServerName is kept, so a certificate naming that host verifies at an IP.
	transport := pool.Transport(&tls.Config{ServerName: testOtherHost, MinVersion: tls.VersionTLS12})
	defer transport.CloseIdleConnections()
	if err := get(&http.Client{Transport: transport, Timeout: testInterval}, srv.URL); err != nil {
		t.Errorf("GET with base ServerName %q: %v", testOtherHost, err)
	}
}

// A failure that clears up by itself, the file returning unchanged, does not silence the
// next identical failure: it is reported again.
func TestServerCertificateReportsARepeatedFailureAfterRecovery(t *testing.T) {
	ca := newAuthority(t, "server-ca")
	first := ca.issue(t, testHost, false)
	dir := t.TempDir()
	bundle, aside := filepath.Join(dir, bundleName), filepath.Join(dir, bundleName+".aside")
	writeFile(t, bundle, first.bundle())

	clock, log := newClock(), &errorLog{}
	sc, err := NewServerCertificate(bundle, bundle, testOptions(clock, log))
	if err != nil {
		t.Fatalf("NewServerCertificate: %v", err)
	}
	addr, roots := startServer(t, sc, ca)

	// A rename keeps the inode, size and mtime, so the restored file is the loaded version.
	for i, path := range []string{aside, bundle, aside} {
		from := bundle
		if path == bundle {
			from = aside
		}
		if err := os.Rename(from, path); err != nil {
			t.Fatal(err)
		}
		clock.advance()
		if got := servedSerial(t, addr, roots); got != first.serial {
			t.Errorf("step %d: served serial %d, want %d", i, got, first.serial)
		}
	}
	if n := log.count(); n != 2 {
		t.Errorf("%d reload errors reported, want 2: the file went missing twice", n)
	}
}
