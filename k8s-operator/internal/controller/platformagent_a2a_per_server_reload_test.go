package controller

import (
	"context"
	"errors"
	"strconv"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// a2aClusterReloadFixture is a three-server bus as the reload sees it: the
// live config Secret with annotations, the StatefulSet on hash "h1", and its
// three pods, Ready unless notReady names them.
func a2aClusterReloadFixture(t *testing.T, liveConf string, annotations map[string]string, notReady ...int) (*PlatformAgentReconciler, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet, *record.FakeRecorder) {
	t.Helper()
	agent := a2aTestAgentWithBusReplicas(3)
	sts := buildA2ANATSStatefulSet(agent, "h1")
	sts.UID = "sts-uid"
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace, Annotations: annotations},
		Data:       map[string][]byte{a2aNATSConfKey: []byte(liveConf)},
	}
	objs := []client.Object{agent, sts, secret}
	for i := range 3 {
		ready := corev1.ConditionTrue
		for _, n := range notReady {
			if n == i {
				ready = corev1.ConditionFalse
			}
		}
		objs = append(objs, &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{
				Name: sts.Name + "-" + strconv.Itoa(i), Namespace: agent.Namespace, UID: types.UID("pod-uid-" + strconv.Itoa(i)),
				CreationTimestamp: metav1.NewTime(time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)),
				OwnerReferences:   []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: sts.Name, UID: sts.UID, Controller: ptr.To(true)}},
			},
			Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: ready,
				LastTransitionTime: metav1.NewTime(time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC))}}},
		})
	}
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(objs...).Build()
	rec := record.NewFakeRecorder(10)
	return &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: rec}, agent, sts, rec
}

// loadTimes answers each server's config_load_time from times, by pod name,
// and fails a server named in down.
func loadTimes(times map[string]string, down ...string) func(context.Context, *agentv1alpha1.PlatformAgent, *corev1.Pod) (string, error) {
	return func(_ context.Context, _ *agentv1alpha1.PlatformAgent, pod *corev1.Pod) (string, error) {
		for _, d := range down {
			if d == pod.Name {
				return "", errors.New("connection refused")
			}
		}
		return times[pod.Name], nil
	}
}

func pendingOnEvery(sts *appsv1.StatefulSet, asked time.Time, baseline string) map[string]string {
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), servers: map[string]a2aBusServerRecord{}}
	for i := range 3 {
		state.servers[sts.Name+"-"+strconv.Itoa(i)] = a2aBusServerRecord{UID: types.UID("pod-uid-" + strconv.Itoa(i)), Start: "s", Loaded: baseline}
	}
	config := &corev1.Secret{}
	state.stamp(config, sts.Name)
	return config.Annotations
}

// A reload is confirmed only when every server's config_load_time has moved
// off the baseline read from that server: two of three is still pending.
func TestEveryServerConfirmsAReloadBeforeItLands(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	sts := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
	r, agent, sts, _ := a2aClusterReloadFixture(t, "new", pendingOnEvery(sts, asked, "t0"))
	times := map[string]string{sts.Name + "-0": "t1", sts.Name + "-1": "t1", sts.Name + "-2": "t0"}
	r.busConfigLoadTime = loadTimes(times)
	plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("two of three servers confirmed the reload")
	}
	times[sts.Name+"-2"] = "t1"
	plan, err = r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if !plan.reloaded {
		t.Fatalf("plan = %+v, want the reload confirmed once every server moved", plan)
	}
}

// Past the bound, the one straggler is restarted, alone, and only while every
// other server is Ready, so the cluster keeps quorum. The record stays.
func TestAStragglerIsRestartedAloneAndOnlyWithTheOthersReady(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	base := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
	pending := pendingOnEvery(base, asked, "t0")
	moved := map[string]string{base.Name + "-0": "t1", base.Name + "-1": "t0", base.Name + "-2": "t1"}

	r, agent, sts, rec := a2aClusterReloadFixture(t, "new", pending)
	r.busConfigLoadTime = loadTimes(moved)
	config := a2aDesiredConfig(agent, "new")
	plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(a2aBusReloadBound))
	if err != nil {
		t.Fatal(err)
	}
	if plan.restart != sts.Name+"-1" || plan.reloaded {
		t.Fatalf("plan = %+v, want the straggler %s-1 restarted", plan, sts.Name)
	}
	if config.Annotations[a2aBusReloadRequestedAnnotation] == "" {
		t.Fatal("the pending record was dropped at the restart; the hold would release")
	}
	if err := r.actOnA2ABusReload(context.Background(), agent, sts.Name, plan); err != nil {
		t.Fatal(err)
	}
	for i, want := range []bool{true, false, true} {
		pod := &corev1.Pod{}
		err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: sts.Name + "-" + strconv.Itoa(i)}, pod)
		if (err == nil) != want {
			t.Errorf("pod %d present = %v, want %v", i, err == nil, want)
		}
	}
	if ev := <-rec.Events; !strings.HasPrefix(ev, corev1.EventTypeWarning+" "+a2aBusReloadFailedReason) || !strings.Contains(ev, sts.Name+"-1") {
		t.Errorf("event = %q, want a %s Warning naming the straggler", ev, a2aBusReloadFailedReason)
	}

	// Another server not Ready: nothing is restarted.
	r, agent, sts, _ = a2aClusterReloadFixture(t, "new", pending, 2)
	r.busConfigLoadTime = loadTimes(moved)
	plan, err = r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(a2aBusReloadBound))
	if err != nil {
		t.Fatal(err)
	}
	if plan.restart != "" {
		t.Fatalf("plan = %+v: a straggler was restarted while another server isn't Ready", plan)
	}
}

