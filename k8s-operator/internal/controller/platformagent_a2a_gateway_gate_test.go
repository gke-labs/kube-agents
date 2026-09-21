package controller

// BusCredentialsReady, from the side that reads it.
//
// The condition reports that the auth callout is serving the identity map that
// every bus principal authenticates against. Until A6 nothing consumed it: the
// CRD reference said so in as many words, and a claim with no reader is a claim
// nobody notices going wrong. What reads it now is the gateway render, and only
// its CREATE - a gateway that already exists keeps reconciling through a
// callout outage, because withholding its updates would freeze its image and
// env at whatever the outage interrupted, and because the sessions it already
// spawned hang off its Deployment UID.

import (
	"context"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	ctrl "sigs.k8s.io/controller-runtime"

	agentv1alpha1 "github.com/gke-labs/kube-agents/k8s-operator/api/v1alpha1"
)

// reportCalloutServing puts the callout Deployment into the state its readiness
// probe reaches once it is answering: every replica ready, and ready on the
// CURRENT pod template. The fake client runs no Deployment controller, so
// without this a test install never gets past the gate.
func reportCalloutServing(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	dep := &appsv1.Deployment{}
	key := types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, key, dep); err != nil {
		t.Fatalf("get callout Deployment: %v", err)
	}
	replicas := int32(1)
	if dep.Spec.Replicas != nil {
		replicas = *dep.Spec.Replicas
	}
	dep.Status.Replicas = replicas
	dep.Status.ReadyReplicas = replicas
	dep.Status.UpdatedReplicas = replicas
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}
}

// letTheGatewayThrough is reportCalloutServing plus the two passes the gate
// costs: syncBusCredentialsReady is deferred, so the reconcile that observes a
// serving callout is the one that publishes the condition, and the NEXT one is
// the first to read it True. Tests that are about something other than the gate
// call this to get past it.
func letTheGatewayThrough(t *testing.T, ctx context.Context, cl client.Client, r *PlatformAgentReconciler, req ctrl.Request, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	reportCalloutServing(t, ctx, cl, agent)
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d after the callout came up: %v", i+1, err)
		}
	}
}

// busCredentialsAreReady sets BusCredentialsReady True on the in-memory CR,
// which is the one thing the gateway gate reads. It is for tests that drive
// reconcileA2A directly instead of going through Reconcile: syncBusCredentialsReady
// runs on the way out of Reconcile, so on that path no pass ever publishes the
// condition, the gate holds forever, and the gateway Deployment is never
// created. An assertion about a gateway that was never created passes on
// absence, which is the failure mode this helper exists to keep out of the
// suite -- so call it, and then assert the Deployment is there before
// asserting anything else about it.
func busCredentialsAreReady(agent *agentv1alpha1.PlatformAgent) {
	meta.SetStatusCondition(&agent.Status.Conditions, metav1.Condition{
		Type:    busCredentialsReadyCondition,
		Status:  metav1.ConditionTrue,
		Reason:  busCredsReasonServing,
		Message: "the auth callout is serving identity map <test>",
	})
}

