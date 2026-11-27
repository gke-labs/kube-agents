package proxyserver

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	testJWTAudience     = "kube-proxy"
	testJWTKeyID        = "key-1"
	testJWTKeySetPath   = "/keys"
	testJWTMetadataPath = "/.well-known/openid-configuration"
	testJWTTTL          = 5 * time.Minute
	testDomainKey       = "example.io"
	testTenantPath      = `example\.io.tenant`
	testUIDPath         = `example\.io.uid`
	testTeamA           = "team-a"
	testTeamB           = "team-b"
	testPodsPath        = "/api/v1/namespaces/default/pods"
	testReviewCaller    = "system:serviceaccount:ns:sa"
	testCacheHit        = "k8s-api-proxy; hit"
	testContentJSON     = "application/json"
	testReviewRefused   = `{"apiVersion":"authentication.k8s.io/v1","kind":"TokenReview","status":{"authenticated":false}}`
	testReviewAudience  = "review-audience"
	testReviewAccepted  = `{"apiVersion":"authentication.k8s.io/v1","kind":"TokenReview","status":{"authenticated":true,"user":{"username":"` + testReviewCaller + `","extra":{"authentication.kubernetes.io/pod-name":["caller-0"],"authentication.kubernetes.io/pod-uid":["caller-uid"]}},"audiences":["` + testReviewAudience + `"]}}`
	// testCallerPodPath and testCallerPod are the pod testReviewAccepted names, at the address
	// httptest.NewRequest gives every request's peer.
	testCallerPodPath = "/api/v1/namespaces/ns/pods/caller-0"
	testCallerPod     = `{"apiVersion":"v1","kind":"Pod","metadata":{"name":"caller-0","namespace":"ns","uid":"caller-uid"},"status":{"phase":"Running","podIP":"192.0.2.1","podIPs":[{"ip":"192.0.2.1"}]}}`
	testForeignIssuer = "https://kubernetes.default.svc"
)

// proxyEnvVars are the KUBE_PROXY_ and CREDENTIAL_PROXY_ variables the binary reads, cleared
// so a developer's environment cannot change what a test parses. The examples test refuses
// a manifest that sets a variable under those prefixes that is not listed here.
var proxyEnvVars = []string{
	listenAddrEnv, metricsPortEnv, tlsCertFileEnv, tlsKeyFileEnv, modeEnv, hostClusterIDEnv,
	redactSecretsEnv, allowUpgradesEnv, kubeAPIServerEnv, kubeCAFileEnv, kubeTokenFileEnv,
	audienceEnv, allowedCallersEnv, cacheMaxBytesEnv, cacheMaxEntryBytesEnv, cacheResourceTTLEnv,
	cacheDiscoveryTTLEnv, scopeFileEnv, holdFileEnv, excludeHostClusterEnv, wifServiceAccountEnv,
	wifTokenFileEnv, wifAudienceEnv, sessionAudienceEnv, sessionCallersEnv, maxInFlightEnv,
	authModeEnv, upstreamURLEnv, denyStreamingEnv, upstreamTokenFileEnv, upstreamCAFileEnv,
	universeDomainEnv, containerAPIURLEnv, scopedPoolFlagEnv, scopedPoolFileEnv,
	jwtIssuerEnv, jwtKeySetURLEnv, jwtAudiencesEnv, jwtUsernameClaimEnv, jwtUIDClaimEnv,
	jwtGroupsClaimEnv, jwtPartitionClaimEnv, jwtRequiredSubPrefixEnv, jwtAllowedPartitionsEnv,
	jwtCAFileEnv, partitionByCallerEnv, maxInFlightPerCallerEnv, maxLongRunningPerCallerEnv,
	mtlsClientCAFileEnv, mtlsIdentitySourceEnv, mtlsAllowedIdentitiesEnv, mtlsRequiredURIPrefixEnv,
	headerClientCAFileEnv, headerNameSourceEnv, headerAllowedNamesEnv, headerUsernameHeadersEnv,
	headerUIDHeadersEnv, headerGroupHeadersEnv, headerPartitionHeadersEnv, headerAllowedPartitionsEnv,
}

func clearProxyEnv(t *testing.T) {
	t.Helper()
	for _, name := range proxyEnvVars {
		t.Setenv(name, "")
	}
}

// parseTestFlags parses args as the binary would, with no environment defaults.
func parseTestFlags(t *testing.T, args ...string) proxyConfig {
	t.Helper()
	var cfg proxyConfig
	if err := parseFlags(flag.NewFlagSet("test", flag.ContinueOnError), args, &cfg); err != nil {
		t.Fatalf("parseFlags(%q): %v", args, err)
	}
	return cfg
}

// testJWTIssuer signs tokens and serves its metadata and key set over TLS. Its URL is the
// issuer, and caFile holds the certificate its server presents.
type testJWTIssuer struct {
	t      *testing.T
	srv    *httptest.Server
	signer jose.Signer
	caFile string
	seq    atomic.Int64
	// served counts the requests its server got, for metadata or keys.
	served atomic.Int32
}