// Fail closed: a server that can't be read hasn't loaded the config, so the
// reload stays pending past the bound and the profile stays held, and the
// unreadable server is the one restarted.
func TestAnUnreadableServerKeepsTheProfileHeld(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	base := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
	pending := pendingOnEvery(base, asked, "t0")
	pending[a2aConfigProfilesAnnotation] = "old,new"
	pending[a2aBusPreviousProfilesAnnotation] = "old"
	r, agent, sts, _ := a2aClusterReloadFixture(t, "new", pending)
	r.busConfigLoadTime = loadTimes(map[string]string{sts.Name + "-0": "t1", sts.Name + "-1": "t1", sts.Name + "-2": "t1"}, sts.Name+"-2")
	for _, at := range []time.Duration{time.Minute, a2aBusReloadBound, 2 * a2aBusReloadBound} {
		config := a2aDesiredConfig(agent, "new")
		config.Annotations = map[string]string{a2aConfigProfilesAnnotation: "old,new", a2aBusPreviousProfilesAnnotation: "old"}
		plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(at))
		if err != nil {
			t.Fatal(err)
		}
		if plan.reloaded {
			t.Fatalf("at %s: a reload with an unreadable server was confirmed", at)
		}
		if at >= a2aBusReloadBound && plan.restart != sts.Name+"-2" {
			t.Errorf("at %s: plan = %+v, want the unreadable server restarted", at, plan)
		}
		loaded, err := a2aServerLoadedFrom(context.Background(), r, agent, config)
		if err != nil {
			t.Fatal(err)
		}
		if loaded["new"] || !loaded["old"] {
			t.Fatalf("at %s: loaded = %v, want only old: the new profile must stay held", at, loaded)
		}
	}
}

// An upgrade from the rolling operator: while a server still runs a revision
// whose template carries the profile list, the loaded set is narrowed to it,
// whatever the config Secret says, so the hold doesn't release early. Once
// every server runs the new template (no list), the Secret alone answers.
func TestAnUpgradeKeepsTheHoldUntilTheOldRevisionsAgree(t *testing.T) {
	agent := a2aTestAgentWithBusReplicas(3)
	agent.Status.BusReplicas = 3
	b := newHoldBus(agent)
	b.revision(t, "bus-old", "x", true)
	b.plainRevision(t, "bus-new")
	b.sts.Status.UpdateRevision = "bus-new"
	b.pod(0, "bus-new")
	b.pod(1, "bus-old")
	b.pod(2, "bus-new")
	b.configSecret(map[string]string{a2aConfigProfilesAnnotation: "x,y"})
	if got := loadedList(loadedOrFatal(t, b.client(), b.agent)); got != "x" {
		t.Fatalf("loaded = %q mid-upgrade, want x: a server still runs the old list", got)
	}

	b = newHoldBus(agent)
	b.plainRevision(t, "bus-new")
	b.sts.Status.UpdateRevision = "bus-new"
	for i := range 3 {
		b.pod(i, "bus-new")
	}
	b.configSecret(map[string]string{a2aConfigProfilesAnnotation: "x,y"})
	if got := loadedList(loadedOrFatal(t, b.client(), b.agent)); got != "x,y" {
		t.Fatalf("loaded = %q after the upgrade's roll, want x,y", got)
	}
}

