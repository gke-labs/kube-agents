package controller

import (
	"context"
	"errors"
	natsserver "github.com/nats-io/nats-server/v2/server"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// The deny covers each profile by name, sorted and once, and skips a name
// that isn't one subject token.
func TestGatewayProfileSupervisorDenyNamesEachProfile(t *testing.T) {
	got := gatewayProfileSupervisorDeny([]string{"tse", "cluster", "tse", "", "a.b", "*", "x y"})
	want := []string{"a2a.tasks.cluster.*.supervisor", "a2a.tasks.tse.*.supervisor"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("deny = %v, want %v", got, want)
	}
	if got := gatewayProfileSupervisorDeny(nil); len(got) != 0 {
		t.Fatalf("no profiles: %v", got)
	}
}

// The profiles reach the gateway's block and no other.
func TestTheProfilesSupervisorDenyRidesTheGatewayBlockAlone(t *testing.T) {
	agent := a2aTestAgent()
	creds := a2aFullCreds("a", "1")
	keys := a2aTestCalloutKeys(t)
	conf := string(buildA2ANATSConfigSecret(agent, creds, keys, []string{"tse"}).Data["nats.conf"])
	start := strings.Index(conf, "user: gateway\n")
	if start < 0 {
		t.Fatal("no gateway user block in nats.conf")
	}
	gw := conf[start:]
	if next := strings.Index(gw[1:], "user: "); next > 0 {
		gw = gw[:next+1]
	}
	if !strings.Contains(gw, "a2a.tasks.tse.*.supervisor") || !strings.Contains(gw, "deny") {
		t.Fatalf("the gateway block carries no deny on tse's supervisor subjects:\n%s", gw)
	}
	if strings.Count(conf, "a2a.tasks.tse.*.supervisor") != 1 {
		t.Fatalf("the deny appears %d times in nats.conf, want once (the gateway's)", strings.Count(conf, "a2a.tasks.tse.*.supervisor"))
	}
}

// On a real nats-server running the rendered config: the gateway is refused a
// profile's supervisor subject and still publishes a session's, and the
// dispatcher's subject stays its own.
func TestTheGatewayIsRefusedAProfilesSupervisorOnARealServer(t *testing.T) {
	creds := a2aFullCreds("a", "1")
	conf := string(buildA2ANATSConfigSecret(a2aTestAgent(), creds, a2aTestCalloutKeys(t), []string{"tse"}).Data["nats.conf"])
	s, log := a2aStartRenderedServer(t, conf)
	a2aProvisionLikeTheScript(t, s.ClientURL(), string(creds.Data["seed-password"]))
	_, gw := a2aConnectAs(t, s.ClientURL(), "gateway", string(creds.Data["gateway-password"]))
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	const (
		profileSupervisor  = "a2a.tasks.tse.task-1.supervisor"
		sessionSupervisor  = "a2a.tasks.chat-heron-3aa270a3.task-2.supervisor"
		platformSupervisor = "a2a.tasks.platform.task-3.supervisor"
		// A session whose name starts like the profile's is still one token,
		// so the deny doesn't reach it.
		prefixedSupervisor = "a2a.tasks.tse-heron-1.task-4.supervisor"
	)
	for _, subj := range []string{sessionSupervisor, platformSupervisor, prefixedSupervisor} {
		if _, err := gw.Publish(ctx, subj, []byte(`{"kind":"status-update"}`)); err != nil {
			t.Errorf("gateway publish on %s: %v, want it stored", subj, err)
		}
	}
	pubCtx, pubCancel := context.WithTimeout(ctx, 3*time.Second)
	defer pubCancel()
	if _, err := gw.Publish(pubCtx, profileSupervisor, []byte(`{"kind":"status-update"}`)); err == nil {
		t.Fatalf("gateway publish on %s stored, want refused", profileSupervisor)
	}
	if !log.refusedPublish("gateway", profileSupervisor) {
		t.Fatalf("no publish violation for gateway on %s; server log: %q", profileSupervisor, log.publishViolations("gateway"))
	}
}

// The reconcile renders each bound profile into the gateway's deny.
func TestTheReconcileDeniesTheGatewayEachBoundProfilesSupervisor(t *testing.T) {
	agent := a2aTestAgent()
	profile := testAgentProfile(agent.Namespace, "tse")
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent, &profile, chartProfileNamespace(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	var secret corev1.Secret
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}, &secret); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(secret.Data["nats.conf"]), "a2a.tasks.tse.*.supervisor") {
		t.Fatal("the rendered nats.conf carries no deny on the bound profile's supervisor subjects")
	}
}