func newTestJWTIssuer(t *testing.T) *testJWTIssuer {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.ES256, Key: key},
		(&jose.SignerOptions{}).WithType("JWT").WithHeader(jose.HeaderKey("kid"), testJWTKeyID))
	if err != nil {
		t.Fatal(err)
	}
	keySet := jose.JSONWebKeySet{Keys: []jose.JSONWebKey{
		{Key: &key.PublicKey, KeyID: testJWTKeyID, Algorithm: string(jose.ES256), Use: "sig"},
	}}
	is := &testJWTIssuer{t: t, signer: signer}
	is.srv = httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		is.served.Add(1)
		w.Header().Set("Content-Type", testContentJSON)
		switch r.URL.Path {
		case testJWTMetadataPath:
			_ = json.NewEncoder(w).Encode(map[string]string{"issuer": is.srv.URL, "jwks_uri": is.srv.URL + testJWTKeySetPath})
		case testJWTKeySetPath:
			_ = json.NewEncoder(w).Encode(keySet)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(is.srv.Close)
	is.caFile = filepath.Join(t.TempDir(), "issuer-ca.crt")
	block := &pem.Block{Type: "CERTIFICATE", Bytes: is.srv.Certificate().Raw}
	if err := os.WriteFile(is.caFile, pem.EncodeToMemory(block), 0o600); err != nil {
		t.Fatal(err)
	}
	return is
}

// sign returns a token for sub carrying domain under the dotted domain key; a nil domain
// leaves the key out. Each token is distinct, so no authenticator's cache answers for it.
func (is *testJWTIssuer) sign(sub, audience string, domain map[string]any) string {
	is.t.Helper()
	return is.signAs(is.srv.URL, sub, audience, domain)
}

// signAs is sign for a token that names issuerName as its issuer.
func (is *testJWTIssuer) signAs(issuerName, sub, audience string, domain map[string]any) string {
	is.t.Helper()
	now := time.Now()
	claims := map[string]any{
		"iss": issuerName,
		"sub": sub,
		"aud": audience,
		"iat": now.Unix(),
		"exp": now.Add(testJWTTTL).Unix(),
		"jti": fmt.Sprintf("token-%d", is.seq.Add(1)),
	}
	if domain != nil {
		claims[testDomainKey] = domain
	}
	raw, err := jwt.Signed(is.signer).Claims(claims).Serialize()
	if err != nil {
		is.t.Fatal(err)
	}
	return raw
}

func (is *testJWTIssuer) tenantToken(sub, tenant string) string {
	return is.sign(sub, testJWTAudience, map[string]any{"tenant": tenant, "uid": "uid-" + sub})
}

// jwtArgs are the flags of a standalone proxy that admits is's tokens.
func (is *testJWTIssuer) jwtArgs(upstreamURL string, more ...string) []string {
	return append([]string{
		"--auth-mode=jwt",
		"--upstream-url=" + upstreamURL,
		"--jwt-issuer=" + is.srv.URL,
		"--jwt-audiences=other-audience, " + testJWTAudience,
		"--jwt-ca-file=" + is.caFile,
		"--cache-resource-ttl=1m",
	}, more...)
}

// countingUpstream is an API server whose every answer differs, so a response shows which
// upstream call produced it. It records the Authorization headers it was sent.
type countingUpstream struct {
	srv   *httptest.Server
	calls atomic.Int32
	mu    sync.Mutex
	authz []string
}

func newCountingUpstream(t *testing.T) *countingUpstream {
	t.Helper()
	u := &countingUpstream{}
	u.srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := u.calls.Add(1)
		u.mu.Lock()
		u.authz = append(u.authz, r.Header.Get("Authorization"))
		u.mu.Unlock()
		w.Header().Set("Content-Type", testContentJSON)
		_, _ = fmt.Fprintf(w, `{"kind":"PodList","apiVersion":"v1","metadata":{"resourceVersion":"%d"},"items":[]}`, n)
	}))
	t.Cleanup(u.srv.Close)
	return u
}

func getPods(h http.Handler, token string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func buildTestHandler(t *testing.T, cfg proxyConfig, logger *slog.Logger) http.Handler {
	t.Helper()
	h, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, k8sapiproxy.NewMetrics(), nil)
	if err != nil {
		t.Fatalf("buildHandler: %v", err)
	}
	return h
}

func TestSplitList(t *testing.T) {
	for raw, want := range map[string][]string{
		"":             nil,
		"  ":           nil,
		" , ,":         nil,
		"a":            {"a"},
		" a , b,,c , ": {"a", "b", "c"},
	} {
		got := splitList(raw)
		if !slices.Equal(got, want) || (want == nil) != (got == nil) {
			t.Errorf("splitList(%q) = %#v, want %#v", raw, got, want)
		}
	}
}

func TestParseAuthModes(t *testing.T) {
	for raw, want := range map[string][]string{
		"token-review":                        {authModeTokenReview},
		"jwt":                                 {authModeJWT},
		"none":                                {authModeNone},
		"token-review,jwt":                    {authModeTokenReview, authModeJWT},
		" jwt , token-review":                 {authModeJWT, authModeTokenReview},
		"mtls":                                {authModeMTLS},
		"requestheader":                       {authModeRequestHeader},
		"jwt,mtls":                            {authModeJWT, authModeMTLS},
		"mtls,requestheader,token-review,jwt": {authModeMTLS, authModeRequestHeader, authModeTokenReview, authModeJWT},
	} {
		got, err := parseAuthModes(raw)
		if err != nil || !slices.Equal(got, want) {
			t.Errorf("parseAuthModes(%q) = %v, %v; want %v", raw, got, err, want)
		}
	}
	for raw, wantMsg := range map[string]string{
		"":                              "invalid --auth-mode",
		",":                             "invalid --auth-mode",
		"NONE":                          "invalid --auth-mode",
		"mTLS":                          "invalid --auth-mode",
		"jwt,request-header":            "invalid --auth-mode",
		"none,mtls":                     "cannot be combined with another mode",
		"requestheader,none":            "cannot be combined with another mode",
		"requestheader,requestheader":   "listed twice",
		"none,jwt":                      "cannot be combined with another mode",
		"token-review,none":             "cannot be combined with another mode",
		"none,none":                     "cannot be combined with another mode",
		"jwt,jwt":                       "listed twice",
		"token-review,jwt,token-review": "listed twice",
	} {
		got, err := parseAuthModes(raw)
		if err == nil {
			t.Errorf("parseAuthModes(%q) = %v, want an error", raw, got)
			continue
		}
		if !strings.Contains(err.Error(), wantMsg) {
			t.Errorf("parseAuthModes(%q): err = %q, want substring %q", raw, err, wantMsg)
		}
	}
}

