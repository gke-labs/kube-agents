package authcallout

import (
	"bytes"
	"context"
	"io"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/nats-io/jwt/v2"
	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
)

// The worker pool, measured without a fleet of TLS clients. The full harness
// (integration_test.go) proves the pool against the operator's rendered
// server; what it cannot do is hold the pool to one round after another,
// because its sixty-four clients arrive as their handshakes finish, spread
// over most of a second under the race detector. Here the tests build the
// requests and call decide and handle themselves, with no server and no
// subscription between, so what is measured is decisions and nothing else.

// directCallout is a Service whose responses can be read back: the requests
// are bound to a subscription on a plain embedded server, so handle's
// m.Respond publishes where the test is listening, with no TLS and no CONNECT
// in the way.
type directCallout struct {
	svc       *Service
	serverKP  nkeys.KeyPair
	serverID  string
	sub       *nats.Subscription
	replies   chan *nats.Msg
	logged    *lockedBuffer
	tokens    map[string]Attested
	replyPath string
}

// lockedBuffer is a slog sink many goroutines write and the test reads.
type lockedBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *lockedBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *lockedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func startDirectCallout(t *testing.T, tokens map[string]Attested, onReview func(token string) func()) *directCallout {
	t.Helper()

	opts := &natsserver.Options{Host: "127.0.0.1", Port: -1, NoLog: true, NoSigs: true}
	srv, err := startNatsServer(opts, 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Shutdown)
	nc, err := nats.Connect(srv.ClientURL())
	if err != nil {
		t.Fatalf("connecting to the reply server: %v", err)
	}
	t.Cleanup(nc.Close)
	const replyPath = "callout.replies"
	replies := make(chan *nats.Msg, 256)
	sub, err := nc.ChanSubscribe(replyPath, replies)
	if err != nil {
		t.Fatalf("subscribing for replies: %v", err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	issuerKP, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	issuerSeed, _ := issuerKP.Seed()
	// The requests are signed by a server key, as the real server signs them.
	serverKP, err := nkeys.CreateServer()
	if err != nil {
		t.Fatalf("CreateServer: %v", err)
	}
	serverID, _ := serverKP.PublicKey()

	store := NewStore(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := store.Update([]byte(twoIdentityMap)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}
	validator, err := NewTokenValidator(tokenReviewer(tokens, onReview), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	logged := &lockedBuffer{}
	svc, err := NewService(store, validator, Config{
		IssuerSeed:         string(issuerSeed),
		ReservedPrincipals: []string{svcUser},
		ReservedAddressees: []string{"platform"},
	}, slog.New(slog.NewTextHandler(logged, nil)))
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return &directCallout{svc: svc, serverKP: serverKP, serverID: serverID, sub: sub, replies: replies, logged: logged, tokens: tokens, replyPath: replyPath}
}

// request builds an authorization request for one presented token, shaped
// and signed as the server shapes and signs them.
func (d *directCallout) request(t *testing.T, token string) *nats.Msg {
	t.Helper()
	userKP, err := nkeys.CreateUser()
	if err != nil {
		t.Fatalf("CreateUser: %v", err)
	}
	userPub, _ := userKP.PublicKey()
	rc := jwt.NewAuthorizationRequestClaims(userPub)
	rc.Server.ID = d.serverID
	rc.UserNkey = userPub
	rc.ConnectOptions.Token = token
	rc.ClientInformation.Host = "10.0.0.1"
	encoded, err := rc.Encode(d.serverKP)
	if err != nil {
		t.Fatalf("encoding an authorization request: %v", err)
	}
	return &nats.Msg{Subject: AuthRequestSubject, Reply: d.replyPath, Data: []byte(encoded), Sub: d.sub}
}

// response decodes one reply the callout published.
func response(t *testing.T, m *nats.Msg) *jwt.AuthorizationResponseClaims {
	t.Helper()
	rc, err := jwt.DecodeAuthorizationResponseClaims(string(m.Data))
	if err != nil {
		t.Fatalf("decoding an authorization response: %v", err)
	}
	return rc
}

// The issue's acceptance: sixty-four callbacks with a 50ms TokenReview each,
// every one answered inside a second. Serially that is 3.2s of review; sixteen
// abreast it is four rounds, 200ms. What the pool owes that bound is the four
// rounds, and the rounds are what is counted here: a wall clock over them
// measured the machine's load as much as the pool, since under the race
// detector sixty-four decisions are most of a second of signing on a busy
// runner before any review time is added.
//
// Every review is held, so the test owns each round. A round is full when
// sixteen reviews are held at once; while it is held, the next sixteen
// requests are already behind it and none of them may be reviewed or
// answered. Releasing it must bring sixteen authorizations and then exactly
// the next sixteen reviews. A pool narrower than sixteen never fills a round.
// A wider one lets a waiting request into a full round; when that request
// arrives is the scheduler's choice, so the slot count is read as well, and
// every round's reviews are counted at the end.
//
// The rounds are the pool's share of the second and no more. What one
// decision costs outside its review is not bounded here: the wall clock is
// logged, not asserted.
//
// Each request gets a goroutine of its own, as handle gives it, but not
// handle's decision budget: that is a wall clock from receipt, and a held
// round on a busy runner outlasts it, which is a refusal the pool did not
// cause. TestHandleReturnsBeforeTheDecisionIsMade covers handle itself.
func TestSixtyFourCallbacksAreDecidedInFourRoundsSixteenAbreast(t *testing.T) {
	const callbacks = 64
	// Sixteen abreast: the width is pinned here, so a change to
	// calloutWorkers is a change to this test as well.
	const rounds = 4
	if calloutWorkers*rounds != callbacks {
		t.Fatalf("calloutWorkers = %d, so %d callbacks are not %d full rounds; this test is written for sixteen abreast", calloutWorkers, callbacks, rounds)
	}
	// How long one step (a review starting, an answer arriving) may take
	// before the test gives up on it. Nothing waits this long unless the
	// step is never going to happen.
	const stepWait = 10 * time.Second

	tokens := map[string]Attested{}
	presented := make([]string, callbacks)
	for i := range presented {
		presented[i] = "token-" + strings.Repeat("x", i+1)
		tokens[presented[i]] = Attested{ServiceAccount: agentSA}
	}

	var mu sync.Mutex
	inFlight, peak, reviews := 0, 0, 0
	// gate holds the reviews of the round now filling; the test closes it
	// to release them, after putting the next round's gate in its place.
	// startedIn counts the reviews that began under each round's gate.
	gate := make(chan struct{})
	filling := 0
	startedIn := make([]int, rounds+1)
	started := make(chan struct{}, callbacks)
	// Closed when the test ends, so a review a failed test left held lets
	// its goroutine go.
	testOver := make(chan struct{})
	d := startDirectCallout(t, tokens, func(string) func() {
		mu.Lock()
		inFlight++
		reviews++
		if inFlight > peak {
			peak = inFlight
		}
		held := gate
		startedIn[filling]++
		mu.Unlock()
		started <- struct{}{}
		select {
		case <-held:
		case <-testOver:
		}
		return func() {
			mu.Lock()
			inFlight--
			mu.Unlock()
		}
	})
	t.Cleanup(func() { close(testOver) })
	if got := cap(d.svc.slots); got != calloutWorkers {
		t.Fatalf("the pool has %d slots, want %d", got, calloutWorkers)
	}

	requests := make([]*nats.Msg, callbacks)
	for i := range requests {
		requests[i] = d.request(t, presented[i])
	}
	begun := time.Now()
	for _, m := range requests {
		go d.svc.decide(context.Background(), m)
	}

	for round := 1; round <= rounds; round++ {
		for n := 0; n < calloutWorkers; n++ {
			select {
			case <-started:
			case <-time.After(stepWait):
				t.Fatalf("round %d: %d of %d reviews had started after %v; the pool is narrower than its %d slots", round, n, calloutWorkers, stepWait, calloutWorkers)
			}
		}

		// The round is full and held, with every later request behind it.
		// Nothing is answered while it is.
		select {
		case m := <-d.replies:
			rc := response(t, m)
			t.Fatalf("round %d: a callback was answered (error %q) while all %d reviews of the round were held", round, rc.Error, calloutWorkers)
		default:
		}
		mu.Lock()
		now := inFlight
		held := gate
		gate = make(chan struct{})
		filling++
		mu.Unlock()
		if now != calloutWorkers {
			t.Fatalf("round %d: %d reviews in flight with the round held, want exactly the %d slots", round, now, calloutWorkers)
		}
		close(held)
		for n := 0; n < calloutWorkers; n++ {
			select {
			case m := <-d.replies:
				if rc := response(t, m); rc.Error != "" || rc.Jwt == "" {
					t.Fatalf("round %d: a callback was refused: %q", round, rc.Error)
				}
			case <-time.After(stepWait):
				t.Fatalf("round %d: %d of %d callbacks answered %v after their reviews were released; the rest never were", round, n, calloutWorkers, stepWait)
			}
		}
	}

	elapsed := time.Since(begun)

	mu.Lock()
	defer mu.Unlock()
	// The wall clock is the record of what the decisions cost besides their
	// reviews, which here cost nothing. A loaded runner sets it, so it is
	// not asserted.
	t.Logf("%d callbacks decided in %d rounds, peak %d in flight, %v in all with no review time", callbacks, rounds, peak, elapsed)
	if reviews != callbacks {
		t.Errorf("TokenReviews = %d for %d callbacks; a review ran that no round accounted for", reviews, callbacks)
	}
	// A review that got into a round already full is counted in that round
	// even where the reads above came too early to see it in flight.
	for round := 1; round <= rounds; round++ {
		if got := startedIn[round-1]; got != calloutWorkers {
			t.Errorf("round %d: %d reviews started while it was filling or held, want exactly the %d slots", round, got, calloutWorkers)
		}
	}
	if got := startedIn[rounds]; got != 0 {
		t.Errorf("%d reviews started after the last round was released", got)
	}
	select {
	case m := <-d.replies:
		t.Errorf("a callback was answered (error %q) beyond the %d the rounds read", response(t, m).Error, callbacks)
	default:
	}
	if peak != calloutWorkers {
		t.Errorf("peak concurrent decisions = %d, want exactly the %d slots", peak, calloutWorkers)
	}
}

// handle hands the request on and returns, which is what lets the one
// goroutine that dispatches the subscription's callbacks start the next
// decision while this one is still at its TokenReview. The review here waits
// for handle to have returned. Under a handle that decided inline it would
// wait for ever, so it gives up after handOffWait and says so.
//
// Both budgets are lifted past handOffWait, as serveCallout lifts them, so
// the shipped 1.5s is not a clock this test runs against: a runner that
// stalls between handle's return and the review seeing it is not refused.
func TestHandleReturnsBeforeTheDecisionIsMade(t *testing.T) {
	// How long the review waits for handle to return, and the test for the
	// answer. Nothing waits this long unless it is never going to happen.
	const handOffWait = 10 * time.Second

	returned := make(chan struct{})
	var reviewOutwaitedHandle atomic.Bool
	d := startDirectCallout(t, map[string]Attested{agentToken: {ServiceAccount: agentSA}}, func(string) func() {
		select {
		case <-returned:
		case <-time.After(handOffWait):
			reviewOutwaitedHandle.Store(true)
		}
		return func() {}
	})
	d.svc.decisionBudget = testDialTimeout
	d.svc.validator.reviewBudget = testDialTimeout

	d.svc.handle(d.request(t, agentToken))
	close(returned)
	if reviewOutwaitedHandle.Load() {
		t.Fatalf("handle had not returned %v into its request's TokenReview; it decides inline, and holds the goroutine that dispatches every callback", handOffWait)
	}

	select {
	case m := <-d.replies:
		if rc := response(t, m); rc.Error != "" || rc.Jwt == "" {
			t.Fatalf("the callback was refused: %q", rc.Error)
		}
	case <-time.After(handOffWait):
		t.Fatalf("the callback was not answered %v after handle returned", handOffWait)
	}
}

// A request that reaches the slot wait with no budget left and no slot free
// is refused and answered, not left hanging. In the running callout that is
// a boundary rather than a steady state: no holder keeps a slot past its own
// budget, so a waiter always sees one free inside its window, and the branch
// fires when the budget and a slot come ready at once. It is the floor under
// the pool, and the floor has to hold: every slot taken, the budget gone, one
// refusal on the reply subject with its reason in the log and no TokenReview.
func TestARequestWithNoBudgetLeftAndNoFreeSlotIsRefusedNotHeld(t *testing.T) {
	var reviews atomic.Int64
	d := startDirectCallout(t, map[string]Attested{agentToken: {ServiceAccount: agentSA}}, func(string) func() {
		reviews.Add(1)
		return func() {}
	})
	for i := 0; i < calloutWorkers; i++ {
		d.svc.slots <- struct{}{}
	}
	lapsed, cancel := context.WithDeadline(context.Background(), time.Now().Add(-time.Second))
	defer cancel()

	d.svc.decide(lapsed, d.request(t, agentToken))

	select {
	case m := <-d.replies:
		rc := response(t, m)
		if rc.Error != "authorization timed out" || rc.Jwt != "" {
			t.Fatalf("answered %q with a JWT of %d bytes; want the timed-out refusal and no JWT", rc.Error, len(rc.Jwt))
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the request was never answered; a client would hang to the server's timer")
	}
	if got := reviews.Load(); got != 0 {
		t.Errorf("TokenReviews = %d for a request with no budget left; want none", got)
	}
	if logged := d.logged.String(); !strings.Contains(logged, "the decision budget lapsed before a worker was free") {
		t.Errorf("the refusal was not logged with its reason; log:\n%s", logged)
	}
}

// The suite's harnesses lift the callout's two budgets (serveCallout); what
// ships is whatever the constructors set. Both are authDecisionBudget, the
// bound the server's first-ping timer puts on a decision, and a constructor
// that left either at zero would refuse connections.
func TestTheConstructorsSetTheShippedDecisionBudget(t *testing.T) {
	d := startDirectCallout(t, map[string]Attested{}, nil)
	if d.svc.decisionBudget != authDecisionBudget {
		t.Errorf("NewService's decision budget = %v, want %v", d.svc.decisionBudget, authDecisionBudget)
	}
	if d.svc.validator.reviewBudget != authDecisionBudget {
		t.Errorf("NewTokenValidator's review budget = %v, want %v", d.svc.validator.reviewBudget, authDecisionBudget)
	}
}
