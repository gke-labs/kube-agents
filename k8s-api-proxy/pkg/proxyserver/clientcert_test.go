package proxyserver

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// testCAInterval is the client CA bundles' check interval, on the test's clock.
	testCAInterval = time.Minute
	testCertLife   = 24 * time.Hour
	// testShortCertLife is the validity of a certificate a test lets expire: longer than
	// the check interval, far shorter than a CA's.
	testShortCertLife = 10 * time.Minute

	testWorkloadURI      = "spiffe://example.test/ns/agents/sa/sandbox"
	testOtherWorkloadURI = "spiffe://example.test/ns/agents/sa/other"
	testOutsideURI       = "spiffe://example.test/ns/elsewhere/sa/sandbox"
	testURIPrefix        = "spiffe://example.test/ns/agents/"
	testWorkloadDNS      = "sandbox.agents.example.test"
	testOtherDNS         = "other.agents.example.test"
	testWorkloadCN       = "sandbox"
	testOtherCN          = "other"
	testGatewayURI       = "spiffe://example.test/ns/gateways/sa/egress"
	testUnlistedGateway  = "spiffe://example.test/ns/gateways/sa/unlisted"

	testUserHeader   = "X-Caller-User"
	testUIDHeader    = "X-Caller-Uid"
	testGroupHeader  = "X-Caller-Group"
	testTenantHeader = "X-Caller-Tenant"
	testAlice        = "alice"
	testBob          = "bob"
	testCarol        = "carol"
	testAliceUID     = "uid-alice"
	testGroupOne     = "developers"
	testGroupTwo     = "on-call"
	testJWTSubject   = "agent-1"

	testLoopbackIP      = "127.0.0.1"
	testLoopbackAnyPort = testLoopbackIP + ":0"
	// testRecorderPeerIP is the peer address httptest.NewRequest gives a request, which
	// testCallerPod names as the pod's; a request to a real listener comes from loopback.
	testRecorderPeerIP  = "192.0.2.1"
	testHTTP2           = 2
	testPEMCertificate  = "CERTIFICATE"
	testPEMKey          = "EC PRIVATE KEY"
	testAuthorization   = "Authorization"
	testBearerPrefix    = "Bearer "
	testReloadFailures2 = "kubeagents_kube_proxy_certificate_reload_failures_total 2"
	testNotABundle      = "not a PEM bundle"
)

var (
	testSerial atomic.Int64
	// testMtime gives every bundle write a distinct modification time.
	testMtime atomic.Int64
	// libraryNames are the Go names of pkg/mtlsauth that no startup error may carry.
	libraryNames = []string{"mtlsauth", "ClientCertConfig", "FrontProxyConfig", "IdentitySource", "NameSource", "UsernameHeaders", "UIDHeaders", "GroupHeaders", "PartitionHeaders", "AllowedNames", "AllowedIdentities", "RequiredURIPrefix", "ClientCAFile"}
)

// testClock is the time client certificates are verified at and the clock the client CA
// bundles are reloaded on.
type testClock struct{ nanos atomic.Int64 }

func newTestClock() *testClock {
	c := &testClock{}
	c.nanos.Store(time.Now().UnixNano())
	return c
}

func (c *testClock) now() time.Time          { return time.Unix(0, c.nanos.Load()) }
func (c *testClock) advance(d time.Duration) { c.nanos.Add(int64(d)) }

// passCheckInterval moves the clock far enough for a changed bundle to be read.
func (c *testClock) passCheckInterval() { c.advance(testCAInterval + time.Nanosecond) }

// lockedBuffer is a log sink handlers on several connections write to.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

type testCA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func newTestKey(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

func newTestCA(t *testing.T, name string) *testCA {
	t.Helper()
	key := newTestKey(t)
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(testSerial.Add(1)),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-testCertLife),
		NotAfter:              time.Now().Add(testCertLife),
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
	return &testCA{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: testPEMCertificate, Bytes: der})}
}

// leafSpec is the content of a certificate a testCA issues: a client certificate naming
// its holder in one field, or the proxy's serving certificate.
type leafSpec struct {
	uri, dns, cn string
	// life is how long the certificate stays valid from now; testCertLife when zero.
	life   time.Duration
	server bool
}

func (ca *testCA) issue(t *testing.T, spec leafSpec) *tls.Certificate {
	t.Helper()
	key := newTestKey(t)
	life := spec.life
	if life == 0 {
		life = testCertLife
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(testSerial.Add(1)),
		Subject:      pkix.Name{CommonName: spec.cn},
		NotBefore:    time.Now().Add(-testCertLife),
		NotAfter:     time.Now().Add(life),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	if spec.server {
		tmpl.ExtKeyUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
		tmpl.IPAddresses = []net.IP{net.ParseIP(testLoopbackIP)}
	}
	if spec.dns != "" {
		tmpl.DNSNames = []string{spec.dns}
	}
	if spec.uri != "" {
		uri, err := url.Parse(spec.uri)
		if err != nil {
			t.Fatal(err)
		}
		tmpl.URIs = []*url.URL{uri}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		t.Fatal(err)
	}
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key, Leaf: leaf}
}

// workload issues a client certificate whose one URI SAN is uri.
func (ca *testCA) workload(t *testing.T, uri string) *tls.Certificate {
	t.Helper()
	return ca.issue(t, leafSpec{uri: uri})
}

// writeBundle replaces the file at path in one rename, as a volume update does, so no
// reader sees it half written and the reloader sees a new file.
func writeBundle(t *testing.T, path string, data []byte) {
	t.Helper()
	next := path + ".next"
	if err := os.WriteFile(next, data, 0o600); err != nil {
		t.Fatal(err)
	}
	when := time.Now().Add(time.Duration(testMtime.Add(1)) * time.Second)
	if err := os.Chtimes(next, when, when); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(next, path); err != nil {
		t.Fatal(err)
	}
}

// recordingUpstream is an API server that counts its requests and keeps the headers of the
// last one.
type recordingUpstream struct {
	srv   *httptest.Server
	calls atomic.Int32
	mu    sync.Mutex
	last  http.Header
}

func newRecordingUpstream(t *testing.T) *recordingUpstream {
	t.Helper()
	u := &recordingUpstream{}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := u.calls.Add(1)
		u.mu.Lock()
		u.last = r.Header.Clone()
		u.mu.Unlock()
		w.Header().Set("Content-Type", testContentJSON)
		_, _ = fmt.Fprintf(w, `{"kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"%d"},"items":[]}`, n)
	}))
	t.Cleanup(u.srv.Close)
	return u
}

func (u *recordingUpstream) headers() http.Header {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.last
}

// callerLog records the caller of every request the handler authenticated, through the
// embedder's Authorize hook: who the proxy took the request to be from.
type callerLog struct {
	mu      sync.Mutex
	callers []k8sapiproxy.CallerIdentity
}

func (l *callerLog) authorize(attrs k8sapiproxy.AuthorizeAttributes) error {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.callers = append(l.callers, *attrs.Caller)
	return nil
}

// take returns the callers recorded since the last call.
func (l *callerLog) take() []k8sapiproxy.CallerIdentity {
	l.mu.Lock()
	defer l.mu.Unlock()
	callers := l.callers
	l.callers = nil
	return callers
}

// certProxy is the binary's server on a loopback TLS listener, built by newServer from
// flags, with a CA for workloads and another for front proxies.
type certProxy struct {
	clock          *testClock
	workloads      *testCA
	gateways       *testCA
	workloadCAFile string
	gatewayCAFile  string
	tlsCertFile    string
	tlsKeyFile     string
	roots          *x509.CertPool
	upstream       *recordingUpstream
	callers        *callerLog
	logs           *lockedBuffer
	metrics        *k8sapiproxy.Metrics
	issuer         *testJWTIssuer
	url            string
}

// newCertProxy prepares the CAs, their bundle files, the serving certificate and the
// upstream; start builds and serves the proxy.
func newCertProxy(t *testing.T) *certProxy {
	t.Helper()
	clearProxyEnv(t)
	dir := t.TempDir()
	p := &certProxy{
		clock:          newTestClock(),
		workloads:      newTestCA(t, "workload-ca"),
		gateways:       newTestCA(t, "gateway-ca"),
		workloadCAFile: filepath.Join(dir, "workload-ca.crt"),
		gatewayCAFile:  filepath.Join(dir, "gateway-ca.crt"),
		tlsCertFile:    filepath.Join(dir, "tls.crt"),
		tlsKeyFile:     filepath.Join(dir, "tls.key"),
		roots:          x509.NewCertPool(),
		upstream:       newRecordingUpstream(t),
		callers:        &callerLog{},
		logs:           &lockedBuffer{},
		metrics:        k8sapiproxy.NewMetrics(),
	}
	writeBundle(t, p.workloadCAFile, p.workloads.pem)
	writeBundle(t, p.gatewayCAFile, p.gateways.pem)

	servingCA := newTestCA(t, "serving-ca")
	p.roots.AddCert(servingCA.cert)
	serving := servingCA.issue(t, leafSpec{server: true})
	key, err := x509.MarshalECPrivateKey(serving.PrivateKey.(*ecdsa.PrivateKey))
	if err != nil {
		t.Fatal(err)
	}
	writeBundle(t, p.tlsCertFile, pem.EncodeToMemory(&pem.Block{Type: testPEMCertificate, Bytes: serving.Certificate[0]}))
	writeBundle(t, p.tlsKeyFile, pem.EncodeToMemory(&pem.Block{Type: testPEMKey, Bytes: key}))
	return p
}

