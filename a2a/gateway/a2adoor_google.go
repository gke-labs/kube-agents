package gateway

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
)

// The A2A door's developer identity class, Google first: a caller presents
// the OAuth access token Google issued it for the install's one
// pre-registered client (the MCP bridge forwards the one Antigravity
// obtained), the door checks it with Google, and the verified email is the
// principal - as Google sent it, case and all, which is the string the
// Google Chat adapter carries for the same person (it keeps the case for the
// audit join, and so does this). It sits beside the eval class, never instead of it: a request
// whose bearer is the door's static token is the eval class exactly as
// before, and only a bearer that is not reaches this verifier.
//
// A Google access token is opaque, so it is checked through the tokeninfo
// endpoint rather than locally. Nothing here holds a refresh token, a client
// secret or anyone's credential: the door holds a client id and the
// allowlist the verified email must be on, and the gateway checks the same
// allowlist again before it resolves the principal.

const (
	// a2aGoogleBackend names the class in authority blocks and in the
	// gateway's per-backend switches. The door stamps it on what a
	// Google-verified caller delivers; nothing else may.
	a2aGoogleBackend = "a2a-google"

	// a2aGoogleVerifiedBy is the mechanism the authority block records.
	a2aGoogleVerifiedBy = "a2a-google-token"

	// a2aGoogleCallerPrefix qualifies a verified caller inside the door:
	// its task scope, its conversation key and its author id. The leading
	// colon is what makes the class disjoint from the eval class. An eval
	// caller is non-empty and may not contain a colon (callerOf), so it can
	// never equal a string carrying one, and its conversation key
	// (a2aConversationKey) never starts with "a2a::" while this class's
	// always does. "google:" without the leading colon would not do: an eval
	// caller "google" naming the context "<email>:<ctx>" spells the same key.
	a2aGoogleCallerPrefix = ":google:"

	// a2aGoogleTokeninfoURL is Google's token introspection endpoint for
	// access tokens.
	a2aGoogleTokeninfoURL = "https://oauth2.googleapis.com/tokeninfo"
	// a2aGoogleTokeninfoParam carries the token. The URL therefore holds a
	// credential and is never logged.
	a2aGoogleTokeninfoParam = "access_token"
	// a2aGoogleTokeninfoTimeout bounds one check. Past it the request is
	// refused, never admitted.
	a2aGoogleTokeninfoTimeout = 5 * time.Second
	// a2aGoogleTokeninfoMaxBytes bounds the response read.
	a2aGoogleTokeninfoMaxBytes = 64 << 10

	// a2aGoogleCacheTTL caps how long a verified token is trusted without
	// asking again, whatever its own expiry: a token revoked at Google
	// stops working here within this bound.
	a2aGoogleCacheTTL = 5 * time.Minute
	// a2aGoogleCacheBudgetBytes bounds the cache by the bytes behind it.
	// Past it the oldest entries are evicted whole. An entry costs its key,
	// its email and a2aGoogleCacheEntryOverhead.
	a2aGoogleCacheBudgetBytes   = 1 << 20
	a2aGoogleCacheEntryOverhead = 64

	// a2aGoogleSchemeName and a2aGoogleOpenIDConfigURL are the agent
	// card's security scheme for the class: Google's OpenID configuration,
	// from which a client finds the endpoints it signs in at.
	a2aGoogleSchemeName      = "google"
	a2aGoogleOpenIDConfigURL = "https://accounts.google.com/.well-known/openid-configuration"

	// a2aGoogleTokeninfoConcurrency bounds the checks in flight at once.
	// A bearer that is not the static token costs the door a call to
	// Google, and anyone who can reach the door can present one; past the
	// bound the request is refused with a 503 rather than queued.
	a2aGoogleTokeninfoConcurrency = 8

	// a2aMaxEmailRunes bounds the verified email: with the prefix it must
	// still fit the caller bound every key segment is held to.
	a2aMaxEmailRunes = a2aMaxCallerRunes - len(a2aGoogleCallerPrefix)
)

// googleTokenVerifier checks Google access tokens for one client id and
// remembers the ones it admitted, for a bounded time and a bounded size.
// Refusals are never remembered: a token that failed for a transient reason
// is asked about again next time.
type googleTokenVerifier struct {
	clientID     string
	tokeninfoURL string
	client       *http.Client
	now          func() time.Time
	// inflight holds a slot per check in progress (a2aGoogleTokeninfoConcurrency).
	inflight chan struct{}
	// pending is the check in progress for each token key: requests that
	// arrive with a token already being checked wait for that answer
	// rather than spending a slot and a tokeninfo call of their own.
	pendingMu sync.Mutex
	pending   map[string]*googleTokenCheck

	mu    sync.Mutex
	cache map[string]googleTokenEntry
	order []string
	bytes int
}

