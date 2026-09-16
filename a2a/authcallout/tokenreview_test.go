package authcallout

import (
	"context"
	"errors"
	"strings"
	"testing"

	authnv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

const (
	testAudience = "nats"
	gatewaySA    = "system:serviceaccount:kubeagents-system:agent-a2a-gateway"
)

// reviewer installs a reactor standing in for the API server's TokenReview
// endpoint. The reactor sees the real request the validator built, so the
// assertions below are about what the validator sends as much as what it does
// with the answer.
func reviewer(t *testing.T, respond func(*authnv1.TokenReview) (*authnv1.TokenReview, error)) *fake.Clientset {
	t.Helper()
	c := fake.NewSimpleClientset()
	c.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		req, ok := action.(k8stesting.CreateAction).GetObject().(*authnv1.TokenReview)
		if !ok {
			t.Fatalf("TokenReview reactor got %T", action.(k8stesting.CreateAction).GetObject())
		}
		out, err := respond(req)
		return true, out, err
	})
	return c
}

func authenticated(username string, audiences ...string) func(*authnv1.TokenReview) (*authnv1.TokenReview, error) {
	return func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		req.Status = authnv1.TokenReviewStatus{
			Authenticated: true,
			User:          authnv1.UserInfo{Username: username},
			Audiences:     audiences,
		}
		return req, nil
	}
}

// boundToPod answers as the API server does for a projected token minted into
// a pod's volume: authenticated, plus the pod's name and UID in Extra. The key
// strings are written out in full here rather than taken from the constants
// the code under test uses, so that a typo in either is a failing test rather
// than a matched pair of typos.
func boundToPod(username, pod, uid string, audiences ...string) func(*authnv1.TokenReview) (*authnv1.TokenReview, error) {
	return func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		req.Status = authnv1.TokenReviewStatus{
			Authenticated: true,
			Audiences:     audiences,
			User: authnv1.UserInfo{
				Username: username,
				Extra: map[string]authnv1.ExtraValue{
					"authentication.kubernetes.io/pod-name": {pod},
					"authentication.kubernetes.io/pod-uid":  {uid},
				},
			},
		}
		return req, nil
	}
}

func TestValidateReturnsTheServiceAccountTheClusterVouchesFor(t *testing.T) {
	c := reviewer(t, authenticated(gatewaySA, testAudience))
	v, err := NewTokenValidator(c, testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	got, err := v.Validate(context.Background(), "a-token")
	if err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if got.ServiceAccount != gatewaySA {
		t.Errorf("Validate = %q, want %q", got.ServiceAccount, gatewaySA)
	}
}

// The pod claim, which is what claim narrowing stands on. It is the API
// server's word about which pod the token was minted into, and the callout
// derives a session's entire grant set from it, so it is asserted to survive
// the review rather than assumed to.
func TestValidateCarriesThePodTheTokenIsBoundTo(t *testing.T) {
	c := reviewer(t, boundToPod(gatewaySA, "chat-otter-1a2b", "uid-1234", testAudience))
	v, _ := NewTokenValidator(c, testAudience)
	got, err := v.Validate(context.Background(), "a-token")
	if err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if got.PodName != "chat-otter-1a2b" || got.PodUID != "uid-1234" {
		t.Errorf("Validate = %+v, want pod chat-otter-1a2b/uid-1234", got)
	}
}

// A token bound to no pod authenticates perfectly well — it is what every
// non-projected ServiceAccount token is. It must simply report no pod, so that
// a narrowed entry refuses it instead of deriving grants from an empty name.
func TestValidateReportsNoPodForATokenBoundToNone(t *testing.T) {
	c := reviewer(t, authenticated(gatewaySA, testAudience))
	v, _ := NewTokenValidator(c, testAudience)
	got, err := v.Validate(context.Background(), "a-token")
	if err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if got.PodName != "" || got.PodUID != "" {
		t.Errorf("Validate = %+v, want no pod at all", got)
	}
}

// Extra is multi-valued by type. The authenticator writes exactly one value
// per pod field, so two of them is not a set to pick a winner from — it is a
// shape this code does not understand, and understanding it wrongly would mean
// minting a session's grants for whichever pod name sorted first.
func TestValidateTreatsAMultiValuedPodClaimAsNoClaim(t *testing.T) {
	c := reviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		req.Status = authnv1.TokenReviewStatus{
			Authenticated: true,
			Audiences:     []string{testAudience},
			User: authnv1.UserInfo{
				Username: gatewaySA,
				Extra: map[string]authnv1.ExtraValue{
					"authentication.kubernetes.io/pod-name": {"chat-otter-1a2b", "chat-badger-9f9f"},
					"authentication.kubernetes.io/pod-uid":  {"uid-1234"},
				},
			},
		}
		return req, nil
	})
	v, _ := NewTokenValidator(c, testAudience)
	got, err := v.Validate(context.Background(), "a-token")
	if err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if got.PodName != "" {
		t.Errorf("pod name = %q from a two-valued claim; want none", got.PodName)
	}
}