// baseArgs are the flags every certProxy starts with: a loopback TLS listener in front of
// the upstream.
func (p *certProxy) baseArgs() []string {
	return []string{
		"--listen-addr=" + testLoopbackAnyPort,
		"--upstream-url=" + p.upstream.srv.URL,
		"--tls-cert-file=" + p.tlsCertFile,
		"--tls-key-file=" + p.tlsKeyFile,
	}
}

// mtlsArgs are the --mtls-* flags that admit a workload certificate under testURIPrefix.
func (p *certProxy) mtlsArgs() []string {
	return []string{
		"--mtls-client-ca-file=" + p.workloadCAFile,
		"--mtls-identity-source=uri",
		"--mtls-required-uri-prefix=" + testURIPrefix,
	}
}

// headerArgs are the --requestheader-* flags that believe testGatewayURI's headers.
func (p *certProxy) headerArgs() []string {
	return []string{
		"--requestheader-client-ca-file=" + p.gatewayCAFile,
		"--requestheader-name-source=uri",
		"--requestheader-allowed-names=" + testGatewayURI,
		"--requestheader-username-headers=" + testUserHeader,
		"--requestheader-uid-headers=" + testUIDHeader,
		"--requestheader-group-headers=" + testGroupHeader,
		"--requestheader-partition-headers=" + testTenantHeader,
	}
}

// jwtArgs are the --jwt-* flags that admit the tokens of an issuer made for this proxy.
func (p *certProxy) jwtArgs(t *testing.T) []string {
	t.Helper()
	p.issuer = newTestJWTIssuer(t)
	return []string{
		"--jwt-issuer=" + p.issuer.srv.URL,
		"--jwt-audiences=" + testJWTAudience,
		"--jwt-ca-file=" + p.issuer.caFile,
	}
}

// token is a bearer token the proxy's JWT authenticator admits as testJWTSubject.
func (p *certProxy) token() string {
	return p.issuer.sign(testJWTSubject, testJWTAudience, nil)
}

// config is the configuration Run would assemble from the base flags and args in a binary
// passing opts, with the test's clock for the client CA bundles and certificate times.
func (p *certProxy) config(t *testing.T, opts Options, args ...string) (proxyConfig, k8sapiproxy.Mode) {
	t.Helper()
	opts.Authorize = p.callers.authorize
	all := append(p.baseArgs(), args...)
	cfg, mode, versionOnly, err := assemble(testFlagSet(), all, opts)
	if err != nil || versionOnly {
		t.Fatalf("assemble(%q) = versionOnly %v, err %v; want a configuration", all, versionOnly, err)
	}
	cfg.clientCert.reload = certreload.Options{CheckInterval: testCAInterval, Now: p.clock.now}
	cfg.clientCert.now = p.clock.now
	return cfg, mode
}

// start builds the server from the flags, as serve does, and serves it on loopback.
func (p *certProxy) start(t *testing.T, opts Options, args ...string) *certProxy {
	t.Helper()
	cfg, mode := p.config(t, opts, args...)
	logger := slog.New(slog.NewJSONHandler(p.logs, nil))
	srv, tlsEnabled, err := newServer(context.Background(), cfg, mode, logger, p.metrics, nil)
	if err != nil {
		t.Fatalf("newServer: %v", err)
	}
	if !tlsEnabled {
		t.Fatal("newServer built a plain HTTP server for a configuration with a serving certificate")
	}
	ln, err := net.Listen("tcp", testLoopbackAnyPort)
	if err != nil {
		t.Fatal(err)
	}
	p.url = "https://" + ln.Addr().String()
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	t.Cleanup(func() { _ = srv.Close() })
	return p
}

// certClient is a client with a connection pool of its own. asked counts the handshakes in
// which the server asked for a client certificate.
type certClient struct {
	http  *http.Client
	asked atomic.Int32
}

// client returns a client that presents cert whenever the server asks for a certificate,
// whichever CAs the server names, or none when cert is nil.
func (p *certProxy) client(t *testing.T, cert *tls.Certificate) *certClient {
	t.Helper()
	c := &certClient{}
	transport := &http.Transport{
		ForceAttemptHTTP2: true,
		TLSClientConfig: &tls.Config{
			RootCAs:    p.roots,
			MinVersion: tls.VersionTLS12,
			GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
				c.asked.Add(1)
				if cert == nil {
					return &tls.Certificate{}, nil
				}
				return cert, nil
			},
		},
	}
	t.Cleanup(transport.CloseIdleConnections)
	c.http = &http.Client{Transport: transport}
	return c
}

// certReply is the answer to one request: err when the connection could not be made.
type certReply struct {
	status int
	// http2 says the request went over HTTP/2, and reused that it went on a connection an
	// earlier request of the client opened.
	http2  bool
	reused bool
	err    error
}

// get sends a GET; headers alternate name and value, and a name may repeat.
func (c *certClient) get(t *testing.T, target string, headers ...string) certReply {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, target, nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Add(headers[i], headers[i+1])
	}
	var reused atomic.Bool
	trace := &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) { reused.Store(info.Reused) }}
	resp, err := c.http.Do(req.WithContext(httptrace.WithClientTrace(req.Context(), trace)))
	if err != nil {
		return certReply{err: err}
	}
	defer func() { _ = resp.Body.Close() }()
	if _, err := io.Copy(io.Discard, resp.Body); err != nil {
		t.Fatalf("reading the response: %v", err)
	}
	return certReply{status: resp.StatusCode, http2: resp.ProtoMajor == testHTTP2, reused: reused.Load()}
}

// bearer is the Authorization header of a caller's token, as get's headers.
func bearer(token string) []string {
	return []string{testAuthorization, testBearerPrefix + token}
}

// identityHeaders are the headers a front proxy sends for username in tenant.
func identityHeaders(username, tenant string) []string {
	return []string{testUserHeader, username, testUIDHeader, "uid-" + username, testTenantHeader, tenant}
}

// certCase is one request to a certProxy and what the proxy must make of it.
type certCase struct {
	name    string
	cert    *tls.Certificate
	headers []string
	// refusedInHandshake says the connection itself must fail: the certificate is from
	// no CA the listener accepts.
	refusedInHandshake bool
	// status is the answer; for 200, username, authenticator and partition are the
	// caller the proxy must have admitted.
	status        int
	username      string
	authenticator string
	partition     string
}

// identityHeaderNames are the headers no upstream request may carry, whoever sent them.
var identityHeaderNames = []string{testUserHeader, testUIDHeader, testGroupHeader, testTenantHeader, testAuthorization}

// check sends tc's request on a connection of its own over HTTP/2 and verifies the answer,
// the caller the proxy admitted, and that an admitted request reached the upstream without
// any of strip.
func (p *certProxy) check(t *testing.T, tc certCase, strip []string) {
	t.Helper()
	p.callers.take()
	before := p.upstream.calls.Load()
	got := p.client(t, tc.cert).get(t, p.url+testPodsPath, tc.headers...)
	if tc.refusedInHandshake {
		if got.err == nil {
			t.Fatalf("status %d, want the connection refused in the TLS handshake", got.status)
		}
		return
	}
	if got.err != nil {
		t.Fatalf("GET: %v", got.err)
	}
	if got.status != tc.status {
		t.Fatalf("status %d, want %d", got.status, tc.status)
	}
	if !got.http2 {
		t.Error("the request did not go over HTTP/2")
	}
	callers := p.callers.take()
	if tc.status != http.StatusOK {
		if len(callers) != 0 || p.upstream.calls.Load() != before {
			t.Errorf("a refused request was authenticated as %+v or reached the upstream", callers)
		}
		return
	}
	if len(callers) != 1 {
		t.Fatalf("the proxy authenticated %d callers, want 1: %+v", len(callers), callers)
	}
	if c := callers[0]; c.Username != tc.username || c.Authenticator != tc.authenticator || c.Partition != tc.partition {
		t.Errorf("caller = %q by %q in partition %q, want %q by %q in partition %q", c.Username, c.Authenticator, c.Partition, tc.username, tc.authenticator, tc.partition)
	}
	if p.upstream.calls.Load() != before+1 {
		t.Fatalf("upstream calls went from %d to %d, want one more", before, p.upstream.calls.Load())
	}
	sent := p.upstream.headers()
	for _, name := range strip {
		if values := sent.Values(name); len(values) != 0 {
			t.Errorf("the upstream received %s = %q", name, values)
		}
	}
}

func (p *certProxy) checkAll(t *testing.T, cases []certCase, strip []string) {
	t.Helper()
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) { p.check(t, tc, strip) })
	}
}

