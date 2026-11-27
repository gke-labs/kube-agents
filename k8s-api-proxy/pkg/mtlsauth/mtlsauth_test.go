package mtlsauth

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/asn1"
	"encoding/pem"
	"errors"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testInterval = time.Minute
	testValidity = 24 * time.Hour
	// testShortLife is the validity left on a certificate a test lets expire: longer than
	// the reload interval, far shorter than the CA's.
	testShortLife = 10 * time.Minute
	caFileName    = "ca.crt"
	proxyCAName   = "proxy-ca.crt"
	pemTypeCert   = "CERTIFICATE"

	testWorkloadURI  = "spiffe://example.test/ns/agents/sa/sandbox"
	testOtherURI     = "spiffe://example.test/ns/agents/sa/other"
	testURIPrefix    = "spiffe://example.test/ns/agents/"
	testWorkloadDNS  = "sandbox.agents.example.test"
	testOtherDNS     = "other.agents.example.test"
	testWorkloadCN   = "sandbox"
	testProxyURI     = "spiffe://example.test/ns/gateways/sa/egress"
	testUserHeader   = "X-Caller-User"
	testUserHeader2  = "X-Caller-User-Fallback"
	testUIDHeader    = "X-Caller-Uid"
	testGroupHeader  = "X-Caller-Group"
	testGroupHeader2 = "X-Caller-Team"
	testTenantHeader = "X-Caller-Tenant"
	testUser         = "alice"
	testTenant       = "tenant-a"
)

var (
	serialNumber atomic.Int64
	// mtimeTick gives every write a distinct modification time: a filesystem's timestamp
	// granularity could otherwise make two quick same-size writes look unchanged.
	mtimeTick atomic.Int64

	clientUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}
	serverUsage = []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}
)

// fakeClock is the time certificates are verified at and the CA bundle reloader's clock.
type fakeClock struct{ nanos atomic.Int64 }

func newClock() *fakeClock {
	c := &fakeClock{}
	c.nanos.Store(time.Now().UnixNano())
	return c
}

func (c *fakeClock) now() time.Time { return time.Unix(0, c.nanos.Load()) }

func (c *fakeClock) advance(d time.Duration) { c.nanos.Add(int64(d)) }

// passReloadInterval moves the clock far enough for the next request to check the bundle.
func (c *fakeClock) passReloadInterval() { c.advance(testInterval + time.Nanosecond) }

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

func reloadOptions(clock *fakeClock, log *errorLog) certreload.Options {
	return certreload.Options{CheckInterval: testInterval, OnReloadError: log.record, Now: clock.now}
}

type authority struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func nextSerial() *big.Int { return big.NewInt(serialNumber.Add(1)) }

func newKey(t *testing.T) *ecdsa.PrivateKey {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

// newAuthority returns a CA: self-signed when parent is nil, else an intermediate of it.
func newAuthority(t *testing.T, name string, parent *authority) *authority {
	t.Helper()
	key := newKey(t)
	tmpl := &x509.Certificate{
		SerialNumber:          nextSerial(),
		Subject:               pkix.Name{CommonName: name},
		NotBefore:             time.Now().Add(-testValidity),
		NotAfter:              time.Now().Add(testValidity),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign,
	}
	issuer, issuerKey := tmpl, key
	if parent != nil {
		issuer, issuerKey = parent.cert, parent.key
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, issuer, &key.PublicKey, issuerKey)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &authority{cert: cert, key: key, pem: pem.EncodeToMemory(&pem.Block{Type: pemTypeCert, Bytes: der})}
}

// certSpec is the content of a leaf certificate under test.
type certSpec struct {
	cn string
	// cns, when set, are the subject's common name attributes in place of cn: more than
	// one of them.
	cns  []string
	uris []string
	// rawURIs, when set, are the URI SANs written as given, in place of uris and dns:
	// forms net/url would not write back, such as an empty fragment.
	rawURIs []string
	dns     []string
	// usages are the extended key usages; nil leaves the extension out.
	usages []x509.ExtKeyUsage
	// notBefore and notAfter default to a day either side of now.
	notBefore time.Time
	notAfter  time.Time
}

type issued struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
}

