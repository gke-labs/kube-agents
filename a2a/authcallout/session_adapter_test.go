package authcallout

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// The real worker adapter, running a whole task, under the real callout, with
// nothing but its own session's grants.
//
// The grant tests next door assert subject by subject that a session may do
// what the adapter needs. That is an enumeration of what somebody believed the
// adapter does. This runs the adapter — the actual Run loop, an actual harness
// subprocess, actual named consumers, an actual terminal event — against a
// server that will refuse anything the enumeration got wrong. The two failure
// directions it catches are the ones a grant list cannot:
//
//   - a grant that is missing, which no negative test finds because nobody
//     thought to assert the thing the adapter quietly needs;
//   - a grant that is present but unusable, because the client library asks
//     for it in a shape the permission does not match. That is not
//     hypothetical here: ordered consumers were exactly this, granted in
//     spirit and unnameable in fact.
//
// It lives in this package rather than in worker-adapter because the harness
// is here: the real rendered nats.conf, the real map, the real callout. A
// second copy over there would be a second thing to keep in step.
func TestASessionAdapterRunsAWholeTaskUnderItsOwnGrants(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	provisionTasksStream(t, h)
	const taskID = "task-e2e-1"
	submitAs(t, h, podA, taskID, "do the thing")

	res, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:      h.url,
		BusTokenFile: tokenFile(t, tokenPodA),
		PodName:      podA,
		TaskID:       taskID,
		Profile:      "chat",
		Session:      podA,
		HarnessCommand: harnessStub(t, `
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"working on it"}]}}'
echo '{"type":"result","subtype":"success","result":"the thing is done"}'
`),
		HarnessEnv:   os.Environ(),
		TaskDeadline: 30 * time.Second,
		KillGrace:    time.Second,
	})
	if err != nil {
		t.Fatalf("the adapter could not complete a task under its own session grants: %v", err)
	}
	if res.State != lib.StateCompleted {
		t.Fatalf("terminal state = %q, want completed", res.State)
	}

	// And the events actually landed on the bus, read back by a principal
	// that is allowed to look. Run returning completed only says the adapter
	// believes it published.
	events := readEvents(t, h, podA, taskID)
	if len(events) == 0 {
		t.Fatal("no events on the session's own subject; the adapter reported success and published nothing")
	}
	var sawFinal bool
	for _, e := range events {
		if e.Kind == lib.KindStatusUpdate && strings.Contains(string(e.Payload), `"final":true`) {
			sawFinal = true
		}
	}
	if !sawFinal {
		t.Error("no final status event on the session's own subject")
	}

	// The consumers it created are named the way the grant expects, which is
	// the contract lib.SessionConsumerName exists to hold. A library that
	// quietly renamed them would still pass the run above only until the
	// grants tightened.
	names := consumerNames(t, h)
	for _, role := range lib.SessionConsumerRoles {
		want := lib.SessionConsumerName(podA, role)
		if role == lib.SessionConsumerEvents {
			// Deleted as soon as the respawn check is done, so it is
			// legitimately absent by now.
			continue
		}
		if !containsString(names, want) {
			t.Errorf("no consumer named %q on TASKS; found %v", want, names)
		}
	}
	for _, n := range names {
		if !strings.HasPrefix(n, podA+"-") && n != relayDurable && n != "test-reader" {
			t.Errorf("consumer %q is not this session's, the gateway's relay, or the provisioner's; a session created something outside its naming contract", n)
		}
	}
}