// The folded-write rule is per server: a write into a pending reload folds
// only on the servers still without the earlier write. A server whose move
// then arrives re-arms rather than confirms; one that wasn't folded confirms.
func TestAFoldedWriteIsPerServer(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	base := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
	r, agent, sts, _ := a2aClusterReloadFixture(t, "v1", pendingOnEvery(base, asked, "t0"))
	// v1 landed on server 0 (t1) but not on 1 and 2 (still t0).
	times := map[string]string{sts.Name + "-0": "t1", sts.Name + "-1": "t0", sts.Name + "-2": "t0"}
	r.busConfigLoadTime = loadTimes(times)
	config := a2aDesiredConfig(agent, "v2")
	if _, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(20*time.Second)); err != nil {
		t.Fatal(err)
	}
	state, _ := readA2ABusReloadState(config.Annotations, sts.Name)
	if state.folded[sts.Name+"-0"] || !state.folded[sts.Name+"-1"] || !state.folded[sts.Name+"-2"] {
		t.Fatalf("folded = %v, want servers 1 and 2 only", state.folded)
	}
	live := &corev1.Secret{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(config), live); err != nil {
		t.Fatal(err)
	}
	live.Data[a2aNATSConfKey], live.Annotations = []byte("v2"), config.Annotations
	if err := r.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	// Every server moves once: 0 confirms v2, 1 and 2 re-arm.
	times[sts.Name+"-0"], times[sts.Name+"-1"], times[sts.Name+"-2"] = "t2", "t1", "t1"
	config = a2aDesiredConfig(agent, "v2")
	plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(40*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("a first move on a folded server confirmed the reload")
	}
	state, _ = readA2ABusReloadState(config.Annotations, sts.Name)
	if !state.rearmed[sts.Name+"-1"] || !state.rearmed[sts.Name+"-2"] || state.rearmed[sts.Name+"-0"] {
		t.Fatalf("rearmed = %v, want servers 1 and 2", state.rearmed)
	}
}

// One server takes the same path: its reload is confirmed by its own pod's
// load time, and past the bound that pod is restarted (no other server to
// wait on).
func TestASingleServerTakesTheSamePath(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	agent := a2aSingleServerTestAgent()
	sts := buildA2ANATSStatefulSet(agent, "h1")
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), servers: map[string]a2aBusServerRecord{sts.Name + "-0": {UID: "pod-uid", Start: "s", Loaded: "t0"}}}
	pending := &corev1.Secret{}
	state.stamp(pending, sts.Name)
	r, agent, sts, _ := a2aReloadFixture(t, "new", pending.Annotations)
	r.busConfigLoadTime = loadTimes(map[string]string{sts.Name + "-0": "t0"})
	plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(a2aBusReloadBound))
	if err != nil {
		t.Fatal(err)
	}
	if plan.restart != sts.Name+"-0" {
		t.Fatalf("plan = %+v, want the one server restarted past the bound", plan)
	}
	r.busConfigLoadTime = loadTimes(map[string]string{sts.Name + "-0": "t1"})
	plan, err = r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if !plan.reloaded {
		t.Fatalf("plan = %+v, want the one server's move confirming", plan)
	}
}

// A scale-down (three to one, say) leaves pods past spec.replicas serving the
// config they started with. While one exists, the reload isn't confirmed,
// nothing is restarted, and the hold reads the bus as not settled.
func TestAScaleDownsLeftoverServerKeepsTheHold(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	agent := a2aSingleServerTestAgent()
	sts := buildA2ANATSStatefulSet(agent, "h1")
	sts.UID = "sts-uid"
	sts.Status.Replicas = 3
	owner := []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: sts.Name, UID: sts.UID, Controller: ptr.To(true)}}
	old := metav1.NewTime(time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC))
	pod := func(i int) *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: sts.Name + "-" + strconv.Itoa(i), Namespace: agent.Namespace, UID: types.UID("pod-" + strconv.Itoa(i)), CreationTimestamp: old, OwnerReferences: owner},
			Status:     corev1.PodStatus{Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}},
		}
	}
	// Both servers have moved, the leftover too: it still blocks, since it is
	// on its way out and nothing says its config will hold.
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), servers: map[string]a2aBusServerRecord{
		sts.Name + "-0": {UID: "pod-0", Start: "s", Loaded: "t0"}, sts.Name + "-2": {UID: "pod-2", Start: "s", Loaded: "t0"}}}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace},
		Data:       map[string][]byte{a2aNATSConfKey: []byte("new")},
	}
	state.stamp(secret, sts.Name)
	cl := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, sts, secret, pod(0), pod(2)).Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(), Recorder: record.NewFakeRecorder(4),
		busConfigLoadTime: loadTimes(map[string]string{sts.Name + "-0": "t1", sts.Name + "-2": "t1"})}
	for _, at := range []time.Duration{time.Minute, a2aBusReloadBound} {
		plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(at))
		if err != nil {
			t.Fatal(err)
		}
		if plan.reloaded || plan.restart != "" {
			t.Fatalf("at %s: plan = %+v with %s-2 still serving; want pending and nothing restarted", at, plan, sts.Name)
		}
	}

	// The hold: a previous list recorded, the StatefulSet otherwise settled
	// on its one replica, and pod -2 still there.
	b := newHoldBus(agent)
	b.sts.Generation, b.sts.Status.ObservedGeneration, b.sts.Status.UpdatedReplicas = 1, 1, 1
	b.sts.Status.CurrentRevision, b.sts.Status.UpdateRevision = "bus-a", "bus-a"
	b.plainRevision(t, "bus-a")
	b.pod(0, "bus-a")
	b.pod(2, "bus-a")
	b.configSecret(map[string]string{a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x"})
	if got := loadedList(loadedOrFatal(t, b.client(), b.agent)); got != "x" {
		t.Fatalf("loaded = %q with a leftover server, want x: the new profile must stay held", got)
	}
}

// H1: the key an earlier operator reads keeps its shape, pod -0's load time
// as a bare string, and is empty when it couldn't be read, so a rolled-back
// operator restarts rather than confirming on a value it misreads.
func TestTheOldBaselineKeyKeepsTheShapeAnEarlierOperatorReads(t *testing.T) {
	for _, tc := range []struct {
		name string
		read func(context.Context, *agentv1alpha1.PlatformAgent, *corev1.Pod) (string, error)
		want string
	}{
		{"readable", func(context.Context, *agentv1alpha1.PlatformAgent, *corev1.Pod) (string, error) {
			return "2026-11-17T08:00:00Z", nil
		}, "2026-11-17T08:00:00Z"},
		{"unreadable", func(context.Context, *agentv1alpha1.PlatformAgent, *corev1.Pod) (string, error) {
			return "", errors.New("refused")
		}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, agent, sts, _ := a2aReloadFixture(t, "old", nil)
			r.busConfigLoadTime = tc.read
			config := a2aDesiredConfig(agent, "new")
			if _, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), time.Now()); err != nil {
				t.Fatal(err)
			}
			got, ok := config.Annotations[a2aBusReloadBaselineAnnotation]
			if !ok || got != tc.want {
				t.Fatalf("%s = %q (present %v), want exactly %q", a2aBusReloadBaselineAnnotation, got, ok, tc.want)
			}
			if config.Annotations[a2aBusReloadServersAnnotation] == "" {
				t.Fatal("no per-server record under its own key")
			}
		})
	}
}

