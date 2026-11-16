package authcallout

import (
	"bytes"
	"context"
	"crypto/sha256"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

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

// countingReviewer is reviewer with a tally of the reviews that reached the
// API server, so the tests below can say which presentations were answered
// from memory.
func countingReviewer(t *testing.T, respond func(*authnv1.TokenReview) (*authnv1.TokenReview, error)) (*fake.Clientset, *atomic.Int64) {
	t.Helper()
	var reviews atomic.Int64
	c := reviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		reviews.Add(1)
		return respond(req)
	})
	return c, &reviews
}

// A token presented again inside the TTL is answered from memory: a
// reconnect burst costs one TokenReview per distinct token, not one per
// attempt. The callout logs which, so the source is asserted too.
func TestATokenPresentedTwiceWithinTheTTLIsReviewedOnce(t *testing.T) {
	c, reviews := countingReviewer(t, boundToPod(gatewaySA, "chat-otter-1a2b", "uid-1234", testAudience))
	v, _ := NewTokenValidator(c, testAudience)

	first, source, err := v.validate(context.Background(), "a-token")
	if err != nil || source != reviewFresh {
		t.Fatalf("first presentation = %+v, %q, %v; want a fresh review", first, source, err)
	}
	second, source, err := v.validate(context.Background(), "a-token")
	if err != nil || source != reviewCached {
		t.Fatalf("second presentation = %+v, %q, %v; want the remembered answer", second, source, err)
	}
	if second != first {
		t.Errorf("the remembered answer %+v differs from the reviewed one %+v", second, first)
	}
	if got := reviews.Load(); got != 1 {
		t.Errorf("TokenReviews = %d for two presentations of one token; want 1", got)
	}
}

// The cache key is an HMAC under a key each validator draws for itself, so a
// key says nothing about its token to anyone outside the process: it is not
// the token's plain digest, and two validators key one token differently.
// The entry is still found through it, which the other cache tests assert.
func TestTheCacheKeyIsNotTheTokensPlainDigest(t *testing.T) {
	c, _ := countingReviewer(t, boundToPod(gatewaySA, "chat-otter-1a2b", "uid-1234", testAudience))
	v1, _ := NewTokenValidator(c, testAudience)
	v2, _ := NewTokenValidator(c, testAudience)

	if len(v1.cacheKeyMaterial) != cacheKeyMaterialLen || len(v2.cacheKeyMaterial) != cacheKeyMaterialLen {
		t.Fatalf("key material lengths = %d, %d; want %d", len(v1.cacheKeyMaterial), len(v2.cacheKeyMaterial), cacheKeyMaterialLen)
	}
	if bytes.Equal(v1.cacheKeyMaterial, v2.cacheKeyMaterial) {
		t.Fatal("two validators drew the same key material")
	}
	if v1.cacheKey("a-token") == sha256.Sum256([]byte("a-token")) {
		t.Error("the cache key is the token's plain SHA-256")
	}
	if v1.cacheKey("a-token") == v2.cacheKey("a-token") {
		t.Error("two validators derived the same cache key for one token")
	}
	if v1.cacheKey("a-token") != v1.cacheKey("a-token") {
		t.Error("one validator derived two keys for one token")
	}
}

// Only successes are remembered. A refusal is reviewed again on the next
// presentation, so a token the cluster starts vouching for (or an API server
// that comes back) is not held refused for the TTL.
func TestARefusalIsNotRemembered(t *testing.T) {
	cases := map[string]func(*authnv1.TokenReview) (*authnv1.TokenReview, error){
		"not authenticated": func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
			req.Status = authnv1.TokenReviewStatus{Authenticated: false, Error: "invalid bearer token"}
			return req, nil
		},
		"the wrong audience":   authenticated(gatewaySA, "https://kubernetes.default.svc"),
		"not a ServiceAccount": authenticated("alice@example.com", testAudience),
		"the API server unreachable": func(*authnv1.TokenReview) (*authnv1.TokenReview, error) {
			return nil, errors.New("connection refused")
		},
	}
	for name, respond := range cases {
		t.Run(name, func(t *testing.T) {
			c, reviews := countingReviewer(t, respond)
			v, _ := NewTokenValidator(c, testAudience)
			for i := 0; i < 2; i++ {
				if _, err := v.Validate(context.Background(), "a-token"); err == nil {
					t.Fatalf("presentation %d was accepted", i+1)
				}
			}
			if got := reviews.Load(); got != 2 {
				t.Errorf("TokenReviews = %d for two refused presentations; want 2, a refusal must not be remembered", got)
			}
		})
	}
}

