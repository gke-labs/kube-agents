package authcallout

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/kubernetes/fake"
	authnv1client "k8s.io/client-go/kubernetes/typed/authentication/v1"

	"github.com/gke-labs/kube-agents/a2a/lib"
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

	// The platform agent container's principal: the gateway Pod's own
	// ServiceAccount, as the operator renders it (gatewayServiceAccountName).
	agentSA = "system:serviceaccount:kubeagents-system:agent-gateway-agent"
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
      "serviceAccount": "system:serviceaccount:kubeagents-system:agent-gateway-agent",
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
	server *natsserver.Server
	url    string
	store  *Store
	tokens map[string]Attested
	// dial trusts the CA the rendered conf's serving pair was minted from
	// and sets the suite's connect timeout (withTestTLS); every connection
	// the suite makes needs it, since the render requires TLS of every
	// client.
	dial nats.Option
}

// tokenReviewer maps a presented token to what the cluster would vouch for,
// and refuses anything else — the shape of a real TokenReview answer, including
// the audience the validator insists on and the pod-bound Extra fields the
// authenticator writes for a projected token.
func tokenReviewer(tokens map[string]Attested, onReview func(token string) func()) kubernetes.Interface {
	return clientWithTokenReviews(func(_ context.Context, req *authnv1.TokenReview) (*authnv1.TokenReview, error) {
		if onReview != nil {
			defer onReview(req.Spec.Token)()
		}
		att, ok := tokens[req.Spec.Token]
		if !ok {
			req.Status = authnv1.TokenReviewStatus{Authenticated: false, Error: "invalid bearer token"}
			return req, nil
		}
		user := authnv1.UserInfo{Username: att.ServiceAccount}
		if att.PodName != "" || att.PodUID != "" {
			user.Extra = map[string]authnv1.ExtraValue{
				"authentication.kubernetes.io/pod-name": {att.PodName},
				"authentication.kubernetes.io/pod-uid":  {att.PodUID},
			}
		}
		req.Status = authnv1.TokenReviewStatus{
			Authenticated: true,
			User:          user,
			Audiences:     req.Spec.Audiences,
		}
		return req, nil
	})
}

// tokenReviewCreate answers one TokenReview, with the validator's context.
type tokenReviewCreate func(ctx context.Context, req *authnv1.TokenReview) (*authnv1.TokenReview, error)

// clientWithTokenReviews is the fake clientset with its TokenReview endpoint
// answered by create: concurrently, and with the caller's context, neither
// of which the fake's reactor chain offers. testing.Fake.Invokes holds one
// mutex across every reactor call, so a reactor would serialize the reviews
// and hide the callout's worker pool from the tests that measure it, and the
// typed fake drops the context before the reactor sees it. Only
// TokenReviews().Create is taken over; the rest of the clientset is the
// fake's.
func clientWithTokenReviews(create tokenReviewCreate) kubernetes.Interface {
	return &reviewingClientset{Clientset: fake.NewSimpleClientset(), create: create}
}

// reviewingClientset is the fake clientset with a TokenReview endpoint of the
// test's own in front of it.
type reviewingClientset struct {
	*fake.Clientset
	create tokenReviewCreate
}

func (c *reviewingClientset) AuthenticationV1() authnv1client.AuthenticationV1Interface {
	return &reviewingAuthenticationV1{AuthenticationV1Interface: c.Clientset.AuthenticationV1(), reviews: c}
}

type reviewingAuthenticationV1 struct {
	authnv1client.AuthenticationV1Interface
	reviews authnv1client.TokenReviewInterface
}

func (a *reviewingAuthenticationV1) TokenReviews() authnv1client.TokenReviewInterface {
	return a.reviews
}

// TokenReviewInterface's Create is the one call the validator makes; the
// embedded fake answers the others, which nothing here calls.
var _ authnv1client.TokenReviewInterface = (*reviewingClientset)(nil)

