package authcallout

import (
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	natsserver "github.com/nats-io/nats-server/v2/server"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nkeys"
	authnv1 "k8s.io/api/authentication/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

// The DoD, proven against a server rather than read out of a config.
//
// Everything below runs a real nats-server with a real auth_callout block and
// the real callout service answering it. Only the Kubernetes API is stubbed,
// and it is stubbed at the TokenReview seam: the thing under test is which
// grants an identity receives and whether the server enforces them, not whether
// the API server can validate a JWT.
//
// The house rule this exists for: an authorization test that asserts on config
// vocabulary passes against a server with real holes in it. So every assertion
// here is an observed client outcome — a connection refused, a publish refused,
// a subject that worked.

const (
	// The callout's own user and password as the operator renders them.
	svcUser = "callout"
	svcPass = "pw-callout"

	// The two identities the DoD needs to tell apart.
	gatewayToken = "token-for-the-gateway-serviceaccount-padded-to-a-realistic-length"
	agentToken   = "token-for-the-agent-serviceaccount-padded-to-a-realistic-length"
	strangerSA   = "system:serviceaccount:kubeagents-system:some-other-workload"
	strangerTok  = "token-for-a-serviceaccount-that-is-not-in-the-map-at-all-padded"

	agentSA = "system:serviceaccount:kubeagents-system:agent"
)

// twoIdentityMap grants the two principals deliberately disjoint subjects, so
// "each is refused on the other's" is a real assertion rather than a
// coincidence of one being a subset of the other.
const twoIdentityMap = `{
  "version": "itest-1",
  "identities": [
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-a2a-gateway",
      "user": "gateway",
      "account": "APP",
      "grants": {
        "publish": ["a2a.tasks.>", "_INBOX.gateway.>"],
        "subscribe": ["a2a.tasks.>", "_INBOX.gateway.>"]
      }
    },
    {
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent",
      "user": "agent",
      "account": "APP",
      "grants": {
        "publish": ["a2a.topics.>", "_INBOX.agent.>"],
        "subscribe": ["a2a.topics.>", "_INBOX.agent.>"]
      }
    }
  ]
}`

type harness struct {
	server    *natsserver.Server
	url       string
	store     *Store
	tokenToSA map[string]string
}

// tokenReviewer maps a presented token to the ServiceAccount the cluster would
// vouch for, and refuses anything else — the shape of a real TokenReview
// answer, including the audience the validator insists on.
func tokenReviewer(tokenToSA map[string]string, onReview func() func()) *fake.Clientset {
	c := fake.NewSimpleClientset()
	c.PrependReactor("create", "tokenreviews", func(action k8stesting.Action) (bool, runtime.Object, error) {
		if onReview != nil {
			defer onReview()()
		}
		req := action.(k8stesting.CreateAction).GetObject().(*authnv1.TokenReview)
		sa, ok := tokenToSA[req.Spec.Token]
		if !ok {
			req.Status = authnv1.TokenReviewStatus{Authenticated: false, Error: "invalid bearer token"}
			return true, req, nil
		}
		req.Status = authnv1.TokenReviewStatus{
			Authenticated: true,
			User:          authnv1.UserInfo{Username: sa},
			Audiences:     req.Spec.Audiences,
		}
		return true, req, nil
	})
	return c
}

// harnessOption tweaks one harness. There is one so far; see onTokenReview.
type harnessOption func(*harnessOptions)

type harnessOptions struct {
	// onTokenReview is called on entry to every TokenReview the callout makes
	// and its result on exit, so a test can watch how many are in flight at
	// once and how long each one takes.
	onTokenReview func() func()
}

// watchingTokenReviews wraps every TokenReview the callout performs.
func watchingTokenReviews(around func() func()) harnessOption {
	return func(o *harnessOptions) { o.onTokenReview = around }
}

// startHarness renders a real nats.conf with an auth_callout block, starts a
// server from it, and connects the callout service.
func startHarness(t *testing.T, identityMap string, tokenToSA map[string]string, tweaks ...harnessOption) *harness {
	t.Helper()

	var options harnessOptions
	for _, tweak := range tweaks {
		tweak(&options)
	}

	// The issuer is an ACCOUNT keypair: the server holds the public half and
	// the callout signs with the seed. A user or curve key here is refused by
	// the server's own config validation.
	issuerKP, err := nkeys.CreateAccount()
	if err != nil {
		t.Fatalf("CreateAccount: %v", err)
	}
	issuerSeed, _ := issuerKP.Seed()
	issuerPub, _ := issuerKP.PublicKey()

	// The curve key encrypts the authorization request, which carries the
	// client's raw ServiceAccount token. Without it that token crosses the
	// bus in the clear.
	xkeyKP, err := nkeys.CreateCurveKeys()
	if err != nil {
		t.Fatalf("CreateCurveKeys: %v", err)
	}
	xkeySeed, _ := xkeyKP.Seed()
	xkeyPub, _ := xkeyKP.PublicKey()

	// The operator's REAL rendered nats.conf, not a hand-written mirror of it.
	//
	// This is the point of the fixture. A callout suite that writes its own
	// config proves the callout works against that config; it says nothing
	// about the one this product ships, and every interesting failure here is
	// a config-shape failure — a malformed auth_callout block, a static user
	// missing from auth_users, max_control_line left at a default that a
	// projected token does not fit inside. Starting a real server from the
	// real render is what makes those fail in CI instead of on a cluster.
	//
	// Two substitutions, both mechanical. The committed fixture carries
	// placeholder public keys because the seeds must not be in the repository,
	// so the freshly generated public halves go in here where the matching
	// seeds are in hand. Ports and the store directory are overridden on the
	// parsed options below rather than in the text.
	rendered, err := os.ReadFile("testdata/rendered-nats.conf")
	if err != nil {
		t.Fatalf("reading the operator's rendered nats.conf: %v", err)
	}
	conf := replaceConfKey(string(rendered), "issuer: ", issuerPub)
	conf = replaceConfKey(conf, "xkey: ", xkeyPub)

	confPath := filepath.Join(t.TempDir(), "nats.conf")
	if err := os.WriteFile(confPath, []byte(conf), 0o600); err != nil {
		t.Fatalf("writing nats.conf: %v", err)
	}

	opts, err := natsserver.ProcessConfigFile(confPath)
	if err != nil {
		t.Fatalf("the rendered nats.conf was refused by the server: %v", err)
	}
	opts.NoLog, opts.NoSigs = true, true
	// Ports and paths are the only things a test may move: the render pins
	// 4222 and 9222 and a JetStream store at /data, none of which a test
	// process can have. Everything the test is actually asserting about —
	// accounts, users, grants, the callout block — is untouched.
	opts.Port = -1
	opts.Websocket.Port = -1
	opts.StoreDir = t.TempDir()

	srv, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatalf("NewServer: %v", err)
	}
	go srv.Start()
	if !srv.ReadyForConnections(10 * time.Second) {
		t.Fatal("nats-server not ready")
	}
	t.Cleanup(srv.Shutdown)

	store := NewStore(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := store.Update([]byte(identityMap)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}

	validator, err := NewTokenValidator(tokenReviewer(tokenToSA, options.onTokenReview), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}

	svc, err := NewService(store, validator, Config{
		IssuerSeed: string(issuerSeed),
		XKeySeed:   string(xkeySeed),
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	// The callout is itself a bus client, and it authenticates statically
	// because it cannot authenticate through itself. auth_users is what
	// exempts it.
	svcConn, err := nats.Connect(srv.ClientURL(), nats.UserInfo(svcUser, svcPass), nats.Name("auth-callout"))
	if err != nil {
		t.Fatalf("the callout service could not connect: %v", err)
	}
	t.Cleanup(svcConn.Close)
	if _, err := svc.Subscribe(svcConn); err != nil {
		t.Fatalf("Subscribe: %v", err)
	}

	return &harness{server: srv, url: srv.ClientURL(), store: store, tokenToSA: tokenToSA}
}

// connectAs dials with a ServiceAccount token, the way a workload will.
func (h *harness) connectAs(t *testing.T, user, token string) (*nats.Conn, chan error) {
	t.Helper()
	violations := make(chan error, 16)
	nc, err := nats.Connect(h.url,
		nats.Token(token),
		nats.CustomInboxPrefix("_INBOX."+user),
		nats.Name(user),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, e error) {
			violations <- e
		}),
	)
	if err != nil {
		t.Fatalf("%s could not connect: %v", user, err)
	}
	t.Cleanup(nc.Close)
	return nc, violations
}

// publishRefused reports whether the server refused a publish. The refusal is
// asynchronous — Publish itself returns nil — so it arrives on the error
// handler, and the connection stays open throughout.
func publishRefused(t *testing.T, nc *nats.Conn, violations chan error, subject string) bool {
	t.Helper()
	if err := nc.Publish(subject, []byte("x")); err != nil {
		t.Fatalf("Publish(%s) returned a synchronous error: %v", subject, err)
	}
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush after publishing to %s: %v", subject, err)
	}
	select {
	case e := <-violations:
		if !strings.Contains(e.Error(), "ermissions") {
			t.Fatalf("unexpected async error publishing to %s: %v", subject, e)
		}
		return true
	case <-time.After(500 * time.Millisecond):
		return false
	}
}

