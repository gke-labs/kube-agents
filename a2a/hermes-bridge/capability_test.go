package hermesbridge

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

// The default install's executor, from the attacker's side.
//
// The session executor's suite (a2a/worker-adapter/capability_test.go) covers
// the same ground for the `delegate:` route. Both exist because both run: the
// operator renders no A2A_DEFAULT_ADDRESSEE, so an unqualified task reaches
// this bridge and nothing else. A refusal proven only on the route nobody
// takes by default is not a control.
//
// Every test here runs the real verifier against the real bucket (startServer
// arms both) and gives the bridge a command that cannot exist, so a task that
// gets past the check fails as something other than rejected. That is how
// these tests tell "refused" from "ran and then failed".

// noCommand is a hermes path that cannot exist.
var noCommand = []string{"/nonexistent/hermes"}

// submitWithAuthority publishes a submission carrying an authority block the
// caller chose, rather than the honest one submit() mints.
func submitWithAuthority(t *testing.T, c *lib.Client, taskID, prompt string, authority json.RawMessage) {
	t.Helper()
	contextID := "ctx-" + taskID
	opts := []lib.EnvelopeOption{lib.WithTo(lib.Party{Session: "platform"})}
	if authority != nil {
		opts = append(opts, lib.WithAuthority(authority))
	}
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		messagePayload(t, taskID, contextID, prompt), opts...)
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", taskID), env); err != nil {
		t.Fatalf("submission publish: %v", err)
	}
}