// varzOf answers /varz per pod name, the server named after its pod unless
// names overrides it, and down fails.
func varzOf(v map[string]a2aBusVarz, down ...string) func(context.Context, *agentv1alpha1.PlatformAgent, *corev1.Pod) (a2aBusVarz, error) {
	return func(_ context.Context, _ *agentv1alpha1.PlatformAgent, pod *corev1.Pod) (a2aBusVarz, error) {
		for _, d := range down {
			if d == pod.Name {
				return a2aBusVarz{}, errors.New("refused")
			}
		}
		out := v[pod.Name]
		if out.Name == "" {
			out.Name = pod.Name
		}
		return out, nil
	}
}

const (
	t0 = "2026-11-17T08:00:00Z"
	t1 = "2026-11-17T08:01:00Z"
)

// a2aRecordedCluster is the three-server fixture with every server recorded
// on start t0 and load time t0, written at writtenAt by the API server.
func a2aRecordedCluster(t *testing.T, asked, writtenAt time.Time) (*PlatformAgentReconciler, *agentv1alpha1.PlatformAgent, *appsv1.StatefulSet, *record.FakeRecorder) {
	t.Helper()
	base := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), servers: map[string]a2aBusServerRecord{}}
	for i := range 3 {
		state.servers[base.Name+"-"+strconv.Itoa(i)] = a2aBusServerRecord{UID: types.UID("pod-uid-" + strconv.Itoa(i)), Start: t0, Loaded: t0}
	}
	secret := &corev1.Secret{}
	state.stamp(secret, base.Name)
	r, agent, sts, rec := a2aClusterReloadFixture(t, "new", secret.Annotations)
	r.busConfigWrittenAt = func(*corev1.Secret) (time.Time, bool) { return writtenAt, true }
	return r, agent, sts, rec
}

// H2: a server restarted in place reloads whatever file the kubelet last
// synced, so its load time moving at the start isn't the reload. Confirmed by
// a load time later than the new start, or a start past the kubelet's sync
// window after the write; otherwise it waits.
func TestARestartedServerIsNotTakenAsReloaded(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 30, 0, time.UTC)
	written := time.Date(2026, 11, 17, 8, 0, 31, 0, time.UTC)
	for _, tc := range []struct {
		name   string
		start  string
		loaded string
		want   bool
	}{
		{"restarted inside the sync window, load at start", "2026-11-17T08:01:00Z", "2026-11-17T08:01:00Z", false},
		{"restarted inside the sync window, a later reload", "2026-11-17T08:01:00Z", "2026-11-17T08:01:40Z", true},
		{"restarted past the sync window", "2026-11-17T08:02:30Z", "2026-11-17T08:02:30Z", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, agent, sts, _ := a2aRecordedCluster(t, asked, written)
			v := map[string]a2aBusVarz{}
			for i := range 3 {
				v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
			}
			v[sts.Name+"-1"] = a2aBusVarz{Start: tc.start, Loaded: tc.loaded}
			r.busVarz = varzOf(v)
			plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
			if err != nil {
				t.Fatal(err)
			}
			if plan.reloaded != tc.want {
				t.Fatalf("reloaded = %v, want %v", plan.reloaded, tc.want)
			}
		})
	}
}