func defaultTokens() map[string]string {
	return map[string]string{
		gatewayToken: "system:serviceaccount:kubeagents-system:agent-a2a-gateway",
		agentToken:   agentSA,
		strangerTok:  strangerSA,
	}
}

// DoD 1: a client with a valid KSA token connects and gets exactly its mapped
// grants.
func TestAValidTokenGetsExactlyItsMappedGrants(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())
	nc, violations := h.connectAs(t, "gateway", gatewayToken)

	if publishRefused(t, nc, violations, "a2a.tasks.platform.t1.in") {
		t.Error("gateway was refused a subject its map entry grants")
	}
	if !publishRefused(t, nc, violations, "a2a.topics.shared.blueprint") {
		t.Error("gateway was allowed a subject its map entry does not grant")
	}
	if !nc.IsConnected() {
		t.Error("the connection closed on a permissions violation; it must stay open")
	}
}

// DoD 2: a client with a token for a different KSA gets a different grant set,
// and each is refused on the other's subjects.
func TestADifferentServiceAccountGetsADifferentGrantSet(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	gateway, gwViolations := h.connectAs(t, "gateway", gatewayToken)
	agent, agViolations := h.connectAs(t, "agent", agentToken)

	if publishRefused(t, agent, agViolations, "a2a.topics.shared.blueprint") {
		t.Error("agent was refused a subject its own map entry grants")
	}
	if !publishRefused(t, agent, agViolations, "a2a.tasks.platform.t1.in") {
		t.Error("agent reached the gateway's task plane; the two grant sets are not distinct")
	}
	if !publishRefused(t, gateway, gwViolations, "a2a.topics.shared.blueprint") {
		t.Error("gateway reached the agent's topics; the two grant sets are not distinct")
	}
}