func (a *authority) issue(t *testing.T, spec certSpec) *issued {
	t.Helper()
	key := newKey(t)
	tmpl := &x509.Certificate{
		SerialNumber: nextSerial(),
		Subject:      pkix.Name{CommonName: spec.cn},
		DNSNames:     spec.dns,
		NotBefore:    time.Now().Add(-testValidity),
		NotAfter:     time.Now().Add(testValidity),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  spec.usages,
	}
	// pkix.Name writes ExtraNames in place of CommonName, not beside it.
	for _, cn := range spec.cns {
		tmpl.Subject.ExtraNames = append(tmpl.Subject.ExtraNames, pkix.AttributeTypeAndValue{Type: oidCommonName, Value: cn})
	}
	for _, raw := range spec.uris {
		uri, err := url.Parse(raw)
		if err != nil {
			t.Fatal(err)
		}
		tmpl.URIs = append(tmpl.URIs, uri)
	}
	if len(spec.rawURIs) > 0 {
		var names []byte
		for _, raw := range spec.rawURIs {
			name, err := asn1.Marshal(asn1.RawValue{Class: asn1.ClassContextSpecific, Tag: sanURITag, Bytes: []byte(raw)})
			if err != nil {
				t.Fatal(err)
			}
			names = append(names, name...)
		}
		san, err := asn1.Marshal(asn1.RawValue{Class: asn1.ClassUniversal, Tag: asn1.TagSequence, IsCompound: true, Bytes: names})
		if err != nil {
			t.Fatal(err)
		}
		tmpl.ExtraExtensions = []pkix.Extension{{Id: oidSubjectAltName, Value: san}}
	}
	if !spec.notBefore.IsZero() {
		tmpl.NotBefore = spec.notBefore
	}
	if !spec.notAfter.IsZero() {
		tmpl.NotAfter = spec.notAfter
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, a.cert, &key.PublicKey, a.key)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return &issued{cert: cert, key: key}
}

// workload issues a client certificate carrying one URI SAN.
func (a *authority) workload(t *testing.T, uri string) *issued {
	t.Helper()
	return a.issue(t, certSpec{uris: []string{uri}, usages: clientUsage})
}

func (i *issued) tlsCertificate() tls.Certificate {
	return tls.Certificate{Certificate: [][]byte{i.cert.Raw}, PrivateKey: i.key, Leaf: i.cert}
}

// connection is the state of a TLS connection whose peer presented leaf and the given
// intermediates, as net/http hands it to a handler in Request.TLS.
func connection(leaf *issued, intermediates ...*authority) *tls.ConnectionState {
	state := &tls.ConnectionState{PeerCertificates: []*x509.Certificate{leaf.cert}}
	for _, ca := range intermediates {
		state.PeerCertificates = append(state.PeerCertificates, ca.cert)
	}
	return state
}

// repeat returns ca n times, for a peer that sends the same intermediate over and over.
func repeat(ca *authority, n int) []*authority {
	out := make([]*authority, n)
	for i := range out {
		out[i] = ca
	}
	return out
}

// request is a request that arrived on a connection in the given state; nil for plain
// HTTP. headers alternate name and value.
func request(state *tls.ConnectionState, headers ...string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, "/api", nil)
	r.TLS = state
	for i := 0; i+1 < len(headers); i += 2 {
		r.Header.Add(headers[i], headers[i+1])
	}
	return r
}

// writeFile writes data in place and gives the file a modification time no other write had.
func writeFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
	mtime := time.Now().Add(time.Duration(mtimeTick.Add(1)) * time.Second)
	if err := os.Chtimes(path, mtime, mtime); err != nil {
		t.Fatal(err)
	}
}

// caFile writes ca's certificate to a new file and returns its path.
func caFile(t *testing.T, name string, ca *authority) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	writeFile(t, path, ca.pem)
	return path
}

// fixture is one CA with a bundle file, a clock and a reload error log.
type fixture struct {
	ca    *authority
	file  string
	clock *fakeClock
	log   *errorLog
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	ca := newAuthority(t, "client-ca", nil)
	return &fixture{ca: ca, file: caFile(t, caFileName, ca), clock: newClock(), log: &errorLog{}}
}

// clientCert builds a ClientCertAuthenticator on the fixture; edit adjusts its config.
func (f *fixture) clientCert(t *testing.T, source IdentitySource, edit func(*ClientCertConfig)) *ClientCertAuthenticator {
	t.Helper()
	cfg := ClientCertConfig{
		ClientCAFile:   f.file,
		IdentitySource: source,
		Reload:         reloadOptions(f.clock, f.log),
		Now:            f.clock.now,
	}
	if edit != nil {
		edit(&cfg)
	}
	authn, err := NewClientCertAuthenticator(cfg)
	if err != nil {
		t.Fatalf("NewClientCertAuthenticator: %v", err)
	}
	return authn
}

// frontProxyConfig is a complete FrontProxyConfig on the fixture, admitting testProxyURI.
func (f *fixture) frontProxyConfig() FrontProxyConfig {
	return FrontProxyConfig{
		ClientCAFile:    f.file,
		NameSource:      SourceURI,
		AllowedNames:    []string{testProxyURI},
		UsernameHeaders: []string{testUserHeader},
		Reload:          reloadOptions(f.clock, f.log),
		Now:             f.clock.now,
	}
}

