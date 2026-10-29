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
	// server directly. Required.
	Audience string
	// AllowedCallers are the usernames admitted, such as
	// "system:serviceaccount:<namespace>:<name>". Required.
	AllowedCallers []string
	// CacheTTL is how long a review is reused; defaultTokenReviewCacheTTL when zero.
	CacheTTL time.Duration
}

type tokenReviewAuthenticator struct {
	audiences authenticator.Audiences
	allowed   sets.Set[string]
	bearer    authenticator.Request
}

// NewTokenReviewAuthenticator returns a CallerAuthenticator that accepts a bearer token in
// the Authorization header when a TokenReview finds it valid for cfg.Audience and its user
// is in cfg.AllowedCallers. Reviews that accept or refuse the token are cached for
// cfg.CacheTTL; a review that fails is not.
func NewTokenReviewAuthenticator(cfg TokenReviewConfig) (CallerAuthenticator, error) {
	if cfg.Kubernetes == nil {
		return nil, errors.New("k8sapiproxy: TokenReviewConfig.Kubernetes is required")
	}
	audience := strings.TrimSpace(cfg.Audience)
	if audience == "" {
		return nil, errors.New("k8sapiproxy: TokenReviewConfig.Audience is required")
	}
	allowed := sets.New[string]()
	for _, c := range cfg.AllowedCallers {
		if c = strings.TrimSpace(c); c != "" {
			allowed.Insert(c)
		}
	}
	if allowed.Len() == 0 {
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
		audiences: authenticator.Audiences{audience},
		allowed:   allowed,
		bearer:    bearertoken.New(cache.New(review, false, ttl, ttl)),
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
	if !a.allowed.Has(name) {
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
