package controller

import (
	"context"
	"reflect"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

func gatewayLeaseKey(agent *agentv1alpha1.PlatformAgent) types.NamespacedName {
	return types.NamespacedName{Namespace: agent.Namespace, Name: agent.Name + "-a2a-gateway"}
}

// TestTheGatewayRoleGrantsOnlyGetAndUpdateOnItsLease: the gateway reads and
// renews its one Lease and can do nothing else with Leases. RBAC can't scope
// create to a name, so a create grant would be a grant on every Lease name in
// the namespace; the operator creates it instead.
func TestTheGatewayRoleGrantsOnlyGetAndUpdateOnItsLease(t *testing.T) {
	agent := a2aTestAgent()
	var leaseRules int
	for _, rule := range buildA2AGatewayRole(agent).Rules {
		for _, group := range rule.APIGroups {
			if group != "coordination.k8s.io" {
				continue
			}
			leaseRules++
			if !reflect.DeepEqual(rule.Resources, []string{"leases"}) ||
				!reflect.DeepEqual(rule.ResourceNames, []string{"test-agent-a2a-gateway"}) ||
				!reflect.DeepEqual(rule.Verbs, []string{"get", "update"}) {
				t.Errorf("lease rule = %+v, want get and update on test-agent-a2a-gateway alone", rule)
			}
		}
	}
	if leaseRules != 1 {
		t.Fatalf("%d coordination.k8s.io rules, want exactly one", leaseRules)
	}
}

// TestTheGatewayIsGivenItsLeaseAndPodName: the gateway campaigns only when
// A2A_GATEWAY_LEASE names the Lease, and its identity is its own pod's name
// from the downward API, so a container restarting in its pod re-takes its
// lease at once.
func TestTheGatewayIsGivenItsLeaseAndPodName(t *testing.T) {
	agent := a2aTestAgent()
	dep := buildA2AGatewayDeployment(agent)
	env := map[string]string{}
	var podName bool
	for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
		env[e.Name] = e.Value
		if e.Name == "POD_NAME" {
			podName = e.ValueFrom != nil && e.ValueFrom.FieldRef != nil && e.ValueFrom.FieldRef.FieldPath == "metadata.name"
		}
	}
	if env["A2A_GATEWAY_LEASE"] != "test-agent-a2a-gateway" {
		t.Errorf("A2A_GATEWAY_LEASE = %q, want the Lease's name", env["A2A_GATEWAY_LEASE"])
	}
	if !podName {
		t.Error("POD_NAME isn't the pod's metadata.name from the downward API")
	}
	// One replica and Recreate: no standby exists until the replicas guard
	// (docs/designs/gateway-replicas.md, "Operator changes") lands.
	if ptr.Deref(dep.Spec.Replicas, 0) != 1 || dep.Spec.Strategy.Type != "Recreate" {
		t.Errorf("replicas %d, strategy %s; want one replica with Recreate", ptr.Deref(dep.Spec.Replicas, 0), dep.Spec.Strategy.Type)
	}
}

func leaseReconciler(t *testing.T, objs ...client.Object) (*PlatformAgentReconciler, client.Client) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl
}

// TestTheOperatorCreatesTheGatewayLease: a missing Lease, never seen, is the
// first install: created at once, owned by the PlatformAgent so it goes with
// it, and labelled so the manager's cache sees it.
func TestTheOperatorCreatesTheGatewayLease(t *testing.T) {
	agent := a2aTestAgent()
	agent.UID = "agent-uid"
	r, cl := leaseReconciler(t, agent)
	wait, err := r.ensureA2AGatewayLease(context.Background(), agent)
	if err != nil || wait != 0 {
		t.Fatalf("ensure = %s, %v; want created at once", wait, err)
	}
	lease := &coordinationv1.Lease{}
	if err := cl.Get(context.Background(), gatewayLeaseKey(agent), lease); err != nil {
		t.Fatalf("the Lease wasn't created: %v", err)
	}
	if len(lease.OwnerReferences) != 1 || lease.OwnerReferences[0].UID != "agent-uid" || !ptr.Deref(lease.OwnerReferences[0].Controller, false) {
		t.Errorf("owner references %+v, want the PlatformAgent as controller", lease.OwnerReferences)
	}
	if lease.Labels[a2aGatewayLeaseLabel] != "true" {
		t.Errorf("labels %v, want %s", lease.Labels, a2aGatewayLeaseLabel)
	}
	if lease.Spec.HolderIdentity != nil {
		t.Errorf("the operator wrote a holder: %q", *lease.Spec.HolderIdentity)
	}
}

// TestTheOperatorLeavesALiveLeaseAlone: the Lease's spec is the holder's.
func TestTheOperatorLeavesALiveLeaseAlone(t *testing.T) {
	agent := a2aTestAgent()
	held := buildA2AGatewayLease(agent)
	held.Spec.HolderIdentity = ptr.To("test-agent-a2a-gateway-7d9f-abcde")
	held.Spec.LeaseDurationSeconds = ptr.To(int32(20))
	r, cl := leaseReconciler(t, agent, held)
	if wait, err := r.ensureA2AGatewayLease(context.Background(), agent); err != nil || wait != 0 {
		t.Fatalf("ensure = %s, %v", wait, err)
	}
	lease := &coordinationv1.Lease{}
	if err := cl.Get(context.Background(), gatewayLeaseKey(agent), lease); err != nil {
		t.Fatal(err)
	}
	if ptr.Deref(lease.Spec.HolderIdentity, "") != "test-agent-a2a-gateway-7d9f-abcde" || lease.ResourceVersion != held.ResourceVersion && held.ResourceVersion != "" {
		t.Errorf("the live Lease was touched: holder %q, resourceVersion %s", ptr.Deref(lease.Spec.HolderIdentity, ""), lease.ResourceVersion)
	}
}

