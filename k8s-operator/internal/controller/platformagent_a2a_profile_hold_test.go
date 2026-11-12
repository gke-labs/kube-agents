package controller

import (
	"context"
	"encoding/json"
	"slices"
	"strconv"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// holdBus is a bus StatefulSet as the hold reads it, with its pods and
// revisions, for the fake client.
type holdBus struct {
	agent *agentv1alpha1.PlatformAgent
	sts   *appsv1.StatefulSet
	objs  []client.Object
}

func newHoldBus(agent *agentv1alpha1.PlatformAgent) *holdBus {
	sts := buildA2ANATSStatefulSet(agent, "h1")
	sts.UID = "sts-uid"
	return &holdBus{agent: agent, sts: sts}
}

func (b *holdBus) owner() []metav1.OwnerReference {
	return []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: b.sts.Name, UID: b.sts.UID, Controller: ptr.To(true)}}
}

// revision adds a ControllerRevision whose pod template lists profiles.
// owned false makes it someone else's.
func (b *holdBus) revision(t *testing.T, name, profiles string, owned bool) {
	t.Helper()
	data := map[string]any{"spec": map[string]any{"template": map[string]any{
		"$patch":   "replace",
		"metadata": map[string]any{"annotations": map[string]string{a2aConfigProfilesAnnotation: profiles}},
	}}}
	raw, err := json.Marshal(data)
	if err != nil {
		t.Fatal(err)
	}
	rev := &appsv1.ControllerRevision{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: b.agent.Namespace},
		Data:       runtime.RawExtension{Raw: raw},
	}
	if owned {
		rev.OwnerReferences = b.owner()
	}
	b.objs = append(b.objs, rev)
}

// pod adds the server at ordinal on revision.
func (b *holdBus) pod(ordinal int, revision string, mutate ...func(*corev1.Pod)) {
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: b.sts.Name + "-" + strconv.Itoa(ordinal), Namespace: b.agent.Namespace,
		Labels:          map[string]string{appsv1.ControllerRevisionHashLabelKey: revision},
		OwnerReferences: b.owner(),
	}}
	for _, m := range mutate {
		m(pod)
	}
	b.objs = append(b.objs, pod)
}

func (b *holdBus) client(extra ...client.Object) client.Client {
	objs := append([]client.Object{b.agent, b.sts}, b.objs...)
	return fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(append(objs, extra...)...).
		WithStatusSubresource(&agentv1alpha1.AgentProfile{}).Build()
}

func loadedOrFatal(t *testing.T, c client.Reader, agent *agentv1alpha1.PlatformAgent) a2aLoadedProfiles {
	t.Helper()
	loaded, err := a2aBusLoadedProfiles(context.Background(), c, agent)
	if err != nil {
		t.Fatal(err)
	}
	return loaded
}

func loadedList(l a2aLoadedProfiles) string {
	if l.all {
		return "<all>"
	}
	return encodeA2AConfigProfiles(setNames(l.names))
}

