package upstream

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
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"
	"time"

	"golang.org/x/oauth2"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// testReread is longer than both the bearer token file's and the CA bundle's check
	// interval, so advancing the clock by it makes the next request look at the files.
	testReread      = 2 * time.Second
	testCallerToken = "caller-token"
	testLoopback    = "127.0.0.1"
	// testPipeWait is how long a refusal that must not block is waited for.
	testPipeWait = 10 * time.Second
)

var testSerial atomic.Int64

// stepClock is a clock a test advances by hand.
type stepClock struct{ nanos atomic.Int64 }

func newStepClock() *stepClock {
	c := &stepClock{}
	c.nanos.Store(time.Now().UnixNano())
	return c
}

func (c *stepClock) now() time.Time { return time.Unix(0, c.nanos.Load()) }

func (c *stepClock) advance() { c.nanos.Add(int64(testReread)) }

// replaceFile writes data beside path and renames it into place, as an atomic rotation does:
// the file's inode changes and no reader sees it half written.
func replaceFile(t *testing.T, path string, data []byte) {
	t.Helper()
	tmp := path + ".next"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(tmp, path); err != nil {
		t.Fatal(err)
	}
}

// testAuthority is a CA that issues server certificates.
type testAuthority struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func newTestAuthority(t *testing.T, name string) *testAuthority {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(testSerial.Add(1)),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
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
	return &testAuthority{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

// issue returns a server certificate for the loopback address, or for dnsName alone when
// one is given.
func (a *testAuthority) issue(t *testing.T, dnsName string) *tls.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(testSerial.Add(1)),
		Subject:      pkix.Name{CommonName: "upstream"},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	if dnsName != "" {
		tmpl.DNSNames = []string{dnsName}
	} else {
		tmpl.IPAddresses = []net.IP{net.ParseIP(testLoopback)}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, a.cert, &key.PublicKey, a.key)
	if err != nil {
		t.Fatal(err)
	}
	return &tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// issuedServer is a TLS API server that, like kube-apiserver, offers HTTP/2. It presents
// whichever certificate serve last set, and records each request's Authorization header and
// protocol. httptest's TLS server is not used: it always holds a certificate of its own,
// which crypto/tls prefers to GetCertificate for a client that dials an IP address.
type issuedServer struct {
	URL  string
	cert atomic.Pointer[tls.Certificate]

	mu     sync.Mutex
	authz  []string
	protos []string
}

func newIssuedServer(t *testing.T, cert *tls.Certificate) *issuedServer {
	t.Helper()
	s := &issuedServer{}
	s.cert.Store(cert)
	ln, err := net.Listen("tcp", net.JoinHostPort(testLoopback, "0"))
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			s.mu.Lock()
			s.authz = append(s.authz, r.Header.Get("Authorization"))
			s.protos = append(s.protos, r.Proto)
			s.mu.Unlock()
			_, _ = io.WriteString(w, "ok")
		}),
		TLSConfig: &tls.Config{
			MinVersion: tls.VersionTLS12,
			GetCertificate: func(*tls.ClientHelloInfo) (*tls.Certificate, error) {
				return s.cert.Load(), nil
			},
		},
		ReadHeaderTimeout: time.Minute,
		// The tests have the client refuse several handshakes on purpose.
		ErrorLog: log.New(io.Discard, "", 0),
	}
	s.URL = "https://" + ln.Addr().String()
	// Empty file names make the server take certificates from TLSConfig.GetCertificate.
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	t.Cleanup(func() { _ = srv.Close() })
	return s
}

func (s *issuedServer) serve(cert *tls.Certificate) { s.cert.Store(cert) }

func (s *issuedServer) seen() (authz, protos []string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.authz...), append([]string(nil), s.protos...)
}