// terminalText pulls the message text off the final event, which is where a
// supervisor and a human both read the reason.
func terminalText(t *testing.T, url, taskID string) string {
	t.Helper()
	var last string
	for _, env := range replayEvents(t, url, taskID) {
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

// refuseAndFold asserts the task ended terminal rejected on the stream with a
// reason naming the capability, and that no subprocess ever started.
func refuseAndFold(t *testing.T, url string, c *lib.Client, taskID string) {
	t.Helper()
	task := waitTerminal(t, c, taskID)
	if task.State != lib.StateRejected {
		t.Fatalf("state = %s, want rejected; the task was not refused", task.State)
	}
	text := terminalText(t, url, taskID)
	if !strings.Contains(text, "capability-refused") {
		t.Fatalf("the terminal event does not say why: %q", text)
	}
	for _, env := range replayEvents(t, url, taskID) {
		if state, _ := statusState(t, env); state == lib.StateWorking {
			t.Fatalf("the bridge published working for a task it refused")
		}
	}
}

// A capability that exists, is well-formed, and names somebody else as its
// delegate. Possession of the reference is not the test: it travels on a bus
// other principals read.
func TestTheBridgeRefusesACapabilityMintedForAnotherDelegate(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-otherdelegate"
	ref := mintFor(t, c, taskID, "chat-vole-somebody-else")
	submitWithAuthority(t, c, taskID, "do the thing", authorityFor(t, ref))
	refuseAndFold(t, url, c, taskID)
}

// A forged reference: a key the gateway never wrote, at a revision invented to
// look plausible.
func TestTheBridgeRefusesAForgedCapabilityReference(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-forged"
	submitWithAuthority(t, c, taskID, "do the thing",
		authorityFor(t, capability.Ref{Key: "root." + taskID + "-forged", Revision: 1}))
	refuseAndFold(t, url, c, taskID)
}

// A reference to a real, resolvable capability at the wrong revision. The pin
// is what makes an overwritten entry stop resolving, and the bridge inherits
// that only if it passes the revision through rather than looking the key up
// live.
func TestTheBridgeRefusesAReferenceAtTheWrongRevision(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-badrev"
	ref := mintFor(t, c, taskID, "platform")
	ref.Revision++
	submitWithAuthority(t, c, taskID, "do the thing", authorityFor(t, ref))
	refuseAndFold(t, url, c, taskID)
}

// An unpinned reference. A gateway that shipped `revision: 0` would be handing
// the verifier a key to look up live, which is the whole overwrite hole.
func TestTheBridgeRefusesAnUnpinnedReference(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-unpinned"
	ref := mintFor(t, c, taskID, "platform")
	ref.Revision = 0
	submitWithAuthority(t, c, taskID, "do the thing", authorityFor(t, ref))
	refuseAndFold(t, url, c, taskID)
}

// No capability at all, which is the pre-A3b gateway's envelope. This is the
// one that matters most here: the bridge is a sidecar the operator renders no
// environment for, so its default has to be the enforcing one with nothing
// configured.
func TestTheBridgeRefusesASubmissionWithNoCapabilityByDefault(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-null"
	submitWithAuthority(t, c, taskID, "do the thing", json.RawMessage(`{"grants":null}`))
	refuseAndFold(t, url, c, taskID)
}

// The zero value of the knob is the enforcing one, asserted on the Config the
// binary actually builds rather than on the tests' own.
func TestTheBridgesDefaultIsToRequireACapability(t *testing.T) {
	if (Config{}).CapabilityOptional {
		t.Fatal("the zero value must be the enforcing one: an unconfigured sidecar must refuse, not execute")
	}
}

// ...and the mixed-version window, which is the only thing the knob buys: a
// gateway that predates the mint keeps working against a new bridge.
func TestTheMixedVersionKnobLetsAnUncapabledSubmissionThroughTheBridge(t *testing.T) {
	_, url := startServer(t)
	startBridgeOptional(t, url, script(t, `echo '{"type":"result","subtype":"success","result":"done"}'`))
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-optional"
	submitWithAuthority(t, c, taskID, "do the thing", nil)
	if task := waitTerminal(t, c, taskID); task.State != lib.StateCompleted {
		t.Fatalf("state = %s, want completed; the rollout window does not work", task.State)
	}
}

// The knob relaxes a MISSING capability and nothing else. An attacker who can
// set one environment variable still cannot run a refused task.
func TestTheMixedVersionKnobDoesNotRelaxAPresentCapabilityAtTheBridge(t *testing.T) {
	_, url := startServer(t)
	startBridgeOptional(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-optional-present"
	submitWithAuthority(t, c, taskID, "do the thing",
		authorityFor(t, capability.Ref{Key: "root." + taskID + "-forged", Revision: 1}))
	refuseAndFold(t, url, c, taskID)
}

// An authority block that does not parse. Not a rollout state - a block is
// either absent or well-formed - so it is a refusal rather than a relaxation,
// and it stays one with the knob set.
func TestAMalformedAuthorityBlockIsRefusedByTheBridgeEvenWithTheKnobSet(t *testing.T) {
	_, url := startServer(t)
	startBridgeOptional(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-malformed"
	submitWithAuthority(t, c, taskID, "do the thing",
		json.RawMessage(`{"grants":{"capability":"not-an-object"}}`))
	refuseAndFold(t, url, c, taskID)
}

// The refusal names the rule and never anything the caller supplied. A bridge
// that echoed the key back would confirm which task ids are live to anyone who
// can name one.
func TestTheBridgesRefusalQuotesNothingTheCallerSupplied(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-oracle"
	const marker = "root.task-bridge-cap-oracle-forged"
	submitWithAuthority(t, c, taskID, "do the thing",
		authorityFor(t, capability.Ref{Key: marker, Revision: 77}))
	refuseAndFold(t, url, c, taskID)
	if text := terminalText(t, url, taskID); strings.Contains(text, marker) || strings.Contains(text, "77") {
		t.Fatalf("the refusal quoted the caller's own reference back: %q", text)
	}
}

// A capability that exists but names somebody else, and one that does not
// exist at all, must be indistinguishable.
func TestAtTheBridgeAMissingCapabilityAndSomebodyElsesAreIndistinguishable(t *testing.T) {
	_, url := startServer(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskA = "task-bridge-cap-absent"
	submitWithAuthority(t, c, taskA, "go",
		authorityFor(t, capability.Ref{Key: "root." + taskA + "-nope", Revision: 1}))
	refuseAndFold(t, url, c, taskA)

	const taskB = "task-bridge-cap-present"
	submitWithAuthority(t, c, taskB, "go",
		authorityFor(t, mintFor(t, c, taskB, "chat-vole-somebody-else")))
	refuseAndFold(t, url, c, taskB)

	if a, b := terminalText(t, url, taskA), terminalText(t, url, taskB); a != b {
		t.Fatalf("the two refusals differ, which tells a caller whether the key existed:\n  absent  %q\n  present %q", a, b)
	}
}

// A verifier that is not there refuses the task. The alternative - execute
// when the thing that says yes cannot be reached - makes the verifier's
// availability the attacker's target rather than the operator's. "Fails
// closed" is a claim about behaviour under an outage, and nothing but an
// outage demonstrates it.
func TestAVerifierThatCannotBeReachedRefusesTheBridgesTask(t *testing.T) {
	_, url := startServerNoVerifier(t)
	startBridge(t, url, noCommand)
	c := gatewayClient(t, url)

	const taskID = "task-bridge-cap-outage"
	submitWithAuthority(t, c, taskID, "do the thing",
		authorityFor(t, capability.Ref{Key: "root." + taskID, Revision: 1}))
	refuseAndFold(t, url, c, taskID)
}

// The bridge going away is not the capability's fault.
//
// capability.Client.Check turns any error out of NextMsgWithContext into a
// refusal, context.Canceled included, and accept passes it the bridge's own
// Run context. So a submission whose check is still in flight when the bridge
// is terminated used to land as terminal `rejected` with a capability reason:
// a task nothing was wrong with, blamed on its authority, and - because
// rejected is terminal and no supervisor retries it - not run again. Every
// other pending task gets the retryable bridge-shutdown instead.
//
// This is the client-side twin of what capability.DrainAndCancel guards on the
// verifier: "the store is unreachable" and "this process is going away" are
// different facts, and the fail-closed branch cannot tell them apart on its
// own. The verifier was taught the difference; the two callers were not.
func TestTheBridgeShuttingDownMidVerifyIsNotACapabilityRefusal(t *testing.T) {
	_, url := startServerNoVerifier(t)
	// A verifier that is subscribed but never answers: the only shape that
	// leaves a window to shut down inside. With nothing subscribed the bus
	// answers no-responders at once and Check returns before a shutdown
	// could overlap it -- that outage is
	// TestAVerifierThatCannotBeReachedRefusesTheBridgesTask, and refusing
	// there is correct.
	silentVerifier(t, url)
	c := gatewayClient(t, url)

	// accept and one worker are driven directly rather than through Run,
	// and that is the point of the test rather than a shortcut. Run's
	// shutdownTasks finalizes the same registered run with bridge-shutdown,
	// and finalize is first-wins, so end to end the two racing writers
	// usually produce the right answer anyway and the bug hides. Here the
	// worker is the only writer, which is the case this branch exists for.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	b, err := New(ctx, Config{
		NATSURL:      url,
		Command:      noCommand,
		TaskDeadline: 20 * time.Second,
		KillGrace:    500 * time.Millisecond,
		Scope:        capability.NamespaceScope(""),
	})
	if err != nil {
		t.Fatalf("bridge new: %v", err)
	}
	t.Cleanup(b.close)

	const taskID = "task-bridge-cap-shutdown"
	contextID := "ctx-" + taskID
	env, err := lib.NewMessageEnvelope(gatewayParty, taskID, contextID, "corr-"+taskID,
		messagePayload(t, taskID, contextID, "do the thing"),
		lib.WithTo(lib.Party{Session: "platform"}),
		lib.WithAuthority(authorityFor(t, capability.Ref{Key: "root." + taskID, Revision: 1})))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}

	// accept publishes submitted and queues; the worker then blocks in
	// Check for its full 5s against the silent verifier, so cancelling
	// shortly after lands inside that window with a wide margin.
	b.accept(ctx, env)
	b.wg.Add(1)
	go b.worker(ctx)
	go func() {
		time.Sleep(250 * time.Millisecond)
		cancel()
	}()

	task := waitTerminal(t, c, taskID)
	if task.State != lib.StateFailed {
		t.Errorf("state = %s, want failed; a shutdown is not a refusal", task.State)
	}
	text := terminalText(t, url, taskID)
	if strings.Contains(text, "capability-refused") {
		t.Errorf("the bridge blamed the capability for its own shutdown: %q", text)
	}
	if !strings.Contains(text, "bridge-shutdown") {
		t.Errorf("the terminal event does not name the shutdown: %q", text)
	}
}

// silentVerifier holds the verify subject without ever replying, so a Check
// against it blocks until its own timeout or until the caller's context is
// cancelled. It stands in for a verifier that is up, reachable and wedged.
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

// A verifier outage must not make a cancel wait behind the submissions it is
// refusing. The bridge has ONE durable consumer on `a2a.tasks.platform.*.in`
// and it delivers serially, so whatever the submission path does on that
// callback, a KindCancel envelope behind it does not get looked at until it
// returns. Check blocks for capability.DefaultTimeout against a verifier that
// does not answer; done inline that is 5s of head-of-line block per pending
// submission, and a user trying to stop a task cannot, because of an outage
// in the component that authorizes new ones. Hence capabilityPermits on the
// worker.
//
// Three submissions and then a cancel, all published before the bridge is
// reading, so stream order puts the cancel last -- the worst case, and the
// only one worth pinning. The assertion is on the clock as well as the state:
// with the check back on the consumer this is three full timeouts (~15s) and
// the third task ends `rejected` rather than `canceled`.
func TestAVerifierOutageDoesNotDelayACancel(t *testing.T) {
	_, url := startServerNoVerifier(t)
	silentVerifier(t, url)
	c := gatewayClient(t, url)

	const doomed = "task-cap-hol-3"
	submit(t, c, "task-cap-hol-1", "do the thing")
	submit(t, c, "task-cap-hol-2", "do the thing")
	origin := submit(t, c, doomed, "do the thing")
	cancelEnv, err := lib.NewCancelEnvelope(gatewayParty, origin.TaskID, origin.ContextID,
		origin.CorrelationID, lib.WithTo(lib.Party{Session: "platform"}))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Publish(testCtx(t), lib.TaskInSubject("platform", doomed), cancelEnv); err != nil {
		t.Fatal(err)
	}

	// Concurrency 1: one worker means the two submissions ahead of it hold
	// the verify slot for a full timeout each, so the queued third cannot
	// be picked up and flipped to running underneath the cancel.
	start := time.Now()
	_ = startBridgeCfg(t, url, noCommand, 1, false)

	task := waitTerminal(t, c, doomed)
	elapsed := time.Since(start)
	if task.State != lib.StateCanceled {
		t.Errorf("state = %s, want canceled; the cancel was overtaken by the refusals ahead of it",
			task.State)
	}
	// One timeout's worth of margin over the ~0s this should take, and still
	// far inside the two timeouts the inline check would have cost.
	if elapsed > capability.DefaultTimeout {
		t.Errorf("the cancel took %s to reach a terminal; it queued behind the verifier outage", elapsed)
	}
	if text := terminalText(t, url, doomed); !strings.Contains(text, "canceled-before-start") {
		t.Errorf("the terminal event is not the cancel's: %q", text)
	}
}