// A cluster mid-roll has loaded what every server's revision lists, and
// what the update revision lists for a server still to be created. Once
// every server is on the update revision, it has loaded that revision's list.
func TestAClusterHasLoadedTheProfilesEveryServersRevisionLists(t *testing.T) {
	agent := a2aTestAgentWithBusReplicas(3)
	agent.Status.BusReplicas = 3
	for _, tc := range []struct {
		name  string
		setup func(t *testing.T, b *holdBus)
		want  string
	}{
		{"mid-roll", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-a", "x", true)
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-a")
			b.pod(1, "bus-a")
			b.pod(2, "bus-b")
		}, "x"},
		{"rolled", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			for i := range 3 {
				b.pod(i, "bus-b")
			}
		}, "x,y"},
		{"a server being recreated counts as the update revision", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-a", "x", true)
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-b")
			b.pod(1, "bus-b")
		}, "x,y"},
		{"a terminating server on the old revision still counts", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-a", "x", true)
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-b")
			b.pod(1, "bus-b")
			b.pod(2, "bus-a", func(p *corev1.Pod) {
				p.DeletionTimestamp = &metav1.Time{Time: time.Now()}
				p.Finalizers = []string{"test/hold"}
			})
		}, "x"},
		{"a revision that is not the StatefulSet's loads nothing", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-b", "x,y", false)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-b")
		}, ""},
		{"a revision rendered before the list was recorded loads nothing", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-a", "", true)
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-a")
		}, ""},
		{"a missing revision loads nothing", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-gone")
		}, ""},
		{"a bus pod the StatefulSet doesn't control loads nothing", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-other", func(p *corev1.Pod) { p.OwnerReferences = nil })
		}, ""},
		{"no revision yet loads nothing", func(t *testing.T, b *holdBus) {}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := newHoldBus(agent.DeepCopy())
			tc.setup(t, b)
			if got := loadedList(loadedOrFatal(t, b.client(), b.agent)); got != tc.want {
				t.Errorf("loaded = %q, want %q", got, tc.want)
			}
		})
	}
}

// With no bus StatefulSet no server runs an older config: the first one
// starts on a config with every bound profile, so nothing waits.
func TestWithNoBusEveryProfileCountsAsLoaded(t *testing.T) {
	for _, replicas := range []int{1, 3} {
		agent := a2aTestAgentWithBusReplicas(replicas)
		agent.Status.BusReplicas = int32(replicas) // #nosec G115 -- 1 or 3
		c := fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent).Build()
		if l := loadedOrFatal(t, c, agent); !l.has("anything") {
			t.Errorf("%d servers, no StatefulSet: loaded = %s, want every profile", replicas, loadedList(l))
		}
		// A StatefulSet deleted without its pods: they still serve.
		orphan := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: a2aNATSName(agent) + "-1", Namespace: agent.Namespace}}
		c = fake.NewClientBuilder().WithScheme(setupScheme()).WithObjects(agent, orphan).Build()
		if l := loadedOrFatal(t, c, agent); l.has("anything") {
			t.Errorf("%d servers, no StatefulSet but a bus pod left: loaded = %s, want nothing", replicas, loadedList(l))
		}
	}
}

// A single server has loaded the list its config Secret renders, narrowed
// to the previous list while a reload is pending or the server may still
// run the earlier config.
func TestASingleServerHasLoadedTheSecretsListOnceTheReloadLands(t *testing.T) {
	agent := a2aSingleServerTestAgent()
	settled := func(b *holdBus) {
		b.sts.Generation = 1
		b.sts.Status.ObservedGeneration = 1
		b.sts.Status.UpdatedReplicas = 1
		b.sts.Status.CurrentRevision, b.sts.Status.UpdateRevision = "bus-a", "bus-a"
	}
	terminating := func(p *corev1.Pod) {
		p.DeletionTimestamp = &metav1.Time{Time: time.Now()}
		p.Finalizers = []string{"test/hold"}
	}
	for _, tc := range []struct {
		name        string
		annotations map[string]string
		setup       func(b *holdBus)
		want        string
	}{
		{"no reload pending, nothing recorded", map[string]string{a2aConfigProfilesAnnotation: "x,y"}, func(b *holdBus) { settled(b); b.pod(0, "bus-a") }, "x,y"},
		{"reload pending", map[string]string{
			a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x",
			a2aBusReloadRequestedAnnotation: "2026-11-11T00:00:00Z", a2aBusReloadBaselineAnnotation: "t0",
		}, func(b *holdBus) { settled(b); b.pod(0, "bus-a") }, "x"},
		{"reload pending, no previous list recorded", map[string]string{
			a2aConfigProfilesAnnotation: "x,y", a2aBusReloadRequestedAnnotation: "2026-11-11T00:00:00Z",
		}, func(b *holdBus) { settled(b); b.pod(0, "bus-a") }, ""},
		{"restarted for a failed reload, the old pod still exiting", map[string]string{
			a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x",
		}, func(b *holdBus) { settled(b); b.pod(0, "bus-a", terminating) }, "x"},
		{"rolling onto a new template", map[string]string{
			a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x",
		}, func(b *holdBus) {
			settled(b)
			b.sts.Generation = 2
			b.pod(0, "bus-a")
		}, "x"},
		{"settled on the new config", map[string]string{
			a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x",
		}, func(b *holdBus) { settled(b); b.pod(0, "bus-a") }, "x,y"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := newHoldBus(agent.DeepCopy())
			tc.setup(b)
			secret := &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace, Annotations: tc.annotations},
				Data:       map[string][]byte{a2aNATSConfKey: []byte("conf")},
			}
			if got := loadedList(loadedOrFatal(t, b.client(secret), b.agent)); got != tc.want {
				t.Errorf("loaded = %q, want %q", got, tc.want)
			}
		})
	}
}

