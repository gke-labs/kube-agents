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
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/tools/record"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

const (
	dispatcherMapFixturePath  = "../../../a2a/authcallout/testdata/rendered-identity-map-dispatcher.json"
	dispatchConfigFixturePath = "../../../a2a/lib/testdata/rendered-dispatch-config.json"
	testDispatcherImage       = "registry.example/a2a-dispatcher:test"
)

func withDispatcher(t *testing.T) {
	t.Helper()
	t.Setenv(a2aDispatcherImageEnvVar, testDispatcherImage)
}

// Without an image the operator renders no dispatcher, not even its bus
// identity: the image isn't in the release set yet.
func TestNoDispatcherImageRendersNoDispatcher(t *testing.T) {
	hasDispatcher := func() bool {
		doc, err := renderA2AAuthMap(a2aTestAgent(), nil)
		if err != nil {
			t.Fatal(err)
		}
		for _, id := range doc.Identities {
			if id.User == a2aDispatcherUser {
				return true
			}
		}
		return false
	}
	t.Setenv(a2aDispatcherImageEnvVar, "")
	if hasDispatcher() {
		t.Fatal("the identity map carries the dispatcher with no image to run it")
	}
	withDispatcher(t)
	if !hasDispatcher() {
		t.Error("with an image, the identity map has no dispatcher")
	}
}

// The dispatcher's grants are exactly the contract's, and in particular it
// reads no task subject and creates no consumer on TASKS.
func TestTheDispatcherIdentityHoldsOnlyItsContractGrants(t *testing.T) {
	agent := a2aTestAgent()
	id := dispatcherIdentity(agent, agent.Namespace, []string{"tse"}, []string{"profile-tse-read-top", "profile-tse-read-child"})
	if id.serviceAccount != "system:serviceaccount:test-ns:test-agent-a2a-dispatcher" || id.auth != a2aAuthCallout {
		t.Errorf("serviceAccount %q, auth %v", id.serviceAccount, id.auth)
	}
	for _, s := range id.subscribe {
		if s == ">" || slices.Contains([]string{"a2a.tasks.>", "a2a.>", "a2a.tasks.*.*.in", "a2a.tasks.*.*.events"}, s) {
			t.Errorf("the dispatcher subscribes to %s; it reads submissions only through its durables", s)
		}
	}
	for _, p := range id.publish {
		switch p {
		case "$JS.API.CONSUMER.CREATE.TASKS.>", "$JS.API.CONSUMER.DURABLE.CREATE.TASKS.>", "$KV.cap.>", "a2a.tasks.>", ">":
			t.Errorf("the dispatcher publishes %s", p)
		}
	}
	for _, p := range id.publish {
		if strings.Contains(p, "TASKS") || strings.Contains(p, "PROFILE_SUBMITS.*") || strings.Contains(p, "profile-*") {
			t.Errorf("the dispatcher's grant %s reaches TASKS or a wildcard of consumers: it reads only its own durables", p)
		}
	}
	for _, p := range id.publish {
		if strings.HasSuffix(p, ".events") || p == "a2a.tasks.*.*.supervisor" {
			t.Errorf("the dispatcher publishes %s: its terminals go on its profiles' .supervisor, so .events stays the executor's alone", p)
		}
	}
	for _, want := range []string{"$KV.PROFILE_LEASES.>", "a2a.dispatch.*.*.*", "a2a.tasks.tse.*.supervisor", "a2a.cap.verify.dispatcher",
		"$JS.API.CONSUMER.MSG.NEXT.PROFILE_SUBMITS.profile-tse-read-top", "$JS.ACK.PROFILE_SUBMITS.profile-tse-read-child.>"} {
		if !slices.Contains(id.publish, want) {
			t.Errorf("the dispatcher can't publish %s", want)
		}
	}
	for _, want := range []string{"a2a.dispatch.*.*.*.accept", "a2a.dispatch.*.*.*.lease", "a2a.cap.reply.dispatcher.>"} {
		if !slices.Contains(id.subscribe, want) {
			t.Errorf("the dispatcher can't subscribe %s", want)
		}
	}
}

