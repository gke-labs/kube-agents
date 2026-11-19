package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/gke-labs/kube-agents/a2a/capability"
	"github.com/gke-labs/kube-agents/a2a/lib"
	workeradapter "github.com/gke-labs/kube-agents/a2a/worker-adapter"
)

// The session route's capability, end to end: for each plain turn the gateway
// mints a capability whose delegate is the session pod, and the session pod's
// worker-adapter is the thing that refuses a turn carrying anything else.
// Every test here runs the real gateway (RouteSession, so the turn's addressee
// is the spawned session pod), the real verifier over the real cap bucket, and
// the real worker-adapter Run. Its Config is built directly, with the values
// the gateway's spawner puts in a session pod's A2A_SESSION (the pod's
// session) and A2A_AUTHORITY_SCOPE (the gateway's own resolved AuthorityScope).
//
// The gateway mints under a real namespace scope (gatewayAuthorityScope), not
// the `namespace/-` a worker with no scope set falls back to, so a pod that
// ignored its configured scope would be caught rather than agree by default.
//
// What tells "refused" from "ran" is the harness, not the adapter's report: the
// stub's first act is to create a marker file, so a harness that started at
// all, for however short a time, leaves it behind.

const (
	// sessionPodRunBound bounds one worker-adapter Run. The verifier answers
	// at once here, so this only decides how long a hung run takes to fail.
	sessionPodRunBound = 60 * time.Second
	// sessionPodTaskDeadline and sessionPodKillGrace are the adapter's own
	// harness bounds, short because the stub answers immediately.
	sessionPodTaskDeadline = 20 * time.Second
	sessionPodKillGrace    = 500 * time.Millisecond
	// sessionPodProfile is the profile a session pod runs as.
	sessionPodProfile = "chat"
	// capRefusalPrefix opens the worker-adapter's terminal reason for every
	// capability refusal (worker-adapter/capability.go); the rule that fired
	// follows it.
	capRefusalPrefix = "reason: capability-refused - "
	// noCapabilityRule is the worker-adapter's own rule for a submission with
	// no capability (worker-adapter/capability.go; unexported there).
	noCapabilityRule = "the submission carries no capability"
	// outsideScopeRule is the verifier's verb-check rule for a resource the
	// capability's scope does not contain (capability/verb.go Permits;
	// unexported there). The chain-walk rule is capability.WalkRefused.
	outsideScopeRule = "resource is outside the capability's scope"
	// gatewayAuthorityScope is the scope the rig's gateway mints under, and
	// so the scope its session pods check against.
	gatewayAuthorityScope capability.Scope = "namespace/team-a"
	// siblingScope is a namespace next to the gateway's that its capability
	// does not reach.
	siblingScope capability.Scope = "namespace/team-b"
	// busCallBound bounds one publish or fold read against the rig's bus.
	busCallBound = 10 * time.Second
	// harnessStartedMarker is the file the marker harness creates first.
	harnessStartedMarker = "harness-started"
	// harnessAnswer is the result the marker harness reports.
	harnessAnswer = "answered on the minted capability"
	// forgedRevision is a plausible revision on a key the gateway never wrote.
	forgedRevision = 1
	// forgedKeySuffix makes a key next to the real one that nothing minted.
	forgedKeySuffix = "-forged"
	// refusedTaskPrefix prefixes the task ids the attacker republishes under.
	refusedTaskPrefix = "task-attack-"
)