func (f *fixture) frontProxy(t *testing.T, edit func(*FrontProxyConfig)) *FrontProxyAuthenticator {
	t.Helper()
	cfg := f.frontProxyConfig()
	if edit != nil {
		edit(&cfg)
	}
	authn, err := NewFrontProxyAuthenticator(cfg)
	if err != nil {
		t.Fatalf("NewFrontProxyAuthenticator: %v", err)
	}
	return authn
}

// wantFallThrough fails the test unless err is a refusal a chain follows with its next
// authenticator: neither final nor a failure to decide.
func wantFallThrough(t *testing.T, err error) {
	t.Helper()
	if err == nil {
		t.Fatal("the request was admitted, want a refusal")
	}
	if errors.Is(err, k8sapiproxy.ErrCallerRefused) {
		t.Errorf("err = %v wraps ErrCallerRefused, want a refusal the next authenticator may follow", err)
	}
	if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("err = %v wraps ErrAuthenticationUnavailable, want a refusal", err)
	}
}

// wantFinal fails the test unless err is a refusal that ends a chain.
func wantFinal(t *testing.T, err error) {
	t.Helper()
	if !errors.Is(err, k8sapiproxy.ErrCallerRefused) {
		t.Errorf("err = %v, want one wrapping ErrCallerRefused", err)
	}
	if errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("err = %v wraps ErrAuthenticationUnavailable, want a refusal", err)
	}
}

func TestClientCertReadsOneConfiguredField(t *testing.T) {
	f := newFixture(t)
	tests := []struct {
		name     string
		source   IdentitySource
		spec     certSpec
		username string
		wantErr  error
	}{
		{name: "uri", source: SourceURI, spec: certSpec{cn: testWorkloadCN, dns: []string{testWorkloadDNS}, uris: []string{testWorkloadURI}}, username: testWorkloadURI},
		{name: "uri absent, other fields present", source: SourceURI, spec: certSpec{cn: testWorkloadCN, dns: []string{testWorkloadDNS}}, wantErr: errNoName},
		{name: "two URIs", source: SourceURI, spec: certSpec{uris: []string{testWorkloadURI, testOtherURI}}, wantErr: errSeveralNames},
		{name: "dns", source: SourceDNS, spec: certSpec{cn: testWorkloadCN, dns: []string{testWorkloadDNS}, uris: []string{testWorkloadURI}}, username: testWorkloadDNS},
		{name: "dns absent, other fields present", source: SourceDNS, spec: certSpec{cn: testWorkloadCN, uris: []string{testWorkloadURI}}, wantErr: errNoName},
		{name: "two DNS names", source: SourceDNS, spec: certSpec{dns: []string{testWorkloadDNS, testOtherDNS}}, wantErr: errSeveralNames},
		{name: "cn", source: SourceCN, spec: certSpec{cn: testWorkloadCN, dns: []string{testWorkloadDNS}, uris: []string{testWorkloadURI}}, username: testWorkloadCN},
		{name: "cn absent, other fields present", source: SourceCN, spec: certSpec{dns: []string{testWorkloadDNS}, uris: []string{testWorkloadURI}}, wantErr: errNoName},
		{name: "two common names", source: SourceCN, spec: certSpec{cns: []string{testWorkloadCN, "other"}}, wantErr: errSeveralNames},
		{name: "uri scheme is lower-cased", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{"SPIFFE://example.test/ns/agents/sa/sandbox"}}, username: testWorkloadURI},
		{name: "uri written as given", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{testWorkloadURI}}, username: testWorkloadURI},
		{name: "uri with userinfo", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{"spiffe://user@example.test/ns/agents/sa/sandbox"}}, wantErr: errURINotPlain},
		{name: "uri with a query", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{testWorkloadURI + "?a=b"}}, wantErr: errURINotPlain},
		{name: "uri with an empty query", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{testWorkloadURI + "?"}}, wantErr: errURINotPlain},
		{name: "uri with a fragment", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{testWorkloadURI + "#a"}}, wantErr: errURINotPlain},
		{name: "uri with an empty fragment", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{testWorkloadURI + "#"}}, wantErr: errURINotPlain},
		{name: "two URIs written as given", source: SourceURI, spec: certSpec{cn: testWorkloadCN, rawURIs: []string{testWorkloadURI, testOtherURI}}, wantErr: errSeveralNames},
		{name: "two URIs do not matter to dns", source: SourceDNS, spec: certSpec{dns: []string{testWorkloadDNS}, uris: []string{testWorkloadURI, testOtherURI}}, username: testWorkloadDNS},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			authn := f.clientCert(t, tc.source, nil)
			tc.spec.usages = clientUsage
			caller, err := authn.AuthenticateRequest(request(connection(f.ca.issue(t, tc.spec))))
			if tc.wantErr != nil {
				wantFallThrough(t, err)
				if !errors.Is(err, tc.wantErr) {
					t.Errorf("err = %v, want %v", err, tc.wantErr)
				}
				return
			}
			if err != nil {
				t.Fatalf("AuthenticateRequest: %v", err)
			}
			want := k8sapiproxy.CallerIdentity{Username: tc.username}
			if caller.Username != want.Username || caller.UID != "" || caller.Partition != "" || len(caller.Groups) != 0 || caller.Extra != nil {
				t.Errorf("caller = %+v, want only Username %q", *caller, tc.username)
			}
		})
	}
}

