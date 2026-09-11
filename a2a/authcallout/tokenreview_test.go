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
	if got != gatewaySA {
		t.Errorf("Validate = %q, want %q", got, gatewaySA)
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
				t.Fatalf("Validate returned %q; want a refusal containing %q", got, tc.want)
			}
			if got != "" {
				t.Errorf("Validate returned username %q alongside an error; it must return none", got)
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