// A profile removed while the dispatcher still drains its tasks keeps the
// gateway's deny, as it keeps the dispatcher's grant.
func TestADrainingProfileKeepsTheGatewaysDeny(t *testing.T) {
	agent := a2aTestAgent()
	live := testAgentProfile(agent.Namespace, "tse")
	applied, err := buildA2ADispatchConfigMap(agent, []agentv1alpha1.AgentProfile{live, testAgentProfile(agent.Namespace, "old")}, nil)
	if err != nil {
		t.Fatal(err)
	}
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent, &live, chartProfileNamespace(agent), applied).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
	ctx := context.Background()
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	var secret corev1.Secret
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}, &secret); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(secret.Data["nats.conf"]), "a2a.tasks.old.*.supervisor") {
		t.Fatal("the removed, draining profile lost the gateway's deny on its supervisor subjects")
	}
}

// Binding a profile changes the config Secret but not the pod template, on
// one server and on a cluster alike, so no server restarts and no cluster
// rolls. The pass records a pending reload on the Secret, with a baseline per
// server; it clears only once every server's config_load_time has moved.
func TestBindingAProfileReloadsTheBusInsteadOfRollingIt(t *testing.T) {
	for _, agent := range []*agentv1alpha1.PlatformAgent{a2aSingleServerTestAgent(), a2aTestAgentWithBusReplicas(3)} {
		servers := int(a2aNATSReplicas(agent))
		t.Run(strconv.Itoa(servers)+" servers", func(t *testing.T) {
			cl := fake.NewClientBuilder().
				WithScheme(setupScheme()).
				WithObjects(agent, chartProfileNamespace(agent)).
				WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
				WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
				Build()
			loaded := map[string]string{}
			r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(),
				busConfigLoadTime: func(_ context.Context, _ *agentv1alpha1.PlatformAgent, pod *corev1.Pod) (string, error) {
					return loaded[pod.Name], nil
				}}
			ctx := context.Background()
			theCalloutIsServing(t, ctx, cl, r, agent)
			pass := func() a2aProvisionState {
				t.Helper()
				st, err := r.reconcileA2A(ctx, agent, homeChannelRecord{})
				if err != nil {
					t.Fatal(err)
				}
				return st
			}
			read := func() (*corev1.Secret, *appsv1.StatefulSet) {
				t.Helper()
				var secret corev1.Secret
				if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}, &secret); err != nil {
					t.Fatal(err)
				}
				var sts appsv1.StatefulSet
				if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, &sts); err != nil {
					t.Fatal(err)
				}
				return &secret, &sts
			}
			pass()
			pass()
			_, sts := read()
			hashBefore := sts.Spec.Template.Annotations[a2aConfigHashAnnotation]
			var names []string
			for i := range servers {
				name := sts.Name + "-" + strconv.Itoa(i)
				names = append(names, name)
				loaded[name] = "2026-11-09T22:00:00Z"
				if err := cl.Create(ctx, &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
					Name: name, Namespace: agent.Namespace, UID: types.UID("uid-" + name),
					OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: sts.Name, UID: sts.UID, Controller: ptr.To(true)}},
				}}); err != nil {
					t.Fatal(err)
				}
			}
			profile := testAgentProfile(agent.Namespace, "tse")
			if err := cl.Create(ctx, &profile); err != nil {
				t.Fatal(err)
			}
			st := pass()
			if st.busHeld {
				t.Fatal("a bound profile held the bus for a roll")
			}
			secret, sts := read()
			if sts.Spec.Template.Annotations[a2aConfigHashAnnotation] != hashBefore {
				t.Fatal("a bound profile moved the pod template's hash, so the bus would roll")
			}
			if got, ok := sts.Spec.Template.Annotations[a2aConfigProfilesAnnotation]; ok {
				t.Fatalf("the pod template lists profiles %q; a profile change would roll it", got)
			}
			if !strings.Contains(string(secret.Data["nats.conf"]), "a2a.tasks.tse.*.supervisor") {
				t.Fatal("the config Secret carries no deny for the bound profile")
			}
			state, pending := readA2ABusReloadState(secret.Annotations, sts.Name)
			if !pending || len(state.servers) != servers {
				t.Fatalf("no pending reload with a baseline per server recorded on the Secret: %v", secret.Annotations)
			}
			for _, name := range names {
				if state.servers[name].Loaded != "2026-11-09T22:00:00Z" {
					t.Fatalf("baseline for %s = %q, want the load time read before the write", name, state.servers[name].Loaded)
				}
			}
			if st.busReloadRequeue <= 0 {
				t.Fatal("a pending reload asked for no recheck")
			}
			// Every server but the last has reloaded: still pending.
			for _, name := range names[:len(names)-1] {
				loaded[name] = "2026-11-09T22:00:41Z"
			}
			pass()
			if secret, _ := read(); secret.Annotations[a2aBusReloadRequestedAnnotation] == "" {
				t.Fatal("the pending reload was dropped before every server's config_load_time moved")
			}
			loaded[names[len(names)-1]] = "2026-11-09T22:00:42Z"
			if st := pass(); st.busReloadRequeue != 0 {
				t.Fatal("a landed reload still asks for a recheck")
			}
			if secret, _ := read(); secret.Annotations[a2aBusReloadRequestedAnnotation] != "" {
				t.Fatalf("a landed reload left its pending record: %v", secret.Annotations)
			}
		})
	}
}