// googleTokenCheck is one tokeninfo check that several requests may wait on.
type googleTokenCheck struct {
	done  chan struct{}
	email string
	err   error
}

type googleTokenEntry struct {
	email   string
	expires time.Time
	size    int
}

// googleTokeninfo is the part of the tokeninfo response the door reads. The
// endpoint sends numbers and booleans as strings; flexString takes either
// form and keeps a bare number's own text.
type googleTokeninfo struct {
	Aud           flexString `json:"aud"`
	Azp           flexString `json:"azp"`
	Email         flexString `json:"email"`
	EmailVerified flexString `json:"email_verified"`
	Exp           flexString `json:"exp"`
	ExpiresIn     flexString `json:"expires_in"`
}

// flexString decodes a JSON string, number or boolean into its text. A
// number keeps its literal text: decoding it as a float and printing it back
// would turn an exp of 1759800000 into "1.7598e+09", which ParseInt refuses.
type flexString string

func (f *flexString) UnmarshalJSON(b []byte) error {
	var s string
	if err := json.Unmarshal(b, &s); err == nil {
		*f = flexString(s)
		return nil
	}
	var n json.Number
	if err := json.Unmarshal(b, &n); err == nil {
		*f = flexString(n.String())
		return nil
	}
	var v bool
	if err := json.Unmarshal(b, &v); err != nil {
		return err
	}
	*f = flexString(strconv.FormatBool(v))
	return nil
}

func newGoogleTokenVerifier(clientID string) *googleTokenVerifier {
	return &googleTokenVerifier{
		clientID:     clientID,
		tokeninfoURL: a2aGoogleTokeninfoURL,
		client:       &http.Client{Timeout: a2aGoogleTokeninfoTimeout},
		now:          time.Now,
		inflight:     make(chan struct{}, a2aGoogleTokeninfoConcurrency),
		pending:      map[string]*googleTokenCheck{},
		cache:        map[string]googleTokenEntry{},
	}
}

// errGoogleTokenRefused is every refusal's root, so a caller can tell a
// verdict on the token from a failure to reach one. The message under it is
// what the client is told; it never carries the token.
var errGoogleTokenRefused = errors.New("the Google access token was refused")

// verify returns the verified email the token was issued to, as Google
// sent it, or an error saying why not. Concurrent requests with the same
// token share one check: a client that opens with a burst (a bridge fanning
// out tasks/get, parallel sends, a retry storm) costs one tokeninfo call and
// one slot, not one per request.
func (v *googleTokenVerifier) verify(ctx context.Context, token string) (string, error) {
	key := googleTokenKey(token)
	if email, ok := v.cached(key); ok {
		return email, nil
	}
	v.pendingMu.Lock()
	if check, ok := v.pending[key]; ok {
		v.pendingMu.Unlock()
		select {
		case <-check.done:
			return check.email, check.err
		case <-ctx.Done():
			return "", errors.New("the Google access token could not be checked: the request ended while its check was in progress")
		}
	}
	check := &googleTokenCheck{done: make(chan struct{})}
	v.pending[key] = check
	v.pendingMu.Unlock()
	// Not on the first requester's context: others may be waiting on this
	// check, and that one leaving must not fail theirs. The HTTP client's
	// timeout still bounds it.
	check.email, check.err = v.check(context.WithoutCancel(ctx), key, token)
	v.pendingMu.Lock()
	delete(v.pending, key)
	v.pendingMu.Unlock()
	close(check.done)
	return check.email, check.err
}

// check is one tokeninfo check for a token not in the cache, under a slot.
func (v *googleTokenVerifier) check(ctx context.Context, key, token string) (string, error) {
	select {
	case v.inflight <- struct{}{}:
	default:
		return "", errors.New("the Google access token could not be checked: too many checks are in flight; retry shortly")
	}
	info, err := v.tokeninfo(ctx, token)
	<-v.inflight
	if err != nil {
		return "", err
	}
	if string(info.Aud) != v.clientID && string(info.Azp) != v.clientID {
		return "", fmt.Errorf("%w: it was not issued for this install's client", errGoogleTokenRefused)
	}
	if string(info.EmailVerified) != "true" {
		return "", fmt.Errorf("%w: it carries no verified email (request the email scope)", errGoogleTokenRefused)
	}
	email := strings.TrimSpace(string(info.Email))
	if err := googleEmailWellFormed(email); err != nil {
		return "", fmt.Errorf("%w: %v", errGoogleTokenRefused, err)
	}
	now := v.now()
	expires, ok := googleTokenExpiry(info, now)
	if !ok || !expires.After(now) {
		return "", fmt.Errorf("%w: it has expired", errGoogleTokenRefused)
	}
	if limit := now.Add(a2aGoogleCacheTTL); expires.After(limit) {
		expires = limit
	}
	v.remember(key, email, expires)
	return email, nil
}

