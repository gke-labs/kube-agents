// Package jwtauth is a k8sapiproxy.CallerAuthenticator that admits a caller presenting a
// signed JWT: a bearer token naming its key (kid) whose signature verifies against an
// issuer's JSON Web Key Set, whose issuer and audience match, and which carries an expiry it
// has not passed. The caller's identity comes from configurable claims. It suits a platform that mints a token per sandbox, such as an
// agent runtime that injects an actor token at its egress gateway.
package jwtauth

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	"golang.org/x/sync/singleflight"

	"github.com/gke-labs/kube-agents/k8s-api-proxy/pkg/k8sapiproxy"
)

const (
	// defaultUsernameClaim names the caller after the token's subject.
	defaultUsernameClaim = "sub"
	// defaultKeySetTTL is how long a fetched key set is used before it is fetched again.
	defaultKeySetTTL = time.Hour
	// minKeySetRefetchInterval bounds how often a token signed by an unknown key can make the
	// authenticator fetch the key set, so a flood of such tokens cannot flood the issuer.
	minKeySetRefetchInterval = 10 * time.Second
	keySetFetchTimeout       = 10 * time.Second
	// maxKeySetBytes caps the key set response read into memory.
	maxKeySetBytes = 1 << 20
	// clockLeeway tolerates clock skew between the issuer and the proxy on exp, nbf and iat.
	clockLeeway         = jwt.DefaultLeeway
	bearerPrefix        = "Bearer "
	headerAuthorization = "Authorization"
)

// defaultSignatureAlgorithms are the asymmetric algorithms a token may be signed with.
// Symmetric (HS*) algorithms and "none" are never accepted.
var defaultSignatureAlgorithms = []jose.SignatureAlgorithm{jose.RS256, jose.ES256}

var errNotAuthenticated = errors.New("the caller's token was not accepted")

// Config configures NewAuthenticator.
type Config struct {
	// Issuer is the iss a token must carry. Required.
	Issuer string
	// KeySetURL serves the issuer's JSON Web Key Set over HTTPS. Required.
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
	// the request with 401.
	Allow func(*k8sapiproxy.CallerIdentity) error
	// SignatureAlgorithms are the algorithms accepted; RS256 and ES256 when empty. Only
	// asymmetric algorithms are allowed.
	SignatureAlgorithms []jose.SignatureAlgorithm
	// HTTPClient fetches the key set; a client with a 10s timeout when nil.
	HTTPClient *http.Client
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
// Authorization header against cfg. A key set that cannot be fetched when a token needs it
// is reported as k8sapiproxy.ErrAuthenticationUnavailable, so the handler answers 503.
func NewAuthenticator(cfg Config) (k8sapiproxy.CallerAuthenticator, error) {
	if cfg.Issuer == "" {
		return nil, errors.New("jwtauth: Config.Issuer is required")
	}
	if !strings.HasPrefix(cfg.KeySetURL, "https://") && !strings.HasPrefix(cfg.KeySetURL, "http://") {
		return nil, errors.New("jwtauth: Config.KeySetURL must be an http(s) URL")
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
		if isSymmetric(alg) {
			return nil, fmt.Errorf("jwtauth: signature algorithm %s is symmetric; only asymmetric algorithms are allowed", alg)
		}
	}
	usernameClaim := cfg.UsernameClaim
	if usernameClaim == "" {
		usernameClaim = defaultUsernameClaim
	}
	client := cfg.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: keySetFetchTimeout}
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

func isSymmetric(alg jose.SignatureAlgorithm) bool {
	switch alg {
	case jose.HS256, jose.HS384, jose.HS512:
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
	// A token must name its key: a key set is matched by kid, never tried key by key.
	if len(tok.Headers) == 0 || tok.Headers[0].KeyID == "" {
		return nil, errNotAuthenticated
	}
	kid := tok.Headers[0].KeyID

	set, err := a.keys.get(r.Context(), kid)
	if err != nil {
		return nil, err
	}
	var claims jwt.Claims
	var extra map[string]any
	if err := tok.Claims(set, &claims, &extra); err != nil {
		return nil, errNotAuthenticated
	}
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
// triggers a fetch, at most once per minKeySetRefetchInterval, so a rotated key is picked
// up without a restart. Concurrent requests share one fetch, and none holds the lock while
// it runs, so a slow issuer delays only the requests that need a new key.
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
}

// get returns a key set to verify a token signed with kid. It fails with
// k8sapiproxy.ErrAuthenticationUnavailable only when no cached set holds kid and the set
// cannot be fetched; otherwise the signature check decides.
func (k *keySet) get(ctx context.Context, kid string) (*jose.JSONWebKeySet, error) {
	k.mu.Lock()
	now := k.now()
	cached := k.set
	hasKid := cached != nil && len(cached.Key(kid)) > 0
	fresh := cached != nil && now.Sub(k.fetchedAt) < k.ttl
	recentlyTried := cached != nil && now.Sub(k.attemptedAt) < minKeySetRefetchInterval
	k.mu.Unlock()
	if (fresh && hasKid) || recentlyTried {
		// A set that holds kid, or one fetched or tried a moment ago: use it rather than ask again.
		return cached, nil
	}

	v, err, _ := k.flight.Do(k.url, func() (any, error) {
		k.mu.Lock()
		k.attemptedAt = k.now()
		k.mu.Unlock()
		// The fetch outlives a caller that gives up, so the requests sharing it still get keys.
		set, err := k.fetch(context.WithoutCancel(ctx))
		if err != nil {
			return nil, err
		}
		k.mu.Lock()
		k.set, k.fetchedAt = set, k.now()
		k.mu.Unlock()
		return set, nil
	})
	if err != nil {
		if hasKid {
			// The issuer is unreachable but the key is known: keep verifying with it.
			return cached, nil
		}
		return nil, fmt.Errorf("%w: %w", k8sapiproxy.ErrAuthenticationUnavailable, err)
	}
	return v.(*jose.JSONWebKeySet), nil
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
	var set jose.JSONWebKeySet
	if err := json.NewDecoder(io.LimitReader(resp.Body, maxKeySetBytes)).Decode(&set); err != nil {
		return nil, fmt.Errorf("decoding key set: %w", err)
	}
	return &set, nil
}