// A profile the controller refuses has no dispatcher grant, so it gets no
// gateway deny: the supervisor grants, the one part of the config a profile
// changes, stay as they were.
func TestARefusedProfileLeavesTheSupervisorGrantsAlone(t *testing.T) {
	agent := a2aTestAgent()
	live := testAgentProfile(agent.Namespace, "tse")
	refused := testAgentProfile(agent.Namespace, "rogue")
	refused.Spec.Bus.PublishTopics = []string{"agent.tse.findings"}
	confFor := func(objs ...client.Object) string {
		t.Helper()
		cl := fake.NewClientBuilder().
			WithScheme(setupScheme()).
			WithObjects(append([]client.Object{agent.DeepCopy(), chartProfileNamespace(agent)}, objs...)...).
			WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
			WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
			Build()
		r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
		ctx := context.Background()
		if _, err := r.reconcileA2A(ctx, agent.DeepCopy(), homeChannelRecord{}); err != nil {
			t.Fatal(err)
		}
		var secret corev1.Secret
		if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}, &secret); err != nil {
			t.Fatal(err)
		}
		return string(secret.Data["nats.conf"])
	}
	without := confFor(live.DeepCopy())
	with := confFor(live.DeepCopy(), refused.DeepCopy())
	if !strings.Contains(with, "a2a.tasks.tse.*.supervisor") {
		t.Fatal("the admitted profile lost its deny")
	}
	if strings.Contains(with, "a2a.tasks.rogue.*.supervisor") {
		t.Fatal("a refused profile got a gateway deny")
	}
	// Each fresh client mints its own credentials and callout keys, so only
	// the supervisor grants are compared.
	supervisorLines := func(conf string) []string {
		var out []string
		for _, l := range strings.Split(conf, "\n") {
			if strings.Contains(l, "supervisor") {
				out = append(out, strings.TrimSpace(l))
			}
		}
		return out
	}
	if !reflect.DeepEqual(supervisorLines(with), supervisorLines(without)) {
		t.Fatalf("a refused profile changed the bus's supervisor grants:\n%v\nvs\n%v", supervisorLines(with), supervisorLines(without))
	}
}