// With no flag and no environment the binary authenticates by TokenReview alone, in one
// partition, as it did before the JWT flags existed.
func TestParseFlagsDefaultsToTokenReviewOnly(t *testing.T) {
	clearProxyEnv(t)
	cfg := parseTestFlags(t)
	if cfg.authMode != authModeTokenReview || cfg.partitionByCaller != partitionByNone {
		t.Errorf("authMode = %q, partitionByCaller = %q; want %q and %q", cfg.authMode, cfg.partitionByCaller, authModeTokenReview, partitionByNone)
	}
	if set := jwtFlagsSet(cfg); len(set) != 0 {
		t.Errorf("JWT flags set by default: %v", set)
	}
	modes, err := parseAuthModes(cfg.authMode)
	if err != nil || !slices.Equal(modes, []string{authModeTokenReview}) {
		t.Fatalf("parseAuthModes = %v, %v", modes, err)
	}
	partition, err := callerPartitionFunc(cfg, modes)
	if err != nil || partition != nil {
		t.Errorf("callerPartitionFunc = (set=%v, %v), want no partition func", partition != nil, err)
	}
}

func TestRegisterFlagsReadsJWTEnvVars(t *testing.T) {
	clearProxyEnv(t)
	env := map[string]string{
		authModeEnv:             "token-review,jwt",
		jwtIssuerEnv:            "https://issuer.test",
		jwtKeySetURLEnv:         "https://issuer.test/keys",
		jwtAudiencesEnv:         "aud-a,aud-b",
		jwtUsernameClaimEnv:     `example\.io.user`,
		jwtUIDClaimEnv:          testUIDPath,
		jwtGroupsClaimEnv:       "groups",
		jwtPartitionClaimEnv:    testTenantPath,
		jwtRequiredSubPrefixEnv: "workload/",
		jwtAllowedPartitionsEnv: "team-a, team-b",
		jwtCAFileEnv:            "/etc/issuer/ca.crt",
		partitionByCallerEnv:    partitionByPartition,
	}
	for name, value := range env {
		t.Setenv(name, value)
	}
	cfg := parseTestFlags(t)
	got := map[string]string{
		authModeEnv:             cfg.authMode,
		jwtIssuerEnv:            cfg.jwtIssuer,
		jwtKeySetURLEnv:         cfg.jwtKeySetURL,
		jwtAudiencesEnv:         cfg.jwtAudiences,
		jwtUsernameClaimEnv:     cfg.jwtUsernameClaim,
		jwtUIDClaimEnv:          cfg.jwtUIDClaim,
		jwtGroupsClaimEnv:       cfg.jwtGroupsClaim,
		jwtPartitionClaimEnv:    cfg.jwtPartitionClaim,
		jwtRequiredSubPrefixEnv: cfg.jwtRequiredSubPrefix,
		jwtAllowedPartitionsEnv: cfg.jwtAllowedPartitions,
		jwtCAFileEnv:            cfg.jwtCAFile,
		partitionByCallerEnv:    cfg.partitionByCaller,
	}
	for name, want := range env {
		if got[name] != want {
			t.Errorf("%s: parsed %q, want %q", name, got[name], want)
		}
	}

	// A flag overrides its environment variable.
	cfg = parseTestFlags(t, "--jwt-issuer=https://flag.test", "--partition-by-caller=username")
	if cfg.jwtIssuer != "https://flag.test" || cfg.partitionByCaller != partitionByUsername {
		t.Errorf("jwtIssuer = %q, partitionByCaller = %q; want the flag values", cfg.jwtIssuer, cfg.partitionByCaller)
	}
}

// authenticatorFunc adapts a func to a CallerAuthenticator.
type authenticatorFunc func(*http.Request) (*k8sapiproxy.CallerIdentity, error)

func (f authenticatorFunc) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	return f(r)
}

// fixedAuthenticator answers every request the same way and counts the requests.
type fixedAuthenticator struct {
	caller *k8sapiproxy.CallerIdentity
	err    error
	calls  atomic.Int32
}

func (f *fixedAuthenticator) AuthenticateRequest(*http.Request) (*k8sapiproxy.CallerIdentity, error) {
	f.calls.Add(1)
	return f.caller, f.err
}

// emptyListUpstream answers every forwarded request with an empty PodList.
type emptyListUpstream struct{ calls atomic.Int32 }

func (u *emptyListUpstream) RoundTripCluster(_ context.Context, _ string, req *http.Request) (*http.Response, error) {
	u.calls.Add(1)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"Content-Type": []string{testContentJSON}},
		Body:       io.NopCloser(strings.NewReader(`{"kind":"PodList","apiVersion":"v1","items":[]}`)),
		Request:    req,
	}, nil
}