// tokeninfo asks Google about the token. Any answer but a 200 with a body
// that decodes is a refusal; a failure to ask at all is one too, said as
// such, since admitting a caller nobody vouched for is the one outcome
// this must not have.
func (v *googleTokenVerifier) tokeninfo(ctx context.Context, token string) (googleTokeninfo, error) {
	var info googleTokeninfo
	u := v.tokeninfoURL + "?" + url.Values{a2aGoogleTokeninfoParam: {token}}.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return info, errors.New("the Google access token could not be checked")
	}
	resp, err := v.client.Do(req)
	if err != nil {
		// Not err itself: a *url.Error quotes the URL, and the URL holds
		// the token.
		return info, errors.New("the Google access token could not be checked: Google's tokeninfo endpoint did not answer")
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(io.LimitReader(resp.Body, a2aGoogleTokeninfoMaxBytes))
	if err != nil {
		return info, errors.New("the Google access token could not be checked: the tokeninfo answer did not arrive whole")
	}
	switch {
	case resp.StatusCode == http.StatusBadRequest:
		// Google's answer for a token it does not recognise.
		return info, fmt.Errorf("%w: Google does not recognise it (invalid, expired or revoked)", errGoogleTokenRefused)
	case resp.StatusCode != http.StatusOK:
		// A 5xx or a 429 is Google failing to answer, not a verdict: a
		// client told to sign in again would loop through a sign-in that
		// cannot help.
		return info, fmt.Errorf("the Google access token could not be checked: tokeninfo answered HTTP %d", resp.StatusCode)
	}
	if err := json.Unmarshal(body, &info); err != nil {
		return info, errors.New("the Google access token could not be checked: the tokeninfo answer did not decode")
	}
	return info, nil
}

// googleTokenExpiry reads the token's expiry: exp when Google sent it,
// else now plus expires_in.
func googleTokenExpiry(info googleTokeninfo, now time.Time) (time.Time, bool) {
	if exp, err := strconv.ParseInt(string(info.Exp), 10, 64); err == nil {
		return time.Unix(exp, 0), true
	}
	if in, err := strconv.ParseInt(string(info.ExpiresIn), 10, 64); err == nil {
		return now.Add(time.Duration(in) * time.Second), true
	}
	return time.Time{}, false
}

// googleEmailWellFormed holds the verified email to the rules every
// caller-derived key segment is held to, plus the colon rule the prefix's
// disjointness rests on.
func googleEmailWellFormed(email string) error {
	if email == "" {
		return errors.New("it carries no email")
	}
	if len([]rune(email)) > a2aMaxEmailRunes {
		return fmt.Errorf("its email is longer than %d runes", a2aMaxEmailRunes)
	}
	for _, r := range email {
		if unicode.IsControl(r) || r == ':' {
			return errors.New("its email contains a character the door does not accept")
		}
	}
	return nil
}

