// Package jwtauth is a k8sapiproxy.CallerAuthenticator that admits a caller presenting a
// signed JWT: a bearer token naming its key (kid) whose signature verifies against an
// issuer's JSON Web Key Set, fetched over HTTPS, and whose issuer, audience and expiry
// match. The caller's identity comes from configurable claims. It suits a platform that
// mints a token per sandbox, such as an agent runtime that injects an actor token at its
// egress gateway.
package jwtauth

import (
	"context"
	"crypto/ecdsa"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"golang.org/x/sync/singleflight"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/certreload"
	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// defaultUsernameClaim names the caller after the token's subject.
	defaultUsernameClaim = "sub"
	// defaultKeySetTTL is how long a fetched key set is used before it is fetched again.
	defaultKeySetTTL = time.Hour
	// maxStaleKeySet is how long past its TTL a key set still verifies tokens whose key it
	// holds while the issuer cannot be reached, so an outage does not lock every caller out
	// but a key the issuer has since removed stops working within a bounded time.
	maxStaleKeySet = 6 * time.Hour
	// minKeySetFetchInterval bounds how often the key set is fetched, successfully or not,
	// so neither tokens naming unknown keys nor an unreachable issuer can make the proxy
	// flood the issuer.
	minKeySetFetchInterval = 10 * time.Second
	keySetFetchTimeout     = 10 * time.Second
	// maxKeySetBytes caps the key set response read into memory.
	maxKeySetBytes = 1 << 20
	// maxKeySetRedirects is net/http's default limit, which a custom redirect policy replaces.
	maxKeySetRedirects = 10
	// clockLeeway tolerates clock skew between the issuer and the proxy on exp, nbf and iat.
	clockLeeway = jwt.DefaultLeeway

	schemeHTTPS         = "https"
	bearerPrefix        = "Bearer "
	headerAuthorization = "Authorization"
	// keyUseSignature is the JWK "use" of a signing key; a key with another use is skipped.
	keyUseSignature = "sig"
	// The algorithm-name prefixes of the RSA (PKCS #1 and PSS) and ECDSA families.
	algPrefixRSA    = "RS"
	algPrefixRSAPSS = "PS"
	algPrefixECDSA  = "ES"
)

// defaultSignatureAlgorithms are the asymmetric algorithms a token may be signed with.
// Symmetric (HS*) algorithms and "none" are never accepted.
var defaultSignatureAlgorithms = []jose.SignatureAlgorithm{jose.RS256, jose.ES256}

var errNotAuthenticated = errors.New("the caller's token was not accepted")

// Config configures NewAuthenticator.
type Config struct {
	// Issuer is the iss a token must carry. Required.
	Issuer string
	// KeySetURL serves the issuer's JSON Web Key Set. Required, and must be https: anyone
	// who can alter the key set can mint tokens for any caller.
	KeySetURL string
	// Audiences lists the aud values accepted; a token must carry at least one. Choose ones
	// no other service accepts, so a token minted for the proxy is useless elsewhere.
	// At least one is required.
	Audiences []string
	// UsernameClaim names the string claim used as the caller's username; "sub" when empty.
	UsernameClaim string
	// GroupsClaim names a claim holding the caller's groups, a string or a list of strings;
	// optional.
	GroupsClaim string
	// Allow, when set, is asked whether an authenticated caller is admitted; an error refuses
	// the request with 401. Without it, every token from Issuer carrying one of Audiences is
	// admitted, so leave it nil only when the issuer mints that audience for callers that
	// may all use the proxy.
	Allow func(*k8sapiproxy.CallerIdentity) error
	// SignatureAlgorithms are the algorithms accepted; RS256 and ES256 when empty. Only
	// RS*, PS* and ES* are allowed.
	SignatureAlgorithms []jose.SignatureAlgorithm
	// HTTPClient fetches the key set; a client with a 10s timeout when nil. Its redirects are
	// refused unless they stay on https.
	HTTPClient *http.Client
	// CAFile, when set, is a PEM bundle of the root CAs the key set's server is verified
	// against instead of the system roots. It is re-read when it changes, so a rotated CA is
	// trusted from the next connection without a restart; a changed bundle that cannot be
	// loaded is logged and the previous one kept. The file must load when NewAuthenticator
	// is called. The fetch then speaks HTTP/1.1 only and uses no HTTP proxy. It cannot be combined with an
	// HTTPClient that sets its own Transport.
	CAFile string
	// KeySetTTL is how long a fetched key set is reused; one hour when zero.
	KeySetTTL time.Duration
	// Now returns the current time; time.Now when nil. For tests.
	Now func() time.Time
}