// DoD 3: an unmapped KSA is refused at connect.
//
// Asserted as a connect failure specifically. A refusal and a permissions
// violation are different observables — one has no connection at all, the other
// is an async error on an open one — and a test that conflated them would pass
// against a callout that granted everyone everything.
func TestAnUnmappedServiceAccountIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	nc, err := nats.Connect(h.url, nats.Token(strangerTok), nats.Name("stranger"))
	if err == nil {
		nc.Close()
		t.Fatal("a ServiceAccount absent from the identity map connected")
	}
	if !strings.Contains(err.Error(), "Authorization Violation") {
		t.Errorf("connect error = %v, want an Authorization Violation", err)
	}
}

func TestATokenTheClusterRefusesIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	nc, err := nats.Connect(h.url, nats.Token("a-token-no-cluster-ever-issued"), nats.Name("forged"))
	if err == nil {
		nc.Close()
		t.Fatal("a token the cluster does not authenticate was accepted")
	}
	if !strings.Contains(err.Error(), "Authorization Violation") {
		t.Errorf("connect error = %v, want an Authorization Violation", err)
	}
}

func TestNoTokenIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	nc, err := nats.Connect(h.url, nats.Name("anonymous"))
	if err == nil {
		nc.Close()
		t.Fatal("a client presenting no credential at all connected")
	}
}