// completeTheProvisionJob reports the A2A provision Job complete, which is what
// sets a2aProvisionState.done. The fake client runs no Job controller, so
// without this every A2A reconcile looks unprovisioned and requeues for that
// reason alone.
func completeTheProvisionJob(t *testing.T, ctx context.Context, cl client.Client, agent *agentv1alpha1.PlatformAgent) {
	t.Helper()
	// By the exact name the controller renders, not by matching the infix.
	// The name is a digest of the whole JobSpec, so a spec edit produces a
	// second Job rather than replacing the first - which is the whole point
	// of the digest - and a substring scan over the namespace would pick
	// whichever of them the List happened to return last. Completing the
	// wrong one leaves the reconcile under test reading an unprovisioned bus
	// while this helper reports success.
	name := buildA2AProvisionJob(agent).Name
	job := &batchv1.Job{}
	if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, job); err != nil {
		jobs := &batchv1.JobList{}
		if lerr := cl.List(ctx, jobs, client.InNamespace(agent.Namespace)); lerr != nil {
			t.Fatalf("get provision Job %s: %v (and listing Jobs failed: %v)", name, err, lerr)
		}
		var names []string
		for i := range jobs.Items {
			names = append(names, jobs.Items[i].Name)
		}
		t.Fatalf("no A2A provision Job named %s; the namespace holds %v. The render and this helper disagree about the digest, so completing anything here would be completing the wrong Job", name, names)
	}
	job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
	if err := cl.Status().Update(ctx, job); err != nil {
		t.Fatalf("update provision Job status: %v", err)
	}

	// Read it back and assert it, because the caller's assertion cannot.
	// Both arms of the requeue in Reconcile - provisioning unfinished, and
	// the gateway held - return the same 30s, so a caller measuring that
	// interval gets the number it wants whether or not this helper worked.
	// The comment at the call site says the measurement was taken with
	// provisioning complete; this is what makes that a checked precondition
	// rather than an assumption the test cannot see failing.
	check := &batchv1.Job{}
	if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, check); err != nil {
		t.Fatalf("re-read provision Job %s: %v", name, err)
	}
	for _, c := range check.Status.Conditions {
		if c.Type == batchv1.JobComplete && c.Status == corev1.ConditionTrue {
			return
		}
	}
	t.Fatalf("provision Job %s does not read back complete (%+v); every requeue measured after this would be the unprovisioned arm, at the same 30s", name, check.Status.Conditions)
}

// extra is for the one test that needs the rest of the install standing: the
// phase is decided from the agent gateway, the shell sandbox and the credential
// broker, none of which this file's other tests care about because none of them
// look at the phase.
func a2aGateTestReconciler(t *testing.T, agent *agentv1alpha1.PlatformAgent, extra ...client.Object) (*PlatformAgentReconciler, client.Client, ctrl.Request) {
	t.Helper()
	scheme := setupScheme()
	cl := fake.NewClientBuilder().
		WithScheme(scheme).
		WithObjects(append([]client.Object{agent, sandboxKeysSecret(agent)}, extra...)...).
		WithStatusSubresource(&agentv1alpha1.PlatformAgent{}).
		WithInterceptorFuncs(fakeServerSideApplyInterceptors()).
		Build()
	return &PlatformAgentReconciler{Client: cl, Scheme: scheme}, cl,
		ctrl.Request{NamespacedName: types.NamespacedName{Name: agent.Name, Namespace: agent.Namespace}}
}

// The gate itself: no gateway until the callout serves.
//
// The rest of the bus stack must still render - the gate is on the one
// component that dispatches work onto the bus, not on provisioning it. A gate
// that also withheld NATS would deadlock: the callout cannot become ready
// without a bus to attach to.
func TestTheGatewayIsWithheldUntilTheCalloutServes(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	for i := 0; i < 3; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}

	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Fatalf("gateway rendered while BusCredentialsReady is False (err=%v); it would dispatch onto a bus that refuses every session's connect", err)
	}

	// Everything the callout needs in order to become ready is already
	// there, or the gate is a deadlock rather than an ordering.
	for _, name := range []string{a2aNATSName(agent), a2aCalloutName(agent)} {
		if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, &appsv1.Deployment{}); err != nil {
			if err := cl.Get(ctx, types.NamespacedName{Name: name, Namespace: agent.Namespace}, &appsv1.StatefulSet{}); err != nil {
				t.Errorf("%s did not render while the gateway waits: %v", name, err)
			}
		}
	}

	// And the wait is reported rather than silent: the reconcile requeues,
	// so the gate converges on its own instead of waiting for an unrelated
	// event to wake the controller.
	//
	// Measured with the provision Job reported complete, which is the state
	// that makes this a real question. An incomplete Job requeues on its own
	// account, so a gate tested against one would requeue whether or not
	// anything watched it — and the install where the gate actually has to
	// carry the requeue is exactly the provisioned one, where the bus is up
	// and the callout is the only thing not serving yet.
	completeTheProvisionJob(t, ctx, cl, agent)
	res, err := r.Reconcile(ctx, req)
	if err != nil {
		t.Fatalf("Reconcile while held: %v", err)
	}
	// The interval, not merely non-zero. A provisioned, Ready install still
	// requeues for unrelated reasons — the telemetry re-probe is 15 minutes —
	// so `!= 0` would pass with the gate's own requeue deleted, and the gate
	// would converge a quarter of an hour late while the assertion stayed
	// green.
	if res.RequeueAfter != 30*time.Second {
		t.Errorf("a held gateway on a fully provisioned bus requeued after %s, want 30s; nothing else is watching the callout's readiness at that cadence", res.RequeueAfter)
	}

	letTheGatewayThrough(t, ctx, cl, r, req, agent)

	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); err != nil {
		t.Fatalf("gateway still withheld after the callout reported serving: %v", err)
	}
}

