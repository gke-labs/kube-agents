package k8sapiproxy

import (
	"bytes"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
)

// StaticAuthenticator returns a fixed CallerIdentity for every request.
type StaticAuthenticator struct {
	Identity CallerIdentity
}

// AuthenticateRequest returns the configured CallerIdentity and strips any caller Authorization header.
func (s *StaticAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	if r != nil && r.Header != nil {
		r.Header.Del("Authorization")
	}
	id := s.Identity
	return &id, nil
}

// TokenReviewConfig configures TokenReviewAuthenticator.
type TokenReviewConfig struct {
	// APIServerURL is the base URL of the host Kubernetes API server (for example, "https://kubernetes.default.svc").
	APIServerURL string

	// CAFile is the path to the cluster CA bundle PEM file.
	CAFile string

	// TokenFile is the path to the proxy ServiceAccount token file used to call TokenReview.
	TokenFile string

	// Audience is the required JWT audience in TokenReviewSpec.
	Audience string

	// AllowedCallers is the allowlist of Kubernetes usernames (for example, "system:serviceaccount:ns:sa").
	AllowedCallers []string

	// CacheTTL is the duration for which verified tokens remain cached. Defaults to 60 seconds.
	CacheTTL time.Duration

	// HTTPClient is an optional HTTP client override for tests.
	HTTPClient *http.Client
}

type cachedIdentity struct {
	identity  CallerIdentity
	expiresAt time.Time
}

// TokenReviewAuthenticator validates incoming Bearer tokens via the Kubernetes TokenReview API.
type TokenReviewAuthenticator struct {
	apiServerURL   string
	tokenFile      string
	audience       string
	allowedCallers map[string]struct{}
	cacheTTL       time.Duration
	client         *http.Client

	mu    sync.RWMutex
	cache map[string]cachedIdentity
}

const (
	defaultAPIServerURL         = "https://kubernetes.default.svc"
	defaultServiceAccountCAFile = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	defaultServiceAccountToken  = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	defaultTokenReviewTimeout   = 10 * time.Second
	defaultTokenReviewCacheTTL  = 60 * time.Second
	maxTokenReviewResponseBytes = 1 << 20
	tokenReviewAPIPath          = "/apis/authentication.k8s.io/v1/tokenreviews"
)

// NewTokenReviewAuthenticator constructs a TokenReviewAuthenticator.
func NewTokenReviewAuthenticator(cfg TokenReviewConfig) (*TokenReviewAuthenticator, error) {
	apiServerURL := strings.TrimRight(strings.TrimSpace(cfg.APIServerURL), "/")
	if apiServerURL == "" {
		apiServerURL = defaultAPIServerURL
	}
	tokenFile := strings.TrimSpace(cfg.TokenFile)
	if tokenFile == "" {
		tokenFile = defaultServiceAccountToken
	}
	caFile := strings.TrimSpace(cfg.CAFile)
	if caFile == "" {
		caFile = defaultServiceAccountCAFile
	}

	client := cfg.HTTPClient
	if client == nil {
		tlsCfg := &tls.Config{MinVersion: tls.VersionTLS12}
		if caBytes, err := os.ReadFile(caFile); err == nil && len(caBytes) > 0 {
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(caBytes) {
				return nil, fmt.Errorf("failed to parse CA bundle at %s", caFile)
			}
			tlsCfg.RootCAs = pool
		}
		client = &http.Client{
			Timeout: defaultTokenReviewTimeout,
			Transport: &http.Transport{
				TLSClientConfig: tlsCfg,
			},
		}
	}

	allowed := make(map[string]struct{}, len(cfg.AllowedCallers))
	for _, caller := range cfg.AllowedCallers {
		trimmed := strings.TrimSpace(caller)
		if trimmed != "" {
			allowed[trimmed] = struct{}{}
		}
	}
	if len(allowed) == 0 {
		return nil, errors.New("TokenReviewAuthenticator requires at least one allowed caller ServiceAccount")
	}

	ttl := cfg.CacheTTL
	if ttl <= 0 {
		ttl = defaultTokenReviewCacheTTL
	}

	return &TokenReviewAuthenticator{
		apiServerURL:   apiServerURL,
		tokenFile:      tokenFile,
		audience:       strings.TrimSpace(cfg.Audience),
		allowedCallers: allowed,
		cacheTTL:       ttl,
		client:         client,
		cache:          make(map[string]cachedIdentity),
	}, nil
}

type tokenReviewPayload struct {
	APIVersion string          `json:"apiVersion"`
	Kind       string          `json:"kind"`
	Spec       tokenReviewSpec `json:"spec"`
}

type tokenReviewSpec struct {
	Token     string   `json:"token"`
	Audiences []string `json:"audiences,omitempty"`
}

type tokenReviewResponse struct {
	Status struct {
		Authenticated bool   `json:"authenticated"`
		Error         string `json:"error,omitempty"`
		User          struct {
			Username string   `json:"username"`
			UID      string   `json:"uid"`
			Groups   []string `json:"groups"`
		} `json:"user"`
	} `json:"status"`
}

