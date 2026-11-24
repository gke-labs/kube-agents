// Package jwtauth is a k8sapiproxy.CallerAuthenticator that admits a caller presenting a
// signed JWT: a bearer token naming its key (kid) whose signature verifies against an
// issuer's JSON Web Key Set, fetched over HTTPS, and whose issuer, audience and expiry
// match. The caller's identity comes from configurable claims, each named by a path into
// the token's claims. The key set's URL is configured or discovered from the issuer's OpenID
// Connect metadata. It suits a platform that
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
	// maxDiscoveryBytes caps the issuer metadata response read into memory.
	maxDiscoveryBytes = 1 << 20
	// maxQuotedIssuerBytes bounds how much of the issuer a metadata document names is quoted
	// in an error: the error is repeated in every audit record until the next fetch.
	maxQuotedIssuerBytes = 256
	// maxBearerTokenBytes caps the Authorization header's bearer token before any of it is
	// parsed, so a caller cannot make the proxy decode an arbitrarily large token. Signed
	// tokens with nested claims stay well under it.
	maxBearerTokenBytes = 16 << 10
	// discoveryPath is where an issuer serves its metadata, relative to the issuer URL
	// (OpenID Connect Discovery 1.0 section 4).
	discoveryPath = "/.well-known/openid-configuration"
	// keySetFlightKey is the one key every fetch shares: an authenticator has one key set.
	keySetFlightKey = "key-set"
	// claimPathSeparator splits a claim path into the keys of nested JSON objects;
	// claimPathEscape before it makes it part of a key.
	claimPathSeparator = '.'
	claimPathEscape    = '\\'
	defaultHTTPSPort   = "443"
	// issuerDelimiters start a query or a fragment, which an issuer used for discovery
	// cannot have (OpenID Connect Discovery 1.0 section 3), even an empty one.
	issuerDelimiters = "?#"
	urlPathSeparator = "/"
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

// claimPathFields names the Config fields that hold claim paths, in the order
// NewAuthenticator parses them, for its errors.
var claimPathFields = []string{"UsernameClaim", "UIDClaim", "GroupsClaim", "PartitionClaim"}

var (
	errNotAuthenticated  = errors.New("the caller's token was not accepted")
	errEmptyClaimPathKey = errors.New("a claim path cannot have an empty key")
)

