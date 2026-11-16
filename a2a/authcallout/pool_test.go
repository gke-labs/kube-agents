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

// The worker pool, measured at the handler rather than through a fleet of TLS
// clients. The full harness (integration_test.go) proves the pool against the
// operator's rendered server; what it cannot do is time the pool on its own,
// because its sixty-four clients arrive as their handshakes finish, spread
// over most of a second under the race detector. Here the requests are handed
// to handle directly, the way the subscription's one dispatch goroutine does,
// so the clock measures decisions and nothing else.

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
// abreast, four rounds of 50ms. The reviews are held until sixteen are in
// flight, so the peak is the pool's width by construction and the clock runs
// from the pool filling to the last answer.
func TestSixtyFourCallbacksFinishInsideASecondSixteenAbreast(t *testing.T) {
	const callbacks = 64
	const reviewCost = 50 * time.Millisecond
	const deadline = time.Second
	const gateWait = 10 * time.Second

	tokens := map[string]Attested{}
	presented := make([]string, callbacks)
	for i := range presented {
		presented[i] = "token-" + strings.Repeat("x", i+1)
		tokens[presented[i]] = Attested{ServiceAccount: agentSA}
	}

	var mu sync.Mutex
	inFlight, peak, reviews := 0, 0, 0
	poolFull := make(chan struct{})
	poolFilled := false
	d := startDirectCallout(t, tokens, func(string) func() {
		mu.Lock()
		inFlight++
		reviews++
		if inFlight > peak {
			peak = inFlight
		}
		if inFlight == calloutWorkers && !poolFilled {
			poolFilled = true
			close(poolFull)
		}
		mu.Unlock()
		select {
		case <-poolFull:
		case <-time.After(gateWait):
		}
		time.Sleep(reviewCost)
		return func() {
			mu.Lock()
			inFlight--
			mu.Unlock()
		}
	})

	requests := make([]*nats.Msg, callbacks)
	for i := range requests {
		requests[i] = d.request(t, presented[i])
	}
	start := time.Now()
	for _, m := range requests {
		d.svc.handle(m)
	}
	handedOver := time.Since(start)

	answered := 0
	for answered < callbacks {
		select {
		case m := <-d.replies:
			if rc := response(t, m); rc.Error != "" || rc.Jwt == "" {
				t.Fatalf("a callback was refused: %q", rc.Error)
			}
			answered++
		case <-time.After(gateWait):
			t.Fatalf("%d of %d callbacks answered after %v; the rest never were", answered, callbacks, gateWait)
		}
	}
	finished := time.Since(start)

	mu.Lock()
	defer mu.Unlock()
	t.Logf("%d callbacks handed over in %v and all answered in %v, peak %d in flight", callbacks, handedOver, finished, peak)
	if reviews != callbacks {
		t.Fatalf("TokenReviews = %d for %d callbacks; the measurement below is not of what it claims", reviews, callbacks)
	}
	if peak != calloutWorkers {
		t.Errorf("peak concurrent decisions = %d, want exactly the %d slots", peak, calloutWorkers)
	}
	if finished > deadline {
		t.Errorf("%d callbacks at %v of TokenReview each took %v to answer, over the %v the issue's acceptance allows", callbacks, reviewCost, finished, deadline)
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