// googleTokenKey is the cache key: a digest, so the cache never holds a
// token.
func googleTokenKey(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

func (v *googleTokenVerifier) cached(key string) (string, bool) {
	v.mu.Lock()
	defer v.mu.Unlock()
	e, ok := v.cache[key]
	if !ok {
		return "", false
	}
	if !v.now().Before(e.expires) {
		v.dropLocked(key)
		return "", false
	}
	return e.email, true
}

func (v *googleTokenVerifier) remember(key, email string, expires time.Time) {
	v.mu.Lock()
	defer v.mu.Unlock()
	if _, ok := v.cache[key]; ok {
		v.dropLocked(key)
	}
	size := len(key) + len(email) + a2aGoogleCacheEntryOverhead
	for v.bytes+size > a2aGoogleCacheBudgetBytes && len(v.order) > 0 {
		v.dropLocked(v.order[0])
	}
	v.cache[key] = googleTokenEntry{email: email, expires: expires, size: size}
	v.order = append(v.order, key)
	v.bytes += size
}

func (v *googleTokenVerifier) dropLocked(key string) {
	e, ok := v.cache[key]
	if !ok {
		return
	}
	delete(v.cache, key)
	v.bytes -= e.size
	for i, k := range v.order {
		if k == key {
			v.order = append(v.order[:i], v.order[i+1:]...)
			break
		}
	}
}

// a2aGoogleCaller is the door's caller for a verified email.
func a2aGoogleCaller(email string) string {
	return a2aGoogleCallerPrefix + email
}

// a2aBackendForCaller is the backend a door caller's messages are stamped
// with: the Google class's for a verified caller, the door's own for
// everyone else. One helper, so the send and cancel paths cannot disagree.
func a2aBackendForCaller(caller string) string {
	if strings.HasPrefix(caller, a2aGoogleCallerPrefix) {
		return a2aGoogleBackend
	}
	return a2aBackend
}

// googleAllowlist is the door's copy of the class's allowlist, keyed
// lower-cased; the gateway builds its own from the same config.
func googleAllowlist(users []string) map[string]bool {
	allowed := map[string]bool{}
	for _, u := range users {
		if u = strings.TrimSpace(u); u != "" {
			allowed[strings.ToLower(u)] = true
		}
	}
	return allowed
}

// googleVerifierFor is the verifier for a configured client id, or nil
// when the class is off.
func googleVerifierFor(clientID string) *googleTokenVerifier {
	if clientID = strings.TrimSpace(clientID); clientID == "" {
		return nil
	}
	return newGoogleTokenVerifier(clientID)
}

// bearerOf reads the request's bearer token, if it has one. The doors'
// shared bearer check (bearerAuthorized) reads it the same way.
func bearerOf(r *http.Request) (string, bool) {
	header := r.Header.Get(authorizationHeader)
	if len(header) > len(bearerScheme) && strings.EqualFold(header[:len(bearerScheme)], bearerScheme) {
		if token := strings.TrimSpace(header[len(bearerScheme):]); token != "" {
			return token, true
		}
	}
	return "", false
}

// identify authenticates a request. The door's static token is the eval
// class, as it always was: ok with no verified caller, and each method
// names its caller (callerOf). Any other bearer, when the developer class is
// armed, is checked as a Google access token, and the verified caller is
// returned if its email is on the door's allowlist. Everything else is the
// 401 it was before.
//
// A refusal on the token's merits is a 401 a client answers by signing in
// again; a failure to reach a verdict (Google unreachable, the door's
// check concurrency spent) is a 503, so a client does not loop through a
// sign-in that cannot help.
func (d *A2ADoor) identify(w http.ResponseWriter, r *http.Request) (verified string, ok bool) {
	presented, hasBearer := bearerOf(r)
	if hasBearer && subtle.ConstantTimeCompare([]byte(presented), []byte(d.token)) == 1 {
		return "", true
	}
	if d.google == nil || !hasBearer {
		return "", d.authorized(w, r)
	}
	email, err := d.google.verify(r.Context(), presented)
	if err == nil && !d.googleAllowed[strings.ToLower(email)] {
		// Checked here, before the door holds anything for the caller, as
		// well as by the gateway (resolveA2AGooglePrincipal): an account
		// off the list must not create conversations or submissions that
		// would push an allowed developer's out of the door's bounds. A
		// 403, not a 401: signing in again as the same account cannot help.
		d.log.Warn("the A2A door refused a Google-verified caller who is not on its allowlist")
		injectError(w, http.StatusForbidden, "this Google account is not on the A2A door's allowed users list; an admin adds it to A2A_DOOR_ALLOWED_USERS")
		return "", false
	}
	if err != nil {
		d.log.Warn("the A2A door refused a bearer as a Google access token", "reason", err.Error())
		if errors.Is(err, errGoogleTokenRefused) {
			w.Header().Set("WWW-Authenticate", `Bearer error="invalid_token"`)
			injectError(w, http.StatusUnauthorized, err.Error())
		} else {
			injectError(w, http.StatusServiceUnavailable, err.Error())
		}
		return "", false
	}
	return a2aGoogleCaller(email), true
}

// callerFor names the caller of one method. A verified caller is its
// identity and nothing else: a request that also names a caller is refused
// rather than having the name ignored, so a token never travels with a name
// that disagrees with it. The eval class names itself (callerOf).
func (d *A2ADoor) callerFor(r *http.Request, verified string, metadata map[string]any) (string, *rpcError) {
	if verified == "" {
		return callerOf(r, metadata)
	}
	_, inMetadata := metadata[a2aCallerMetadataKey]
	if strings.TrimSpace(r.Header.Get(a2aCallerHeader)) != "" || inMetadata {
		return "", &rpcError{Code: rpcInvalidParams, Message: fmt.Sprintf(
			"a caller signed in with a Google access token is named by the token: drop the %s header and message.metadata.%s",
			a2aCallerHeader, a2aCallerMetadataKey)}
	}
	return verified, nil
}