// The chain admits on the first success. When every authenticator fails, one that could
// not decide makes the answer 503, and only refusals all round make it 401.
func TestChainAuthenticator(t *testing.T) {
	refused := errors.New("token not accepted")
	unavailable := fmt.Errorf("%w: review failed", k8sapiproxy.ErrAuthenticationUnavailable)
	first := &k8sapiproxy.CallerIdentity{Username: "first", Extra: map[string][]string{"example.io/pod": {"p"}}}
	second := &k8sapiproxy.CallerIdentity{Username: "second"}

	for _, tc := range []struct {
		name            string
		chain           []*fixedAuthenticator
		wantCaller      *k8sapiproxy.CallerIdentity
		wantUnavailable bool
		wantCalls       []int32
		wantCode        int
	}{
		{
			name:       "first accepts, second is not asked",
			chain:      []*fixedAuthenticator{{caller: first}, {caller: second}},
			wantCaller: first, wantCalls: []int32{1, 0}, wantCode: http.StatusOK,
		},
		{
			name:       "first refuses, second accepts",
			chain:      []*fixedAuthenticator{{err: refused}, {caller: second}},
			wantCaller: second, wantCalls: []int32{1, 1}, wantCode: http.StatusOK,
		},
		{
			name:       "first unavailable, second accepts",
			chain:      []*fixedAuthenticator{{err: unavailable}, {caller: second}},
			wantCaller: second, wantCalls: []int32{1, 1}, wantCode: http.StatusOK,
		},
		{
			name:      "both refuse",
			chain:     []*fixedAuthenticator{{err: refused}, {err: refused}},
			wantCalls: []int32{1, 1}, wantCode: http.StatusUnauthorized,
		},
		{
			name:      "an authenticator that names no caller and no error refuses",
			chain:     []*fixedAuthenticator{{}, {err: refused}},
			wantCalls: []int32{1, 1}, wantCode: http.StatusUnauthorized,
		},
		{
			name:            "first unavailable, second refuses",
			chain:           []*fixedAuthenticator{{err: unavailable}, {err: refused}},
			wantUnavailable: true, wantCalls: []int32{1, 1}, wantCode: http.StatusServiceUnavailable,
		},
		{
			name:            "first refuses, second unavailable",
			chain:           []*fixedAuthenticator{{err: refused}, {err: unavailable}},
			wantUnavailable: true, wantCalls: []int32{1, 1}, wantCode: http.StatusServiceUnavailable,
		},
		{
			name:            "both unavailable",
			chain:           []*fixedAuthenticator{{err: unavailable}, {err: unavailable}},
			wantUnavailable: true, wantCalls: []int32{1, 1}, wantCode: http.StatusServiceUnavailable,
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			authenticators := make([]k8sapiproxy.CallerAuthenticator, 0, len(tc.chain))
			for _, a := range tc.chain {
				authenticators = append(authenticators, a)
			}
			chain := newChainAuthenticator(authenticators)
			if _, ok := chain.(chainAuthenticator); !ok {
				t.Fatalf("newChainAuthenticator returned %T for %d authenticators, want a chain", chain, len(authenticators))
			}

			caller, err := chain.AuthenticateRequest(httptest.NewRequest(http.MethodGet, testPodsPath, nil))
			if tc.wantCaller != nil {
				if err != nil || caller != tc.wantCaller {
					t.Fatalf("got (%+v, %v), want caller %+v", caller, err, tc.wantCaller)
				}
			} else {
				if err == nil || caller != nil {
					t.Fatalf("got (%+v, %v), want an error and no caller", caller, err)
				}
				if got := errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable); got != tc.wantUnavailable {
					t.Errorf("errors.Is(err, ErrAuthenticationUnavailable) = %v, want %v (err = %v)", got, tc.wantUnavailable, err)
				}
				// One line, naming the sentinel once however many authenticators wrapped it.
				wantSentinels := 0
				if tc.wantUnavailable {
					wantSentinels = 1
				}
				if got := strings.Count(err.Error(), k8sapiproxy.ErrAuthenticationUnavailable.Error()); got != wantSentinels || strings.Contains(err.Error(), "\n") {
					t.Errorf("err = %q, want one line naming the unavailable sentinel %d times", err.Error(), wantSentinels)
				}
			}
			for i, a := range tc.chain {
				if got := a.calls.Load(); got != tc.wantCalls[i] {
					t.Errorf("authenticator %d asked %d times, want %d", i, got, tc.wantCalls[i])
				}
			}

			// The same chain behind the handler answers with the matching status.
			upstream := &emptyListUpstream{}
			h, err := k8sapiproxy.NewHandler(k8sapiproxy.Config{
				Authenticator:    chain,
				Upstream:         upstream,
				Policy:           k8sapiproxy.Policy{Mode: k8sapiproxy.ModeReadOnlyWithDryRun, RedactSecrets: true},
				DefaultClusterID: k8sapiproxy.InClusterID,
				Logger:           slog.New(slog.DiscardHandler),
			})
			if err != nil {
				t.Fatal(err)
			}
			rec := getPods(h, "")
			if rec.Code != tc.wantCode {
				t.Errorf("handler answered %d, want %d", rec.Code, tc.wantCode)
			}
			wantForwarded := int32(0)
			if tc.wantCode == http.StatusOK {
				wantForwarded = 1
			}
			if got := upstream.calls.Load(); got != wantForwarded {
				t.Errorf("upstream calls = %d, want %d", got, wantForwarded)
			}
		})
	}

	// A named authenticator stamps the caller it admits and labels its reason when it refuses.
	named := func(name string, a *fixedAuthenticator) k8sapiproxy.CallerAuthenticator {
		return namedAuthenticator{name: name, next: a}
	}
	for _, tc := range []struct {
		name              string
		review, verifier  *fixedAuthenticator
		wantAuthenticator string
	}{
		{"token-review admits", &fixedAuthenticator{caller: first}, &fixedAuthenticator{caller: second}, authModeTokenReview},
		{"jwt admits", &fixedAuthenticator{err: refused}, &fixedAuthenticator{caller: second}, authModeJWT},
	} {
		chain := newChainAuthenticator([]k8sapiproxy.CallerAuthenticator{named(authModeTokenReview, tc.review), named(authModeJWT, tc.verifier)})
		caller, err := chain.AuthenticateRequest(httptest.NewRequest(http.MethodGet, testPodsPath, nil))
		if err != nil || caller.Authenticator != tc.wantAuthenticator {
			t.Errorf("%s: got (%+v, %v), want Authenticator %q", tc.name, caller, err, tc.wantAuthenticator)
		}
		// The stamped copy keeps what the authenticator said about the caller.
		if err == nil && caller.Username == first.Username && !slices.Equal(caller.Extra["example.io/pod"], []string{"p"}) {
			t.Errorf("%s: Extra = %v, want the authenticator's", tc.name, caller.Extra)
		}
	}
	if first.Authenticator != "" || second.Authenticator != "" {
		t.Error("a named authenticator changed the identity it was handed instead of a copy")
	}
	_, err := newChainAuthenticator([]k8sapiproxy.CallerAuthenticator{
		named(authModeTokenReview, &fixedAuthenticator{err: unavailable}), named(authModeJWT, &fixedAuthenticator{err: refused}),
	}).AuthenticateRequest(httptest.NewRequest(http.MethodGet, testPodsPath, nil))
	if want := "caller authentication is unavailable: token-review: review failed; jwt: token not accepted"; err == nil || err.Error() != want {
		t.Errorf("err = %v, want %q", err, want)
	}
	if !errors.Is(err, k8sapiproxy.ErrAuthenticationUnavailable) {
		t.Errorf("errors.Is(%v, ErrAuthenticationUnavailable) = false", err)
	}

	// Every authenticator sees the Authorization header the request arrived with, even after
	// an earlier one consumed it, and the request itself keeps it.
	var seen []string
	consumes := authenticatorFunc(func(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
		seen = append(seen, r.Header.Get("Authorization"))
		r.Header.Del("Authorization")
		return nil, refused
	})
	req := httptest.NewRequest(http.MethodGet, testPodsPath, nil)
	req.Header.Set("Authorization", "Bearer caller-token")
	if _, err := newChainAuthenticator([]k8sapiproxy.CallerAuthenticator{consumes, consumes, consumes}).AuthenticateRequest(req); err == nil {
		t.Error("a chain of refusing authenticators accepted")
	}
	if want := []string{"Bearer caller-token", "Bearer caller-token", "Bearer caller-token"}; !slices.Equal(seen, want) {
		t.Errorf("authenticators saw Authorization %q, want %q", seen, want)
	}
	if got := req.Header.Get("Authorization"); got != "Bearer caller-token" {
		t.Errorf("the request's Authorization after the chain = %q, want it unchanged", got)
	}

	// One authenticator is used as it is, not wrapped.
	only := &fixedAuthenticator{caller: first}
	if got := newChainAuthenticator([]k8sapiproxy.CallerAuthenticator{only}); got != k8sapiproxy.CallerAuthenticator(only) {
		t.Errorf("newChainAuthenticator wrapped a single authenticator as %T", got)
	}
}

