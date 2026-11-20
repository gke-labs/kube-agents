package gateway

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"slices"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/nats-io/nats.go/jetstream"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	k8sfake "k8s.io/client-go/kubernetes/fake"

	"github.com/gke-labs/kube-agents/a2a/lib"
)

// The warm pool (warm_pool.go): its size against the cap, the claim and when
// it is made, the cap count and the sweeps over warm pods, recycling, and
// what a gateway that predates the pool sees.

func warmOn(warm, maxSessions int) func(*Config) {
	return func(c *Config) {
		c.SessionReuse = true
		c.WarmSessions = warm
		c.MaxSessions = maxSessions
	}
}

// startWarmRig is the post-flip gateway with reuse on and a pool of one under
// a cap of four.
func startWarmRig(t *testing.T) (*rig, *fakeSpawner) {
	t.Helper()
	return startRigWithSpawnerCap(t, RouteSession, 0, warmOn(1, 4))
}

func readyWarm(name string) warmPod {
	return warmPod{Name: name, Created: time.Now(), Ready: true, Current: true}
}

func warmClaims(t *testing.T, r *rig) map[string]string {
	t.Helper()
	claims, err := r.g.reg.WarmClaims(context.Background())
	if err != nil {
		t.Fatalf("claim index: %v", err)
	}
	return claims
}

// The pool's size across caps: the configured count, never the cap's last
// slot, nothing below a cap of two, and nothing when the count is 0.
func TestTheWarmPoolNeverTakesTheLastSlot(t *testing.T) {
	for _, tc := range []struct {
		warm, maxSessions, live, want int
	}{
		{1, 10, 0, 1},
		{1, 10, 9, 0},
		{1, 10, 8, 1},
		{1, 4, 0, 1},
		{1, 4, 2, 1},
		{1, 4, 3, 0},
		{3, 4, 0, 3},
		{3, 4, 1, 2},
		{5, 4, 0, 3},
		{1, 2, 0, 1},
		{1, 2, 1, 0},
		{1, 1, 0, 0},
		{0, 10, 0, 0},
		{1, 4, 6, 0},
	} {
		if got := warmPoolSize(tc.warm, tc.maxSessions, tc.live); got != tc.want {
			t.Errorf("warmPoolSize(warm %d, cap %d, live %d) = %d, want %d", tc.warm, tc.maxSessions, tc.live, got, tc.want)
		}
	}
	for _, tc := range []struct {
		name string
		cfg  Config
		want bool
	}{
		{"on", Config{DefaultAddressee: RouteSession, SessionReuse: true, WarmSessions: 1, MaxSessions: 2}, true},
		{"the platform route", Config{DefaultAddressee: "platform", SessionReuse: true, WarmSessions: 1, MaxSessions: 10}, false},
		{"warmSessions 0", Config{DefaultAddressee: RouteSession, SessionReuse: true, WarmSessions: 0, MaxSessions: 10}, false},
		{"a cap below two", Config{DefaultAddressee: RouteSession, SessionReuse: true, WarmSessions: 1, MaxSessions: 1}, false},
		{"reuse off", Config{DefaultAddressee: RouteSession, SessionReuse: false, WarmSessions: 1, MaxSessions: 10}, false},
	} {
		cfg := tc.cfg
		g := &Gateway{cfg: &cfg, spawner: &fakeSpawner{}}
		if got := g.warmPoolOn(); got != tc.want {
			t.Errorf("%s: warmPoolOn = %v, want %v", tc.name, got, tc.want)
		}
	}
	if (&Gateway{cfg: &Config{DefaultAddressee: RouteSession, SessionReuse: true, WarmSessions: 1, MaxSessions: 10}}).warmPoolOn() {
		t.Error("a gateway with no spawner keeps a pool")
	}
}

// warmSessions 0 starts no pool: the leader creates no warm pod.
func TestWarmSessionsZeroCreatesNoWarmPod(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, warmOn(0, 10))
	if r.g.warmPoolOn() {
		t.Fatal("the pool is on at warmSessions 0")
	}
	time.Sleep(200 * time.Millisecond)
	if got := spawn.warmSpawned(); len(got) != 0 {
		t.Fatalf("warm pods created at warmSessions 0: %v", got)
	}
}

// A new conversation's first turn is handed the Ready warm pod: no spawn, the
// task and its capability addressed to the pod's own name, the record naming
// the pod as its reused pod, and the claim index naming the conversation.
func TestANewConversationsFirstTurnIsHandedAReadyWarmPod(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	const pod = "chat-otter-w1aa"
	spawn.setWarm(readyWarm(pod))
	conv := "discord:g1/t-warm-claim"
	say(r, conv, "m1", "what pods are failing")
	origin := r.awaitTask(t, pod)
	waitFor(t, "the record naming the warm pod", func() bool {
		rec, err := r.g.reg.Get(context.Background(), conv)
		return err == nil && rec != nil && rec.PodName == pod
	})
	if n := len(spawn.calls()); n != 0 {
		t.Fatalf("%d spawns; the warm pod should have taken the first turn", n)
	}
	rec, _ := r.g.reg.Get(context.Background(), conv)
	if rec.BusSession != pod || rec.Addressee != pod || !rec.PodReuse {
		t.Fatalf("record busSession=%q addressee=%q podReuse=%v, want the warm pod %q, reused", rec.BusSession, rec.Addressee, rec.PodReuse, pod)
	}
	var auth Authority
	if err := json.Unmarshal(origin.Authority, &auth); err != nil {
		t.Fatal(err)
	}
	assertRootCapability(t, r, auth, origin.TaskID, pod)
	if got := warmClaims(t, r)[pod]; got != conv {
		t.Fatalf("claim index names %q for %s, want %q", got, pod, conv)
	}
}