// The config Secret records the server's previous list from the pass that
// changes the config, carries it while the server may still run the earlier
// one, and drops it once the server is settled with no reload pending.
func TestTheConfigSecretRecordsThePreviousListUntilTheServerIsOnTheNewOne(t *testing.T) {
	agent := a2aSingleServerTestAgent()
	b := newHoldBus(agent)
	b.sts.Generation, b.sts.Status.ObservedGeneration, b.sts.Status.UpdatedReplicas = 1, 1, 1
	b.pod(0, "bus-a")
	live := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace,
			Annotations: map[string]string{a2aConfigProfilesAnnotation: "x"}},
		Data: map[string][]byte{a2aNATSConfKey: []byte("old")},
	}
	c := b.client(live)
	r := &PlatformAgentReconciler{Client: c, Scheme: setupScheme()}
	ctx := context.Background()

	// A new config: the previous list is what the server has now.
	config := a2aDesiredConfig(agent, "new")
	config.Annotations = map[string]string{a2aConfigProfilesAnnotation: "x,y"}
	if err := r.stampA2ABusPreviousProfiles(ctx, agent, config, b.sts); err != nil {
		t.Fatal(err)
	}
	if got, ok := config.Annotations[a2aBusPreviousProfilesAnnotation]; !ok || got != "x" {
		t.Fatalf("previous = %q (recorded %v), want \"x\"", got, ok)
	}

	// The same config with the reload confirmed and the server settled:
	// the record is dropped.
	live.Data[a2aNATSConfKey] = []byte("new")
	live.Annotations = map[string]string{a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x"}
	if err := c.Update(ctx, live); err != nil {
		t.Fatal(err)
	}
	config = a2aDesiredConfig(agent, "new")
	if err := r.stampA2ABusPreviousProfiles(ctx, agent, config, b.sts); err != nil {
		t.Fatal(err)
	}
	if got, ok := config.Annotations[a2aBusPreviousProfilesAnnotation]; ok {
		t.Fatalf("previous = %q still recorded on a settled server", got)
	}

	// Still pending: carried as it was.
	config = a2aDesiredConfig(agent, "new")
	stampA2ABusReload(config, "2026-11-11T00:00:00Z", "t0")
	live.Annotations[a2aBusReloadRequestedAnnotation] = "2026-11-11T00:00:00Z"
	if err := c.Update(ctx, live); err != nil {
		t.Fatal(err)
	}
	if err := r.stampA2ABusPreviousProfiles(ctx, agent, config, b.sts); err != nil {
		t.Fatal(err)
	}
	if got := config.Annotations[a2aBusPreviousProfilesAnnotation]; got != "x" {
		t.Fatalf("previous = %q while the reload is pending, want \"x\"", got)
	}
}