// --auth-mode=jwt with --partition-by-caller=partition: callers whose tokens carry
// different partition claims never share a cached response, and callers in one partition do.
func TestBuildHandlerJWTPartitionsCachedResponsesByCaller(t *testing.T) {
	clearProxyEnv(t)
	is := newTestJWTIssuer(t)
	up := newCountingUpstream(t)
	var logs bytes.Buffer
	cfg := parseTestFlags(t, is.jwtArgs(up.srv.URL,
		"--jwt-partition-claim="+testTenantPath,
		"--jwt-uid-claim="+testUIDPath,
		"--partition-by-caller=partition",
	)...)
	h := buildTestHandler(t, cfg, slog.New(slog.NewJSONHandler(&logs, nil)))

	tokenA := is.tenantToken("agent-a1", testTeamA)
	firstA := getPods(h, tokenA)
	if firstA.Code != http.StatusOK || up.calls.Load() != 1 {
		t.Fatalf("team-a first read: code = %d, upstream calls = %d; want 200 and 1; body = %s", firstA.Code, up.calls.Load(), firstA.Body.String())
	}

	tokenB := is.tenantToken("agent-b1", testTeamB)
	firstB := getPods(h, tokenB)
	if firstB.Code != http.StatusOK {
		t.Fatalf("team-b first read: code = %d, body = %s", firstB.Code, firstB.Body.String())
	}
	if got := up.calls.Load(); got != 2 {
		t.Errorf("upstream calls after team-b's first read = %d, want 2: it must not be served team-a's response", got)
	}
	if firstB.Header().Get("Cache-Status") == testCacheHit || firstB.Body.String() == firstA.Body.String() {
		t.Errorf("team-b got team-a's cached response (Cache-Status %q, body %s)", firstB.Header().Get("Cache-Status"), firstB.Body.String())
	}

	// Each partition is then answered from its own entry, whoever in it asks.
	for _, tc := range []struct {
		name, token, wantBody string
	}{
		{"team-a again", tokenA, firstA.Body.String()},
		{"another team-a caller", is.tenantToken("agent-a2", testTeamA), firstA.Body.String()},
		{"team-b again", tokenB, firstB.Body.String()},
		{"another team-b caller", is.tenantToken("agent-b2", testTeamB), firstB.Body.String()},
	} {
		rec := getPods(h, tc.token)
		if rec.Code != http.StatusOK || rec.Header().Get("Cache-Status") != testCacheHit || rec.Body.String() != tc.wantBody {
			t.Errorf("%s: code = %d, Cache-Status = %q, body = %s; want 200, a hit, and body %s",
				tc.name, rec.Code, rec.Header().Get("Cache-Status"), rec.Body.String(), tc.wantBody)
		}
	}
	if got := up.calls.Load(); got != 2 {
		t.Errorf("upstream calls = %d, want 2: one per partition", got)
	}

	// A caller without the partition claim is refused, not put in a shared partition.
	for name, token := range map[string]string{
		"no partition claim":    is.sign("agent-x", testJWTAudience, map[string]any{"uid": "uid-x"}),
		"empty partition claim": is.tenantToken("agent-x", ""),
		"no domain object":      is.sign("agent-x", testJWTAudience, nil),
		"wrong audience":        is.sign("agent-x", "kube-apiserver", map[string]any{"tenant": testTeamA, "uid": "uid-x"}),
		"not a JWT":             "caller-token",
		"no token":              "",
	} {
		if rec := getPods(h, token); rec.Code != http.StatusUnauthorized {
			t.Errorf("%s: code = %d, want 401", name, rec.Code)
		}
	}
	if got := up.calls.Load(); got != 2 {
		t.Errorf("upstream calls = %d after refused callers, want 2", got)
	}

	up.mu.Lock()
	for i, authz := range up.authz {
		if authz != "" {
			t.Errorf("upstream request %d carried the caller's Authorization header", i)
		}
	}
	up.mu.Unlock()
	// The audit record names the caller and its partition and never carries a token.
	for _, want := range []string{`"caller_authenticator":"jwt"`, `"caller_partition":"team-a"`, `"caller_partition":"team-b"`, `"caller_username":"agent-a1"`, `"caller_uid":"uid-agent-a1"`} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("logs lack %s", want)
		}
	}
	for _, token := range []string{tokenA, tokenB} {
		if strings.Contains(logs.String(), token) || strings.Contains(logs.String(), token[strings.LastIndex(token, ".")+1:]) {
			t.Error("logs carry a caller's token")
		}
	}
}

