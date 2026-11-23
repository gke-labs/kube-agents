package gateway

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	k8sfake "k8s.io/client-go/kubernetes/fake"
	k8stesting "k8s.io/client-go/testing"
)

// capRig is a gateway with a session cap over a fake cluster holding the
// given session pods, each with an idle conversation record, so the cap's
// count, eviction and slot reservation run against them (#6033).
func capRig(t *testing.T, limit int, idle ...string) (*Gateway, *k8sfake.Clientset, *fakeAdapter) {
	t.Helper()
	r, _ := startReuseRig(t)
	ctx := context.Background()
	var objs []runtime.Object
	for i, name := range idle {
		key := fmt.Sprintf("discord:g1/t-cap-%s", name)
		rec := &SessionRecord{Key: key, ContextID: fmt.Sprintf("ctx-%d", i), Kind: "group", Profile: "chat",
			Addressee: name, BusSession: name, PodName: name, PodReuse: true, SessionRouted: true,
			LastUserMessage: time.Now().Add(-time.Hour + time.Duration(i)*time.Minute).UTC()}
		if err := r.g.reg.Put(ctx, rec); err != nil {
			t.Fatal(err)
		}
		objs = append(objs, &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns",
				Labels:      map[string]string{labelPartOf: partOfValue, labelRole: sessionRole},
				Annotations: map[string]string{annoConvo: key, annoReuse: reuseValue}},
			Status: corev1.PodStatus{Phase: corev1.PodRunning},
		})
	}
	cs := k8sfake.NewSimpleClientset(objs...)
	g := &Gateway{
		cfg:          &Config{MaxSessions: limit},
		spawner:      &podSpawner{cfg: &Config{Namespace: "test-ns"}, client: cs, log: slog.Default()},
		reg:          r.g.reg,
		adapter:      r.adapter,
		log:          slog.Default(),
		sessionLocks: map[string]*sessionLockEntry{},
	}
	return g, cs, r.adapter
}

func livePods(t *testing.T, cs *k8sfake.Clientset) int {
	t.Helper()
	pods, err := cs.CoreV1().Pods("test-ns").List(context.Background(), metav1.ListOptions{})
	if err != nil {
		t.Fatal(err)
	}
	return len(pods.Items)
}

// A burst at the cap can't overshoot it: conversations checking together are
// serialized, and a slot granted but not yet spawned counts, so with a cap
// of 3 and nothing to evict, exactly 3 of 5 simultaneous conversations get a
// slot and 2 are refused. Before #6033 every one counted the same live pods
// and all 5 passed.
func TestABurstAtTheSessionCapDoesNotOvershootIt(t *testing.T) {
	g, cs, _ := capRig(t, 3)
	// A pod list that takes a moment, as a real one does, so checks that
	// weren't serialized would all read the same holds before any of them
	// records its own.
	cs.PrependReactor("list", "pods", func(k8stesting.Action) (bool, runtime.Object, error) {
		time.Sleep(20 * time.Millisecond)
		return false, nil, nil
	})
	var wg sync.WaitGroup
	var mu sync.Mutex
	granted := 0
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			if !g.refuseAtSessionCap(context.Background(), &SessionRecord{Key: fmt.Sprintf("discord:g1/t-burst-%d", i)}, false) {
				mu.Lock()
				granted++
				mu.Unlock()
			}
		}(i)
	}
	wg.Wait()
	if granted != 3 {
		t.Fatalf("%d of 5 simultaneous conversations got a slot under a cap of 3", granted)
	}
}

