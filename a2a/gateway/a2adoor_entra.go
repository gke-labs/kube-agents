package gateway

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
	"regexp"
	"strings"
	"sync"
	"time"
)

// The A2A door's third identity class: a person signed in with Microsoft
// Entra ID. A caller presents the JWT Entra issued it for the install's one
// registered application; the door checks it locally against the tenant's
// published signing keys; the token's object id is the principal. It is
// built on the Google class's shape (a2adoor_google.go) and sits beside it
// and the eval class: a bearer that is the door's static token is the eval
// class as before, a bearer shaped like a Google access token goes to that
// class, and a bearer shaped like a JWT reaches this verifier.
//
// Unlike a Google access token, an Entra token is a signed JWT, so nothing
// is asked of Microsoft per request: the one outbound call is the fetch of
// the tenant's key set, cached and refetched when a token names a key the
// cache does not hold. Nothing here holds a client secret or anyone's
// credential: the door holds a tenant id, a client id and the allowlist the
// verified object id must be on, and the gateway checks the same allowlist
// again before it resolves the principal.
//
// The object id (the oid claim), never the UPN or the display name, is the
// principal: it is immutable for the account's life, while a user principal
// name is renamed. It is the same string the Teams backend reads for the
// same person (from.aadObjectId), so a person's Teams turns and API calls
// share one principal. The preferred_username claim is kept for the ingress
// log and nothing else.

const (
	// a2aEntraBackend names the class in authority blocks and in the
	// gateway's per-backend switches. The door stamps it on what an
	// Entra-verified caller delivers; nothing else may.
	a2aEntraBackend = "a2a-entra"

	// a2aEntraVerifiedBy is the mechanism the authority block records.
	a2aEntraVerifiedBy = "a2a-entra-token"

	// a2aEntraCallerPrefix qualifies a verified caller inside the door, for
	// the reason a2aGoogleCallerPrefix has its leading colon: an eval
	// caller may not contain one, so no eval caller spells this class's
	// task scope or conversation key.
	a2aEntraCallerPrefix = ":entra:"

	// a2aEntraAuthorityHost is where every Entra tenant publishes its
	// issuer, its keys and its OpenID configuration; the tenant id is the
	// path's first segment in each of the three formats below.
	a2aEntraAuthorityHost = "https://login.microsoftonline.com/"
	// a2aEntraIssuerV2Format is the issuer of a v2.0 token.
	a2aEntraIssuerV2Format = a2aEntraAuthorityHost + "%s/v2.0"
	// a2aEntraIssuerV1Host and a2aEntraIssuerV1Format: the issuer of a v1.0
	// access token, which is what an application registration issues for
	// its own scopes unless its manifest asks for v2.0. Both are the same
	// tenant signing with the same keys, so both are accepted.
	a2aEntraIssuerV1Host   = "https://sts.windows.net/"
	a2aEntraIssuerV1Format = a2aEntraIssuerV1Host + "%s/"
	// a2aEntraKeysURLFormat is the tenant's signing-key set (a JWK Set).
	a2aEntraKeysURLFormat = a2aEntraAuthorityHost + "%s/discovery/v2.0/keys"
	// a2aEntraOpenIDConfigFormat is the agent card's scheme URL for the
	// class: the tenant's OpenID configuration, from which a client finds
	// the endpoints it signs in at.
	a2aEntraOpenIDConfigFormat = a2aEntraAuthorityHost + "%s/v2.0/.well-known/openid-configuration"
	// a2aEntraSchemeName names the scheme on the card.
	a2aEntraSchemeName = "entra"

	// a2aEntraAudienceURIPrefix: an application that exposes an API names
	// itself in a token's audience either by its client id or by its
	// application id URI, which by default is the client id under this
	// prefix. Both are the install's one application.
	a2aEntraAudienceURIPrefix = "api://"

	// a2aEntraAlgorithm is the one signing algorithm accepted. Entra signs
	// every token with it; a token naming any other (an HMAC alg with the
	// public key as its secret, or none) is refused before any key is read.
	a2aEntraAlgorithm = "RS256"
	// a2aEntraKeyType is the JWK key type the key set is read for.
	a2aEntraKeyType = "RSA"
	// a2aEntraSegments is how many dot-separated segments a JWS compact
	// serialization has.
	a2aEntraSegments = 3
	// a2aEntraMaxTokenBytes bounds a bearer this class will parse. An Entra
	// token with the usual claims is one to three kilobytes; a bearer past
	// this bound is refused by shape, so a junk bearer costs no decode.
	a2aEntraMaxTokenBytes = 8 << 10

	// a2aEntraClockSkew is the tolerance on exp and nbf: a token is live
	// from nbf minus this to exp plus this. Microsoft's own token
	// validation defaults to five minutes, and a tighter bound would refuse
	// tokens of clients whose clocks are no worse than a cluster node's.
	a2aEntraClockSkew = 5 * time.Minute

	// a2aEntraKeysTimeout bounds one key-set fetch. Past it the token that
	// needed the fetch is not admitted.
	a2aEntraKeysTimeout = 5 * time.Second
	// a2aEntraKeysMaxBytes bounds the key-set read. A tenant's set is a
	// handful of keys, a few kilobytes.
	a2aEntraKeysMaxBytes = 256 << 10
	// a2aEntraKeysTTL is how long a fetched key set is used without being
	// refetched. Entra rotates its keys on a cadence of weeks and keeps the
	// old ones in the set for the overlap, so a day is well inside it.
	a2aEntraKeysTTL = 24 * time.Hour
	// a2aEntraKeysMinRefresh is the least time between two fetches made
	// because a token named a key the set does not hold. A real rotation
	// needs one such fetch; anyone who can reach the door can present
	// tokens naming invented keys, and this is what keeps that from
	// becoming a stream of fetches.
	a2aEntraKeysMinRefresh = time.Minute

	// a2aMaxObjectIDRunes bounds the verified object id: with the prefix it
	// must still fit the caller bound every key segment is held to.
	a2aMaxObjectIDRunes = a2aMaxCallerRunes - len(a2aEntraCallerPrefix)
)

