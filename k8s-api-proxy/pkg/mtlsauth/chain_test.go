package mtlsauth

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// partitionSeparator joins the username and the partition in the test server's reply.
	partitionSeparator = "|"
	httpMajorVersion2  = 2
)

// pair is the two authenticators of this package side by side, each with its own CA and
// bundle file, on one clock.
type pair struct {
	workloads, proxies *fixture
	clientCert         *ClientCertAuthenticator
	frontProxy         *FrontProxyAuthenticator
}

func newPair(t *testing.T) *pair {
	t.Helper()
	workloads := newFixture(t)
	proxyCA := newAuthority(t, "front-proxy-ca", nil)
	proxies := &fixture{ca: proxyCA, file: caFile(t, proxyCAName, proxyCA), clock: workloads.clock, log: workloads.log}
	return &pair{
		workloads:  workloads,
		proxies:    proxies,
		clientCert: workloads.clientCert(t, SourceURI, nil),
		frontProxy: proxies.frontProxy(t, func(c *FrontProxyConfig) { c.PartitionHeaders = []string{testTenantHeader} }),
	}
}

// inOrder tries authenticators as the binary's chain does: the first to admit the caller
// wins, and a refusal wrapping ErrCallerRefused stops the walk. It returns how many
// authenticators were asked.
func inOrder(r *http.Request, authenticators ...k8sapiproxy.CallerAuthenticator) (*k8sapiproxy.CallerIdentity, int, error) {
	var last error
	for i, authn := range authenticators {
		caller, err := authn.AuthenticateRequest(r.Clone(r.Context()))
		if err == nil {
			return caller, i + 1, nil
		}
		last = err
		if errors.Is(err, k8sapiproxy.ErrCallerRefused) {
			return nil, i + 1, err
		}
	}
	return nil, len(authenticators), last
}

// admitAll stands for a later authenticator that would accept the request.
type admitAll struct{}

func (admitAll) AuthenticateRequest(*http.Request) (*k8sapiproxy.CallerIdentity, error) {
	return &k8sapiproxy.CallerIdentity{Username: "admitted-by-the-next-authenticator"}, nil
}

func TestEachAuthenticatorTrustsOnlyItsOwnCA(t *testing.T) {
	p := newPair(t)
	workload := connection(p.workloads.ca.workload(t, testWorkloadURI))
	proxy := connection(p.proxies.ca.workload(t, testProxyURI))
	// A certificate of the workload CA that carries the front proxy's name.
	impostor := connection(p.workloads.ca.workload(t, testProxyURI))
	headers := []string{testUserHeader, testUser, testTenantHeader, testTenant}

	if caller, err := p.clientCert.AuthenticateRequest(request(workload)); err != nil || caller.Username != testWorkloadURI {
		t.Errorf("client certificate authenticator, workload certificate: caller %+v, err %v; want %q admitted", caller, err, testWorkloadURI)
	}
	if caller, err := p.frontProxy.AuthenticateRequest(request(proxy, headers...)); err != nil || caller.Username != testUser || caller.Partition != testTenant {
		t.Errorf("front proxy authenticator, proxy certificate: caller %+v, err %v; want %q in %q admitted", caller, err, testUser, testTenant)
	}

	_, err := p.clientCert.AuthenticateRequest(request(proxy, headers...))
	wantFallThrough(t, err)
	if !errors.Is(err, errNotVerified) {
		t.Errorf("client certificate authenticator, proxy certificate: err = %v, want %v", err, errNotVerified)
	}
	for name, state := range map[string]*tls.ConnectionState{"workload": workload, "impostor": impostor} {
		_, err := p.frontProxy.AuthenticateRequest(request(state, headers...))
		wantFallThrough(t, err)
		if !errors.Is(err, errNotVerified) {
			t.Errorf("front proxy authenticator, %s certificate: err = %v, want %v", name, err, errNotVerified)
		}
	}
}

