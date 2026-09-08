package gateway

import (
	"context"
	"errors"
	"log/slog"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"
)

// The cap tests drive fakeSpawner's live count by hand: the fake's spawn
// ledger tracks calls, not cluster truth, and conflating the two would test
// the fake. setLive is the "what the k8s API says" dial.
func (s *fakeSpawner) setLive(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.live = n
}

func (s *fakeSpawner) setLiveErr(err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.liveErr = err
}

func (s *fakeSpawner) LiveSessions(context.Context) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.live, s.liveErr
}

// TestDelegateRefusedAtSessionCap: "Delegate:" makes pod creation
// user-triggerable and threads are free, so the cap is the gateway's
// usability bound. At the cap the delegation is refused with a reply naming
// the numbers - never silently queued, never dropped - and nothing is
// spawned or published. When capacity frees, the same conversation
// delegates normally.
func TestDelegateRefusedAtSessionCap(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 2)
	spawn.setLive(2)
	conv := "discord:g1/thread-cap1"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-100", Text: "Delegate: write a haiku",
	}
	waitFor(t, "refusal names the numbers", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "2 session workers") && strings.Contains(p, "cap 2") {
				return true
			}
		}
		return false
	})
	// The refusal post is ordered after the check, so by now a spawn or a
	// placeholder would already be visible.
	if got := len(spawn.calls()); got != 0 {
		t.Fatalf("spawned %d pods at the cap", got)
	}
	for _, p := range r.adapter.postTexts() {
		if strings.Contains(p, "submitted") {
			t.Fatalf("task started at the cap: %q", p)
		}
	}

	spawn.setLive(1)
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-101", Text: "Delegate: write a haiku",
	}
	waitFor(t, "spawn under the cap", func() bool { return len(spawn.calls()) == 1 })
}

// TestDelegateReplacementNotDoubleCounted: a Delegate on a conversation that
// still holds an incarnation retires that pod in the same turn, so the doomed
// pod must not occupy the slot the new one needs - otherwise a full board can
// never be re-delegated, only abandoned.
func TestDelegateReplacementNotDoubleCounted(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 1)
	conv := "discord:g1/thread-cap2"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-cap2", Kind: "group",
		Addressee: "chat-otter-aaaa", BusSession: "chat-otter-aaaa",
		PodName: "chat-otter-aaaa",
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	spawn.setLive(1) // the conversation's own lingering pod is the 1

	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-110", Text: "Delegate: try again",
	}
	waitFor(t, "replacement spawn", func() bool { return len(spawn.calls()) == 1 })
	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != "chat-otter-aaaa" {
		t.Fatalf("previous incarnation not retired: %v", deleted)
	}
}

// TestSessionRouteRefusedAtSessionCap: with the default route on session
// pods, a plain first ask spawns - so the cap must hold there too, or
// Delegate refusals just push the flood one affordance over.
func TestSessionRouteRefusedAtSessionCap(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 1)
	spawn.setLive(1)
	conv := "discord:g1/thread-cap3"
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-120", Text: "check the fleet",
	}
	waitFor(t, "refusal names the numbers", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "1 session worker") && strings.Contains(p, "cap 1") {
				return true
			}
		}
		return false
	})
	if got := len(spawn.calls()); got != 0 {
		t.Fatalf("spawned %d pods at the cap", got)
	}

	spawn.setLive(0)
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-121", Text: "check the fleet",
	}
	waitFor(t, "spawn under the cap", func() bool { return len(spawn.calls()) == 1 })
}

// TestSessionCapCountErrorRefusesHonestly: if the live count cannot be read
// the gateway says so and starts nothing - proceeding blind would make the
// cap advisory exactly when the API is misbehaving, and silence is the
// "looked like a dropped message" shape this branch keeps re-fixing.
func TestSessionCapCountErrorRefusesHonestly(t *testing.T) {
	r, spawn := startRigWithSpawnerCap(t, "platform", 2)
	spawn.setLiveErr(errors.New("boom"))
	r.adapter.inbox <- InboundMessage{
		Conversation: "discord:g1/thread-cap4", Kind: "group",
		AuthorID: "1001", MessageID: "c-130", Text: "Delegate: write a haiku",
	}
	waitFor(t, "honest failure post", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "not started") {
				return true
			}
		}
		return false
	})
	if got := len(spawn.calls()); got != 0 {
		t.Fatalf("spawned %d pods without a count", got)
	}
}