// --auth-mode=mtls alone: the caller is the certificate, read from the one configured
// field, over TLS and HTTP/2.
func TestMTLSModeAdmitsACallerByItsClientCertificate(t *testing.T) {
	t.Run("uri", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, append(p.mtlsArgs(), "--auth-mode=mtls")...)
		p.checkAll(t, []certCase{
			{name: "workload certificate", cert: p.workloads.workload(t, testWorkloadURI), status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
			{name: "workload certificate sending identity headers", cert: p.workloads.workload(t, testWorkloadURI), headers: identityHeaders(testAlice, testTeamA), status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
			{name: "workload certificate sending a bearer token", cert: p.workloads.workload(t, testWorkloadURI), headers: bearer("any-token"), status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
			{name: "URI outside the required prefix", cert: p.workloads.workload(t, testOutsideURI), status: http.StatusUnauthorized},
			{name: "no URI in the certificate", cert: p.workloads.issue(t, leafSpec{dns: testWorkloadDNS, cn: testWorkloadCN}), status: http.StatusUnauthorized},
			{name: "no certificate", status: http.StatusUnauthorized},
			{name: "certificate from another CA", cert: p.gateways.workload(t, testWorkloadURI), refusedInHandshake: true},
		}, []string{testAuthorization})
	})
	t.Run("dns", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, "--auth-mode=mtls", "--mtls-client-ca-file="+p.workloadCAFile, "--mtls-identity-source=dns", "--mtls-allowed-identities="+testWorkloadDNS+", "+testWorkloadCN)
		p.checkAll(t, []certCase{
			{name: "allowed DNS name", cert: p.workloads.issue(t, leafSpec{dns: testWorkloadDNS, cn: testOtherCN}), status: http.StatusOK, username: testWorkloadDNS, authenticator: authModeMTLS},
			{name: "DNS name not in the allowlist", cert: p.workloads.issue(t, leafSpec{dns: testOtherDNS}), status: http.StatusUnauthorized},
			{name: "allowed name in another field", cert: p.workloads.issue(t, leafSpec{cn: testWorkloadDNS, uri: testWorkloadURI}), status: http.StatusUnauthorized},
		}, nil)
	})
	t.Run("cn", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, "--auth-mode=mtls", "--mtls-client-ca-file="+p.workloadCAFile, "--mtls-identity-source=cn")
		p.checkAll(t, []certCase{
			{name: "common name", cert: p.workloads.issue(t, leafSpec{cn: testWorkloadCN, dns: testOtherDNS}), status: http.StatusOK, username: testWorkloadCN, authenticator: authModeMTLS},
			{name: "no common name", cert: p.workloads.issue(t, leafSpec{dns: testWorkloadDNS}), status: http.StatusUnauthorized},
		}, nil)
	})
}

// --auth-mode=requestheader alone: an allowed front proxy's headers name the caller, and
// none of them, nor an embedder's own caller header, reaches the upstream.
func TestRequestHeaderModeBelievesAnAllowedFrontProxy(t *testing.T) {
	p := newCertProxy(t)
	p.start(t, Options{ExtraCallerHeaders: []string{testEmbedderHeader, strings.ToLower(testUserHeader)}}, append(p.headerArgs(), "--auth-mode=requestheader")...)
	gateway := p.gateways.workload(t, testGatewayURI)
	full := []string{
		testUserHeader, testAlice, testUIDHeader, testAliceUID, testTenantHeader, testTeamA,
		testGroupHeader, testGroupOne, testGroupHeader, testGroupTwo,
		testEmbedderHeader, testEmbedderHeaderValue, testStockHeader, testStockHeaderValue,
	}
	strip := append(slices.Clone(identityHeaderNames), testEmbedderHeader)

	t.Run("every identity field", func(t *testing.T) {
		p.callers.take()
		got := p.client(t, gateway).get(t, p.url+testPodsPath, full...)
		if got.err != nil || got.status != http.StatusOK || !got.http2 {
			t.Fatalf("reply %+v, want 200 over HTTP/2", got)
		}
		callers := p.callers.take()
		if len(callers) != 1 {
			t.Fatalf("the proxy authenticated %d callers, want 1", len(callers))
		}
		c := callers[0]
		if c.Username != testAlice || c.UID != testAliceUID || c.Partition != testTeamA || c.Authenticator != authModeRequestHeader {
			t.Errorf("caller = %+v, want %s, %s, partition %s by %s", c, testAlice, testAliceUID, testTeamA, authModeRequestHeader)
		}
		if want := []string{testGroupOne, testGroupTwo}; !slices.Equal(c.Groups, want) {
			t.Errorf("groups = %q, want %q", c.Groups, want)
		}
		sent := p.upstream.headers()
		for _, name := range strip {
			if values := sent.Values(name); len(values) != 0 {
				t.Errorf("the upstream received %s = %q", name, values)
			}
		}
		if v := sent.Get(testStockHeader); v != testStockHeaderValue {
			t.Errorf("upstream %s = %q, want %q: a header nothing names is forwarded", testStockHeader, v, testStockHeaderValue)
		}
	})

	p.checkAll(t, []certCase{
		{name: "allowed front proxy", cert: gateway, headers: identityHeaders(testBob, testTeamB), status: http.StatusOK, username: testBob, authenticator: authModeRequestHeader, partition: testTeamB},
		{name: "front proxy without the UID header", cert: gateway, headers: []string{testUserHeader, testAlice, testTenantHeader, testTeamA}, status: http.StatusUnauthorized},
		{name: "front proxy without headers", cert: gateway, status: http.StatusUnauthorized},
		{name: "front proxy repeating the username", cert: gateway, headers: append(identityHeaders(testAlice, testTeamA), testUserHeader, testBob), status: http.StatusUnauthorized},
		{name: "front proxy name that is not allowed", cert: p.gateways.workload(t, testUnlistedGateway), headers: identityHeaders(testAlice, testTeamA), status: http.StatusUnauthorized},
		{name: "no certificate", headers: identityHeaders(testAlice, testTeamA), status: http.StatusUnauthorized},
		{name: "allowed name under the workload CA", cert: p.workloads.workload(t, testGatewayURI), headers: identityHeaders(testAlice, testTeamA), refusedInHandshake: true},
	}, strip)
}

// --auth-mode with requestheader and jwt, in either order: the front proxy is asked first,
// a connection with a client certificate is never admitted on a bearer token, and a caller
// without a certificate is admitted on its token with identity headers of its own ignored
// and stripped.
func TestRequestHeaderChainedWithJWT(t *testing.T) {
	for _, order := range []string{"requestheader,jwt", "jwt,requestheader"} {
		t.Run(order, func(t *testing.T) {
			p := newCertProxy(t)
			p.start(t, Options{}, append(append(p.headerArgs(), p.jwtArgs(t)...), "--auth-mode="+order)...)
			gateway := p.gateways.workload(t, testGatewayURI)
			withToken := func(headers ...string) []string { return append(headers, bearer(p.token())...) }

			// First, before any token was verified: the JWT authenticator fetches its key
			// set for the first token it is asked about.
			p.check(t, certCase{name: "unlisted", cert: p.gateways.workload(t, testUnlistedGateway), headers: withToken(identityHeaders(testAlice, testTeamA)...), status: http.StatusUnauthorized}, nil)
			p.check(t, certCase{name: "no headers", cert: gateway, headers: withToken(), status: http.StatusUnauthorized}, nil)
			if served := p.issuer.served.Load(); served != 0 {
				t.Errorf("the JWT issuer served %d requests: a token on a connection with a client certificate was offered to the JWT authenticator", served)
			}

			p.checkAll(t, []certCase{
				{name: "front proxy naming a caller", cert: gateway, headers: identityHeaders(testAlice, testTeamA), status: http.StatusOK, username: testAlice, authenticator: authModeRequestHeader, partition: testTeamA},
				{name: "front proxy forwarding the caller's token too", cert: gateway, headers: withToken(identityHeaders(testAlice, testTeamA)...), status: http.StatusOK, username: testAlice, authenticator: authModeRequestHeader, partition: testTeamA},
				{name: "front proxy forwarding a token and no identity", cert: gateway, headers: withToken(), status: http.StatusUnauthorized},
				{name: "front proxy naming a caller twice, with a token", cert: gateway, headers: withToken(append(identityHeaders(testAlice, testTeamA), testUserHeader, testBob)...), status: http.StatusUnauthorized},
				{name: "unlisted front proxy with a token", cert: p.gateways.workload(t, testUnlistedGateway), headers: withToken(identityHeaders(testAlice, testTeamA)...), status: http.StatusUnauthorized},
				{name: "token without a certificate", headers: withToken(), status: http.StatusOK, username: testJWTSubject, authenticator: authModeJWT},
				{name: "token without a certificate, sending identity headers", headers: withToken(identityHeaders(testAlice, testTeamA)...), status: http.StatusOK, username: testJWTSubject, authenticator: authModeJWT},
				{name: "identity headers without a certificate or a token", headers: identityHeaders(testAlice, testTeamA), status: http.StatusUnauthorized},
				{name: "nothing", status: http.StatusUnauthorized},
			}, identityHeaderNames)
		})
	}
}

// newPodReviewServer is an API server whose TokenReview accepts every token as
// testReviewCaller, bound to a pod at the loopback address a real listener's requests come
// from. reviews counts the TokenReviews it answered.
func newPodReviewServer(t *testing.T) (srv *httptest.Server, reviews *atomic.Int32) {
	t.Helper()
	reviews = &atomic.Int32{}
	pod := strings.ReplaceAll(testCallerPod, testRecorderPeerIP, testLoopbackIP)
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", testContentJSON)
		if r.Method == http.MethodGet && r.URL.Path == testCallerPodPath {
			_, _ = io.WriteString(w, pod)
			return
		}
		reviews.Add(1)
		_, _ = io.WriteString(w, testReviewAccepted)
	}))
	t.Cleanup(srv.Close)
	return srv, reviews
}