// staticGet sends a GET for /api through st, with mutate applied to the request first.
func staticGet(st *StaticTransport, mutate func(*http.Request)) error {
	req := httptest.NewRequest(http.MethodGet, "/api", nil)
	if mutate != nil {
		mutate(req)
	}
	resp, err := st.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, req)
	if err != nil {
		return err
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return errors.New("status " + resp.Status)
	}
	return nil
}

// dropConnections closes st's idle connections, so the next request dials and verifies the
// server again.
func dropConnections(t *testing.T, st *StaticTransport) {
	t.Helper()
	for _, rt := range []http.RoundTripper{st.rt, st.upgrade} {
		tr, ok := rt.(*http.Transport)
		if !ok {
			t.Fatalf("round tripper is %T, want *http.Transport", rt)
		}
		tr.CloseIdleConnections()
	}
}

func TestNewServesInClusterWithoutGoogleCredentials(t *testing.T) {
	// A credentials file that does not exist makes the lookup of Application Default
	// Credentials fail at once on any machine, including one whose metadata server or
	// gcloud login would otherwise supply them.
	t.Setenv("GOOGLE_APPLICATION_CREDENTIALS", filepath.Join(t.TempDir(), "absent.json"))

	inCluster := newFakeAPIServer(t, nil)
	tr, err := New(context.Background(), Config{
		InCluster: &rest.Config{
			Host:            inCluster.URL,
			BearerToken:     testSAToken,
			TLSClientConfig: rest.TLSClientConfig{CAData: inCluster.caPEM()},
		},
	})
	if err != nil {
		t.Fatalf("New without Google credentials: %v", err)
	}

	if resp, err := roundTrip(t, tr, k8sapiproxy.InClusterID, "/api"); err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("in-cluster request = %v, %v; want 200", resp, err)
	}

	_, err = roundTrip(t, tr, testClusterID, "/api")
	if err == nil {
		t.Fatal("a gke_ request succeeded without Google credentials")
	}
	if !strings.Contains(err.Error(), adcNotFound) || !strings.Contains(err.Error(), testClusterID) {
		t.Errorf("gke_ request error = %q, want it to name the cluster and %q", err, adcNotFound)
	}

	// The failed gke_ request leaves in-cluster serving.
	if resp, err := roundTrip(t, tr, k8sapiproxy.InClusterID, "/version"); err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("in-cluster request after the gke_ failure = %v, %v; want 200", resp, err)
	}
	want := []string{"Bearer " + testSAToken + " /api", "Bearer " + testSAToken + " /version"}
	if got := inCluster.requests(); strings.Join(got, "|") != strings.Join(want, "|") {
		t.Errorf("in-cluster API server saw %q, want %q", got, want)
	}
}

func TestDefaultCredentialsAreLookedUpOnUseAndRetriedAfterAFailure(t *testing.T) {
	var lookups int
	failing := true
	creds := &defaultCredentials{find: func() (oauth2.TokenSource, error) {
		lookups++
		if failing {
			return nil, errors.New("could not find default credentials")
		}
		return oauth2.StaticTokenSource(&oauth2.Token{AccessToken: testToken}), nil
	}}
	if lookups != 0 {
		t.Fatalf("lookups before the first Token call = %d, want 0", lookups)
	}
	if _, err := creds.Token(); err == nil || !strings.Contains(err.Error(), adcNotFound) {
		t.Fatalf("Token without credentials = %v, want an error naming %q", err, adcNotFound)
	}
	failing = false
	for i := 0; i < 2; i++ {
		tok, err := creds.Token()
		if err != nil || tok.AccessToken != testToken {
			t.Fatalf("Token once credentials exist = %v, %v; want %q", tok, err, testToken)
		}
	}
	if lookups != 2 {
		t.Errorf("lookups = %d, want 2: one failed, one found and kept", lookups)
	}
}

