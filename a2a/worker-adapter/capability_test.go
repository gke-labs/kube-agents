package workeradapter

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The executor's half of the capability envelope, from the attacker's side.
//
// Every test here runs the real verifier against the real bucket (startServer
// arms both) and points the adapter at a harness path that cannot exist, so a
// task that starts fails loudly and cannot be mistaken for a task that was
// refused. The assertion is always the same shape: terminal rejected, on the
// stream, folded — a refusal an operator and a supervisor can both see, not a
// log line.

// noHarness is a harness path that cannot exist. A test that gets past the
// capability check spawns it and fails with something other than rejected,
// which is how these tests tell "refused" from "ran and then failed".
var noHarness = []string{"/nonexistent/harness"}

// refuseAndFold runs the adapter and asserts the task ended rejected on the
// stream with a reason naming the capability.
func refuseAndFold(t *testing.T, url string, c *lib.Client, session, taskID string, cfg Config) *lib.Task {
	t.Helper()
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.res.State != lib.StateRejected {
		t.Fatalf("state %q err %v; the task was not refused", out.res.State, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if task.State != lib.StateRejected || !task.Final {
		t.Fatalf("the refusal is not terminal on the stream: %+v", task)
	}
	text := terminalText(t, url, session, taskID)
	if !strings.Contains(text, "capability-refused") {
		t.Fatalf("the terminal event does not say why: %q", text)
	}
	return task
}

// terminalText pulls the message text off the final event, which is where a
// supervisor and a human both read the reason.
func terminalText(t *testing.T, url, addressee, taskID string) string {
	t.Helper()
	var last string
	for _, env := range replayEvents(t, url, addressee, taskID) {
		var upd lib.StatusUpdate
		if err := json.Unmarshal(env.Payload, &upd); err != nil {
			continue
		}
		if upd.Status.Message == nil {
			continue
		}
		for _, p := range upd.Status.Message.Parts {
			if p.Kind == "text" {
				last = p.Text
			}
		}
	}
	return last
}

// The DoD's first refusal: a verb the capability does not permit. The
// executor's scope is a namespace the capability was never issued for, so
// task.execute there is outside it — the same shape a hop that narrowed
// elsewhere would present.
func TestAnExecutorRefusesAVerbOutsideTheCapabilitysScope(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap1", "task-cap-scope"
	submit(t, c, session, taskID, "do the thing")

	cfg := adapterConfig(url, taskID, session, noHarness)
	// The capability was minted at namespace/-; this pod claims to run in
	// a different namespace, so the verb is outside its scope.
	cfg.Scope = capability.NamespaceScope("some-other-namespace")
	refuseAndFold(t, url, c, session, taskID, cfg)
}

// A capability that exists, is well-formed, and belongs to somebody else.
// Possession of the reference is not the test: it travels on a bus other
// principals read.
func TestAnExecutorCannotUseACapabilityMintedForAnotherPrincipal(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap2", "task-cap-otherdelegate"
	ref := mintFor(t, c, taskID, "chat-vole-somebody-else")
	submitWithAuthority(t, c, session, taskID, "do the thing", authorityFor(t, ref))

	refuseAndFold(t, url, c, session, taskID, adapterConfig(url, taskID, session, noHarness))
}

// A forged reference: a key the gateway never wrote, at a revision invented
// to look plausible. This is the DoD's "a forged authority block from a
// principal whose grants do not match is caught".
func TestAForgedCapabilityReferenceIsRefused(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap3", "task-cap-forged"
	forged := capability.Ref{Key: "root." + taskID + "-forged", Revision: 1}
	submitWithAuthority(t, c, session, taskID, "do the thing", authorityFor(t, forged))

	refuseAndFold(t, url, c, session, taskID, adapterConfig(url, taskID, session, noHarness))
}

// A reference to a real, resolvable capability at the wrong revision. The pin
// is what makes an overwritten or recreated entry stop resolving, and the
// executor inherits that for free — but only if it passes the revision
// through rather than looking the key up live.
func TestAReferenceAtTheWrongRevisionIsRefused(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap4", "task-cap-badrev"
	ref := mintFor(t, c, taskID, session)
	ref.Revision++
	submitWithAuthority(t, c, session, taskID, "do the thing", authorityFor(t, ref))

	refuseAndFold(t, url, c, session, taskID, adapterConfig(url, taskID, session, noHarness))
}

// An unpinned reference. A gateway that shipped `revision: 0` would be
// handing the verifier a key to look up live, which is the whole overwrite
// hole; the executor refuses rather than letting it through.
func TestAnUnpinnedReferenceIsRefused(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap5", "task-cap-unpinned"
	ref := mintFor(t, c, taskID, session)
	ref.Revision = 0
	submitWithAuthority(t, c, session, taskID, "do the thing", authorityFor(t, ref))

	refuseAndFold(t, url, c, session, taskID, adapterConfig(url, taskID, session, noHarness))
}

// No capability at all, which is the pre-A3b gateway's envelope. The default
// refuses it: an executor that ran unauthorized work whenever the field was
// missing would be one absent field away from no control at all.
func TestASubmissionWithNoCapabilityIsRefusedByDefault(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap6", "task-cap-null"
	submitWithAuthority(t, c, session, taskID, "do the thing", json.RawMessage(`{"grants":null}`))

	cfg := adapterConfig(url, taskID, session, noHarness)
	if cfg.CapabilityOptional {
		t.Fatal("the zero value must be the enforcing one")
	}
	refuseAndFold(t, url, c, session, taskID, cfg)
}

// ...and the mixed-version window, which is the only thing the knob buys. A
// gateway that predates the mint keeps working against a new executor.
func TestTheMixedVersionKnobLetsAnUncapabledSubmissionThrough(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap7", "task-cap-optional"
	submitWithAuthority(t, c, session, taskID, "do the thing", nil)

	cfg := adapterConfig(url, taskID, session,
		stub(t, `echo '{"type":"result","subtype":"success","result":"done"}'`))
	cfg.CapabilityOptional = true
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.res.State != lib.StateCompleted {
		t.Fatalf("state %q err %v; the rollout window does not work", out.res.State, out.err)
	}
}

// The knob relaxes a MISSING capability and nothing else. A capability that
// is present is checked whatever the configuration says, so an attacker who
// can set one environment variable still cannot run a refused verb.
func TestTheMixedVersionKnobDoesNotRelaxAPresentCapability(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap8", "task-cap-optional-present"
	forged := capability.Ref{Key: "root." + taskID + "-forged", Revision: 1}
	submitWithAuthority(t, c, session, taskID, "do the thing", authorityFor(t, forged))

	cfg := adapterConfig(url, taskID, session, noHarness)
	cfg.CapabilityOptional = true
	refuseAndFold(t, url, c, session, taskID, cfg)
}

// An authority block that does not parse. Not a rollout state — a block is
// either absent or well-formed — so it is a refusal rather than a relaxation,
// and it stays one with the knob set.
func TestAMalformedAuthorityBlockIsRefusedEvenWithTheKnobSet(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap9", "task-cap-malformed"
	submitWithAuthority(t, c, session, taskID, "do the thing",
		json.RawMessage(`{"grants":{"capability":"not-an-object"}}`))

	cfg := adapterConfig(url, taskID, session, noHarness)
	cfg.CapabilityOptional = true
	refuseAndFold(t, url, c, session, taskID, cfg)
}

// Authorization is answered before content. An unauthorized caller must not
// learn anything about how its submission was read — including that the
// prompt was empty, which is the one other pre-spend refusal this executor
// has.
func TestAnUnauthorizedTaskIsRefusedBeforeItsContentIsJudged(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap10", "task-cap-order"
	forged := capability.Ref{Key: "root." + taskID + "-forged", Revision: 1}
	submitWithAuthority(t, c, session, taskID, "", authorityFor(t, forged)) // data-only parts

	refuseAndFold(t, url, c, session, taskID, adapterConfig(url, taskID, session, noHarness))
	if text := terminalText(t, url, session, taskID); strings.Contains(text, "no text parts") {
		t.Fatalf("the refusal reported the content check to a caller that was never authorized: %q", text)
	}
}

// The refusal names the rule and never anything the caller supplied. A
// verifier that echoed the key back would confirm which request ids are live
// to anyone who can name one.
func TestTheRefusalQuotesNothingTheCallerSupplied(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap11", "task-cap-oracle"
	const marker = "root.task-cap-oracle-forged"
	submitWithAuthority(t, c, session, taskID, "do the thing",
		authorityFor(t, capability.Ref{Key: marker, Revision: 77}))

	refuseAndFold(t, url, c, session, taskID, adapterConfig(url, taskID, session, noHarness))
	text := terminalText(t, url, session, taskID)
	if strings.Contains(text, marker) || strings.Contains(text, "77") {
		t.Fatalf("the refusal quoted the caller's own reference back: %q", text)
	}
}

// A capability that exists but names nobody this executor is, and one that
// does not exist at all, must be indistinguishable. Otherwise the executor is
// an oracle for which request ids are live.
func TestAMissingCapabilityAndSomebodyElsesAreIndistinguishable(t *testing.T) {
	url := startServer(t)
	c := testClient(t, url)

	const sessionA, taskA = "chat-ibex-cap12", "task-cap-absent"
	submitWithAuthority(t, c, sessionA, taskA, "go",
		authorityFor(t, capability.Ref{Key: "root." + taskA + "-nope", Revision: 1}))
	refuseAndFold(t, url, c, sessionA, taskA, adapterConfig(url, taskA, sessionA, noHarness))

	const sessionB, taskB = "chat-ibex-cap13", "task-cap-present"
	submitWithAuthority(t, c, sessionB, taskB, "go",
		authorityFor(t, mintFor(t, c, taskB, "chat-vole-somebody-else")))
	refuseAndFold(t, url, c, sessionB, taskB, adapterConfig(url, taskB, sessionB, noHarness))

	if a, b := terminalText(t, url, sessionA, taskA), terminalText(t, url, sessionB, taskB); a != b {
		t.Fatalf("the two refusals differ, which tells a caller whether the key existed:\n  absent  %q\n  present %q", a, b)
	}
}

// A verifier that is not there refuses the task. This is the cost of the
// control and it is deliberate: the alternative — execute when the thing that
// says yes cannot be reached — makes the verifier's availability the
// attacker's target rather than the operator's. It belongs in the runbook,
// and it belongs in a test, because "fails closed" is a claim about behaviour
// under an outage and nothing else demonstrates it.
func TestAVerifierThatCannotBeReachedRefusesTheTask(t *testing.T) {
	url := startServerNoVerifier(t)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap14", "task-cap-outage"
	submit(t, c, session, taskID, "do the thing")

	cfg := adapterConfig(url, taskID, session, noHarness)
	// The default is five seconds; the task is refused either way, but the
	// suite should not wait for it.
	out := waitOutcome(t, runAdapter(context.Background(), cfg), 30*time.Second)
	if out.res.State != lib.StateRejected {
		t.Fatalf("state %q err %v; an unreachable verifier did not fail closed", out.res.State, out.err)
	}
	if task := foldTask(t, c, session, taskID); task.State != lib.StateRejected || !task.Final {
		t.Fatalf("the refusal is not terminal on the stream: %+v", task)
	}
}

// silentVerifier subscribes to the verify subject and never answers. Without
// it there is no window to cancel inside: core NATS answers a request with no
// responder immediately, so "the verifier is absent" and "the verifier is
// wedged" are different fixtures and only the second one holds the check open.
func silentVerifier(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url, nats.Name("cap-verifier-silent"))
	if err != nil {
		t.Fatalf("silent verifier connect: %v", err)
	}
	t.Cleanup(nc.Close)
	sub, err := nc.QueueSubscribe(capability.VerifySubscribe, capability.VerifyQueue,
		func(*nats.Msg) {})
	if err != nil {
		t.Fatalf("silent verifier subscribe: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

// A SIGTERM inside the verify window is an eviction, not a refusal. The
// executor's context is what the kubelet cancels, and it is also the context
// Check waits on, so cancellation arrives at the call site wearing the same
// clothes as an unreachable verifier: a non-empty refusal reason. Publishing
// that as terminal `rejected` blames the task's capability for the cluster
// taking its pod away, and rejected is final -- nothing retries it, and an
// operator reading the fold sees an authorization failure that never
// happened. The eviction branch in the run loop already draws this line; the
// verify window was simply on the wrong side of it.
func TestASIGTERMInsideTheVerifyWindowIsAnEvictionNotARefusal(t *testing.T) {
	url := startServerNoVerifier(t)
	silentVerifier(t, url)
	c := testClient(t, url)
	const session, taskID = "chat-ibex-cap15", "task-cap-evicted-midverify"
	submit(t, c, session, taskID, "do the thing")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := runAdapter(ctx, adapterConfig(url, taskID, session, noHarness))

	// Cancel on the state, not on a clock: `submitted` on the stream means
	// the adapter is past every step before the check and into the window
	// the silent verifier is holding open.
	waitState(t, c, session, taskID, lib.StateSubmitted)
	cancel()

	out := waitOutcome(t, done, 30*time.Second)
	if out.res.State != lib.StateFailed || !out.res.Evicted {
		t.Fatalf("state %q evicted %v err %v; a shutdown mid-verify was not reported as an eviction",
			out.res.State, out.res.Evicted, out.err)
	}
	task := foldTask(t, c, session, taskID)
	if task.State != lib.StateFailed || !task.Final {
		t.Fatalf("the eviction is not terminal on the stream: %+v", task)
	}
	text := terminalText(t, url, session, taskID)
	if !strings.Contains(text, "worker-evicted") {
		t.Fatalf("the terminal event does not name the eviction: %q", text)
	}
	if strings.Contains(text, "capability-refused") {
		t.Fatalf("the eviction was published as a capability refusal: %q", text)
	}
}