func TestClientCertVerifiesTheCertificateOnEachRequest(t *testing.T) {
	f := newFixture(t)
	otherCA := newAuthority(t, "other-ca", nil)
	intermediate := newAuthority(t, "intermediate-ca", f.ca)
	start := f.clock.now()

	// reason reports whether err holds an x509 verification failure for that reason.
	reason := func(want x509.InvalidReason) func(error) bool {
		return func(err error) bool {
			var invalid x509.CertificateInvalidError
			return errors.As(err, &invalid) && invalid.Reason == want
		}
	}
	unknownAuthority := func(err error) bool {
		var unknown x509.UnknownAuthorityError
		return errors.As(err, &unknown)
	}
	tests := []struct {
		name  string
		state func(t *testing.T) *tls.ConnectionState
		// advance moves the clock before the request.
		advance time.Duration
		admit   bool
		wantErr error
		// detail, when set, must hold for the refusal.
		detail func(error) bool
	}{
		{
			name:    "plain HTTP",
			state:   func(*testing.T) *tls.ConnectionState { return nil },
			wantErr: errNoClientCertificate,
		},
		{
			name:    "TLS without a client certificate",
			state:   func(*testing.T) *tls.ConnectionState { return &tls.ConnectionState{} },
			wantErr: errNoClientCertificate,
		},
		{
			name: "as many certificates as are verified",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(intermediate.workload(t, testWorkloadURI), repeat(intermediate, maxPeerCertificates-1)...)
			},
			admit: true,
		},
		{
			name: "more certificates than are verified",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(intermediate.workload(t, testWorkloadURI), repeat(intermediate, maxPeerCertificates)...)
			},
			wantErr: errTooManyCertificates,
		},
		{
			name:  "client authentication usage",
			state: func(t *testing.T) *tls.ConnectionState { return connection(f.ca.workload(t, testWorkloadURI)) },
			admit: true,
		},
		{
			name: "no extended key usage extension",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{uris: []string{testWorkloadURI}}))
			},
			admit: true,
		},
		{
			name: "server authentication usage only",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{uris: []string{testWorkloadURI}, usages: serverUsage}))
			},
			wantErr: errNotVerified,
			detail:  reason(x509.IncompatibleUsage),
		},
		{
			name: "expired since the connection opened",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{uris: []string{testWorkloadURI}, usages: clientUsage, notAfter: start.Add(testShortLife)}))
			},
			advance: 2 * testShortLife,
			wantErr: errNotVerified,
			detail:  reason(x509.Expired),
		},
		{
			name: "not valid yet",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{uris: []string{testWorkloadURI}, usages: clientUsage, notBefore: start.Add(testValidity / 2)}))
			},
			wantErr: errNotVerified,
			detail:  reason(x509.Expired),
		},
		{
			name:    "signed by another CA",
			state:   func(t *testing.T) *tls.ConnectionState { return connection(otherCA.workload(t, testWorkloadURI)) },
			wantErr: errNotVerified,
			detail:  unknownAuthority,
		},
		{
			name: "through an intermediate the peer sent",
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(intermediate.workload(t, testWorkloadURI), intermediate)
			},
			admit: true,
		},
		{
			name:    "through an intermediate the peer did not send",
			state:   func(t *testing.T) *tls.ConnectionState { return connection(intermediate.workload(t, testWorkloadURI)) },
			wantErr: errNotVerified,
			detail:  unknownAuthority,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			clock := newClock()
			clock.nanos.Store(start.UnixNano())
			authn, err := NewClientCertAuthenticator(ClientCertConfig{
				ClientCAFile:   f.file,
				IdentitySource: SourceURI,
				Reload:         reloadOptions(clock, f.log),
				Now:            clock.now,
			})
			if err != nil {
				t.Fatalf("NewClientCertAuthenticator: %v", err)
			}
			state := tc.state(t)
			if tc.advance > 0 {
				// Good when the connection opened: only the clock separates the two requests.
				if _, err := authn.AuthenticateRequest(request(state)); err != nil {
					t.Fatalf("AuthenticateRequest before the clock moved: %v", err)
				}
				clock.advance(tc.advance)
			}
			caller, err := authn.AuthenticateRequest(request(state))
			if tc.admit {
				if err != nil {
					t.Fatalf("AuthenticateRequest: %v", err)
				}
				if caller.Username != testWorkloadURI {
					t.Errorf("Username = %q, want %q", caller.Username, testWorkloadURI)
				}
				return
			}
			wantFallThrough(t, err)
			if !errors.Is(err, tc.wantErr) {
				t.Errorf("err = %v, want %v", err, tc.wantErr)
			}
			if tc.detail != nil && !tc.detail(err) {
				t.Errorf("err = %v does not carry the expected x509 failure", err)
			}
		})
	}
}