// TestPodSpawnerLiveSessionsCountsNonTerminal: the denominator is what the
// k8s API holds - session-labeled pods in this namespace that are not yet
// terminal. Succeeded/Failed pods are sweep's inventory, not load; other
// workloads and other namespaces are not ours to count.
func TestPodSpawnerLiveSessionsCountsNonTerminal(t *testing.T) {
	mk := func(name, ns string, phase corev1.PodPhase, sessionLabels bool) *corev1.Pod {
		labels := map[string]string{}
		if sessionLabels {
			labels = map[string]string{labelPartOf: partOfValue, labelRole: sessionRole}
		}
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, Labels: labels},
			Status:     corev1.PodStatus{Phase: phase},
		}
	}
	cs := k8sfake.NewSimpleClientset(
		mk("running", "test-ns", corev1.PodRunning, true),
		mk("pending", "test-ns", corev1.PodPending, true),
		mk("done", "test-ns", corev1.PodSucceeded, true),
		mk("dead", "test-ns", corev1.PodFailed, true),
		mk("bystander", "test-ns", corev1.PodRunning, false),
		mk("elsewhere", "other-ns", corev1.PodRunning, true),
	)
	s := &podSpawner{cfg: &Config{Namespace: "test-ns"}, client: cs, log: slog.Default()}
	n, err := s.LiveSessions(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if n != 2 {
		t.Fatalf("LiveSessions = %d, want 2 (running + pending)", n)
	}
}

// TestFromEnvMaxSessions: the env contract for the cap - absent means the
// documented default, and a value the cap cannot honestly enforce (zero,
// negative, non-numeric) refuses at boot rather than surprising at spawn.
func TestFromEnvMaxSessions(t *testing.T) {
	t.Setenv("NATS_URL", "nats://127.0.0.1:4222")
	t.Setenv("NATS_PASSWORD", "pw")
	t.Setenv("DISCORD_TOKEN", "x")
	// Pin against the invoking shell: envOr treats empty as unset, so this
	// asserts the default even if the developer exports a real value.
	t.Setenv("A2A_MAX_SESSIONS", "")

	cfg, err := FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MaxSessions != 10 {
		t.Fatalf("default MaxSessions = %d, want 10", cfg.MaxSessions)
	}

	t.Setenv("A2A_MAX_SESSIONS", "3")
	cfg, err = FromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MaxSessions != 3 {
		t.Fatalf("MaxSessions = %d, want 3", cfg.MaxSessions)
	}

	for _, bad := range []string{"0", "-1", "ten"} {
		t.Setenv("A2A_MAX_SESSIONS", bad)
		if _, err := FromEnv(); err == nil {
			t.Fatalf("A2A_MAX_SESSIONS=%q accepted", bad)
		}
	}
}

// TestSessionRoutedRecordWithoutSpawnerDoesNotPanic: SessionRouted persists
// in the KV record; the spawner is a setting (A2A_SPAWN_SESSIONS). A W4
// rollback therefore leaves session-routed records behind with no spawner
// to count against - the turn must degrade the way it always did (publish
// toward an addressee nothing owns), not kill the gateway on the nil
// spawner inside the cap check.
func TestSessionRoutedRecordWithoutSpawnerDoesNotPanic(t *testing.T) {
	r := startRig(t) // spawner dark
	conv := "discord:g1/thread-rollback"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-rollback", Kind: "group",
		Addressee: "chat-otter-dead", BusSession: "chat-otter-dead",
		SessionRouted: true, Profile: "chat",
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-140", Text: "hello again",
	}
	waitFor(t, "task still starts after a rollback", func() bool {
		for _, p := range r.adapter.postTexts() {
			if strings.Contains(p, "submitted") {
				return true
			}
		}
		return false
	})
}

// TestPreFlipUpgradeRetiresLingeringPod: the W4-upgrade branch must apply
// the Delegate branch's delete-and-clear to a lingering incarnation before
// minting the next one. Left set, the stale PodName makes ensureSessionPod
// a no-op - the task publishes to an addressee with no executor (the
// dropped-message shape) - and a wedged Running pod holds a cap slot
// forever, since the active conversation keeps resetting the reap clock and
// sweep only sees terminal phases.
func TestPreFlipUpgradeRetiresLingeringPod(t *testing.T) {
	r, spawn := startRigWithSpawnerRoute(t, RouteSession)
	conv := "discord:g1/thread-upgrade-pod"
	rec := &SessionRecord{
		Key: conv, ContextID: "ctx-upgrade", Kind: "group",
		Addressee:  "platform", // pre-flip fixed route
		BusSession: "chat-otter-stale", PodName: "chat-otter-stale",
	}
	if err := r.g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
	r.adapter.inbox <- InboundMessage{
		Conversation: conv, Kind: "group",
		AuthorID: "1001", MessageID: "c-150", Text: "check the fleet",
	}
	waitFor(t, "fresh incarnation for the upgraded record", func() bool { return len(spawn.calls()) == 1 })
	if call := spawn.calls()[0]; call.Session == "chat-otter-stale" {
		t.Fatalf("upgrade reused the stale incarnation %q", call.Session)
	}
	if deleted := spawn.deleted(); len(deleted) != 1 || deleted[0] != "chat-otter-stale" {
		t.Fatalf("stale incarnation not retired: %v", deleted)
	}
}