// A conversation with an earlier turn has a primer to rehydrate, which a
// warm pod has no way to read, so its next incarnation is spawned cold even
// with a warm pod Ready.
func TestATurnWithAPrimerSpawnsColdWithAWarmPodReady(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	conv := "discord:g1/t-warm-primer"
	say(r, conv, "m1", "first question")
	waitFor(t, "first spawn", func() bool { return len(spawn.calls()) == 1 })
	first := spawn.calls()[0].Session
	completeTask(t, r.execFor(t, r.awaitTask(t, first), first), "first answer")
	waitIdle(t, r, conv)

	const pod = "chat-otter-w2bb"
	spawn.setWarm(readyWarm(pod))
	say(r, conv, "m2", "second question")
	waitFor(t, "second spawn", func() bool { return len(spawn.calls()) == 2 })
	if got := spawn.calls()[1].Session; got == pod {
		t.Fatalf("the rehydrating turn was spawned as the warm pod's name %s", got)
	}
	if claims := warmClaims(t, r); len(claims) != 0 {
		t.Fatalf("a turn with a primer claimed a warm pod: %v", claims)
	}
	if envs := inSubjectEnvelopes(t, r.url, pod); len(envs) != 0 {
		t.Fatalf("%d envelope(s) reached the warm pod's subjects", len(envs))
	}
}

// A Delegate is a one-shot pod, never a warm one, even as a session-routed
// conversation's first turn.
func TestADelegateNeverClaimsAWarmPod(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	const pod = "chat-otter-w3cc"
	spawn.setWarm(readyWarm(pod))
	conv := "discord:g1/t-warm-delegate"
	say(r, conv, "m1", "delegate: check the nodes")
	waitFor(t, "the delegate's spawn", func() bool { return len(spawn.calls()) == 1 })
	if got := spawn.calls()[0].Session; got == pod {
		t.Fatalf("the Delegate was spawned as the warm pod %s", got)
	}
	if claims := warmClaims(t, r); len(claims) != 0 {
		t.Fatalf("a Delegate claimed a warm pod: %v", claims)
	}
}

// With no warm pod that can take it, the first turn spawns cold as before:
// none Ready yet, one past its claim window, one ending, or one another
// conversation claimed.
func TestAFirstTurnSpawnsColdWithNoClaimableWarmPod(t *testing.T) {
	t.Parallel()
	const pod = "chat-otter-w4dd"
	for _, tc := range []struct {
		name    string
		warm    warmPod
		claimed bool
	}{
		{"not ready", warmPod{Name: pod, Created: time.Now(), Current: true}, false},
		{"made under an earlier config", warmPod{Name: pod, Created: time.Now(), Ready: true}, false},
		{"past its claim window", warmPod{Name: pod, Created: time.Now().Add(-warmClaimableAge), Ready: true, Current: true}, false},
		{"ending", warmPod{Name: pod, Created: time.Now(), Ready: true, Gone: true, Current: true}, false},
		{"claimed by another conversation", readyWarm(pod), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			r, spawn := startWarmRig(t)
			spawn.setWarm(tc.warm)
			if tc.claimed {
				if err := r.g.reg.ClaimWarmPod(context.Background(), pod, "discord:g1/t-other"); err != nil {
					t.Fatal(err)
				}
			}
			say(r, "discord:g1/t-warm-cold", "m1", "what pods are failing")
			waitFor(t, "a cold spawn", func() bool { return len(spawn.calls()) == 1 })
			if got := spawn.calls()[0].Session; got == pod {
				t.Fatalf("spawned as the warm pod's name %s", got)
			}
		})
	}
}

// warmCapPod is a live session pod in the cap rig's fake cluster: warm, or a
// spawned pod of conversation key.
func warmCapPod(t *testing.T, g *Gateway, cs *k8sfake.Clientset, name string, warm bool, key string) {
	t.Helper()
	anno := map[string]string{annoReuse: reuseValue}
	if warm {
		anno[annoWarm] = warmValue
	} else {
		anno[annoConvo] = key
	}
	_, err := cs.CoreV1().Pods("test-ns").Create(context.Background(), &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "test-ns",
			Labels: map[string]string{labelPartOf: partOfValue, labelRole: sessionRole}, Annotations: anno},
		Status: corev1.PodStatus{Phase: corev1.PodRunning},
	}, metav1.CreateOptions{})
	if err != nil {
		t.Fatal(err)
	}
}

// busyRecord stores a conversation running a task on pod, so the cap can't
// evict it.
func busyRecord(t *testing.T, g *Gateway, key, pod string) {
	t.Helper()
	rec := &SessionRecord{Key: key, ContextID: "ctx-" + pod, Kind: "group", Profile: sessionProfile, Addressee: pod, BusSession: pod,
		PodName: pod, PodReuse: true, SessionRouted: true,
		ActiveTask: &ActiveTask{TaskID: "task-" + pod, CorrelationID: "corr-" + pod, SubmittedAt: time.Now()}}
	if err := g.reg.Put(context.Background(), rec); err != nil {
		t.Fatal(err)
	}
}