func TestClientCertFilters(t *testing.T) {
	f := newFixture(t)
	tests := []struct {
		name   string
		source IdentitySource
		edit   func(*ClientCertConfig)
		spec   certSpec
		admit  bool
	}{
		{
			name:   "identity in the allowlist",
			source: SourceURI,
			edit: func(c *ClientCertConfig) {
				c.AllowedIdentities = []string{testOtherURI, " " + testWorkloadURI + " ", ""}
			},
			spec:  certSpec{uris: []string{testWorkloadURI}},
			admit: true,
		},
		{
			name:   "identity not in the allowlist",
			source: SourceURI,
			edit:   func(c *ClientCertConfig) { c.AllowedIdentities = []string{testOtherURI} },
			spec:   certSpec{uris: []string{testWorkloadURI}},
		},
		{
			name:   "allowlist entry is not a prefix match",
			source: SourceURI,
			edit:   func(c *ClientCertConfig) { c.AllowedIdentities = []string{testURIPrefix} },
			spec:   certSpec{uris: []string{testWorkloadURI}},
		},
		{
			name:   "allowlist is case-sensitive",
			source: SourceDNS,
			edit:   func(c *ClientCertConfig) { c.AllowedIdentities = []string{"Sandbox.agents.example.test"} },
			spec:   certSpec{dns: []string{testWorkloadDNS}},
		},
		{
			name:   "allowlist applies to a common name",
			source: SourceCN,
			edit:   func(c *ClientCertConfig) { c.AllowedIdentities = []string{testWorkloadCN} },
			spec:   certSpec{cn: testWorkloadCN},
			admit:  true,
		},
		{
			name:   "URI under the required prefix",
			source: SourceURI,
			edit:   func(c *ClientCertConfig) { c.RequiredURIPrefix = testURIPrefix },
			spec:   certSpec{uris: []string{testWorkloadURI}},
			admit:  true,
		},
		{
			name:   "URI outside the required prefix",
			source: SourceURI,
			edit:   func(c *ClientCertConfig) { c.RequiredURIPrefix = testURIPrefix },
			spec:   certSpec{uris: []string{testProxyURI}},
		},
		{
			name:   "longer path segment than the required prefix names",
			source: SourceURI,
			edit:   func(c *ClientCertConfig) { c.RequiredURIPrefix = testURIPrefix },
			spec:   certSpec{uris: []string{"spiffe://example.test/ns/agents-evil/sa/sandbox"}},
		},
		{
			name:   "longer trust domain than the required prefix names",
			source: SourceURI,
			edit:   func(c *ClientCertConfig) { c.RequiredURIPrefix = "spiffe://example.test/" },
			spec:   certSpec{uris: []string{"spiffe://example.test.evil/ns/agents/sa/sandbox"}},
		},
		{
			name:   "prefix and allowlist both apply",
			source: SourceURI,
			edit: func(c *ClientCertConfig) {
				c.RequiredURIPrefix = testURIPrefix
				c.AllowedIdentities = []string{testOtherURI}
			},
			spec: certSpec{uris: []string{testWorkloadURI}},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			authn := f.clientCert(t, tc.source, tc.edit)
			tc.spec.usages = clientUsage
			_, err := authn.AuthenticateRequest(request(connection(f.ca.issue(t, tc.spec))))
			if tc.admit {
				if err != nil {
					t.Errorf("AuthenticateRequest: %v", err)
				}
				return
			}
			wantFallThrough(t, err)
			if !errors.Is(err, errIdentityNotAdmitted) {
				t.Errorf("err = %v, want %v", err, errIdentityNotAdmitted)
			}
		})
	}
}