// The same run again, this time down the path a gateway-spawned worker
// actually takes: the spawner names the submission's stream sequence and the
// adapter fetches exactly that message.
//
// This exists because the origin-sequence mechanism had never once been
// exercised under a session's real bus permissions. Everything that proved it
// works — the three tests in worker-adapter/adapter_origin_cap_test.go — runs
// against a bare nats-server with no auth_callout, no minted JWT and no
// grants, and calls fetchOrigin directly. That proves the logic. It cannot
// prove the one thing this package exists to prove, which is that the session
// credential admits the call at all, and the specific worry is sharp rather
// than vague: fetchOriginAtSeq asks for a consumer with
// DeliverPolicy=DeliverByStartSequence and OptStartSeq set, which is a
// different consumer config from anything a session had ever created before
// this change. If a session's grants refused that create — if the permission
// only admitted the DeliverAll shape the scan used — every gateway-spawned
// worker would hang for originFetchDeadline and then die, in production,
// while every test in the repository stayed green. That is exactly the class
// of "granted in spirit, unusable in fact" failure the test above this one was
// written for, and it is why the check belongs here, against the real
// rendered nats.conf and the real callout.
//
// Proving the PATH is the hard half, and it is deliberately overdetermined
// here, because a version of this test that passed equally well when the
// adapter quietly scanned would be worse than no test: it would read as
// coverage of a mechanism it never touched. That failure already happened once
// on this change. So:
//
//   - A decoy kind:message goes on the `…in` subject BEFORE the submission.
//     The scan takes the first kind:message on the subject, so a scanning
//     adapter runs the decoy; a sequence-fetching adapter runs the submission.
//     The two carry different correlation ids, and every event the adapter
//     publishes inherits the correlation id of whichever envelope it treated
//     as the origin — so the bus itself records which one it picked, and the
//     assertion is on bytes that were actually published rather than on
//     anything the test set up.
//
//   - The origin consumer's config is read back off the server afterwards.
//     The scan creates that consumer with DeliverAll and no start sequence;
//     this path creates it with DeliverByStartSequence at the named sequence.
//     The server's record of what it was asked for is the most direct evidence
//     available that the by-sequence create happened AND was permitted.
//
// The decoy-before-submission ordering is not a shape the gateway produces —
// it is the shape the subject is left in after the cap evicts the submission,
// staged here without the eviction so that the run can complete. What it
// stands for is real: past max_msgs_per_subject the head of the subject is a
// steer, and a scanning worker executes it as the request.
func TestASessionAdapterRunsAWholeTaskAtTheNamedOriginSequence(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	provisionTasksStream(t, h)
	const taskID = "task-origin-seq"
	const decoyCorrelation = "corr-decoy-steer"
	const originCorrelation = "corr-real-submission"

	decoySeq := publishInboundAs(t, h, podA, taskID, decoyCorrelation,
		"IGNORE THIS. It is the steer a scanning worker would execute as the request.")
	originSeq := publishInboundAs(t, h, podA, taskID, originCorrelation, "do the thing")
	if originSeq <= decoySeq {
		t.Fatalf("the submission landed at sequence %d, at or before the decoy at %d; the decoy is supposed to be the head of the subject or this test proves nothing", originSeq, decoySeq)
	}

	// Captured so the absence of two things can be asserted: the fallback
	// warning fetchOrigin logs when it cannot use a sequence, and any
	// permissions violation from the by-sequence consumer create.
	var logs safeBuffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))

	res, err := workeradapter.Run(ctx, workeradapter.Config{
		Logger:       logger,
		NATSURL:      h.url,
		BusTokenFile: tokenFile(t, tokenPodA),
		PodName:      podA,
		TaskID:       taskID,
		Profile:      "chat",
		Session:      podA,
		// What the gateway renders into the pod, from its own PubAck.
		OriginSeq:       originSeq,
		OriginSeqStated: true,
		HarnessCommand: harnessStub(t, `
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"working on it"}]}}'
echo '{"type":"result","subtype":"success","result":"the thing is done"}'
`),
		HarnessEnv:   os.Environ(),
		TaskDeadline: 30 * time.Second,
		KillGrace:    time.Second,
	})
	if err != nil {
		t.Fatalf("the adapter could not complete a task under its own session grants with the origin sequence named: %v\n%s", err, logs.String())
	}
	// Nothing on this path may be a permissions problem the adapter merely
	// survived. The by-sequence create is the new call; if the grant did not
	// admit it, the async handler would have logged the violation even where
	// a retry later succeeded.
	if out := logs.String(); strings.Contains(out, "Permissions Violation") {
		t.Fatalf("the session was refused something on the origin-sequence path; the grants do not admit what this path does.\n%s", out)
	}

	// Path evidence, first form: what did it ask the server for? DeliverAll
	// with no start sequence is the scan; this is not that.
	cfg := sessionConsumerConfig(t, h, podA, lib.SessionConsumerOrigin)
	if cfg.DeliverPolicy != jetstream.DeliverByStartSequencePolicy {
		t.Errorf("the origin consumer was created with DeliverPolicy %v, want DeliverByStartSequence; the adapter took the scan path and this test would otherwise have passed anyway", cfg.DeliverPolicy)
	}
	if cfg.OptStartSeq != originSeq {
		t.Errorf("the origin consumer starts at sequence %d, want the submission's %d", cfg.OptStartSeq, originSeq)
	}
	// The filter rides the CREATE subject, so this is also a statement about
	// what the grant permitted: a consumer this session created can only ever
	// have filtered its own subject.
	if want := lib.TaskInSubject(podA, taskID); cfg.FilterSubject != want {
		t.Errorf("the origin consumer filters %q, want %q", cfg.FilterSubject, want)
	}

	// Path evidence, second form: whose message did it run?
	events := readEvents(t, h, podA, taskID)
	if len(events) == 0 {
		t.Fatal("no events on the session's own subject; the adapter reported success and published nothing")
	}
	var sawFinal bool
	for _, e := range events {
		if e.CorrelationID == decoyCorrelation {
			t.Fatalf("the adapter ran the decoy at sequence %d rather than the submission at %d: it scanned the subject instead of fetching the sequence it was given", decoySeq, originSeq)
		}
		if e.CorrelationID != originCorrelation {
			t.Errorf("event %s carries correlation id %q, want the submission's %q", e.EnvelopeID, e.CorrelationID, originCorrelation)
		}
		if e.Kind == lib.KindStatusUpdate && strings.Contains(string(e.Payload), `"final":true`) {
			sawFinal = true
		}
	}
	if !sawFinal {
		t.Error("no final status event on the session's own subject")
	}

	// And the fallback never ran. fetchOrigin logs this when it is told
	// nothing usable and has to scan; seeing it here would mean the sequence
	// never reached the adapter in the first place.
	if out := logs.String(); strings.Contains(out, "falling back to scanning the in subject") {
		t.Errorf("the adapter logged the scan fallback despite being handed sequence %d.\n%s", originSeq, out)
	}

	// Last, because it is the weakest of these: a completed run is what a
	// scanning adapter can also produce, so it is the thing the assertions
	// above exist to be stronger than.
	if res.State != lib.StateCompleted {
		t.Fatalf("terminal state = %q, want completed", res.State)
	}
}