// AuthenticateRequest extracts the Bearer token from r, strips the Authorization header, and validates it via TokenReview.
func (a *TokenReviewAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	authHeader := strings.TrimSpace(r.Header.Get("Authorization"))
	r.Header.Del("Authorization")
	if authHeader == "" {
		return nil, errors.New("missing Authorization header")
	}
	parts := strings.SplitN(authHeader, " ", 2)
	if len(parts) != 2 || !strings.EqualFold(parts[0], "Bearer") {
		return nil, errors.New("Authorization header must use Bearer scheme")
	}
	callerToken := strings.TrimSpace(parts[1])
	if callerToken == "" {
		return nil, errors.New("empty Bearer token")
	}

	now := time.Now()
	jwtExpiry, hasJWTExpiry := parseJWTExpiry(callerToken)
	if hasJWTExpiry && !now.Before(jwtExpiry) {
		return nil, errors.New("caller token has expired")
	}

	sum := sha256.Sum256([]byte(callerToken))
	cacheKey := hex.EncodeToString(sum[:])

	a.mu.RLock()
	if entry, ok := a.cache[cacheKey]; ok && now.Before(entry.expiresAt) {
		id := entry.identity
		a.mu.RUnlock()
		return &id, nil
	}
	a.mu.RUnlock()

	reviewerTokenBytes, err := os.ReadFile(a.tokenFile)
	if err != nil {
		return nil, fmt.Errorf("failed to read reviewer service account token: %w", err)
	}
	reviewerToken := strings.TrimSpace(string(reviewerTokenBytes))
	if reviewerToken == "" {
		return nil, errors.New("reviewer service account token is empty")
	}

	reqBody := tokenReviewPayload{
		APIVersion: "authentication.k8s.io/v1",
		Kind:       "TokenReview",
		Spec: tokenReviewSpec{
			Token: callerToken,
		},
	}
	if a.audience != "" {
		reqBody.Spec.Audiences = []string{a.audience}
	}

	bodyBytes, err := json.Marshal(reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to encode TokenReview request: %w", err)
	}

	reviewURL := a.apiServerURL + tokenReviewAPIPath
	httpReq, err := http.NewRequestWithContext(r.Context(), http.MethodPost, reviewURL, bytes.NewReader(bodyBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to create TokenReview request: %w", err)
	}
	httpReq.Header.Set("Authorization", "Bearer "+reviewerToken)
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json")

	resp, err := a.client.Do(httpReq)
	if err != nil {
		return nil, fmt.Errorf("TokenReview request failed: %w", err)
	}
	defer resp.Body.Close()

	respBytes, err := io.ReadAll(io.LimitReader(resp.Body, maxTokenReviewResponseBytes))
	if err != nil {
		return nil, fmt.Errorf("failed to read TokenReview response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("TokenReview returned HTTP %d", resp.StatusCode)
	}

	var parsed tokenReviewResponse
	if err := json.Unmarshal(respBytes, &parsed); err != nil {
		return nil, fmt.Errorf("failed to decode TokenReview response: %w", err)
	}
	if !parsed.Status.Authenticated {
		if parsed.Status.Error != "" {
			return nil, fmt.Errorf("caller token is not authenticated: %s", parsed.Status.Error)
		}
		return nil, errors.New("caller token is not authenticated")
	}

	username := strings.TrimSpace(parsed.Status.User.Username)
	if username == "" {
		return nil, errors.New("TokenReview returned an empty username")
	}

	if len(a.allowedCallers) == 0 {
		return nil, errors.New("no allowed callers configured")
	}
	if _, ok := a.allowedCallers[username]; !ok {
		return nil, fmt.Errorf("caller %q is not in the allowed callers list", username)
	}

	identity := CallerIdentity{
		Username: username,
		UID:      parsed.Status.User.UID,
		Groups:   parsed.Status.User.Groups,
	}

	expiresAt := now.Add(a.cacheTTL)
	if hasJWTExpiry && jwtExpiry.Before(expiresAt) {
		expiresAt = jwtExpiry
	}

	a.mu.Lock()
	for k, v := range a.cache {
		if !now.Before(v.expiresAt) {
			delete(a.cache, k)
		}
	}
	a.cache[cacheKey] = cachedIdentity{
		identity:  identity,
		expiresAt: expiresAt,
	}
	a.mu.Unlock()

	return &identity, nil
}

func parseJWTExpiry(token string) (time.Time, bool) {
	segments := strings.Split(token, ".")
	if len(segments) != 3 {
		return time.Time{}, false
	}
	payloadBytes, err := base64.RawURLEncoding.DecodeString(segments[1])
	if err != nil {
		return time.Time{}, false
	}
	var claims struct {
		Exp *int64 `json:"exp"`
	}
	if err := json.Unmarshal(payloadBytes, &claims); err != nil || claims.Exp == nil || *claims.Exp <= 0 {
		return time.Time{}, false
	}
	return time.Unix(*claims.Exp, 0), true
}
