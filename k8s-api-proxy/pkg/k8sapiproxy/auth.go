package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	authenticationv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apiserver/pkg/authentication/authenticator"
	"k8s.io/apiserver/pkg/authentication/request/bearertoken"
	"k8s.io/apiserver/pkg/authentication/token/cache"
	"k8s.io/apiserver/pkg/authentication/user"
	authenticationv1client "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"
)

const (
	// defaultTokenReviewCacheTTL bounds how long a token stays accepted after it is revoked
	// or expires; kube-apiserver uses the same for delegated authentication.
	defaultTokenReviewCacheTTL = 10 * time.Second
	tokenReviewTimeout         = 10 * time.Second
	// tokenReviewQPS and tokenReviewBurst replace client-go's 5 and 10, which a burst of
	// unknown tokens would exhaust; kube-apiserver's delegated authentication uses the same.
	tokenReviewQPS   = 200
	tokenReviewBurst = 400
)

// errAuthenticationUnavailable is returned when the TokenReview itself fails, so the
// handler answers 503 rather than telling the caller its token is wrong.
var errAuthenticationUnavailable = errors.New("caller authentication is unavailable")

var errNotAuthenticated = errors.New("the caller's token was not accepted")

// TokenReviewConfig configures NewTokenReviewAuthenticator.
type TokenReviewConfig struct {
	// Kubernetes reaches the API server that reviews caller tokens, as the proxy's own
	// identity, which needs create on tokenreviews. Required; rest.InClusterConfig in a pod.
	Kubernetes *rest.Config
	// Audience is the audience a caller's token must be issued for. Set it to one the API
	// server does not accept, so a token sent to the proxy cannot be used against the API
	// server directly. Comma-separated values are split. Either Audience, Audiences, or
	// AudienceCallers is required.
	Audience string
	// Audiences lists additional accepted TokenReview audiences bound to AllowedCallers.
	Audiences []string
	// AllowedCallers are the usernames admitted for Audience / Audiences, such as
	// "system:serviceaccount:<namespace>:<name>".
	AllowedCallers []string
	// AudienceCallers binds additional audiences to their own allowed caller usernames, so a
	// caller admitted for one audience cannot authenticate with another audience's token.
	AudienceCallers map[string][]string
	// CacheTTL is how long a review is reused; defaultTokenReviewCacheTTL when zero.
	CacheTTL time.Duration
}

type tokenReviewAuthenticator struct {
	audiences         authenticator.Audiences
	allowedByAudience map[string]sets.Set[string]
	bearer            authenticator.Request
}