// The other half of "creation only": once the gateway exists, an unready
// callout must not stop it being reconciled.
//
// A callout that crash-loops after the gateway is up is an outage, and the
// operator's job during an outage is to keep converging the spec - not to pin
// the running gateway to whatever image and env the outage interrupted. This
// pins the distinction by moving a value the CR owns while the callout is down
// and requiring it to reach the live Deployment.
func TestARunningGatewayKeepsReconcilingThroughACalloutOutage(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent)
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}
	letTheGatewayThrough(t, ctx, cl, r, req, agent)

	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); err != nil {
		t.Fatalf("gateway did not render: %v", err)
	}

	// The outage: no replica serving, on the current template or any other.
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}, dep); err != nil {
		t.Fatalf("get callout: %v", err)
	}
	dep.Status.ReadyReplicas = 0
	dep.Status.UpdatedReplicas = 0
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}
	// Two passes to land it, for the same deferred-write reason as
	// letTheGatewayThrough: the condition is published on the way out. The
	// spec change below must arrive when the gate is genuinely reading False,
	// or this test passes on staleness rather than on the rule it is about.
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d after the callout went unready: %v", i+1, err)
		}
	}
	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if meta.IsStatusConditionTrue(fresh.Status.Conditions, busCredentialsReadyCondition) {
		t.Fatal("BusCredentialsReady is still true after the callout went unready; the rest of this test would prove nothing")
	}

	// A spec change lands during it.
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	sessions := 7
	fresh.Spec.Harness = &agentv1alpha1.HarnessSpec{Tuning: &agentv1alpha1.TuningSpec{MaxSessions: &sessions}}
	if err := cl.Update(ctx, fresh); err != nil {
		t.Fatalf("update agent: %v", err)
	}
	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d during the outage: %v", i+1, err)
		}
	}

	live := &appsv1.Deployment{}
	if err := cl.Get(ctx, gwKey, live); err != nil {
		t.Fatalf("a callout outage deleted the running gateway: %v", err)
	}
	var got string
	for _, e := range live.Spec.Template.Spec.Containers[0].Env {
		if e.Name == "A2A_MAX_SESSIONS" {
			got = e.Value
		}
	}
	if got != "7" {
		t.Errorf("A2A_MAX_SESSIONS = %q on the live gateway, want \"7\"; the gate froze a running gateway's spec instead of only withholding its creation", got)
	}
}