// The cap counts conversations' pods, so an unclaimed warm pod holds no slot
// and a claimed one holds its conversation's. Under a cap of 2 with one busy
// conversation, a warm pod beside it leaves the second slot free; once a
// second busy conversation has claimed it, the cap is full.
func TestTheSessionCapCountsAClaimedWarmPodAndNotAnUnclaimedOne(t *testing.T) {
	g, cs, _ := capRig(t, 2)
	ctx := context.Background()
	warmCapPod(t, g, cs, "chat-vole-busy", false, "discord:g1/t-busy")
	busyRecord(t, g, "discord:g1/t-busy", "chat-vole-busy")
	warmCapPod(t, g, cs, "chat-vole-warm", true, "")

	newcomer := &SessionRecord{Key: "discord:g1/t-new"}
	if g.refuseAtSessionCap(ctx, newcomer, false) {
		t.Fatal("refused at a cap of 2 with one conversation's pod and one unclaimed warm pod: the warm pod was counted")
	}
	g.releaseCapHold(newcomer.Key)

	if err := g.reg.ClaimWarmPod(ctx, "chat-vole-warm", "discord:g1/t-claimer"); err != nil {
		t.Fatal(err)
	}
	busyRecord(t, g, "discord:g1/t-claimer", "chat-vole-warm")
	if !g.refuseAtSessionCap(ctx, newcomer, false) {
		t.Fatal("granted a slot at a cap of 2 with two busy conversations, one on a claimed warm pod")
	}
}

// The untracked sweep leaves an unclaimed warm pod alone (it is no
// conversation's, so no conversation's record can fail to name it), deletes
// a claimed one its conversation's record no longer names, and keeps one it
// does.
func TestTheUntrackedSweepTreatsWarmPodsByTheirClaim(t *testing.T) {
	g, cs, _ := capRig(t, 10)
	ctx := context.Background()
	warmCapPod(t, g, cs, "chat-vole-unclaimed", true, "")
	warmCapPod(t, g, cs, "chat-vole-tracked", true, "")
	warmCapPod(t, g, cs, "chat-vole-dropped", true, "")
	for pod, key := range map[string]string{"chat-vole-tracked": "discord:g1/t-tracked", "chat-vole-dropped": "discord:g1/t-dropped"} {
		if err := g.reg.ClaimWarmPod(ctx, pod, key); err != nil {
			t.Fatal(err)
		}
	}
	busyRecord(t, g, "discord:g1/t-tracked", "chat-vole-tracked")
	if err := g.reg.Put(ctx, &SessionRecord{Key: "discord:g1/t-dropped", ContextID: "ctx-d", Kind: "group", Addressee: "chat-vole-next",
		BusSession: "chat-vole-next", SessionRouted: true}); err != nil {
		t.Fatal(err)
	}
	g.sweepUntracked(ctx)
	for pod, want := range map[string]bool{"chat-vole-unclaimed": true, "chat-vole-tracked": true, "chat-vole-dropped": false} {
		_, err := cs.CoreV1().Pods("test-ns").Get(ctx, pod, metav1.GetOptions{})
		if (err == nil) != want {
			t.Errorf("%s present=%v after the sweep, want %v", pod, err == nil, want)
		}
	}
}

// A claimed warm pod that ends mid-task leaves its conversation's active task
// to the sweep, which closes it as supervisor, deletes the pod, releases the
// record and drops the claim, though the pod carries no task annotation. An
// unclaimed warm pod that ended is deleted with nothing published.
func TestTheSweepClosesAClaimedWarmPodsTaskAndDeletesAnUnclaimedOne(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	ctx := context.Background()
	const claimed, unclaimed = "chat-otter-w5ee", "chat-otter-w5ff"
	conv := "discord:g1/t-warm-sweep"
	rec := &SessionRecord{Key: conv, ContextID: "ctx-sweep", Kind: "group", Profile: sessionProfile, Addressee: claimed,
		BusSession: claimed, PodName: claimed, PodReuse: true, SessionRouted: true,
		ActiveTask: &ActiveTask{TaskID: "task-warm-sweep", CorrelationID: "corr-warm-sweep", SubmittedAt: time.Now()},
		Tasks:      []TaskRef{{ID: "task-warm-sweep", Addressee: claimed}}}
	if err := r.g.reg.Put(ctx, rec); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.ClaimWarmPod(ctx, claimed, conv); err != nil {
		t.Fatal(err)
	}
	spawn.setOrphans([]orphanPod{{PodName: claimed, Addressee: claimed, Warm: true}, {PodName: unclaimed, Addressee: unclaimed, Warm: true}})
	r.g.sweepOnce(ctx)

	task, err := r.bus.TasksGet(ctx, claimed, "task-warm-sweep")
	if err != nil || !task.Final || task.State != lib.StateFailed {
		t.Fatalf("the claimed warm pod's task after the sweep: %+v err=%v, want a failed final", task, err)
	}
	deleted := map[string]bool{}
	for _, d := range spawn.deleted() {
		deleted[d] = true
	}
	if !deleted[claimed] || !deleted[unclaimed] {
		t.Fatalf("deleted %v, want both warm pods", spawn.deleted())
	}
	if _, err := r.bus.TasksGet(ctx, unclaimed, "task-warm-sweep"); err == nil || !isTaskNotFound(err) {
		t.Errorf("something was published for the unclaimed warm pod: %v", err)
	}
	got, _ := r.g.reg.Get(ctx, conv)
	if got.PodName != "" {
		t.Errorf("the record still names the swept pod %q", got.PodName)
	}
	if _, ok := warmClaims(t, r)[claimed]; ok {
		t.Error("the swept pod's claim is still in the index")
	}
}