// The refusal, under the same real grants: the submission the spawner named
// has been evicted by the per-subject cap, and the adapter must decline to run
// rather than execute the steer that outlived it.
//
// The distinction this test exists to draw is between two ways the run can
// fail, which look similar from a distance and mean opposite things:
//
//   - the adapter's own refusal, which is the feature working. The consumer
//     was created, the server delivered the next message the filter matched
//     (nats-server does not reject an evicted start sequence — it silently
//     moves forward, which is why the check is on the sequence that came back
//     rather than on an error), the adapter noticed the sequence was not the
//     one it asked for, and it stopped.
//
//   - a NATS permissions violation, which would mean the session's grants do
//     not admit a DeliverByStartSequence consumer create at all. That is not a
//     test gap, it is a production bug: every gateway-spawned worker would
//     fail this way, and the refusal message an operator reads would be about
//     an evicted submission when the real cause was the credential.
//
// A test that only asserted "Run returned an error" would pass in both worlds.
// So this asserts the text of the refusal AND the absence of any violation on
// the adapter's log, where a refused publish surfaces — a refused JetStream
// request gets no reply at all, so the violation never appears in the returned
// error and only ever reaches the async error handler.
func TestASessionAdapterRefusesAnEvictedOriginRatherThanBeingRefusedByTheBus(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	// One message per subject, so the steer below evicts the submission. The
	// install renders 4096; the mechanism is the same at either number.
	provisionTasksStreamCapped(t, h, 1)
	const taskID = "task-origin-evicted"

	originSeq := publishInboundAs(t, h, podA, taskID, "corr-real-submission", "the originating request")
	steerSeq := publishInboundAs(t, h, podA, taskID, "corr-steer", "a steer sent later")
	if steerSeq <= originSeq {
		t.Fatalf("the steer landed at %d, not after the submission at %d", steerSeq, originSeq)
	}

	var logs safeBuffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))

	_, err := workeradapter.Run(ctx, workeradapter.Config{
		Logger:          logger,
		NATSURL:         h.url,
		BusTokenFile:    tokenFile(t, tokenPodA),
		PodName:         podA,
		TaskID:          taskID,
		Profile:         "chat",
		Session:         podA,
		OriginSeq:       originSeq,
		OriginSeqStated: true,
		HarnessCommand:  harnessStub(t, `echo '{"type":"result","subtype":"success","result":"ran the steer"}'`),
		HarnessEnv:      os.Environ(),
		TaskDeadline:    30 * time.Second,
		KillGrace:       time.Second,
	})
	if err == nil {
		t.Fatalf("the adapter ran a task whose submission the cap had evicted; it executed the steer at sequence %d as the request", steerSeq)
	}
	t.Logf("refused, as it should be: %v", err)

	// The adapter's refusal, not the bus's. Each fragment is load-bearing for
	// an operator: what happened, which message is missing, and where.
	for _, want := range []string{
		"evicted",
		fmt.Sprintf("stream sequence %d", originSeq),
		fmt.Sprintf("the oldest message left is %d", steerSeq),
		lib.TaskInSubject(podA, taskID),
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the refusal does not mention %q, so this is not the adapter's eviction refusal: %v", want, err)
		}
	}

	// The half that makes the test worth writing. If the grant did not admit
	// the by-sequence create, the create would be refused, fetchOriginAtSeq
	// would spin to originFetchDeadline and return a consumer error, and the
	// violation would be sitting here.
	out := logs.String()
	if strings.Contains(out, "Permissions Violation") {
		t.Fatalf("the bus refused the session something on this path. A DeliverByStartSequence consumer create that the session's grants do not admit is a production bug in the origin-sequence mechanism, not a test problem.\n%s", out)
	}
	if strings.Contains(out, "the bus refused this session") {
		t.Fatalf("the adapter logged a bus refusal; the grants do not cover what the origin-sequence path does.\n%s", out)
	}

	// Refusing means refusing to publish, too. A submitted event here would
	// have started a lifecycle for a task that is never going to run.
	if events := readEvents(t, h, podA, taskID); len(events) != 0 {
		t.Errorf("%d events were published for a task the adapter refused to run", len(events))
	}
}