// a2aEntraObjectIDPattern is the shape of an Entra object id: a GUID.
// Pinning the shape keeps the principal, which becomes a key segment, free
// of anything a key cannot carry, and a token whose oid is anything else
// was not minted by Entra for a person.
var a2aEntraObjectIDPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// errEntraTokenRefused is every refusal's root, so a caller can tell a
// verdict on the token from a failure to reach one. The message under it is
// what the client is told; it never carries the token.
var errEntraTokenRefused = errors.New("the Microsoft Entra token was refused")

// entraTokenVerifier checks Entra tokens for one tenant and one application
// against the tenant's published keys, which it caches.
type entraTokenVerifier struct {
	tenantID string
	clientID string
	keysURL  string
	// issuers are the issuer values a token of this tenant may carry.
	issuers []string
	client  *http.Client
	now     func() time.Time

	// keysMu serialises fetches: concurrent tokens naming an unknown key
	// cost one fetch, not one each.
	keysMu sync.Mutex
	keys   map[string]*rsa.PublicKey
	// fetched is when keys was last fetched, successfully or not; it is
	// what a2aEntraKeysTTL and a2aEntraKeysMinRefresh are measured from.
	fetched time.Time
	// fetchErr is the last fetch's failure, kept so a token naming an
	// unknown key can say whether the set is current or unreachable.
	fetchErr error
}

// entraIdentity is what a verified token says about its holder.
type entraIdentity struct {
	// objectID is the principal, as Entra sent it.
	objectID string
	// username is the preferred_username claim, for the log alone.
	username string
}

// entraHeader is the part of the JWS header the door reads.
type entraHeader struct {
	Alg string `json:"alg"`
	Kid string `json:"kid"`
}

// entraClaims is the part of the payload the door reads. aud is a string
// in every token Entra issues, and a JWT may carry it as an array; both
// shapes decode.
type entraClaims struct {
	Iss      string        `json:"iss"`
	Aud      entraAudience `json:"aud"`
	Tid      string        `json:"tid"`
	Oid      string        `json:"oid"`
	Username string        `json:"preferred_username"`
	Exp      int64         `json:"exp"`
	Nbf      int64         `json:"nbf"`
}

type entraAudience []string