// Config configures NewAuthenticator.
type Config struct {
	// Issuer is the iss a token must carry. Required.
	Issuer string
	// KeySetURL serves the issuer's JSON Web Key Set, and must be https: anyone who can
	// alter the key set can mint tokens for any caller. When empty, Issuer must be an https
	// URL, and the key set URL is the jwks_uri of the metadata the issuer serves at
	// <Issuer>/.well-known/openid-configuration. That metadata is used only when its issuer
	// equals Issuer exactly and its jwks_uri is https. It is read again on every key set
	// fetch, under the same limits, so a key set the issuer moved is followed.
	KeySetURL string
	// Audiences lists the aud values accepted; a token must carry at least one. Choose ones
	// no other service accepts, so a token minted for the proxy is useless elsewhere.
	// At least one is required.
	Audiences []string
	// UsernameClaim is the path of the string claim used as the caller's username; "sub"
	// when empty. A claim path names a claim by the keys of the JSON objects that hold it,
	// separated by dots: "a.b" reads claims["a"]["b"]. A dot that is part of a key is written
	// "\.", so "example\.io.tenant" reads claims["example.io"]["tenant"]. A token without a
	// non-empty string there is refused.
	UsernameClaim string
	// UIDClaim is the path of the string claim used as the caller's UID. When empty the UID
	// is the token's sub. When set, a token without a non-empty string there is refused.
	UIDClaim string
	// GroupsClaim is the path of a claim holding the caller's groups, a string or a list of
	// strings; optional. A token without the claim has no groups; one holding anything else
	// there is refused.
	GroupsClaim string
	// PartitionClaim is the path of the string claim stored in CallerIdentity.Partition, for
	// a k8sapiproxy.Config.CallerPartition that separates tenants. When set, a token without
	// a non-empty string there is refused: a caller never falls back to the "" partition.
	PartitionClaim string
	// AllowedPartitions, when not empty, lists the PartitionClaim values admitted; a token
	// carrying another is refused. Entries are trimmed and empty ones dropped; a list left
	// with none is an error. It requires PartitionClaim.
	AllowedPartitions []string
	// RequiredSubPrefix, when set, refuses a token whose sub does not start with it.
	RequiredSubPrefix string
	// Allow, when set, is asked whether an authenticated caller is admitted; an error refuses
	// the request with 401. Without it, every token from Issuer carrying one of Audiences is
	// admitted, so leave it nil only when the issuer mints that audience for callers that
	// may all use the proxy.
	Allow func(*k8sapiproxy.CallerIdentity) error
	// SignatureAlgorithms are the algorithms accepted; RS256 and ES256 when empty. Only
	// RS*, PS* and ES* are allowed.
	SignatureAlgorithms []jose.SignatureAlgorithm
	// HTTPClient fetches the key set and the issuer metadata; a client with a 10s timeout
	// when nil. Its redirects are refused unless they stay on https and on the host and port
	// of the URL being fetched.
	HTTPClient *http.Client
	// CAFile, when set, is a PEM bundle of the root CAs the key set's and the metadata's server is verified
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
	issuer    string
	audiences []string
	// The claim paths, split into keys; nil when the claim is not configured.
	usernamePath      []string
	uidPath           []string
	groupsPath        []string
	partitionPath     []string
	allowedPartitions map[string]struct{}
	requiredSubPrefix string
	allow             func(*k8sapiproxy.CallerIdentity) error
	algorithms        []jose.SignatureAlgorithm
	keys              *keySet
	now               func() time.Time
}