// One dispatcher, replaced with Recreate, running the operator's image, as
// its own ServiceAccount, told where its config is.
func TestTheDispatcherDeploymentRunsOneDispatcher(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	dep := buildA2ADispatcherDeployment(agent)
	if *dep.Spec.Replicas != 1 || dep.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Errorf("replicas %d, strategy %s; two dispatchers would each enforce the ceiling on half the picture", *dep.Spec.Replicas, dep.Spec.Strategy.Type)
	}
	pod := dep.Spec.Template.Spec
	if pod.ServiceAccountName != a2aDispatcherName(agent) || pod.Containers[0].Image != testDispatcherImage {
		t.Errorf("serviceAccount %q, image %q", pod.ServiceAccountName, pod.Containers[0].Image)
	}
	env := map[string]string{}
	for _, e := range pod.Containers[0].Env {
		env[e.Name] = e.Value
	}
	if env["DISPATCH_CONFIGMAP"] != a2aDispatchConfigName(agent) {
		t.Errorf("DISPATCH_CONFIGMAP = %q", env["DISPATCH_CONFIGMAP"])
	}
}

// The config the dispatcher reads: every value resolved, refused profiles
// left out, and a durable per (tier, kind) for the rest.
func TestTheDispatchConfigIsResolvedAndListsTheDurables(t *testing.T) {
	agent := a2aTestAgent()
	both := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.AllowedTiers = []string{agentv1alpha1.AgentProfileTierRead, agentv1alpha1.AgentProfileTierUnattended}
		p.Spec.Concurrency = 3
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "cluster", Required: true}}
	})
	refused := testAgentProfile(agent.Namespace, "platform") // the bridge's addressee
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{both, refused})
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Ceiling != defaultKanbanMaxInProgress || cfg.ChildReserve != 2 || cfg.InteractiveReserve != 2 || cfg.LeaseSeconds != 90 || cfg.Namespace != "test-ns" || cfg.ProfileNamespace != "test-ns-profiles" {
		t.Errorf("config = %+v", cfg)
	}
	if len(cfg.Profiles) != 1 || cfg.Profiles[0].Name != "tse" {
		t.Fatalf("profiles = %+v, want tse alone (platform is refused)", cfg.Profiles)
	}
	var names []string
	for _, d := range cfg.Profiles[0].Durables {
		names = append(names, d.Name)
	}
	want := []string{"profile-tse-read-top", "profile-tse-read-child", "profile-tse-unattended-top", "profile-tse-unattended-child"}
	if !slices.Equal(names, want) {
		t.Errorf("durables = %v, want %v", names, want)
	}
	again, _ := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{both, refused})
	if again.Version != cfg.Version || cfg.Version == "" {
		t.Errorf("version %q then %q: the version must follow the content", cfg.Version, again.Version)
	}
	tuned := agent.DeepCopy()
	tuned.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{Profiles: &agentv1alpha1.ProfileTuning{LeaseSeconds: ptr.To(120)}}}
	changed, _ := desiredDispatchConfig(tuned, []agentv1alpha1.AgentProfile{both})
	if changed.Version == cfg.Version {
		t.Error("a tuning change kept the version")
	}
}