// startCapVerifier runs the real verifier service against the rig's bus: the
// Service the verifier Deployment runs, resolving out of the bucket the
// gateway mints into. Modelled on worker-adapter's startVerifier; a stub that
// answered "allowed" would prove nothing about the control.
func startCapVerifier(t *testing.T, url string) {
	t.Helper()
	nc, err := nats.Connect(url, nats.Name("cap-verifier-gateway-test"))
	if err != nil {
		t.Fatalf("verifier connect: %v", err)
	}
	t.Cleanup(nc.Close)
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	js, err := jetstream.New(nc)
	if err != nil {
		t.Fatalf("verifier jetstream: %v", err)
	}
	store, err := capability.NewStore(ctx, js)
	if err != nil {
		t.Fatalf("verifier store: %v", err)
	}
	svc := &capability.Service{Resolver: &capability.Resolver{Store: store}, Log: slog.Default()}
	sub, err := svc.Subscribe(ctx, nc)
	if err != nil {
		t.Fatalf("verifier subscribe: %v", err)
	}
	t.Cleanup(func() { _ = sub.Unsubscribe() })
}

// startSessionCapRig is the post-flip gateway (every conversation's default
// addressee is its own session pod), minting under gatewayAuthorityScope, with
// the verifier running beside it.
func startSessionCapRig(t *testing.T) (*rig, *fakeSpawner) {
	t.Helper()
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, func(c *Config) {
		c.AuthorityScope = gatewayAuthorityScope
	})
	startCapVerifier(t, r.url)
	return r, spawn
}

// plainSessionTurn sends a plain chat turn (no `delegate:`) into conv as the
// n-th conversation the rig has seen (0-based), and returns the session pod
// the gateway spawned for it and the submission the gateway published there.
func plainSessionTurn(t *testing.T, r *rig, spawn *fakeSpawner, n int, conv, text string) (string, *lib.Envelope) {
	t.Helper()
	say(r, conv, "m-"+conv, text)
	waitFor(t, fmt.Sprintf("spawn %d", n+1), func() bool { return len(spawn.calls()) > n })
	session := spawn.calls()[n].Session
	origin := r.awaitTask(t, session)
	if got := envText(t, origin); !strings.Contains(got, text) || strings.Contains(got, "delegate:") {
		t.Fatalf("the submission on %s reads %q; want the plain turn %q", session, got, text)
	}
	return session, origin
}

// gatewayGrants returns the grants half of the authority block the gateway put
// on a submission, after checking it is the root the gateway minted with the
// session pod as its delegate, under gatewayAuthorityScope.
func gatewayGrants(t *testing.T, r *rig, origin *lib.Envelope, session string) json.RawMessage {
	t.Helper()
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatalf("authority: %v", err)
	}
	assertRootCapability(t, r, auth, origin.TaskID, session)

	var grants AuthorityGrants
	if err := json.Unmarshal(auth.Grants, &grants); err != nil {
		t.Fatalf("grants: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), busCallBound)
	defer cancel()
	store, err := capability.NewStore(ctx, r.client.JetStream())
	if err != nil {
		t.Fatalf("cap store: %v", err)
	}
	entry, err := (&capability.Resolver{Store: store}).Resolve(ctx, session, grants.Capability)
	if err != nil {
		t.Fatalf("the session pod could not resolve its own capability: %v", err)
	}
	if entry.Scope != gatewayAuthorityScope {
		t.Fatalf("minted scope = %q, want the gateway's AuthorityScope %q", entry.Scope, gatewayAuthorityScope)
	}
	return auth.Grants
}

// grantsFor renders the grants half naming ref, as the gateway renders it.
func grantsFor(t *testing.T, ref capability.Ref) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(AuthorityGrants{Capability: ref})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// republishWithGrants is the attacker: the gateway's own submission, copied
// whole (sender, context, correlation, every part of the message and the
// advisory halves of the authority block) onto a new task on session's
// subjects, with only the grants replaced. Nothing but the capability differs
// from a turn the pod would run.
func republishWithGrants(t *testing.T, r *rig, origin *lib.Envelope, session, taskID string, grants json.RawMessage) {
	t.Helper()
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatalf("authority: %v", err)
	}
	auth.Grants = grants
	authority, err := json.Marshal(auth)
	if err != nil {
		t.Fatal(err)
	}
	var msg lib.Message
	if err := json.Unmarshal(origin.Payload, &msg); err != nil {
		t.Fatalf("submission message: %v", err)
	}
	msg.TaskID, msg.MessageID = taskID, "msg-"+taskID
	payload, err := json.Marshal(msg)
	if err != nil {
		t.Fatal(err)
	}
	env, err := lib.NewMessageEnvelope(origin.From, taskID, origin.ContextID, origin.CorrelationID, payload,
		lib.WithTo(lib.Party{Session: session}), lib.WithAuthority(authority))
	if err != nil {
		t.Fatalf("submission envelope: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), busCallBound)
	defer cancel()
	if err := r.bus.Publish(ctx, lib.TaskInSubject(session, taskID), env); err != nil {
		t.Fatalf("publish %s: %v", taskID, err)
	}
}

// markerHarness is a harness whose first act is to create marker, and which
// then reads the prompt and answers success. It returns the argv.
func markerHarness(t *testing.T, marker string) []string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "harness.sh")
	body := fmt.Sprintf(`#!/bin/bash
: > %q
read first || exit 1
echo '{"type":"result","subtype":"success","result":%q}'
`, marker, harnessAnswer)
	if err := os.WriteFile(path, []byte(body), 0o755); err != nil {
		t.Fatalf("write harness: %v", err)
	}
	return []string{"/bin/bash", path}
}

