package gateway

// The tests here pin the fixes from the upstream cut's adversarial pass:
// the spawn-failure supervisor terminal, the route-conditioned steer
// acknowledgement, the stop guards, the detached-task width bias, the
// pre-delete replay guard, the healed-terminal card, and the session pod's
// seccomp profile.

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// TestSpawnFailureClosesTheTaskAsItsSupervisor: the task publish precedes
// the spawn attempt, so a refused pod create (a quota, an API error) used
// to strand the task on the stream with no executor, no pod for Sweep to
// see, and a rolling line stuck at "submitted…" — non-terminal for the
// whole retention window. The supervisor rule applies at creation exactly
// as at deletion: the terminal goes on the stream, the relay renders it,
// and the conversation is free for the next ask.
func TestSpawnFailureClosesTheTaskAsItsSupervisor(t *testing.T) {
	r, spawn := startRigWithSpawner(t)
	spawn.failSpawns(errors.New(`pods "chat-x" is forbidden: exceeded quota`))
	conv := "discord:g1/thread-spawnfail"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sf-1", Text: "Delegate: doomed work"}

	var ref TaskRef
	waitFor(t, "task recorded", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		if err != nil || rec == nil || len(rec.Tasks) == 0 {
			return false
		}
		ref = rec.Tasks[0]
		return true
	})
	waitFor(t, "supervisor terminal `failed` on the stream", func() bool {
		finals, err := finalsFor(r.url, ref.Addressee, ref.ID)
		return err == nil && len(finals) == 1 && finals[0].Status.State == lib.StateFailed
	})
	// The relay renders the terminal onto the rolling line — the user sees
	// the failure, not an eternal placeholder.
	waitFor(t, "failure rendered to chat", func() bool {
		for _, e := range r.adapter.editTexts() {
			if strings.Contains(e, "failed") {
				return true
			}
		}
		return false
	})
	// Not wedged: the next plain ask starts a fresh task.
	spawn.failSpawns(nil)
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sf-2", Text: "try again please"}
	waitFor(t, "next task after the failure", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && len(rec.Tasks) == 2
	})
}

// TestSteerAckIsRouteConditioned: the spec's gateway-authored-posts rule
// (amended 8/31) — the acknowledgement reports the steer is on the stream,
// and what it says next depends on what the executor will do with it.
func TestSteerAckIsRouteConditioned(t *testing.T) {
	t.Run("fixed route says the executor refuses", func(t *testing.T) {
		r := startRig(t)
		conv := "discord:g1/thread-ackfixed"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
			AuthorID: "1001", MessageID: "af-1", Text: "check the fleet"}
		r.awaitTask(t, "platform")
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
			AuthorID: "1001", MessageID: "af-2", Text: "actually only prod"}
		waitFor(t, "fixed-route steer ack", func() bool {
			for _, p := range r.adapter.postTexts() {
				if strings.Contains(p, "does not take mid-task input") {
					return true
				}
			}
			return false
		})
	})
	t.Run("session route says the worker absorbs if still running", func(t *testing.T) {
		r, _ := startRigWithSpawnerRoute(t, RouteSession)
		conv := "discord:g1/thread-acksession"
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
			AuthorID: "1001", MessageID: "as-1", Text: "check the fleet"}
		waitFor(t, "session task recorded", func() bool {
			rec, err := r.g.reg.Get(context.Background(), conv)
			return err == nil && rec != nil && rec.ActiveTask != nil
		})
		r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
			AuthorID: "1001", MessageID: "as-2", Text: "actually only prod"}
		waitFor(t, "session-route steer ack", func() bool {
			for _, p := range r.adapter.postTexts() {
				if strings.Contains(p, "if the task is still running") {
					return true
				}
			}
			return false
		})
	})
}