// The cross-module fixtures: the identity map with the dispatcher in it,
// which the callout suite runs a real server against, and the dispatch
// config, which lib decodes and validates.
func TestRenderedDispatcherFixtures(t *testing.T) {
	withDispatcher(t)
	// With the operator, whose durable grants come with the dispatcher.
	withOperatorBusPrincipal(t)
	agent := authMapTestAgent()
	tse := testAgentProfile(agent.Namespace, "tse", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Parameters = []agentv1alpha1.AgentProfileParameter{{Name: "project", Required: true}, {Name: "location", Required: true}, {Name: "cluster", Required: true}}
	})
	cm, _, err := buildA2AAuthMapConfigMap(agent, []agentv1alpha1.AgentProfile{tse})
	if err != nil {
		t.Fatalf("buildA2AAuthMapConfigMap: %v", err)
	}
	checkFixture(t, dispatcherMapFixturePath, cm.Data[a2aAuthMapKey])
	cfgMap, err := buildA2ADispatchConfigMap(agent, []agentv1alpha1.AgentProfile{tse})
	if err != nil {
		t.Fatal(err)
	}
	checkFixture(t, dispatchConfigFixturePath, cfgMap.Data[a2aDispatchConfigKey])
	var decoded map[string]any
	if err := json.Unmarshal([]byte(cfgMap.Data[a2aDispatchConfigKey]), &decoded); err != nil {
		t.Fatalf("the dispatch config is not JSON: %v", err)
	}
}

// Turning the image off removes what the dispatcher had: the owned objects
// in the agent's namespace, and the pod-read Role and binding in the profile
// namespace that carry this agent's labels, and nothing someone else made.
func TestRemovingTheDispatcherImageRemovesTheDispatcher(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent)).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	ctx := context.Background()
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("render: %v / %v", err, profileErr)
	}
	pns := profileNamespaceName(agent)
	name := a2aDispatcherName(agent)
	for _, check := range []struct {
		obj client.Object
		key types.NamespacedName
	}{
		{&appsv1.Deployment{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&corev1.ConfigMap{}, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: pns, Name: name}},
		{&rbacv1.RoleBinding{}, types.NamespacedName{Namespace: pns, Name: name}},
	} {
		if err := c.Get(ctx, check.key, check.obj); err != nil {
			t.Fatalf("precondition: %T %s not rendered: %v", check.obj, check.key, err)
		}
	}
	t.Setenv(a2aDispatcherImageEnvVar, "")
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("remove: %v / %v", err, profileErr)
	}
	for _, check := range []struct {
		obj client.Object
		key types.NamespacedName
	}{
		{&appsv1.Deployment{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&corev1.ServiceAccount{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&corev1.ConfigMap{}, types.NamespacedName{Namespace: agent.Namespace, Name: a2aDispatchConfigName(agent)}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: agent.Namespace, Name: name}},
		{&rbacv1.Role{}, types.NamespacedName{Namespace: pns, Name: name}},
		{&rbacv1.RoleBinding{}, types.NamespacedName{Namespace: pns, Name: name}},
	} {
		if err := c.Get(ctx, check.key, check.obj); !apierrors.IsNotFound(err) {
			t.Errorf("%T %s survived the image going away: %v", check.obj, check.key, err)
		}
	}
	// A Role under the dispatcher's name in the profile namespace that
	// doesn't carry this agent's labels is someone else's, and stays.
	foreign := &rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: pns, Labels: map[string]string{"x": "y"}}}
	if err := c.Create(ctx, foreign); err != nil {
		t.Fatal(err)
	}
	if profileErr, err := r.reconcileA2ADispatcher(ctx, agent, nil, true); err != nil || profileErr != nil {
		t.Fatalf("remove again: %v / %v", err, profileErr)
	}
	if err := c.Get(ctx, types.NamespacedName{Namespace: pns, Name: name}, &rbacv1.Role{}); err != nil {
		t.Errorf("an unlabelled Role under the dispatcher's name was deleted: %v", err)
	}
}

// TASKS is sized for the sessions alone, dispatcher or not: the profiles'
// durables live on PROFILE_SUBMITS, whose cap is their allowance.
func TestTheProfileDurablesTakeNoTasksSlot(t *testing.T) {
	agent := a2aTestAgent()
	t.Setenv(a2aDispatcherImageEnvVar, "")
	without := a2aTasksMaxConsumers(agent)
	withDispatcher(t)
	if got := a2aTasksMaxConsumers(agent); got != without {
		t.Errorf("a new TASKS with a dispatcher gets max_consumers %d, want %d as without one", got, without)
	}
	script := a2aProvisionScript(agent)
	if !strings.Contains(script, "--max-consumers="+strconv.Itoa(a2aProfileDurablesAllowed)+" --defaults") || !strings.Contains(script, "stream add PROFILE_SUBMITS") {
		t.Error("the provision script doesn't create PROFILE_SUBMITS with the durables' allowance as its cap")
	}
}