func (c *reviewingClientset) Create(ctx context.Context, req *authnv1.TokenReview, _ metav1.CreateOptions) (*authnv1.TokenReview, error) {
	return c.create(ctx, req.DeepCopy())
}

// harnessOption tweaks one harness.
type harnessOption func(*harnessOptions)

type harnessOptions struct {
	// onTokenReview is called, with the token under review, on entry to
	// every TokenReview the callout makes and its result on exit, so a test
	// can watch how many are in flight at once, how long each one takes,
	// and whose they are.
	onTokenReview func(token string) func()
	// grantTTL, when set, replaces the callout's one-hour grant lifetime,
	// so a test can watch a connection outlive the JWT it was issued.
	grantTTL time.Duration
	// taskKeys are the task-signing public keys the callout trusts, for the
	// per-task credential tests (task_credential_test.go).
	taskKeys []string
	// now, when set, is the callout's clock.
	now func() time.Time
	// taskLifetime and taskLifetimeErr are the callout's task credential
	// lifetime settings (Config.TaskCredentialMaxLifetime and ...Invalid).
	taskLifetime    time.Duration
	taskLifetimeErr error
	// noReviewCache turns the validator's 30s memory of a successful
	// TokenReview off, for a test that counts reviews to count reconnects
	// of one token.
	noReviewCache bool
}

// withGrantTTL shortens every grant the callout issues.
func withGrantTTL(d time.Duration) harnessOption {
	return func(o *harnessOptions) { o.grantTTL = d }
}

// withTaskSigningKeys makes the callout honour task credentials from these
// keys.
func withTaskSigningKeys(keys ...string) harnessOption {
	return func(o *harnessOptions) { o.taskKeys = keys }
}

// withTaskLifetime sets the callout's task credential lifetime, as
// ParseTaskCredentialMaxLifetime returned it.
func withTaskLifetime(d time.Duration, err error) harnessOption {
	return func(o *harnessOptions) { o.taskLifetime, o.taskLifetimeErr = d, err }
}

// withNow sets the callout's clock.
func withNow(now func() time.Time) harnessOption {
	return func(o *harnessOptions) { o.now = now }
}

// watchingTokenReviews wraps every TokenReview the callout performs. The
// validator remembers a success for reviewCacheTTL, so a token presented
// again inside it is not reviewed again and is not seen here; a test that
// needs every presentation adds withoutReviewCache.
func watchingTokenReviews(around func(token string) func()) harnessOption {
	return func(o *harnessOptions) { o.onTokenReview = around }
}

// withoutReviewCache makes the callout review every presented token.
func withoutReviewCache() harnessOption {
	return func(o *harnessOptions) { o.noReviewCache = true }
}