// H2 and M1: a pod the record doesn't know (a new UID) is confirmed only if
// the API server created it after the config write, whatever the operator's
// own clock said when it asked.
func TestARecreatedPodIsConfirmedOnlyByItsCreationAfterTheWrite(t *testing.T) {
	asked := time.Date(2026, 11, 17, 7, 0, 0, 0, time.UTC) // an operator clock an hour behind
	written := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name    string
		created time.Time
		want    bool
	}{
		{"created after the write", written.Add(5 * time.Second), true},
		{"created before the write, after the operator's request", written.Add(-5 * time.Second), false},
		{"created in the write's second", written, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, agent, sts, _ := a2aRecordedCluster(t, asked, written)
			pod := &corev1.Pod{}
			if err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: sts.Name + "-2"}, pod); err != nil {
				t.Fatal(err)
			}
			if err := r.Delete(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			pod.ResourceVersion, pod.UID, pod.CreationTimestamp = "", "pod-uid-2-new", metav1.NewTime(tc.created)
			if err := r.Create(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			v := map[string]a2aBusVarz{}
			for i := range 3 {
				v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
			}
			v[sts.Name+"-2"] = a2aBusVarz{Start: "2026-11-17T08:00:10Z", Loaded: "2026-11-17T08:00:10Z"}
			r.busVarz = varzOf(v)
			plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
			if err != nil {
				t.Fatal(err)
			}
			if plan.reloaded != tc.want {
				t.Fatalf("reloaded = %v, want %v", plan.reloaded, tc.want)
			}
		})
	}
}

// The liveness gate: a restarted server that never shows the reload is
// restarted past the bound, the StatefulSet's fresh pod mounts the current
// Secret, and its first read confirms the reload, releasing the hold.
func TestAnOperatorRestartedStragglerReleasesTheHold(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	written := asked.Add(time.Second)
	r, agent, sts, _ := a2aRecordedCluster(t, asked, written)
	v := map[string]a2aBusVarz{}
	for i := range 3 {
		v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
	}
	// Restarted in place inside the sync window; no reload ever follows.
	v[sts.Name+"-1"] = a2aBusVarz{Start: "2026-11-17T08:00:20Z", Loaded: "2026-11-17T08:00:20Z"}
	r.busVarz = varzOf(v)
	held := func(cfg *corev1.Secret) bool {
		loaded, err := a2aServerLoadedFrom(context.Background(), r, agent, cfg)
		if err != nil {
			t.Fatal(err)
		}
		return !loaded["new"]
	}
	config := a2aDesiredConfig(agent, "new")
	plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(a2aBusReloadBound))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded || plan.restart != sts.Name+"-1" {
		t.Fatalf("plan = %+v, want the restarted-in-place server restarted past the bound", plan)
	}
	config.Annotations[a2aConfigProfilesAnnotation], config.Annotations[a2aBusPreviousProfilesAnnotation] = "old,new", "old"
	if !held(config) {
		t.Fatal("the profile was released before the straggler's restart was confirmed")
	}
	if err := r.actOnA2ABusReload(context.Background(), agent, sts.Name, plan); err != nil {
		t.Fatal(err)
	}
	// The StatefulSet recreates it after the write.
	fresh := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: sts.Name + "-1", Namespace: agent.Namespace, UID: "pod-uid-1-fresh",
		CreationTimestamp: metav1.NewTime(asked.Add(a2aBusReloadBound + time.Minute)),
		OwnerReferences:   []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: sts.Name, UID: sts.UID, Controller: ptr.To(true)}},
	}}
	if err := r.Create(context.Background(), fresh); err != nil {
		t.Fatal(err)
	}
	v[sts.Name+"-1"] = a2aBusVarz{Start: "2026-11-17T08:04:10Z", Loaded: "2026-11-17T08:04:10Z"}
	config = a2aDesiredConfig(agent, "new")
	plan, err = r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(a2aBusReloadBound+90*time.Second))
	if err != nil {
		t.Fatal(err)
	}
	if !plan.reloaded {
		t.Fatalf("plan = %+v, want the fresh pod's first read confirming the reload", plan)
	}
	// Confirmed: the apply drops the pending record, and the hold lets go.
	released := a2aDesiredConfig(agent, "new")
	released.Annotations = map[string]string{a2aConfigProfilesAnnotation: "old,new"}
	if held(released) {
		t.Fatal("the profile is still held once every server confirmed")
	}
}

// L1: an answer under another server's name (an address reused by another
// pod) is no answer.
func TestAVarzAnswerUnderAnotherNameIsUnreadable(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	r, agent, sts, _ := a2aRecordedCluster(t, asked, asked.Add(time.Second))
	v := map[string]a2aBusVarz{}
	for i := range 3 {
		v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
	}
	v[sts.Name+"-2"] = a2aBusVarz{Name: "someone-else", Start: t0, Loaded: t1}
	r.busVarz = varzOf(v)
	plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("an answer under another server's name confirmed the reload")
	}
}