// The reserves are disjoint shares of the ceiling that always leave a slot
// for top-level unattended work, child first, and nothing at a ceiling of 1.
func TestTheDispatchReservesLeaveASlotForEveryKind(t *testing.T) {
	for ceiling, want := range map[int][2]int{
		1: {0, 0}, 2: {1, 0}, 3: {1, 1}, 4: {2, 1}, 6: {2, 2}, 9: {3, 3}, 10: {4, 4},
	} {
		agent := profileTunedAgent(ptr.To(ceiling), nil)
		child, interactive := dispatchReserves(agent, ceiling)
		if child != want[0] || interactive != want[1] {
			t.Errorf("ceiling %d: child %d, interactive %d; want %d, %d", ceiling, child, interactive, want[0], want[1])
		}
		if ceiling > 1 && child+interactive > ceiling-1 {
			t.Errorf("ceiling %d: reserves %d+%d leave no slot for top-level unattended work", ceiling, child, interactive)
		}
	}
}

// A ceiling too small for every reserve warns on the PlatformAgent, naming
// the reserve that is short; the default ceiling honours both.
func TestASmallCeilingWarnsWhichReserveIsShort(t *testing.T) {
	for ceiling, want := range map[int][]string{
		1: {"child reserve is 0 of 1", "interactive reserve is 0 of 1", "at least 3"},
		2: {"interactive reserve is 0 of 1", "at least 3"},
		3: nil,
		// Not monotonic: 4 is short where 3 isn't, and the advice is 5.
		4: {"interactive reserve is 1 of 2", "at least 5"},
		5: nil,
		6: nil,
	} {
		agent := profileTunedAgent(ptr.To(ceiling), nil)
		child, interactive := dispatchReserves(agent, ceiling)
		msg := dispatchReservesShortfall(agent, ceiling, child, interactive)
		if len(want) == 0 && msg != "" {
			t.Errorf("ceiling %d warns: %s", ceiling, msg)
		}
		for _, w := range want {
			if !strings.Contains(msg, w) {
				t.Errorf("ceiling %d: warning %q doesn't say %q", ceiling, msg, w)
			}
		}
	}
	withDispatcher(t)
	agent := profileTunedAgent(ptr.To(2), nil)
	scheme := setupScheme()
	rec := record.NewFakeRecorder(10)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme, Recorder: rec}
	if _, err := r.reconcileA2ADispatcher(context.Background(), agent, nil, false); err != nil {
		t.Fatal(err)
	}
	select {
	case e := <-rec.Events:
		if !strings.Contains(e, reasonProfileReservesShort) || !strings.Contains(e, "interactive reserve") {
			t.Errorf("event = %q", e)
		}
	default:
		t.Error("a ceiling of 2 wrote no warning")
	}
}