// What the gate costs a new install whose callout is only partly up, pinned
// because Risk & Rollout states it in prose and prose does not go red.
//
// The gate passes on BusCredentialsReady alone, and that condition is True only
// when every replica of the callout Deployment is ready AND on the current pod
// template. The Deployment is rendered at two. But the callout's replicas join
// a NATS queue group (a2a/authcallout/service.go, AuthQueueGroup) precisely so
// that exactly one of them answers each authorization request, so ONE ready
// replica already mints credentials for every session. The condition's
// all-replicas rule was written for a different question -- "is the callout as
// a whole serving the map this render names", where a half-rolled Deployment
// genuinely is not -- and the gate imports it wholesale.
//
// So a second replica that cannot schedule (node pressure, a namespace quota,
// an image pull that fails on one node) or a roll that wedges holds a NEW
// install's gateway for as long as that lasts, on a bus that would have
// authenticated every one of its sessions. Before the gate the install got a
// gateway and working sessions. The hold is not silent -- BusCredentialsReady
// is False on the CR and its message carries the 1-of-2 -- but nothing ties it
// to the gateway's absence: the hold writes no condition of its own, and the
// phase is decided from the agent gateway, shell sandbox and credential broker,
// so the CR reads Ready with no A2A gateway in the namespace.
//
// This is a characterisation test, and it is written to fail in both
// directions. Loosening the gate to one ready replica breaks the "withheld"
// assertion; making the hold observable on the CR breaks the phase assertion.
// Either of those is a real decision, and when it is taken this test and the
// Risk & Rollout paragraph move together, which is the point of pinning it.
func TestAPartlyReadyCalloutStillWithholdsANewGateway(t *testing.T) {
	agent := a2aTestAgent()
	r, cl, req := a2aGateTestReconciler(t, agent,
		readyGateway(agent), shellSandbox(agent, 1), credentialBroker(agent, 1))
	ctx := context.Background()

	for i := 0; i < 2; i++ {
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d: %v", i+1, err)
		}
	}

	// More than one replica is the premise. At one the condition and the gate
	// would agree with the queue group and there would be nothing to pin, so
	// read it off the render rather than assuming the number.
	calloutKey := types.NamespacedName{Name: a2aCalloutName(agent), Namespace: agent.Namespace}
	dep := &appsv1.Deployment{}
	if err := cl.Get(ctx, calloutKey, dep); err != nil {
		t.Fatalf("get callout Deployment: %v", err)
	}
	rendered := int32(1)
	if dep.Spec.Replicas != nil {
		rendered = *dep.Spec.Replicas
	}
	if rendered < 2 {
		t.Fatalf("the callout renders at %d replicas; this test is about the gap between one replica serving and every replica ready, and at one there is no gap", rendered)
	}

	// One of them up, on the current template, and the Deployment controller
	// has seen the current spec. The only thing short of serving is the
	// second pod.
	dep.Status.ObservedGeneration = dep.Generation
	dep.Status.Replicas = rendered
	dep.Status.ReadyReplicas = 1
	dep.Status.UpdatedReplicas = 1
	if err := cl.Status().Update(ctx, dep); err != nil {
		t.Fatalf("update callout status: %v", err)
	}

	// Two passes for the deferred condition write, the same reason
	// letTheGatewayThrough takes two, and the Job kept complete across them so
	// the requeue below is the gate's arm and not provisioning's.
	for i := 0; i < 2; i++ {
		completeTheProvisionJob(t, ctx, cl, agent)
		if _, err := r.Reconcile(ctx, req); err != nil {
			t.Fatalf("Reconcile %d at one ready replica: %v", i+1, err)
		}
	}

	fresh := &agentv1alpha1.PlatformAgent{}
	if err := cl.Get(ctx, req.NamespacedName, fresh); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	cond := meta.FindStatusCondition(fresh.Status.Conditions, busCredentialsReadyCondition)
	if cond == nil || cond.Status != metav1.ConditionFalse {
		t.Fatalf("BusCredentialsReady = %+v at one of two replicas ready, want False; the rest of this test would prove nothing", cond)
	}

	gwKey := types.NamespacedName{Name: a2aGatewayName(agent), Namespace: agent.Namespace}
	if err := cl.Get(ctx, gwKey, &appsv1.Deployment{}); !errors.IsNotFound(err) {
		t.Errorf("the A2A gateway exists (err=%v) with one of two callout replicas ready; if the gate was deliberately loosened to one ready replica, the Risk & Rollout paragraph about what a partly-ready callout costs a new install has to change with it", err)
	}

	// The half that used to be silent. The three workloads the phase was computed
	// from are all up, so before readSplitWorkloads counted the A2A gateway an
	// operator watching `kubectl get platformagent` saw a healthy install with no
	// dispatcher -- Ready: True sitting directly beside the False condition read
	// above it. The hold is unchanged; what the phase says about it is not.
	if fresh.Status.Phase != "Provisioning" {
		t.Errorf("phase = %q with the A2A gateway withheld, want %q; if the gateway was deliberately taken back out of Ready, the Risk & Rollout paragraph about what a partly-ready callout costs a new install has to change with it", fresh.Status.Phase, "Provisioning")
	}
	ready := meta.FindStatusCondition(fresh.Status.Conditions, "Ready")
	if ready == nil {
		t.Fatalf("no Ready condition, so there is nothing for an operator to read")
	}
	if !strings.Contains(ready.Message, a2aGatewayName(agent)) {
		t.Errorf("the Ready message is %q; it has to name %s, because naming the object is the whole difference between a phase that says converging and a phase that says which describe to run", ready.Message, a2aGatewayName(agent))
	}
}