// The same callers without --partition-by-caller share one cached response, which is what
// the flag changes; --partition-by-caller=username separates each caller.
func TestBuildHandlerPartitionByCallerModes(t *testing.T) {
	clearProxyEnv(t)
	is := newTestJWTIssuer(t)
	tokenA := is.tenantToken("agent-a1", testTeamA)
	tokenA2 := is.tenantToken("agent-a2", testTeamA)
	tokenB := is.tenantToken("agent-b1", testTeamB)

	for _, tc := range []struct {
		name      string
		args      []string
		wantCalls int32
	}{
		{"default", nil, 1},
		{"none", []string{"--partition-by-caller=none"}, 1},
		{"partition", []string{"--partition-by-caller=partition", "--jwt-partition-claim=" + testTenantPath}, 2},
		{"username", []string{"--partition-by-caller=username"}, 3},
	} {
		t.Run(tc.name, func(t *testing.T) {
			up := newCountingUpstream(t)
			h := buildTestHandler(t, parseTestFlags(t, is.jwtArgs(up.srv.URL, tc.args...)...), slog.New(slog.DiscardHandler))
			for _, token := range []string{tokenA, tokenA2, tokenB, tokenA, tokenA2, tokenB} {
				if rec := getPods(h, token); rec.Code != http.StatusOK {
					t.Fatalf("code = %d, body = %s", rec.Code, rec.Body.String())
				}
			}
			if got := up.calls.Load(); got != tc.wantCalls {
				t.Errorf("upstream calls = %d, want %d", got, tc.wantCalls)
			}
		})
	}
}

// --auth-mode=token-review,jwt through the built handler: a caller either authenticator
// accepts is forwarded, one both refuse is a 401, and one the JWT authenticator refuses
// while the TokenReview fails is a 503.
func TestBuildHandlerChainsTokenReviewAndJWT(t *testing.T) {
	clearProxyEnv(t)
	is := newTestJWTIssuer(t)
	up := newCountingUpstream(t)
	var reviewFails atomic.Bool
	var reviews atomic.Int32
	review := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		reviews.Add(1)
		if reviewFails.Load() {
			w.WriteHeader(http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", testContentJSON)
		_, _ = io.WriteString(w, testReviewRefused)
	}))
	t.Cleanup(review.Close)

	for _, order := range []string{"token-review,jwt", "jwt,token-review"} {
		t.Run(order, func(t *testing.T) {
			reviewFails.Store(false)
			cfg := parseTestFlags(t, is.jwtArgs(up.srv.URL,
				"--auth-mode="+order,
				"--allowed-callers="+testReviewCaller,
				"--kube-api-server="+review.URL,
				"--kube-ca-file=",
				"--kube-token-file=",
				"--cache-max-bytes=0",
			)...)
			h := buildTestHandler(t, cfg, slog.New(slog.DiscardHandler))
			valid := func() string { return is.sign("agent-1", testJWTAudience, nil) }
			invalid := func() string { return is.sign("agent-1", "kube-apiserver", nil) }

			if rec := getPods(h, valid()); rec.Code != http.StatusOK {
				t.Errorf("valid JWT, TokenReview refusing: code = %d, want 200; body = %s", rec.Code, rec.Body.String())
			}
			before := reviews.Load()
			if rec := getPods(h, invalid()); rec.Code != http.StatusUnauthorized {
				t.Errorf("JWT for another audience, TokenReview refusing: code = %d, want 401", rec.Code)
			}
			if reviews.Load() == before {
				t.Error("the TokenReview authenticator was not asked about a token the JWT authenticator refused")
			}

			reviewFails.Store(true)
			if rec := getPods(h, invalid()); rec.Code != http.StatusServiceUnavailable {
				t.Errorf("JWT for another audience, TokenReview failing: code = %d, want 503", rec.Code)
			}
			if rec := getPods(h, valid()); rec.Code != http.StatusOK {
				t.Errorf("valid JWT, TokenReview failing: code = %d, want 200", rec.Code)
			}
		})
	}
}