// A task deadline past the cap is clamped in the dispatch config, warned on,
// and the callout is told the same bound for task credentials.
func TestATaskDeadlinePastTheCapIsClampedAndWarned(t *testing.T) {
	withDispatcher(t)
	agent := a2aTestAgent()
	long := testAgentProfile(agent.Namespace, "slow", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Lifecycle.ActiveDeadlineSeconds = 86400
	})
	short := testAgentProfile(agent.Namespace, "quick")
	cfg, err := desiredDispatchConfig(agent, []agentv1alpha1.AgentProfile{long, short})
	if err != nil {
		t.Fatal(err)
	}
	got := map[string]int{}
	for _, p := range cfg.Profiles {
		got[p.Name] = p.ActiveDeadlineSeconds
	}
	if got["slow"] != 21600 || got["quick"] != int(short.Spec.Lifecycle.ActiveDeadlineSeconds) {
		t.Errorf("deadlines = %v, want slow clamped to 21600 and quick as set", got)
	}
	scheme := setupScheme()
	rec := record.NewFakeRecorder(10)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme, Recorder: rec}
	if _, err := r.reconcileA2ADispatcher(context.Background(), agent, []agentv1alpha1.AgentProfile{long, short}, false); err != nil {
		t.Fatal(err)
	}
	warned := false
	for len(rec.Events) > 0 {
		e := <-rec.Events
		warned = warned || (strings.Contains(e, reasonProfileDeadlineCapped) && strings.Contains(e, "slow") && strings.Contains(e, "clamped to 21600s"))
	}
	if !warned {
		t.Error("no warning for the clamped deadline")
	}
	// A refused profile has no tasks to cap, and gets no warning.
	refused := testAgentProfile(agent.Namespace, "platform", func(p *agentv1alpha1.AgentProfile) {
		p.Spec.Lifecycle.ActiveDeadlineSeconds = 86400
	})
	rec2 := record.NewFakeRecorder(10)
	r2 := &PlatformAgentReconciler{Client: c, Scheme: scheme, Recorder: rec2}
	if _, err := r2.reconcileA2ADispatcher(context.Background(), agent, []agentv1alpha1.AgentProfile{refused}, false); err != nil {
		t.Fatal(err)
	}
	for len(rec2.Events) > 0 {
		if e := <-rec2.Events; strings.Contains(e, reasonProfileDeadlineCapped) {
			t.Errorf("a refused profile was warned about its deadline: %s", e)
		}
	}
	env := map[string]string{}
	for _, e := range buildA2ACalloutDeployment(agent, "").Spec.Template.Spec.Containers[0].Env {
		env[e.Name] = e.Value
	}
	if env["A2A_TASK_CREDENTIAL_MAX_LIFETIME"] != "6h0m0s" {
		t.Errorf("the callout's credential lifetime = %q, want the cap", env["A2A_TASK_CREDENTIAL_MAX_LIFETIME"])
	}
	// Lowered on the CR, the clamp and the callout move together.
	lowered := agent.DeepCopy()
	lowered.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{Profiles: &agentv1alpha1.ProfileTuning{MaxTaskDeadlineSeconds: ptr.To(900)}}}
	cfg, _ = desiredDispatchConfig(lowered, []agentv1alpha1.AgentProfile{short})
	env = map[string]string{}
	for _, e := range buildA2ACalloutDeployment(lowered, "").Spec.Template.Spec.Containers[0].Env {
		env[e.Name] = e.Value
	}
	lifetime, err := time.ParseDuration(env["A2A_TASK_CREDENTIAL_MAX_LIFETIME"])
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Profiles[0].ActiveDeadlineSeconds != 900 || lifetime != 900*time.Second {
		t.Errorf("lowered to 900s: the clamp gives %ds and the callout %s; they must be the same bound", cfg.Profiles[0].ActiveDeadlineSeconds, lifetime)
	}
}