func (a *entraAudience) UnmarshalJSON(b []byte) error {
	var one string
	if err := json.Unmarshal(b, &one); err == nil {
		*a = entraAudience{one}
		return nil
	}
	var many []string
	if err := json.Unmarshal(b, &many); err != nil {
		return err
	}
	*a = entraAudience(many)
	return nil
}

// entraKeySet is the JWK Set document, read for its RSA signing keys.
type entraKeySet struct {
	Keys []struct {
		Kty string `json:"kty"`
		Kid string `json:"kid"`
		N   string `json:"n"`
		E   string `json:"e"`
	} `json:"keys"`
}

func newEntraTokenVerifier(tenantID, clientID string) *entraTokenVerifier {
	return &entraTokenVerifier{
		tenantID: tenantID,
		clientID: clientID,
		keysURL:  fmt.Sprintf(a2aEntraKeysURLFormat, tenantID),
		issuers: []string{
			fmt.Sprintf(a2aEntraIssuerV2Format, tenantID),
			fmt.Sprintf(a2aEntraIssuerV1Format, tenantID),
		},
		client: &http.Client{Timeout: a2aEntraKeysTimeout},
		now:    time.Now,
		keys:   map[string]*rsa.PublicKey{},
	}
}

// entraVerifierFor is the verifier for a configured tenant and client, or
// nil when the class is off.
func entraVerifierFor(tenantID, clientID string) *entraTokenVerifier {
	tenantID, clientID = strings.TrimSpace(tenantID), strings.TrimSpace(clientID)
	if tenantID == "" || clientID == "" {
		return nil
	}
	return newEntraTokenVerifier(tenantID, clientID)
}

// entraTokenShaped reports whether a bearer has the shape of a JWS compact
// serialization: three non-empty segments of base64url, within the size
// bound. It is the door's dispatch between the Google class and this one,
// and the pre-filter that keeps a junk bearer from costing a decode.
func entraTokenShaped(token string) bool {
	if len(token) > a2aEntraMaxTokenBytes {
		return false
	}
	segments := strings.Split(token, ".")
	if len(segments) != a2aEntraSegments {
		return false
	}
	for _, s := range segments {
		if s == "" {
			return false
		}
		for _, r := range s {
			if !(r >= 'A' && r <= 'Z' || r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
				return false
			}
		}
	}
	return true
}

// verify returns the identity the token was issued to, or an error saying
// why not. A refusal wraps errEntraTokenRefused; a failure to reach a
// verdict (the key set unreachable and the token's key not cached) does
// not.
func (v *entraTokenVerifier) verify(ctx context.Context, token string) (entraIdentity, error) {
	var id entraIdentity
	if !entraTokenShaped(token) {
		return id, fmt.Errorf("%w: it is not shaped like a signed JWT", errEntraTokenRefused)
	}
	segments := strings.Split(token, ".")
	headerBytes, err := base64.RawURLEncoding.DecodeString(segments[0])
	if err != nil {
		return id, fmt.Errorf("%w: its header does not decode", errEntraTokenRefused)
	}
	var header entraHeader
	if err := json.Unmarshal(headerBytes, &header); err != nil {
		return id, fmt.Errorf("%w: its header does not decode", errEntraTokenRefused)
	}
	// The algorithm is pinned before any key is read: the door never lets
	// the token choose how it is checked.
	if header.Alg != a2aEntraAlgorithm {
		return id, fmt.Errorf("%w: it is not signed with %s", errEntraTokenRefused, a2aEntraAlgorithm)
	}
	if header.Kid == "" {
		return id, fmt.Errorf("%w: it names no signing key", errEntraTokenRefused)
	}
	key, err := v.key(ctx, header.Kid)
	if err != nil {
		return id, err
	}
	signature, err := base64.RawURLEncoding.DecodeString(segments[2])
	if err != nil {
		return id, fmt.Errorf("%w: its signature does not decode", errEntraTokenRefused)
	}
	digest := sha256.Sum256([]byte(segments[0] + "." + segments[1]))
	if err := rsa.VerifyPKCS1v15(key, crypto.SHA256, digest[:], signature); err != nil {
		return id, fmt.Errorf("%w: its signature does not verify against the tenant's key", errEntraTokenRefused)
	}
	payload, err := base64.RawURLEncoding.DecodeString(segments[1])
	if err != nil {
		return id, fmt.Errorf("%w: its claims do not decode", errEntraTokenRefused)
	}
	var claims entraClaims
	if err := json.Unmarshal(payload, &claims); err != nil {
		return id, fmt.Errorf("%w: its claims do not decode", errEntraTokenRefused)
	}
	return v.identity(claims)
}