// L2: a straggler waits for the other servers to have been Ready a while.
func TestAStragglerWaitsForTheOthersToHaveBeenReadyAWhile(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	at := asked.Add(a2aBusReloadBound)
	for _, tc := range []struct {
		name    string
		readyAt time.Time
		want    bool
	}{
		{"just back", at.Add(-10 * time.Second), false},
		{"settled", at.Add(-a2aBusReadyBeforeRestart), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, agent, sts, _ := a2aRecordedCluster(t, asked, asked.Add(time.Second))
			pod := &corev1.Pod{}
			if err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: sts.Name + "-0"}, pod); err != nil {
				t.Fatal(err)
			}
			pod.Status.Conditions[0].LastTransitionTime = metav1.NewTime(tc.readyAt)
			if err := r.Status().Update(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			v := map[string]a2aBusVarz{}
			for i := range 3 {
				v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
			}
			v[sts.Name+"-1"] = a2aBusVarz{Start: t0, Loaded: t0}
			r.busVarz = varzOf(v)
			plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), at)
			if err != nil {
				t.Fatal(err)
			}
			if (plan.restart != "") != tc.want {
				t.Fatalf("plan = %+v, want a restart %v", plan, tc.want)
			}
		})
	}
}

// L3 and L4 (the owner check): a pod under a server's name that the
// StatefulSet doesn't control can't confirm, however it answers, and a
// Warning names it.
func TestAPodTheStatefulSetDoesntControlHoldsTheReloadAndIsNamed(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	r, agent, sts, rec := a2aRecordedCluster(t, asked, asked.Add(time.Second))
	pod := &corev1.Pod{}
	if err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: sts.Name + "-2"}, pod); err != nil {
		t.Fatal(err)
	}
	pod.OwnerReferences[0].UID = "another-sts"
	if err := r.Update(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	v := map[string]a2aBusVarz{}
	for i := range 3 {
		v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
	}
	r.busVarz = varzOf(v)
	plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("a pod the StatefulSet doesn't control confirmed the reload")
	}
	if err := r.actOnA2ABusReload(context.Background(), agent, sts.Name, plan); err != nil {
		t.Fatal(err)
	}
	select {
	case ev := <-rec.Events:
		if !strings.HasPrefix(ev, corev1.EventTypeWarning+" "+a2aBusForeignPodReason) || !strings.Contains(ev, sts.Name+"-2") {
			t.Fatalf("event = %q, want a %s Warning naming the pod", ev, a2aBusForeignPodReason)
		}
	default:
		t.Fatal("no Warning for the pod the StatefulSet doesn't control")
	}
}

// L4: the restart deletes only the pod the plan read; one recreated since
// is left alone.
func TestARestartLeavesARecreatedPodAlone(t *testing.T) {
	r, agent, sts, _ := a2aClusterReloadFixture(t, "new", nil)
	plan := a2aBusReload{restart: sts.Name + "-1", restartUID: "an-earlier-pod", busUID: sts.UID}
	if err := r.actOnA2ABusReload(context.Background(), agent, sts.Name, plan); err != nil {
		t.Fatal(err)
	}
	if err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: sts.Name + "-1"}, &corev1.Pod{}); err != nil {
		t.Fatalf("the recreated pod was deleted: %v", err)
	}
}

// The write time is the API server's, from the operator's own field manager:
// a later kubectl annotate doesn't move it, and with no entry of the
// operator's there is none.
func TestTheConfigWriteTimeIsTheLatestManagedFieldsEntry(t *testing.T) {
	a, b := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC), time.Date(2026, 11, 17, 8, 5, 0, 0, time.UTC)
	s := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{ManagedFields: []metav1.ManagedFieldsEntry{
		{Manager: fieldOwner, Operation: metav1.ManagedFieldsOperationApply, Time: &metav1.Time{Time: a}},
		{Manager: "kubectl-annotate", Operation: metav1.ManagedFieldsOperationUpdate, Time: &metav1.Time{Time: b}},
		{Manager: fieldOwner},
	}}}
	if got, ok := a2aBusConfigWrittenAt(s); !ok || !got.Equal(a) {
		t.Fatalf("written at %v (%v), want the operator's own write %v", got, ok, a)
	}
	other := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{ManagedFields: []metav1.ManagedFieldsEntry{
		{Manager: "kubectl-annotate", Time: &metav1.Time{Time: b}},
	}}}
	if _, ok := a2aBusConfigWrittenAt(other); ok {
		t.Fatal("another manager's entry gave a write time")
	}
	if _, ok := a2aBusConfigWrittenAt(&corev1.Secret{}); ok {
		t.Fatal("a Secret with no managedFields gave a write time")
	}
}