// With no dispatcher image, a next install renders exactly what it did
// before the dispatcher existed: no dispatcher principal, no durable grants
// on the operator, TASKS sized for sessions alone, no dispatcher objects,
// and no durable syncs from the AgentProfile controller.
func TestDefaultOffRendersNoDispatcherAndNoGrantsForIt(t *testing.T) {
	t.Setenv(a2aDispatcherImageEnvVar, "")
	withOperatorBusPrincipal(t)
	agent := a2aTestAgent()
	doc, err := renderA2AAuthMap(agent, []agentv1alpha1.AgentProfile{testAgentProfile(agent.Namespace, "tse")})
	if err != nil {
		t.Fatal(err)
	}
	for _, id := range doc.Identities {
		if id.User == a2aDispatcherUser {
			t.Error("the identity map has a dispatcher with the dispatcher off")
		}
		if id.User == a2aOperatorBusUser {
			for _, p := range id.Grants.Publish {
				if strings.Contains(p, "CONSUMER") {
					t.Errorf("the operator holds %s with the dispatcher off", p)
				}
			}
		}
	}
	if got, want := a2aTasksMaxConsumers(agent), max(a2aTasksConsumerBudget(agent), a2aTasksMaxConsumersFloor); got != want {
		t.Errorf("a fresh TASKS gets max_consumers %d with the dispatcher off, want %d", got, want)
	}
	scheme := setupScheme()
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(agent, chartProfileNamespace(agent)).WithInterceptorFuncs(fakeServerSideApplyInterceptors()).Build()
	r := &PlatformAgentReconciler{Client: c, Scheme: scheme}
	if _, err := r.reconcileA2ADispatcher(context.Background(), agent, nil, true); err != nil {
		t.Fatal(err)
	}
	for _, obj := range []client.Object{&appsv1.Deployment{}, &corev1.ServiceAccount{}, &corev1.ConfigMap{}} {
		name := a2aDispatcherName(agent)
		if _, isCM := obj.(*corev1.ConfigMap); isCM {
			name = a2aDispatchConfigName(agent)
		}
		if err := c.Get(context.Background(), types.NamespacedName{Namespace: agent.Namespace, Name: name}, obj); !apierrors.IsNotFound(err) {
			t.Errorf("%T rendered with the dispatcher off: %v", obj, err)
		}
	}
	p := testAgentProfile(agent.Namespace, "tse")
	h := newProfileHarness(t, agent, &p)
	h.reconcile(agent.Namespace, "tse")
	if len(h.dir.durables) != 0 {
		t.Errorf("durables synced with the dispatcher off: %+v", h.dir.durables)
	}
}

// Deleting the PlatformAgent removes the dispatcher's pod-read Role and
// binding in the profile namespace, which nothing owns: left behind, a later
// ServiceAccount of the dispatcher's name would inherit pod read there. A
// pair under that name without this agent's labels is someone else's, and
// stays.
func TestHandleDeletionRemovesTheDispatchersPodReadRBAC(t *testing.T) {
	for _, own := range []bool{true, false} {
		scheme := setupScheme()
		agent := a2aTestAgent()
		agent.Finalizers = []string{platformAgentFinalizer}
		now := metav1.Now()
		agent.DeletionTimestamp = &now
		role, binding := buildA2ADispatcherPodsRole(agent), buildA2ADispatcherPodsRoleBinding(agent)
		if own {
			withCommonLabels(role, agent)
			withCommonLabels(binding, agent)
		} else {
			role.Labels = map[string]string{labelInstance: "foreign-agent"}
			binding.Labels = map[string]string{labelInstance: "foreign-agent"}
		}
		cl := fake.NewClientBuilder().WithScheme(scheme).
			WithObjects(agent.DeepCopy(), role, binding).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
		ctx := context.Background()
		current := &agentv1alpha1.PlatformAgent{}
		if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name}, current); err != nil {
			t.Fatal(err)
		}
		if _, err := r.handleDeletion(ctx, current); err != nil {
			t.Fatalf("handleDeletion (own=%v): %v", own, err)
		}
		key := types.NamespacedName{Namespace: profileNamespaceName(agent), Name: a2aDispatcherName(agent)}
		for _, obj := range []client.Object{&rbacv1.Role{}, &rbacv1.RoleBinding{}} {
			err := cl.Get(ctx, key, obj)
			switch {
			case own && !apierrors.IsNotFound(err):
				t.Errorf("this agent's dispatcher pod-read %T survived deletion: %v", obj, err)
			case !own && err != nil:
				t.Errorf("a foreign %T under the dispatcher's name was deleted: %v", obj, err)
			}
		}
	}
}