// A roll about to start (the StatefulSet this pass applies carries a new
// config hash) keeps the previous list, though the live StatefulSet still
// reads as settled: until the roll replaces the pod, it may run the earlier
// config. So does a retry after the StatefulSet's apply failed, which reads
// the same way.
func TestTheSecretKeepsThePreviousListWhileARollIsComing(t *testing.T) {
	agent := a2aSingleServerTestAgent()
	b := newHoldBus(agent)
	b.sts.Generation, b.sts.Status.ObservedGeneration, b.sts.Status.UpdatedReplicas = 1, 1, 1
	b.pod(0, "bus-a")
	live := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace,
			Annotations: map[string]string{a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x"}},
		Data: map[string][]byte{a2aNATSConfKey: []byte("new")},
	}
	r := &PlatformAgentReconciler{Client: b.client(live), Scheme: setupScheme()}
	desired := b.sts.DeepCopy()
	desired.Spec.Template.Annotations = map[string]string{a2aConfigHashAnnotation: "h2"}
	config := a2aDesiredConfig(agent, "new")
	if err := r.stampA2ABusPreviousProfiles(context.Background(), agent, config, desired); err != nil {
		t.Fatal(err)
	}
	if got, ok := config.Annotations[a2aBusPreviousProfilesAnnotation]; !ok || got != "x" {
		t.Fatalf("previous = %q (recorded %v) with a roll coming, want \"x\" kept", got, ok)
	}
	// The same template as the live one: no roll, and a settled server
	// drops the record.
	config = a2aDesiredConfig(agent, "new")
	if err := r.stampA2ABusPreviousProfiles(context.Background(), agent, config, b.sts); err != nil {
		t.Fatal(err)
	}
	if got, ok := config.Annotations[a2aBusPreviousProfilesAnnotation]; ok {
		t.Fatalf("previous = %q still recorded with no roll coming on a settled server", got)
	}
}

// The read side waits for the hash the record names: a StatefulSet that
// reads as settled on an earlier template (its apply failed, or the roll
// hasn't started) still holds the new profile back.
func TestASingleServerOnAnEarlierTemplateHasNotLoadedTheNewList(t *testing.T) {
	agent := a2aSingleServerTestAgent()
	b := newHoldBus(agent)
	b.sts.Generation, b.sts.Status.ObservedGeneration, b.sts.Status.UpdatedReplicas = 1, 1, 1
	b.pod(0, "bus-a")
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace, Annotations: map[string]string{
			a2aConfigProfilesAnnotation: "x,y", a2aBusPreviousProfilesAnnotation: "x", a2aBusPreviousUntilHashAnnotation: "h2",
		}},
		Data: map[string][]byte{a2aNATSConfKey: []byte("conf")},
	}
	if got := loadedList(loadedOrFatal(t, b.client(secret), b.agent)); got != "x" {
		t.Errorf("loaded = %q on template h1 with the record waiting for h2, want \"x\"", got)
	}
	b2 := newHoldBus(agent.DeepCopy())
	b2.sts.Spec.Template.Annotations[a2aConfigHashAnnotation] = "h2"
	b2.sts.Generation, b2.sts.Status.ObservedGeneration, b2.sts.Status.UpdatedReplicas = 1, 1, 1
	b2.pod(0, "bus-a")
	if got := loadedList(loadedOrFatal(t, b2.client(secret.DeepCopy()), b2.agent)); got != "x,y" {
		t.Errorf("loaded = %q settled on h2, want \"x,y\"", got)
	}
}

// The profile list rides a cluster's pod template, so each revision records
// it, and stays off a single server's, where it reaches the server by a
// reload and a template change would restart it.
func TestTheProfileListRidesOnlyAClustersPodTemplate(t *testing.T) {
	cluster := a2aTestAgentWithBusReplicas(3)
	sts := buildA2ANATSStatefulSet(cluster, "h1")
	annotateA2ABusProfiles(cluster, sts, []string{"y", "x", "y"})
	if got := sts.Spec.Template.Annotations[a2aConfigProfilesAnnotation]; got != "x,y" {
		t.Errorf("cluster template lists %q, want \"x,y\"", got)
	}
	single := a2aSingleServerTestAgent()
	sts = buildA2ANATSStatefulSet(single, "h1")
	annotateA2ABusProfiles(single, sts, []string{"x"})
	if got, ok := sts.Spec.Template.Annotations[a2aConfigProfilesAnnotation]; ok {
		t.Errorf("single-server template lists %q; a profile change would restart the server", got)
	}
	secret := buildA2ANATSConfigSecret(single, &corev1.Secret{}, &a2aCalloutKeys{}, []string{"y", "x"})
	if got := secret.Annotations[a2aConfigProfilesAnnotation]; got != "x,y" {
		t.Errorf("config Secret lists %q, want \"x,y\"", got)
	}
}