// The token and the audience the validator sends are the whole security
// contract with the API server, so they are asserted on the request itself
// rather than inferred from a successful answer.
func TestValidateRequestsTheBoundAudienceAndThePresentedToken(t *testing.T) {
	var seen *authnv1.TokenReview
	c := reviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		seen = req.DeepCopy()
		return authenticated(gatewaySA, testAudience)(req)
	})
	v, _ := NewTokenValidator(c, testAudience)
	if _, err := v.Validate(context.Background(), "the-exact-token"); err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if seen.Spec.Token != "the-exact-token" {
		t.Errorf("token sent = %q, want the-exact-token", seen.Spec.Token)
	}
	if len(seen.Spec.Audiences) != 1 || seen.Spec.Audiences[0] != testAudience {
		t.Errorf("audiences sent = %v, want [%s]", seen.Spec.Audiences, testAudience)
	}
}

func TestValidateRefuses(t *testing.T) {
	cases := []struct {
		name    string
		respond func(*authnv1.TokenReview) (*authnv1.TokenReview, error)
		want    string
	}{
		{
			name: "a token the cluster does not authenticate",
			respond: func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
				req.Status = authnv1.TokenReviewStatus{Authenticated: false, Error: "invalid bearer token"}
				return req, nil
			},
			want: "invalid bearer token",
		},
		{
			// The hole the audience binding closes: an ordinary pod's
			// default ServiceAccount token authenticates fine, for the
			// API server rather than for the bus. Accepting it would
			// make every readable token in the cluster a bus
			// credential.
			name:    "a token minted for a different audience",
			respond: authenticated(gatewaySA, "https://kubernetes.default.svc"),
			want:    "not for audience",
		},
		{
			name:    "an authenticated token with no audience at all",
			respond: authenticated(gatewaySA),
			want:    "not for audience",
		},
		{
			name:    "a human rather than a ServiceAccount",
			respond: authenticated("alice@example.com", testAudience),
			want:    "not a ServiceAccount",
		},
		{
			name:    "a node identity",
			respond: authenticated("system:node:gke-pool-1", testAudience),
			want:    "not a ServiceAccount",
		},
		{
			name: "the API server being unreachable",
			respond: func(*authnv1.TokenReview) (*authnv1.TokenReview, error) {
				return nil, errors.New("connection refused")
			},
			want: "TokenReview call failed",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			v, _ := NewTokenValidator(reviewer(t, tc.respond), testAudience)
			got, err := v.Validate(context.Background(), "a-token")
			if err == nil {
				t.Fatalf("Validate returned %+v; want a refusal containing %q", got, tc.want)
			}
			if got != (Attested{}) {
				t.Errorf("Validate returned %+v alongside an error; it must return nothing usable", got)
			}
			if !strings.Contains(err.Error(), tc.want) {
				t.Errorf("error = %v, want it to contain %q", err, tc.want)
			}
		})
	}
}

func TestValidateRefusesAnEmptyTokenWithoutCallingTheAPIServer(t *testing.T) {
	called := false
	c := reviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		called = true
		return authenticated(gatewaySA, testAudience)(req)
	})
	v, _ := NewTokenValidator(c, testAudience)
	if _, err := v.Validate(context.Background(), ""); err == nil {
		t.Fatal("an empty token was accepted")
	}
	if called {
		t.Error("an empty token reached the API server; it should be refused before the call")
	}
}

func TestNewTokenValidatorRefusesAnUnboundAudience(t *testing.T) {
	if _, err := NewTokenValidator(fake.NewSimpleClientset(), ""); err == nil {
		t.Fatal("a validator with no audience was built; that would accept every token in the cluster")
	}
}