// A replacement created after the write that is still starting (no /varz
// yet) isn't restarted again: it gets a full bound from its own creation.
// Past that bound, still unconfirmed, it is restarted, so the wait ends.
func TestAStartingReplacementIsNotRestartedAgain(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	written := asked.Add(time.Second)
	created := written.Add(4 * time.Minute)
	for _, tc := range []struct {
		name string
		at   time.Time
		want bool
	}{
		{"one second old", created.Add(time.Second), false},
		{"past its own bound", created.Add(a2aBusReloadBound + time.Second), true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), servers: map[string]a2aBusServerRecord{}}
			agent := a2aSingleServerTestAgent()
			name := a2aNATSName(agent) + a2aNATSPodOrdinalSuffix
			state.servers[name] = a2aBusServerRecord{UID: "the-old-pod", Start: t0, Loaded: t0}
			pending := &corev1.Secret{}
			state.stamp(pending, a2aNATSName(agent))
			r, agent, sts, _ := a2aReloadFixture(t, "new", pending.Annotations)
			pod := &corev1.Pod{}
			if err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: name}, pod); err != nil {
				t.Fatal(err)
			}
			pod.CreationTimestamp = metav1.NewTime(created)
			if err := r.Delete(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			pod.ResourceVersion, pod.UID = "", "the-replacement"
			if err := r.Create(context.Background(), pod); err != nil {
				t.Fatal(err)
			}
			r.busConfigWrittenAt = func(*corev1.Secret) (time.Time, bool) { return written, true }
			r.busVarz = varzOf(nil, name)
			plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), tc.at)
			if err != nil {
				t.Fatal(err)
			}
			if (plan.restart != "") != tc.want {
				t.Fatalf("plan = %+v, want a restart %v", plan, tc.want)
			}
		})
	}
}

// The lone server that isn't Ready, unconfirmed, is restarted first when
// every other server is Ready (no quorum lost); with a second server down
// too, nothing is restarted.
func TestTheLoneDownStragglerIsRestartedFirst(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		name     string
		notReady []int
		want     string
	}{
		{"server 1 Pending, the others Ready", []int{1}, "-1"},
		{"servers 1 and 2 down", []int{1, 2}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			base := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
			state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), servers: map[string]a2aBusServerRecord{}}
			for i := range 3 {
				state.servers[base.Name+"-"+strconv.Itoa(i)] = a2aBusServerRecord{UID: types.UID("pod-uid-" + strconv.Itoa(i)), Start: t0, Loaded: t0}
			}
			pending := &corev1.Secret{}
			state.stamp(pending, base.Name)
			r, agent, sts, _ := a2aClusterReloadFixture(t, "new", pending.Annotations, tc.notReady...)
			r.busConfigWrittenAt = func(*corev1.Secret) (time.Time, bool) { return asked.Add(time.Second), true }
			// Server 0 Ready but unconfirmed; server 1 Pending (no /varz);
			// server 2 confirmed.
			r.busVarz = varzOf(map[string]a2aBusVarz{
				sts.Name + "-0": {Start: t0, Loaded: t0},
				sts.Name + "-2": {Start: t0, Loaded: t1},
			}, sts.Name+"-1")
			plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(a2aBusReloadBound))
			if err != nil {
				t.Fatal(err)
			}
			want := ""
			if tc.want != "" {
				want = sts.Name + tc.want
			}
			if plan.restart != want {
				t.Fatalf("restart = %q, want %q", plan.restart, want)
			}
		})
	}
}

// A renewed TLS certificate synced into the pods while a reload is pending
// reloads the servers on whatever nats.conf they hold: the move that follows
// re-arms the wait, and only a further move confirms.
func TestATLSChangeDuringAReloadFoldsEveryServer(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	r, agent, sts, _ := a2aRecordedCluster(t, asked, asked.Add(time.Second))
	tls := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: agentv1alpha1.A2ANATSTLSSecretName(agent.Name), Namespace: agent.Namespace}}
	if err := r.Create(context.Background(), tls); err != nil {
		t.Fatal(err)
	}
	// The record saw the TLS Secret as it was.
	live := &corev1.Secret{}
	if err := r.Get(context.Background(), client.ObjectKey{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}, live); err != nil {
		t.Fatal(err)
	}
	live.Annotations[a2aBusReloadWatchedAnnotation] = tls.ResourceVersion
	if err := r.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	// The certificate is renewed.
	tls.Data = map[string][]byte{"tls.crt": []byte("renewed")}
	if err := r.Update(context.Background(), tls); err != nil {
		t.Fatal(err)
	}
	v := map[string]a2aBusVarz{}
	for i := range 3 {
		v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
	}
	r.busVarz = varzOf(v)
	config := a2aDesiredConfig(agent, "new")
	plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("a move after a TLS change confirmed the reload")
	}
	state, _ := readA2ABusReloadState(config.Annotations, sts.Name)
	if len(state.rearmed) != 3 || state.watched != tls.ResourceVersion {
		t.Fatalf("rearmed %v, watched %q: want every server re-armed and the new version recorded", state.rearmed, state.watched)
	}
}

