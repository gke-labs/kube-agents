package upstream

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"golang.org/x/oauth2"
	"k8s.io/client-go/rest"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testClusterID = "gke_proj_us-central1_c1"
	testToken     = "google-token"
	testSAToken   = "in-cluster-token"
)

// fakeAPIServer is a TLS API server that, like kube-apiserver, offers HTTP/2, and records each
// request's Authorization header, URL and protocol.
type fakeAPIServer struct {
	*httptest.Server
	mu     sync.Mutex
	seen   []string
	protos []string
}

func newFakeAPIServer(t *testing.T, handler http.HandlerFunc) *fakeAPIServer {
	t.Helper()
	s := &fakeAPIServer{}
	s.Server = httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		s.seen = append(s.seen, r.Header.Get("Authorization")+" "+r.URL.RequestURI())
		s.protos = append(s.protos, r.Proto)
		s.mu.Unlock()
		if handler != nil {
			handler(w, r)
			return
		}
		_, _ = io.WriteString(w, "ok")
	}))
	s.EnableHTTP2 = true
	s.StartTLS()
	t.Cleanup(s.Close)
	return s
}

func (s *fakeAPIServer) caPEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: s.Certificate().Raw})
}

// selfSignedCAPEM returns a CA certificate that signed none of the test servers' certificates.
// Every httptest TLS server shares one certificate, so another server's would not do.
func selfSignedCAPEM(t *testing.T) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "other-ca"},
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
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func (s *fakeAPIServer) requests() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.seen...)
}

// fakeContainerAPI describes every cluster as desc returns it and counts the calls.
type fakeContainerAPI struct {
	*httptest.Server
	calls atomic.Int32
}

func newFakeContainerAPI(t *testing.T, status int, desc func() clusterDescription) *fakeContainerAPI {
	t.Helper()
	f := &fakeContainerAPI{}
	f.Server = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		f.calls.Add(1)
		if r.Header.Get("Authorization") != "Bearer "+testToken {
			http.Error(w, "unauthenticated", http.StatusUnauthorized)
			return
		}
		if r.URL.Path != "/v1/projects/proj/locations/us-central1/clusters/c1" {
			http.NotFound(w, r)
			return
		}
		if status != http.StatusOK {
			http.Error(w, "failed", status)
			return
		}
		_ = json.NewEncoder(w).Encode(desc())
	}))
	t.Cleanup(f.Close)
	return f
}

func ipEndpoint(s *fakeAPIServer) func() clusterDescription {
	return func() clusterDescription {
		var d clusterDescription
		d.Endpoint = strings.TrimPrefix(s.URL, "https://")
		d.MasterAuth.ClusterCACertificate = base64.StdEncoding.EncodeToString(s.caPEM())
		return d
	}
}

func newTestTransport(t *testing.T, inCluster, gke *fakeAPIServer, api *fakeContainerAPI, mutate func(*Config)) *Transport {
	t.Helper()
	cfg := Config{
		InCluster: &rest.Config{
			Host:            inCluster.URL,
			BearerToken:     testSAToken,
			TLSClientConfig: rest.TLSClientConfig{CAData: inCluster.caPEM()},
		},
		TokenSource:     oauth2.StaticTokenSource(&oauth2.Token{AccessToken: testToken}),
		ContainerAPIURL: api.URL,
	}
	if mutate != nil {
		mutate(&cfg)
	}
	tr, err := New(context.Background(), cfg)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if gke != nil {
		tr.dnsCAData = gke.caPEM()
	}
	tr.discovery.Transport.(*oauth2.Transport).Base = api.Client().Transport
	return tr
}

// fakeClock is a settable Transport.now.
type fakeClock struct {
	mu sync.Mutex
	t  time.Time
}

func useFakeClock(tr *Transport) *fakeClock {
	c := &fakeClock{t: time.Now()}
	tr.now = func() time.Time {
		c.mu.Lock()
		defer c.mu.Unlock()
		return c.t
	}
	return c
}

func (c *fakeClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

func roundTrip(t *testing.T, tr *Transport, clusterID, path string) (*http.Response, error) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, path, nil)
	req.RequestURI = ""
	req.URL.Scheme, req.URL.Host, req.Host = "", "", ""
	resp, err := tr.RoundTripCluster(context.Background(), clusterID, req)
	if err == nil {
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
	}
	return resp, err
}