func TestNewClientCertAuthenticatorRefusesAnIncompleteConfig(t *testing.T) {
	f := newFixture(t)
	notPEM := filepath.Join(t.TempDir(), caFileName)
	writeFile(t, notPEM, []byte("not PEM"))
	tests := map[string]func(*ClientCertConfig){
		"no CA file":                    func(c *ClientCertConfig) { c.ClientCAFile = "" },
		"CA file missing":               func(c *ClientCertConfig) { c.ClientCAFile = filepath.Join(t.TempDir(), "absent.crt") },
		"CA file without a certificate": func(c *ClientCertConfig) { c.ClientCAFile = notPEM },
		"no identity source":            func(c *ClientCertConfig) { c.IdentitySource = "" },
		"unknown identity source":       func(c *ClientCertConfig) { c.IdentitySource = "email" },
		"identity source in upper case": func(c *ClientCertConfig) { c.IdentitySource = "URI" },
		"URI prefix with dns":           func(c *ClientCertConfig) { c.IdentitySource, c.RequiredURIPrefix = SourceDNS, testURIPrefix },
		"URI prefix with cn":            func(c *ClientCertConfig) { c.IdentitySource, c.RequiredURIPrefix = SourceCN, testURIPrefix },
		"URI prefix cut inside a path":  func(c *ClientCertConfig) { c.RequiredURIPrefix = "spiffe://example.test/ns/agents" },
		"URI prefix cut after the host": func(c *ClientCertConfig) { c.RequiredURIPrefix = "spiffe://example.test" },
		"allowlist of empty entries":    func(c *ClientCertConfig) { c.AllowedIdentities = []string{"", "  "} },
	}
	for name, edit := range tests {
		t.Run(name, func(t *testing.T) {
			cfg := ClientCertConfig{ClientCAFile: f.file, IdentitySource: SourceURI}
			edit(&cfg)
			if authn, err := NewClientCertAuthenticator(cfg); err == nil {
				t.Errorf("NewClientCertAuthenticator returned %v, want an error", authn)
			}
		})
	}
}