// A newly bound profile waits; one the dispatch config already lists live
// does not, even when the bus can't say it has loaded it.
func TestANewlyBoundProfileIsHeldUntilTheBusHasLoadedIt(t *testing.T) {
	agent := a2aTestAgentWithBusReplicas(3)
	agent.Status.BusReplicas = 3
	b := newHoldBus(agent)
	b.revision(t, "bus-a", "x", true)
	b.revision(t, "bus-b", "x,y,z", true)
	b.sts.Status.UpdateRevision = "bus-b"
	b.pod(0, "bus-a")
	applied, err := json.Marshal(a2aDispatchConfig{Profiles: []a2aDispatchProfile{
		{Name: "z"},
		{Name: "gone", DrainingUntil: time.Now().Add(time.Hour)},
	}})
	if err != nil {
		t.Fatal(err)
	}
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: a2aDispatchConfigName(agent), Namespace: agent.Namespace},
		Data:       map[string]string{a2aDispatchConfigKey: string(applied)},
	}
	r := &PlatformAgentReconciler{Client: b.client(cm), Scheme: setupScheme()}
	profiles := []agentv1alpha1.AgentProfile{
		testAgentProfile(agent.Namespace, "x"),
		testAgentProfile(agent.Namespace, "y"),
		testAgentProfile(agent.Namespace, "z"),
	}
	dispatchable, held, err := r.a2aDispatchableProfiles(context.Background(), agent, profiles)
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, p := range dispatchable {
		names = append(names, p.Name)
	}
	if !slices.Equal(names, []string{"x", "z"}) || !slices.Equal(held, []string{"y"}) {
		t.Errorf("dispatchable %v, held %v; want [x z] and [y]", names, held)
	}
}

