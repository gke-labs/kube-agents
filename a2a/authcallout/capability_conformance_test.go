package authcallout

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// docs/architecture/09-capability-envelope.md §9's conformance tests, run
// against the operator's real render on a real server.
//
// 09 states three properties and they are not independent claims — they are
// the whole of why an unsigned capability in a shared KV bucket is safe:
//
//  1. no broker writes in another principal's namespace;
//  2. no broker READS the store, by any path;
//  3. the permissions that make (1) and (2) true are actually configured.
//
// (3) is not a formality. Every subject in the cap design is a `$KV.…` or
// `$JS.API.…` subject, and a test written against the bare key names — `root.x`
// rather than `$KV.cap.root.x` — matches no subject any KV operation touches.
// Such a test passes against a server with no capability permissions at all,
// which is exactly the failure this file exists to make impossible. So every
// assertion below is a client outcome on a wire subject, and each principal
// that is refused something is also shown reaching something else on the same
// connection.
//
// The map is the operator's rendered fixture rather than a hand-written one:
// the grants under test are the shipped grants or the test is theatre.

// capMap is the operator's render, read from the same fixture contract_test.go
// parses. The identities it carries are provision, session (narrowed) and
// verifier; gateway, bridge and seed are static users in the rendered
// nats.conf and are reached with connectStatic instead.
func capMap(t *testing.T) string {
	t.Helper()
	b, err := os.ReadFile("testdata/rendered-identity-map.json")
	if err != nil {
		t.Fatalf("reading the operator's rendered identity map: %v", err)
	}
	return string(b)
}

const (
	verifierSA  = "system:serviceaccount:kubeagents-system:agent-a2a-verifier"
	provisionSA = "system:serviceaccount:kubeagents-system:agent-a2a-provision"

	tokenVerifier  = "token-for-the-verifier-serviceaccount-padded-to-a-realistic-len"
	tokenProvision = "token-for-the-provision-serviceaccount-padded-to-a-realistic-l"
)

// capTokens attests the callout-issued ServiceAccounts the rendered map keys
// on. The session pods reuse the pod-bound tokens the narrowing tests already
// define, so a session's grants here are the derived ones, not a fixture.
func capTokens() map[string]Attested {
	return map[string]Attested{
		tokenVerifier:  {ServiceAccount: verifierSA},
		tokenProvision: {ServiceAccount: provisionSA},
		agentToken:     {ServiceAccount: agentSA},
		tokenPodA:      {ServiceAccount: sessionSA, PodName: podA, PodUID: "uid-a"},
		tokenPodB:      {ServiceAccount: sessionSA, PodName: podB, PodUID: "uid-b"},
	}
}

// capReadSubjects is every JetStream API subject that would read, copy or
// destroy the cap bucket. Written at both depths the stream name occupies and
// with and without a trailing token, because that is the shape the deny in
// platformagent_a2a_identities.go is written against and a deny that missed a
// depth would leave a working read behind.
func capReadSubjects() []string {
	s := capability.Stream
	return []string{
		"$JS.API.STREAM.INFO." + s,
		"$JS.API.DIRECT.GET." + s,
		"$JS.API.DIRECT.GET." + s + "." + capability.Subject("root.task-1"),
		"$JS.API.STREAM.MSG.GET." + s,
		"$JS.API.CONSUMER.CREATE." + s + ".spy",
		"$JS.API.CONSUMER.CREATE." + s + ".spy." + capability.SubjectPrefix + ">",
		"$JS.API.CONSUMER.MSG.NEXT." + s + ".spy",
		"$JS.API.STREAM.SNAPSHOT." + s,
		"$JS.API.STREAM.PURGE." + s,
		"$JS.API.STREAM.DELETE." + s,
	}
}

func refuseAll(subjects []string) map[string]bool {
	m := make(map[string]bool, len(subjects))
	for _, s := range subjects {
		m[s] = true
	}
	return m
}