// The pool keeps its size, replaces an unclaimed pod at the end of its claim
// window (deleting it first, so it never holds more than it was sized for),
// never deletes a claimed pod however old, drops a claim whose pod is gone,
// and creates nothing while live sessions leave no slot to spare.
func TestTheWarmPoolKeepsItsSizeAndRecyclesByAge(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	ctx := context.Background()
	waitFor(t, "the leader's first warm pod", func() bool { return len(spawn.warmSpawned()) == 1 })
	r.g.warmPoolOnce(ctx)
	if n := len(spawn.warmSpawned()); n != 1 {
		t.Fatalf("%d warm pods created with one already warm, want 1", n)
	}

	const aged, old = "chat-otter-w6aa", "chat-otter-w6bb"
	spawn.setWarm(warmPod{Name: aged, Created: time.Now().Add(-warmClaimableAge), Ready: true, Current: true},
		warmPod{Name: old, Created: time.Now().Add(-2 * warmRecycleAge), Ready: true, Current: true})
	if err := r.g.reg.ClaimWarmPod(ctx, old, "discord:g1/t-old"); err != nil {
		t.Fatal(err)
	}
	if err := r.g.reg.ClaimWarmPod(ctx, "chat-otter-gone", "discord:g1/t-gone"); err != nil {
		t.Fatal(err)
	}
	r.g.warmPoolOnce(ctx)
	deleted := spawn.deleted()
	if len(deleted) != 1 || deleted[0] != aged {
		t.Fatalf("deleted %v, want only the unclaimed pod past its claim window", deleted)
	}
	if n := len(spawn.warmSpawned()); n != 2 {
		t.Fatalf("%d warm pods created, want a replacement for the aged one", n)
	}
	claims := warmClaims(t, r)
	if _, ok := claims["chat-otter-gone"]; ok {
		t.Error("a claim whose pod is gone was kept")
	}
	if claims[old] != "discord:g1/t-old" {
		t.Error("a claim whose pod is live was dropped")
	}

	// Three live sessions under a cap of four: the pool takes no slot.
	spawn.setWarm()
	spawn.mu.Lock()
	spawn.live = 3
	spawn.mu.Unlock()
	r.g.warmPoolOnce(ctx)
	if n := len(spawn.warmSpawned()); n != 2 {
		t.Fatalf("%d warm pods created with three of four slots live, want none more", n)
	}
}

// SpawnWarm builds Spawn's session pod with no task or conversation on it:
// the pod an older gateway sees after a rollback. It carries no session key
// and no task annotation, so that gateway's eviction and untracked sweep
// skip it and its cap counts it; its adapter exits unclaimed at the recycle
// age, and that gateway's sweep deletes a terminal pod with no task
// annotations. Its deadline gives a claimed pod a fresh pod's whole life, and
// it is Ready only once its readiness probe says the watcher is live.
func TestAWarmPodIsASessionPodWithNoTaskOrConversation(t *testing.T) {
	cfg := &Config{Namespace: "ns", WorkerImage: "worker", SessionServiceAccount: "sa", TaskDeadline: 30 * time.Minute,
		SessionReuse: true, SessionMaxLifetime: 4 * time.Hour}
	cs := k8sfake.NewSimpleClientset()
	s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
	const name = "chat-otter-w7aa"
	if err := s.SpawnWarm(context.Background(), name); err != nil {
		t.Fatal(err)
	}
	pod, err := cs.CoreV1().Pods("ns").Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{annoTask, annoContext, annoCorr, annoConvo, annoPrimer} {
		if _, ok := pod.Annotations[k]; ok {
			t.Errorf("a warm pod carries %s", k)
		}
	}
	if pod.Annotations[annoWarm] != warmValue || pod.Annotations[annoReuse] != reuseValue || pod.Annotations[annoAddr] != name {
		t.Errorf("annotations %v, want the warm mark, reuse, and its own name as addressee", pod.Annotations)
	}
	if len(pod.Labels) != 2 || pod.Labels[labelRole] != sessionRole || pod.Labels[labelPartOf] != partOfValue {
		t.Errorf("labels %v, want exactly the session labels", pod.Labels)
	}
	if got, want := *pod.Spec.ActiveDeadlineSeconds, int64((warmRecycleAge+podLifetime(cfg, true))/time.Second); got != want {
		t.Errorf("activeDeadlineSeconds %d, want the recycle age plus a fresh reused pod's lifetime, %d", got, want)
	}
	env := map[string]string{}
	for _, e := range pod.Spec.Containers[0].Env {
		env[e.Name] = e.Value
	}
	if _, ok := env[envTaskID]; ok {
		t.Error("a warm pod carries TASK_ID")
	}
	if _, ok := env[lib.EnvOriginSeq]; ok {
		t.Error("a warm pod carries an origin sequence")
	}
	if env["A2A_SESSION"] != name || env["PROFILE"] != sessionProfile || env[lib.EnvSessionReuse] != reuseValue ||
		env[lib.EnvSessionWarm] != warmValue || env[lib.EnvSessionWarmIdleSeconds] != strconv.Itoa(int(warmRecycleAge/time.Second)) {
		t.Errorf("env %v, want its own session, the chat profile, reuse, warm, and the recycle age as its idle bound", env)
	}
	probe := pod.Spec.Containers[0].ReadinessProbe
	if probe == nil || probe.Exec == nil || len(probe.Exec.Command) != 2 || probe.Exec.Command[1] != lib.SessionWarmReadyCommand {
		t.Errorf("readiness probe %+v, want `worker-adapter %s`", probe, lib.SessionWarmReadyCommand)
	}

	// WarmPods: Ready only past the readiness probe; Gone when terminal.
	running := corev1.PodStatus{Phase: corev1.PodRunning, StartTime: &metav1.Time{Time: time.Now()},
		ContainerStatuses: []corev1.ContainerStatus{{Name: workerContainer, State: corev1.ContainerState{Running: &corev1.ContainerStateRunning{}}}}}
	for _, tc := range []struct {
		name        string
		status      corev1.PodStatus
		ready, gone bool
	}{
		{"running, probe not passed", running, false, false},
		{"running and ready", withReady(running), true, false},
		{"exited", corev1.PodStatus{Phase: corev1.PodSucceeded}, false, true},
	} {
		pod.Status = tc.status
		if _, err := cs.CoreV1().Pods("ns").UpdateStatus(context.Background(), pod, metav1.UpdateOptions{}); err != nil {
			t.Fatal(err)
		}
		pods, err := s.WarmPods(context.Background())
		if err != nil || len(pods) != 1 {
			t.Fatalf("%s: WarmPods = %v, %v", tc.name, pods, err)
		}
		if pods[0].Ready != tc.ready || pods[0].Gone != tc.gone {
			t.Errorf("%s: Ready %v Gone %v, want %v %v", tc.name, pods[0].Ready, pods[0].Gone, tc.ready, tc.gone)
		}
	}
	pod.Status = withReady(running)
	if _, err := cs.CoreV1().Pods("ns").UpdateStatus(context.Background(), pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	live, err := s.SessionPods(context.Background())
	if err != nil || len(live) != 1 || live[0].SessionKey != "" || !live[0].Warm {
		t.Errorf("SessionPods = %+v, %v; want the warm pod, marked, with no session key", live, err)
	}
}

func withReady(s corev1.PodStatus) corev1.PodStatus {
	s.Conditions = []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}
	return s
}