// A held profile keeps its own identities in the map, but the dispatcher's
// grant leaves out its supervisor subjects until it is dispatchable.
func TestAHeldProfileIsOutOfTheDispatchersGrantOnly(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	x, y := testAgentProfile(agent.Namespace, "x"), testAgentProfile(agent.Namespace, "y")
	doc, err := renderA2AAuthMapHeld(agent, []agentv1alpha1.AgentProfile{x, y}, []agentv1alpha1.AgentProfile{x}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var dispatcher *a2aAuthMapIdentity
	var yIdentities int
	for i := range doc.Identities {
		id := &doc.Identities[i]
		if id.User == a2aDispatcherUser {
			dispatcher = id
		}
		if strings.Contains(id.ServiceAccount, "agentprofile-y-") {
			yIdentities++
		}
	}
	if dispatcher == nil {
		t.Fatal("no dispatcher in the map")
	}
	grants := strings.Join(append(slices.Clone(dispatcher.Grants.Publish), dispatcher.Grants.Subscribe...), " ")
	if !strings.Contains(grants, "a2a.tasks.x.*.supervisor") || strings.Contains(grants, "a2a.tasks.y.") {
		t.Errorf("dispatcher grants %q: want x's supervisor subjects and none of y's", grants)
	}
	if yIdentities == 0 {
		t.Error("the held profile lost its own identities")
	}
}

// The card waits with the dispatcher: a newly bound profile's card is not
// published, and the profile says why, until every bus server has loaded its
// config. A card already on the directory stays.
func TestANewlyBoundProfilesCardWaitsForTheBus(t *testing.T) {
	withOperatorBusPrincipal(t)
	agent := a2aTestAgentWithBusReplicas(3)
	agent.Status.BusReplicas = 3
	p := testAgentProfile(agent.Namespace, "auditor")
	b := newHoldBus(agent)
	b.revision(t, "bus-a", "", true)
	b.sts.Status.UpdateRevision = "bus-a"
	b.pod(0, "bus-a")
	c := b.client(chartProfileNamespace(agent), &p)
	dir := newFakeDirectory()
	h := &profileHarness{t: t, c: c, r: &AgentProfileReconciler{Client: c, Scheme: setupScheme(), Cards: dir}, dir: dir}

	res := h.reconcile(agent.Namespace, "auditor")
	if len(dir.publishes) != 0 {
		t.Fatalf("published %v before the bus loaded the profile", dir.publishes)
	}
	if res.RequeueAfter != a2aBusReloadRecheck {
		t.Errorf("RequeueAfter = %v, want %v while held", res.RequeueAfter, a2aBusReloadRecheck)
	}
	if got := condition(h.profile(agent.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionCardPublished); got.Status != metav1.ConditionFalse || got.Reason != reasonAgentProfileBusConfigPending {
		t.Errorf("CardPublished = %s/%s, want False/%s", got.Status, got.Reason, reasonAgentProfileBusConfigPending)
	}

	// Every server rolls onto a revision listing it: published.
	rev := &appsv1.ControllerRevision{}
	if err := c.Get(context.Background(), types.NamespacedName{Name: "bus-a", Namespace: agent.Namespace}, rev); err != nil {
		t.Fatal(err)
	}
	rev.Data.Raw = []byte(`{"spec":{"template":{"metadata":{"annotations":{"` + a2aConfigProfilesAnnotation + `":"auditor"}}}}}`)
	if err := c.Update(context.Background(), rev); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "auditor")
	if !slices.Equal(dir.publishes, []string{"card:auditor"}) {
		t.Fatalf("publishes = %v, want the card once the bus has the profile", dir.publishes)
	}

	// The bus reads as not having it again (a roll onto a revision
	// recorded before the list, say): the published card stays.
	rev.Data.Raw = []byte(`{"spec":{"template":{"metadata":{}}}}`)
	if err := c.Update(context.Background(), rev); err != nil {
		t.Fatal(err)
	}
	h.reconcile(agent.Namespace, "auditor")
	if got := condition(h.profile(agent.Namespace, "auditor"), agentv1alpha1.AgentProfileConditionCardPublished); got.Reason != reasonAgentProfilePublished {
		t.Errorf("CardPublished reason = %s after the bus read changed, want %s: a published card stays", got.Reason, reasonAgentProfilePublished)
	}
}

// The whole pass: with a bus running whose servers haven't loaded a newly
// bound profile, the dispatch config and the dispatcher's grant leave it out,
// the bus config carries it, and the pass asks to look again at the recheck.
func TestTheReconcileHoldsANewProfileOutOfTheDispatcherUntilTheBusHasIt(t *testing.T) {
	withDispatcher(t)
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	b := newHoldBus(agent)
	b.revision(t, "bus-a", "", true)
	b.sts.Status.UpdateRevision = "bus-a"
	b.pod(0, "bus-a")
	p := testAgentProfile(agent.Namespace, "auditor")
	extra := append([]client.Object{b.sts, chartProfileNamespace(agent), &p}, b.objs...)
	r, cl, req := a2aGateTestReconciler(t, agent, extra...)
	ctx := context.Background()

	var res ctrl.Result
	for i := range 3 {
		var err error
		if res, err = r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	if res.RequeueAfter <= 0 || res.RequeueAfter > a2aBusReloadRecheck {
		t.Errorf("RequeueAfter = %v, want at most %v while a profile is held", res.RequeueAfter, a2aBusReloadRecheck)
	}
	cm := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}, cm); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(cm.Data[a2aDispatchConfigKey], `"auditor"`) {
		t.Errorf("the dispatch config lists the held profile: %s", cm.Data[a2aDispatchConfigKey])
	}
	shard := &corev1.ConfigMap{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aAuthMapShardName(agent, 0)}, shard); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(shard.Data[a2aAuthMapKey], `"a2a.tasks.auditor.*.supervisor"`) {
		t.Error("the dispatcher's grant carries the held profile's supervisor subjects")
	}
	config := &corev1.Secret{}
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}, config); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(config.Data[a2aNATSConfKey]), "a2a.tasks.auditor.*.supervisor") {
		t.Error("the bus config lacks the held profile's deny; nothing would ever load it")
	}
}