func TestStaticTransportSendsTheRotatingBearerTokenFile(t *testing.T) {
	ca := newTestAuthority(t, "ca")
	srv := newIssuedServer(t, ca.issue(t, ""))
	dir := t.TempDir()
	caFile, tokenFile := filepath.Join(dir, "ca.crt"), filepath.Join(dir, "token")
	replaceFile(t, caFile, ca.pem)
	replaceFile(t, tokenFile, []byte("  token-one\n"))

	clock := newStepClock()
	st, err := newStatic(StaticConfig{URL: srv.URL, CAFile: caFile, BearerTokenFile: tokenFile}, clock.now)
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}

	// The caller's own credential is dropped however its header is spelled.
	withCallerCredential := func(r *http.Request) {
		r.Header.Set("Authorization", "Bearer "+testCallerToken)
		r.Header["authorization"] = []string{"Bearer " + testCallerToken}
	}
	if err := staticGet(st, withCallerCredential); err != nil {
		t.Fatalf("first request: %v", err)
	}

	// Rotated on disk: sent once the file is looked at again, without a restart.
	replaceFile(t, tokenFile, []byte("token-two\n"))
	clock.advance()
	if err := staticGet(st, withCallerCredential); err != nil {
		t.Fatalf("request after rotation: %v", err)
	}
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("request without a caller credential: %v", err)
	}

	authz, _ := srv.seen()
	want := []string{"Bearer token-one", "Bearer token-two", "Bearer token-two"}
	if strings.Join(authz, "|") != strings.Join(want, "|") {
		t.Errorf("upstream saw Authorization %q, want %q", authz, want)
	}
}

func TestStaticTransportFailsClosedWhileTheTokenFileHoldsNoToken(t *testing.T) {
	ca := newTestAuthority(t, "ca")
	srv := newIssuedServer(t, ca.issue(t, ""))
	dir := t.TempDir()
	caFile, tokenFile := filepath.Join(dir, "ca.crt"), filepath.Join(dir, "token")
	replaceFile(t, caFile, ca.pem)
	replaceFile(t, tokenFile, []byte("token-one"))

	clock := newStepClock()
	st, err := newStatic(StaticConfig{URL: srv.URL, CAFile: caFile, BearerTokenFile: tokenFile}, clock.now)
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("first request: %v", err)
	}

	refused := func(what, wantReason string) {
		t.Helper()
		clock.advance()
		err := staticGet(st, nil)
		if err == nil {
			t.Fatalf("%s: the request was sent", what)
		}
		if !strings.Contains(err.Error(), wantReason) || !strings.Contains(err.Error(), tokenFile) {
			t.Errorf("%s: error = %q, want it to name %q and the file", what, err, wantReason)
		}
		for _, held := range []string{"token-one", "split token"} {
			if strings.Contains(err.Error(), held) {
				t.Errorf("%s: error %q carries the token", what, err)
			}
		}
	}

	replaceFile(t, tokenFile, nil)
	refused("emptied file", "is empty")
	replaceFile(t, tokenFile, []byte(" \n\t\n"))
	refused("whitespace-only file", "is empty")
	replaceFile(t, tokenFile, []byte("split token\n"))
	refused("token with a space", "cannot carry")
	replaceFile(t, tokenFile, []byte("split\ntoken\n"))
	refused("token with a line break", "cannot carry")
	if err := os.Remove(tokenFile); err != nil {
		t.Fatal(err)
	}
	refused("removed file", "bearer token file")
	if err := os.Mkdir(tokenFile, 0o700); err != nil {
		t.Fatal(err)
	}
	refused("directory in the file's place", "is not a regular file")
	if err := os.Remove(tokenFile); err != nil {
		t.Fatal(err)
	}

	// None of the refused requests reached the upstream, with the earlier token or without one.
	if authz, _ := srv.seen(); len(authz) != 1 {
		t.Fatalf("upstream saw %q, want only the first request", authz)
	}

	replaceFile(t, tokenFile, []byte("token-three\n"))
	clock.advance()
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("request once the file holds a token again: %v", err)
	}
	if authz, _ := srv.seen(); len(authz) != 2 || authz[1] != "Bearer token-three" {
		t.Errorf("upstream saw %q, want the restored token last", authz)
	}
}

