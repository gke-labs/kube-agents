package k8sapiproxy

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"k8s.io/apimachinery/pkg/util/sets"
	"k8s.io/apimachinery/pkg/util/wait"
	"k8s.io/apiserver/pkg/authentication/authenticator"
	"k8s.io/apiserver/pkg/authentication/request/bearertoken"
	"k8s.io/apiserver/pkg/authentication/token/cache"
	"k8s.io/apiserver/plugin/pkg/authenticator/token/webhook"
	authenticationv1client "k8s.io/client-go/kubernetes/typed/authentication/v1"
	"k8s.io/client-go/rest"
)

const (
	// defaultTokenReviewCacheTTL bounds how long a token stays accepted after it is revoked
	// or expires; kube-apiserver uses the same for delegated authentication.
	defaultTokenReviewCacheTTL = 10 * time.Second
	tokenReviewTimeout         = 10 * time.Second
)

// tokenReviewBackoff is kube-apiserver's default retry for delegated authentication.
var tokenReviewBackoff = wait.Backoff{Duration: 500 * time.Millisecond, Factor: 1.5, Jitter: 0.2, Steps: 5}

var errNotAuthenticated = errors.New("the caller's token was not accepted")

// TokenReviewConfig configures NewTokenReviewAuthenticator.
type TokenReviewConfig struct {
	// Kubernetes reaches the API server that reviews caller tokens, as the proxy's own
	// identity, which needs create on tokenreviews. Required; rest.InClusterConfig in a pod.
	Kubernetes *rest.Config
	// Audience is the audience a caller's token must be issued for, distinct from the API
	// server's, so a token the proxy receives cannot be replayed against the API server.
	// Required.
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
// is in cfg.AllowedCallers. Reviews, valid or not, are cached for cfg.CacheTTL.
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

	client, err := authenticationv1client.NewForConfig(cfg.Kubernetes)
	if err != nil {
		return nil, fmt.Errorf("k8sapiproxy: TokenReview client: %w", err)
	}
	// No implicit audiences: a review that names none is refused.
	review, err := webhook.NewFromInterface(client, nil, tokenReviewBackoff, tokenReviewTimeout, webhook.AuthenticatorMetrics{
		RecordRequestTotal:   func(context.Context, string) {},
		RecordRequestLatency: func(context.Context, string, float64) {},
	})
	if err != nil {
		return nil, fmt.Errorf("k8sapiproxy: TokenReview authenticator: %w", err)
	}
	return &tokenReviewAuthenticator{
		audiences: authenticator.Audiences{audience},
		allowed:   allowed,
		bearer:    bearertoken.New(cache.New(review, false, ttl, ttl)),
	}, nil
}

func (a *tokenReviewAuthenticator) AuthenticateRequest(r *http.Request) (*CallerIdentity, error) {
	// The review checks audiences only when the context names them.
	r = r.WithContext(authenticator.WithAudiences(r.Context(), a.audiences))
	resp, ok, err := a.bearer.AuthenticateRequest(r)
	if err != nil {
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
