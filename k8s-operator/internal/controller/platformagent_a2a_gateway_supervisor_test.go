package controller

import (
	"context"
	"reflect"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
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

// The profiles reach the gateway's block and no other, and they move the
// config hash, so binding or unbinding a profile rolls the bus.
func TestTheProfilesSupervisorDenyRidesTheGatewayBlockAndTheHash(t *testing.T) {
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
	without := a2aConfigRolloutHash(agent, creds, keys, "fp", nil)
	with := a2aConfigRolloutHash(agent, creds, keys, "fp", []string{"tse"})
	if without == with {
		t.Fatal("a bound profile doesn't move the config hash, so the bus wouldn't pick up its deny")
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

// Binding a profile moves the bus's config hash, which is what rolls the bus
// onto a config carrying the new deny: the pass that sees the profile finds
// the desired hash apart from the live one (and holds the bus for the callout
// rolling the new identity map, a2aBusWaitsForCallout, which it does only
// when the two differ).
func TestBindingAProfileRollsTheBus(t *testing.T) {
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(setupScheme()).
		WithObjects(agent, chartProfileNamespace(agent)).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: setupScheme()}
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
	pass()
	if st := pass(); st.busHeld {
		t.Fatal("with nothing changed the bus's desired config hash moved")
	}
	profile := testAgentProfile(agent.Namespace, "tse")
	if err := cl.Create(ctx, &profile); err != nil {
		t.Fatal(err)
	}
	if st := pass(); !st.busHeld {
		t.Fatal("a bound profile left the bus's desired config hash where it was, so the bus wouldn't roll onto its deny")
	}
}