// With jwt listed first, a token from another issuer, such as a ServiceAccount token meant
// for the TokenReview authenticator, reaches TokenReview without the JWT authenticator
// contacting its own issuer for the token's unknown key.
func TestBuildHandlerChainPassesAnotherIssuersTokenToTokenReview(t *testing.T) {
	clearProxyEnv(t)
	is := newTestJWTIssuer(t)
	foreign := newTestJWTIssuer(t)
	up := newCountingUpstream(t)
	var reviews atomic.Int32
	review := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", testContentJSON)
		if r.Method == http.MethodGet && r.URL.Path == testCallerPodPath {
			_, _ = io.WriteString(w, testCallerPod)
			return
		}
		reviews.Add(1)
		_, _ = io.WriteString(w, testReviewAccepted)
	}))
	t.Cleanup(review.Close)
	cfg := parseTestFlags(t, is.jwtArgs(up.srv.URL,
		"--auth-mode=jwt,token-review",
		"--allowed-callers="+testReviewCaller,
		"--audience="+testReviewAudience,
		"--kube-api-server="+review.URL,
		"--kube-ca-file=",
		"--kube-token-file=",
	)...)
	var logs bytes.Buffer
	h := buildTestHandler(t, cfg, slog.New(slog.NewJSONHandler(&logs, nil)))

	rec := getPods(h, foreign.signAs(testForeignIssuer, "system:serviceaccount:ns:sa", testReviewAudience, nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("another issuer's token that TokenReview accepts: code = %d, want 200; body = %s", rec.Code, rec.Body.String())
	}
	if got := reviews.Load(); got != 1 {
		t.Errorf("TokenReviews = %d, want 1", got)
	}
	if got := is.served.Load(); got != 0 {
		t.Errorf("the JWT issuer got %d requests for another issuer's token, want 0", got)
	}
	for _, want := range []string{`"caller_username":"` + testReviewCaller + `"`, `"caller_authenticator":"token-review"`} {
		if !strings.Contains(logs.String(), want) {
			t.Errorf("logs lack %s", want)
		}
	}
}

// --partition-by-caller passed empty is refused; left out, or empty in the environment, it
// is the default, none.
func TestBuildHandlerRejectsAnEmptyPartitionByCallerFlag(t *testing.T) {
	clearProxyEnv(t)
	is := newTestJWTIssuer(t)
	logger := slog.New(slog.DiscardHandler)
	for _, arg := range []string{"--partition-by-caller=", "--partition-by-caller= ", "--partition-by-caller=\t\n"} {
		cfg := parseTestFlags(t, is.jwtArgs("http://127.0.0.1:18080", arg)...)
		_, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil)
		if err == nil || !strings.Contains(err.Error(), "invalid --"+partitionByCallerFlag) {
			t.Errorf("%q: err = %v, want invalid --%s", arg, err, partitionByCallerFlag)
		}
	}

	t.Setenv(partitionByCallerEnv, "  ")
	cfg := parseTestFlags(t, is.jwtArgs("http://127.0.0.1:18080")...)
	if cfg.partitionByCaller != partitionByNone || cfg.partitionByCallerSet {
		t.Errorf("partitionByCaller = %q (set=%v), want the default %q", cfg.partitionByCaller, cfg.partitionByCallerSet, partitionByNone)
	}
	if _, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil); err != nil {
		t.Errorf("an empty %s: %v", partitionByCallerEnv, err)
	}
}

