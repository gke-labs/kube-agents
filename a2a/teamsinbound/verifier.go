package teamsinbound

import (
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"sync"
	"time"
)

// What a Bot Framework token must say, and how long its keys are kept.
const (
	// botFrameworkIssuer is the issuer of every token Bot Framework sends
	// to a bot's messaging endpoint.
	botFrameworkIssuer = "https://api.botframework.com"
	// serviceURLClaim is the claim naming the service the activity came
	// from; it must match the activity's own serviceUrl, or a token minted
	// for one conversation could be replayed against another.
	serviceURLClaim = "serviceurl"
	// signingAlgorithm is the one algorithm accepted. Bot Framework signs
	// with RSA; pinning it before the key is read closes the algorithm
	// confusion family.
	signingAlgorithm = "RS256"
	// keyType is the JWK kty of a key this reads; every other kind is skipped.
	keyType = "RSA"
	// clockSkew is the tolerance on exp and nbf.
	clockSkew = 5 * time.Minute
	// keysTTL is how long a fetched key set is trusted before it is fetched
	// again; Microsoft rotates on a schedule of days.
	keysTTL = 24 * time.Hour
	// keysMinRefresh bounds how often an unknown key id may trigger a fetch,
	// so a stream of junk tokens cannot make the receiver hammer Microsoft.
	keysMinRefresh = time.Minute
	// keysMaxBytes bounds the OpenID configuration and the key set.
	keysMaxBytes = 1 << 20
	// maxTokenBytes bounds a bearer before it is decoded; a Bot Framework
	// token is well under 4 KiB.
	maxTokenBytes = 16 << 10
	// minRSAExponent refuses an exponent of 1 or 2, under which a signature
	// proves nothing.
	minRSAExponent = 3
	// httpTimeout bounds each fetch of the configuration or the key set.
	httpTimeout = 10 * time.Second
	// jwsSegments is the number of dot-separated parts of a compact JWS.
	jwsSegments = 3
)

// ErrRefused is the root of every verdict against a token or an activity: a
// caller can tell "this request is not Microsoft's" from "this could not be
// checked". The message under it is what the client is told; it never
// carries the token.
var ErrRefused = errors.New("the request was refused")

// ErrUnchecked is the root of every failure to reach a verdict: the key set
// could not be read, or was read too recently to read again. The client gets
// a 503 and Microsoft retries.
var ErrUnchecked = errors.New("the request could not be checked")

// Verifier checks Bot Framework tokens for one bot against Microsoft's
// published signing keys, which it caches.
type Verifier struct {
	appID           string
	openIDConfigURL string
	client          *http.Client
	now             func() time.Time

	// keysMu guards the fields below. No fetch runs under it: a fetch in
	// progress is announced through pending, so tokens under a cached key
	// are checked while it runs, and tokens that need it wait for that one
	// rather than starting another.
	keysMu sync.Mutex
	keys   map[string]signingKey
	// fetched is when the last fetch started, successful or not; keysTTL
	// and keysMinRefresh are measured from it.
	fetched time.Time
	// fetchErr is the last fetch's failure.
	fetchErr error
	// everFetched is set by the first successful fetch; readiness reads it.
	everFetched bool
	// pending is closed when the fetch in progress ends; nil when none is.
	pending chan struct{}
}

// signingKey is one published key and the channels Microsoft endorses it
// for. A key endorsed for a list of channels signs tokens for those channels
// only; a token for an msteams activity under a key endorsed for other
// channels is refused.
type signingKey struct {
	key          *rsa.PublicKey
	endorsements []string
}

// NewVerifier makes a verifier for one bot. openIDConfigURL is where the
// configuration naming the key set is read; the default is Bot Framework's.
func NewVerifier(appID, openIDConfigURL string) *Verifier {
	return &Verifier{
		appID:           appID,
		openIDConfigURL: openIDConfigURL,
		client:          &http.Client{Timeout: httpTimeout},
		now:             time.Now,
	}
}

// Ready reports whether a key set has been read at least once, so a request
// arriving now could be checked.
func (v *Verifier) Ready() bool {
	v.keysMu.Lock()
	defer v.keysMu.Unlock()
	return v.everFetched
}

// Warm reads the key set once, ahead of the first request. Failure is not
// fatal: the first request fetches again.
func (v *Verifier) Warm(ctx context.Context) error {
	_, err := v.key(ctx, "")
	if errors.Is(err, ErrRefused) {
		return nil
	}
	return err
}

type tokenHeader struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
}

// audience decodes aud as a string or an array, both of which a JWT may carry.
type audience []string

func (a *audience) UnmarshalJSON(b []byte) error {
	var one string
	if err := json.Unmarshal(b, &one); err == nil {
		*a = audience{one}
		return nil
	}
	var many []string
	if err := json.Unmarshal(b, &many); err != nil {
		return err
	}
	*a = audience(many)
	return nil
}