// runSessionPod runs the real worker-adapter for one task the way a session
// pod does: its session as the addressee, and the gateway's resolved
// AuthorityScope as the scope it checks the capability against. It returns
// the terminal state Run reported.
func runSessionPod(t *testing.T, r *rig, session, taskID string, harness []string) lib.TaskState {
	t.Helper()
	scope := r.g.cfg.AuthorityScope
	if scope != gatewayAuthorityScope {
		t.Fatalf("the gateway resolved AuthorityScope %q, want %q", scope, gatewayAuthorityScope)
	}
	return runSessionPodAt(t, r, session, taskID, scope, harness)
}

// runSessionPodAt is runSessionPod with the pod's scope chosen by the caller,
// for a pod configured with a scope other than the gateway's.
func runSessionPodAt(t *testing.T, r *rig, session, taskID string, scope capability.Scope, harness []string) lib.TaskState {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), sessionPodRunBound)
	defer cancel()
	freshPodConsumers(ctx, t, r, session)
	res, err := workeradapter.Run(ctx, workeradapter.Config{
		NATSURL:        r.url,
		TaskID:         taskID,
		Profile:        sessionPodProfile,
		Session:        session,
		Scope:          scope,
		HarnessCommand: harness,
		HarnessEnv:     os.Environ(),
		TaskDeadline:   sessionPodTaskDeadline,
		KillGrace:      sessionPodKillGrace,
	})
	if err != nil {
		t.Fatalf("worker-adapter run for %s on %s: %v", taskID, session, err)
	}
	return res.State
}

// freshPodConsumers deletes the session's named consumers on TASKS, so each
// Run starts with the consumer state a fresh pod has. A non-reused session pod
// runs one task and its consumers are reaped after it; two Runs under one
// session name in a test would otherwise share them, and a consumer's start
// sequence cannot be updated in place, which fails the second Run at its
// in-subject subscribe before any capability check is reached.
func freshPodConsumers(ctx context.Context, t *testing.T, r *rig, session string) {
	t.Helper()
	for _, role := range lib.SessionConsumerRoles {
		err := r.bus.JetStream().DeleteConsumer(ctx, lib.TasksStream, lib.SessionConsumerName(session, role))
		if err != nil && !errors.Is(err, jetstream.ErrConsumerNotFound) {
			t.Fatalf("delete %s consumer of %s: %v", role, session, err)
		}
	}
}

// foldOn reads a task's fold off the stream, which is what the gateway and an
// operator both see.
func foldOn(t *testing.T, r *rig, session, taskID string) *lib.Task {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), busCallBound)
	defer cancel()
	task, err := r.bus.TasksGet(ctx, session, taskID)
	if err != nil {
		t.Fatalf("tasks/get %s: %v", taskID, err)
	}
	return task
}