// TestADeletedLeaseIsRecreatedOnlyAfterTheHold: a Lease the operator saw and
// then found gone was deleted. Recreated with no holder at once, a standby
// could take it while the old leader still acts, so the operator waits one
// lease duration from when it saw it gone.
func TestADeletedLeaseIsRecreatedOnlyAfterTheHold(t *testing.T) {
	agent := a2aTestAgent()
	r, cl := leaseReconciler(t, agent)
	r.gatewayLeaseHold = 300 * time.Millisecond
	ctx := context.Background()
	if _, err := r.ensureA2AGatewayLease(ctx, agent); err != nil {
		t.Fatal(err)
	}
	if err := cl.Delete(ctx, buildA2AGatewayLease(agent)); err != nil {
		t.Fatal(err)
	}

	wait, err := r.ensureA2AGatewayLease(ctx, agent)
	if err != nil || wait <= 0 || wait > r.gatewayLeaseHold {
		t.Fatalf("ensure right after the deletion = %s, %v; want a wait of at most the hold", wait, err)
	}
	if err := cl.Get(ctx, gatewayLeaseKey(agent), &coordinationv1.Lease{}); !errors.IsNotFound(err) {
		t.Fatalf("the Lease was recreated inside the hold: %v", err)
	}
	// A second pass inside the hold waits for what's left, from the first
	// sighting, not a fresh hold.
	time.Sleep(r.gatewayLeaseHold / 2)
	if again, _ := r.ensureA2AGatewayLease(ctx, agent); again <= 0 || again >= wait {
		t.Fatalf("second wait %s, want less than the first %s", again, wait)
	}

	time.Sleep(r.gatewayLeaseHold)
	if wait, err := r.ensureA2AGatewayLease(ctx, agent); err != nil || wait != 0 {
		t.Fatalf("ensure after the hold = %s, %v; want created", wait, err)
	}
	if err := cl.Get(ctx, gatewayLeaseKey(agent), &coordinationv1.Lease{}); err != nil {
		t.Fatalf("the Lease wasn't recreated after the hold: %v", err)
	}
}

// TestForgettingAnAgentForgetsItsLease: a CR deleted and created again
// under the same name is a first install for its Lease.
func TestForgettingAnAgentForgetsItsLease(t *testing.T) {
	agent := a2aTestAgent()
	r, _ := leaseReconciler(t, agent)
	key := gatewayLeaseKey(agent)
	r.gatewayLeaseSeen.Store(key, true)
	r.gatewayLeaseGoneAt.Store(key, time.Now())
	r.forgetA2AGatewayLease(agent)
	if _, ok := r.gatewayLeaseSeen.Load(key); ok {
		t.Error("still seen after the agent was forgotten")
	}
	if _, ok := r.gatewayLeaseGoneAt.Load(key); ok {
		t.Error("still held after the agent was forgotten")
	}
}

// TestTheGatewayLeaseFollowsTheMode: a reconcile under mode next creates the
// Lease, and the flip to today removes it with the rest of the stack.
func TestTheGatewayLeaseFollowsTheMode(t *testing.T) {
	scheme := setupScheme()
	agent := a2aTestAgent()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(agent).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	r := &PlatformAgentReconciler{Client: cl, Scheme: scheme}
	req := ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
	ctx := context.Background()
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	if err := cl.Get(ctx, gatewayLeaseKey(agent), &coordinationv1.Lease{}); err != nil {
		t.Fatalf("no gateway Lease under mode next: %v", err)
	}

	live := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, live); err != nil {
		t.Fatal(err)
	}
	live.Spec.Mode = ptr.To("today")
	if err := cl.Update(ctx, live); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, req); err != nil {
		t.Fatalf("Reconcile under today: %v", err)
	}
	if err := cl.Get(ctx, gatewayLeaseKey(agent), &coordinationv1.Lease{}); !errors.IsNotFound(err) {
		t.Fatalf("the gateway Lease outlived the flip to today: %v", err)
	}
}

// TestTheCacheHoldsOnlyTheGatewaysLeases: the Lease informer selects the
// label the gateway's Lease carries, so node and controller Leases stay out
// of the operator's memory and its watch.
func TestTheCacheHoldsOnlyTheGatewaysLeases(t *testing.T) {
	opts := ManagerCacheOptions()
	var sel labels.Selector
	for obj, by := range opts.ByObject {
		if _, ok := obj.(*coordinationv1.Lease); ok {
			sel = by.Label
		}
	}
	if sel == nil {
		t.Fatal("no cache scope for Leases")
	}
	if !sel.Matches(labels.Set(buildA2AGatewayLease(a2aTestAgent()).Labels)) {
		t.Error("the gateway's own Lease falls outside the cache's scope")
	}
	if sel.Matches(labels.Set{}) {
		t.Error("an unlabelled Lease (a node's, a controller's) is cached")
	}
}