// --auth-mode=mtls,token-review: a workload's certificate admits it without a TokenReview,
// a pod without a certificate is admitted on its ServiceAccount token from its own
// address, and a token on a connection with a certificate is never reviewed.
func TestMTLSChainedWithTokenReview(t *testing.T) {
	for _, order := range []string{"mtls,token-review", "token-review,mtls"} {
		t.Run(order, func(t *testing.T) {
			p := newCertProxy(t)
			review, reviews := newPodReviewServer(t)
			p.start(t, Options{}, append(p.mtlsArgs(),
				"--auth-mode="+order,
				"--allowed-callers="+testReviewCaller,
				"--audience="+testReviewAudience,
				"--kube-api-server="+review.URL,
				"--kube-ca-file=",
				"--kube-token-file=",
			)...)
			workload := p.workloads.workload(t, testWorkloadURI)

			p.checkAll(t, []certCase{
				{name: "workload certificate", cert: workload, status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
				{name: "workload certificate sending a token", cert: workload, headers: bearer("token-with-certificate"), status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
				{name: "certificate outside the prefix sending a token", cert: p.workloads.workload(t, testOutsideURI), headers: bearer("token-outside-prefix"), status: http.StatusUnauthorized},
			}, identityHeaderNames)
			if got := reviews.Load(); got != 0 {
				t.Errorf("%d TokenReviews for requests on connections with a client certificate, want none", got)
			}

			p.checkAll(t, []certCase{
				{name: "pod token without a certificate", headers: bearer("pod-token"), status: http.StatusOK, username: testReviewCaller, authenticator: authModeTokenReview},
				{name: "nothing", status: http.StatusUnauthorized},
			}, identityHeaderNames)
			if got := reviews.Load(); got == 0 {
				t.Error("a token on a connection without a client certificate was not reviewed")
			}
		})
	}
}

// --auth-mode with all of mtls, requestheader and jwt: each credential is judged by its
// own authenticator, and each CA vouches for its own mode only.
func TestMTLSRequestHeaderAndJWTChained(t *testing.T) {
	p := newCertProxy(t)
	p.start(t, Options{}, append(append(append(p.mtlsArgs(), p.headerArgs()...), p.jwtArgs(t)...), "--auth-mode=jwt,mtls,requestheader")...)
	workload, gateway := p.workloads.workload(t, testWorkloadURI), p.gateways.workload(t, testGatewayURI)
	withToken := func(headers ...string) []string { return append(headers, bearer(p.token())...) }

	p.checkAll(t, []certCase{
		{name: "workload certificate", cert: workload, status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
		{name: "workload certificate sending identity headers and a token", cert: workload, headers: withToken(identityHeaders(testAlice, testTeamA)...), status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS},
		{name: "front proxy naming a caller", cert: gateway, headers: identityHeaders(testAlice, testTeamA), status: http.StatusOK, username: testAlice, authenticator: authModeRequestHeader, partition: testTeamA},
		{name: "front proxy without headers, with a token", cert: gateway, headers: withToken(), status: http.StatusUnauthorized},
		{name: "front proxy's name under the workload CA", cert: p.workloads.workload(t, testGatewayURI), headers: identityHeaders(testAlice, testTeamA), status: http.StatusUnauthorized},
		{name: "workload's name under the front proxy CA", cert: p.gateways.workload(t, testWorkloadURI), status: http.StatusUnauthorized},
		{name: "certificate outside the prefix, with a token", cert: p.workloads.workload(t, testOutsideURI), headers: withToken(), status: http.StatusUnauthorized},
		{name: "token without a certificate", headers: withToken(), status: http.StatusOK, username: testJWTSubject, authenticator: authModeJWT},
		{name: "nothing", status: http.StatusUnauthorized},
	}, identityHeaderNames)
}

// A kubelet probe presents no certificate and no token: the health endpoint answers it
// with either certificate mode on.
func TestHealthzNeedsNoClientCertificate(t *testing.T) {
	for _, mode := range []string{authModeMTLS, authModeRequestHeader, "mtls,requestheader,jwt"} {
		t.Run(mode, func(t *testing.T) {
			p := newCertProxy(t)
			args := []string{"--auth-mode=" + mode}
			if strings.Contains(mode, authModeMTLS) {
				args = append(args, p.mtlsArgs()...)
			}
			if strings.Contains(mode, authModeRequestHeader) {
				args = append(args, p.headerArgs()...)
			}
			if strings.Contains(mode, authModeJWT) {
				args = append(args, p.jwtArgs(t)...)
			}
			p.start(t, Options{}, args...)
			client := p.client(t, nil)
			got := client.get(t, p.url+k8sapiproxy.HealthzPath)
			if got.err != nil || got.status != http.StatusOK || !got.http2 {
				t.Errorf("reply %+v, want 200 over HTTP/2", got)
			}
			if client.asked.Load() == 0 {
				t.Error("the listener did not ask for a client certificate")
			}
		})
	}
}

// What changes after the handshake is enforced on the next request of the same HTTP/2
// connection: a certificate that expired, and one whose CA left the bundle. For a front
// proxy in a chain with jwt that is also the end of the request: the caller's token it
// forwards is not consulted.
func TestOpenConnectionIsRefusedOnceItsCertificateIsNoLongerGood(t *testing.T) {
	peers := []struct {
		name string
		// args are the proxy's flags; ca and caFile pick the peer's CA and its bundle.
		args    func(t *testing.T, p *certProxy) []string
		ca      func(p *certProxy) (*testCA, string)
		uri     string
		headers func(p *certProxy) []string
		want    certCase
	}{
		{
			name: "mtls",
			args: func(_ *testing.T, p *certProxy) []string { return append(p.mtlsArgs(), "--auth-mode=mtls") },
			ca:   func(p *certProxy) (*testCA, string) { return p.workloads, p.workloadCAFile },
			uri:  testWorkloadURI,
			want: certCase{username: testWorkloadURI, authenticator: authModeMTLS},
		},
		{
			name:    "requestheader",
			args:    func(_ *testing.T, p *certProxy) []string { return append(p.headerArgs(), "--auth-mode=requestheader") },
			ca:      func(p *certProxy) (*testCA, string) { return p.gateways, p.gatewayCAFile },
			uri:     testGatewayURI,
			headers: func(*certProxy) []string { return identityHeaders(testAlice, testTeamA) },
			want:    certCase{username: testAlice, authenticator: authModeRequestHeader, partition: testTeamA},
		},
		{
			name: "requestheader forwarding the caller's token, chained with jwt",
			args: func(t *testing.T, p *certProxy) []string {
				return append(append(p.headerArgs(), p.jwtArgs(t)...), "--auth-mode=requestheader,jwt")
			},
			ca:  func(p *certProxy) (*testCA, string) { return p.gateways, p.gatewayCAFile },
			uri: testGatewayURI,
			headers: func(p *certProxy) []string {
				return append(identityHeaders(testAlice, testTeamA), bearer(p.token())...)
			},
			want: certCase{username: testAlice, authenticator: authModeRequestHeader, partition: testTeamA},
		},
	}
	changes := []struct {
		name string
		life time.Duration
		// invalidate makes the peer's certificate no longer good.
		invalidate func(t *testing.T, p *certProxy, caFile string)
	}{
		{
			name: "CA rotated out of the bundle",
			invalidate: func(t *testing.T, p *certProxy, caFile string) {
				writeBundle(t, caFile, newTestCA(t, "next-ca").pem)
				p.clock.passCheckInterval()
			},
		},
		{
			name:       "certificate expired",
			life:       testShortCertLife,
			invalidate: func(_ *testing.T, p *certProxy, _ string) { p.clock.advance(2 * testShortCertLife) },
		},
	}
	for _, peer := range peers {
		for _, change := range changes {
			t.Run(peer.name+"/"+change.name, func(t *testing.T) {
				p := newCertProxy(t)
				p.start(t, Options{}, peer.args(t, p)...)
				ca, caFile := peer.ca(p)
				client := p.client(t, ca.issue(t, leafSpec{uri: peer.uri, life: change.life}))
				send := func() certReply {
					var headers []string
					if peer.headers != nil {
						headers = peer.headers(p)
					}
					return client.get(t, p.url+testPodsPath, headers...)
				}

				first := send()
				if first.err != nil || first.status != http.StatusOK || !first.http2 {
					t.Fatalf("first request: %+v, want 200 over HTTP/2", first)
				}
				again := send()
				if again.status != http.StatusOK || !again.reused {
					t.Fatalf("second request: %+v, want 200 on the same connection", again)
				}
				callers := p.callers.take()
				if len(callers) != 2 || callers[1].Username != peer.want.username || callers[1].Authenticator != peer.want.authenticator || callers[1].Partition != peer.want.partition {
					t.Fatalf("callers = %+v, want two of %q by %q", callers, peer.want.username, peer.want.authenticator)
				}

				change.invalidate(t, p, caFile)
				calls := p.upstream.calls.Load()
				after := send()
				if after.err != nil || after.status != http.StatusUnauthorized {
					t.Fatalf("request after the change: %+v, want 401", after)
				}
				if !after.reused || !after.http2 {
					t.Errorf("request after the change: %+v, want it on the same HTTP/2 connection", after)
				}
				if callers := p.callers.take(); len(callers) != 0 || p.upstream.calls.Load() != calls {
					t.Errorf("the refused request was authenticated as %+v or reached the upstream", callers)
				}
			})
		}
	}
}

// A rotated client CA file is trusted, and the CA it replaced refused, by the next
// handshake and the next request, without a restart; the other mode's bundle is untouched.
func TestClientCAFilesReloadWithoutARestart(t *testing.T) {
	p := newCertProxy(t)
	p.start(t, Options{}, append(append(p.mtlsArgs(), p.headerArgs()...), "--auth-mode=mtls,requestheader")...)
	nextWorkloads, nextGateways := newTestCA(t, "next-workload-ca"), newTestCA(t, "next-gateway-ca")
	headers := identityHeaders(testAlice, testTeamA)
	workload := func(ca *testCA, refused bool) certCase {
		return certCase{cert: ca.workload(t, testWorkloadURI), refusedInHandshake: refused, status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS}
	}
	gateway := func(ca *testCA, refused bool) certCase {
		return certCase{cert: ca.workload(t, testGatewayURI), headers: headers, refusedInHandshake: refused, status: http.StatusOK, username: testAlice, authenticator: authModeRequestHeader, partition: testTeamA}
	}

	p.check(t, workload(p.workloads, false), identityHeaderNames)
	p.check(t, gateway(p.gateways, false), identityHeaderNames)
	p.check(t, workload(nextWorkloads, true), nil)
	p.check(t, gateway(nextGateways, true), nil)

	writeBundle(t, p.workloadCAFile, nextWorkloads.pem)
	p.clock.passCheckInterval()
	p.check(t, workload(nextWorkloads, false), identityHeaderNames)
	p.check(t, workload(p.workloads, true), nil)
	p.check(t, gateway(p.gateways, false), identityHeaderNames)

	writeBundle(t, p.gatewayCAFile, nextGateways.pem)
	p.clock.passCheckInterval()
	p.check(t, gateway(nextGateways, false), identityHeaderNames)
	p.check(t, gateway(p.gateways, true), nil)
	p.check(t, workload(nextWorkloads, false), identityHeaderNames)
}

// A changed client CA file that does not load keeps the bundle it replaced, for the
// handshake and for the authenticator, and is logged and counted once for each of them.
func TestClientCAFileThatStopsLoadingKeepsTheLastGoodBundle(t *testing.T) {
	p := newCertProxy(t)
	p.start(t, Options{}, append(p.mtlsArgs(), "--auth-mode=mtls")...)
	admitted := certCase{cert: p.workloads.workload(t, testWorkloadURI), status: http.StatusOK, username: testWorkloadURI, authenticator: authModeMTLS}
	p.check(t, admitted, nil)

	writeBundle(t, p.workloadCAFile, []byte(testNotABundle))
	p.clock.passCheckInterval()
	p.check(t, admitted, nil)

	logs := p.logs.String()
	for _, want := range []string{`"level":"WARN"`, "--" + mtlsClientCAFileFlag, "a changed client CA file could not be loaded"} {
		if !strings.Contains(logs, want) {
			t.Errorf("logs = %s, want them to contain %s", logs, want)
		}
	}
	rec := httptest.NewRecorder()
	p.metrics.Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, k8sapiproxy.MetricsPath, nil))
	if !strings.Contains(rec.Body.String(), testReloadFailures2) {
		t.Errorf("metrics exposition missing %q: the listener's pool and the authenticator's each count the failed reload", testReloadFailures2)
	}
}

// --partition-by-caller separates cached responses by the partition a front proxy names,
// or by username under a single certificate mode.
func TestCertificateModesPartitionCachedResponses(t *testing.T) {
	const cacheTTL = "--cache-resource-ttl=1m"
	t.Run("requestheader by partition", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, append(p.headerArgs(), "--auth-mode=requestheader", "--partition-by-caller=partition", cacheTTL)...)
		client := p.client(t, p.gateways.workload(t, testGatewayURI))
		callers := [][]string{identityHeaders(testAlice, testTeamA), identityHeaders(testBob, testTeamA), identityHeaders(testCarol, testTeamB)}
		for _, headers := range append(callers, callers...) {
			if got := client.get(t, p.url+testPodsPath, headers...); got.err != nil || got.status != http.StatusOK {
				t.Fatalf("reply %+v for %q, want 200", got, headers)
			}
		}
		if got := p.upstream.calls.Load(); got != 2 {
			t.Errorf("upstream calls = %d, want 2: one for each tenant", got)
		}
	})
	t.Run("requestheader by username", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, append(p.headerArgs(), "--auth-mode=requestheader", "--partition-by-caller=username", cacheTTL)...)
		client := p.client(t, p.gateways.workload(t, testGatewayURI))
		callers := [][]string{identityHeaders(testAlice, testTeamA), identityHeaders(testBob, testTeamA), identityHeaders(testCarol, testTeamB)}
		for _, headers := range append(callers, callers...) {
			if got := client.get(t, p.url+testPodsPath, headers...); got.err != nil || got.status != http.StatusOK {
				t.Fatalf("reply %+v for %q, want 200", got, headers)
			}
		}
		if got := p.upstream.calls.Load(); got != 3 {
			t.Errorf("upstream calls = %d, want 3: one for each username", got)
		}
	})
	t.Run("mtls by username", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, append(p.mtlsArgs(), "--auth-mode=mtls", "--partition-by-caller=username", cacheTTL)...)
		clients := []*certClient{
			p.client(t, p.workloads.workload(t, testWorkloadURI)),
			p.client(t, p.workloads.workload(t, testOtherWorkloadURI)),
			// Another certificate of the first workload: the same username.
			p.client(t, p.workloads.workload(t, testWorkloadURI)),
		}
		for _, client := range append(clients, clients...) {
			if got := client.get(t, p.url+testPodsPath); got.err != nil || got.status != http.StatusOK {
				t.Fatalf("reply %+v, want 200", got)
			}
		}
		if got := p.upstream.calls.Load(); got != 2 {
			t.Errorf("upstream calls = %d, want 2: one for each username", got)
		}
	})
	t.Run("mtls without the flag", func(t *testing.T) {
		p := newCertProxy(t)
		p.start(t, Options{}, append(p.mtlsArgs(), "--auth-mode=mtls", cacheTTL)...)
		for _, uri := range []string{testWorkloadURI, testOtherWorkloadURI} {
			if got := p.client(t, p.workloads.workload(t, uri)).get(t, p.url+testPodsPath); got.err != nil || got.status != http.StatusOK {
				t.Fatalf("reply %+v, want 200", got)
			}
		}
		if got := p.upstream.calls.Load(); got != 1 {
			t.Errorf("upstream calls = %d, want 1: every caller shares one partition", got)
		}
	})
}