func TestStaticTransportSendsTheTokenFileOverAConnectionVerifiedByTLSClientConfig(t *testing.T) {
	ca, otherCA := newTestAuthority(t, "ca"), newTestAuthority(t, "other-ca")
	srv := newIssuedServer(t, ca.issue(t, ""))
	tokenFile := filepath.Join(t.TempDir(), "token")
	replaceFile(t, tokenFile, []byte("token-one\n"))

	// No CAFile and no custom Transport: the roots come from TLSClientConfig alone.
	rootsOf := func(a *testAuthority) *tls.Config {
		pool := x509.NewCertPool()
		pool.AddCert(a.cert)
		return &tls.Config{RootCAs: pool}
	}
	st, err := NewStatic(StaticConfig{URL: srv.URL, TLSClientConfig: rootsOf(ca), BearerTokenFile: tokenFile})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+testCallerToken) }); err != nil {
		t.Fatalf("request: %v", err)
	}

	// A server whose CA is not among the roots is refused before the token is sent.
	unverified, err := NewStatic(StaticConfig{URL: srv.URL, TLSClientConfig: rootsOf(otherCA), BearerTokenFile: tokenFile})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(unverified, nil); err == nil || !strings.Contains(err.Error(), "x509:") {
		t.Fatalf("request to a server an unknown CA issued = %v, want a certificate error", err)
	}

	if authz, _ := srv.seen(); len(authz) != 1 || authz[0] != "Bearer token-one" {
		t.Errorf("upstream saw Authorization %q, want the file's token once", authz)
	}
}

func TestStaticTransportFollowsAVolumeSymlinkSwapOfTheTokenFile(t *testing.T) {
	ca := newTestAuthority(t, "ca")
	srv := newIssuedServer(t, ca.issue(t, ""))
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	replaceFile(t, caFile, ca.pem)

	// A projected volume: token is a symlink through ..data, which is swapped to the
	// directory holding the new version.
	volume := t.TempDir()
	publish := func(version, token string) {
		t.Helper()
		if err := os.Mkdir(filepath.Join(volume, version), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(volume, version, "token"), []byte(token), 0o600); err != nil {
			t.Fatal(err)
		}
		next := filepath.Join(volume, "..data_tmp")
		if err := os.Symlink(version, next); err != nil {
			t.Fatal(err)
		}
		if err := os.Rename(next, filepath.Join(volume, "..data")); err != nil {
			t.Fatal(err)
		}
	}
	publish("..v1", "token-one\n")
	tokenFile := filepath.Join(volume, "token")
	if err := os.Symlink(filepath.Join("..data", "token"), tokenFile); err != nil {
		t.Fatal(err)
	}

	clock := newStepClock()
	st, err := newStatic(StaticConfig{URL: srv.URL, CAFile: caFile, BearerTokenFile: tokenFile}, clock.now)
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("first request: %v", err)
	}
	publish("..v2", "token-two\n")
	clock.advance()
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("request after the volume swap: %v", err)
	}
	if authz, _ := srv.seen(); strings.Join(authz, "|") != "Bearer token-one|Bearer token-two" {
		t.Errorf("upstream saw Authorization %q, want the token of each volume version", authz)
	}
}