// A reload a later config was folded into is not confirmed by one load-time
// move: the move may be the earlier write landing, so the wait re-arms off
// the new load time, and the profile it would admit stays held.
func TestAFoldedReloadIsNotConfirmedByTheFirstMove(t *testing.T) {
	r, agent, sts, _ := a2aReloadFixture(t, "v1", map[string]string{
		a2aBusReloadRequestedAnnotation: time.Date(2026, 11, 9, 22, 0, 0, 0, time.UTC).Format(time.RFC3339Nano),
		a2aBusReloadBaselineAnnotation:  "t0",
	})
	now := time.Date(2026, 11, 9, 22, 0, 30, 0, time.UTC)
	r.busConfigLoadTime = func(context.Context, *agentv1alpha1.PlatformAgent) (string, error) { return "t0", nil }
	config := a2aDesiredConfig(agent, "v2")
	if _, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), now); err != nil {
		t.Fatal(err)
	}
	if config.Annotations[a2aBusReloadFoldedAnnotation] == "" {
		t.Fatalf("a write into a pending reload is not marked folded: %v", config.Annotations)
	}

	// v2 is live with the fold recorded; the load time moves.
	live := &corev1.Secret{}
	if err := r.Get(context.Background(), types.NamespacedName{Name: config.Name, Namespace: config.Namespace}, live); err != nil {
		t.Fatal(err)
	}
	live.Data[a2aNATSConfKey], live.Annotations = []byte("v2"), config.Annotations
	if err := r.Update(context.Background(), live); err != nil {
		t.Fatal(err)
	}
	r.busConfigLoadTime = func(context.Context, *agentv1alpha1.PlatformAgent) (string, error) { return "t1", nil }
	config = a2aDesiredConfig(agent, "v2")
	plan, err := r.planA2ABusReload(context.Background(), agent, config, sts.DeepCopy(), now.Add(time.Minute))
	if err != nil {
		t.Fatal(err)
	}
	if plan.reloaded || plan.restartPod {
		t.Fatalf("plan = %+v: the first move confirmed a folded reload", plan)
	}
	if config.Annotations[a2aBusReloadBaselineAnnotation] != "t1" || config.Annotations[a2aBusReloadFoldedAnnotation] != "" {
		t.Errorf("the wait didn't re-arm off the new load time: %v", config.Annotations)
	}
}

// A bus whose render is one server while the StatefulSet still runs three
// (a conversion to one server) is read as the cluster it still is: the
// single server's Secret list from the cluster's time was never confirmed.
func TestABusConvertingToOneServerIsReadAsACluster(t *testing.T) {
	agent := a2aSingleServerTestAgent()
	b := newHoldBus(agent)
	b.sts.Status.Replicas = 3
	b.revision(t, "bus-a", "x", true)
	b.revision(t, "bus-b", "", true)
	b.sts.Status.UpdateRevision = "bus-b"
	b.pod(0, "bus-b")
	b.pod(1, "bus-a")
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: a2aNATSConfigSecretName(agent), Namespace: agent.Namespace,
			Annotations: map[string]string{a2aConfigProfilesAnnotation: "x,y"}},
		Data: map[string][]byte{a2aNATSConfKey: []byte("conf")},
	}
	if got := loadedList(loadedOrFatal(t, b.client(secret), b.agent)); got != "" {
		t.Errorf("loaded = %q mid-conversion, want nothing: the one-server revision records no list", got)
	}
}