// 09 §9 (2). No broker reads the store — not the three subjects the verifier
// holds, not a consumer, not a snapshot, and not the bucket's own subject
// space.
//
// Every broker here now reaches JetStream through an enumerated list rather
// than a wildcard: gke-labs#1316 enumerated the worker and seed grants and
// gke-labs#1666 did the same for the gateway, retiring the `$JS.API.>` that
// gke-labs#1306 was filed against. That makes the cap bucket unreachable by
// construction rather than by subtraction — which is exactly why the question
// still has to be asked here. An enumerated list is narrower only until
// someone adds a line to it, and the line that would matter is a one-word
// edit. So each principal is asked for every subject that would read, copy or
// destroy the bucket, and the answer is read off the wire.
//
// The four principals are the whole set that could hold one. `gateway`,
// `bridge` and `seed` are the static users in the rendered nats.conf;
// gke-labs#1653 split the old `worker` credential into `bridge` (static, the
// executor for the platform addressee) and `agent` (callout-authenticated,
// blackboard only), so both halves of what used to be one answer are asked
// separately below.
func TestNoBrokerCanReadTheCapabilityStore(t *testing.T) {
	h, serverLog := startHarnessWithServerLogMap(t, capMap(t), capTokens())

	for _, user := range []string{"gateway", "bridge", "seed"} {
		t.Run(user, func(t *testing.T) {
			nc, violations := connectStatic(t, h, user, "pw-"+user)
			want := refuseAll(capReadSubjects())
			if user == "seed" {
				// The one exception in this whole test, and it is asserted
				// as an allow rather than dropped, so that taking it away
				// fails here instead of in an install.
				//
				// seed PROVISIONS this bucket. `kv info cap || kv add cap`
				// is the provision script's idempotency guard, so STREAM.INFO
				// on the bucket is a grant it has to hold. What that returns
				// is stream state — a message count, a subject list, a first
				// and last sequence — and none of it is a capability. Every
				// subject that does return one (DIRECT.GET, STREAM.MSG.GET,
				// each CONSUMER verb), the copies (SNAPSHOT), the destructive
				// ones and the subscribe on the bucket's subject space all
				// stay refused below, which is what makes this a carve-out
				// and not a hole.
				want["$JS.API.STREAM.INFO."+capability.Stream] = false
			}
			checkPublish(t, nc, violations, want)
			if !subscribeRefused(t, nc, violations, capability.SubjectPrefix+">") {
				t.Errorf("%s may subscribe to %s>; it would see every capability as it is minted",
					user, capability.SubjectPrefix)
			}
			// The control for this whole subtest: the connection is
			// live and the same JetStream API works on a stream that is
			// not the cap bucket. Without this, a broken credential
			// would pass every assertion above.
			checkPublish(t, nc, violations, map[string]bool{
				"$JS.API.STREAM.INFO.TASKS": false,
			})
		})
	}

	// The callout half of the old `worker`. It authenticates with a
	// ServiceAccount token rather than a password, so its grants come from
	// the operator's rendered identity map instead of the nats.conf, and
	// the deny that covers the static users does not cover it at all --
	// the map simply never grants it anything under the bucket. That is a
	// different mechanism reaching the same answer, which is why it is
	// asked rather than assumed.
	t.Run("agent", func(t *testing.T) {
		nc, violations := h.connectAs(t, "agent", agentToken)
		checkPublish(t, nc, violations, refuseAll(capReadSubjects()))
		if !subscribeRefused(t, nc, violations, capability.SubjectPrefix+">") {
			t.Errorf("agent may subscribe to %s>; it would see every capability as it is minted",
				capability.SubjectPrefix)
		}
		// The control: a subject this principal really does hold, so the
		// refusals above are its grants at work and not a connection
		// that can publish nothing.
		checkPublish(t, nc, violations, map[string]bool{
			"a2a.topics.shared.blueprint": false,
		})
	})

	t.Run("session", func(t *testing.T) {
		nc, violations := h.connectAs(t, podA, tokenPodA)
		checkPublish(t, nc, violations, refuseAll(capReadSubjects()))
		if !subscribeRefused(t, nc, violations, capability.SubjectPrefix+">") {
			t.Error("a session may subscribe to the cap bucket's subject space")
		}
		// The control: this session's own task subject still works, so
		// the refusals above are this pod's narrowed grants at work and
		// not a connection that can publish nothing.
		checkPublish(t, nc, violations, map[string]bool{
			lib.TaskEventsSubject(podA, "task-1"): false,
		})
	})

	if !serverLog.sawViolationFor(capability.Stream) {
		t.Error("the server logged no violation naming KV_cap; the refusals have to be visible where an operator looks")
	}
}