// After the TTL the token is reviewed again: the memory is a bound on the
// next-connection revocation clock, and this is the bound.
func TestARememberedTokenIsReviewedAgainAfterTheTTL(t *testing.T) {
	c, reviews := countingReviewer(t, authenticated(gatewaySA, testAudience))
	v, _ := NewTokenValidator(c, testAudience)
	clock := time.Date(2026, time.November, 2, 1, 0, 0, 0, time.UTC)
	v.now = func() time.Time { return clock }

	for i := 0; i < 2; i++ {
		if _, err := v.Validate(context.Background(), "a-token"); err != nil {
			t.Fatalf("Validate: %v", err)
		}
	}
	clock = clock.Add(reviewCacheTTL - time.Second)
	if _, err := v.Validate(context.Background(), "a-token"); err != nil {
		t.Fatalf("Validate: %v", err)
	}
	if got := reviews.Load(); got != 1 {
		t.Fatalf("TokenReviews = %d inside the TTL; want 1", got)
	}

	clock = clock.Add(time.Second)
	_, source, err := v.validate(context.Background(), "a-token")
	if err != nil || source != reviewFresh {
		t.Fatalf("presentation at the TTL = %q, %v; want a fresh review", source, err)
	}
	if got := reviews.Load(); got != 2 {
		t.Errorf("TokenReviews = %d after the TTL; want 2", got)
	}
}

// Expired entries leave with the next insert, so the map holds at most the
// distinct tokens seen in one TTL rather than every token ever presented.
func TestExpiredEntriesArePrunedOnInsert(t *testing.T) {
	c, _ := countingReviewer(t, authenticated(gatewaySA, testAudience))
	v, _ := NewTokenValidator(c, testAudience)
	clock := time.Date(2026, time.November, 2, 1, 0, 0, 0, time.UTC)
	v.now = func() time.Time { return clock }

	for _, token := range []string{"token-one", "token-two", "token-three"} {
		if _, err := v.Validate(context.Background(), token); err != nil {
			t.Fatalf("Validate(%s): %v", token, err)
		}
	}
	clock = clock.Add(reviewCacheTTL)
	if _, err := v.Validate(context.Background(), "token-four"); err != nil {
		t.Fatalf("Validate: %v", err)
	}
	v.mu.Lock()
	defer v.mu.Unlock()
	if len(v.cache) != 1 {
		t.Errorf("cache holds %d entries after three expired and one was added; want 1", len(v.cache))
	}
}

// Concurrent presentations of one token share one review: the burst a
// reconnecting pod's connections make arrives at the API server as one call.
func TestConcurrentPresentationsOfOneTokenShareOneReview(t *testing.T) {
	const presentations = 32
	var arrived sync.WaitGroup
	arrived.Add(presentations)
	c, reviews := countingReviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		// Hold the one review until every presentation has been made, so
		// the collapse is the subject rather than the memory of a review
		// that finished first.
		arrived.Wait()
		return authenticated(gatewaySA, testAudience)(req)
	})
	v, _ := NewTokenValidator(c, testAudience)

	results := make(chan error, presentations)
	for i := 0; i < presentations; i++ {
		go func() {
			arrived.Done()
			att, err := v.Validate(context.Background(), "a-token")
			if err == nil && att.ServiceAccount != gatewaySA {
				err = errors.New("a shared review answered with " + att.ServiceAccount)
			}
			results <- err
		}()
	}
	for i := 0; i < presentations; i++ {
		if err := <-results; err != nil {
			t.Fatalf("a presentation failed: %v", err)
		}
	}
	if got := reviews.Load(); got != 1 {
		t.Errorf("TokenReviews = %d for %d concurrent presentations of one token; want 1", got, presentations)
	}
}

// A presentation that misses the memory just before a review of its token is
// remembered, and reaches singleflight just after that review's call is gone,
// reads the remembered answer rather than starting a second review. The
// afterMiss hook puts the presentation in that window on purpose: while it is
// held there, another presentation of the same token runs a review to the end
// and is remembered, so the only way the held one can come out with one review
// counted is the second check inside the shared call.
func TestAPresentationThatMissedTheMemoryBeforeAReviewWasRememberedReadsIt(t *testing.T) {
	c, reviews := countingReviewer(t, boundToPod(gatewaySA, "chat-otter-1a2b", "uid-1234", testAudience))
	v, _ := NewTokenValidator(c, testAudience)

	var first Attested
	held := false
	v.afterMiss = func() {
		// Only the presentation under test is held; the one that reviews
		// the token while it waits misses the memory too and must not be
		// held in turn.
		if held {
			return
		}
		held = true
		att, source, err := v.validate(context.Background(), "a-token")
		if err != nil || source != reviewFresh {
			t.Errorf("the review made during the window = %+v, %q, %v; want a fresh review", att, source, err)
		}
		first = att
	}

	att, source, err := v.validate(context.Background(), "a-token")
	if err != nil {
		t.Fatalf("the held presentation failed: %v", err)
	}
	if !held {
		t.Fatal("afterMiss did not run; the presentation never entered the window")
	}
	if source != reviewCached {
		t.Errorf("the held presentation was answered %q; want %q, the review remembered while it waited", source, reviewCached)
	}
	if att != first {
		t.Errorf("the held presentation got %+v; want the remembered %+v", att, first)
	}
	if got := reviews.Load(); got != 1 {
		t.Errorf("TokenReviews = %d for a presentation that joined after its token was remembered; want 1", got)
	}
}