// TestStopWithNothingToStopStartsNoTask: a stop word with nothing running —
// first contact, or the impatient second "stop" while a cancel awaits its
// terminal — must be answered deterministically, never published as a task
// that literally reads "stop".
func TestStopWithNothingToStopStartsNoTask(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-stopguard"

	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sg-1", Text: "stop"}
	waitFor(t, "bare stop answered", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "nothing is running") {
				return true
			}
		}
		return false
	})
	rec, err := r.g.reg.Get(context.Background(), conv)
	if err != nil {
		t.Fatal(err)
	}
	if rec != nil && (rec.ActiveTask != nil || len(rec.Tasks) != 0) {
		t.Fatalf("bare stop started a task: %+v", rec)
	}

	// A real task, a real stop, then the impatient second stop.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sg-2", Text: "run the audit"}
	r.awaitTask(t, "platform")
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sg-3", Text: "stop"}
	waitFor(t, "task detached", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "sg-4", Text: "stop"}
	waitFor(t, "second stop answered", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "cancel already sent") {
				return true
			}
		}
		return false
	})
	rec, err = r.g.reg.Get(context.Background(), conv)
	if err != nil {
		t.Fatal(err)
	}
	if len(rec.Tasks) != 1 {
		t.Fatalf("second stop started a task: %d tasks recorded", len(rec.Tasks))
	}
}

// TestDetachedTaskNarrowsTheStatusMatcher: the wide interrogative rule is
// justified where the alternative reading is a refused steer. After a stop
// the alternative is a NEW task, so a status-shaped new ask must start one
// rather than replaying the dead task — while the exact phrases still
// answer status.
func TestDetachedTaskNarrowsTheStatusMatcher(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-detachwide"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "dw-1", Text: "run the audit"}
	r.awaitTask(t, "platform")
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "dw-2", Text: "stop"}
	waitFor(t, "task detached", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.ActiveTask != nil && rec.ActiveTask.Detached
	})
	// Wide-shaped but not an exact phrase: a new ask, not a status poke.
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "dw-3", Text: "any update on the rollout"}
	waitFor(t, "new task started", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && len(rec.Tasks) == 2 &&
			rec.ActiveTask != nil && rec.ActiveTask.Ask == "any update on the rollout"
	})
}

// TestPreDeleteGuardKeepsPodWhenReplayFails: a replay failure means an
// existing final could not be ruled out, so the pre-delete path must keep
// the pod and publish nothing — proceeding would risk the second
// `final: true` assertion 10 forbids. TaskNotFound (a real "no events"
// answer) still proceeds; that case is covered by the supervisor tests.
func TestPreDeleteGuardKeepsPodWhenReplayFails(t *testing.T) {
	r := startRig(t)
	rec := &SessionRecord{Key: "discord:g1/thread-guard", ContextID: "ctx-guard",
		BusSession: "chat-guard-0001", Addressee: "chat-guard-0001", PodName: "chat-guard-0001",
		ActiveTask: &ActiveTask{TaskID: "task-guard", CorrelationID: "corr-guard", Detached: true},
		Tasks:      []TaskRef{{ID: "task-guard", Addressee: "chat-guard-0001"}},
	}
	cctx, cancel := context.WithCancel(context.Background())
	cancel() // every TasksGet under this context fails as transport, not TaskNotFound
	if r.g.closeDetachedBeforeDelete(cctx, rec) {
		t.Fatal("closeDetachedBeforeDelete proceeded although replay could not rule out an existing final")
	}
	finals, err := finalsFor(r.url, "chat-guard-0001", "task-guard")
	if err != nil {
		t.Fatal(err)
	}
	if len(finals) != 0 {
		t.Fatalf("a terminal was published despite the replay failure: %+v", finals)
	}
}

// TestHealPostsTheLostTerminal: healing a stale ActiveTask means the render
// was lost (an acked event is never redelivered), so the heal posts the
// replayed status card instead of clearing silently.
func TestHealPostsTheLostTerminal(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-heal"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "h-1", Text: "summarize the fleet"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateSubmitted, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	// Wait for the normal retire, then re-inject the stale record the
	// lost-render crash would have left behind.
	waitFor(t, "normal retire", func() bool {
		rec, err := r.g.reg.Get(ctx, conv)
		return err == nil && rec != nil && rec.ActiveTask == nil
	})
	rec, err := r.g.reg.Get(ctx, conv)
	if err != nil {
		t.Fatal(err)
	}
	rec.ActiveTask = &ActiveTask{TaskID: origin.TaskID, CorrelationID: origin.CorrelationID,
		Ask: "summarize the fleet", SubmittedAt: time.Now()}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "h-2", Text: "hello again"}
	waitFor(t, "healed terminal card posted", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, origin.TaskID) && strings.Contains(p, string(lib.StateCompleted)) {
				return true
			}
		}
		return false
	})
}