// 09 §9 (1). A broker writes in its own namespace and nowhere else.
//
// The gateway's grant is `$KV.cap.root.*` — one token, so one request id, and
// no reach into the hop namespace at all. A session's is nothing: there is no
// second hop in the product yet, so the write a hop would need is deliberately
// ungranted rather than granted early (see sessionGrants).
func TestACapabilityWriterCannotWriteOutsideItsOwnNamespace(t *testing.T) {
	h, _ := startHarnessWithServerLogMap(t, capMap(t), capTokens())

	gw, gwViolations := connectStatic(t, h, "gateway", renderedGatewayPassword)
	checkPublish(t, gw, gwViolations, map[string]bool{
		// Its own namespace, one token deep.
		capability.Subject("root.task-1"): false,
		// Two tokens: `*` is one token, so a request id with a dot in it
		// is refused rather than silently landing somewhere else.
		capability.Subject("root.task.1"): true,
		// The hop namespace, which belongs to whoever is attenuating.
		capability.Subject("hop." + podA + ".0"): true,
		capability.Subject("hop.gateway.0"):      true,
		// Nothing above the namespace.
		"$KV.cap.anything": true,
	})

	session, sessionViolations := h.connectAs(t, podA, tokenPodA)
	checkPublish(t, session, sessionViolations, map[string]bool{
		// A session cannot mint a root, which would be a task
		// authorizing itself.
		capability.Subject("root.task-1"): true,
		// Nor a hop, its own included: the rules for attenuation exist
		// and are tested, but no shipped component performs one, so the
		// grant lands with the caller rather than ahead of it.
		capability.Subject("hop." + podA + ".0"): true,
		capability.Subject("hop." + podB + ".0"): true,
	})

	// Both halves of the credential a compromised agent-side sidecar could
	// hold -- gke-labs#1653 split the old static `worker` into the bridge's
	// static executor credential and the callout-issued `agent`. Neither
	// writes in this namespace, and they are asked separately because they
	// are refused by different mechanisms: the bridge by a deny on an
	// enumerated static grant, the agent by a rendered map that grants it
	// nothing here.
	bridge, bridgeViolations := connectStatic(t, h, "bridge", "pw-bridge")
	checkPublish(t, bridge, bridgeViolations, map[string]bool{
		capability.Subject("root.task-1"):        true,
		capability.Subject("hop." + podA + ".0"): true,
	})

	agent, agentViolations := h.connectAs(t, "agent", agentToken)
	checkPublish(t, agent, agentViolations, map[string]bool{
		capability.Subject("root.task-1"):        true,
		capability.Subject("hop." + podA + ".0"): true,
	})
}