// With neither certificate mode the listener's TLS configuration is what it was before the
// modes existed, and its handshake asks no peer for a certificate; with one it asks, and
// requires none.
func TestListenerAsksForAClientCertificateOnlyWithACertificateMode(t *testing.T) {
	for _, tc := range []struct {
		name string
		args func(t *testing.T, p *certProxy) []string
		asks bool
	}{
		{name: authModeNone, args: func(*testing.T, *certProxy) []string { return []string{"--auth-mode=none"} }},
		{name: authModeJWT, args: func(t *testing.T, p *certProxy) []string { return append(p.jwtArgs(t), "--auth-mode=jwt") }},
		{name: authModeMTLS, args: func(_ *testing.T, p *certProxy) []string { return append(p.mtlsArgs(), "--auth-mode=mtls") }, asks: true},
		{name: authModeRequestHeader, args: func(_ *testing.T, p *certProxy) []string {
			return append(p.headerArgs(), "--auth-mode=requestheader")
		}, asks: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := newCertProxy(t)
			args := tc.args(t, p)
			// Read from a server nothing serves: net/http writes to a served one's.
			proxyCfg, mode := p.config(t, Options{}, args...)
			unserved, _, err := newServer(context.Background(), proxyCfg, mode, slog.New(slog.DiscardHandler), nil, nil)
			if err != nil {
				t.Fatalf("newServer: %v", err)
			}
			cfg := unserved.TLSConfig
			if len(cfg.NextProtos) != 0 || cfg.VerifyPeerCertificate != nil || cfg.VerifyConnection != nil {
				t.Errorf("the server's own TLS configuration sets NextProtos %q or a verification hook", cfg.NextProtos)
			}
			if cfg.MinVersion != tls.VersionTLS12 || cfg.GetCertificate == nil {
				t.Errorf("MinVersion = %x, GetCertificate set = %v; want TLS 1.2 and the reloaded serving certificate", cfg.MinVersion, cfg.GetCertificate != nil)
			}
			// The server's own configuration never asks: a mode's handshakes get theirs
			// from GetConfigForClient.
			if cfg.ClientAuth != tls.NoClientCert || cfg.ClientCAs != nil {
				t.Errorf("ClientAuth = %v, ClientCAs set = %v; want neither on the server's own configuration", cfg.ClientAuth, cfg.ClientCAs != nil)
			}
			if got := cfg.GetConfigForClient != nil; got != tc.asks {
				t.Fatalf("GetConfigForClient set = %v, want %v", got, tc.asks)
			}
			if tc.asks {
				handshake, err := cfg.GetConfigForClient(&tls.ClientHelloInfo{})
				if err != nil {
					t.Fatal(err)
				}
				if handshake.ClientAuth != tls.VerifyClientCertIfGiven || handshake.ClientCAs == nil || handshake.MinVersion != tls.VersionTLS12 || handshake.GetCertificate == nil {
					t.Errorf("handshake configuration = ClientAuth %v, ClientCAs set %v, MinVersion %x; want VerifyClientCertIfGiven against the client CAs at TLS 1.2", handshake.ClientAuth, handshake.ClientCAs != nil, handshake.MinVersion)
				}
				again, err := cfg.GetConfigForClient(&tls.ClientHelloInfo{})
				if err != nil || again != handshake {
					t.Errorf("a second handshake got another configuration (err %v) while the client CAs are unchanged", err)
				}
			}

			p.start(t, Options{}, args...)
			client := p.client(t, nil)
			got := client.get(t, p.url+k8sapiproxy.HealthzPath)
			if got.err != nil || got.status != http.StatusOK || !got.http2 {
				t.Fatalf("reply %+v, want 200 over HTTP/2", got)
			}
			if asked := client.asked.Load() > 0; asked != tc.asks {
				t.Errorf("the handshake asked for a client certificate = %v, want %v", asked, tc.asks)
			}
		})
	}
}

