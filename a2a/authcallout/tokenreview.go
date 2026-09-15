package authcallout

import (
	"context"
	"fmt"
	"slices"
	"strings"

	authnv1 "k8s.io/api/authentication/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// TokenValidator turns a client-presented Kubernetes ServiceAccount token into
// the ServiceAccount username the cluster vouches for.
//
// TokenReview against the local API server is the primary path the deployment
// spec names, and it is deliberately the one with no key handling: the cluster
// is already an OIDC issuer with audience-bound, short-lived, kubelet-rotated
// tokens, and revocation is the issuer's problem, which it has already solved.
// Local JWKS verification is the offline alternative the spec keeps for a
// cluster where the callout must not depend on the API server; it is not
// implemented here, and the seam to add it is this type.
type TokenValidator struct {
	client   kubernetes.Interface
	audience string
}

// NewTokenValidator builds a validator bound to one audience.
//
// The audience is not optional and it is not decoration. A TokenReview that
// requests no audience validates the token against the API server's own
// default audience, which every ordinary pod's ServiceAccount token already
// carries — so a callout that omits it would accept any token from any pod in
// the cluster as proof of that pod's identity, and any workload able to read
// another pod's default token could authenticate to the bus as it. Binding to
// a dedicated audience means the only tokens the bus accepts are ones minted
// for the bus, by a projected volume that names it.
func NewTokenValidator(client kubernetes.Interface, audience string) (*TokenValidator, error) {
	if client == nil {
		return nil, fmt.Errorf("token validator needs a Kubernetes client")
	}
	if audience == "" {
		return nil, fmt.Errorf("token validator needs an audience; see NewTokenValidator on why it may not be empty")
	}
	return &TokenValidator{client: client, audience: audience}, nil
}

// Attested is what the cluster vouched for about one presented token.
//
// The pod fields are the API server's, not the client's. When a token is minted
// into a pod's projected volume it carries a bound-object reference, and the
// ServiceAccount token authenticator writes that pod's name and UID onto the
// review as Extra fields. A client cannot set them, and the API server stops
// authenticating the token once the pod object is gone. They are what makes one
// credential distinguishable from another when many pods share a
// ServiceAccount, which is the whole of claim narrowing; see session.go.
type Attested struct {
	// ServiceAccount is the full TokenReview username, in the
	// system:serviceaccount:<namespace>:<name> form the identity map is
	// keyed by.
	ServiceAccount string

	// PodName and PodUID are empty when the token is bound to no pod — a
	// legacy Secret-based token, or one minted with no boundObjectRef. An
	// entry that narrows on the pod refuses such a token rather than
	// falling back to whatever the map holds for it.
	PodName string
	PodUID  string
}

// Validate returns what the cluster vouches for about the token. Every failure
// is an error and no failure returns an identity: a caller that ignores the
// error must not be able to end up with a usable one.
func (v *TokenValidator) Validate(ctx context.Context, token string) (Attested, error) {
	if token == "" {
		return Attested{}, fmt.Errorf("no token presented")
	}

	review, err := v.client.AuthenticationV1().TokenReviews().Create(ctx, &authnv1.TokenReview{
		ObjectMeta: metav1.ObjectMeta{},
		Spec: authnv1.TokenReviewSpec{
			Token:     token,
			Audiences: []string{v.audience},
		},
	}, metav1.CreateOptions{})
	if err != nil {
		// The API server being unreachable is not a refusal — it is the
		// callout being unable to decide. Both end in a refused
		// connection, but they are different operational events and the
		// error text has to let 3 AM tell them apart.
		return Attested{}, fmt.Errorf("TokenReview call failed: %w", err)
	}

	if !review.Status.Authenticated {
		if msg := review.Status.Error; msg != "" {
			return Attested{}, fmt.Errorf("token not authenticated: %s", msg)
		}
		return Attested{}, fmt.Errorf("token not authenticated")
	}

	// Checked rather than assumed. The API server returns the intersection
	// of the requested audiences and the token's own, and an authenticator
	// that ignored spec.Audiences would report Authenticated with an
	// audience set that does not contain ours. That combination is exactly
	// the hole the audience binding exists to close, so it is asserted
	// here rather than inferred from Authenticated alone.
	if !slices.Contains(review.Status.Audiences, v.audience) {
		return Attested{}, fmt.Errorf("token authenticated but not for audience %q (got %v)", v.audience, review.Status.Audiences)
	}

	username := review.Status.User.Username
	if !strings.HasPrefix(username, ServiceAccountPrefix) ||
		len(strings.Split(username, ":")) != serviceAccountFields {
		// A human or a node authenticating to the bus is not something
		// this deployment has a meaning for, and mapping one would mean
		// the map's keys were no longer all the same kind of thing.
		return Attested{}, fmt.Errorf("authenticated as %q, which is not a ServiceAccount", username)
	}

	return Attested{
		ServiceAccount: username,
		PodName:        extraOne(review.Status.User.Extra, extraPodName),
		PodUID:         extraOne(review.Status.User.Extra, extraPodUID),
	}, nil
}

// extraOne reads a single-valued Extra field, and treats anything else as
// absent. Extra is multi-valued by type; the pod fields are written by the
// authenticator as exactly one value each, so a field carrying two of them is
// not something to pick a winner from.
func extraOne(extra map[string]authnv1.ExtraValue, key string) string {
	v, ok := extra[key]
	if !ok || len(v) != 1 {
		return ""
	}
	return v[0]
}