// On a real nats-server, a profile's deny delivered by a config reload, with
// no restart, refuses the gateway on a connection it opened before the
// reload, and config_load_time moves.
func TestAReloadedDenyRefusesTheGatewayWithoutARestart(t *testing.T) {
	agent := a2aTestAgent()
	creds := a2aFullCreds("a", "1")
	keys := a2aTestCalloutKeys(t)
	dir := t.TempDir()
	path := filepath.Join(dir, "nats.conf")
	// The test-only overrides a2aStartRenderedServerAt makes, held here so
	// the reload can make the same ones: nats-server refuses a reload that
	// moves the port, the store or the file it was started from.
	load := func(profiles []string) *natsserver.Options {
		t.Helper()
		conf := string(buildA2ANATSConfigSecret(agent, creds, keys, profiles).Data["nats.conf"])
		if err := os.WriteFile(path, []byte(a2aWithTestTLS(t, a2aSingleServerTestConf(t, conf))), 0o600); err != nil {
			t.Fatal(err)
		}
		opts, err := natsserver.ProcessConfigFile(path)
		if err != nil {
			t.Fatal(err)
		}
		opts.Host = "127.0.0.1"
		opts.HTTPPort = 0
		opts.Websocket.Port = 0
		opts.StoreDir = filepath.Join(dir, "store")
		opts.NoSigs = true
		opts.PidFile = filepath.Join(dir, "nats.pid")
		return opts
	}
	opts := load(nil)
	opts.Port = -1
	s, err := natsserver.NewServer(opts)
	if err != nil {
		t.Fatal(err)
	}
	log := &a2aServerLog{}
	s.SetLoggerV2(log, false, false, false)
	go s.Start()
	t.Cleanup(func() {
		s.Shutdown()
		s.WaitForShutdown()
	})
	if !s.ReadyForConnections(10 * time.Second) {
		t.Fatal("embedded nats-server did not come up")
	}
	a2aProvisionLikeTheScript(t, s.ClientURL(), string(creds.Data["seed-password"]))
	_, gw := a2aConnectAs(t, s.ClientURL(), "gateway", string(creds.Data["gateway-password"]))
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	const profileSupervisor = "a2a.tasks.tse.task-1.supervisor"
	if _, err := gw.Publish(ctx, profileSupervisor, []byte(`{"kind":"status-update"}`)); err != nil {
		t.Fatalf("before the profile, gateway publish on %s: %v, want it stored", profileSupervisor, err)
	}
	varz, err := s.Varz(nil)
	if err != nil {
		t.Fatal(err)
	}
	before := varz.ConfigLoadTime

	reloaded := load([]string{"tse"})
	reloaded.Port = s.Addr().(*net.TCPAddr).Port
	time.Sleep(10 * time.Millisecond)
	if err := s.ReloadOptions(reloaded); err != nil {
		t.Fatalf("nats-server refused to reload the config with the profile's deny: %v", err)
	}
	if varz, err = s.Varz(nil); err != nil {
		t.Fatal(err)
	}
	if !varz.ConfigLoadTime.After(before) {
		t.Fatalf("config_load_time %s didn't move off %s after the reload", varz.ConfigLoadTime, before)
	}
	pubCtx, pubCancel := context.WithTimeout(ctx, 3*time.Second)
	defer pubCancel()
	if _, err := gw.Publish(pubCtx, profileSupervisor, []byte(`{"kind":"status-update"}`)); err == nil {
		t.Fatalf("after the reload, gateway publish on %s stored, want refused", profileSupervisor)
	}
	if !log.refusedPublish("gateway", profileSupervisor) {
		t.Fatalf("no publish violation for gateway on %s after the reload; server log: %q", profileSupervisor, log.publishViolations("gateway"))
	}
}