// Distinct tokens are distinct reviews: the memory is keyed by the token, and
// one pod's attestation is never handed to another's presentation.
func TestDistinctTokensAreReviewedSeparately(t *testing.T) {
	c, reviews := countingReviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		return boundToPod(gatewaySA, "pod-for-"+req.Spec.Token, "uid-"+req.Spec.Token, testAudience)(req)
	})
	v, _ := NewTokenValidator(c, testAudience)
	for _, token := range []string{"token-one", "token-two", "token-one", "token-two"} {
		att, err := v.Validate(context.Background(), token)
		if err != nil {
			t.Fatalf("Validate(%s): %v", token, err)
		}
		if att.PodName != "pod-for-"+token {
			t.Errorf("Validate(%s) attested pod %q; want the pod reviewed for that token", token, att.PodName)
		}
	}
	if got := reviews.Load(); got != 2 {
		t.Errorf("TokenReviews = %d for two distinct tokens presented twice each; want 2", got)
	}
}

// A presentation waiting on another's review leaves when its own budget
// runs out, with an error, while the review goes on for the one that started
// it. A waiter that outlived its budget would hold a worker slot past the
// point the client has given up.
func TestAWaiterLeavesWhenItsOwnBudgetRunsOut(t *testing.T) {
	release := make(chan struct{})
	c, reviews := countingReviewer(t, func(req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		<-release
		return authenticated(gatewaySA, testAudience)(req)
	})
	v, _ := NewTokenValidator(c, testAudience)

	first := make(chan error, 1)
	go func() {
		_, err := v.Validate(context.Background(), "a-token")
		first <- err
	}()
	// The second presentation finds the first's review in flight.
	deadline := time.Now().Add(5 * time.Second)
	for reviews.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	att, err := v.Validate(ctx, "a-token")
	if err == nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("the waiter returned %+v, %v; want its own deadline", att, err)
	}
	if att != (Attested{}) {
		t.Errorf("the waiter returned %+v alongside an error; it must return nothing usable", att)
	}

	close(release)
	if err := <-first; err != nil {
		t.Fatalf("the presentation that started the review failed: %v", err)
	}
	if got := reviews.Load(); got != 1 {
		t.Errorf("TokenReviews = %d; want 1, the waiter must not have started another", got)
	}
}

// The presentation that started a review may leave before it ends: its budget
// began at receipt and most of it may have gone on waiting for a worker slot.
// Its leaving must not end the review for the presentations that joined it,
// which still have budget of their own.
func TestTheFirstPresentationLeavingDoesNotFailTheOnesThatJoinedItsReview(t *testing.T) {
	release := make(chan struct{})
	var reviews atomic.Int64
	// client-go gives up a call when its context ends, so the double does
	// too; the fake clientset's reactors never see the context.
	c := clientWithTokenReviews(func(ctx context.Context, req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		reviews.Add(1)
		select {
		case <-release:
			return authenticated(gatewaySA, testAudience)(req)
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	})
	v, _ := NewTokenValidator(c, testAudience)

	firstCtx, cancelFirst := context.WithCancel(context.Background())
	first := make(chan error, 1)
	go func() {
		_, err := v.Validate(firstCtx, "a-token")
		first <- err
	}()
	deadline := time.Now().Add(5 * time.Second)
	for reviews.Load() == 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}

	// The second joins the review in flight, then the first leaves.
	second := make(chan error, 1)
	go func() {
		att, err := v.Validate(context.Background(), "a-token")
		if err == nil && att.ServiceAccount != gatewaySA {
			err = errors.New("the joined review answered with " + att.ServiceAccount)
		}
		second <- err
	}()
	time.Sleep(20 * time.Millisecond)
	cancelFirst()
	if err := <-first; err == nil || !errors.Is(err, context.Canceled) {
		t.Fatalf("the first presentation returned %v; want its own cancellation", err)
	}

	close(release)
	select {
	case err := <-second:
		if err != nil {
			t.Fatalf("the presentation that joined the review failed with it: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the presentation that joined the review never returned")
	}
	if got := reviews.Load(); got != 1 {
		t.Errorf("TokenReviews = %d; want 1, the first leaving must neither end the review nor start another", got)
	}
}