func TestRoundTripGKEClusterByIPEndpoint(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, ipEndpoint(gke))
	tr := newTestTransport(t, inCluster, nil, api, nil)

	for range 2 {
		resp, err := roundTrip(t, tr, testClusterID, "/api/v1/namespaces/default/pods?limit=5")
		if err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("RoundTripCluster = %v, %v", resp, err)
		}
	}
	want := "Bearer " + testToken + " /api/v1/namespaces/default/pods?limit=5"
	if got := gke.requests(); len(got) != 2 || got[0] != want || got[1] != want {
		t.Errorf("GKE API server saw %q, want %q twice", got, want)
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("Container API calls = %d, want 1: the endpoint is cached", n)
	}
	if got := inCluster.requests(); len(got) != 0 {
		t.Errorf("in-cluster API server saw %q, want nothing", got)
	}
}

func TestRoundTripGKEClusterByDNSEndpoint(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, nil)
	yes := true
	api := newFakeContainerAPI(t, http.StatusOK, func() clusterDescription {
		var d clusterDescription
		// The IP endpoint is unreachable and has no CA; rule 1 must not use it.
		d.Endpoint = "192.0.2.1"
		d.ControlPlaneEndpointsConfig.DNSEndpointConfig.Endpoint = strings.TrimPrefix(gke.URL, "https://")
		d.ControlPlaneEndpointsConfig.DNSEndpointConfig.AllowExternalTraffic = &yes
		return d
	})
	tr := newTestTransport(t, inCluster, gke, api, nil)

	if resp, err := roundTrip(t, tr, testClusterID, "/version"); err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("RoundTripCluster = %v, %v", resp, err)
	}
	if got := gke.requests(); len(got) != 1 || got[0] != "Bearer "+testToken+" /version" {
		t.Errorf("GKE API server saw %q", got)
	}
}

func TestRoundTripInCluster(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, nil)
	tr := newTestTransport(t, inCluster, nil, api, func(c *Config) { c.HostClusterID = testClusterID })

	for _, id := range []string{k8sapiproxy.InClusterID, testClusterID} {
		if resp, err := roundTrip(t, tr, id, "/api"); err != nil || resp.StatusCode != http.StatusOK {
			t.Fatalf("RoundTripCluster(%q) = %v, %v", id, resp, err)
		}
	}
	want := "Bearer " + testSAToken + " /api"
	if got := inCluster.requests(); len(got) != 2 || got[0] != want || got[1] != want {
		t.Errorf("in-cluster API server saw %q, want %q twice", got, want)
	}
	if n := api.calls.Load(); n != 0 {
		t.Errorf("Container API calls = %d, want 0", n)
	}
}

func TestRoundTripRefusesKindCluster(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, nil)
	tr := newTestTransport(t, inCluster, nil, api, nil)

	if _, err := roundTrip(t, tr, "kind-dev", "/api"); err == nil {
		t.Fatal("RoundTripCluster(kind-dev) succeeded")
	}
	if got := inCluster.requests(); len(got) != 0 {
		t.Errorf("in-cluster API server saw %q: a kind cluster must not fall back to it", got)
	}
}

func TestRoundTripCachesDiscoveryFailure(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusForbidden, nil)
	tr := newTestTransport(t, inCluster, nil, api, nil)

	for range 3 {
		if _, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
			t.Fatal("RoundTripCluster succeeded with a failing Container API")
		}
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("Container API calls = %d, want 1: the failure is cached", n)
	}
}

func TestRoundTripSharesConcurrentDiscovery(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, nil)
	release := make(chan struct{})
	describe := ipEndpoint(gke)
	api := newFakeContainerAPI(t, http.StatusOK, func() clusterDescription {
		<-release
		return describe()
	})
	tr := newTestTransport(t, inCluster, nil, api, nil)

	const callers = 8
	var wg sync.WaitGroup
	errs := make(chan error, callers)
	for range callers {
		wg.Go(func() {
			if _, err := roundTrip(t, tr, testClusterID, "/api"); err != nil {
				errs <- err
			}
		})
	}
	time.Sleep(100 * time.Millisecond)
	close(release)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Errorf("RoundTripCluster: %v", err)
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("Container API calls = %d, want 1", n)
	}
}