// The token may arrive as the password rather than the connection token —
// nats.UserInfo delivers it byte-identically and gives the callout a claimed
// identity to log. Both paths must resolve to the same grants.
func TestTheTokenIsAcceptedAsAPasswordToo(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	nc, err := nats.Connect(h.url,
		nats.UserInfo(agentSA, agentToken),
		nats.CustomInboxPrefix("_INBOX.agent"),
		nats.Name("agent-via-userinfo"))
	if err != nil {
		t.Fatalf("the token was not accepted as a password: %v", err)
	}
	defer nc.Close()
}

// The inbox trap, end to end. Each principal is granted only its own prefix, so
// a client that does not set a matching custom prefix has every reply refused —
// and it presents as a timeout on an open, healthy-looking connection rather
// than as a permissions error. This is the failure W6 found twice.
func TestAClientMustUseTheInboxPrefixItsGrantsCover(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	responder, _ := h.connectAs(t, "gateway", gatewayToken)
	if _, err := responder.Subscribe("a2a.tasks.svc.echo", func(m *nats.Msg) {
		_ = m.Respond([]byte("pong"))
	}); err != nil {
		t.Fatalf("Subscribe: %v", err)
	}
	if err := responder.Flush(); err != nil {
		t.Fatalf("Flush: %v", err)
	}

	// The default random inbox is not covered by the gateway's grant.
	wrong, err := nats.Connect(h.url, nats.Token(gatewayToken), nats.Name("gateway-default-inbox"))
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer wrong.Close()
	if _, err := wrong.Request("a2a.tasks.svc.echo", nil, 750*time.Millisecond); err == nil {
		t.Error("a request on an ungranted inbox prefix succeeded")
	}
	if !wrong.IsConnected() {
		t.Error("the connection closed; the inbox failure must present on an open connection")
	}

	// With the prefix its grants cover, the same request works.
	right, _ := h.connectAs(t, "gateway", gatewayToken)
	msg, err := right.Request("a2a.tasks.svc.echo", nil, 5*time.Second)
	if err != nil {
		t.Fatalf("request with the granted inbox prefix failed: %v", err)
	}
	if string(msg.Data) != "pong" {
		t.Errorf("reply = %q, want pong", msg.Data)
	}
}

// A callout serving no map must refuse rather than invent grants.
func TestACalloutWithNoMapRefusesEveryConnection(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	// Drop to a state where nothing is served by starting a fresh store —
	// the same state a callout is in before its first informer sync.
	h.store.current.Store(nil)

	nc, err := nats.Connect(h.url, nats.Token(gatewayToken), nats.Name("gateway"))
	if err == nil {
		nc.Close()
		t.Fatal("a connection was authorized while no identity map was being served")
	}
}

// Grants are fixed when a connection authenticates and the callout is never
// consulted again, so a narrowed map does not reach anything already connected.
// That is the property to know about rather than discover: it is why issued
// grants carry an expiry at all.
func TestANarrowedMapDoesNotReachAnEstablishedConnection(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())
	nc, violations := h.connectAs(t, "gateway", gatewayToken)

	if publishRefused(t, nc, violations, "a2a.tasks.platform.t1.in") {
		t.Fatal("gateway was refused a granted subject before the map changed")
	}

	narrowed := strings.Replace(twoIdentityMap, `"a2a.tasks.>", "_INBOX.gateway.>"`, `"_INBOX.gateway.>"`, 1)
	narrowed = strings.Replace(narrowed, `"version": "itest-1"`, `"version": "itest-2"`, 1)
	if err := h.store.Update([]byte(narrowed)); err != nil {
		t.Fatalf("narrowing the map: %v", err)
	}

	if publishRefused(t, nc, violations, "a2a.tasks.platform.t1.in") {
		t.Error("the established connection lost a grant when the map changed; if this now holds, the expiry rationale needs rewriting")
	}

	// A new connection gets the narrowed set immediately.
	fresh, freshViolations := h.connectAs(t, "gateway", gatewayToken)
	if !publishRefused(t, fresh, freshViolations, "a2a.tasks.platform.t1.in") {
		t.Error("a connection made after the narrowing still holds the old grant")
	}
}