// NewTokenReviewAuthenticator returns a CallerAuthenticator that accepts a bearer token in
// the Authorization header when a TokenReview finds it valid for a configured audience and
// its user is in that audience's allowed caller set. Reviews that accept or refuse the
// token are cached for cfg.CacheTTL; a review that fails is not.
func NewTokenReviewAuthenticator(cfg TokenReviewConfig) (CallerAuthenticator, error) {
	if cfg.Kubernetes == nil {
		return nil, errors.New("k8sapiproxy: TokenReviewConfig.Kubernetes is required")
	}
	var audiences authenticator.Audiences
	seenAudiences := sets.New[string]()
	allowedByAudience := make(map[string]sets.Set[string])
	callerPrimaryAudience := make(map[string]string)

	normalizeCallers := func(callers []string) sets.Set[string] {
		out := sets.New[string]()
		for _, c := range callers {
			if c = strings.TrimSpace(c); c != "" {
				out.Insert(c)
			}
		}
		return out
	}

	primaryCallers := normalizeCallers(cfg.AllowedCallers)
	if primaryCallers.Len() > 0 {
		for _, raw := range append([]string{cfg.Audience}, cfg.Audiences...) {
			for _, entry := range strings.Split(raw, ",") {
				aud := strings.TrimSpace(entry)
				if aud == "" {
					continue
				}
				if !seenAudiences.Has(aud) {
					seenAudiences.Insert(aud)
					audiences = append(audiences, aud)
				}
				allowedByAudience[aud] = primaryCallers.Clone()
			}
		}
		if len(audiences) > 0 {
			firstPrimary := audiences[0]
			for c := range primaryCallers {
				callerPrimaryAudience[c] = firstPrimary
			}
		}
	}

	// Sort keys so validation errors are deterministic.
	extraKeys := make([]string, 0, len(cfg.AudienceCallers))
	for k := range cfg.AudienceCallers {
		extraKeys = append(extraKeys, k)
	}
	for i := 1; i < len(extraKeys); i++ {
		for j := i; j > 0 && extraKeys[j] < extraKeys[j-1]; j-- {
			extraKeys[j], extraKeys[j-1] = extraKeys[j-1], extraKeys[j]
		}
	}
	for _, rawAud := range extraKeys {
		callerSet := normalizeCallers(cfg.AudienceCallers[rawAud])
		if callerSet.Len() == 0 {
			continue
		}
		var groupAudiences []string
		for _, entry := range strings.Split(rawAud, ",") {
			aud := strings.TrimSpace(entry)
			if aud == "" {
				continue
			}
			if seenAudiences.Has(aud) {
				return nil, fmt.Errorf("k8sapiproxy: AudienceCallers audience %q duplicates an already configured audience", aud)
			}
			seenAudiences.Insert(aud)
			audiences = append(audiences, aud)
			allowedByAudience[aud] = callerSet.Clone()
			groupAudiences = append(groupAudiences, aud)
		}
		if len(groupAudiences) == 0 {
			continue
		}
		for _, c := range sets.List(callerSet) {
			if prevAud, exists := callerPrimaryAudience[c]; exists {
				return nil, fmt.Errorf("k8sapiproxy: caller %q cannot be bound to both %q and %q", c, prevAud, groupAudiences[0])
			}
			callerPrimaryAudience[c] = groupAudiences[0]
		}
	}
	if len(audiences) == 0 {
		hasAudience := strings.TrimSpace(cfg.Audience) != ""
		for _, a := range cfg.Audiences {
			if strings.TrimSpace(a) != "" {
				hasAudience = true
			}
		}
		for a := range cfg.AudienceCallers {
			if strings.TrimSpace(a) != "" {
				hasAudience = true
			}
		}
		if !hasAudience {
			return nil, errors.New("k8sapiproxy: TokenReviewConfig.Audience is required")
		}
		return nil, errors.New("k8sapiproxy: TokenReviewConfig.AllowedCallers is required")
	}
	ttl := cfg.CacheTTL
	if ttl <= 0 {
		ttl = defaultTokenReviewCacheTTL
	}

	kc := rest.CopyConfig(cfg.Kubernetes)
	kc.QPS, kc.Burst = tokenReviewQPS, tokenReviewBurst
	client, err := authenticationv1client.NewForConfig(kc)
	if err != nil {
		return nil, fmt.Errorf("k8sapiproxy: TokenReview client: %w", err)
	}
	review := &tokenReviewer{reviews: client.TokenReviews()}
	return &tokenReviewAuthenticator{
		audiences:         audiences,
		allowedByAudience: allowedByAudience,
		bearer:            bearertoken.New(cache.New(review, false, ttl, ttl)),
	}, nil
}

func (a *tokenReviewAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	r = r.WithContext(authenticator.WithAudiences(r.Context(), a.audiences))
	resp, ok, err := a.bearer.AuthenticateRequest(r)
	if errors.Is(err, errAuthenticationUnavailable) {
		return nil, err
	}
	if !ok {
		return nil, errNotAuthenticated
	}
	name := resp.User.GetName()
	allowed := false
	for _, aud := range resp.Audiences {
		if callers, ok := a.allowedByAudience[aud]; ok && callers.Has(name) {
			allowed = true
			break
		}
	}
	if !allowed {
		return nil, fmt.Errorf("caller %q is not allowed", name)
	}
	return &CallerIdentity{Username: name, UID: resp.User.GetUID(), Groups: resp.User.GetGroups()}, nil
}

// tokenReviewer is an authenticator.Token backed by the TokenReview API. A refused token is
// (nil, false, nil), which the cache keeps; a failed review is errAuthenticationUnavailable
// wrapping the cause, which it does not.
type tokenReviewer struct {
	reviews authenticationv1client.TokenReviewInterface
}

func (t *tokenReviewer) AuthenticateToken(ctx context.Context, token string) (*authenticator.Response, bool, error) {
	want, ok := authenticator.AudiencesFrom(ctx)
	if !ok || len(want) == 0 {
		return nil, false, nil
	}
	ctx, cancel := context.WithTimeout(ctx, tokenReviewTimeout)
	defer cancel()
	result, err := t.reviews.Create(ctx, &authenticationv1.TokenReview{
		Spec: authenticationv1.TokenReviewSpec{Token: token, Audiences: want},
	}, metav1.CreateOptions{})
	if err != nil {
		return nil, false, fmt.Errorf("%w: %w", errAuthenticationUnavailable, err)
	}
	got := want.Intersect(result.Status.Audiences)
	if !result.Status.Authenticated || len(got) == 0 {
		return nil, false, nil
	}
	u := result.Status.User
	return &authenticator.Response{
		User:      &user.DefaultInfo{Name: u.Username, UID: u.UID, Groups: u.Groups},
		Audiences: got,
	}, true, nil
}