func TestRoundTripRediscoversAfterTransportError(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, ipEndpoint(gke))
	tr := newTestTransport(t, inCluster, nil, api, nil)
	clock := useFakeClock(tr)

	if _, err := roundTrip(t, tr, testClusterID, "/api"); err != nil {
		t.Fatalf("RoundTripCluster: %v", err)
	}
	gke.Close()
	for range 3 {
		if _, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
			t.Fatal("RoundTripCluster succeeded against a closed API server")
		}
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("Container API calls = %d, want 1: a failing endpoint is not described again within the failure TTL", n)
	}
	clock.advance(failureTTL)
	if _, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
		t.Fatal("RoundTripCluster succeeded against a closed API server")
	}
	if n := api.calls.Load(); n != 2 {
		t.Errorf("Container API calls = %d, want 2: a transport error shortens the cached endpoint to the failure TTL", n)
	}
}

func TestRoundTripCacheExpiry(t *testing.T) {
	for _, tc := range []struct {
		name   string
		status int
		ttl    time.Duration
	}{
		{"endpoint", http.StatusOK, defaultEndpointTTL},
		{"failure", http.StatusForbidden, failureTTL},
	} {
		t.Run(tc.name, func(t *testing.T) {
			inCluster := newFakeAPIServer(t, nil)
			gke := newFakeAPIServer(t, nil)
			api := newFakeContainerAPI(t, tc.status, ipEndpoint(gke))
			tr := newTestTransport(t, inCluster, nil, api, nil)
			clock := useFakeClock(tr)

			_, _ = roundTrip(t, tr, testClusterID, "/api")
			clock.advance(tc.ttl - time.Second)
			_, _ = roundTrip(t, tr, testClusterID, "/api")
			if n := api.calls.Load(); n != 1 {
				t.Errorf("Container API calls before the TTL = %d, want 1", n)
			}
			clock.advance(time.Second)
			_, _ = roundTrip(t, tr, testClusterID, "/api")
			if n := api.calls.Load(); n != 2 {
				t.Errorf("Container API calls after the TTL = %d, want 2", n)
			}
		})
	}
}

func TestRoundTripCachesSlowDiscoveryFailure(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	var clock *fakeClock
	api := newFakeContainerAPI(t, http.StatusOK, func() clusterDescription {
		// The describe outlasts failureTTL, then returns a cluster with no endpoint.
		clock.advance(2 * failureTTL)
		return clusterDescription{}
	})
	tr := newTestTransport(t, inCluster, nil, api, nil)
	clock = useFakeClock(tr)

	for range 2 {
		if _, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
			t.Fatal("RoundTripCluster succeeded for a cluster with no endpoint")
		}
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("Container API calls = %d, want 1: the failure TTL runs from when the describe ended", n)
	}
}

func TestRoundTripPrunesExpiredEntries(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, nil)
	tr := newTestTransport(t, inCluster, nil, api, nil)
	clock := useFakeClock(tr)

	for _, id := range []string{"gke_proj_us-central1_a", "gke_proj_us-central1_b"} {
		_, _ = roundTrip(t, tr, id, "/api")
	}
	clock.advance(failureTTL)
	_, _ = roundTrip(t, tr, "gke_proj_us-central1_c", "/api")
	tr.mu.Lock()
	defer tr.mu.Unlock()
	if len(tr.clusters) != 1 {
		t.Errorf("cached clusters = %d, want 1: expired entries are dropped", len(tr.clusters))
	}
}

func TestRoundTripCancelledCallerDoesNotFailOthers(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, nil)
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	describe := ipEndpoint(gke)
	api := newFakeContainerAPI(t, http.StatusOK, func() clusterDescription {
		once.Do(func() { close(started) })
		// Giving up with the test lets the server's Close return if an assertion below fails
		// before release is closed.
		select {
		case <-release:
		case <-t.Context().Done():
		}
		return describe()
	})
	tr := newTestTransport(t, inCluster, nil, api, nil)

	ctx, cancel := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() {
		req := httptest.NewRequest(http.MethodGet, "/api", nil)
		req.RequestURI = ""
		_, err := tr.RoundTripCluster(ctx, testClusterID, req)
		first <- err
	}()
	select {
	case <-started:
	case err := <-first:
		t.Fatalf("RoundTripCluster returned before the describe started: %v", err)
	}
	cancel()
	if err := <-first; err == nil {
		t.Fatal("the cancelled caller succeeded")
	}
	close(release)
	if resp, err := roundTrip(t, tr, testClusterID, "/api"); err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("RoundTripCluster after another caller gave up = %v, %v", resp, err)
	}
	if n := api.calls.Load(); n != 1 {
		t.Errorf("Container API calls = %d, want 1: the shared resolution completed", n)
	}
}