// The callout places issued users in the account its map entry names, not in
// the global account. The account is the tenant boundary, so a callout that
// quietly dropped everyone into $G would still pass every grant assertion above
// while removing the boundary entirely.
func TestIssuedUsersLandInTheAccountTheirMapEntryNames(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())
	h.connectAs(t, "gateway", gatewayToken)

	z, err := h.server.Connz(&natsserver.ConnzOptions{Username: true})
	if err != nil {
		t.Fatalf("Connz: %v", err)
	}

	var found bool
	for _, c := range z.Conns {
		if c.Name != "gateway" {
			continue
		}
		found = true
		if c.Account != "APP" {
			t.Errorf("gateway landed in account %q, want APP", c.Account)
		}
		if c.AuthorizedUser != "gateway" {
			t.Errorf("gateway authorized as %q, want the mapped user name", c.AuthorizedUser)
		}
	}
	if !found {
		t.Fatal("the gateway connection was not visible to Connz")
	}
}

// TestTheCalloutAnswersOneAuthorizationAtATime measures the throughput ceiling
// a replica has, so the number in the deployment spec is a measurement rather
// than a reading of the code.
//
// Subscribe joins the queue group with an async handler, and the client library
// dispatches one subscription's callbacks from one goroutine. handle does the
// TokenReview round trip inline, so a replica answers connections strictly one
// at a time, and each one holds the line for as long as the API server takes.
// Two replicas is therefore two authorizations in flight for the whole fabric,
// not two hundred. Nothing here is wrong today -- connections are rare compared
// to messages, and the queue group means the second replica does take the next
// one -- but it is a property worth knowing before a fleet reconnects at once.
//
// Measured by making the TokenReview slow and watching how many are open at the
// same moment, which is the only way to tell serial dispatch from a race that
// happened not to overlap.
func TestTheCalloutAnswersOneAuthorizationAtATime(t *testing.T) {
	const clients = 4
	// Long enough that concurrent handling would overlap unmistakably, short
	// enough that four in series stay inside authDecisionBudget and well
	// inside the server's first-ping timer.
	const reviewCost = 120 * time.Millisecond

	var mu sync.Mutex
	inFlight, peak, reviews := 0, 0, 0

	h := startHarness(t, twoIdentityMap, defaultTokens(), watchingTokenReviews(func() func() {
		mu.Lock()
		inFlight++
		reviews++
		if inFlight > peak {
			peak = inFlight
		}
		mu.Unlock()

		time.Sleep(reviewCost)

		return func() {
			mu.Lock()
			inFlight--
			mu.Unlock()
		}
	}))

	failures := make(chan error, clients)
	var wg sync.WaitGroup
	start := time.Now()
	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			nc, err := nats.Connect(h.url, nats.Token(agentToken), nats.CustomInboxPrefix("_INBOX.agent"), nats.Name("agent"))
			if err != nil {
				failures <- err
				return
			}
			nc.Close()
		}()
	}
	wg.Wait()
	elapsed := time.Since(start)
	close(failures)
	for err := range failures {
		t.Fatalf("a client could not connect: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()

	if reviews != clients {
		t.Fatalf("TokenReviews = %d for %d connections; the measurement below is not of what it claims", reviews, clients)
	}
	if peak != 1 {
		t.Errorf("peak concurrent authorizations = %d, want 1.\n"+
			"A replica now answers more than one at a time, which is better than what shipped -- "+
			"update the per-replica ceiling in docs/designs/spec-nats-deployment.md rather than this number.", peak)
	}
	// The consequence, stated as time rather than as a count: the last client
	// waits behind all the others. Three costs, not four, so a slow scheduler
	// cannot make this flake.
	if floor := (clients - 1) * reviewCost; elapsed < floor {
		t.Errorf("%d connections at %v of TokenReview each took %v, less than the %v serial handling implies; "+
			"either dispatch is no longer serial or the measurement is not reaching the handler", clients, reviewCost, elapsed, floor)
	}
}

// replaceConfKey swaps the value of a `key: value` line in the rendered config.
func replaceConfKey(conf, prefix, value string) string {
	i := strings.Index(conf, prefix)
	if i < 0 {
		return conf
	}
	start := i + len(prefix)
	end := start
	for end < len(conf) && conf[end] != '\n' {
		end++
	}
	return conf[:start] + value + conf[end:]
}