// The rule of the binary's chain, on the authenticators alone: a connection that presented
// a client certificate is the certificate modes' to admit or refuse, and one without is
// the bearer modes'.
func TestCertificateFirstAuthenticator(t *testing.T) {
	admitted := &k8sapiproxy.CallerIdentity{Username: testAlice}
	withCert := func() *http.Request {
		r := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
		r.TLS = &tls.ConnectionState{PeerCertificates: []*x509.Certificate{{}}}
		return r
	}
	withoutCert := func() *http.Request {
		r := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
		r.TLS = &tls.ConnectionState{}
		return r
	}
	plainHTTP := func() *http.Request { return httptest.NewRequest(http.MethodGet, testPodsPath, nil) }
	errRefused := errors.New("not this authenticator's certificate")
	errFinal := fmt.Errorf("%w: the front proxy named no caller", k8sapiproxy.ErrCallerRefused)

	for _, tc := range []struct {
		name string
		// cert and bearer are what each side answers; a nil bearer is a chain without one.
		cert    *fixedAuthenticator
		bearer  *fixedAuthenticator
		request func() *http.Request
		// wantCaller says the request is admitted; otherwise wantErr is in the refusal.
		wantCaller      bool
		wantErr         error
		wantCertCalls   int32
		wantBearerCalls int32
		// wantNotConsulted says the refusal names the bearer modes as not asked.
		wantNotConsulted bool
	}{
		{name: "certificate admitted", cert: &fixedAuthenticator{caller: admitted}, bearer: &fixedAuthenticator{err: errRefused}, request: withCert, wantCaller: true, wantCertCalls: 1},
		{name: "certificate refused, bearer would admit", cert: &fixedAuthenticator{err: errRefused}, bearer: &fixedAuthenticator{caller: admitted}, request: withCert, wantErr: errRefused, wantCertCalls: 1, wantNotConsulted: true},
		{name: "front proxy's final refusal, bearer would admit", cert: &fixedAuthenticator{err: errFinal}, bearer: &fixedAuthenticator{caller: admitted}, request: withCert, wantErr: k8sapiproxy.ErrCallerRefused, wantCertCalls: 1, wantNotConsulted: true},
		{name: "certificate modes return nothing, bearer would admit", cert: &fixedAuthenticator{}, bearer: &fixedAuthenticator{caller: admitted}, request: withCert, wantErr: errNoAuthenticatorAccepted, wantCertCalls: 1, wantNotConsulted: true},
		{name: "no certificate, bearer admits", cert: &fixedAuthenticator{caller: admitted}, bearer: &fixedAuthenticator{caller: admitted}, request: withoutCert, wantCaller: true, wantBearerCalls: 1},
		{name: "no certificate, bearer refuses", cert: &fixedAuthenticator{caller: admitted}, bearer: &fixedAuthenticator{err: errRefused}, request: withoutCert, wantErr: errRefused, wantBearerCalls: 1},
		{name: "plain HTTP, bearer admits", cert: &fixedAuthenticator{err: errRefused}, bearer: &fixedAuthenticator{caller: admitted}, request: plainHTTP, wantCaller: true, wantBearerCalls: 1},
		{name: "no bearer mode, certificate refused", cert: &fixedAuthenticator{err: errRefused}, request: withCert, wantErr: errRefused, wantCertCalls: 1},
		{name: "no bearer mode, no certificate", cert: &fixedAuthenticator{err: errRefused}, request: withoutCert, wantErr: errRefused, wantCertCalls: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var bearerModes []k8sapiproxy.CallerAuthenticator
			if tc.bearer != nil {
				bearerModes = append(bearerModes, tc.bearer)
			}
			authn := newModeAuthenticator([]k8sapiproxy.CallerAuthenticator{tc.cert}, bearerModes)
			caller, err := authn.AuthenticateRequest(tc.request())
			if tc.wantCaller != (caller != nil && err == nil) {
				t.Fatalf("caller = %+v, err = %v; want admitted = %v", caller, err, tc.wantCaller)
			}
			if tc.wantErr != nil && !errors.Is(err, tc.wantErr) {
				t.Errorf("err = %v, want it to wrap %v", err, tc.wantErr)
			}
			if got := errors.Is(err, errBearerNotConsulted); got != tc.wantNotConsulted {
				t.Errorf("refusal says the bearer modes were not consulted = %v, want %v (err %v)", got, tc.wantNotConsulted, err)
			}
			if got := tc.cert.calls.Load(); got != tc.wantCertCalls {
				t.Errorf("certificate modes asked %d times, want %d", got, tc.wantCertCalls)
			}
			if tc.bearer != nil {
				if got := tc.bearer.calls.Load(); got != tc.wantBearerCalls {
					t.Errorf("bearer modes asked %d times, want %d", got, tc.wantBearerCalls)
				}
			}
		})
	}

	// Without a certificate mode the authenticator is the chain the binary always built.
	bearerOnly := &fixedAuthenticator{caller: admitted}
	if got := newModeAuthenticator(nil, []k8sapiproxy.CallerAuthenticator{bearerOnly}); got != k8sapiproxy.CallerAuthenticator(bearerOnly) {
		t.Errorf("newModeAuthenticator without a certificate mode = %T, want the one bearer authenticator itself", got)
	}
}