func TestRoundTripPassesNotModified(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotModified)
	})
	api := newFakeContainerAPI(t, http.StatusOK, ipEndpoint(gke))
	tr := newTestTransport(t, inCluster, nil, api, nil)

	if resp, err := roundTrip(t, tr, testClusterID, "/api"); err != nil || resp.StatusCode != http.StatusNotModified {
		t.Fatalf("RoundTripCluster = %v, %v; want a 304", resp, err)
	}
}

func TestRoundTripDropsCallerAuthorization(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, nil)
	tr := newTestTransport(t, inCluster, nil, api, nil)

	req := httptest.NewRequest(http.MethodGet, "/api", nil)
	req.RequestURI = ""
	req.Header.Set("Authorization", "Bearer caller-token")
	resp, err := tr.RoundTripCluster(context.Background(), k8sapiproxy.InClusterID, req)
	if err != nil {
		t.Fatalf("RoundTripCluster: %v", err)
	}
	_ = resp.Body.Close()
	if got := inCluster.requests(); len(got) != 1 || got[0] != "Bearer "+testSAToken+" /api" {
		t.Errorf("in-cluster API server saw %q, want the service account token", got)
	}
}

func TestRoundTripEmptyClusterIDIsNotInCluster(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, nil)
	tr := newTestTransport(t, inCluster, nil, api, nil)

	if _, err := roundTrip(t, tr, "", "/api"); err == nil {
		t.Fatal("RoundTripCluster accepted an empty cluster ID")
	}
	if got := inCluster.requests(); len(got) != 0 {
		t.Errorf("in-cluster API server saw %q", got)
	}
}

func TestNewRejectsConfig(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	base := Config{
		InCluster:   &rest.Config{Host: inCluster.URL},
		TokenSource: oauth2.StaticTokenSource(&oauth2.Token{AccessToken: testToken}),
	}
	for name, mutate := range map[string]func(*Config){
		"invalid host cluster ID":    func(c *Config) { c.HostClusterID = "kind-dev" },
		"plain HTTP container API":   func(c *Config) { c.ContainerAPIURL = "http://container.example" },
		"plain HTTP in-cluster host": func(c *Config) { c.InCluster = &rest.Config{Host: "http://10.0.0.1"} },
	} {
		cfg := base
		mutate(&cfg)
		if _, err := New(context.Background(), cfg); err == nil {
			t.Errorf("%s: New succeeded", name)
		}
	}
}

func TestTargetURL(t *testing.T) {
	for _, tc := range []struct {
		in, want string
	}{
		{"1.2.3.4", "https://1.2.3.4"},
		{"2001:db8::1", "https://[2001:db8::1]"},
		{"gke-x.goog:443", "https://gke-x.goog:443"},
		{"https://gke-x.goog", "https://gke-x.goog"},
		{"http://gke-x.goog", ""},
		{"https://gke-x.goog/path", ""},
		{"https://user@gke-x.goog", ""},
	} {
		got, err := targetURL(tc.in)
		if tc.want == "" {
			if err == nil {
				t.Errorf("targetURL(%q) = %v, want an error", tc.in, got)
			}
			continue
		}
		if err != nil || got.String() != tc.want {
			t.Errorf("targetURL(%q) = %v, %v; want %s", tc.in, got, err, tc.want)
		}
	}
}

func TestRoundTripRefusesRedirect(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "https://elsewhere.example/api", http.StatusFound)
	})
	api := newFakeContainerAPI(t, http.StatusOK, ipEndpoint(gke))
	tr := newTestTransport(t, inCluster, nil, api, nil)

	if resp, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
		t.Fatalf("RoundTripCluster relayed a %d", resp.StatusCode)
	}
}