// TestCompletedResultRendersExactlyOnce: the worker adapter has no explicit
// progress tool — assistant text becomes `progress` and the final text
// becomes `result`, so on a single-turn task the SAME text arrives as both
// (W4's documented deviation). The rolling line must therefore drop its
// progress tail at `completed`, where the result is posted separately, or
// the room sees the answer twice — observed live in Discord, 9/3. The
// other terminals post no result, so their tail is genuine context and
// stays.
func TestCompletedResultRendersExactlyOnce(t *testing.T) {
	const answer = "There once was a pod in a queue"
	r := startRig(t)
	conv := "discord:g1/thread-double"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "dr-1", Text: "write me a limerick"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress,
		Parts: []lib.Part{{Kind: "text", Text: answer}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactResult,
		Parts: []lib.Part{{Kind: "text", Text: answer}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateCompleted, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "result posted", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, answer) {
				return true
			}
		}
		return false
	})
	waitFor(t, "terminal rolling line", func() bool {
		edits := r.adapter.editTexts()
		return len(edits) > 0 && strings.Contains(edits[len(edits)-1], string(lib.StateCompleted))
	})
	// What the user is left looking at: the posts, plus the FINAL state of
	// the rolling line (intermediate edits are overwritten in place).
	// The answer appears exactly once.
	seen := 0
	for _, p := range r.adapter.postTexts() {
		seen += strings.Count(p, answer)
	}
	edits := r.adapter.editTexts()
	finalLine := edits[len(edits)-1]
	seen += strings.Count(finalLine, answer)
	if seen != 1 {
		t.Fatalf("the answer renders %d times (final line %q, posts %q); want exactly once",
			seen, finalLine, r.adapter.postTexts())
	}
}

// TestNonCompletedTerminalKeepsTheNarrationTail: failed/canceled/rejected
// post no result, so the last narration on the rolling line is genuine
// context there and must survive the completed-tail fix.
func TestNonCompletedTerminalKeepsTheNarrationTail(t *testing.T) {
	r := startRig(t)
	conv := "discord:g1/thread-failtail"
	r.adapter.inbox <- InboundMessage{Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "ft-1", Text: "check node pressure"}
	origin := r.awaitTask(t, "platform")
	exec := r.execFor(t, origin, "platform")
	ctx := context.Background()
	if err := exec.PublishStatus(ctx, lib.StateWorking, false); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishArtifact(ctx, lib.Artifact{Name: lib.ArtifactProgress,
		Parts: []lib.Part{{Kind: "text", Text: "was checking node pressure"}}}); err != nil {
		t.Fatal(err)
	}
	if err := exec.PublishStatus(ctx, lib.StateFailed, true); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "failed rolling line keeps the narration", func() bool {
		edits := r.adapter.editTexts()
		if len(edits) == 0 {
			return false
		}
		last := edits[len(edits)-1]
		return strings.Contains(last, string(lib.StateFailed)) &&
			strings.Contains(last, "was checking node pressure")
	})
}

// TestSpawnSetsSeccompRuntimeDefault: the operator's own workloads pin
// seccompProfile RuntimeDefault; a session pod without it is rejected
// outright in a namespace enforcing restricted pod security — which would
// land every spawn in the failure path above.
func TestSpawnSetsSeccompRuntimeDefault(t *testing.T) {
	cs := k8sfake.NewSimpleClientset()
	cfg := &Config{Namespace: "test-ns", WorkerImage: "img", NATSCredsSecret: "creds",
		TaskDeadline: 15 * time.Minute}
	s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
	rec := &SessionRecord{Key: "discord:g1/t", ContextID: "ctx-1",
		BusSession: "chat-otter-seccomp", Addressee: "chat-otter-seccomp"}
	if _, err := s.Spawn(context.Background(), rec, "task-1", ""); err != nil {
		t.Fatal(err)
	}
	pod, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-otter-seccomp", metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	psc := pod.Spec.SecurityContext
	if psc == nil || psc.SeccompProfile == nil || psc.SeccompProfile.Type != corev1.SeccompProfileTypeRuntimeDefault {
		t.Fatalf("pod securityContext missing seccompProfile RuntimeDefault: %+v", psc)
	}
	csc := pod.Spec.Containers[0].SecurityContext
	if csc == nil || csc.SeccompProfile == nil || csc.SeccompProfile.Type != corev1.SeccompProfileTypeRuntimeDefault {
		t.Fatalf("container securityContext missing seccompProfile RuntimeDefault: %+v", csc)
	}
}