// Over the cap with idle pods (a burst before #6033, or a lowered cap), a new
// conversation evicts the idle surplus, so the count comes back to the cap:
// 4 idle pods under a cap of 2 lose 3, and the newcomer takes the freed slot.
// Before, it evicted one and the count stayed at 4.
func TestAnOverCapCountConvergesByEvictingTheIdleSurplus(t *testing.T) {
	g, cs, _ := capRig(t, 2, "chat-vole-a", "chat-vole-b", "chat-vole-c", "chat-vole-d")
	if g.refuseAtSessionCap(context.Background(), &SessionRecord{Key: "discord:g1/t-new"}, false) {
		t.Fatal("refused, though idle pods could make room")
	}
	if n := livePods(t, cs); n != 1 {
		t.Fatalf("%d session pods left; want 1, so the newcomer's pod brings the count to the cap of 2", n)
	}
	// The survivor is the most recently active.
	if _, err := cs.CoreV1().Pods("test-ns").Get(context.Background(), "chat-vole-d", metav1.GetOptions{}); err != nil {
		t.Errorf("the most recently active pod was evicted: %v", err)
	}
}

// A granted slot ends when the spawn returns (releaseCapHold), and one never
// spawned stops counting after sessionCapHoldTTL, so neither leaks a slot.
// A conversation's own earlier slot doesn't count against its new check.
func TestAGrantedSessionSlotEndsAtTheSpawnOrExpires(t *testing.T) {
	g, _, _ := capRig(t, 1)
	ctx := context.Background()
	first := &SessionRecord{Key: "discord:g1/t-first"}
	if g.refuseAtSessionCap(ctx, first, false) {
		t.Fatal("the first conversation was refused under an empty cap of 1")
	}
	second := &SessionRecord{Key: "discord:g1/t-second"}
	if !g.refuseAtSessionCap(ctx, second, false) {
		t.Fatal("a second conversation got a slot while the first's was still held")
	}
	if g.refuseAtSessionCap(ctx, first, false) {
		t.Error("a conversation's own held slot counted against its next check")
	}
	g.releaseCapHold(first.Key)
	if g.refuseAtSessionCap(ctx, second, false) {
		t.Fatal("the slot stayed held after the first conversation's spawn returned")
	}
	// The second's slot, never spawned, expires.
	g.capMu.Lock()
	g.capHolds[second.Key] = capHold{at: time.Now().Add(-sessionCapHoldTTL - time.Second)}
	g.capMu.Unlock()
	if g.refuseAtSessionCap(ctx, &SessionRecord{Key: "discord:g1/t-third"}, false) {
		t.Fatal("an expired slot still counted against the cap")
	}
}

// With fewer idle pods than the surplus, the turn is refused after the idle
// ones go, and the refusal names the count after them, not before.
func TestAPartialEvictionRefusesWithTheCountAfterIt(t *testing.T) {
	g, cs, adapter := capRig(t, 1, "chat-vole-a", "chat-vole-b")
	ctx := context.Background()
	busy, err := g.reg.Get(ctx, "discord:g1/t-cap-chat-vole-b")
	if err != nil || busy == nil {
		t.Fatal(err)
	}
	busy.ActiveTask = &ActiveTask{TaskID: "task-b", CorrelationID: "corr-b", SubmittedAt: time.Now()}
	if err := g.reg.Put(ctx, busy); err != nil {
		t.Fatal(err)
	}
	newcomer := &SessionRecord{Key: "discord:g1/t-new"}
	if !g.refuseAtSessionCap(ctx, newcomer, false) {
		t.Fatal("granted a slot with 2 live under a cap of 1 and only 1 idle")
	}
	if n := livePods(t, cs); n != 1 {
		t.Errorf("%d pods left; the idle one should have gone", n)
	}
	posts := adapter.postTextsFor(newcomer.Key)
	if len(posts) != 1 || !strings.Contains(posts[0], "1 session worker is already running") {
		t.Errorf("refusal %q; want it to name the 1 pod left", posts)
	}
	if paused := adapter.postTextsFor("discord:g1/t-cap-chat-vole-a"); len(paused) != 1 || paused[0] != noticeSessionPaused {
		t.Errorf("the evicted conversation was told %q", paused)
	}
}