func TestChainFallsThroughOnAnotherCAAndStopsAtAFrontProxyWithoutACaller(t *testing.T) {
	p := newPair(t)
	workload := connection(p.workloads.ca.workload(t, testWorkloadURI))
	proxy := connection(p.proxies.ca.workload(t, testProxyURI))
	tests := []struct {
		name     string
		request  *http.Request
		chain    []k8sapiproxy.CallerAuthenticator
		username string
		asked    int
		final    bool
	}{
		{
			name:     "workload certificate passes the front proxy authenticator",
			request:  request(workload),
			chain:    []k8sapiproxy.CallerAuthenticator{p.frontProxy, p.clientCert},
			username: testWorkloadURI,
			asked:    2,
		},
		{
			name:     "front proxy certificate passes the client certificate authenticator",
			request:  request(proxy, testUserHeader, testUser, testTenantHeader, testTenant),
			chain:    []k8sapiproxy.CallerAuthenticator{p.clientCert, p.frontProxy},
			username: testUser,
			asked:    2,
		},
		{
			name:     "no certificate reaches the next authenticator",
			request:  request(nil, testUserHeader, testUser, testTenantHeader, testTenant),
			chain:    []k8sapiproxy.CallerAuthenticator{p.clientCert, p.frontProxy, admitAll{}},
			username: "admitted-by-the-next-authenticator",
			asked:    3,
		},
		{
			name:    "front proxy without a username is final",
			request: request(proxy, testTenantHeader, testTenant),
			chain:   []k8sapiproxy.CallerAuthenticator{p.frontProxy, admitAll{}},
			asked:   1,
			final:   true,
		},
		{
			name:    "front proxy with two usernames is final",
			request: request(proxy, testUserHeader, testUser, testUserHeader, "bob", testTenantHeader, testTenant),
			chain:   []k8sapiproxy.CallerAuthenticator{p.clientCert, p.frontProxy, admitAll{}},
			asked:   2,
			final:   true,
		},
		{
			name:    "front proxy without a partition is final",
			request: request(proxy, testUserHeader, testUser),
			chain:   []k8sapiproxy.CallerAuthenticator{p.frontProxy, admitAll{}},
			asked:   1,
			final:   true,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			caller, asked, err := inOrder(tc.request, tc.chain...)
			if asked != tc.asked {
				t.Errorf("%d authenticators asked, want %d", asked, tc.asked)
			}
			if tc.final {
				wantFinal(t, err)
				if caller != nil {
					t.Errorf("caller = %+v, want nil", *caller)
				}
				return
			}
			if err != nil {
				t.Fatalf("chain: %v", err)
			}
			if caller.Username != tc.username {
				t.Errorf("Username = %q, want %q", caller.Username, tc.username)
			}
		})
	}
}

// A request on a connection that outlived its CA is refused: the retained connection
// state is what net/http keeps handing the handler for as long as the connection lives.
func TestRotatedCAIsEnforcedOnAnExistingConnection(t *testing.T) {
	p := newPair(t)
	newWorkloadCA, newProxyCA := newAuthority(t, "next-client-ca", nil), newAuthority(t, "next-front-proxy-ca", nil)
	headers := []string{testUserHeader, testUser, testTenantHeader, testTenant}
	oldWorkload := connection(p.workloads.ca.workload(t, testWorkloadURI))
	oldProxy := connection(p.proxies.ca.workload(t, testProxyURI))
	newWorkload := connection(newWorkloadCA.workload(t, testWorkloadURI))
	newProxy := connection(newProxyCA.workload(t, testProxyURI))

	admitted := func(when string, workload, proxy *tls.ConnectionState) {
		t.Helper()
		if _, err := p.clientCert.AuthenticateRequest(request(workload)); err != nil {
			t.Errorf("%s: client certificate authenticator: %v", when, err)
		}
		if _, err := p.frontProxy.AuthenticateRequest(request(proxy, headers...)); err != nil {
			t.Errorf("%s: front proxy authenticator: %v", when, err)
		}
	}
	refused := func(when string, workload, proxy *tls.ConnectionState) {
		t.Helper()
		var unknown x509.UnknownAuthorityError
		_, err := p.clientCert.AuthenticateRequest(request(workload))
		wantFallThrough(t, err)
		if !errors.As(err, &unknown) {
			t.Errorf("%s: client certificate authenticator: err = %v, want an unknown authority error", when, err)
		}
		_, err = p.frontProxy.AuthenticateRequest(request(proxy, headers...))
		wantFallThrough(t, err)
		if !errors.As(err, &unknown) {
			t.Errorf("%s: front proxy authenticator: err = %v, want an unknown authority error", when, err)
		}
	}

	admitted("before the rotation", oldWorkload, oldProxy)
	refused("before the rotation, next CA", newWorkload, newProxy)

	// Both CAs during the overlap, then the old one dropped.
	writeFile(t, p.workloads.file, append(append([]byte{}, p.workloads.ca.pem...), newWorkloadCA.pem...))
	writeFile(t, p.proxies.file, append(append([]byte{}, p.proxies.ca.pem...), newProxyCA.pem...))
	admitted("within the check interval", oldWorkload, oldProxy)
	refused("within the check interval, next CA", newWorkload, newProxy)
	p.workloads.clock.passReloadInterval()
	admitted("during the overlap, old CA", oldWorkload, oldProxy)
	admitted("during the overlap, next CA", newWorkload, newProxy)

	writeFile(t, p.workloads.file, newWorkloadCA.pem)
	writeFile(t, p.proxies.file, newProxyCA.pem)
	p.workloads.clock.passReloadInterval()
	refused("after the old CA was rotated out", oldWorkload, oldProxy)
	admitted("after the old CA was rotated out, next CA", newWorkload, newProxy)
	if n := p.workloads.log.count(); n != 0 {
		t.Errorf("%d reload errors reported, want none", n)
	}
}