// A server already re-armed folds again on a TLS change: the TLS reload's
// move must re-arm it, not confirm it.
func TestATLSChangeFoldsAReArmedServerToo(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	base := buildA2ANATSStatefulSet(a2aTestAgentWithBusReplicas(3), "h1")
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), watched: "a-stale-version", servers: map[string]a2aBusServerRecord{}, rearmed: map[string]bool{}}
	for i := range 3 {
		state.servers[base.Name+"-"+strconv.Itoa(i)] = a2aBusServerRecord{UID: types.UID("pod-uid-" + strconv.Itoa(i)), Start: t0, Loaded: t0}
		state.rearmed[base.Name+"-"+strconv.Itoa(i)] = true
	}
	pending := &corev1.Secret{}
	state.stamp(pending, base.Name)
	r, agent, sts, _ := a2aClusterReloadFixture(t, "new", pending.Annotations)
	r.busConfigWrittenAt = func(*corev1.Secret) (time.Time, bool) { return asked, true }
	if err := r.Create(context.Background(), &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: agentv1alpha1.A2ANATSTLSSecretName(agent.Name), Namespace: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	v := map[string]a2aBusVarz{}
	for i := range 3 {
		v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t1}
	}
	r.busVarz = varzOf(v)
	plan, err := r.planA2ABusReload(context.Background(), agent, a2aDesiredConfig(agent, "new"), sts.DeepCopy(), asked.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded {
		t.Fatal("re-armed servers confirmed on the move a TLS change can cause")
	}
}

// A TLS write just before the config write may land in the pods after it:
// every server is folded from the start and stays folded through the
// kubelet's sync window; after it, a further move confirms.
func TestATLSWriteJustBeforeTheConfigFoldsThroughTheSyncWindow(t *testing.T) {
	asked := time.Date(2026, 11, 17, 8, 0, 0, 0, time.UTC)
	r, agent, sts, _ := a2aClusterReloadFixture(t, "old", nil)
	if err := r.Create(context.Background(), &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: agentv1alpha1.A2ANATSTLSSecretName(agent.Name), Namespace: agent.Namespace}}); err != nil {
		t.Fatal(err)
	}
	r.busTLSWrittenAt = func(*corev1.Secret) (time.Time, bool) { return asked.Add(-30 * time.Second), true }
	r.busConfigWrittenAt = func(*corev1.Secret) (time.Time, bool) { return asked, true }
	v := map[string]a2aBusVarz{}
	for i := range 3 {
		v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: t0}
	}
	r.busVarz = varzOf(v)
	config := a2aDesiredConfig(agent, "new")
	if _, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), asked); err != nil {
		t.Fatal(err)
	}
	state, _ := readA2ABusReloadState(config.Annotations, sts.Name)
	if len(state.folded) != 3 || state.tlsSeen == "" {
		t.Fatalf("folded %v, tls seen %q: want every server folded from the start", state.folded, state.tlsSeen)
	}
	live := &corev1.Secret{}
	if err := r.Get(context.Background(), client.ObjectKeyFromObject(config), live); err != nil {
		t.Fatal(err)
	}
	live.Data[a2aNATSConfKey], live.Annotations = []byte("new"), config.Annotations
	if err := r.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	pass := func(at time.Duration, loaded string) a2aBusReload {
		t.Helper()
		for i := range 3 {
			v[sts.Name+"-"+strconv.Itoa(i)] = a2aBusVarz{Start: t0, Loaded: loaded}
		}
		cfg := a2aDesiredConfig(agent, "new")
		plan, err := r.planA2ABusReload(context.Background(), agent, cfg, sts.DeepCopy(), asked.Add(at))
		if err != nil {
			t.Fatal(err)
		}
		if !plan.reloaded {
			live := &corev1.Secret{}
			if err := r.Get(context.Background(), client.ObjectKeyFromObject(cfg), live); err != nil {
				t.Fatal(err)
			}
			live.Annotations = cfg.Annotations
			if err := r.Update(context.Background(), live); err != nil {
				t.Fatal(err)
			}
		}
		return plan
	}
	if pass(30*time.Second, "2026-11-17T08:00:40Z").reloaded {
		t.Fatal("a move inside the sync window confirmed the reload")
	}
	if pass(60*time.Second, "2026-11-17T08:01:10Z").reloaded {
		t.Fatal("a second move still inside the window confirmed the reload")
	}
	if !pass(2*time.Minute, "2026-11-17T08:02:00Z").reloaded {
		t.Fatal("a move after the window didn't confirm the reload")
	}
}