func finalText(task *lib.Task) string {
	if task.FinalMessage == nil {
		return ""
	}
	var parts []string
	for _, p := range task.FinalMessage.Parts {
		if p.Kind == "text" {
			parts = append(parts, p.Text)
		}
	}
	return strings.Join(parts, " ")
}

// assertRefusedBeforeHarness runs the session pod on taskID at scope and
// asserts the turn ended terminal rejected on the stream with exactly the
// capability rule wantRule (not merely some capability refusal, which an
// unreachable verifier also produces), with the harness never started.
func assertRefusedBeforeHarness(t *testing.T, r *rig, session, taskID string, scope capability.Scope, wantRule string) {
	t.Helper()
	marker := filepath.Join(t.TempDir(), harnessStartedMarker)
	if state := runSessionPodAt(t, r, session, taskID, scope, markerHarness(t, marker)); state != lib.StateRejected {
		t.Fatalf("the session pod ended %s with state %q; want it refused", taskID, state)
	}
	task := foldOn(t, r, session, taskID)
	if task.State != lib.StateRejected || !task.Final {
		t.Fatalf("the refusal is not terminal rejected on the stream: state %q final %v", task.State, task.Final)
	}
	if text, want := finalText(task), capRefusalPrefix+wantRule; text != want {
		t.Fatalf("terminal reason = %q, want %q", text, want)
	}
	for _, s := range task.StatusHistory {
		if s == lib.StateWorking {
			t.Fatalf("the refused turn reached working: %v", task.StatusHistory)
		}
	}
	if _, err := os.Stat(marker); !os.IsNotExist(err) {
		t.Fatalf("the harness started for a refused turn (marker stat: %v)", err)
	}
}

// The positive control. The capability the gateway minted for the session pod
// on a plain turn, unchanged, lets the session pod run it: the harness starts (its marker
// is there) and the task completes with the harness's answer. Without this
// the refusals below could be a pod that refuses everything.
func TestASessionPodRunsAPlainTurnOnTheCapabilityTheGatewayMinted(t *testing.T) {
	t.Parallel()
	r, spawn := startSessionCapRig(t)
	session, origin := plainSessionTurn(t, r, spawn, 0, "discord:g1/t-cap-ok", "what pods are failing")
	gatewayGrants(t, r, origin, session)

	marker := filepath.Join(t.TempDir(), harnessStartedMarker)
	if state := runSessionPod(t, r, session, origin.TaskID, markerHarness(t, marker)); state != lib.StateCompleted {
		t.Fatalf("the gateway-minted turn ended %q; want completed", state)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("the harness never started on the gateway's capability: %v", err)
	}
	task := foldOn(t, r, session, origin.TaskID)
	if task.State != lib.StateCompleted || !task.Final {
		t.Fatalf("the turn is not terminal completed on the stream: state %q final %v", task.State, task.Final)
	}
	if got := artifactTextOf(task, lib.ArtifactResult); got != harnessAnswer {
		t.Fatalf("result artifact = %q, want the harness's answer", got)
	}
}