func TestStaticTransportRefusesARedirectWithoutSendingTheTokenOn(t *testing.T) {
	elsewhere := newFakeAPIServer(t, nil)
	srv := newFakeAPIServer(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, elsewhere.URL+"/elsewhere", http.StatusFound)
	})
	tokenFile := filepath.Join(t.TempDir(), "token")
	replaceFile(t, tokenFile, []byte("token-one\n"))
	roots := x509.NewCertPool()
	roots.AddCert(srv.Certificate())

	st, err := NewStatic(StaticConfig{URL: srv.URL, TLSClientConfig: &tls.Config{RootCAs: roots}, BearerTokenFile: tokenFile})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, nil); err == nil || !strings.Contains(err.Error(), "redirect") {
		t.Fatalf("request answered with a redirect = %v, want it refused as a redirect", err)
	}
	if got := srv.requests(); len(got) != 1 || got[0] != "Bearer token-one /api" {
		t.Errorf("upstream saw %q, want the one request with the file's token", got)
	}
	if got := elsewhere.requests(); len(got) != 0 {
		t.Errorf("the redirect target saw %q, want nothing: the token must not follow a redirect", got)
	}
}

// recordingRoundTripper answers every request itself and keeps its Authorization header.
type recordingRoundTripper struct {
	mu    sync.Mutex
	authz []string
}

func (r *recordingRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	r.mu.Lock()
	r.authz = append(r.authz, req.Header.Get("Authorization"))
	r.mu.Unlock()
	return &http.Response{StatusCode: http.StatusOK, Status: "200 OK", Header: http.Header{}, Body: io.NopCloser(strings.NewReader("ok")), Request: req}, nil
}

func TestStaticTransportSendsTheBearerTokenFileThroughACustomTransport(t *testing.T) {
	tokenFile := filepath.Join(t.TempDir(), "token")
	replaceFile(t, tokenFile, []byte("token-one\n"))
	rt := &recordingRoundTripper{}
	st, err := NewStatic(StaticConfig{URL: "https://upstream.example:6443", Transport: rt, BearerTokenFile: tokenFile})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+testCallerToken) }); err != nil {
		t.Fatalf("request: %v", err)
	}
	if len(rt.authz) != 1 || rt.authz[0] != "Bearer token-one" {
		t.Errorf("custom transport saw Authorization %q, want the file's token", rt.authz)
	}
}

func TestNewStaticRefusesAnUnusableBearerTokenFileOrCAFile(t *testing.T) {
	ca := newTestAuthority(t, "ca")
	dir := t.TempDir()
	tokenFile, caFile := filepath.Join(dir, "token"), filepath.Join(dir, "ca.crt")
	emptyFile, absentFile := filepath.Join(dir, "empty"), filepath.Join(dir, "absent")
	garbageFile, largeFile := filepath.Join(dir, "garbage"), filepath.Join(dir, "large")
	replaceFile(t, tokenFile, []byte("token-one\n"))
	replaceFile(t, caFile, ca.pem)
	replaceFile(t, emptyFile, nil)
	replaceFile(t, garbageFile, []byte("not a certificate\n"))
	replaceFile(t, largeFile, []byte(strings.Repeat("a", maxBearerTokenFileBytes+1)))

	const httpsURL = "https://127.0.0.1:6443"
	for name, tc := range map[string]struct {
		cfg  StaticConfig
		want string
	}{
		"token file over http":               {StaticConfig{URL: "http://upstream.example:8080", BearerTokenFile: tokenFile}, "bearer token file"},
		"token file over loopback http":      {StaticConfig{URL: "http://127.0.0.1:8080", BearerTokenFile: tokenFile}, "bearer token file"},
		"token file over IPv6 loopback http": {StaticConfig{URL: "http://[::1]:8080", BearerTokenFile: tokenFile}, "bearer token file"},
		"token file over http with a custom transport": {
			StaticConfig{URL: "http://127.0.0.1:8080", BearerTokenFile: tokenFile, Transport: &recordingRoundTripper{}}, "bearer token file",
		},
		"absent token file":       {StaticConfig{URL: httpsURL, BearerTokenFile: absentFile}, "bearer token file"},
		"empty token file":        {StaticConfig{URL: httpsURL, BearerTokenFile: emptyFile}, "is empty"},
		"directory as token file": {StaticConfig{URL: httpsURL, BearerTokenFile: dir}, "is not a regular file"},
		"token file without server verification": {
			StaticConfig{URL: httpsURL, BearerTokenFile: tokenFile, TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}, "InsecureSkipVerify",
		},
		"CA file without server verification": {
			StaticConfig{URL: httpsURL, CAFile: caFile, TLSClientConfig: &tls.Config{InsecureSkipVerify: true}}, "InsecureSkipVerify",
		},
		"oversized token file": {StaticConfig{URL: httpsURL, BearerTokenFile: largeFile}, "larger than"},
		"CA file over http":    {StaticConfig{URL: "http://127.0.0.1:8080", CAFile: caFile}, "CA file"},
		"absent CA file":       {StaticConfig{URL: httpsURL, CAFile: absentFile}, "CA file"},
		"empty CA file":        {StaticConfig{URL: httpsURL, CAFile: emptyFile}, "CA file"},
		"CA file without PEM":  {StaticConfig{URL: httpsURL, CAFile: garbageFile}, "CA file"},
		"CA file with a custom transport": {
			StaticConfig{URL: httpsURL, CAFile: caFile, Transport: &recordingRoundTripper{}}, "custom Transport",
		},
	} {
		_, err := NewStatic(tc.cfg)
		if err == nil {
			t.Errorf("%s: NewStatic succeeded, want an error", name)
			continue
		}
		if !strings.Contains(err.Error(), tc.want) {
			t.Errorf("%s: error = %q, want substring %q", name, err, tc.want)
		}
	}

	if _, err := NewStatic(StaticConfig{URL: httpsURL, BearerTokenFile: tokenFile, CAFile: caFile}); err != nil {
		t.Errorf("NewStatic with a token file and a CA file over https: %v", err)
	}
}