type authenticator struct {
	issuer        string
	audiences     []string
	usernameClaim string
	groupsClaim   string
	allow         func(*k8sapiproxy.CallerIdentity) error
	algorithms    []jose.SignatureAlgorithm
	keys          *keySet
	now           func() time.Time
}

// NewAuthenticator returns a CallerAuthenticator that verifies a bearer JWT in the
// Authorization header against cfg. When the key set is needed and cannot be fetched, the
// error wraps k8sapiproxy.ErrAuthenticationUnavailable, so the handler answers 503.
func NewAuthenticator(cfg Config) (k8sapiproxy.CallerAuthenticator, error) {
	if cfg.Issuer == "" {
		return nil, errors.New("jwtauth: Config.Issuer is required")
	}
	if u, err := url.Parse(cfg.KeySetURL); err != nil || u.Scheme != schemeHTTPS || u.Host == "" {
		return nil, errors.New("jwtauth: Config.KeySetURL must be an https URL")
	}
	var audiences []string
	for _, aud := range cfg.Audiences {
		if aud = strings.TrimSpace(aud); aud != "" {
			audiences = append(audiences, aud)
		}
	}
	if len(audiences) == 0 {
		return nil, errors.New("jwtauth: Config.Audiences is required")
	}
	algorithms := cfg.SignatureAlgorithms
	if len(algorithms) == 0 {
		algorithms = defaultSignatureAlgorithms
	}
	for _, alg := range algorithms {
		if !isAsymmetric(alg) {
			return nil, fmt.Errorf("jwtauth: signature algorithm %s is not an asymmetric algorithm", alg)
		}
	}
	usernameClaim := cfg.UsernameClaim
	if usernameClaim == "" {
		usernameClaim = defaultUsernameClaim
	}
	client := &http.Client{Timeout: keySetFetchTimeout}
	if cfg.HTTPClient != nil {
		copied := *cfg.HTTPClient
		client = &copied
	}
	client.CheckRedirect = secureRedirectPolicy(client.CheckRedirect)
	if cfg.CAFile != "" {
		if client.Transport != nil {
			return nil, errors.New("jwtauth: Config.CAFile cannot be combined with Config.HTTPClient.Transport")
		}
		roots, err := certreload.NewCAPool(cfg.CAFile, certreload.Options{})
		if err != nil {
			return nil, fmt.Errorf("jwtauth: Config.CAFile: %w", err)
		}
		client.Transport = roots.Transport(nil)
	}
	ttl := cfg.KeySetTTL
	if ttl <= 0 {
		ttl = defaultKeySetTTL
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	return &authenticator{
		issuer:        cfg.Issuer,
		audiences:     audiences,
		usernameClaim: usernameClaim,
		groupsClaim:   cfg.GroupsClaim,
		allow:         cfg.Allow,
		algorithms:    algorithms,
		keys:          &keySet{url: cfg.KeySetURL, client: client, ttl: ttl, now: now},
		now:           now,
	}, nil
}

// secureRedirectPolicy keeps a key set fetch on https across redirects, stops after
// maxKeySetRedirects as net/http's default policy does, and then applies the caller's own
// policy, if any.
func secureRedirectPolicy(next func(*http.Request, []*http.Request) error) func(*http.Request, []*http.Request) error {
	return func(req *http.Request, via []*http.Request) error {
		if req.URL.Scheme != schemeHTTPS {
			return fmt.Errorf("key set redirect to %s refused: not https", req.URL.Scheme)
		}
		if len(via) >= maxKeySetRedirects {
			return fmt.Errorf("key set fetch stopped after %d redirects", maxKeySetRedirects)
		}
		if next != nil {
			return next(req, via)
		}
		return nil
	}
}

func isAsymmetric(alg jose.SignatureAlgorithm) bool {
	switch alg {
	case jose.RS256, jose.RS384, jose.RS512, jose.PS256, jose.PS384, jose.PS512, jose.ES256, jose.ES384, jose.ES512:
		return true
	}
	return false
}

func (a *authenticator) AuthenticateRequest(r *http.Request) (*k8sapiproxy.CallerIdentity, error) {
	raw, ok := strings.CutPrefix(r.Header.Get(headerAuthorization), bearerPrefix)
	if !ok || strings.TrimSpace(raw) == "" {
		return nil, errNotAuthenticated
	}
	tok, err := jwt.ParseSigned(strings.TrimSpace(raw), a.algorithms)
	if err != nil {
		return nil, errNotAuthenticated
	}
	// A token must name its key: keys are matched by kid, never tried one by one.
	if len(tok.Headers) != 1 || tok.Headers[0].KeyID == "" {
		return nil, errNotAuthenticated
	}
	header := tok.Headers[0]

	alg := jose.SignatureAlgorithm(header.Algorithm)
	set, err := a.keys.get(r.Context(), header.KeyID, alg)
	if err != nil {
		return nil, err
	}
	key := signingKey(set, header.KeyID, alg)
	if key == nil {
		return nil, errNotAuthenticated
	}
	var claims jwt.Claims
	var extra map[string]any
	if err := tok.Claims(key, &claims, &extra); err != nil {
		return nil, errNotAuthenticated
	}
	// The library accepts a token without exp; a caller credential must expire.
	if claims.Expiry == nil {
		return nil, errNotAuthenticated
	}
	if err := claims.ValidateWithLeeway(jwt.Expected{
		Issuer:      a.issuer,
		AnyAudience: jwt.Audience(a.audiences),
		Time:        a.now(),
	}, clockLeeway); err != nil {
		return nil, errNotAuthenticated
	}

	username, _ := extra[a.usernameClaim].(string)
	if username == "" {
		return nil, errNotAuthenticated
	}
	id := &k8sapiproxy.CallerIdentity{Username: username, UID: claims.Subject}
	if a.groupsClaim != "" {
		groups, ok := stringsClaim(extra[a.groupsClaim])
		if !ok {
			return nil, errNotAuthenticated
		}
		id.Groups = groups
	}
	if a.allow != nil {
		if err := a.allow(id); err != nil {
			return nil, fmt.Errorf("caller %q is not allowed: %w", username, err)
		}
	}
	return id, nil
}

// signingKey picks, among the keys published under kid, the public key that can verify
// alg: one whose type fits alg, whose use is signing or unset, and whose own alg is alg or
// unset. Keys for another algorithm or use under the same kid are passed over, not tried.
func signingKey(set *jose.JSONWebKeySet, kid string, alg jose.SignatureAlgorithm) any {
	name := string(alg)
	for _, k := range set.Key(kid) {
		if k.Use != "" && k.Use != keyUseSignature {
			continue
		}
		if k.Algorithm != "" && k.Algorithm != name {
			continue
		}
		switch k.Key.(type) {
		case *rsa.PublicKey:
			if strings.HasPrefix(name, algPrefixRSA) || strings.HasPrefix(name, algPrefixRSAPSS) {
				return k.Key
			}
		case *ecdsa.PublicKey:
			if strings.HasPrefix(name, algPrefixECDSA) {
				return k.Key
			}
		}
	}
	return nil
}

// stringsClaim reads a claim that is absent, a string, or a list of strings.
func stringsClaim(v any) ([]string, bool) {
	switch c := v.(type) {
	case nil:
		return nil, true
	case string:
		return []string{c}, true
	case []any:
		out := make([]string, 0, len(c))
		for _, e := range c {
			s, ok := e.(string)
			if !ok {
				return nil, false
			}
			out = append(out, s)
		}
		return out, true
	}
	return nil, false
}

// keySet caches the issuer's JSON Web Key Set. A token whose key ID the cached set lacks
// triggers a fetch, and so does an expired set; fetches, failed or not, are at least
// minKeySetFetchInterval apart. Concurrent requests share one fetch, none holds the lock
// while it runs, and a request whose caller leaves stops waiting for it.
type keySet struct {
	url    string
	client *http.Client
	ttl    time.Duration
	now    func() time.Time
	flight singleflight.Group

	mu          sync.Mutex
	set         *jose.JSONWebKeySet
	fetchedAt   time.Time
	attemptedAt time.Time
	lastErr     error
}

// get returns a key set to verify a token signed by kid with alg. When the key set must be
// fetched and cannot be, and no usable cached set holds such a key, the error wraps
// k8sapiproxy.ErrAuthenticationUnavailable.
func (k *keySet) get(ctx context.Context, kid string, alg jose.SignatureAlgorithm) (*jose.JSONWebKeySet, error) {
	k.mu.Lock()
	now := k.now()
	cached, fetchedAt, lastErr := k.set, k.fetchedAt, k.lastErr
	// The cache holds the key only if one under kid can verify alg; a key of another type
	// under the same kid does not count, so a newly published one is fetched.
	hasKid := cached != nil && signingKey(cached, kid, alg) != nil
	fresh := cached != nil && now.Sub(fetchedAt) < k.ttl
	usableStale := hasKid && now.Sub(fetchedAt) < k.ttl+maxStaleKeySet
	recentlyTried := !k.attemptedAt.IsZero() && now.Sub(k.attemptedAt) < minKeySetFetchInterval
	k.mu.Unlock()

	if fresh && hasKid {
		return cached, nil
	}
	if recentlyTried {
		return settle(cached, hasKid, usableStale, lastErr)
	}

	ch := k.flight.DoChan(k.url, func() (any, error) {
		// The fetch outlives a caller that leaves, so the requests sharing it still get keys.
		set, err := k.fetch(context.WithoutCancel(ctx))
		k.mu.Lock()
		defer k.mu.Unlock()
		// Recorded when the fetch ends, not when it starts: a request arriving while it runs
		// joins it instead of reading "tried a moment ago" and answering without its result.
		k.attemptedAt = k.now()
		k.lastErr = err
		if err == nil {
			k.set, k.fetchedAt = set, k.now()
		}
		return set, err
	})
	select {
	case res := <-ch:
		if res.Err == nil {
			return res.Val.(*jose.JSONWebKeySet), nil
		}
		return settle(cached, hasKid, usableStale, res.Err)
	case <-ctx.Done():
		return nil, fmt.Errorf("%w: %w", k8sapiproxy.ErrAuthenticationUnavailable, ctx.Err())
	}
}

// settle answers without a new fetch: the cached set when it can still verify kid, the
// unavailable error when the last fetch failed or nothing was ever fetched, and otherwise
// the cached set, against which a token naming an unknown key is refused.
func settle(cached *jose.JSONWebKeySet, hasKid, usableStale bool, lastErr error) (*jose.JSONWebKeySet, error) {
	switch {
	case hasKid && usableStale:
		return cached, nil
	case lastErr != nil:
		return nil, fmt.Errorf("%w: %w", k8sapiproxy.ErrAuthenticationUnavailable, lastErr)
	case cached == nil:
		return nil, fmt.Errorf("%w: key set not fetched yet", k8sapiproxy.ErrAuthenticationUnavailable)
	default:
		return cached, nil
	}
}

func (k *keySet) fetch(ctx context.Context) (*jose.JSONWebKeySet, error) {
	ctx, cancel := context.WithTimeout(ctx, keySetFetchTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, k.url, nil)
	if err != nil {
		return nil, fmt.Errorf("key set request: %w", err)
	}
	resp, err := k.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetching key set: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetching key set: status %d", resp.StatusCode)
	}
	return decodeKeySet(io.LimitReader(resp.Body, maxKeySetBytes))
}

// decodeKeySet reads a JSON Web Key Set, keeping the public keys it can use and skipping
// the rest (RFC 7517 section 5): a key of a type the library does not support, a malformed
// key, a private or symmetric key, or one whose use is not signing. A set with no usable
// key is an error.
func decodeKeySet(r io.Reader) (*jose.JSONWebKeySet, error) {
	var raw struct {
		Keys []json.RawMessage `json:"keys"`
	}
	if err := json.NewDecoder(r).Decode(&raw); err != nil {
		return nil, fmt.Errorf("decoding key set: %w", err)
	}
	set := &jose.JSONWebKeySet{}
	for _, msg := range raw.Keys {
		var key jose.JSONWebKey
		if err := key.UnmarshalJSON(msg); err != nil {
			continue
		}
		if !key.IsPublic() || (key.Use != "" && key.Use != keyUseSignature) {
			continue
		}
		set.Keys = append(set.Keys, key)
	}
	if len(set.Keys) == 0 {
		return nil, errors.New("decoding key set: no usable signing key")
	}
	return set, nil
}