// The same plain turn, republished as a new task on the same session pod with
// the capability taken away or replaced by a forgery, is refused by the pod
// before its harness starts.
func TestASessionPodRefusesAPlainTurnWithoutTheGatewaysCapability(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name     string
		grants   func(minted capability.Ref, taskID string) json.RawMessage
		wantRule string
	}{
		{
			name:     "no capability at all",
			grants:   func(capability.Ref, string) json.RawMessage { return json.RawMessage("null") },
			wantRule: noCapabilityRule,
		},
		{
			name:     "a forged reference the gateway never wrote",
			wantRule: capability.WalkRefused,
			grants: func(_ capability.Ref, taskID string) json.RawMessage {
				return grantsFor(t, capability.Ref{Key: "root." + taskID + forgedKeySuffix, Revision: forgedRevision})
			},
		},
		{
			name:     "the gateway's own key at a revision it never wrote",
			wantRule: capability.WalkRefused,
			grants: func(minted capability.Ref, _ string) json.RawMessage {
				minted.Revision++
				return grantsFor(t, minted)
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r, spawn := startSessionCapRig(t)
			session, origin := plainSessionTurn(t, r, spawn, 0, "discord:g1/t-cap-refuse", "what pods are failing")
			var minted AuthorityGrants
			if err := json.Unmarshal(gatewayGrants(t, r, origin, session), &minted); err != nil {
				t.Fatal(err)
			}
			taskID := refusedTaskPrefix + origin.TaskID
			republishWithGrants(t, r, origin, session, taskID, tc.grants(minted.Capability, origin.TaskID))
			assertRefusedBeforeHarness(t, r, session, taskID, gatewayAuthorityScope, tc.wantRule)
		})
	}
}

// A delegate mismatch across two real session pods. Two conversations on the
// session route spawn two pods, X and Y, and on each turn the gateway mints a
// capability whose delegate is that turn's pod. X's capability, presented on a turn for Y and
// checked by Y's worker-adapter, is refused before Y's harness starts. Y's
// own capability runs Y's turn, so the refusal is the delegate and not Y's
// configuration.
func TestACapabilityMintedForOneSessionPodIsRefusedOnAnother(t *testing.T) {
	t.Parallel()
	r, spawn := startSessionCapRig(t)
	sessionX, originX := plainSessionTurn(t, r, spawn, 0, "discord:g1/t-cap-x", "what pods are failing")
	sessionY, originY := plainSessionTurn(t, r, spawn, 1, "discord:g1/t-cap-y", "what nodes are cordoned")
	if sessionX == sessionY {
		t.Fatalf("both conversations landed on %s; the test needs two pods", sessionX)
	}
	grantsX := gatewayGrants(t, r, originX, sessionX)
	gatewayGrants(t, r, originY, sessionY)

	// Y's own turn runs: the refusal below is then the delegate, not Y.
	marker := filepath.Join(t.TempDir(), harnessStartedMarker)
	if state := runSessionPod(t, r, sessionY, originY.TaskID, markerHarness(t, marker)); state != lib.StateCompleted {
		t.Fatalf("Y's own gateway-minted turn ended %q; want completed", state)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("Y's harness never started on Y's own capability: %v", err)
	}

	taskID := refusedTaskPrefix + originY.TaskID
	republishWithGrants(t, r, originY, sessionY, taskID, grantsX)
	assertRefusedBeforeHarness(t, r, sessionY, taskID, gatewayAuthorityScope, capability.WalkRefused)
}

// A scope mismatch. A session pod configured with a namespace next to the
// gateway's, presented the capability the gateway minted for it unchanged, is
// refused by the verifier's scope rule before its harness starts: the
// capability names the pod, so the walk succeeds, and the scope is what fails.
// The pod's configured scope is what is checked, not a default it could fall
// back to.
func TestASessionPodRefusesTheGatewaysCapabilityOutsideItsOwnScope(t *testing.T) {
	t.Parallel()
	r, spawn := startSessionCapRig(t)
	session, origin := plainSessionTurn(t, r, spawn, 0, "discord:g1/t-cap-scope", "what pods are failing")
	gatewayGrants(t, r, origin, session)
	assertRefusedBeforeHarness(t, r, session, origin.TaskID, siblingScope, outsideScopeRule)
}

// artifactTextOf concatenates the text parts of the named artifact.
func artifactTextOf(task *lib.Task, name string) string {
	var b strings.Builder
	for _, a := range task.Artifacts {
		if a.Name != name {
			continue
		}
		for _, p := range a.Parts {
			if p.Kind == "text" {
				b.WriteString(p.Text)
			}
		}
	}
	return b.String()
}