// 09 §9 (3), and the DoD's live half in unit-test form: the real verifier,
// under the grants the operator renders for it, answering a real session's
// real client over the shipped permission set.
//
// This is the test the other two are meaningless without. They assert that a
// long list of subjects is refused; refusing every subject on the bus would
// satisfy them. What this one asserts is that after all that refusing the
// mechanism still works end to end — the gateway mints, the verifier reads,
// the session asks and is answered, and the answer is right.
func TestTheShippedGrantsLetTheVerifierWorkAndNobodyElse(t *testing.T) {
	h, vl := startHarnessWithServerLogMap(t, capMap(t), capTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	// The bucket, created by the principal that creates it in the
	// deployment. The provision Job's grants include STREAM.CREATE.KV_cap
	// and nothing that reads an entry, and this is where that is checked.
	provision, _ := h.connectAs(t, "provision", tokenProvision)
	pjs, err := jetstream.New(provision)
	if err != nil {
		t.Fatalf("jetstream as provision: %v", err)
	}
	if _, err := pjs.CreateKeyValue(ctx, jetstream.KeyValueConfig{
		Bucket: capability.Bucket, History: 1,
	}); err != nil {
		t.Fatalf("the provision principal could not create the cap bucket: %v", err)
	}

	startCapabilityVerifier(t, ctx, h)

	// The gateway mints, as the gateway: a static nats.conf principal with
	// exactly one grant on this path.
	gw, _ := connectStatic(t, h, "gateway", renderedGatewayPassword)
	gjs, err := jetstream.New(gw)
	if err != nil {
		t.Fatalf("jetstream as gateway: %v", err)
	}
	ref, err := capability.NewMinter(gjs).Mint(ctx, "task-1", capability.Entry{
		Tier:     capability.TierDeveloperTeam,
		Scope:    capability.NamespaceScope("kubeagents-system"),
		Delegate: podA,
	})
	if err != nil {
		t.Fatalf("the gateway could not mint under its rendered grant: %v", err)
	}

	// And the session asks, over its own derived verify subject.
	session, _ := h.connectAs(t, podA, tokenPodA)
	client, err := capability.NewClient(session, podA)
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	if err := client.Check(ctx, ref, capability.VerbTaskExecute,
		capability.NamespaceScope("kubeagents-system")); err != nil {
		t.Fatalf("the session's own capability was refused over the shipped grants: %v", err)
	}

	// A verb the capability does not carry, refused by the verifier rather
	// than by the bus — which is the distinction that matters: the session
	// reached the verifier and got an answer.
	if err := client.Check(ctx, ref, capability.VerbFleetRead,
		capability.NamespaceScope("kubeagents-system")); err == nil {
		t.Error("a developer-team capability authorized a platform verb")
	}

	// The other session's client, holding a reference it could only have
	// obtained by reading somebody else's envelope. It reaches the verifier
	// — it has its own verify subject — and the verifier refuses it,
	// because the entry names podA and the subject says podB.
	other, _ := h.connectAs(t, podB, tokenPodB)
	otherClient, err := capability.NewClient(other, podB)
	if err != nil {
		t.Fatalf("NewClient(podB): %v", err)
	}
	err = otherClient.Check(ctx, ref, capability.VerbTaskExecute,
		capability.NamespaceScope("kubeagents-system"))
	if err == nil {
		t.Fatal("podB used a capability minted for podA")
	}
	if !strings.Contains(err.Error(), capability.WalkRefused) {
		t.Errorf("podB's refusal names something other than the walk: %v", err)
	}

	// And podB cannot ask in podA's name, which is what makes the
	// verifier's subject-derived caller identity sound rather than
	// decorative.
	forged, err := capability.NewClient(other, podA)
	if err != nil {
		t.Fatalf("NewClient(podB-as-podA): %v", err)
	}
	short, shortCancel := context.WithTimeout(ctx, 2*time.Second)
	defer shortCancel()
	if err := forged.Check(short, ref, capability.VerbTaskExecute,
		capability.NamespaceScope("kubeagents-system")); err == nil {
		t.Fatal("podB asked the verifier a question in podA's name and was answered")
	}
	// And it was the bus that refused it, rather than the request going
	// unanswered for any of the other reasons a Check can fail.
	assertTheBusRefusedTheForgedAsk(t, vl, podA)
}

// startHarnessWithServerLogMap is startHarnessWithServerLog with the map and
// tokens chosen by the caller. The original hard-codes the narrowing suite's
// pair; this file needs the operator's render.
func startHarnessWithServerLogMap(t *testing.T, identityMap string, tokens map[string]Attested) (*harness, *violationLog) {
	t.Helper()
	h := startHarness(t, identityMap, tokens)
	vl := &violationLog{lines: make(chan string, 256)}
	h.server.SetLoggerV2(vl, false, false, false)
	return h, vl
}

// startCapabilityVerifier runs a real verifier on the harness, under the
// verifier identity the operator renders, and returns once it is listening.
//
// Two things are load-bearing about doing it this way rather than with a stub.
// The bind is the first: NewStore is a `$JS.API.STREAM.INFO.KV_cap` call, so a
// grant list that is missing it fails here, in the same place and for the same
// reason the Deployment would fail on a cluster. And the subscribe is the
// second: the verifier subscribes `a2a.cap.verify.*`, which is a grant no other
// principal on this bus holds, so a caller reaching it at all is evidence that
// the derived session grants and the rendered verifier grants meet.
//
// Subscribe rather than Serve: it returns after the subscription is
// established on the server, and a request that races the subscription is
// answered by the client's timeout, which every broker reads as a denial.
func startCapabilityVerifier(t *testing.T, ctx context.Context, h *harness) {
	t.Helper()
	nc, _ := h.connectAs(t, "verifier", tokenVerifier)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("jetstream as verifier: %v", err)
	}
	store, err := capability.NewStore(ctx, js)
	if err != nil {
		t.Fatalf("the verifier could not bind the cap bucket under its rendered grants: %v", err)
	}
	svc := &capability.Service{Resolver: &capability.Resolver{Store: store}}
	sub, err := svc.Subscribe(ctx, nc)
	if err != nil {
		t.Fatalf("the verifier could not subscribe under its rendered grants: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

// The browser credential cannot join the verifier's queue group.
//
// This is not a confidentiality test and the distinction is the finding. `web`
// subscribes `a2a.>` -- it is the read surface, and the whole task plane is
// deliberately visible to it -- and `a2a.>` covered `a2a.cap.verify.*`, the
// verifier's REQUEST subject. NATS lets any principal permitted to subscribe
// to a subject join any queue group on it, so the credential published to a
// browser could join `cap-verifier` and take a share of every verify request
// in the install. It could not answer them, holding no publish under
// `a2a.cap.reply.>`; it would simply swallow them, the caller's Check would
// time out, and a timeout is a denial by design. A browser credential would
// have rejected a proportion of every task on the bus.
//
// The deny that closes it is on webIdentity. Asserted here rather than as a
// render assertion because "the config has a deny line" and "the server
// refuses the subscription" are different claims, and only the second one is
// the control.
func TestTheWebCredentialCannotReachTheVerifyPlane(t *testing.T) {
	h, _ := startHarnessWithServerLogMap(t, capMap(t), capTokens())
	nc, violations := connectStatic(t, h, "web", "pw-web")

	if !queueSubscribeRefused(t, nc, violations, capability.VerifySubscribe, capability.VerifyQueue) {
		t.Errorf("web joined queue group %q on %s; it can swallow verify requests and every swallowed one is a task rejected",
			capability.VerifyQueue, capability.VerifySubscribe)
	}
	// The plain subscription too: interception is the sharp end, but there
	// is no reason for this credential to watch the verify plane either.
	if !subscribeRefused(t, nc, violations, capability.VerifySubscribe) {
		t.Errorf("web may subscribe to %s", capability.VerifySubscribe)
	}
	if !subscribeRefused(t, nc, violations, capability.ReplyPrefix+">") {
		t.Errorf("web may subscribe to %s>", capability.ReplyPrefix)
	}

	// The control, and it is the point of the deny being scoped to
	// `a2a.cap.>` rather than wider: this credential is still the read
	// surface. If this half fails, the deny took the product's read
	// surface away rather than one namespace.
	if subscribeRefused(t, nc, violations, "a2a.tasks.>") {
		t.Error("web can no longer subscribe to the task plane; the deny is too wide")
	}
}

// The bridge -- the executor a stock install actually runs -- can verify, and
// cannot ask in anybody else's name.
//
// The operator renders A2A_SPAWN_SESSIONS=true and renders no
// A2A_DEFAULT_ADDRESSEE, so the gateway keeps its own default of `platform`
// and every turn a user types lands on this executor. A session pod is
// reached only by an explicit `delegate:`. That makes this test, not the
// session one above, the one that covers the shipped path.
//
// The identity is the thing worth reading twice. The bridge dials as the
// static `bridge` user and asks as `platform`, because `platform` is what the
// gateway wrote into the capability's delegate field. That is sound only
// because the grant is exactly one subject: the second half of this test is
// the server refusing `bridge` on a session pod's verify token, which is what
// keeps the verifier's subject-derived caller identity from being a
// self-assertion.
func TestTheBridgeVerifiesOverItsShippedGrants(t *testing.T) {
	h, vl := startHarnessWithServerLogMap(t, capMap(t), capTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	provision, _ := h.connectAs(t, "provision", tokenProvision)
	pjs, err := jetstream.New(provision)
	if err != nil {
		t.Fatalf("jetstream as provision: %v", err)
	}
	if _, err := pjs.CreateKeyValue(ctx, jetstream.KeyValueConfig{
		Bucket: capability.Bucket, History: 1,
	}); err != nil {
		t.Fatalf("creating the cap bucket: %v", err)
	}
	startCapabilityVerifier(t, ctx, h)

	gw, _ := connectStatic(t, h, "gateway", renderedGatewayPassword)
	gjs, err := jetstream.New(gw)
	if err != nil {
		t.Fatalf("jetstream as gateway: %v", err)
	}
	// Minted exactly as the gateway mints for a default install: the
	// delegate is the addressee, and the addressee is the bridge's profile.
	ref, err := capability.NewMinter(gjs).Mint(ctx, "task-bridge-1", capability.Entry{
		Tier:     capability.TierDeveloperTeam,
		Scope:    capability.NamespaceScope("kubeagents-system"),
		Delegate: bridgeAddressee,
	})
	if err != nil {
		t.Fatalf("the gateway could not mint for the bridge: %v", err)
	}

	bridge, _ := connectStatic(t, h, "bridge", "pw-bridge")
	client, err := capability.NewClient(bridge, bridgeAddressee)
	if err != nil {
		t.Fatalf("NewClient(bridge): %v", err)
	}
	if err := client.Check(ctx, ref, capability.VerbTaskExecute,
		capability.NamespaceScope("kubeagents-system")); err != nil {
		t.Fatalf("the default install's executor was refused its own task's capability: %v", err)
	}

	// A verb the capability does not carry: refused by the verifier, not by
	// the bus, which is what proves the round trip happened.
	if err := client.Check(ctx, ref, capability.VerbFleetRead,
		capability.NamespaceScope("kubeagents-system")); err == nil {
		t.Error("a developer-team capability authorized a platform verb for the bridge")
	}

	// A capability minted for a session pod, presented by the bridge. The
	// entry names podA and the subject says platform, so the walk refuses.
	sessionRef, err := capability.NewMinter(gjs).Mint(ctx, "task-bridge-2", capability.Entry{
		Tier:     capability.TierDeveloperTeam,
		Scope:    capability.NamespaceScope("kubeagents-system"),
		Delegate: podA,
	})
	if err != nil {
		t.Fatalf("minting a session's capability: %v", err)
	}
	if err := client.Check(ctx, sessionRef, capability.VerbTaskExecute,
		capability.NamespaceScope("kubeagents-system")); err == nil {
		t.Error("the bridge used a capability minted for a session pod")
	}

	// And it cannot ask in that pod's name. This is a permissions violation
	// rather than a verifier refusal, and the difference is where the
	// evidence lives: the request DOES leave the client -- PublishRequest
	// queues the line and returns nil -- and is dropped at the server, which
	// logs the violation and tells the caller nothing. So the Check below
	// fails by timing out, which is also how it would fail if the verifier
	// were simply not running, and the server log is the only place the two
	// are distinguishable.
	forged, err := capability.NewClient(bridge, podA)
	if err != nil {
		t.Fatalf("NewClient(bridge-as-podA): %v", err)
	}
	short, shortCancel := context.WithTimeout(ctx, 2*time.Second)
	defer shortCancel()
	if err := forged.Check(short, sessionRef, capability.VerbTaskExecute,
		capability.NamespaceScope("kubeagents-system")); err == nil {
		t.Fatal("the bridge asked the verifier a question in a session pod's name and was answered")
	}
	assertTheBusRefusedTheForgedAsk(t, vl, podA)

	// A scope outside the pod's own namespace, refused: the executor is
	// checked at where it runs, not merely at whether it holds anything.
	if err := client.Check(ctx, ref, capability.VerbTaskExecute,
		capability.NamespaceScope("kube-system")); err == nil {
		t.Error("the bridge executed at a scope its capability does not contain")
	}
}

// bridgeAddressee is the addressee the bridge executes for, which is also the
// token on its verify subject and the delegate the gateway mints. The operator
// spells it a2aBridgeAddressee and the bridge spells it defaultProfile; a
// fourth spelling here is deliberate, because a test that imported one of them
// could not catch the two disagreeing.
const bridgeAddressee = "platform"

// queueSubscribeRefused is subscribeRefused for a queue subscription. It is a
// separate helper because it is a separate permission question: NATS checks
// the subject for both, but a queue group is a claim on OTHER subscribers'
// traffic, and a principal that may watch a subject can also steal from it.
func queueSubscribeRefused(t *testing.T, nc *nats.Conn, violations chan error, subject, queue string) bool {
	t.Helper()
	sub, err := nc.QueueSubscribeSync(subject, queue)
	if err != nil {
		t.Fatalf("QueueSubscribeSync(%s, %s) returned a synchronous error: %v", subject, queue, err)
	}
	defer func() { _ = sub.Unsubscribe() }()
	if err := nc.Flush(); err != nil {
		t.Fatalf("Flush after queue subscribing to %s: %v", subject, err)
	}
	select {
	case e := <-violations:
		if !strings.Contains(e.Error(), "ermissions") {
			t.Fatalf("unexpected async error queue subscribing to %s: %v", subject, e)
		}
		return true
	case <-time.After(500 * time.Millisecond):
		return false
	}
}

// assertTheBusRefusedTheForgedAsk is the evidence half of "podB cannot ask in
// podA's name", and it exists because the client-side half of that claim is
// unfalsifiable on its own.
//
// Check returns a non-nil error for every reason there is -- refused by the
// verifier, malformed answer, verifier down, verifier never started, harness
// misconfigured, subject misspelled. Asserting `err != nil` after a 2s deadline
// therefore passes whether the bus refused the impersonation or the test simply
// asked a question nobody was listening for. The two are indistinguishable from
// the caller, on purpose: NATS reports a permissions violation to the SERVER
// log and to the connection's async error handler, never as an error from
// SubscribeSync or PublishRequest, both of which return nil after queueing a
// protocol line. The request is dropped at the server, so the caller's only
// observation is silence, and silence is what a broken test looks like too.
//
// Two violations, not one, because Check touches two subjects and the grants
// deny both: it subscribes the reply subject before it publishes the request.
// Requiring both is what distinguishes a refusal from a race -- a client that
// died before publishing would produce only the first.
func assertTheBusRefusedTheForgedAsk(t *testing.T, vl *violationLog, impersonated string) {
	t.Helper()

	verify, err := capability.VerifySubject(impersonated)
	if err != nil {
		t.Fatalf("VerifySubject(%q): %v", impersonated, err)
	}
	// Each needle is a pair: the direction nats-server names and the subject
	// it names it for, both required on the same line. The subject half
	// carries the `Subject "` the server prints, so a name that is a prefix
	// of another principal's cannot satisfy the wrong one -- and it closes
	// with the quote for publish, because the verify subject is whole, but
	// with the dot that precedes the nuid ReplySubject appends per request
	// for subscribe, which is also why `reply` above cannot be compared for
	// equality: a second call would name a different subject.
	//
	// The direction half is what stops one refusal from satisfying both
	// needles. Without it a publish violation on the reply subject -- which
	// a client that subscribed fine and then misdirected its request would
	// produce -- reads as proof that the subscribe was refused too.
	// Matched as a separate substring rather than folded into one literal
	// because the server moved the principal in and out of that position
	// between v2.10 and v2.14; the direction word and the subject are stable,
	// the text between them is not.
	type needle struct{ direction, subject string }
	want := map[needle]bool{
		{"Publish Violation", `Subject "` + verify + `"`}:                                     false,
		{"Subscription Violation", `Subject "` + capability.ReplyPrefix + impersonated + "."}: false,
	}
	var seen []string
	deadline := time.After(5 * time.Second)
	for {
		outstanding := 0
		for _, got := range want {
			if !got {
				outstanding++
			}
		}
		if outstanding == 0 {
			return
		}
		select {
		case line := <-vl.lines:
			seen = append(seen, line)
			if !strings.Contains(line, "Violation") {
				continue
			}
			for n := range want {
				if strings.Contains(line, n.direction) && strings.Contains(line, n.subject) {
					want[n] = true
				}
			}
		case <-deadline:
			for n, got := range want {
				if !got {
					t.Errorf("the server logged no %s naming %s: the forged ask was "+
						"not refused by the bus, so the Check above failed for some other reason and "+
						"proves nothing. Violations seen: %q", n.direction, n.subject, seen)
				}
			}
			return
		}
	}
}