func TestOrderedAuthModes(t *testing.T) {
	for raw, want := range map[string][]string{
		"token-review":                        {authModeTokenReview},
		"jwt,token-review":                    {authModeJWT, authModeTokenReview},
		"jwt,requestheader":                   {authModeRequestHeader, authModeJWT},
		"mtls,requestheader":                  {authModeRequestHeader, authModeMTLS},
		"jwt,mtls,token-review,requestheader": {authModeRequestHeader, authModeMTLS, authModeJWT, authModeTokenReview},
	} {
		modes, err := parseAuthModes(raw)
		if err != nil {
			t.Fatalf("parseAuthModes(%q): %v", raw, err)
		}
		if got := orderedAuthModes(modes); !slices.Equal(got, want) {
			t.Errorf("orderedAuthModes(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestMergeCallerHeaders(t *testing.T) {
	embedder := []string{testEmbedderHeader, strings.ToLower(testUserHeader)}
	if got := mergeCallerHeaders(embedder, nil); !slices.Equal(got, embedder) {
		t.Errorf("without a front proxy: %q, want the embedder's %q", got, embedder)
	}
	got := mergeCallerHeaders(embedder, []string{testUserHeader, testTenantHeader})
	if want := []string{testEmbedderHeader, strings.ToLower(testUserHeader), testTenantHeader}; !slices.Equal(got, want) {
		t.Errorf("merged = %q, want %q", got, want)
	}
	if len(embedder) != 2 {
		t.Errorf("the embedder's list was changed: %q", embedder)
	}
}

func TestRegisterFlagsReadsClientCertEnvVars(t *testing.T) {
	clearProxyEnv(t)
	env := map[string]string{
		mtlsClientCAFileEnv:        "/etc/proxy/workload-ca.crt",
		mtlsIdentitySourceEnv:      "uri",
		mtlsAllowedIdentitiesEnv:   testWorkloadURI + ", " + testOtherWorkloadURI,
		mtlsRequiredURIPrefixEnv:   testURIPrefix,
		headerClientCAFileEnv:      "/etc/proxy/gateway-ca.crt",
		headerNameSourceEnv:        "dns",
		headerAllowedNamesEnv:      "egress.gateways.example.test",
		headerUsernameHeadersEnv:   testUserHeader,
		headerUIDHeadersEnv:        testUIDHeader,
		headerGroupHeadersEnv:      testGroupHeader,
		headerPartitionHeadersEnv:  testTenantHeader,
		headerAllowedPartitionsEnv: "team-a, team-b",
	}
	for name, value := range env {
		t.Setenv(name, value)
	}
	cfg := parseTestFlags(t)
	flags := cfg.clientCert.flags()
	if len(flags) != len(env) {
		t.Fatalf("%d certificate flags, %d variables in this test", len(flags), len(env))
	}
	for _, f := range flags {
		if want, ok := env[f.env]; !ok || *f.value != want {
			t.Errorf("--%s (%s): parsed %q, want %q", f.name, f.env, *f.value, want)
		}
		if !slices.Contains(proxyEnvVars, f.env) {
			t.Errorf("proxyEnvVars does not list %s", f.env)
		}
	}

	// A flag overrides its environment variable, and its value is trimmed before use.
	cfg = parseTestFlags(t, "--mtls-identity-source= cn ", "--requestheader-username-headers=X-Other-User")
	trimAuthFlags(&cfg)
	if cfg.clientCert.mtlsIdentitySource != "cn" || cfg.clientCert.headerUsernameHeaders != "X-Other-User" {
		t.Errorf("mtlsIdentitySource = %q, headerUsernameHeaders = %q; want the trimmed flag values", cfg.clientCert.mtlsIdentitySource, cfg.clientCert.headerUsernameHeaders)
	}
}

// Every configuration of the certificate modes the binary refuses to start with, by the
// flags an operator set and never by a Go field of pkg/mtlsauth.
func TestBuildHandlerRefusesCertificateModeFlags(t *testing.T) {
	p := newCertProxy(t)
	absent := filepath.Join(t.TempDir(), "absent-ca.crt")
	garbage := filepath.Join(t.TempDir(), "garbage-ca.crt")
	writeBundle(t, garbage, []byte(testNotABundle))
	issuer := newTestJWTIssuer(t)
	jwtFlags := []string{"--jwt-issuer=" + issuer.srv.URL, "--jwt-audiences=" + testJWTAudience, "--jwt-ca-file=" + issuer.caFile}
	reviewFlags := []string{"--allowed-callers=" + testReviewCaller, "--kube-api-server=" + testUnusedAPIServer, "--kube-ca-file=", "--kube-token-file="}

	// without returns args with the flags named in drop removed; with returns args followed
	// by more, a later value of a flag replacing an earlier one.
	without := func(args []string, drop ...string) []string { return replaceFlags(args, drop, nil) }
	with := func(args []string, more ...string) []string { return append(slices.Clone(args), more...) }
	mtls := with(p.mtlsArgs(), "--auth-mode=mtls")
	header := with(p.headerArgs(), "--auth-mode=requestheader")
	both := with(append(p.mtlsArgs(), p.headerArgs()...), "--auth-mode=mtls,requestheader")
	mtlsJWT := with(append(p.mtlsArgs(), jwtFlags...), "--auth-mode=mtls,jwt")
	headerJWT := with(append(p.headerArgs(), jwtFlags...), "--auth-mode=requestheader,jwt")

	type refusal struct {
		name string
		args []string
		want string
	}
	cases := []refusal{
		// The listener.
		{"mtls without a serving certificate", with(mtls, "--tls-cert-file="), "requires --" + tlsCertFileFlag + " and --" + tlsKeyFileFlag},
		{"mtls without a serving key", with(mtls, "--tls-key-file="), "requires --" + tlsCertFileFlag + " and --" + tlsKeyFileFlag},
		{"requestheader without a serving certificate", with(header, "--tls-cert-file=", "--tls-key-file="), "requires --" + tlsCertFileFlag + " and --" + tlsKeyFileFlag},
		{"none with mtls", with(mtls, "--auth-mode=none,mtls"), "cannot be combined with another mode"},
		{"mtls listed twice", with(mtls, "--auth-mode=mtls,mtls"), "listed twice"},

		// mtls.
		{"mtls without a CA file", without(mtls, mtlsClientCAFileFlag), "--" + mtlsClientCAFileFlag + " (or " + mtlsClientCAFileEnv + ") is required"},
		{"mtls without an identity source", without(mtls, mtlsIdentitySourceFlag, mtlsRequiredURIPrefixFlag), "--" + mtlsIdentitySourceFlag + " (or " + mtlsIdentitySourceEnv + ") is required"},
		{"mtls with an unknown identity source", with(without(mtls, mtlsRequiredURIPrefixFlag), "--mtls-identity-source=email"), "invalid --" + mtlsIdentitySourceFlag},
		{"URI prefix with a DNS identity", with(mtls, "--mtls-identity-source=dns"), "--" + mtlsRequiredURIPrefixFlag + " requires --" + mtlsIdentitySourceFlag + "=uri"},
		{"URI prefix with a common name identity", with(mtls, "--mtls-identity-source=cn"), "--" + mtlsRequiredURIPrefixFlag + " requires --" + mtlsIdentitySourceFlag + "=uri"},
		{"mtls chained with jwt on DNS names", with(without(mtlsJWT, mtlsRequiredURIPrefixFlag), "--mtls-identity-source=dns"), `"mtls" listed with another mode`},
		{"mtls chained with jwt without a URI prefix", without(mtlsJWT, mtlsRequiredURIPrefixFlag), `"mtls" listed with another mode`},
		{"mtls chained with token-review without a URI prefix", with(without(with(mtls, reviewFlags...), mtlsRequiredURIPrefixFlag), "--auth-mode=mtls,token-review"), `"mtls" listed with another mode`},
		{"mtls chained with requestheader without a URI prefix", without(both, mtlsRequiredURIPrefixFlag), `"mtls" listed with another mode`},
		{"allowed identities of only separators", with(mtls, "--mtls-allowed-identities=, ,"), "--" + mtlsAllowedIdentitiesFlag + " (or " + mtlsAllowedIdentitiesEnv + ") names nothing"},

		// requestheader.
		{"requestheader without a CA file", without(header, headerClientCAFileFlag), "--" + headerClientCAFileFlag + " (or " + headerClientCAFileEnv + ") is required"},
		{"requestheader without a name source", without(header, headerNameSourceFlag), "--" + headerNameSourceFlag + " (or " + headerNameSourceEnv + ") is required"},
		{"requestheader with an unknown name source", with(header, "--requestheader-name-source=URI"), "invalid --" + headerNameSourceFlag},
		{"requestheader without allowed names", without(header, headerAllowedNamesFlag), "--" + headerAllowedNamesFlag + " (or " + headerAllowedNamesEnv + ") is required"},
		{"allowed names of only separators", with(header, "--requestheader-allowed-names=,"), "--" + headerAllowedNamesFlag + " (or " + headerAllowedNamesEnv + ") names nothing"},
		{"requestheader without username headers", without(header, headerUsernameHeadersFlag), "--" + headerUsernameHeadersFlag + " (or " + headerUsernameHeadersEnv + ") is required"},
		{"username headers of only separators", with(header, "--requestheader-username-headers=,,"), "--" + headerUsernameHeadersFlag + " (or " + headerUsernameHeadersEnv + ") names nothing"},
		{"UID headers of only separators", with(header, "--requestheader-uid-headers=,"), "--" + headerUIDHeadersFlag + " (or " + headerUIDHeadersEnv + ") names nothing"},
		{"allowed partitions of only separators", with(header, "--requestheader-allowed-partitions=,"), "--" + headerAllowedPartitionsFlag + " (or " + headerAllowedPartitionsEnv + ") names nothing"},
		{"allowed partitions without partition headers", with(without(header, headerPartitionHeadersFlag), "--requestheader-allowed-partitions="+testTeamA), "--" + headerAllowedPartitionsFlag + " requires --" + headerPartitionHeadersFlag},

		// A mode's flags without the mode.
		{"mtls flags with requestheader alone", with(header, p.mtlsArgs()...), "--" + mtlsClientCAFileFlag + ", --" + mtlsIdentitySourceFlag + ", --" + mtlsRequiredURIPrefixFlag + ` requires "mtls" in --auth-mode`},
		{"requestheader flags with mtls alone", with(mtls, "--requestheader-username-headers="+testUserHeader), "--" + headerUsernameHeadersFlag + ` requires "requestheader" in --auth-mode`},

		// --partition-by-caller.
		{"partition with mtls", with(mtls, "--partition-by-caller=partition"), `cannot be combined with "mtls"`},
		{"partition with mtls and requestheader", with(both, "--partition-by-caller=partition"), `cannot be combined with "mtls"`},
		{"partition with mtls and jwt", with(mtlsJWT, "--partition-by-caller=partition", "--jwt-partition-claim=tenant"), `cannot be combined with "mtls"`},
		{"partition with requestheader without partition headers", with(without(header, headerPartitionHeadersFlag), "--partition-by-caller=partition"), `with "requestheader" in --auth-mode requires --` + headerPartitionHeadersFlag},
		{"partition with requestheader and jwt without a claim", with(headerJWT, "--partition-by-caller=partition"), `requires "jwt" in --auth-mode and --` + jwtPartitionClaimFlag},
		{"username with mtls and jwt", with(mtlsJWT, "--partition-by-caller=username"), "cannot be combined with more than one mode"},
		{"username with requestheader and jwt", with(headerJWT, "--partition-by-caller=username"), "cannot be combined with more than one mode"},
		{"username with mtls and requestheader", with(both, "--partition-by-caller=username"), "cannot be combined with more than one mode"},

		// What the library's constructors refuse, named by flag.
		{"allowed identity that is not a URI name", with(mtls, "--mtls-allowed-identities=SPIFFE://example.test/ns/agents/sa/sandbox"), "--" + mtlsAllowedIdentitiesFlag + " entry"},
		{"URI prefix cut inside a path segment", with(mtls, "--mtls-required-uri-prefix=spiffe://example.test/ns/agents"), "--" + mtlsRequiredURIPrefixFlag + ` "spiffe://example.test/ns/agents" must end in /`},
		{"URI prefix without a host", with(mtls, "--mtls-required-uri-prefix=spiffe:///"), "--" + mtlsRequiredURIPrefixFlag},
		{"mtls CA file that does not exist", with(mtls, "--mtls-client-ca-file="+absent), "--" + mtlsClientCAFileFlag + ": "},
		{"mtls CA file that is not a bundle", with(mtls, "--mtls-client-ca-file="+garbage), "--" + mtlsClientCAFileFlag + ": "},
		{"allowed front proxy name that is not a URI name", with(header, "--requestheader-allowed-names=spiffe://example.test/a%20b"), "--" + headerAllowedNamesFlag + " entry"},
		{"username header that is not a header name", with(header, "--requestheader-username-headers=not a header"), "--" + headerUsernameHeadersFlag + " entry"},
		{"group header the proxy needs", with(header, "--requestheader-group-headers=Host"), "--" + headerGroupHeadersFlag + " entry"},
		{"one header in two lists", with(header, "--requestheader-partition-headers="+strings.ToLower(testUserHeader)), "is already listed in --" + headerUsernameHeadersFlag},
		{"requestheader CA file that does not exist", with(header, "--requestheader-client-ca-file="+absent), "--" + headerClientCAFileFlag + ": "},
	}
	// Each flag alone, under a mode that is neither of the two.
	var probe clientCertConfig
	for _, f := range probe.mtlsFlags() {
		cases = append(cases, refusal{"--" + f.name + " without mtls", []string{"--auth-mode=none", "--" + f.name + "=value"}, "--" + f.name + ` requires "mtls" in --auth-mode`})
	}
	for _, f := range probe.requestHeaderFlags() {
		cases = append(cases, refusal{"--" + f.name + " without requestheader", with(jwtFlags, "--auth-mode=jwt", "--"+f.name+"=value"), "--" + f.name + ` requires "requestheader" in --auth-mode`})
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg, mode := p.config(t, Options{}, tc.args...)
			_, _, err := newServer(context.Background(), cfg, mode, slog.New(slog.DiscardHandler), nil, nil)
			if err == nil {
				t.Fatal("the server was built, want a startup error")
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("err = %q, want substring %q", err, tc.want)
			}
			for _, name := range libraryNames {
				if strings.Contains(err.Error(), name) {
					t.Errorf("err = %q names %s, which no operator set", err, name)
				}
			}
		})
	}

	// The same flags, complete, start.
	for name, args := range map[string][]string{"mtls": mtls, "requestheader": header, "both": both, "mtls,jwt": mtlsJWT, "requestheader,jwt": headerJWT,
		"requestheader by partition":                  with(header, "--partition-by-caller=partition"),
		"requestheader and jwt by partition":          with(headerJWT, "--partition-by-caller=partition", "--jwt-partition-claim=tenant"),
		"requestheader and token-review by partition": with(with(header, reviewFlags...), "--auth-mode=token-review,requestheader", "--partition-by-caller=partition"),
	} {
		t.Run("starts with "+name, func(t *testing.T) {
			cfg, mode := p.config(t, Options{}, args...)
			if _, _, err := newServer(context.Background(), cfg, mode, slog.New(slog.DiscardHandler), nil, nil); err != nil {
				t.Fatalf("newServer: %v", err)
			}
		})
	}
}

// One CA file for both certificate modes is refused when the two modes read the name from
// different certificate fields, whichever way the two flags spell the path, and is
// started with a warning when they read the same field. Two files are neither.
func TestOneClientCAFileForBothCertificateModes(t *testing.T) {
	p := newCertProxy(t)
	link := filepath.Join(t.TempDir(), "linked-ca.crt")
	if err := os.Symlink(p.workloadCAFile, link); err != nil {
		t.Fatal(err)
	}
	unclean := filepath.Dir(p.workloadCAFile) + string(filepath.Separator) + "." + string(filepath.Separator) + filepath.Base(p.workloadCAFile)
	both := append(append(p.mtlsArgs(), p.headerArgs()...), "--auth-mode=requestheader,mtls")
	build := func(t *testing.T, args ...string) (string, error) {
		t.Helper()
		var logs bytes.Buffer
		cfg, mode := p.config(t, Options{}, append(slices.Clone(both), args...)...)
		_, _, err := newServer(context.Background(), cfg, mode, slog.New(slog.NewJSONHandler(&logs, nil)), nil, nil)
		return logs.String(), err
	}
	const sameFile, dnsNames = "name the same file", "--requestheader-name-source=dns"

	for name, path := range map[string]string{"the same path": p.workloadCAFile, "an unclean path": unclean, "a symlink": link} {
		t.Run("refuses "+name+" read from different fields", func(t *testing.T) {
			_, err := build(t, "--requestheader-client-ca-file="+path, dnsNames, "--requestheader-allowed-names="+testWorkloadDNS)
			if err == nil || !strings.Contains(err.Error(), sameFile) || !strings.Contains(err.Error(), "--"+headerNameSourceFlag) {
				t.Fatalf("err = %v, want a refusal naming the shared file and --%s", err, headerNameSourceFlag)
			}
		})
		t.Run("warns of "+name+" read from the same field", func(t *testing.T) {
			logs, err := build(t, "--requestheader-client-ca-file="+path)
			if err != nil {
				t.Fatalf("newServer: %v", err)
			}
			for _, want := range []string{`"level":"WARN"`, sameFile, "--" + headerAllowedNamesFlag, testGatewayURI} {
				if !strings.Contains(logs, want) {
					t.Errorf("logs = %s, want them to contain %s", logs, want)
				}
			}
		})
	}
	for name, args := range map[string][]string{"the same field": nil, "different fields": {dnsNames, "--requestheader-allowed-names=" + testWorkloadDNS}} {
		t.Run("two files read from "+name, func(t *testing.T) {
			logs, err := build(t, args...)
			if err != nil {
				t.Fatalf("newServer: %v", err)
			}
			if strings.Contains(logs, sameFile) {
				t.Errorf("logs = %s, want no warning for two CA files", logs)
			}
		})
	}
}

// NewHandler starts no listener, so a certificate mode does not need the serving files
// there; the server Run starts does.
func TestNewHandlerNeedsNoServingCertificateForACertificateMode(t *testing.T) {
	p := newCertProxy(t)
	for _, mode := range [][]string{
		append(p.mtlsArgs(), "--auth-mode=mtls"),
		append(p.headerArgs(), "--auth-mode=requestheader"),
	} {
		args := append(append(p.baseArgs(), mode...), "--tls-cert-file=", "--tls-key-file=")
		if _, err := NewHandler(context.Background(), testFlagSet(), args, Options{}); err != nil {
			t.Errorf("NewHandler(%q): %v", args, err)
		}
		cfg, proxyMode := p.config(t, Options{}, append(slices.Clone(mode), "--tls-cert-file=", "--tls-key-file=")...)
		_, _, err := newServer(context.Background(), cfg, proxyMode, slog.New(slog.DiscardHandler), nil, nil)
		if err == nil || !strings.Contains(err.Error(), "requires --"+tlsCertFileFlag) {
			t.Errorf("newServer(%q): err = %v, want the serving files required", args, err)
		}
	}
}

// The per-handshake configuration is the listener's own with the client certificate
// request added: a field of the base it does not set carries over.
func TestVerifyClientCertificatesKeepsTheBaseConfiguration(t *testing.T) {
	p := newCertProxy(t)
	pool, err := certreload.NewCAPool(p.workloadCAFile, certreload.Options{})
	if err != nil {
		t.Fatal(err)
	}
	curves := []tls.CurveID{tls.X25519}
	suites := []uint16{tls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256}
	getCertificate := func(*tls.ClientHelloInfo) (*tls.Certificate, error) { return nil, nil }
	base := &tls.Config{MinVersion: tls.VersionTLS13, CurvePreferences: curves, CipherSuites: suites, GetCertificate: getCertificate}
	forClient := verifyClientCertificates(base, pool)
	// What the server does next: the base gets the func, and net/http its protocols.
	base.GetConfigForClient = forClient
	base.NextProtos = []string{nextProtoHTTP1}

	got, err := forClient(&tls.ClientHelloInfo{})
	if err != nil {
		t.Fatal(err)
	}
	if got.MinVersion != tls.VersionTLS13 || !slices.Equal(got.CurvePreferences, curves) || !slices.Equal(got.CipherSuites, suites) || got.GetCertificate == nil {
		t.Errorf("MinVersion %x, CurvePreferences %v, CipherSuites %v, GetCertificate set %v; want the base's", got.MinVersion, got.CurvePreferences, got.CipherSuites, got.GetCertificate != nil)
	}
	if got.ClientAuth != tls.VerifyClientCertIfGiven || got.ClientCAs != pool.Pool() || !slices.Equal(got.NextProtos, []string{nextProtoHTTP2, nextProtoHTTP1}) {
		t.Errorf("ClientAuth %v, NextProtos %q; want VerifyClientCertIfGiven against the pool, offering HTTP/2", got.ClientAuth, got.NextProtos)
	}
	if got.GetConfigForClient != nil {
		t.Error("the per-handshake configuration carries GetConfigForClient")
	}
	if base.ClientAuth != tls.NoClientCert || base.ClientCAs != nil {
		t.Error("the base configuration was changed")
	}
}

// A constructor's error is reworded in the field it starts with only: a flag's value or a
// path that holds the name of a field is reported as the operator gave it.
func TestStartupErrorKeepsAValueThatHoldsAFieldName(t *testing.T) {
	p := newCertProxy(t)
	name := "spiffe://example.test/UsernameHeaders/IdentitySource/a%20b"
	absent := filepath.Join(t.TempDir(), "ClientCAFile", "IdentitySource.crt")
	for _, tc := range []struct {
		args []string
		want []string
	}{
		{append(p.headerArgs(), "--auth-mode=requestheader", "--requestheader-allowed-names="+name), []string{"--" + headerAllowedNamesFlag + " entry", name}},
		{append(p.mtlsArgs(), "--auth-mode=mtls", "--mtls-allowed-identities="+name), []string{"--" + mtlsAllowedIdentitiesFlag + " entry", name}},
		{append(p.mtlsArgs(), "--auth-mode=mtls", "--mtls-client-ca-file="+absent), []string{"--" + mtlsClientCAFileFlag + ": ", absent}},
		{append(p.headerArgs(), "--auth-mode=requestheader", "--requestheader-client-ca-file="+absent), []string{"--" + headerClientCAFileFlag + ": ", absent}},
	} {
		cfg, mode := p.config(t, Options{}, tc.args...)
		_, _, err := newServer(context.Background(), cfg, mode, slog.New(slog.DiscardHandler), nil, nil)
		if err == nil {
			t.Fatalf("newServer(%q) succeeded, want a startup error", tc.args)
		}
		for _, want := range tc.want {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("err = %q, want it to contain %q", err, want)
			}
		}
		for _, library := range []string{"mtlsauth", "ClientCertConfig", "FrontProxyConfig"} {
			if strings.Contains(err.Error(), library) {
				t.Errorf("err = %q names %s", err, library)
			}
		}
	}
}