// A turn that takes a cap slot leaves none held when it ends, whether its
// spawn worked or failed, so a failure between the grant and the pod never
// counts against the cap afterwards.
func TestATurnLeavesNoCapSlotHeldWhenItsSpawnWorksOrFails(t *testing.T) {
	for _, fail := range []bool{false, true} {
		t.Run(fmt.Sprintf("spawn fails=%v", fail), func(t *testing.T) {
			r, spawn := startReuseRig(t)
			if fail {
				spawn.failSpawns(errors.New("injected spawn failure"))
			}
			conv := fmt.Sprintf("discord:g1/t-held-%v", fail)
			say(r, conv, "m-1", "what's running?")
			// Past the grant: the pod spawned, or the spawn failed and its
			// supervisor terminal ended the task.
			if fail {
				waitFor(t, "the failed spawn's terminal", func() bool {
					rec, err := r.g.reg.Get(context.Background(), conv)
					return err == nil && rec != nil && len(rec.Tasks) > 0 && rec.ActiveTask == nil
				})
			} else {
				waitFor(t, "the spawn", func() bool { return len(spawn.calls()) == 1 })
			}
			waitFor(t, "the turn's slot released", func() bool {
				r.g.capMu.Lock()
				defer r.g.capMu.Unlock()
				_, held := r.g.capHolds[conv]
				return !held
			})
		})
	}
}

// A replacing turn's slot doesn't count while the pod it replaces is still
// running: that pod is already counted, and the slot is its successor's. So
// another conversation's check sees one pod, not two, and evicts nothing.
func TestAReplacingSlotIsNotCountedBesideThePodItReplaces(t *testing.T) {
	g, cs, _ := capRig(t, 2, "chat-vole-b")
	g.holdCapSlot("discord:g1/t-cap-chat-vole-b", "chat-vole-b")
	if g.refuseAtSessionCap(context.Background(), &SessionRecord{Key: "discord:g1/t-other"}, false) {
		t.Fatal("refused under a cap of 2 with one pod and its own successor's slot")
	}
	if n := livePods(t, cs); n != 1 {
		t.Errorf("%d pods left; the replaced pod was evicted as if counted twice", n)
	}
}

// A turn whose context ends while another conversation's check holds the
// cap's lock is refused with its own notice, posted after the wait, rather
// than waiting past its budget.
func TestATurnThatWaitsOutItsContextForTheCapIsToldSo(t *testing.T) {
	g, _, adapter := capRig(t, 3)
	if !g.acquireCapCheck(context.Background()) {
		t.Fatal("could not hold the cap check")
	}
	defer g.releaseCapCheck()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	rec := &SessionRecord{Key: "discord:g1/t-waited"}
	if !g.refuseAtSessionCap(ctx, rec, false) {
		t.Fatal("granted a slot without the cap check")
	}
	const want = "⚠️ not started: other conversations were starting session workers and this one waited too long — try again in a moment"
	if posts := adapter.postTextsFor(rec.Key); len(posts) != 1 || posts[0] != want {
		t.Errorf("posts %q; want the waited-too-long notice", posts)
	}
}

// A slot a turn holds ends with the turn even when the turn never reaches a
// spawn (a retire or a publish that fails first): the conversation's locked
// turn releases it as it ends. Here a held slot is released by a turn that
// starts no pod at all.
func TestAHeldCapSlotEndsWithATurnThatNeverSpawns(t *testing.T) {
	r, spawn := startReuseRig(t)
	conv := "discord:g1/t-held-nospawn"
	r.g.holdCapSlot(conv, "")
	say(r, conv, "m-stop", "stop")
	waitFor(t, "the turn's slot released", func() bool {
		r.g.capMu.Lock()
		defer r.g.capMu.Unlock()
		_, held := r.g.capHolds[conv]
		return !held
	})
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns; a stop with nothing running should start no pod", n)
	}
}