func TestBuildHandlerValidatesJWTFlags(t *testing.T) {
	clearProxyEnv(t)
	base := proxyConfig{
		listenAddr:    "127.0.0.1:18443",
		listenAddrSet: true,
		authMode:      authModeJWT,
		upstreamURL:   "http://127.0.0.1:18080",
		redactSecrets: true,
		maxInFlight:   defaultMaxInFlight,
		jwtIssuer:     "https://issuer.test",
		jwtAudiences:  testJWTAudience,
	}
	logger := slog.New(slog.DiscardHandler)

	// Neither --allowed-callers nor the host cluster's ServiceAccount files are needed.
	for name, mutate := range map[string]func(*proxyConfig){
		"discovered key set":                     func(*proxyConfig) {},
		"explicit key set URL, issuer not a URL": func(c *proxyConfig) { c.jwtIssuer, c.jwtKeySetURL = "issuer-name", "https://keys.test/jwks" },
		"every claim flag": func(c *proxyConfig) {
			c.jwtUsernameClaim, c.jwtUIDClaim, c.jwtGroupsClaim = `example\.io.user`, testUIDPath, "groups"
			c.jwtPartitionClaim, c.jwtAllowedPartitions, c.jwtRequiredSubPrefix = testTenantPath, "team-a, team-b", "workload/"
			c.partitionByCaller = partitionByPartition
		},
		"values padded with whitespace": func(c *proxyConfig) {
			c.jwtIssuer, c.jwtKeySetURL, c.jwtAudiences = " https://issuer.test\n", "\thttps://issuer.test/keys \n", " "+testJWTAudience+"\n"
			c.jwtPartitionClaim, c.jwtAllowedPartitions, c.jwtRequiredSubPrefix = " "+testTenantPath+" ", " team-a\n", " workload/\n"
			c.partitionByCaller = " " + partitionByPartition + "\n"
		},
		"whitespace-only optional values": func(c *proxyConfig) {
			c.jwtKeySetURL, c.jwtUsernameClaim, c.jwtAllowedPartitions, c.jwtCAFile, c.partitionByCaller = " ", "\n", "  ", "\t", " "
		},
		"partition by username": func(c *proxyConfig) { c.partitionByCaller = partitionByUsername },
		"chained with token-review": func(c *proxyConfig) {
			c.authMode, c.allowedCallers, c.audience = "jwt,token-review", testReviewCaller, defaultAudience
			c.kubeAPIServer = "http://127.0.0.1:18080"
		},
	} {
		t.Run("accepts "+name, func(t *testing.T) {
			cfg := base
			mutate(&cfg)
			if _, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil); err != nil {
				t.Errorf("buildHandler: %v", err)
			}
		})
	}

	for _, tc := range []struct {
		name    string
		mutate  func(*proxyConfig)
		wantMsg string
	}{
		{"none combined with jwt", func(c *proxyConfig) { c.authMode = "none,jwt" }, "cannot be combined with another mode"},
		{"jwt listed twice", func(c *proxyConfig) { c.authMode = "jwt,jwt" }, "listed twice"},
		{"no issuer", func(c *proxyConfig) { c.jwtIssuer = " " }, "--" + jwtIssuerFlag + " (or " + jwtIssuerEnv + ") is required"},
		{"no audiences", func(c *proxyConfig) { c.jwtAudiences = " , " }, "--" + jwtAudiencesFlag + " (or " + jwtAudiencesEnv + ") is required"},
		{"http issuer without a key set URL", func(c *proxyConfig) { c.jwtIssuer = "http://issuer.test" }, "KeySetURL is required"},
		{"issuer that is not a URL without a key set URL", func(c *proxyConfig) { c.jwtIssuer = "issuer-name" }, "KeySetURL is required"},
		{"http key set URL", func(c *proxyConfig) { c.jwtKeySetURL = "http://issuer.test/keys" }, "KeySetURL must be an https URL"},
		{"allowed partitions without a partition claim", func(c *proxyConfig) { c.jwtAllowedPartitions = "team-a" }, "AllowedPartitions requires"},
		{"allowed partitions of only separators", func(c *proxyConfig) {
			c.jwtPartitionClaim, c.jwtAllowedPartitions = testTenantPath, " , "
		}, "--" + jwtAllowedPartitionsFlag + " (or " + jwtAllowedPartitionsEnv + ") names no partition"},
		{"allowed partitions of one separator, no partition claim", func(c *proxyConfig) { c.jwtAllowedPartitions = "," }, "names no partition"},
		{"issuer with a bare ? and no key set URL", func(c *proxyConfig) { c.jwtIssuer = "https://issuer.test/a?" }, "KeySetURL is required"},
		{"issuer with a bare # and no key set URL", func(c *proxyConfig) { c.jwtIssuer = "https://issuer.test/a#" }, "KeySetURL is required"},
		{"partition-by-caller=username with two modes", func(c *proxyConfig) {
			c.authMode, c.allowedCallers, c.audience = "token-review,jwt", testReviewCaller, defaultAudience
			c.partitionByCaller = partitionByUsername
		}, "--" + partitionByCallerFlag + "=username cannot be combined with more than one mode"},
		{"empty key in a claim path", func(c *proxyConfig) { c.jwtUsernameClaim = "a..b" }, "UsernameClaim"},
		{"CA file that does not exist", func(c *proxyConfig) { c.jwtCAFile = filepath.Join(t.TempDir(), "absent.crt") }, "CAFile"},
		{"allowed-callers without token-review", func(c *proxyConfig) { c.allowedCallers = testReviewCaller }, "--allowed-callers"},
		{"session audience without token-review", func(c *proxyConfig) { c.sessionAudience = "session-aud" }, sessionAudienceEnv},
		{"session callers without token-review", func(c *proxyConfig) { c.sessionCallers = testReviewCaller }, sessionCallersEnv},
		{"chain without allowed-callers", func(c *proxyConfig) { c.authMode = "token-review,jwt" }, "--allowed-callers"},
		{"JWT flags with token-review alone", func(c *proxyConfig) {
			c.authMode, c.allowedCallers = authModeTokenReview, testReviewCaller
		}, "--" + jwtIssuerFlag + ", --" + jwtAudiencesFlag + ` requires "jwt"`},
		{"JWT CA file with none", func(c *proxyConfig) {
			c.authMode, c.jwtIssuer, c.jwtAudiences, c.jwtCAFile = authModeNone, "", "", "/etc/issuer/ca.crt"
		}, "--" + jwtCAFileFlag + ` requires "jwt"`},
		{"unknown partition-by-caller", func(c *proxyConfig) { c.partitionByCaller = "tenant" }, "invalid --" + partitionByCallerFlag},
		{"partition-by-caller=partition without a partition claim", func(c *proxyConfig) { c.partitionByCaller = partitionByPartition }, "--" + partitionByCallerFlag + "=partition requires"},
		{"partition-by-caller=partition without jwt", func(c *proxyConfig) {
			c.authMode, c.allowedCallers, c.jwtIssuer, c.jwtAudiences = authModeTokenReview, testReviewCaller, "", ""
			c.partitionByCaller = partitionByPartition
		}, "--" + partitionByCallerFlag + "=partition requires"},
	} {
		t.Run("rejects "+tc.name, func(t *testing.T) {
			cfg := base
			tc.mutate(&cfg)
			_, err := buildHandler(context.Background(), cfg, k8sapiproxy.ModeReadOnlyWithDryRun, logger, nil, nil)
			if err == nil {
				t.Fatal("buildHandler succeeded, want error")
			}
			if !strings.Contains(err.Error(), tc.wantMsg) {
				t.Errorf("err = %q, want substring %q", err.Error(), tc.wantMsg)
			}
		})
	}
}