func TestFrontProxyReadsTheCallerFromHeaders(t *testing.T) {
	f := newFixture(t)
	proxy := connection(f.ca.workload(t, testProxyURI))
	full := func(c *FrontProxyConfig) {
		c.UsernameHeaders = []string{testUserHeader, testUserHeader2}
		c.UIDHeaders = []string{testUIDHeader}
		c.GroupHeaders = []string{testGroupHeader, testGroupHeader2}
		c.PartitionHeaders = []string{testTenantHeader}
	}
	allow := func(partitions ...string) func(*FrontProxyConfig) {
		return func(c *FrontProxyConfig) {
			full(c)
			c.AllowedPartitions = partitions
		}
	}
	tests := []struct {
		name    string
		edit    func(*FrontProxyConfig)
		headers []string
		want    *k8sapiproxy.CallerIdentity
		wantErr error
	}{
		{
			name:    "username only",
			headers: []string{testUserHeader, testUser},
			want:    &k8sapiproxy.CallerIdentity{Username: testUser},
		},
		{
			name: "every field",
			edit: full,
			headers: []string{
				testUserHeader, testUser, testUIDHeader, "uid-1", testGroupHeader, "dev", testGroupHeader, "ops, sre",
				testGroupHeader2, "platform", testGroupHeader2, "", testTenantHeader, testTenant,
			},
			want: &k8sapiproxy.CallerIdentity{Username: testUser, UID: "uid-1", Groups: []string{"dev", "ops, sre", "platform"}, Partition: testTenant},
		},
		{
			name:    "header names match in any case",
			edit:    func(c *FrontProxyConfig) { c.UsernameHeaders = []string{"x-caller-user"} },
			headers: []string{"X-CALLER-USER", testUser},
			want:    &k8sapiproxy.CallerIdentity{Username: testUser},
		},
		{
			name:    "second username header when the first is absent",
			edit:    func(c *FrontProxyConfig) { c.UsernameHeaders = []string{testUserHeader, testUserHeader2} },
			headers: []string{testUserHeader2, testUser},
			want:    &k8sapiproxy.CallerIdentity{Username: testUser},
		},
		{
			name:    "first username header wins",
			edit:    func(c *FrontProxyConfig) { c.UsernameHeaders = []string{testUserHeader, testUserHeader2} },
			headers: []string{testUserHeader2, "bob", testUserHeader, testUser},
			want:    &k8sapiproxy.CallerIdentity{Username: testUser},
		},
		{
			name:    "no groups sent",
			edit:    full,
			headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1", testTenantHeader, testTenant},
			want:    &k8sapiproxy.CallerIdentity{Username: testUser, UID: "uid-1", Partition: testTenant},
		},
		{name: "no username header", headers: nil, wantErr: errHeaderAbsent},
		{name: "empty username", headers: []string{testUserHeader, ""}, wantErr: errHeaderEmpty},
		{name: "blank username", headers: []string{testUserHeader, "  "}, wantErr: errHeaderEmpty},
		{name: "two usernames", headers: []string{testUserHeader, testUser, testUserHeader, "bob"}, wantErr: errHeaderRepeated},
		{name: "the same username twice", headers: []string{testUserHeader, testUser, testUserHeader, testUser}, wantErr: errHeaderRepeated},
		{
			name:    "empty first username header is not replaced by the second",
			edit:    func(c *FrontProxyConfig) { c.UsernameHeaders = []string{testUserHeader, testUserHeader2} },
			headers: []string{testUserHeader, "", testUserHeader2, testUser},
			wantErr: errHeaderEmpty,
		},
		{name: "UID header configured and absent", edit: full, headers: []string{testUserHeader, testUser, testTenantHeader, testTenant}, wantErr: errHeaderAbsent},
		{name: "two UIDs", edit: full, headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1", testUIDHeader, "uid-2", testTenantHeader, testTenant}, wantErr: errHeaderRepeated},
		{name: "partition header configured and absent", edit: full, headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1"}, wantErr: errHeaderAbsent},
		{name: "empty partition", edit: full, headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1", testTenantHeader, ""}, wantErr: errHeaderEmpty},
		{name: "two partitions", edit: full, headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1", testTenantHeader, testTenant, testTenantHeader, "tenant-b"}, wantErr: errHeaderRepeated},
		{
			name:    "partition in the allowlist",
			edit:    allow("tenant-b", " "+testTenant+" ", ""),
			headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1", testTenantHeader, testTenant},
			want:    &k8sapiproxy.CallerIdentity{Username: testUser, UID: "uid-1", Partition: testTenant},
		},
		{
			name:    "partition not in the allowlist",
			edit:    allow("tenant-b"),
			headers: []string{testUserHeader, testUser, testUIDHeader, "uid-1", testTenantHeader, testTenant},
			wantErr: errPartitionNotAllowed,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			authn := f.frontProxy(t, tc.edit)
			caller, err := authn.AuthenticateRequest(request(proxy, tc.headers...))
			if tc.wantErr != nil {
				wantFinal(t, err)
				if !errors.Is(err, tc.wantErr) {
					t.Errorf("err = %v, want %v", err, tc.wantErr)
				}
				if caller != nil {
					t.Errorf("caller = %+v with an error, want nil", *caller)
				}
				return
			}
			if err != nil {
				t.Fatalf("AuthenticateRequest: %v", err)
			}
			if caller.Username != tc.want.Username || caller.UID != tc.want.UID || caller.Partition != tc.want.Partition ||
				!equalStrings(caller.Groups, tc.want.Groups) || caller.Extra != nil || caller.Authenticator != "" {
				t.Errorf("caller = %+v, want %+v", *caller, *tc.want)
			}
		})
	}
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// A peer that is not an allowed front proxy is not believed whatever headers it sends, and
// the refusal leaves the request to the next authenticator.
func TestFrontProxyIgnoresHeadersFromAnyOtherPeer(t *testing.T) {
	f := newFixture(t)
	otherCA := newAuthority(t, "other-ca", nil)
	tests := []struct {
		name    string
		source  IdentitySource
		state   func(t *testing.T) *tls.ConnectionState
		wantErr error
	}{
		{name: "plain HTTP", source: SourceURI, state: func(*testing.T) *tls.ConnectionState { return nil }, wantErr: errNoClientCertificate},
		{
			name:    "certificate of another CA with an allowed name",
			source:  SourceURI,
			state:   func(t *testing.T) *tls.ConnectionState { return connection(otherCA.workload(t, testProxyURI)) },
			wantErr: errNotVerified,
		},
		{
			name:    "name not allowed",
			source:  SourceURI,
			state:   func(t *testing.T) *tls.ConnectionState { return connection(f.ca.workload(t, testWorkloadURI)) },
			wantErr: errProxyNotAllowed,
		},
		{
			name:   "allowed name beside a second URI",
			source: SourceURI,
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{uris: []string{testProxyURI, testOtherURI}, usages: clientUsage}))
			},
			wantErr: errSeveralNames,
		},
		{
			name:   "allowed name in a field that is not the configured one",
			source: SourceDNS,
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{cn: testProxyURI, uris: []string{testProxyURI}, usages: clientUsage}))
			},
			wantErr: errNoName,
		},
		{
			name:   "server certificate with an allowed name",
			source: SourceURI,
			state: func(t *testing.T) *tls.ConnectionState {
				return connection(f.ca.issue(t, certSpec{uris: []string{testProxyURI}, usages: serverUsage}))
			},
			wantErr: errNotVerified,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			authn := f.frontProxy(t, func(c *FrontProxyConfig) { c.NameSource = tc.source })
			caller, err := authn.AuthenticateRequest(request(tc.state(t), testUserHeader, testUser))
			wantFallThrough(t, err)
			if !errors.Is(err, tc.wantErr) {
				t.Errorf("err = %v, want %v", err, tc.wantErr)
			}
			if caller != nil {
				t.Errorf("caller = %+v, want nil", *caller)
			}
		})
	}
}