// The same adapter, the same server, one thing different: the token belongs to
// the pod next door. Everything the run above did is refused, and it is refused
// in a way the operator can read.
//
// This is the demonstration gke-labs#1270 asks for, in test form. A credential
// lifted out of one session pod — by /proc/1/environ or by reading the token
// file, both of which the harness can do — buys nothing in another session.
func TestASessionAdapterCannotRunAnotherSessionsTask(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	provisionTasksStream(t, h)
	const taskID = "task-e2e-2"
	// The task belongs to podA, and podA's pod is where it would run.
	submitAs(t, h, podA, taskID, "do the thing")

	// The adapter's own log, captured, because half of what this test is
	// checking is whether an operator could tell what happened. A refused
	// JetStream publish gets no reply, so the call waits out its context and
	// reports a deadline — the reason arrives separately, on the async error
	// handler, and if it were not logged there it would arrive nowhere.
	var logs safeBuffer
	logger := slog.New(slog.NewTextHandler(&logs, &slog.HandlerOptions{Level: slog.LevelDebug}))

	// podB's credential, pointed at podA's task. The adapter is told it is
	// podA — Session, TaskID and addressee all say so — and only the token
	// disagrees, because the token is the only thing it cannot choose.
	_, err := workeradapter.Run(ctx, workeradapter.Config{
		Logger:         logger,
		NATSURL:        h.url,
		BusTokenFile:   tokenFile(t, tokenPodB),
		PodName:        podA,
		TaskID:         taskID,
		Profile:        "chat",
		Session:        podA,
		HarnessCommand: harnessStub(t, `echo '{"type":"result","subtype":"success","result":"stolen"}'`),
		HarnessEnv:     os.Environ(),
		TaskDeadline:   15 * time.Second,
		KillGrace:      time.Second,
	})
	if err == nil {
		t.Fatal("an adapter holding another session's token ran the task to completion")
	}
	t.Logf("refused, as it should be: %v", err)

	// The deadline error alone names the wrong problem. The log has to carry
	// the real one, and name the subject that was refused.
	out := logs.String()
	if !strings.Contains(out, "the bus refused this session") {
		t.Errorf("the refusal never reached the log; an operator would see only a timeout.\n%s", out)
	}
	if !strings.Contains(out, "Permissions Violation") {
		t.Errorf("the log does not say the refusal was a permissions violation.\n%s", out)
	}

	// Nothing of podA's was written. A refusal that still leaked a
	// submitted event would corrupt the real session's lifecycle.
	if events := readEvents(t, h, podA, taskID); len(events) != 0 {
		t.Errorf("%d events reached podA's subject from a pod holding podB's credential", len(events))
	}
}