func TestNewStaticRefusesANamedPipeAsBearerTokenFile(t *testing.T) {
	pipe := filepath.Join(t.TempDir(), "token")
	if err := syscall.Mkfifo(pipe, 0o600); err != nil {
		t.Skipf("no named pipes here: %v", err)
	}
	// Opening a pipe nobody writes to would block; the refusal has to come before that.
	done := make(chan error, 1)
	go func() {
		_, err := NewStatic(StaticConfig{URL: "https://127.0.0.1:6443", BearerTokenFile: pipe})
		done <- err
	}()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "is not a regular file") {
			t.Errorf("NewStatic with a named pipe = %v, want it refused as not a regular file", err)
		}
	case <-time.After(testPipeWait):
		t.Fatal("NewStatic blocked on a named pipe")
	}
}

func TestStaticTransportCAFileTrustsARotatedCAOnTheNextConnection(t *testing.T) {
	oldCA, newCA := newTestAuthority(t, "old-ca"), newTestAuthority(t, "new-ca")
	srv := newIssuedServer(t, oldCA.issue(t, ""))
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	replaceFile(t, caFile, oldCA.pem)

	clock := newStepClock()
	var reloadErrors atomic.Int32
	st, err := newStatic(StaticConfig{
		URL:             srv.URL,
		CAFile:          caFile,
		OnCAReloadError: func(error) { reloadErrors.Add(1) },
	}, clock.now)
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("request to a server the bundle's CA issued: %v", err)
	}

	// The server moves to a certificate from a CA the bundle does not hold yet.
	srv.serve(newCA.issue(t, ""))
	dropConnections(t, st)
	clock.advance()
	if err := staticGet(st, nil); err == nil || !strings.Contains(err.Error(), "x509:") {
		t.Fatalf("request to a server an unknown CA issued = %v, want a certificate error", err)
	}

	// A bundle caught mid-write is not loaded: the previous one stays in force.
	replaceFile(t, caFile, newCA.pem[:len(newCA.pem)/2])
	clock.advance()
	if err := staticGet(st, nil); err == nil || !strings.Contains(err.Error(), "x509:") {
		t.Fatalf("request with a truncated bundle on disk = %v, want a certificate error", err)
	}
	if got := reloadErrors.Load(); got != 1 {
		t.Errorf("OnCAReloadError calls after a bundle that does not load = %d, want 1", got)
	}

	// The rotated bundle is trusted by the next connection, without a restart.
	replaceFile(t, caFile, newCA.pem)
	clock.advance()
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("request after the bundle rotated to the new CA: %v", err)
	}

	// And the CA it no longer holds is refused again.
	srv.serve(oldCA.issue(t, ""))
	dropConnections(t, st)
	clock.advance()
	if err := staticGet(st, nil); err == nil || !strings.Contains(err.Error(), "x509:") {
		t.Fatalf("request to a server the removed CA issued = %v, want a certificate error", err)
	}

	if authz, _ := srv.seen(); len(authz) != 2 {
		t.Errorf("upstream served %d requests, want the 2 made over verified connections", len(authz))
	}
	if got := reloadErrors.Load(); got != 1 {
		t.Errorf("OnCAReloadError calls = %d, want only the one for the truncated bundle", got)
	}
}