// A bundle that cannot be loaded after a change neither locks callers out nor makes the
// authenticator report itself unavailable: the last good bundle keeps verifying.
func TestUnreadableCABundleKeepsTheLastGoodOne(t *testing.T) {
	p := newPair(t)
	otherCA := newAuthority(t, "other-ca", nil)
	headers := []string{testUserHeader, testUser, testTenantHeader, testTenant}
	workload := connection(p.workloads.ca.workload(t, testWorkloadURI))
	proxy := connection(p.proxies.ca.workload(t, testProxyURI))

	writeFile(t, p.workloads.file, []byte("not PEM"))
	writeFile(t, p.proxies.file, nil)
	p.workloads.clock.passReloadInterval()
	if _, err := p.clientCert.AuthenticateRequest(request(workload)); err != nil {
		t.Errorf("client certificate authenticator with an unreadable bundle: %v", err)
	}
	if _, err := p.frontProxy.AuthenticateRequest(request(proxy, headers...)); err != nil {
		t.Errorf("front proxy authenticator with an unreadable bundle: %v", err)
	}
	// One report per authenticator's bundle.
	if n := p.workloads.log.count(); n != 2 {
		t.Errorf("%d reload errors reported, want 2", n)
	}
	_, err := p.clientCert.AuthenticateRequest(request(connection(otherCA.workload(t, testWorkloadURI))))
	wantFallThrough(t, err)
}

// reply is what the test server answered one request.
type reply struct {
	status int
	body   string
	// http2 says the request went over HTTP/2, and reused that it went on a connection
	// an earlier request opened.
	http2  bool
	reused bool
}

// serve starts an HTTP/2 TLS server that requests a client certificate, verifies one that
// is given against handshakeCAs, and answers each request with what authn decided: 200 and
// "username|partition", or 401.
func serve(t *testing.T, authn k8sapiproxy.CallerAuthenticator, handshakeCAs ...*authority) *httptest.Server {
	t.Helper()
	pool := x509.NewCertPool()
	for _, ca := range handshakeCAs {
		pool.AddCert(ca.cert)
	}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		caller, err := authn.AuthenticateRequest(r)
		if err != nil {
			w.WriteHeader(http.StatusUnauthorized)
			return
		}
		_, _ = io.WriteString(w, caller.Username+partitionSeparator+caller.Partition)
	}))
	srv.EnableHTTP2 = true
	srv.TLS = &tls.Config{ClientAuth: tls.VerifyClientCertIfGiven, ClientCAs: pool, MinVersion: tls.VersionTLS12}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv
}

// clientWith returns a client of srv, with a connection pool of its own, that presents
// cert, or no certificate when nil.
func clientWith(t *testing.T, srv *httptest.Server, cert *issued) *http.Client {
	t.Helper()
	roots := x509.NewCertPool()
	roots.AddCert(srv.Certificate())
	cfg := &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}
	if cert != nil {
		cfg.Certificates = []tls.Certificate{cert.tlsCertificate()}
	}
	transport := &http.Transport{TLSClientConfig: cfg, ForceAttemptHTTP2: true}
	t.Cleanup(transport.CloseIdleConnections)
	return &http.Client{Transport: transport}
}

func send(t *testing.T, client *http.Client, url string, headers ...string) reply {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Add(headers[i], headers[i+1])
	}
	var got reply
	var reused atomic.Bool
	trace := &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) { reused.Store(info.Reused) }}
	resp, err := client.Do(req.WithContext(httptrace.WithClientTrace(req.Context(), trace)))
	if err != nil {
		t.Fatalf("GET %s: %v", url, err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("reading the response: %v", err)
	}
	got.status, got.body, got.http2, got.reused = resp.StatusCode, string(body), resp.ProtoMajor == httpMajorVersion2, reused.Load()
	return got
}