func TestRoundTripRefusesPlainHTTPEndpoint(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	yes := true
	api := newFakeContainerAPI(t, http.StatusOK, func() clusterDescription {
		var d clusterDescription
		d.ControlPlaneEndpointsConfig.DNSEndpointConfig.Endpoint = "http://gke-x.goog"
		d.ControlPlaneEndpointsConfig.DNSEndpointConfig.AllowExternalTraffic = &yes
		return d
	})
	tr := newTestTransport(t, inCluster, nil, api, nil)

	if _, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
		t.Fatal("RoundTripCluster used an http:// endpoint")
	}
}

func TestRoundTripRejectsUntrustedCertificate(t *testing.T) {
	inCluster := newFakeAPIServer(t, nil)
	gke := newFakeAPIServer(t, nil)
	api := newFakeContainerAPI(t, http.StatusOK, func() clusterDescription {
		d := ipEndpoint(gke)()
		d.MasterAuth.ClusterCACertificate = base64.StdEncoding.EncodeToString(selfSignedCAPEM(t))
		return d
	})
	tr := newTestTransport(t, inCluster, nil, api, nil)

	if _, err := roundTrip(t, tr, testClusterID, "/api"); err == nil {
		t.Fatal("RoundTripCluster trusted a certificate the cluster CA did not sign")
	}
	if got := gke.requests(); len(got) != 0 {
		t.Errorf("GKE API server saw %q", got)
	}
}

// An upgrade must reach the API server over HTTP/1.1 even with an HTTP/2 connection to it
// pooled, and come back with a body the reverse proxy can splice.
func TestRoundTripUpgradeOverHTTP1(t *testing.T) {
	for _, clusterID := range []string{k8sapiproxy.InClusterID, testClusterID} {
		t.Run(clusterID, func(t *testing.T) {
			upgradeServer := func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Upgrade") == "" {
					_, _ = io.WriteString(w, "ok")
					return
				}
				conn, rw, err := http.NewResponseController(w).Hijack()
				if err != nil {
					http.Error(w, err.Error(), http.StatusHTTPVersionNotSupported)
					return
				}
				defer func() { _ = conn.Close() }()
				_, _ = rw.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: SPDY/3.1\r\n\r\n")
				_ = rw.Flush()
				line, _ := rw.ReadString('\n')
				_, _ = rw.WriteString("echo " + line)
				_ = rw.Flush()
			}
			inCluster := newFakeAPIServer(t, upgradeServer)
			gke := newFakeAPIServer(t, upgradeServer)
			api := newFakeContainerAPI(t, http.StatusOK, ipEndpoint(gke))
			tr := newTestTransport(t, inCluster, nil, api, nil)
			server := gke
			if clusterID == k8sapiproxy.InClusterID {
				server = inCluster
			}

			if _, err := roundTrip(t, tr, clusterID, "/api"); err != nil {
				t.Fatalf("RoundTripCluster: %v", err)
			}
			req := httptest.NewRequest(http.MethodPost, "/api/v1/namespaces/default/pods/p/exec", nil)
			req.RequestURI = ""
			req.URL.Scheme, req.URL.Host, req.Host = "", "", ""
			req.Header.Set("Connection", "Upgrade")
			req.Header.Set("Upgrade", "SPDY/3.1")
			resp, err := tr.RoundTripCluster(context.Background(), clusterID, req)
			if err != nil {
				t.Fatalf("RoundTripCluster(upgrade): %v", err)
			}
			defer func() { _ = resp.Body.Close() }()
			server.mu.Lock()
			protos := append([]string(nil), server.protos...)
			server.mu.Unlock()
			if len(protos) != 2 || protos[0] != "HTTP/2.0" || protos[1] != "HTTP/1.1" {
				t.Errorf("protocols = %q, want HTTP/2.0 for the request and HTTP/1.1 for the upgrade", protos)
			}
			if resp.StatusCode != http.StatusSwitchingProtocols {
				t.Fatalf("status = %d, want 101", resp.StatusCode)
			}
			stream, ok := resp.Body.(io.ReadWriteCloser)
			if !ok {
				t.Fatalf("101 body is %T, not an io.ReadWriteCloser", resp.Body)
			}
			if _, err := io.WriteString(stream, "hello\n"); err != nil {
				t.Fatalf("write: %v", err)
			}
			got := make([]byte, len("echo hello\n"))
			if _, err := io.ReadFull(stream, got); err != nil || string(got) != "echo hello\n" {
				t.Errorf("read %q, %v", got, err)
			}
		})
	}
}