func TestStaticTransportCAFileKeepsHostnameVerification(t *testing.T) {
	ca := newTestAuthority(t, "ca")
	// Issued by the trusted CA, but for another name than the address being dialed.
	srv := newIssuedServer(t, ca.issue(t, "other.example"))
	caFile := filepath.Join(t.TempDir(), "ca.crt")
	replaceFile(t, caFile, ca.pem)

	st, err := NewStatic(StaticConfig{URL: srv.URL, CAFile: caFile})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	asUpgrade := func(r *http.Request) {
		r.Header.Set("Connection", "Upgrade")
		r.Header.Set("Upgrade", "SPDY/3.1")
	}
	for name, mutate := range map[string]func(*http.Request){"request": nil, "upgrade request": asUpgrade} {
		err := staticGet(st, mutate)
		if err == nil || !strings.Contains(err.Error(), "x509:") || !strings.Contains(err.Error(), testLoopback) {
			t.Errorf("%s to a server whose certificate names another host = %v, want a certificate error naming %s", name, err, testLoopback)
		}
	}
	if authz, _ := srv.seen(); len(authz) != 0 {
		t.Errorf("upstream served %d requests over an unverified connection", len(authz))
	}
}

func TestStaticTransportCAFileKeepsHTTP2AndHTTP1Upgrades(t *testing.T) {
	ca := newTestAuthority(t, "ca")
	srv := newIssuedServer(t, ca.issue(t, ""))
	dir := t.TempDir()
	caFile, tokenFile := filepath.Join(dir, "ca.crt"), filepath.Join(dir, "token")
	replaceFile(t, caFile, ca.pem)
	replaceFile(t, tokenFile, []byte("token-one\n"))

	st, err := NewStatic(StaticConfig{URL: srv.URL, CAFile: caFile, BearerTokenFile: tokenFile})
	if err != nil {
		t.Fatalf("NewStatic: %v", err)
	}
	if err := staticGet(st, nil); err != nil {
		t.Fatalf("request: %v", err)
	}
	// A request that asks for an upgrade goes over its own HTTP/1.1 connection, verified
	// against the same bundle and carrying the same token.
	err = staticGet(st, func(r *http.Request) {
		r.Header.Set("Connection", "Upgrade")
		r.Header.Set("Upgrade", "SPDY/3.1")
	})
	if err != nil {
		t.Fatalf("upgrade request: %v", err)
	}
	authz, protos := srv.seen()
	if strings.Join(protos, "|") != "HTTP/2.0|HTTP/1.1" {
		t.Errorf("protocols = %v, want [HTTP/2.0 HTTP/1.1]", protos)
	}
	if strings.Join(authz, "|") != "Bearer token-one|Bearer token-one" {
		t.Errorf("upstream saw Authorization %q, want the file's token on both", authz)
	}
}
