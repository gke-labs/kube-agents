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
			Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: ready}}},
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
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), baselines: map[string]string{}}
	for i := range 3 {
		state.baselines[sts.Name+"-"+strconv.Itoa(i)] = baseline
	}
	config := &corev1.Secret{}
	state.stamp(config)
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
	state := a2aBusReloadState{requestedAt: asked.Format(time.RFC3339Nano), baselines: map[string]string{sts.Name + "-0": "t0"}}
	pending := &corev1.Secret{}
	state.stamp(pending)
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