func TestFrontProxyNameSources(t *testing.T) {
	f := newFixture(t)
	tests := []struct {
		name   string
		source IdentitySource
		spec   certSpec
	}{
		{name: "dns", source: SourceDNS, spec: certSpec{dns: []string{testWorkloadDNS}}},
		{name: "cn", source: SourceCN, spec: certSpec{cn: testWorkloadCN}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			authn := f.frontProxy(t, func(c *FrontProxyConfig) {
				c.NameSource = tc.source
				c.AllowedNames = []string{testWorkloadDNS, testWorkloadCN}
			})
			tc.spec.usages = clientUsage
			caller, err := authn.AuthenticateRequest(request(connection(f.ca.issue(t, tc.spec)), testUserHeader, testUser))
			if err != nil {
				t.Fatalf("AuthenticateRequest: %v", err)
			}
			if caller.Username != testUser {
				t.Errorf("Username = %q, want %q", caller.Username, testUser)
			}
		})
	}
}

func TestNewFrontProxyAuthenticatorRefusesAnIncompleteConfig(t *testing.T) {
	f := newFixture(t)
	tests := map[string]func(*FrontProxyConfig){
		"no CA file":                        func(c *FrontProxyConfig) { c.ClientCAFile = "" },
		"CA file missing":                   func(c *FrontProxyConfig) { c.ClientCAFile = filepath.Join(t.TempDir(), "absent.crt") },
		"no name source":                    func(c *FrontProxyConfig) { c.NameSource = "" },
		"unknown name source":               func(c *FrontProxyConfig) { c.NameSource = "ip" },
		"no allowed names":                  func(c *FrontProxyConfig) { c.AllowedNames = nil },
		"allowed names of empty entries":    func(c *FrontProxyConfig) { c.AllowedNames = []string{"", " "} },
		"no username headers":               func(c *FrontProxyConfig) { c.UsernameHeaders = nil },
		"empty username header name":        func(c *FrontProxyConfig) { c.UsernameHeaders = []string{""} },
		"username header name with a space": func(c *FrontProxyConfig) { c.UsernameHeaders = []string{"X Caller"} },
		"username header name with a colon": func(c *FrontProxyConfig) { c.UsernameHeaders = []string{"X-Caller:"} },
		"UID header name with a newline":    func(c *FrontProxyConfig) { c.UIDHeaders = []string{"X-Uid\n"} },
		"group header name not ASCII":       func(c *FrontProxyConfig) { c.GroupHeaders = []string{"X-Grüppe"} },
		"partition header name empty":       func(c *FrontProxyConfig) { c.PartitionHeaders = []string{testTenantHeader, ""} },
		"username header the proxy frames":  func(c *FrontProxyConfig) { c.UsernameHeaders = []string{"Host"} },
		"group header the proxy drops":      func(c *FrontProxyConfig) { c.GroupHeaders = []string{"connection"} },
		"partition header content-length":   func(c *FrontProxyConfig) { c.PartitionHeaders = []string{"Content-Length"} },
		"one header in two fields":          func(c *FrontProxyConfig) { c.PartitionHeaders = []string{"x-caller-user"} },
		"one header twice in a field":       func(c *FrontProxyConfig) { c.GroupHeaders = []string{testGroupHeader, testGroupHeader} },
		"allowed partitions without header": func(c *FrontProxyConfig) { c.AllowedPartitions = []string{testTenant} },
		"allowed partitions of empty entries": func(c *FrontProxyConfig) {
			c.PartitionHeaders, c.AllowedPartitions = []string{testTenantHeader}, []string{"", " "}
		},
	}
	for name, edit := range tests {
		t.Run(name, func(t *testing.T) {
			cfg := f.frontProxyConfig()
			edit(&cfg)
			if authn, err := NewFrontProxyAuthenticator(cfg); err == nil {
				t.Errorf("NewFrontProxyAuthenticator returned %v, want an error", authn)
			}
		})
	}
}

func TestFrontProxyHeaderNames(t *testing.T) {
	f := newFixture(t)
	authn := f.frontProxy(t, func(c *FrontProxyConfig) {
		c.UsernameHeaders = []string{"x-caller-user", testUserHeader2}
		c.UIDHeaders = []string{testUIDHeader}
		c.GroupHeaders = []string{testGroupHeader}
		c.PartitionHeaders = []string{testTenantHeader}
	})
	want := []string{testUserHeader, testUserHeader2, testUIDHeader, testGroupHeader, testTenantHeader}
	if got := authn.HeaderNames(); !equalStrings(got, want) {
		t.Errorf("HeaderNames() = %q, want %q", got, want)
	}
}