// identity checks the claims of a token whose signature verified and
// returns the identity they assert.
func (v *entraTokenVerifier) identity(claims entraClaims) (entraIdentity, error) {
	var id entraIdentity
	issued := false
	for _, iss := range v.issuers {
		if claims.Iss == iss {
			issued = true
		}
	}
	if !issued || claims.Tid != v.tenantID {
		return id, fmt.Errorf("%w: it was not issued by this install's tenant", errEntraTokenRefused)
	}
	audience := false
	for _, aud := range claims.Aud {
		if aud == v.clientID || aud == a2aEntraAudienceURIPrefix+v.clientID {
			audience = true
		}
	}
	if !audience {
		return id, fmt.Errorf("%w: it was not issued for this install's application", errEntraTokenRefused)
	}
	now := v.now()
	if claims.Exp == 0 || !time.Unix(claims.Exp, 0).Add(a2aEntraClockSkew).After(now) {
		return id, fmt.Errorf("%w: it has expired", errEntraTokenRefused)
	}
	if claims.Nbf != 0 && time.Unix(claims.Nbf, 0).Add(-a2aEntraClockSkew).After(now) {
		return id, fmt.Errorf("%w: it is not valid yet", errEntraTokenRefused)
	}
	oid := strings.TrimSpace(claims.Oid)
	if err := entraObjectIDWellFormed(oid); err != nil {
		return id, fmt.Errorf("%w: %v", errEntraTokenRefused, err)
	}
	return entraIdentity{objectID: oid, username: strings.TrimSpace(claims.Username)}, nil
}

// entraObjectIDWellFormed holds the object id to the GUID shape, which is
// inside the rules every caller-derived key segment is held to.
func entraObjectIDWellFormed(oid string) error {
	if oid == "" {
		return errors.New("it carries no object id")
	}
	if len(oid) > a2aMaxObjectIDRunes || !a2aEntraObjectIDPattern.MatchString(oid) {
		return errors.New("its object id is not shaped like one")
	}
	return nil
}

// key is the tenant's public key named kid: from the cached set when it
// holds it, else from a fresh fetch, rate-limited so invented key ids cost
// at most one fetch a minute between them. A key a fresh set does not hold
// is a refusal: the token was not signed by this tenant. A key that could
// not be looked for, because the set could not be fetched or was fetched
// too recently to fetch again, is a failure to check, not a refusal.
func (v *entraTokenVerifier) key(ctx context.Context, kid string) (*rsa.PublicKey, error) {
	v.keysMu.Lock()
	defer v.keysMu.Unlock()
	now := v.now()
	age := now.Sub(v.fetched)
	if key, ok := v.keys[kid]; ok && age < a2aEntraKeysTTL {
		return key, nil
	}
	fetchedNow := false
	if age >= a2aEntraKeysMinRefresh {
		v.fetchLocked(ctx, now)
		fetchedNow = true
	}
	if key, ok := v.keys[kid]; ok {
		return key, nil
	}
	if fetchedNow && v.fetchErr == nil {
		return nil, fmt.Errorf("%w: it names a signing key the tenant does not publish", errEntraTokenRefused)
	}
	if v.fetchErr != nil {
		return nil, v.fetchErr
	}
	return nil, errors.New("the Microsoft Entra token could not be checked: it names a signing key the cached set does not hold, and the set was refreshed too recently to refresh again; retry shortly")
}

// fetchLocked replaces the cached set with the tenant's current one. A fetch
// that fails leaves the old set in place, so a transient outage costs
// nothing to tokens signed with a key already held, and records the failure
// for the ones it cannot check.
func (v *entraTokenVerifier) fetchLocked(ctx context.Context, now time.Time) {
	v.fetched = now
	v.fetchErr = nil
	keys, err := v.fetchKeys(ctx)
	if err != nil {
		v.fetchErr = err
		return
	}
	v.keys = keys
}