// A restart owed at the bound happens before the pass applies the config, so
// a delete that fails leaves the pending record on the Secret for the next
// pass rather than dropping it with the restart never done.
func TestAFailedRestartKeepsThePendingReload(t *testing.T) {
	agent := a2aSingleServerTestAgent()
	refuseDeletes := false
	funcs := fakeServerSideApplyInterceptors()
	funcs.Delete = func(ctx context.Context, cl client.WithWatch, obj client.Object, opts ...client.DeleteOption) error {
		if _, ok := obj.(*corev1.Pod); ok && refuseDeletes {
			return errors.New("the API server refused the delete")
		}
		return cl.Delete(ctx, obj, opts...)
	}
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent, chartProfileNamespace(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(funcs).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme(),
		busConfigLoadTime: func(context.Context, *agentv1alpha1.PlatformAgent, *corev1.Pod) (string, error) { return "t0", nil }}
	ctx := context.Background()
	theCalloutIsServing(t, ctx, cl, r, agent)
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	var sts appsv1.StatefulSet
	if err := cl.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSName(agent)}, &sts); err != nil {
		t.Fatal(err)
	}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{
		Name: sts.Name + a2aNATSPodOrdinalSuffix, Namespace: agent.Namespace,
		OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "StatefulSet", Name: sts.Name, UID: sts.UID, Controller: ptr.To(true)}},
	}}
	if err := cl.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	profile := testAgentProfile(agent.Namespace, "tse")
	if err := cl.Create(ctx, &profile); err != nil {
		t.Fatal(err)
	}
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	// The reload never lands: move its request past the bound.
	var secret corev1.Secret
	key := types.NamespacedName{Namespace: agent.Namespace, Name: a2aNATSConfigSecretName(agent)}
	if err := cl.Get(ctx, key, &secret); err != nil {
		t.Fatal(err)
	}
	if secret.Annotations[a2aBusReloadRequestedAnnotation] == "" {
		t.Fatal("no pending reload after binding a profile")
	}
	secret.Annotations[a2aBusReloadRequestedAnnotation] = time.Now().Add(-2 * a2aBusReloadBound).UTC().Format(time.RFC3339Nano)
	if err := cl.Update(ctx, &secret); err != nil {
		t.Fatal(err)
	}
	refuseDeletes = true
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err == nil {
		t.Fatal("a refused restart didn't fail the pass")
	}
	if err := cl.Get(ctx, key, &secret); err != nil {
		t.Fatal(err)
	}
	if secret.Annotations[a2aBusReloadRequestedAnnotation] == "" {
		t.Fatal("the pending reload was dropped though its restart failed")
	}
	refuseDeletes = false
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, client.ObjectKeyFromObject(pod), &corev1.Pod{}); err == nil {
		t.Fatal("the bus pod wasn't restarted once the delete went through")
	}
	if err := cl.Get(ctx, key, &secret); err != nil {
		t.Fatal(err)
	}
	// Fail closed: the record stays until the restarted server is read on
	// the new config, so a profile held on it stays held meanwhile.
	if secret.Annotations[a2aBusReloadRequestedAnnotation] == "" {
		t.Fatal("the pending reload was dropped before the restarted server was read")
	}
	// The StatefulSet recreates it after the config write, by the API
	// server's clock (the live Secret's managedFields): a fresh pod mounts
	// the current Secret, so its first read confirms it.
	written := time.Now().Add(-time.Minute).Truncate(time.Second)
	r.busConfigWrittenAt = func(*corev1.Secret) (time.Time, bool) { return written, true }
	restarted := pod.DeepCopy()
	restarted.ResourceVersion, restarted.UID = "", "pod-2"
	restarted.CreationTimestamp = metav1.NewTime(written.Add(30 * time.Second))
	if err := cl.Create(ctx, restarted); err != nil {
		t.Fatal(err)
	}
	if _, err := r.reconcileA2A(ctx, agent, homeChannelRecord{}); err != nil {
		t.Fatal(err)
	}
	if err := cl.Get(ctx, key, &secret); err != nil {
		t.Fatal(err)
	}
	if secret.Annotations[a2aBusReloadRequestedAnnotation] != "" {
		t.Fatal("the pending reload outlived the restarted server's first read")
	}
}