// A pod whose A2A_SESSION and A2A_POD_NAME disagree fails at once, with both
// names in the message.
//
// The two are equal by construction — the gateway names the pod after the bus
// session — so a disagreement means a spawner bug or a hand-edited pod. Left
// to run, the adapter would authenticate perfectly well, pin its inbox to the
// wrong name and then hang on the first JetStream call, which reads like a bus
// outage.
func TestAnAdapterRefusesAPodNameAndSessionThatDisagree(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	_, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:        h.url,
		BusTokenFile:   tokenFile(t, tokenPodA),
		PodName:        podA,
		TaskID:         "task-e2e-3",
		Profile:        "chat",
		Session:        podB,
		HarnessCommand: harnessStub(t, `echo '{"type":"result","subtype":"success","result":"x"}'`),
		HarnessEnv:     os.Environ(),
		TaskDeadline:   10 * time.Second,
	})
	if err == nil {
		t.Fatal("the adapter started with a pod name and a session name that disagree")
	}
	if !strings.Contains(err.Error(), podA) || !strings.Contains(err.Error(), podB) {
		t.Errorf("the error names neither side of the disagreement: %v", err)
	}
}

// The quiet half of the case above. An unset A2A_SESSION is not a disagreement
// the eye catches: Addressee falls back to Profile, so the adapter publishes as
// `chat` while the callout derived its grants from the pod. Every publish is
// refused and no reply ever arrives, which is the failure mode this check
// exists to convert into a startup error.
func TestAnAdapterRefusesAPodNameWithNoSessionAtAll(t *testing.T) {
	h := startHarness(t, sessionMap, sessionTokens())
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()

	_, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:        h.url,
		BusTokenFile:   tokenFile(t, tokenPodA),
		PodName:        podA,
		TaskID:         "task-e2e-4",
		Profile:        "chat",
		HarnessCommand: harnessStub(t, `echo '{"type":"result","subtype":"success","result":"x"}'`),
		HarnessEnv:     os.Environ(),
		TaskDeadline:   10 * time.Second,
	})
	if err == nil {
		t.Fatal("the adapter started with a bus token and no session name, so it would have published as its profile")
	}
	// The addressee it would have used, not the empty string: the error has to
	// name the wrong thing it was about to be.
	if !strings.Contains(err.Error(), podA) || !strings.Contains(err.Error(), "chat") {
		t.Errorf("the error names neither the pod it is bound to nor the addressee it would have used: %v", err)
	}
}