// fetchKeys reads the tenant's JWK Set and returns its RSA keys by id. A
// failure to read it is said as such, since admitting a caller nobody
// vouched for is the one outcome this must not have.
func (v *entraTokenVerifier) fetchKeys(ctx context.Context) (map[string]*rsa.PublicKey, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, v.keysURL, nil)
	if err != nil {
		return nil, errors.New("the Microsoft Entra token could not be checked")
	}
	resp, err := v.client.Do(req)
	if err != nil {
		// Not err itself: a *url.Error carries transport detail the client
		// has no use for.
		return nil, errors.New("the Microsoft Entra token could not be checked: the tenant's signing keys did not answer")
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, a2aEntraKeysMaxBytes))
	if err != nil {
		return nil, errors.New("the Microsoft Entra token could not be checked: the tenant's signing keys did not arrive whole")
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("the Microsoft Entra token could not be checked: the tenant's signing keys answered HTTP %d", resp.StatusCode)
	}
	var set entraKeySet
	if err := json.Unmarshal(body, &set); err != nil {
		return nil, errors.New("the Microsoft Entra token could not be checked: the tenant's signing keys did not decode")
	}
	keys := map[string]*rsa.PublicKey{}
	for _, k := range set.Keys {
		if k.Kty != a2aEntraKeyType || k.Kid == "" {
			continue
		}
		key, err := entraRSAKey(k.N, k.E)
		if err != nil {
			continue
		}
		keys[k.Kid] = key
	}
	if len(keys) == 0 {
		return nil, errors.New("the Microsoft Entra token could not be checked: the tenant's signing keys hold no RSA key")
	}
	return keys, nil
}

// entraRSAKey builds a public key from a JWK's modulus and exponent.
func entraRSAKey(n, e string) (*rsa.PublicKey, error) {
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
	if !ex.IsInt64() || ex.Int64() > int64(^uint32(0)) || ex.Int64() < 3 {
		return nil, errors.New("exponent out of range")
	}
	return &rsa.PublicKey{N: new(big.Int).SetBytes(modulus), E: int(ex.Int64())}, nil
}

// a2aEntraCaller is the door's caller for a verified object id.
func a2aEntraCaller(objectID string) string {
	return a2aEntraCallerPrefix + objectID
}

// a2aEntraOpenIDConfigURL is the card's scheme URL for the tenant.
func a2aEntraOpenIDConfigURL(tenantID string) string {
	return fmt.Sprintf(a2aEntraOpenIDConfigFormat, tenantID)
}

// identifyEntra is identify's arm for a bearer shaped like a JWT when the
// class is armed: the verified caller if its object id is on the door's
// allowlist, else the refusal. The split of refusals is the Google class's:
// a verdict against the token is a 401 a client answers by signing in again,
// an account off the list is a 403 that signing in again cannot help, and a
// failure to reach a verdict (the tenant's keys unreachable) is a 503.
func (d *A2ADoor) identifyEntra(w http.ResponseWriter, r *http.Request, presented string) (verified string, ok bool) {
	id, err := d.entra.verify(r.Context(), presented)
	if err == nil && !d.entraAllowed[strings.ToLower(id.objectID)] {
		// Checked here, before the door holds anything for the caller, as
		// well as by the gateway (resolveA2AEntraPrincipal), for the reason
		// identify gives on the Google arm. The object id is what the admin
		// adds; the username says who that is.
		d.log.Warn("the A2A door refused an Entra-verified caller who is not on its allowlist", "objectID", id.objectID, "username", id.username)
		injectError(w, http.StatusForbidden, "this Microsoft Entra account is not on the A2A door's allowed users list; an admin adds its object id to A2A_DOOR_ENTRA_ALLOWED_USERS")
		return "", false
	}
	if err != nil {
		d.log.Warn("the A2A door refused a bearer as a Microsoft Entra token", "reason", err.Error())
		if errors.Is(err, errEntraTokenRefused) {
			w.Header().Set("WWW-Authenticate", `Bearer error="invalid_token"`)
			injectError(w, http.StatusUnauthorized, "the bearer is neither the A2A door's token nor a Microsoft Entra token this install accepts: "+err.Error())
		} else {
			injectError(w, http.StatusServiceUnavailable, err.Error())
		}
		return "", false
	}
	return a2aEntraCaller(id.objectID), true
}