func TestAuthenticatorsOverTLSAndHTTP2(t *testing.T) {
	p := newPair(t)
	authn := authenticatorFunc(func(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
		caller, _, err := inOrder(r, p.clientCert, p.frontProxy)
		return caller, err
	})
	srv := serve(t, authn, p.workloads.ca, p.proxies.ca)
	headers := []string{testUserHeader, testUser, testTenantHeader, testTenant}

	tests := []struct {
		name    string
		cert    *issued
		headers []string
		status  int
		body    string
	}{
		{name: "workload certificate", cert: p.workloads.ca.workload(t, testWorkloadURI), status: http.StatusOK, body: testWorkloadURI + partitionSeparator},
		{name: "workload certificate sending identity headers", cert: p.workloads.ca.workload(t, testWorkloadURI), headers: headers, status: http.StatusOK, body: testWorkloadURI + partitionSeparator},
		{name: "front proxy certificate", cert: p.proxies.ca.workload(t, testProxyURI), headers: headers, status: http.StatusOK, body: testUser + partitionSeparator + testTenant},
		{name: "front proxy certificate without headers", cert: p.proxies.ca.workload(t, testProxyURI), status: http.StatusUnauthorized},
		{name: "front proxy name under the workload CA", cert: p.workloads.ca.workload(t, testProxyURI), headers: headers, status: http.StatusOK, body: testProxyURI + partitionSeparator},
		{name: "no certificate", headers: headers, status: http.StatusUnauthorized},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := send(t, clientWith(t, srv, tc.cert), srv.URL, tc.headers...)
			if got.status != tc.status || got.body != tc.body {
				t.Errorf("status %d body %q, want status %d body %q", got.status, got.body, tc.status, tc.body)
			}
			if !got.http2 {
				t.Error("the request did not go over HTTP/2")
			}
		})
	}
}

// authenticatorFunc adapts a function to k8sapiproxy.CallerAuthenticator.
type authenticatorFunc func(*http.Request) (*k8sapiproxy.CallerIdentity, error)

func (f authenticatorFunc) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	return f(r)
}

// The handshake verified the certificate once, when the connection opened. What changes
// afterwards, the CA leaving the bundle or the certificate expiring, is enforced on the
// next request of that same HTTP/2 connection.
func TestOpenHTTP2ConnectionIsRefusedOnceItsCertificateIsNoLongerGood(t *testing.T) {
	tests := []struct {
		name string
		// life is how long the certificate stays valid from the start of the test; zero
		// for the default.
		life time.Duration
		// invalidate makes the certificate no longer good.
		invalidate func(t *testing.T, f *fixture)
	}{
		{
			name: "CA rotated out of the bundle",
			invalidate: func(t *testing.T, f *fixture) {
				writeFile(t, f.file, newAuthority(t, "next-ca", nil).pem)
				f.clock.passReloadInterval()
			},
		},
		{
			name:       "certificate expired",
			life:       testShortLife,
			invalidate: func(_ *testing.T, f *fixture) { f.clock.advance(2 * testShortLife) },
		},
	}
	peers := []struct {
		name string
		// pick returns the peer's CA fixture and the authenticator that admits it.
		pick    func(*pair) (*fixture, k8sapiproxy.CallerAuthenticator)
		uri     string
		headers []string
		body    string
	}{
		{
			name: "client certificate",
			pick: func(p *pair) (*fixture, k8sapiproxy.CallerAuthenticator) { return p.workloads, p.clientCert },
			uri:  testWorkloadURI,
			body: testWorkloadURI + partitionSeparator,
		},
		{
			name:    "front proxy",
			pick:    func(p *pair) (*fixture, k8sapiproxy.CallerAuthenticator) { return p.proxies, p.frontProxy },
			uri:     testProxyURI,
			headers: []string{testUserHeader, testUser, testTenantHeader, testTenant},
			body:    testUser + partitionSeparator + testTenant,
		},
	}
	for _, tc := range tests {
		for _, peer := range peers {
			t.Run(tc.name+"/"+peer.name, func(t *testing.T) {
				f, authn := peer.pick(newPair(t))
				spec := certSpec{uris: []string{peer.uri}, usages: clientUsage}
				if tc.life > 0 {
					spec.notAfter = f.clock.now().Add(tc.life)
				}
				// The handshake's pool is fixed, as on a connection that is already open.
				srv := serve(t, authn, f.ca)
				client := clientWith(t, srv, f.ca.issue(t, spec))

				first := send(t, client, srv.URL, peer.headers...)
				if first.status != http.StatusOK || first.body != peer.body || !first.http2 {
					t.Fatalf("first request: %+v, want 200 %q over HTTP/2", first, peer.body)
				}
				again := send(t, client, srv.URL, peer.headers...)
				if again.status != http.StatusOK || !again.reused {
					t.Fatalf("second request: %+v, want 200 on the same connection", again)
				}

				tc.invalidate(t, f)
				after := send(t, client, srv.URL, peer.headers...)
				if after.status != http.StatusUnauthorized {
					t.Errorf("request after the change: status %d, want 401", after.status)
				}
				if !after.reused || !after.http2 {
					t.Errorf("request after the change: %+v, want it on the same HTTP/2 connection", after)
				}
			})
		}
	}
}