type tokenClaims struct {
	Iss        string   `json:"iss"`
	Aud        audience `json:"aud"`
	Exp        int64    `json:"exp"`
	Nbf        int64    `json:"nbf"`
	ServiceURL string   `json:"serviceurl"`
}

type openIDConfig struct {
	JWKSURI string `json:"jwks_uri"`
}

type keySet struct {
	Keys []struct {
		Kty          string   `json:"kty"`
		Kid          string   `json:"kid"`
		N            string   `json:"n"`
		E            string   `json:"e"`
		Endorsements []string `json:"endorsements"`
	} `json:"keys"`
}

// Verify checks that token is Microsoft's, for this bot, current, minted for
// the activity's service, and signed by a key endorsed for the activity's
// channel. Every failure is an ErrRefused or an ErrUnchecked.
func (v *Verifier) Verify(ctx context.Context, token string, act activity) error {
	if len(token) > maxTokenBytes {
		return fmt.Errorf("%w: the bearer is larger than any token Microsoft issues", ErrRefused)
	}
	parts := strings.Split(token, ".")
	if len(parts) != jwsSegments || parts[0] == "" || parts[1] == "" || parts[2] == "" {
		return fmt.Errorf("%w: the bearer is not shaped like a signed token", ErrRefused)
	}
	headerJSON, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return fmt.Errorf("%w: the token header does not decode", ErrRefused)
	}
	var header tokenHeader
	if err := json.Unmarshal(headerJSON, &header); err != nil {
		return fmt.Errorf("%w: the token header does not decode", ErrRefused)
	}
	if header.Alg != signingAlgorithm {
		return fmt.Errorf("%w: the token is not signed with %s", ErrRefused, signingAlgorithm)
	}
	if header.Kid == "" {
		return fmt.Errorf("%w: the token names no signing key", ErrRefused)
	}
	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return fmt.Errorf("%w: the token signature does not decode", ErrRefused)
	}
	sk, err := v.key(ctx, header.Kid)
	if err != nil {
		return err
	}
	digest := sha256.Sum256([]byte(parts[0] + "." + parts[1]))
	if err := rsa.VerifyPKCS1v15(sk.key, crypto.SHA256, digest[:], signature); err != nil {
		return fmt.Errorf("%w: the token signature does not verify", ErrRefused)
	}
	payloadJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return fmt.Errorf("%w: the token payload does not decode", ErrRefused)
	}
	var claims tokenClaims
	if err := json.Unmarshal(payloadJSON, &claims); err != nil {
		return fmt.Errorf("%w: the token payload does not decode", ErrRefused)
	}
	if claims.Iss != botFrameworkIssuer {
		return fmt.Errorf("%w: the token was not issued by Bot Framework", ErrRefused)
	}
	if !slices.ContainsFunc(claims.Aud, func(a string) bool { return strings.EqualFold(a, v.appID) }) {
		return fmt.Errorf("%w: the token is not for this bot", ErrRefused)
	}
	now := v.now()
	if claims.Exp == 0 || now.After(time.Unix(claims.Exp, 0).Add(clockSkew)) {
		return fmt.Errorf("%w: the token has expired", ErrRefused)
	}
	if claims.Nbf != 0 && now.Before(time.Unix(claims.Nbf, 0).Add(-clockSkew)) {
		return fmt.Errorf("%w: the token is not yet valid", ErrRefused)
	}
	if !sameServiceURL(claims.ServiceURL, act.ServiceURL) {
		return fmt.Errorf("%w: the token was minted for another service URL than the activity names", ErrRefused)
	}
	if len(sk.endorsements) > 0 && !slices.Contains(sk.endorsements, act.ChannelID) {
		return fmt.Errorf("%w: the signing key is not endorsed for the activity's channel", ErrRefused)
	}
	return nil
}

// sameServiceURL compares the token's service URL with the activity's as
// Bot Framework documents it: equal after a trailing slash is dropped, with
// the scheme and host compared without case. Both must be present.
func sameServiceURL(claimed, named string) bool {
	c, err := url.Parse(strings.TrimSuffix(strings.TrimSpace(claimed), "/"))
	if err != nil || c.Host == "" {
		return false
	}
	n, err := url.Parse(strings.TrimSuffix(strings.TrimSpace(named), "/"))
	if err != nil || n.Host == "" {
		return false
	}
	return strings.EqualFold(c.Scheme, n.Scheme) && strings.EqualFold(c.Host, n.Host) && c.Path == n.Path
}