// --- helpers ---------------------------------------------------------------

// safeBuffer is a bytes.Buffer the async error handler and the test goroutine
// can both touch. Without the mutex this test is a data race that passes.
type safeBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *safeBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *safeBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// tokenFile writes a token where the adapter's own file-reading path will find
// it, rather than handing the adapter a string. The rotation re-read is part of
// what is under test in the run above.
func tokenFile(t *testing.T, token string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(p, []byte(token), 0o600); err != nil {
		t.Fatalf("writing the token file: %v", err)
	}
	return p
}

func harnessStub(t *testing.T, body string) []string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "stub.sh")
	if err := os.WriteFile(p, []byte("#!/bin/bash\n"+body+"\n"), 0o755); err != nil {
		t.Fatalf("writing the harness stub: %v", err)
	}
	return []string{"/bin/bash", p}
}

// gatewayConn is the privileged side of these tests: it provisions the stream
// and submits the task, exactly as the gateway does in the deployment. It is a
// mapped principal with broad grants, which is the point — the session under
// test is the constrained one.
func gatewayConn(t *testing.T, h *harness) *nats.Conn {
	t.Helper()
	nc, _ := h.connectAs(t, "gateway", gatewayToken)
	return nc
}

func provisionTasksStream(t *testing.T, h *harness) {
	t.Helper()
	provisionTasksStreamCapped(t, h, 0)
}

// provisionTasksStreamCapped is the same stream with max_msgs_per_subject
// dialled down, so a test can reach the cap with two messages instead of
// 4096.
//
// perSubject of 0 is JetStream's "no limit", which is what the uncapped
// callers above get. The install renders 4096 with discard=old
// (a2aTasksMaxMsgsPerSubject in platformagent_a2a_manifests.go); the number is
// the only thing a test moves, because what matters here is that the cap
// exists at all and that it lands on a task's `…in` subject, where the oldest
// message is the submission and everything after it is a steer.
func provisionTasksStreamCapped(t *testing.T, h *harness, perSubject int64) {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if _, err := js.CreateStream(ctx, jetstream.StreamConfig{
		Name:              lib.TasksStream,
		Subjects:          []string{"a2a.tasks.>"},
		Retention:         jetstream.LimitsPolicy,
		Discard:           jetstream.DiscardOld,
		MaxAge:            72 * time.Hour,
		MaxMsgsPerSubject: perSubject,
	}); err != nil {
		t.Fatalf("provisioning TASKS: %v", err)
	}
}

// sessionConsumerConfig reads back the config of one of the session's named
// consumers, as the gateway, which may look at the task plane the session may
// only write to.
//
// This is how a test can see WHICH code path inside the adapter created a
// consumer, rather than only that the run finished. The DeliverPolicy and
// OptStartSeq the adapter asked for are recorded on the server, so they can be
// read back afterwards and compared against the path that was supposed to run.
// The session's consumers outlive the run by consumerInactiveThreshold (five
// seconds), which the test next door already depends on when it asserts they
// exist at all.
func sessionConsumerConfig(t *testing.T, h *harness, session, role string) jetstream.ConsumerConfig {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	name := lib.SessionConsumerName(session, role)
	cons, err := js.Consumer(ctx, lib.TasksStream, name)
	if err != nil {
		t.Fatalf("the session's %q consumer is not on TASKS after the run, so there is no record of how the adapter created it: %v", name, err)
	}
	return cons.CachedInfo().Config
}

