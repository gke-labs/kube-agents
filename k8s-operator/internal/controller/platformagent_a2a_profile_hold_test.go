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
		{"a pod the StatefulSet doesn't control is not a server", func(t *testing.T, b *holdBus) {
			b.revision(t, "bus-b", "x,y", true)
			b.sts.Status.UpdateRevision = "bus-b"
			b.pod(0, "bus-other", func(p *corev1.Pod) { p.OwnerReferences = nil })
		}, "x,y"},
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
	if err := r.stampA2ABusPreviousProfiles(ctx, agent, config); err != nil {
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
	if err := r.stampA2ABusPreviousProfiles(ctx, agent, config); err != nil {
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
	if err := r.stampA2ABusPreviousProfiles(ctx, agent, config); err != nil {
		t.Fatal(err)
	}
	if got := config.Annotations[a2aBusPreviousProfilesAnnotation]; got != "x" {
		t.Fatalf("previous = %q while the reload is pending, want \"x\"", got)
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