// startHarness renders a real nats.conf with an auth_callout block, starts a
// server from it, and connects the callout service.
func startHarness(t *testing.T, identityMap string, tokens map[string]Attested, tweaks ...harnessOption) *harness {
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
	conf, dialOpt := withTestTLS(t, conf)

	confPath := filepath.Join(t.TempDir(), "nats.conf")
	if err := os.WriteFile(confPath, []byte(conf), 0o600); err != nil {
		t.Fatalf("writing nats.conf: %v", err)
	}

	opts, err := natsserver.ProcessConfigFile(confPath)
	if err != nil {
		t.Fatalf("the rendered nats.conf was refused by the server: %v", err)
	}
	opts.NoLog, opts.NoSigs = true, true
	serverConnectBudget(opts)
	// Ports, paths and the connect budget are the only things a test may
	// move: the render pins 4222, 8222 and 9222 and a JetStream store at
	// /data, none of which a test process can have. -1 asks the server for a
	// free port on each listener, the monitor included, so the server keeps
	// the shipped shape while nothing fixed is bound. Everything the test is
	// actually asserting about — accounts, users, grants, the callout block's
	// keys and policy — is untouched; its two-second timeout is the one
	// value serverConnectBudget lifts, and why.
	opts.Port = -1
	opts.Websocket.Port = -1
	opts.HTTPPort = -1
	// The render binds every interface; the test's serving certificate names
	// the loopback address, so the server is bound there instead.
	opts.Host = "127.0.0.1"
	opts.StoreDir = t.TempDir()

	// startNatsServer keeps the server's own log, so a server that never
	// becomes ready fails the test with its reason rather than a timeout.
	srv, err := startNatsServer(opts, 10*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(srv.Shutdown)

	store := NewStore(slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err := store.Update([]byte(identityMap)); err != nil {
		t.Fatalf("loading the identity map: %v", err)
	}

	validator, err := NewTokenValidator(tokenReviewer(tokens, options.onTokenReview), testAudience)
	if err != nil {
		t.Fatalf("NewTokenValidator: %v", err)
	}
	if options.noReviewCache {
		validator.cacheTTL = 0
	}

	svc, err := NewService(store, validator, Config{
		IssuerSeed: string(issuerSeed),
		XKeySeed:   string(xkeySeed),
		// The reserved names come from the same rendered file the server
		// was started from, so the callout refuses exactly the static
		// users this server authenticates by password.
		ReservedPrincipals: renderedAuthUsers(t, string(rendered)),

		// The reserved addressees the operator renders, from the fixture it
		// keeps equal to its render.
		ReservedAddressees: renderedFixtureAddressees(t),

		GrantTTL:        options.grantTTL,
		TaskSigningKeys: options.taskKeys,
		Now:             options.now,

		TaskCredentialMaxLifetime:        options.taskLifetime,
		TaskCredentialMaxLifetimeInvalid: options.taskLifetimeErr,
	}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}

	// The callout is itself a bus client, and it authenticates statically
	// because it cannot authenticate through itself. auth_users is what
	// exempts it.
	svcConn, err := nats.Connect(srv.ClientURL(), nats.UserInfo(svcUser, svcPass), nats.Name("auth-callout"), dialOpt)
	if err != nil {
		t.Fatalf("the callout service could not connect: %v", err)
	}
	t.Cleanup(svcConn.Close)
	if _, err := svc.Subscribe(svcConn); err != nil {
		t.Fatalf("Subscribe: %v", err)
	}

	return &harness{server: srv, url: srv.ClientURL(), store: store, tokens: tokens, dial: dialOpt}
}

// connectAs dials with a ServiceAccount token, the way a workload will.
func (h *harness) connectAs(t *testing.T, user, token string) (*nats.Conn, chan error) {
	t.Helper()
	violations := make(chan error, 16)
	nc, err := nats.Connect(h.url, h.dial,
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

func defaultTokens() map[string]Attested {
	return map[string]Attested{
		gatewayToken: {ServiceAccount: "system:serviceaccount:kubeagents-system:agent-a2a-gateway"},
		agentToken:   {ServiceAccount: agentSA},
		strangerTok:  {ServiceAccount: strangerSA},
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

	nc, err := nats.Connect(h.url, h.dial, nats.Token(strangerTok), nats.Name("stranger"))
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

	nc, err := nats.Connect(h.url, h.dial, nats.Token("a-token-no-cluster-ever-issued"), nats.Name("forged"))
	if err == nil {
		nc.Close()
		t.Fatal("a token the cluster does not authenticate was accepted")
	}
	if !strings.Contains(err.Error(), "Authorization Violation") {
		t.Errorf("connect error = %v, want an Authorization Violation", err)
	}
}

// The rendered config requires TLS of every client: the server says so in
// its INFO, refuses a CONNECT sent in the clear, and refuses a TLS client that
// does not trust the bus CA. Each of the three would be a plaintext bus by
// another route.
func TestTheRenderedBusRefusesPlaintextAndUntrustedClients(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())
	addr := strings.TrimPrefix(strings.TrimPrefix(h.url, "tls://"), "nats://")

	conn, err := net.DialTimeout("tcp", addr, 5*time.Second)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
	r := bufio.NewReader(conn)
	info, err := r.ReadString('\n')
	if err != nil {
		t.Fatalf("reading INFO: %v", err)
	}
	if !strings.Contains(info, `"tls_required":true`) {
		t.Fatalf("the server does not advertise tls_required: %s", info)
	}
	if _, err := conn.Write([]byte("CONNECT {\"verbose\":false,\"name\":\"plaintext\"}\r\nPING\r\n")); err != nil {
		t.Fatalf("writing CONNECT: %v", err)
	}
	reply, _ := r.ReadString('\n')
	if strings.HasPrefix(reply, "PONG") {
		t.Fatal("a CONNECT sent in the clear was answered with PONG")
	}

	t.Setenv(lib.EnvNATSCAFile, "")
	nc, err := nats.Connect(h.url, nats.Token(tokenPodA), nats.Name("untrusted"), nats.Timeout(testDialTimeout))
	if err == nil {
		nc.Close()
		t.Fatal("a client that does not trust the bus CA connected")
	}
	if !strings.Contains(err.Error(), "certificate") {
		t.Errorf("connect error = %v, want a certificate verification failure", err)
	}
}

// lib.TLSOptions is the trust every product client takes, so it is driven
// here against the rendered bus rather than inspected: a client that trusts
// another CA, or the right CA under a name the certificate does not carry, is
// refused at the handshake, and the right CA under the certificate's name
// gets past it. A TLSOptions that stopped verifying would pass the third case
// and fail the first two.
func TestLibTLSOptionsVerifiesTheBus(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())
	rightCA := os.Getenv(lib.EnvNATSCAFile)
	if rightCA == "" {
		t.Fatal("the harness set no CA file; every case below would be inert")
	}
	_, _ = withTestTLS(t, renderedTLSDir)
	otherCA := os.Getenv(lib.EnvNATSCAFile)

	// A token the harness maps, so the third case connects outright rather
	// than being refused at authorization after a handshake it may or may not
	// have made.
	dial := func(t *testing.T, ca, serverName string) error {
		t.Helper()
		t.Setenv(lib.EnvNATSCAFile, ca)
		t.Setenv(lib.EnvNATSTLSServerName, serverName)
		nc, err := nats.Connect(h.url, append(lib.TLSOptions(), nats.Token(gatewayToken), nats.Name("tls-options"), nats.Timeout(testDialTimeout))...)
		if err == nil {
			nc.Close()
		}
		return err
	}
	if err := dial(t, otherCA, ""); err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Errorf("a client trusting another CA: err = %v, want a certificate verification failure", err)
	}
	if err := dial(t, rightCA, "attacker.example"); err == nil || !strings.Contains(err.Error(), "certificate") {
		t.Errorf("the right CA under a name the certificate lacks: err = %v, want a certificate verification failure", err)
	}
	if err := dial(t, rightCA, "localhost"); err != nil {
		t.Errorf("the right CA under the certificate's name did not connect: %v", err)
	}
}

func TestNoTokenIsRefusedAtConnect(t *testing.T) {
	h := startHarness(t, twoIdentityMap, defaultTokens())

	nc, err := nats.Connect(h.url, h.dial, nats.Name("anonymous"))
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

	nc, err := nats.Connect(h.url, h.dial,
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
	wrong, err := nats.Connect(h.url, h.dial, nats.Token(gatewayToken), nats.Name("gateway-default-inbox"))
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

	nc, err := nats.Connect(h.url, h.dial, nats.Token(gatewayToken), nats.Name("gateway"))
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

// TestTheCalloutAnswersSixteenAuthorizationsAtOnce measures the throughput
// ceiling a replica has, so the number in the deployment spec is a measurement
// rather than a reading of the code.
//
// Subscribe joins the queue group with an async handler, and the client library
// dispatches one subscription's callbacks from one goroutine. Until #4332 handle
// did the TokenReview round trip inline, so a replica answered connections
// strictly one at a time, and twelve workers reconnecting per replica queued
// past the server's two-second first-ping timer. handle now hands each request
// to a goroutine that takes one of calloutWorkers slots, so a replica decides
// sixteen at once and the slow API server bounds the rate at sixteen divided by
// its latency rather than one.
//
// Measured by making the TokenReview slow and watching how many are open at the
// same moment, which is the only way to tell pooled dispatch from a race that
// happened not to overlap. The tokens are distinct so every connection is one
// review; the same token sixty-four times would measure the validator's cache
// instead (tokenreview_test.go has that).
func TestTheCalloutAnswersSixteenAuthorizationsAtOnce(t *testing.T) {
	const clients = 64
	// A review slow enough that overlap is unmistakable. Serially sixty-four
	// of them are 3.2s of review alone.
	const reviewCost = 50 * time.Millisecond
	// How long the first reviews wait for the sixteenth to join them before
	// the test gives up on it; far past any arrival skew, and then the peak
	// assertion below says what went wrong.
	const gateWait = 10 * time.Second

	tokens := defaultTokens()
	clientTokens := make([]string, clients)
	for i := range clientTokens {
		clientTokens[i] = fmt.Sprintf("token-for-agent-replica-%02d-padded-to-a-realistic-length", i)
		tokens[clientTokens[i]] = Attested{ServiceAccount: agentSA}
	}

	var mu sync.Mutex
	inFlight, peak, reviews := 0, 0, 0
	// Every review is held until sixteen are in flight at once, and the
	// sixteenth releases them all. Clients arrive as their TLS handshakes
	// finish, spread over most of a second under the race detector, so an
	// unheld pool would show whatever overlap the arrivals happened to
	// produce; held, the peak is the pool's width or the test is wrong.
	// A seventeenth can never join: the slot it needs is held.
	poolFull := make(chan struct{})
	poolFilled := false
	var deciding time.Time

	h := startHarness(t, twoIdentityMap, tokens, watchingTokenReviews(func(string) func() {
		mu.Lock()
		inFlight++
		reviews++
		if inFlight > peak {
			peak = inFlight
		}
		if inFlight == calloutWorkers && !poolFilled {
			poolFilled, deciding = true, time.Now()
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
	}))

	failures := make(chan error, clients)
	var wg sync.WaitGroup
	start := time.Now()
	for i := 0; i < clients; i++ {
		wg.Add(1)
		go func(token string) {
			defer wg.Done()
			nc, err := nats.Connect(h.url, h.dial, nats.Token(token), nats.CustomInboxPrefix("_INBOX.agent"), nats.Name("agent"))
			if err != nil {
				failures <- err
				return
			}
			nc.Close()
		}(clientTokens[i])
	}
	wg.Wait()
	connected := time.Now()
	elapsed := connected.Sub(start)
	close(failures)
	for err := range failures {
		t.Fatalf("a client could not connect: %v", err)
	}

	mu.Lock()
	defer mu.Unlock()
	t.Logf("%d connections at %v of TokenReview each: peak %d in flight, %v from the pool filling until every client was connected, %v in all",
		clients, reviewCost, peak, connected.Sub(deciding), elapsed)

	if reviews != clients {
		t.Fatalf("TokenReviews = %d for %d connections; the measurement below is not of what it claims", reviews, clients)
	}
	// Sixteen held at once is the whole measurement: serial dispatch cannot
	// show it, and the pool cannot show more. The wall clock is logged and
	// not asserted, since sixty-four TLS handshakes on a loaded runner set
	// it; the one-second bound the issue asks for is measured without them,
	// in TestSixtyFourCallbacksFinishInsideASecondSixteenAbreast.
	if peak != calloutWorkers {
		t.Errorf("peak concurrent authorizations = %d, want exactly the %d slots: fewer means the sixteenth never arrived while the first was held, more means the pool bounds nothing", peak, calloutWorkers)
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