// submitAs publishes a task's submission as the gateway and hands back the
// TASKS stream sequence the server assigned it.
//
// The sequence is returned rather than dropped because it is the number the
// real gateway renders into the session pod as lib.EnvOriginSeq: gateway.go
// calls g.client.PublishSeq precisely so that the PubAck's sequence can be
// handed to the pod it is about to spawn. A test that wants to drive the
// adapter's sequence-named origin fetch has to obtain that number the same
// way, from the server, at publish time. Passing a literal 1 would be a test
// of nothing: it would agree with a stream that happens to be numbered from
// one and would go on agreeing if the plumbing that carries the real sequence
// were cut.
func submitAs(t *testing.T, h *harness, addressee, taskID, text string) uint64 {
	t.Helper()
	return publishInboundAs(t, h, addressee, taskID, "corr-"+taskID, text)
}

// publishInboundAs puts one kind:message envelope on a task's `…in` subject as
// the gateway, with a caller-chosen correlation id, and returns its stream
// sequence.
//
// Submissions and steers are the same kind on the same subject, and no field
// of the envelope says which is which — that indistinguishability is the
// entire reason lib.EnvOriginSeq exists. So one helper writes both, and the
// correlation id is how a test can afterwards tell which of them the adapter
// actually picked up and ran.
//
// It publishes through JetStream rather than core NATS so there is a PubAck to
// read the sequence off. The gateway's own publish is a JetStream publish for
// the same reason.
func publishInboundAs(t *testing.T, h *harness, addressee, taskID, correlationID, text string) uint64 {
	t.Helper()
	payload, err := json.Marshal(lib.Message{
		Role: "user", Parts: []lib.Part{{Kind: "text", Text: text}},
		MessageID: "msg-" + correlationID, TaskID: taskID, ContextID: "ctx-" + taskID,
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	env, err := lib.NewMessageEnvelope(
		lib.Party{Session: "gateway", AgentType: "a2a-gateway"},
		taskID, "ctx-"+taskID, correlationID, payload,
		lib.WithTo(lib.Party{Session: addressee}))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	raw, err := json.Marshal(env)
	if err != nil {
		t.Fatalf("marshal envelope: %v", err)
	}
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	ack, err := js.Publish(ctx, lib.TaskInSubject(addressee, taskID), raw)
	if err != nil {
		t.Fatalf("publishing %s on %s: %v", correlationID, lib.TaskInSubject(addressee, taskID), err)
	}
	if ack.Sequence == 0 {
		t.Fatalf("the server acked %s on %s with sequence 0", correlationID, lib.TaskInSubject(addressee, taskID))
	}
	return ack.Sequence
}

// readEvents drains a task's events subject as the gateway, which may read
// what the session may only write.
func readEvents(t *testing.T, h *harness, addressee, taskID string) []*lib.Envelope {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cons, err := js.CreateOrUpdateConsumer(ctx, lib.TasksStream, jetstream.ConsumerConfig{
		// Named, so the consumer-naming assertion above can tell the test's
		// own reader from something the session created.
		Name:          "test-reader",
		FilterSubject: lib.TaskEventsSubject(addressee, taskID),
		DeliverPolicy: jetstream.DeliverAllPolicy,
		AckPolicy:     jetstream.AckNonePolicy,
	})
	if err != nil {
		t.Fatalf("reader consumer: %v", err)
	}
	batch, err := cons.FetchNoWait(256)
	if err != nil {
		t.Fatalf("fetch: %v", err)
	}
	var out []*lib.Envelope
	for msg := range batch.Messages() {
		env, err := lib.ParseEnvelope(msg.Data())
		if err != nil {
			t.Errorf("unparseable event on the bus: %v", err)
			continue
		}
		out = append(out, env)
	}
	return out
}

func consumerNames(t *testing.T, h *harness) []string {
	t.Helper()
	js, err := jetstream.New(gatewayConn(t, h))
	if err != nil {
		t.Fatalf("jetstream: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	stream, err := js.Stream(ctx, lib.TasksStream)
	if err != nil {
		t.Fatalf("stream: %v", err)
	}
	var names []string
	lister := stream.ConsumerNames(ctx)
	for name := range lister.Name() {
		names = append(names, name)
	}
	if err := lister.Err(); err != nil {
		t.Fatalf("listing consumers: %v", err)
	}
	return names
}