// NewAuthenticator returns a CallerAuthenticator that verifies a bearer JWT in the
// Authorization header against cfg. When the key set is needed and cannot be fetched, the
// error wraps k8sapiproxy.ErrAuthenticationUnavailable, so the handler answers 503.
func NewAuthenticator(cfg Config) (k8sapiproxy.CallerAuthenticator, error) {
	if cfg.Issuer == "" {
		return nil, errors.New("jwtauth: Config.Issuer is required")
	}
	var discoveryURL string
	if cfg.KeySetURL == "" {
		u, err := url.Parse(cfg.Issuer)
		if err != nil || !isHTTPS(u) || strings.ContainsAny(cfg.Issuer, issuerDelimiters) {
			return nil, errors.New("jwtauth: Config.KeySetURL is required unless Config.Issuer is an https URL without a query or fragment")
		}
		discoveryURL = strings.TrimSuffix(cfg.Issuer, urlPathSeparator) + discoveryPath
	} else if u, err := url.Parse(cfg.KeySetURL); err != nil || !isHTTPS(u) {
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
	paths := make([][]string, 0, len(claimPathFields))
	for i, claim := range []string{usernameClaim, cfg.UIDClaim, cfg.GroupsClaim, cfg.PartitionClaim} {
		path, err := parseClaimPath(claim)
		if err != nil {
			return nil, fmt.Errorf("jwtauth: Config.%s %q: %w", claimPathFields[i], claim, err)
		}
		paths = append(paths, path)
	}
	usernamePath, uidPath, groupsPath, partitionPath := paths[0], paths[1], paths[2], paths[3]
	var allowedPartitions map[string]struct{}
	for _, partition := range cfg.AllowedPartitions {
		if partition = strings.TrimSpace(partition); partition != "" {
			if allowedPartitions == nil {
				allowedPartitions = map[string]struct{}{}
			}
			allowedPartitions[partition] = struct{}{}
		}
	}
	// A list that names nothing is a mistake, not "admit any partition".
	if len(cfg.AllowedPartitions) > 0 && len(allowedPartitions) == 0 {
		return nil, errors.New("jwtauth: Config.AllowedPartitions has only empty entries")
	}
	if len(allowedPartitions) > 0 && partitionPath == nil {
		return nil, errors.New("jwtauth: Config.AllowedPartitions requires Config.PartitionClaim")
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
		issuer:            cfg.Issuer,
		audiences:         audiences,
		usernamePath:      usernamePath,
		uidPath:           uidPath,
		groupsPath:        groupsPath,
		partitionPath:     partitionPath,
		allowedPartitions: allowedPartitions,
		requiredSubPrefix: cfg.RequiredSubPrefix,
		allow:             cfg.Allow,
		algorithms:        algorithms,
		keys: &keySet{
			url: cfg.KeySetURL, issuer: cfg.Issuer, discoveryURL: discoveryURL,
			client: client, ttl: ttl, now: now,
		},
		now: now,
	}, nil
}

func isHTTPS(u *url.URL) bool {
	return u.Scheme == schemeHTTPS && u.Host != ""
}

// parseClaimPath splits a claim path into the keys it names, on each claimPathSeparator
// that no claimPathEscape precedes; an escaped separator is part of its key, and an escape
// before anything else is itself. An empty path is no path; an empty key is an error.
func parseClaimPath(path string) ([]string, error) {
	if path == "" {
		return nil, nil
	}
	var keys []string
	var key strings.Builder
	for i := 0; i < len(path); i++ {
		switch c := path[i]; {
		case c == claimPathEscape && i+1 < len(path) && path[i+1] == claimPathSeparator:
			key.WriteByte(claimPathSeparator)
			i++
		case c == claimPathSeparator:
			if key.Len() == 0 {
				return nil, errEmptyClaimPathKey
			}
			keys = append(keys, key.String())
			key.Reset()
		default:
			key.WriteByte(c)
		}
	}
	if key.Len() == 0 {
		return nil, errEmptyClaimPathKey
	}
	return append(keys, key.String()), nil
}

// lookupClaim follows path through nested JSON objects; ok is false when a key is absent or
// a value on the way is not an object.
func lookupClaim(claims map[string]any, path []string) (value any, ok bool) {
	value = claims
	for _, key := range path {
		object, isObject := value.(map[string]any)
		if !isObject {
			return nil, false
		}
		if value, ok = object[key]; !ok {
			return nil, false
		}
	}
	return value, true
}

// stringClaim is the string at path, or "" when there is none.
func stringClaim(claims map[string]any, path []string) string {
	value, _ := lookupClaim(claims, path)
	s, _ := value.(string)
	return s
}

// secureRedirectPolicy keeps a key set or metadata fetch on https and on the host and port
// of the URL it set out to fetch across redirects, stops after maxKeySetRedirects as
// net/http's default policy does, and then applies the caller's own policy, if any.
func secureRedirectPolicy(next func(*http.Request, []*http.Request) error) func(*http.Request, []*http.Request) error {
	return func(req *http.Request, via []*http.Request) error {
		if req.URL.Scheme != schemeHTTPS {
			return fmt.Errorf("key set redirect to %s refused: not https", req.URL.Scheme)
		}
		if len(via) > 0 && !sameHostPort(req.URL, via[0].URL) {
			return fmt.Errorf("key set redirect to host %s refused: not the host being fetched", req.URL.Host)
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

// sameHostPort reports whether two https URLs name the same host and port.
func sameHostPort(a, b *url.URL) bool {
	return strings.EqualFold(a.Hostname(), b.Hostname()) && httpsPort(a) == httpsPort(b)
}

func httpsPort(u *url.URL) string {
	if port := u.Port(); port != "" {
		return port
	}
	return defaultHTTPSPort
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
	// Before any of it is decoded.
	if len(raw) > maxBearerTokenBytes {
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
	// Read before the signature is checked, only to turn away another issuer's token without
	// a key set lookup: such a token, for example one meant for the next authenticator in a
	// chain, then neither fetches from this issuer nor waits on it. A token naming this
	// issuer is verified below as before.
	var unverified jwt.Claims
	if err := tok.UnsafeClaimsWithoutVerification(&unverified); err != nil || unverified.Issuer != a.issuer {
		return nil, errNotAuthenticated
	}

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

	if !strings.HasPrefix(claims.Subject, a.requiredSubPrefix) {
		return nil, errNotAuthenticated
	}
	username := stringClaim(extra, a.usernamePath)
	if username == "" {
		return nil, errNotAuthenticated
	}
	id := &k8sapiproxy.CallerIdentity{Username: username, UID: claims.Subject}
	if a.uidPath != nil {
		if id.UID = stringClaim(extra, a.uidPath); id.UID == "" {
			return nil, errNotAuthenticated
		}
	}
	if a.groupsPath != nil {
		value, _ := lookupClaim(extra, a.groupsPath)
		groups, ok := stringsClaim(value)
		if !ok {
			return nil, errNotAuthenticated
		}
		id.Groups = groups
	}
	if a.partitionPath != nil {
		if id.Partition = stringClaim(extra, a.partitionPath); id.Partition == "" {
			return nil, errNotAuthenticated
		}
		if _, allowed := a.allowedPartitions[id.Partition]; len(a.allowedPartitions) > 0 && !allowed {
			return nil, errNotAuthenticated
		}
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
// while it runs, and a request whose caller leaves stops waiting for it. Without a
// configured url, every fetch first reads the key set's URL from the issuer's metadata, as
// part of the same fetch and so under the same limits: the cached key set is what is kept,
// and a key set the issuer moved is found at the next fetch.
type keySet struct {
	// url is the configured key set URL; empty when it is discovered.
	url string
	// issuer is what the metadata at discoveryURL must name as its issuer.
	issuer       string
	discoveryURL string
	client       *http.Client
	ttl          time.Duration
	now          func() time.Time
	flight       singleflight.Group

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

	ch := k.flight.DoChan(keySetFlightKey, func() (any, error) {
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
	target := k.url
	if target == "" {
		var err error
		if target, err = k.discover(ctx); err != nil {
			return nil, err
		}
	}
	return k.fetchFrom(ctx, target)
}

func (k *keySet) fetchFrom(ctx context.Context, target string) (*jose.JSONWebKeySet, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
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

// discover reads the key set URL from the issuer's metadata (OpenID Connect Discovery 1.0
// section 4). The metadata must name the configured issuer exactly (section 4.3), so a
// document served for another issuer cannot supply the keys, and its jwks_uri must be https.
func (k *keySet) discover(ctx context.Context) (string, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, k.discoveryURL, nil)
	if err != nil {
		return "", fmt.Errorf("issuer metadata request: %w", err)
	}
	resp, err := k.client.Do(req)
	if err != nil {
		return "", fmt.Errorf("fetching issuer metadata: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("fetching issuer metadata: status %d", resp.StatusCode)
	}
	var metadata struct {
		Issuer    string `json:"issuer"`
		KeySetURL string `json:"jwks_uri"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxDiscoveryBytes)).Decode(&metadata); err != nil {
		return "", fmt.Errorf("decoding issuer metadata: %w", err)
	}
	if metadata.Issuer != k.issuer {
		named := metadata.Issuer
		if len(named) > maxQuotedIssuerBytes {
			named = named[:maxQuotedIssuerBytes]
		}
		return "", fmt.Errorf("issuer metadata names issuer %q, not the configured %q", named, k.issuer)
	}
	if u, err := url.Parse(metadata.KeySetURL); err != nil || !isHTTPS(u) {
		return "", errors.New("issuer metadata jwks_uri is not an https URL")
	}
	return metadata.KeySetURL, nil
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