// After a rollback the older gateway reads a warm pod's annotations and no
// mark: an ended one is a terminal pod with no task annotations, which its
// sweep deletes without publishing anything. (This gateway's TerminalOrphans
// with the warm flag cleared is that read.)
func TestAnOlderGatewaysSweepDeletesAnEndedWarmPod(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerCap(t, RouteSession, 0, reuseOn)
	const pod = "chat-otter-w8aa"
	spawn.setOrphans([]orphanPod{{PodName: pod, Addressee: pod}})
	r.g.sweepOnce(context.Background())
	if d := spawn.deleted(); len(d) != 1 || d[0] != pod {
		t.Fatalf("deleted %v, want the ended warm pod", d)
	}
}

// A recycle over the API is a delete and a create and nothing else: the
// gateway records no Event of its own for it. It does not show the probe is
// quiet, which is the kubelet's to record: that rests on the probe command
// waiting out the adapter's start (cmd/worker-adapter's
// TestAWarmSessionPodStartsWithNoTaskID) and on the endpoint answering for
// the pod's life (worker-adapter's
// TestAWarmPodIsReadyOnceItsWatcherIsLiveAndThenServesItsConversation); this
// checks only that the probe's timeout outlasts the command's wait.
func TestARecycleRecordsNoEvent(t *testing.T) {
	g, cs, _ := capRig(t, 4)
	g.cfg.SessionReuse, g.cfg.WarmSessions = true, 1
	ps := g.spawner.(*podSpawner)
	ps.cfg.SessionReuse, ps.cfg.TaskDeadline = true, 30*time.Minute
	ctx := context.Background()
	const aged = "chat-otter-w9aa"
	if err := ps.SpawnWarm(ctx, aged); err != nil {
		t.Fatal(err)
	}
	pod, err := cs.CoreV1().Pods("test-ns").Get(ctx, aged, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	pod.CreationTimestamp = metav1.NewTime(time.Now().Add(-warmClaimableAge))
	if _, err := cs.CoreV1().Pods("test-ns").Update(ctx, pod, metav1.UpdateOptions{}); err != nil {
		t.Fatal(err)
	}
	cs.ClearActions()
	g.warmPoolOnce(ctx)

	created, deleted := 0, 0
	for _, a := range cs.Actions() {
		if a.GetResource().Resource == "events" {
			t.Errorf("a recycle recorded an Event: %v", a)
		}
		if a.GetResource().Resource == "pods" {
			switch a.GetVerb() {
			case "create":
				created++
			case "delete":
				deleted++
			}
		}
	}
	if created != 1 || deleted != 1 {
		t.Fatalf("the pass created %d and deleted %d pods, want one replacement for the aged one", created, deleted)
	}
	pods, err := cs.CoreV1().Pods("test-ns").List(ctx, metav1.ListOptions{})
	if err != nil || len(pods.Items) != 1 {
		t.Fatalf("pods after the recycle: %v, %v", pods, err)
	}
	probe := pods.Items[0].Spec.Containers[0].ReadinessProbe
	if probe == nil || probe.TimeoutSeconds <= lib.SessionWarmReadyWaitSeconds {
		t.Fatalf("the replacement's probe %+v does not outlast the probe command's %ds wait", probe, lib.SessionWarmReadyWaitSeconds)
	}
}

// A warm pod made under another config (a release that changed the worker
// image, a moved scope, new resources) is never claimed and the pool
// replaces it: the stamp of the spec it was made with no longer matches.
func TestAConfigChangeRetiresTheWarmPodsMadeUnderTheOldOne(t *testing.T) {
	cfg := &Config{Namespace: "ns", WorkerImage: "worker:1", SessionServiceAccount: "sa", TaskDeadline: 30 * time.Minute,
		SessionReuse: true, AuthorityScope: "namespace/team-a"}
	cs := k8sfake.NewSimpleClientset()
	s := &podSpawner{cfg: cfg, client: cs, log: slog.Default()}
	if err := s.SpawnWarm(context.Background(), "chat-otter-wcfg"); err != nil {
		t.Fatal(err)
	}
	current := func() bool {
		pods, err := s.WarmPods(context.Background())
		if err != nil || len(pods) != 1 {
			t.Fatalf("WarmPods = %v, %v", pods, err)
		}
		return pods[0].Current
	}
	if !current() {
		t.Fatal("a warm pod is not current under the config it was made with")
	}
	for name, change := range map[string]func(){
		"worker image": func() { cfg.WorkerImage = "worker:2" },
		"scope":        func() { cfg.AuthorityScope = "namespace/team-b" },
		"capability":   func() { cfg.CapabilityOptional = true },
		"deadline":     func() { cfg.TaskDeadline = 45 * time.Minute },
	} {
		saved := *cfg
		change()
		if current() {
			t.Errorf("a warm pod is still current after the %s changed", name)
		}
		*cfg = saved
	}

	// The pool replaces a pod made under an earlier config, and a first
	// turn meanwhile spawns cold (TestAFirstTurnSpawnsColdWithNoClaimableWarmPod).
	t.Run("replaced", func(t *testing.T) {
		r, spawn := startWarmRig(t)
		waitFor(t, "the leader's first warm pod", func() bool { return len(spawn.warmSpawned()) == 1 })
		const stale = "chat-otter-wold"
		spawn.setWarm(warmPod{Name: stale, Created: time.Now(), Ready: true})
		r.g.warmPoolOnce(context.Background())
		if d := spawn.deleted(); len(d) != 1 || d[0] != stale {
			t.Fatalf("deleted %v, want the pod made under the earlier config", d)
		}
		if n := len(spawn.warmSpawned()); n != 2 {
			t.Fatalf("%d warm pods created, want a replacement", n)
		}
	})
}

// The claim index is not all that keeps a pod to one conversation: a pod
// whose task subjects carry anything is never claimed, so a claimed pod
// whose warm.<pod> entry is lost is not handed to a second conversation. It
// spawns cold instead, and the pod survives: it may be serving the first
// conversation, and its lifetime and that conversation's reap end it.
func TestAClaimedWarmPodWhoseClaimIsLostIsNeverHandedToASecondConversation(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	const pod = "chat-otter-wlost"
	spawn.setWarm(readyWarm(pod))
	say(r, "discord:g1/t-warm-first", "m1", "what pods are failing")
	r.awaitTask(t, pod)
	waitFor(t, "the first conversation's claim", func() bool { return warmClaims(t, r)[pod] != "" })
	if err := r.g.reg.DropWarmClaim(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	second := "discord:g1/t-warm-second"
	say(r, second, "m2", "what nodes are cordoned")
	waitFor(t, "the second conversation's cold spawn", func() bool { return len(spawn.calls()) == 1 })
	if got := spawn.calls()[0].Session; got == pod {
		t.Fatalf("the second conversation was spawned as the first one's pod %s", got)
	}
	rec, _ := r.g.reg.Get(context.Background(), second)
	if rec == nil || rec.BusSession == pod {
		t.Fatalf("the second conversation's record names the first one's pod: %+v", rec)
	}
	if _, ok := warmClaims(t, r)[pod]; ok {
		t.Fatal("the second conversation claimed a pod the first one had used")
	}
	// Nor does the pool delete it once it ages past its claim window.
	spawn.setWarm(warmPod{Name: pod, Created: time.Now().Add(-warmClaimableAge), Ready: true, Current: true})
	r.g.warmPoolOnce(context.Background())
	if slices.Contains(spawn.deleted(), pod) {
		t.Errorf("deleted %v: the used pod was deleted, which would take the first conversation's pod", spawn.deleted())
	}
}

// Two turns claiming the same pod at once: exactly one gets it, because the
// claim is a create in the index, not a put.
func TestTwoClaimsOfOneWarmPodLeaveOneWinner(t *testing.T) {
	t.Parallel()
	r, _ := startWarmRig(t)
	const pod, attempts = "chat-otter-wrace", 8
	errs := make(chan error, attempts)
	var wg sync.WaitGroup
	for i := range attempts {
		wg.Add(1)
		go func() {
			defer wg.Done()
			errs <- r.g.reg.ClaimWarmPod(context.Background(), pod, fmt.Sprintf("discord:g1/t-race-%d", i))
		}()
	}
	wg.Wait()
	close(errs)
	won := 0
	for err := range errs {
		switch {
		case err == nil:
			won++
		case !errors.Is(err, jetstream.ErrKeyExists):
			t.Fatalf("a claim failed for another reason: %v", err)
		}
	}
	if won != 1 {
		t.Fatalf("%d claims of one pod succeeded, want exactly 1", won)
	}
}

// The pool deletes nothing once a newer term has begun: an overlapping
// leader's claim is never deleted from under it.
func TestADeposedLeaderDeletesNoWarmPod(t *testing.T) {
	g, cs, _ := capRig(t, 4)
	ctx := context.Background()
	warmCapPod(t, g, cs, "chat-vole-wdep", true, "")
	kv, err := g.reg.kv(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, termKey, []byte("5")); err != nil {
		t.Fatal(err)
	}
	g.reg = NewRegistry(g.reg.c)
	g.reg.setEpoch(4)
	g.deleteWarmIfUnclaimed(ctx, "chat-vole-wdep", "test")
	if _, err := cs.CoreV1().Pods("test-ns").Get(ctx, "chat-vole-wdep", metav1.GetOptions{}); err != nil {
		t.Fatalf("a deposed leader deleted a warm pod: %v", err)
	}
	g.reg.setEpoch(5)
	g.deleteWarmIfUnclaimed(ctx, "chat-vole-wdep", "test")
	if _, err := cs.CoreV1().Pods("test-ns").Get(ctx, "chat-vole-wdep", metav1.GetOptions{}); err == nil {
		t.Fatal("the leader did not delete an unclaimed warm pod")
	}
}

// A claimed warm pod's reuse window counts from its claim, ending where a
// pod spawned at the claim would stop taking turns, though the warm pod's
// own deadline runs longer by the time it waited.
func TestAClaimedWarmPodsReuseWindowCountsFromItsClaim(t *testing.T) {
	t.Parallel()
	r, spawn := startWarmRig(t)
	const pod = "chat-otter-wlife"
	spawn.setReusable(pod)
	window := podLifetime(r.g.cfg, true) - r.g.cfg.TaskDeadline - 2*podDeadlineGrace
	for _, tc := range []struct {
		name    string
		claimed time.Time
		want    bool
	}{
		{"claimed just now", time.Now(), true},
		{"claimed inside a fresh pod's window", time.Now().Add(-window + time.Minute), true},
		{"claimed past it", time.Now().Add(-window - time.Minute), false},
		{"spawned for the conversation", time.Time{}, true},
	} {
		rec := &SessionRecord{Key: "discord:g1/t-life", BusSession: pod, PodName: pod, PodReuse: true, SessionRouted: true, PodClaimedAt: tc.claimed}
		if got := r.g.reuseLivePod(context.Background(), rec); got != tc.want {
			t.Errorf("%s: reuseLivePod = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// On the platform route nothing new runs: a gateway there creates no warm
// pod and claims none, even with a warm count set, and a Delegate's first
// turn spawns cold.
func TestThePlatformRouteRunsNoWarmPool(t *testing.T) {
	t.Parallel()
	r, spawn := startRigWithSpawnerCap(t, "platform", 0, warmOn(1, 10))
	if r.g.warmPoolOn() {
		t.Fatal("the pool is on on the platform route")
	}
	spawn.setWarm(readyWarm("chat-otter-wplat"))
	say(r, "discord:g1/t-warm-platform", "m1", "delegate: check the nodes")
	waitFor(t, "the delegate's cold spawn", func() bool { return len(spawn.calls()) == 1 })
	if got := spawn.warmSpawned(); len(got) != 0 {
		t.Fatalf("warm pods created on the platform route: %v", got)
	}
	if claims := warmClaims(t, r); len(claims) != 0 {
		t.Fatalf("a warm pod was claimed on the platform route: %v", claims)
	}
}

// warmPassRig is a gateway that runs no loops of its own, with the pool's
// config (one warm pod under a cap of four) over a fake spawner, the reuse
// rig's bus and registry, so a test drives warmPoolOnce alone.
func warmPassRig(t *testing.T) (*Gateway, *fakeSpawner, *rig) {
	t.Helper()
	r, _ := startReuseRig(t)
	spawn := &fakeSpawner{}
	g := &Gateway{
		cfg:          &Config{DefaultAddressee: RouteSession, SessionReuse: true, WarmSessions: 1, MaxSessions: 4},
		spawner:      spawn,
		reg:          r.g.reg,
		client:       r.client,
		adapter:      r.adapter,
		log:          slog.Default(),
		sessionLocks: map[string]*sessionLockEntry{},
	}
	return g, spawn, r
}

// publishTaskTo puts a task submission on pod's subjects, as an earlier pod
// under the same name would have had one.
func publishTaskTo(t *testing.T, r *rig, pod, taskID string) {
	t.Helper()
	if _, err := r.bus.JetStream().Publish(context.Background(), lib.TaskInSubject(pod, taskID), []byte(`{"kind":"message"}`)); err != nil {
		t.Fatal(err)
	}
}

// The pool never creates a warm pod under a name TASKS holds a task for: a
// minted name can repeat an earlier pod's within the stream's retention, so
// it mints again, and a pass whose every attempt is held creates nothing.
func TestThePoolNeverMintsANameTasksHolds(t *testing.T) {
	g, spawn, r := warmPassRig(t)
	ctx := context.Background()
	const held, free = "chat-otter-wheld", "chat-otter-wfree"
	publishTaskTo(t, r, held, "task-old")
	names := []string{held, free}
	g.warmMint = func(string) string {
		n := names[0]
		if len(names) > 1 {
			names = names[1:]
		}
		return n
	}
	g.warmPoolOnce(ctx)
	if got := spawn.warmSpawned(); !slices.Equal(got, []string{free}) {
		t.Fatalf("warm pods created %v, want only %s: %s has a task on TASKS", got, free, held)
	}

	g2, spawn2, r2 := warmPassRig(t)
	publishTaskTo(t, r2, held, "task-old")
	mints := 0
	g2.warmMint = func(string) string { mints++; return held }
	g2.warmPoolOnce(ctx)
	if got := spawn2.warmSpawned(); len(got) != 0 {
		t.Fatalf("warm pods created %v with every minted name held", got)
	}
	if mints != warmMintAttempts {
		t.Fatalf("%d names minted, want the bound of %d", mints, warmMintAttempts)
	}
}

// A deposed leader creates no warm pod: a newer term has begun, so the new
// leader sizes the pool and this one would only overshoot it.
func TestADeposedLeaderCreatesNoWarmPod(t *testing.T) {
	g, spawn, _ := warmPassRig(t)
	ctx := context.Background()
	kv, err := g.reg.kv(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := kv.Put(ctx, termKey, []byte("5")); err != nil {
		t.Fatal(err)
	}
	g.reg = NewRegistry(g.reg.c)
	g.reg.setEpoch(4)
	g.warmPoolOnce(ctx)
	if got := spawn.warmSpawned(); len(got) != 0 {
		t.Fatalf("a deposed leader created warm pods %v", got)
	}
	g.reg.setEpoch(5)
	g.warmPoolOnce(ctx)
	if got := spawn.warmSpawned(); len(got) != 1 {
		t.Fatalf("the leader created %v, want one warm pod", got)
	}
}

// An unclaimed warm pod something was published to (a claim entry lost from
// the bucket, its conversation likely on it) is no longer the pool's: it is
// left out of the pool's count, so the pool replaces it rather than waiting
// out its claim window, and the cap counts it as a live session, with no
// conversation named. An unused warm pod still holds no cap slot.
func TestAUsedUnclaimedWarmPodCountsAsALiveSessionNotAPoolPod(t *testing.T) {
	g, spawn, r := warmPassRig(t)
	ctx := context.Background()
	const used, idle = "chat-otter-wused", "chat-otter-widle"
	publishTaskTo(t, r, used, "task-lost-claim")
	spawn.setWarm(readyWarm(used))
	spawn.setSessionPods(sessionPod{PodName: used, Warm: true, Reuse: true}, sessionPod{PodName: idle, Warm: true, Reuse: true})
	pods, err := g.sessionPods(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(pods) != 1 || pods[0].PodName != used || pods[0].SessionKey != "" {
		t.Fatalf("sessionPods = %+v, want only the used warm pod, with no conversation named", pods)
	}
	g.warmPoolOnce(ctx)
	if got := spawn.warmSpawned(); len(got) != 1 {
		t.Fatalf("warm pods created %v, want one: the used pod is not the pool's", got)
	}
	if slices.Contains(spawn.deleted(), used) {
		t.Fatal("the pool deleted a used warm pod")
	}
}

// A warm pod past its claim window whose task subjects can't be read is kept
// and stays counted, so the pool does not replace it beside itself and run
// one more warm pod than it was budgeted.
func TestAnAgedWarmPodWhoseSubjectsCantBeReadIsNotReplaced(t *testing.T) {
	g, spawn, _ := warmPassRig(t)
	ctx := context.Background()
	const aged = "chat-otter-waged"
	g.warmUsedErr = func(pod string) error {
		if pod == aged {
			return errors.New("direct get: timeout")
		}
		return nil
	}
	spawn.setWarm(warmPod{Name: aged, Created: time.Now().Add(-warmClaimableAge), Ready: true, Current: true})
	g.warmPoolOnce(ctx)
	if got := spawn.deleted(); len(got) != 0 {
		t.Fatalf("deleted %v with the task subjects unreadable", got)
	}
	if got := spawn.warmSpawned(); len(got) != 0 {
		t.Fatalf("warm pods created %v beside a pod that may still be there", got)
	}
}

// The sweep drops the claim of a warm pod that no longer exists whether or
// not the gateway keeps a pool: a claimed pod the reap or the cap eviction
// deleted leaves its claim behind, and with the pool off nothing else
// removes it. A claim whose pod is still there is kept.
func TestTheSweepDropsAGoneWarmPodsClaimWithThePoolOff(t *testing.T) {
	r, spawn := startReuseRig(t)
	if r.g.warmPoolOn() {
		t.Fatal("precondition: the reuse rig keeps a pool")
	}
	ctx := context.Background()
	const gone, live = "chat-otter-wreaped", "chat-otter-wkept"
	spawn.setWarm(readyWarm(live))
	for pod, key := range map[string]string{gone: "discord:g1/t-reaped", live: "discord:g1/t-kept"} {
		if err := r.g.reg.ClaimWarmPod(ctx, pod, key); err != nil {
			t.Fatal(err)
		}
	}
	r.g.sweepOnce(ctx)
	claims := warmClaims(t, r)
	if _, ok := claims[gone]; ok {
		t.Error("the claim of a warm pod that no longer exists was kept")
	}
	if claims[live] != "discord:g1/t-kept" {
		t.Error("the claim of a warm pod that is still there was dropped")
	}
}