// key returns the signing key for kid, fetching the set when it is stale or
// does not hold the key, at most once a minute. Callers that need a fetch
// share the one in progress. A key a fresh set does not hold is a refusal;
// a key that could not be looked for is a failure to check. The empty kid
// is Warm's: it fetches and refuses, and Warm drops the refusal.
func (v *Verifier) key(ctx context.Context, kid string) (signingKey, error) {
	v.keysMu.Lock()
	now := v.now()
	age := now.Sub(v.fetched)
	if sk, ok := v.keys[kid]; ok && age < keysTTL {
		v.keysMu.Unlock()
		return sk, nil
	}
	fresh := false
	switch {
	case v.pending != nil:
		done := v.pending
		v.keysMu.Unlock()
		select {
		case <-done:
		case <-ctx.Done():
			return signingKey{}, fmt.Errorf("%w: the request ended while Microsoft's signing keys were being fetched", ErrUnchecked)
		}
		v.keysMu.Lock()
		fresh = true
	case age >= keysMinRefresh || v.fetched.IsZero():
		done := make(chan struct{})
		v.pending = done
		v.fetched = now
		v.keysMu.Unlock()
		// Not on the requester's context: others may be waiting on this
		// fetch, and a client that leaves must not fail theirs. The HTTP
		// client's timeout bounds it. A fetch that fails leaves the old
		// set in place.
		keys, err := v.fetchKeys(context.WithoutCancel(ctx))
		v.keysMu.Lock()
		v.fetchErr = err
		if err == nil {
			v.keys = keys
			v.everFetched = true
		}
		v.pending = nil
		close(done)
		fresh = true
	}
	defer v.keysMu.Unlock()
	if sk, ok := v.keys[kid]; ok {
		return sk, nil
	}
	if fresh && v.fetchErr == nil {
		return signingKey{}, fmt.Errorf("%w: the token names a signing key Microsoft does not publish", ErrRefused)
	}
	if v.fetchErr != nil {
		return signingKey{}, v.fetchErr
	}
	return signingKey{}, fmt.Errorf("%w: the token names a signing key the cached set does not hold, and the set was refreshed too recently to refresh again", ErrUnchecked)
}

// fetchKeys reads the OpenID configuration, then the key set it names, and
// returns the RSA keys by id with their endorsements.
func (v *Verifier) fetchKeys(ctx context.Context) (map[string]signingKey, error) {
	var cfg openIDConfig
	if err := v.getJSON(ctx, v.openIDConfigURL, &cfg, "Microsoft's OpenID configuration"); err != nil {
		return nil, err
	}
	u, err := url.Parse(cfg.JWKSURI)
	if err != nil || u.Scheme != "https" || u.Host == "" {
		return nil, fmt.Errorf("%w: Microsoft's OpenID configuration names no https key set", ErrUnchecked)
	}
	var set keySet
	if err := v.getJSON(ctx, cfg.JWKSURI, &set, "Microsoft's signing keys"); err != nil {
		return nil, err
	}
	keys := map[string]signingKey{}
	for _, k := range set.Keys {
		if k.Kty != keyType || k.Kid == "" {
			continue
		}
		pub, err := rsaKey(k.N, k.E)
		if err != nil {
			continue
		}
		keys[k.Kid] = signingKey{key: pub, endorsements: k.Endorsements}
	}
	if len(keys) == 0 {
		return nil, fmt.Errorf("%w: Microsoft's signing keys hold no RSA key", ErrUnchecked)
	}
	return keys, nil
}

// getJSON reads one bounded JSON document. Transport detail stays out of the
// error: the client has no use for it, and the log line has the URL.
func (v *Verifier) getJSON(ctx context.Context, rawURL string, into any, what string) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return fmt.Errorf("%w: %s could not be requested", ErrUnchecked, what)
	}
	resp, err := v.client.Do(req)
	if err != nil {
		return fmt.Errorf("%w: %s did not answer", ErrUnchecked, what)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, keysMaxBytes))
	if err != nil {
		return fmt.Errorf("%w: %s did not arrive whole", ErrUnchecked, what)
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("%w: %s answered HTTP %d", ErrUnchecked, what, resp.StatusCode)
	}
	if err := json.Unmarshal(body, into); err != nil {
		return fmt.Errorf("%w: %s did not decode", ErrUnchecked, what)
	}
	return nil
}

// rsaKey builds a public key from a JWK's modulus and exponent.
func rsaKey(n, e string) (*rsa.PublicKey, error) {
	modulus, err := base64.RawURLEncoding.DecodeString(n)
	if err != nil {
		return nil, err
	}
	exponent, err := base64.RawURLEncoding.DecodeString(e)
	if err != nil {
		return nil, err
	}
	if len(modulus) == 0 || len(exponent) == 0 {
		return nil, errors.New("empty modulus or exponent")
	}
	ex := new(big.Int).SetBytes(exponent)
	if !ex.IsInt64() || ex.Int64() > int64(^uint32(0)) || ex.Int64() < minRSAExponent {
		return nil, errors.New("exponent out of range")
	}
	return &rsa.PublicKey{N: new(big.Int).SetBytes(modulus), E: int(ex.Int64())}, nil
}
